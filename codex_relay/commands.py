"""CLI 명령 처리 — start · send · stop · wait · status. 잠금 규칙은 relay.py 머리말과 같다."""

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from .appserver import RpcError
from .relay import Relay
from .task import FAILED, RUNNING, SANDBOX, STOPPED, Task, build_answers

log = logging.getLogger("codex-relay")
Reply = tuple[int, str]


def start(relay: Relay, req: dict[str, Any]) -> Reply:
    out = Path(req["out_dir"]).resolve()
    task = Task(
        id="t-" + secrets.token_hex(4),
        mode=req["mode"],
        sandbox=SANDBOX[req["mode"]],
        workdir=req["workdir"],
        brief=req["brief"],
        out_dir=str(out),
        timeout_s=int(req["timeout_s"]),
        effort=req.get("effort") or None,
        on_save=relay.save_index,
    )
    brief = Path(task.brief).read_text(encoding="utf-8")
    # 한 out-dir 에 작업 둘이면 사본 · 산출물이 섞인다. 검사와 예약을 한 임계 구역에서 — 동시 start 둘이 함께 통과하지 않게
    with relay.cv:
        owner = _out_dir_owner(relay, out)
        if owner:
            return 2, f"out-dir already used by task {owner}: {out}; use a new out-dir"
        relay.tasks[task.id] = task
    try:
        out.mkdir(parents=True, exist_ok=True)
        for stale in ("last.md", "question.json", "events.jsonl"):
            (out / stale).unlink(missing_ok=True)
        with relay.cv:
            task.save()
        result = relay.rpc(task, "thread/start", relay.thread_params(task))
        with relay.cv:
            task.thread_id = result["thread"]["id"]
            task.codex_version = relay.codex_version
            relay.by_thread[task.thread_id] = task
            relay.loaded.add(task.thread_id)
            task.begin_round(time.monotonic())
        _turn_start(relay, task, brief)
    except (RpcError, OSError) as e:
        with relay.cv:
            relay.end_safe(task, FAILED, f"start failed: {e}")
        return 3, f"task {task.id}: start failed: {e}"
    return 0, task.id


def _out_dir_owner(relay: Relay, out: Path) -> str | None:
    """cv 를 쥔 채 부른다. out-dir 를 이미 쓰는 작업 id (색인 또는 남은 task.json) 를 돌려준다."""
    for t in relay.tasks.values():
        if Path(t.out_dir).resolve() == out:
            return t.id
    if not (out / "task.json").exists():
        return None
    try:
        return str(json.loads((out / "task.json").read_text(encoding="utf-8")).get("id")) or "unknown"
    except (OSError, ValueError, AttributeError):
        return "unknown"


def _turn_start(relay: Relay, task: Task, text: str) -> None:
    """begin_round 로 회차를 예약한 뒤에 부른다. 실패는 예외로 — 호출자가 FAILED 로 끝낸다."""
    result = relay.rpc(task, "turn/start", {"threadId": task.thread_id, "input": [{"type": "text", "text": text}]})
    with relay.cv:
        task.turn_id = task.turn_id or result["turn"]["id"]
        task.save()


def send(relay: Relay, task: Task, text: str) -> Reply:
    # 상태 확인과 예약을 cv 한 번 안에서 — 동시에 온 send 가 turn 을 둘 시작하지 않게
    with relay.cv:
        if relay.restarting:
            # 재시작이 끝나기 전에 이으면 곧 내려갈 옛 app-server 에서 turn 이 돈다
            return 3, f"task {task.id}: app-server restart in progress (retry send shortly)"
        if task.input_pending() and task.question:
            question = task.question
            try:
                answers = build_answers(question, text)
            except ValueError as e:
                return 2, str(e)
            try:
                task.log_event("out", {"id": question["request_id"], "result": answers})
                task.resume_running(time.monotonic())
            except OSError as e:
                relay.abort(task, f"cannot write task files: {e}")
                return 3, f"task {task.id}: cannot write task files: {e}"
            relay.cv.notify_all()
            action = "answer"
        elif task.status == RUNNING:
            if task.turn_id is None:
                return 3, f"task {task.id}: a turn is already starting (run wait, then send again)"
            action = "steer"
        elif task.thread_id is None:
            return 3, f"task {task.id}: no thread to continue (thread/start failed)"
        else:
            try:
                task.begin_round(time.monotonic())
            except OSError as e:
                relay.end_safe(task, FAILED, f"cannot write task files: {e}")
                return 3, f"task {task.id}: cannot write task files: {e}"
            action = "resume" if task.thread_id not in relay.loaded else "turn"
    if action == "answer":
        relay.respond(question["request_id"], result=answers)
        return 0, f"task {task.id}: answered question"
    if action == "steer":
        params = {"threadId": task.thread_id, "expectedTurnId": task.turn_id, "input": [{"type": "text", "text": text}]}
        try:
            relay.rpc(task, "turn/steer", params)
        except RpcError as e:
            return 3, f"task {task.id}: steer rejected: {e} (run wait, then send again)"
        return 0, f"task {task.id}: steered running turn"
    try:
        if action == "resume":
            relay.rpc(
                task, "thread/resume", {"threadId": task.thread_id, "excludeTurns": True, **relay.thread_params(task)}
            )
            with relay.cv:
                relay.loaded.add(str(task.thread_id))
                task.codex_version = relay.codex_version
        _turn_start(relay, task, text)
    except (RpcError, OSError) as e:
        with relay.cv:
            relay.end_safe(task, FAILED, f"{action} failed: {e}")
        return 3, f"task {task.id}: {action} failed: {e}"
    return 0, f"task {task.id}: round {task.round} started"


def stop(relay: Relay, task: Task) -> Reply:
    with relay.cv:
        if not (task.status == RUNNING or task.input_pending()):
            relay.end_safe(task, STOPPED, "stopped while idle")
            return task.summary()
        task.stopping = True
        turn_id = task.turn_id
    if turn_id:
        try:
            relay.rpc(task, "turn/interrupt", {"threadId": task.thread_id, "turnId": turn_id})
        except RpcError as e:
            log.warning("interrupt for %s failed: %s", task.id, e)
    deadline = time.monotonic() + relay.grace_s
    with relay.cv:
        while (task.status == RUNNING or task.input_pending()) and time.monotonic() < deadline:
            relay.cv.wait(0.5)
        forced = task.status == RUNNING or task.input_pending()
    if forced:
        # 재시작을 먼저 — 끝날 때까지 작업은 RUNNING 이라 wait 가 풀리지 않고 send 는 거절된다
        relay.restart_app(task.id)
    with relay.cv:
        # 재시작 중 늦은 turn/completed 로 이미 끝났으면 그 결과를 둔다
        if forced and (task.status == RUNNING or task.input_pending()):
            relay.end_safe(task, STOPPED, "interrupt not confirmed; app-server restarted")
        return task.summary()


def wait(relay: Relay, task: Task) -> Reply:
    with relay.cv:
        while task.status == RUNNING and not relay.exiting:
            relay.cv.wait(1.0)
        return task.summary()


def status(relay: Relay, tid: str | None) -> Reply:
    with relay.cv:
        tasks = [relay.tasks[tid]] if tid else list(relay.tasks.values())
        return 0, "\n".join(f"{t.id} {t.status} round={t.round} out={t.out_dir}" for t in tasks)
