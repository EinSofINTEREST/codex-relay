"""`codex app-server` stdio JSON-RPC 클라이언트 — 하위 프로세스 하나와 응답 대기만 맡는다."""

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable
from typing import Any, BinaryIO

log = logging.getLogger("codex-relay")
CALL_TIMEOUT_S = 60


class RpcError(Exception):
    """app-server 가 error 로 응답했거나 응답 전에 끝났다."""


class AppServer:
    def __init__(
        self,
        argv: list[str],
        on_message: Callable[[dict[str, Any]], None],
        on_exit: Callable[["AppServer"], None],
        stderr: BinaryIO,
    ) -> None:
        self._on_message = on_message
        self._on_exit = on_exit
        self._pending: dict[int, queue.Queue[dict[str, Any] | None]] = {}
        self._lock = threading.Lock()
        # 쓰기 전용 잠금 — 파이프가 차서 쓰기가 막혀도 reader 가 _lock 을 잡을 수 있어야 한다
        self._wlock = threading.Lock()
        self._next_id = 0
        # Windows 의 codex 는 npm shim (codex.cmd) 이라 PATHEXT 를 따르는 which 로 풀어야 찾는다.
        # 못 찾으면 원래 이름 그대로 넘겨 Popen 의 FileNotFoundError 로 실패를 드러낸다
        exe = shutil.which(argv[0]) or argv[0]
        self.proc = subprocess.Popen(
            [exe, *argv[1:], "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            # 콘솔 없이 분리된 데몬이 콘솔 앱을 띄우면 창이 뜬다
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self._read, name="app-server-reader", daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("non-JSON line from app-server: %r", raw[:200])
                    continue
                if "method" not in msg and "id" in msg:
                    with self._lock:
                        q = self._pending.pop(msg["id"], None)
                    if q is None:
                        log.warning("response for unknown request id %r", msg["id"])
                    else:
                        q.put(msg)
                    continue
                try:
                    self._on_message(msg)
                except Exception:  # 메시지 하나의 처리 실패로 reader 가 죽으면 모든 wait 가 멈춘다 — 기록하고 계속
                    log.exception("handling app-server message %s failed", msg.get("method"))
        finally:
            # 어떤 이유로 끝나든 대기 중 호출을 풀고 종료를 알린다
            with self._lock:
                pending, self._pending = list(self._pending.values()), {}
            for q in pending:
                q.put(None)
            self._on_exit(self)

    def _write(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        with self._wlock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any], timeout: float = CALL_TIMEOUT_S) -> Any:
        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1)
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = q
        try:
            self._write({"id": rid, "method": method, "params": params})
            resp = q.get(timeout=timeout)
        except (OSError, queue.Empty) as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise RpcError(f"{method}: {type(e).__name__} {e}") from e
        if resp is None:
            raise RpcError(f"{method}: app-server exited")
        if "error" in resp:
            raise RpcError(f"{method}: {resp['error'].get('message', resp['error'])}")
        return resp.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"method": method} if params is None else {"method": method, "params": params})

    def respond(self, rid: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        self._write({"id": rid, "error": error} if error is not None else {"id": rid, "result": result})

    def close(self) -> None:
        # app-server 는 stdin 이 닫히면 스스로 끝난다 (실측). 안 끝나면 kill.
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError as e:
            log.warning("closing app-server stdin: %s", e)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            log.warning("app-server did not exit on stdin close; killing")
            if os.name == "nt":
                # shim 체인 (cmd → node → codex.exe) 이라 proc.kill 만으로는 손자가 남는다 — 트리째 죽인다
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                    check=False,
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            self.proc.kill()  # taskkill 이 실패해도 wait 가 영원히 막히지 않게 직계는 확실히 끝낸다
            self.proc.wait()
