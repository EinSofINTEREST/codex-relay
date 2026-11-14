"""데몬의 핵심 — 작업 색인, app-server 메시지 반영, 시간 초과 · idle 감시. 명령 처리는 commands.py.

잠금 규칙: 상태는 self.cv 하나로 보호하고, app-server 호출 (call · respond) 은 cv 를 놓고 한다.
reader 스레드가 알림을 반영하려면 cv 가 필요하므로, cv 를 쥔 채 응답을 기다리면 교착한다.
"""

import functools
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .appserver import AppServer, RpcError
from .task import (
    COMPLETED,
    FAILED,
    LOST,
    NETWORK_MODES,
    NO_RESULT,
    PERSISTED,
    RUNNING,
    SANDBOX,
    STATUSES,
    STOPPED,
    TIMEOUT,
    Task,
    result_block,
)

log = logging.getLogger("codex-relay")


class Relay:
    def __init__(self, home: Path, codex_argv: list[str], idle_s: float, grace_s: float = 15) -> None:
        # grace_s: interrupt 뒤 turn/completed 를 기다리는 시간 — 넘으면 app-server 를 재시작해 turn 을 확실히 끝낸다
        self.home, self.codex_argv, self.idle_s, self.grace_s = home, codex_argv, idle_s, grace_s
        self.cv = threading.Condition()
        self.tasks: dict[str, Task] = {}
        self.foreign: dict[str, dict[str, Any]] = {}  # 이 버전이 모르는 색인 기록 원문 — 기동 때만 채운다
        self.by_thread: dict[str, Task] = {}
        self.loaded: set[str] = set()  # 지금 app-server 에 올라간 thread — 없으면 thread/resume
        self.app: AppServer | None = None
        self.codex_version: str | None = None
        self.app_lock = threading.Lock()
        self.clients = 0
        self.exiting = False
        self.app_dead = False
        self.restarting = False  # True 인 동안 send 를 거절한다 (restart_app)
        self.idle_since = time.monotonic()
        self._load_index()

    # ---- 색인 (원본) · 재시작 복구
    def _index_path(self) -> Path:
        # 0.1.0 · 0.2.0 은 tasks.json 을 통째로 다시 쓰며 모르는 기록을 지운다 — 그 데몬과 파일을 나눈다
        return self.home / "tasks.v2.json"

    def _load_index(self) -> None:
        path = self._index_path()
        if not path.exists():
            path = self.home / "tasks.json"  # 구 색인은 가져오기만 하고 지우거나 고치지 않는다
            if not path.exists():
                return
        for tid, rec in json.loads(path.read_text(encoding="utf-8")).items():
            raw = dict(rec)
            mode = rec.pop("mode", None)
            if mode not in SANDBOX or not rec.keys() <= set(PERSISTED):
                # 저장 스키마 밖의 키는 생성자가 받는 내부 필드여도 저장 때 사라진다 — 생성자 예외에 기대지 않는다
                log.warning("keeping foreign task %s untouched: unknown mode %r or fields in index", tid, mode)
                self.foreign[tid] = raw
                continue
            try:
                # sandbox 는 저장값을 믿지 않고 mode 에서 다시 계산한다
                task = Task(**rec, mode=mode, sandbox=SANDBOX[mode])
            except TypeError as e:
                log.warning("keeping foreign task %s untouched: unknown index record: %s", tid, e)
                self.foreign[tid] = raw
                continue
            task.on_save = self.save_index
            self.tasks[tid] = task
            if task.thread_id:
                self.by_thread[task.thread_id] = task
        with self.cv:
            for task in self.tasks.values():
                if task.status not in STATUSES or task.status == RUNNING or task.input_pending():
                    # turn 은 이전 app-server 프로세스와 함께 죽었다
                    self.end_safe(task, LOST, "relay daemon restarted; turn lost")
            if path != self._index_path():
                # 가져오기를 바로 끝낸다 — v2 없이 내려가면 그 사이 구 데몬이 tasks.json 에서 지운 기록을 다시 가져온다
                self.save_index()

    def save_index(self) -> None:
        # 다른 버전이 쓴 기록은 실행 · 신뢰하지 않고 원문 그대로 다시 쓴다 — 그 버전이 이어서 다룰 수 있게
        data = {**self.foreign, **{tid: t.record() for tid, t in self.tasks.items()}}
        tmp = self._index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._index_path())

    def end_safe(self, task: Task, status: str, error: str) -> None:
        """cv 를 쥔 채 부른다. out-dir 쓰기가 실패해도 상태는 메모리 · 색인에 반영하고 실패를 기록한다."""
        try:
            task.end(status, error)  # end 는 쓰기 전에 상태부터 바꾼다
        except OSError as e:
            log.error("task %s: cannot write task files while marking %s: %s", task.id, status, e)
        self.cv.notify_all()

    def abort(self, task: Task, error: str) -> None:
        """cv 를 쥔 채 부른다. 작업을 FAILED 로 끝내고 돌던 turn 이 있으면 interrupt 한다 (best effort)."""
        active = task.status == RUNNING or task.input_pending()
        turn_id, thread_id = task.turn_id, task.thread_id
        self.end_safe(task, FAILED, error)
        if active and turn_id and thread_id:
            # reader 스레드에서 불릴 수 있어 응답 대기는 다른 스레드에서
            threading.Thread(target=self._interrupt_quietly, args=(thread_id, turn_id), daemon=True).start()

    def _interrupt_quietly(self, thread_id: str, turn_id: str) -> None:
        try:
            self.get_app().call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except RpcError as e:
            log.warning("interrupt of %s after abort failed: %s", turn_id, e)

    # ---- app-server
    def get_app(self) -> AppServer:
        with self.app_lock:
            if self.app is None:
                if self.app_dead:
                    raise RpcError("app-server exited; relay daemon is restarting")
                try:
                    with open(self.home / "app-server.stderr", "ab") as stderr:  # Popen 이 fd 를 복제한다
                        app = AppServer(self.codex_argv, self._on_message, self._on_app_exit, stderr)
                except OSError as e:
                    # RpcError 로 바꿔야 호출자가 작업을 FAILED 로 끝낸다 — 아니면 RUNNING 으로 남아 wait 가 영원히 블록한다
                    raise RpcError(f"cannot start app-server {self.codex_argv}: {e}") from e
                try:
                    info = app.call(
                        "initialize", {"clientInfo": {"name": "codex-relay", "title": "codex-relay", "version": "1"}}
                    )
                    app.notify("initialized")
                except RpcError:
                    app.close()
                    raise
                # userAgent = "codex-relay/<codex 버전> (...)"
                self.codex_version = str(info.get("userAgent", "")).split(" ")[0].split("/")[-1] or None
                self.app = app
            return self.app

    def restart_app(self, cause: str) -> None:
        """interrupt 가 확인되지 않은 turn 을 확실히 끝내려고 app-server 를 내린다. 다음 호출이 새로 띄운다.

        끝날 때까지 restarting 으로 send 를 막고, app_lock 을 쥐어 get_app 이 옛 프로세스를 돌려주지 않게 한다.
        cause 작업의 상태 공개 (STOPPED · TIMEOUT) 는 호출자가 이 함수가 돌아온 뒤에 한다.
        """
        with self.cv:
            self.restarting = True
            self.loaded.clear()  # 새 프로세스에서는 thread/resume 부터
            for t in self.tasks.values():
                if t.id != cause and (t.status == RUNNING or t.input_pending()):
                    self.end_safe(t, LOST, f"app-server restarted to force-stop task {cause}")
        try:
            with self.app_lock:
                app, self.app = self.app, None
                if app:
                    log.warning("restarting app-server to force-stop task %s", cause)
                    app.close()
        finally:
            with self.cv:
                self.restarting = False
                self.cv.notify_all()

    def _log(self, task: Task, direction: str, msg: dict[str, Any]) -> None:
        try:
            task.log_event(direction, msg)
        except OSError as e:
            self.abort(task, f"cannot write task files: {e}")
            raise RpcError(f"cannot write task files: {e}") from e

    def rpc(self, task: Task, method: str, params: dict[str, Any]) -> Any:
        app = self.get_app()
        with self.cv:
            self._log(task, "out", {"method": method, "params": params})
        try:
            result = app.call(method, params)
        except RpcError as e:
            with self.cv:
                self._log(task, "in", {"response_to": method, "error": str(e)})
            raise
        with self.cv:
            self._log(task, "in", {"response_to": method, "result": result})
        return result

    def thread_params(self, task: Task) -> dict[str, Any]:
        config: dict[str, Any] = {"features.default_mode_request_user_input": True}
        if task.mode in NETWORK_MODES:
            config["sandbox_workspace_write.network_access"] = True
        if task.effort:
            config["model_reasoning_effort"] = task.effort
        return {"cwd": task.workdir, "sandbox": task.sandbox, "approvalPolicy": "never", "config": config}

    def _on_app_exit(self, app: AppServer) -> None:
        with self.cv:
            # 의도한 종료 · 재시작 (self.app 을 먼저 비운다) 은 데몬을 끝내지 않는다
            if self.exiting or app is not self.app:
                return
            log.error("app-server exited unexpectedly; marking active turns lost and shutting down")
            self.app_dead = True
            for task in self.tasks.values():
                if task.status == RUNNING or task.input_pending():
                    self.end_safe(task, LOST, "app-server exited; turn lost")

    def _on_message(self, msg: dict[str, Any]) -> None:
        method = str(msg.get("method", ""))
        params = msg.get("params") or {}
        reply: Callable[[], None] | None = None
        with self.cv:
            task = self.by_thread.get(params.get("threadId", ""))
            try:
                reply = self._handle(task, msg, method, params)
            except Exception as e:  # 기록하고 작업에 반영 — reader 는 다음 메시지를 계속 처리한다
                log.exception("task %s: handling %s failed", task.id if task else "-", method)
                if task and (task.status == RUNNING or task.input_pending()):
                    self.abort(task, f"relay error handling {method}: {type(e).__name__}: {e}")
            self.cv.notify_all()
        if reply:
            reply()

    def _handle(
        self, task: Task | None, msg: dict[str, Any], method: str, params: dict[str, Any]
    ) -> Callable[[], None] | None:
        if task:
            task.log_event("in", msg)
        if "id" not in msg:
            if task:
                self._apply(task, method, params)
            return None
        if method == "item/tool/requestUserInput" and task and task.status == RUNNING:
            question = {
                "kind": "input",
                "request_id": msg["id"],
                "turn_id": params.get("turnId"),
                "questions": params.get("questions") or [],
            }
            task.set_question(question, time.monotonic())
            return None
        # 승인 등 질문 외 요청은 메인에 넘기지 않고 거부한다 (approvalPolicy never 인데 온 것)
        err = {"code": -32601, "message": f"codex-relay does not handle {method}"}
        log.warning("rejecting server request %s (id %s)", method, msg["id"])
        if task:
            task.log_event("out", {"id": msg["id"], "error": err})
        return functools.partial(self.respond, msg["id"], error=err)

    def respond(self, rid: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        app = self.app
        if app is None:
            log.error("no app-server to respond to request %s", rid)
            return
        try:
            app.respond(rid, result=result, error=error)
        except OSError as e:
            log.error("failed to respond to server request %s: %s", rid, e)

    def _apply(self, task: Task, method: str, params: dict[str, Any]) -> None:
        if (params.get("turnId") or (params.get("turn") or {}).get("id")) in task.ended_turns:
            return
        if method == "turn/started" and task.turn_id is None:
            task.turn_id = params.get("turn", {}).get("id")
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                task.final_text = item.get("text")
        elif method == "serverRequest/resolved":
            q = task.question
            if task.input_pending() and q and q["request_id"] == params.get("requestId"):
                task.resume_running(time.monotonic())
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            active = task.status == RUNNING or task.input_pending()
            if active and (task.turn_id is None or turn.get("id") == task.turn_id):
                self._finish(task, turn)

    def _finish(self, task: Task, turn: dict[str, Any]) -> None:
        if turn.get("id"):
            task.ended_turns.add(turn["id"])
        text = task.final_text
        if text is None:
            # 대체 경로도 최종 답만 — commentary 의 결과 블록을 완료로 읽지 않는다.
            # phase 필드 자체가 없는 메시지는 phase 도입 전 형식으로 보고 허용한다
            msgs = [
                i.get("text")
                for i in turn.get("items") or []
                if i.get("type") == "agentMessage" and i.get("phase", "final_answer") == "final_answer"
            ]
            text = msgs[-1] if msgs else None
        if text:
            task.write_answer(text)
        else:
            (task.out / "last.md").unlink(missing_ok=True)  # 이전 회차의 답을 이번 회차 결과로 오인하지 않게
        status = turn.get("status")
        if status == "completed":
            state, block = result_block(text or "")
            task.result = state
            if state == "질문":
                task.set_question({"kind": "result", "text": block}, time.monotonic())
            else:
                task.end(COMPLETED if state else NO_RESULT)
        elif status == "interrupted" and task.timing_out:
            task.end(TIMEOUT, "turn interrupted on timeout")
        elif status == "interrupted" and task.stopping:
            task.end(STOPPED, "interrupted by stop")
        else:
            err = (turn.get("error") or {}).get("message") or f"turn {status}"
            task.end(FAILED, f"turn failed: {err}")

    # ---- 명령에서 쓰는 조회
    def get(self, tid: str) -> Task | None:
        with self.cv:
            return self.tasks.get(tid)

    # ---- 감시 스레드: 시간 초과 interrupt 와 idle 종료
    def watch(self, shutdown: Callable[[], None]) -> None:
        while True:
            try:
                if self._watch_once():
                    log.info("shutting down (app_dead=%s)", self.app_dead)
                    shutdown()
                    return
            except Exception:  # 한 바퀴의 실패로 감시가 멈추면 시간 초과 · idle 종료가 사라진다 — 기록하고 계속
                log.exception("watch iteration failed")

    def _watch_once(self) -> bool:
        to_interrupt: list[Task] = []
        forced: list[Task] = []
        with self.cv:
            self.cv.wait(0.5)
            now = time.monotonic()
            for t in self.tasks.values():
                # turn id 가 생기기 전에는 시계 (run_started) 도 interrupt 대상도 아직 없다
                if t.status != RUNNING or t.turn_id is None:
                    continue
                if not t.timing_out and t.elapsed + now - t.run_started > t.timeout_s:
                    t.timing_out, t.interrupt_at = True, now
                    to_interrupt.append(t)
                elif t.timing_out and now - t.interrupt_at > self.grace_s:
                    forced.append(t)
            if self.clients or any(t.status == RUNNING for t in self.tasks.values()):
                self.idle_since = now
            done = self.app_dead or now - self.idle_since > self.idle_s
        for t in to_interrupt:
            log.info("task %s exceeded %ss; interrupting", t.id, t.timeout_s)
            try:
                self.rpc(t, "turn/interrupt", {"threadId": t.thread_id, "turnId": t.turn_id})
            except RpcError as e:
                log.warning("timeout interrupt for %s failed: %s", t.id, e)
        for t in forced:
            # 재시작을 먼저, 상태 공개는 뒤에 — stop 과 같은 순서 (restart_app 독스트링)
            self.restart_app(t.id)
            with self.cv:
                if t.status == RUNNING:
                    self.end_safe(t, TIMEOUT, "interrupt not confirmed; app-server restarted")
        return done

    def close(self) -> None:
        with self.cv:
            self.exiting = True
            self.cv.notify_all()
        with self.app_lock:
            app, self.app = self.app, None
        if app:
            app.close()
