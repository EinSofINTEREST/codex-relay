"""codex-relay CLI — 인자 검증 후 데몬 (없으면 띄움) 에 요청 하나를 보내고 결과를 출력한다."""

import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .server import endpoint_path, state_home

USAGE = """usage: codex-relay start <read|write|render> <workdir> <brief-file> <out-dir>
       codex-relay wait <id>
       codex-relay send <id> <msg-file>
       codex-relay stop <id>
       codex-relay status [id]"""
CONNECT_TRIES = 50  # 0.2s 간격 — 데몬 기동을 최대 약 10초 기다린다
EOF_RETRIES = 3


class UsageError(Exception):
    pass


def _read_nonempty(path: str, what: str) -> str:
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        raise UsageError(f"{what} missing or empty: {path}")
    return p.read_text(encoding="utf-8")


def build_request(argv: list[str]) -> dict[str, Any]:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "start" and len(args) == 4:
        mode, workdir, brief, out = args
        if mode not in ("read", "write", "render"):
            raise UsageError(f"mode must be read, write or render, got {mode!r}")
        if not Path(workdir).is_dir():
            raise UsageError(f"workdir not a directory: {workdir}")
        _read_nonempty(brief, "brief file")
        raw = os.environ.get("CODEX_TIMEOUT", "1800")
        if not raw.isdigit() or int(raw) == 0:
            raise UsageError("CODEX_TIMEOUT must be positive integer seconds")
        return {
            "cmd": "start",
            "mode": mode,
            "workdir": str(Path(workdir).resolve()),
            "brief": str(Path(brief).resolve()),
            "out_dir": str(Path(out).resolve()),
            "timeout_s": int(raw),
            "effort": os.environ.get("CODEX_EFFORT") or None,
        }
    if cmd in ("wait", "stop") and len(args) == 1:
        return {"cmd": cmd, "id": args[0]}
    if cmd == "send" and len(args) == 2:
        text = _read_nonempty(args[1], "msg-file")
        # 파일 끝 개행은 답의 일부가 아니다
        return {"cmd": "send", "id": args[0], "text": text.rstrip("\n")}
    if cmd == "status" and len(args) <= 1:
        return {"cmd": "status", "id": args[0] if args else None}
    raise UsageError(USAGE)


def _spawn_daemon(home: Path) -> subprocess.Popen[bytes]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = Path(__file__).resolve().parent.parent / "bin" / "codex-relay"
    if os.name == "nt":
        # DETACHED_PROCESS 는 콘솔이 없어, uv 런처가 띄우는 실제 python (콘솔 앱) 이 새 콘솔 창을 연다.
        # 숨은 콘솔 (CREATE_NO_WINDOW) 은 자식 · 손자 (python → app-server → codex) 에게 상속돼 창이 뜨지 않는다
        detach: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        detach = {"start_new_session": True}
    with open(home / "daemon.stderr", "ab") as err:
        return subprocess.Popen(
            [sys.executable, "-I", str(entry), "__daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            cwd=home,
            # 데몬은 cwd 가 달라 상대 경로를 다르게 풀므로 절대 경로로 넘긴다
            env={**os.environ, "CODEX_RELAY_HOME": str(home)},
            **detach,
        )


def _try_connect(home: Path) -> tuple[socket.socket, str] | None:
    try:
        ep = json.loads(endpoint_path(home).read_text(encoding="utf-8"))
        return socket.create_connection(("127.0.0.1", int(ep["port"]))), str(ep["token"])
    except (FileNotFoundError, ConnectionRefusedError, PermissionError):
        # 파일 없음 = 데몬 없음, 연결 거부 = 죽은 데몬이 남긴 파일.
        # PermissionError: Windows 에서 데몬이 지우거나 바꾸는 중인 파일을 열면 난다 — 재시도로 넘긴다
        return None


def connect(home: Path) -> tuple[socket.socket, str]:
    """데몬에 붙어 (소켓, 토큰) 을 돌려준다. 데몬이 없으면 띄운다."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    proc: subprocess.Popen[bytes] | None = None
    for _ in range(CONNECT_TRIES):
        conn = _try_connect(home)
        if conn is not None:
            return conn
        # 띄운 데몬이 잠금 경쟁에 져서 (또는 종료 중인 데몬과 겹쳐) 끝났으면 다시 띄운다
        if proc is None or proc.poll() is not None:
            proc = _spawn_daemon(home)
        time.sleep(0.2)
    raise ConnectionError(f"relay daemon did not come up; see {home / 'daemon.log'} and {home / 'daemon.stderr'}")


def request(req: dict[str, Any]) -> dict[str, Any]:
    home = state_home()
    # wait · status 만 다시 보낸다 — start · send 를 다시 보내면 turn 이 두 번 시작될 수 있다
    tries = EOF_RETRIES if req["cmd"] in ("wait", "status") else 1
    for _ in range(tries):
        s, token = connect(home)
        with s:
            s.sendall((json.dumps({**req, "token": token}, ensure_ascii=False) + "\n").encode())
            line = s.makefile("rb").readline()
        if line:
            resp: dict[str, Any] = json.loads(line)
            return resp
    raise ConnectionError("relay daemon closed the connection without a reply")


def main(argv: list[str]) -> int:
    if argv == ["__daemon"]:
        from .server import main as daemon_main

        return daemon_main()
    # Windows 는 파이프 출력을 로캘 (cp949 등) 로 인코딩한다 — 한국어 요약을 플랫폼과 무관하게 UTF-8 로 낸다
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    try:
        req = build_request(argv)
    except UsageError as e:
        print(f"codex-relay: {e}", file=sys.stderr)
        return 2
    try:
        resp = request(req)
    except (ConnectionError, OSError) as e:
        print(f"codex-relay: {e}", file=sys.stderr)
        return 3
    code, out = int(resp["code"]), str(resp["out"])
    # wait 요약은 실패여도 stdout — 메인은 백그라운드 완료 알림의 stdout 을 읽는다
    print(out, file=sys.stdout if code == 0 or req["cmd"] == "wait" else sys.stderr)
    return code
