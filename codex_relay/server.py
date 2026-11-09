"""데몬 프로세스 — 단일 실행 잠금, 루프백 TCP + 토큰 (줄 단위 JSON), 명령 분배."""

import json
import logging
import os
import secrets
import shlex
import socketserver
import threading
from pathlib import Path
from typing import IO, Any

from . import commands
from .relay import Relay

if os.name == "nt":
    import msvcrt
else:
    import fcntl

log = logging.getLogger("codex-relay")
DEFAULT_IDLE_S = 1800
DEFAULT_GRACE_S = 15


def state_home() -> Path:
    return Path(os.environ.get("CODEX_RELAY_HOME") or Path.home() / ".local" / "state" / "codex-relay").absolute()


# Windows 에 AF_UNIX 가 없어 모든 플랫폼에서 루프백 TCP 를 쓴다. 포트가 임시라 CLI 는 이 파일에서 포트와 토큰을 읽는다
ENDPOINT_NAME = "endpoint.json"


def endpoint_path(home: Path) -> Path:
    return home / ENDPOINT_NAME


def _write_endpoint(path: Path, port: int, token: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)  # O_CREAT 는 이미 있는 파일의 권한을 바꾸지 않는다
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token}, f)
    os.replace(tmp, path)


def _try_lock(f: IO[str]) -> bool:
    try:
        if os.name == "nt":
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except PermissionError:  # msvcrt.locking 은 잠금 경합을 EACCES 로 알린다
        if os.name != "nt":
            raise
        return False
    return True


def codex_argv(raw: str, windows: bool = os.name == "nt") -> list[str]:
    if not windows:
        return shlex.split(raw)
    # posix 모드는 Windows 경로의 백슬래시를 이스케이프로 먹는다. 비 posix 모드는 따옴표를 토큰에 남겨 직접 뗀다
    return [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "'\"" else a for a in shlex.split(raw, posix=False)]


# 인증 전 요청 줄의 상한 — 루프백 TCP 는 누구나 연결할 수 있어 토큰 없는 연결이 스레드를 붙잡지 못하게 한다.
# send 본문이 요청 줄에 들어가므로 크기는 넉넉하게
DEFAULT_READ_TIMEOUT_S = 10
MAX_REQUEST_BYTES = 16 * 1024 * 1024


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    relay: Relay
    token: str
    read_timeout: float


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def handle(self) -> None:
        self.connection.settimeout(self.server.read_timeout)
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        except TimeoutError:
            log.warning("dropped connection: no request line within %ss", self.server.read_timeout)
            return
        if len(line) > MAX_REQUEST_BYTES:
            log.warning("dropped connection: request line exceeds %s bytes", MAX_REQUEST_BYTES)
            return
        # 인증 뒤에는 시간 제한이 없다 — wait 는 서버 쪽에서 오래 막힌 뒤에 응답을 쓴다
        self.connection.settimeout(None)
        try:
            req = json.loads(line)
            token = str(req.pop("token", ""))
        except Exception as e:  # 깨진 요청 하나가 데몬을 죽이지 않게 기록하고 응답한다
            log.warning("malformed request: %s", type(e).__name__)
            self._reply(3, f"relay error: {type(e).__name__}: {e}")
            return
        if not secrets.compare_digest(token.encode(), self.server.token.encode()):
            log.warning("rejected request with bad token")  # 토큰 값은 남기지 않는다
            self._reply(3, "relay rejected request: bad token")
            return
        relay = self.server.relay
        with relay.cv:
            relay.clients += 1
        try:
            code, out = dispatch(relay, req)
        except Exception as e:  # 요청 하나의 실패가 데몬 전체를 죽이지 않게 기록하고 응답한다
            log.exception("request failed")
            code, out = 3, f"relay error: {type(e).__name__}: {e}"
        finally:
            with relay.cv:
                relay.clients -= 1
                relay.cv.notify_all()
        self._reply(code, out)

    def _reply(self, code: int, out: str) -> None:
        self.wfile.write((json.dumps({"code": code, "out": out}, ensure_ascii=False) + "\n").encode())


def dispatch(relay: Relay, req: dict[str, Any]) -> tuple[int, str]:
    cmd = req.get("cmd")
    if cmd == "start":
        return commands.start(relay, req)
    if cmd == "status":
        if req.get("id") and relay.get(req["id"]) is None:
            return 2, f"unknown task id: {req['id']}"
        return commands.status(relay, req.get("id"))
    task = relay.get(str(req.get("id")))
    if task is None:
        return 2, f"unknown task id: {req.get('id')}"
    if cmd == "wait":
        return commands.wait(relay, task)
    if cmd == "send":
        return commands.send(relay, task, str(req["text"]))
    if cmd == "stop":
        return commands.stop(relay, task)
    return 2, f"unknown command: {cmd}"


def _positive_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = float(raw)  # 잘못된 값이면 데몬 기동 실패로 드러낸다
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {raw!r}")
    return value


def main() -> int:
    home = state_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    # "w" 로 열면 잠금을 쥔 다른 데몬의 잠금 파일을 잘라 낸다
    lock = open(home / "lock", "a")  # noqa: SIM115 — 잠금은 프로세스가 끝날 때까지 쥐고 있어야 한다
    lock.seek(0)  # msvcrt.locking 은 현재 위치부터 잠근다 — 모든 데몬이 같은 바이트를 겨루게
    if not _try_lock(lock):
        return 0  # 다른 데몬이 이미 떠 있거나 뜨는 중 — CLI 는 그쪽에 붙는다
    logging.basicConfig(
        filename=home / "daemon.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s"
    )
    endpoint = endpoint_path(home)
    endpoint.unlink(missing_ok=True)  # 잠금을 쥐었으니 남은 파일은 죽은 데몬의 것이다
    server = _Server(("127.0.0.1", 0), _Handler)
    server.token = secrets.token_hex(32)
    server.read_timeout = _positive_env("CODEX_RELAY_READ_TIMEOUT", DEFAULT_READ_TIMEOUT_S)
    (home / "daemon.pid").write_text(f"{os.getpid()}\n")
    relay = Relay(
        home,
        codex_argv(os.environ.get("CODEX_RELAY_CODEX_BIN") or "codex"),
        _positive_env("CODEX_RELAY_IDLE", DEFAULT_IDLE_S),
        _positive_env("CODEX_RELAY_GRACE", DEFAULT_GRACE_S),
    )
    server.relay = relay
    _write_endpoint(endpoint, server.server_address[1], server.token)
    log.info("daemon started pid=%s", os.getpid())

    def shutdown() -> None:
        # endpoint 를 먼저 지워 새 CLI 가 이 데몬에 붙지 않고 새 데몬을 띄우게 한다
        endpoint.unlink(missing_ok=True)
        server.shutdown()

    threading.Thread(target=relay.watch, args=(shutdown,), name="watch", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        relay.close()
        server.server_close()
        endpoint.unlink(missing_ok=True)
        (home / "daemon.pid").unlink(missing_ok=True)
        log.info("daemon stopped")
    return 0
