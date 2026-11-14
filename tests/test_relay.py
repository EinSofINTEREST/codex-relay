"""codex-relay 종단 테스트 — 실제 CLI · 데몬 프로세스에 가짜 app-server 를 꽂아 돌린다.

실행: uv run python -B -m unittest discover -s tests -v
"""

import ctypes
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

HERE = Path(__file__).resolve().parent
ENTRY = HERE.parent / "bin" / "codex-relay"
FAKE = HERE / "fake_app_server.py"
# 콘솔 없는 실행기 (IDE · 에이전트) 에서 돌려도 CLI 가 콘솔 창을 새로 열지 않게 한다. POSIX 는 0 이어야 한다
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
sys.path.insert(0, str(HERE.parent))
from codex_relay import commands
from codex_relay.appserver import AppServer
from codex_relay.relay import Relay
from codex_relay.server import codex_argv
from codex_relay.task import COMPLETED, RUNNING, STOPPED, Task, result_block


def _alive(pid: int) -> bool:
    if os.name == "nt":
        # Windows 의 os.kill(pid, 0) 은 확인이 아니라 TerminateProcess 다
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _cmdline(argv: list[str], windows: bool = os.name == "nt") -> str:
    """CODEX_RELAY_CODEX_BIN 값 조립 — 공백 있는 경로도 codex_argv 가 같은 목록으로 되돌린다."""
    return subprocess.list2cmdline(argv) if windows else shlex.join(argv)


def _rmtree(path: Path) -> None:
    # Windows 는 강제 종료된 프로세스의 파일 핸들을 종료 코드가 보인 뒤에야 닫기도 한다 — 잠깐 재시도한다
    for _ in range(50):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            time.sleep(0.1)
    shutil.rmtree(path)


def _kill(pid: int) -> None:
    # Windows 에는 SIGKILL 이 없다 — os.kill 은 어떤 신호든 TerminateProcess 로 강제 종료한다
    os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


def _wait_dead(pid: int, seconds: float = 10) -> bool:
    for _ in range(int(seconds * 10)):
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


class RelayTest(unittest.TestCase):
    def setUp(self) -> None:
        # CLI 는 경로를 resolve 한다 — Windows 러너의 8.3 짧은 TEMP (RUNNER~1) 와 비교가 어긋나지 않게 맞춘다
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-relay-test-")).resolve()
        # 긴 상태 디렉토리 회귀 — 유닉스 소켓 시절 108바이트 제한에 걸렸던 경로 길이다
        self.home = self.tmp / ("h" * 100) / "home"
        self.workdir = self.tmp / "wd"
        self.workdir.mkdir()
        self.env = {
            **os.environ,
            "CODEX_RELAY_HOME": str(self.home),
            "CODEX_RELAY_IDLE": "60",
            "CODEX_RELAY_CODEX_BIN": _cmdline([sys.executable, "-I", str(FAKE)]),
        }
        self.env.pop("CODEX_TIMEOUT", None)
        self.env.pop("CODEX_EFFORT", None)
        self.env.pop("CODEX_WEB_SEARCH", None)

    def tearDown(self) -> None:
        pidfile = self.home / "daemon.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text(encoding="utf-8"))
            if _alive(pid):
                _kill(pid)
            self.assertTrue(_wait_dead(pid), "daemon still alive")
        # 데몬이 죽으면 stdin EOF 로 가짜 app-server 도 끝나야 한다
        for pid in self._fake_pids():
            ok = _wait_dead(pid)
            if not ok:
                _kill(pid)
            self.assertTrue(ok, f"fake app-server {pid} outlived daemon")
        _rmtree(self.tmp)

    def _fake_pids(self) -> list[int]:
        f = self.home / "fake.pids"
        return [int(x) for x in f.read_text(encoding="utf-8").split()] if f.exists() else []

    def run_cli(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        p = subprocess.run(
            [sys.executable, "-I", str(ENTRY), *args],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=env or self.env,
            timeout=60,
            creationflags=NO_WINDOW,
        )
        return p.returncode, p.stdout, p.stderr

    def write(self, name: str, text: str) -> str:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def start(self, brief: str, out: str = "out", env: dict[str, str] | None = None, mode: str = "read") -> tuple[str, Path]:
        rc, so, se = self.run_cli(
            "start", mode, str(self.workdir), self.write(out + ".md", brief), str(self.tmp / out), env=env
        )
        self.assertEqual(rc, 0, se)
        return so.strip(), self.tmp / out

    def wait(self, tid: str, code: int) -> str:
        rc, so, se = self.run_cli("wait", tid)
        self.assertEqual(rc, code, so + se)
        return so

    def send(self, tid: str, text: str, code: int = 0) -> None:
        rc, so, se = self.run_cli("send", tid, self.write("msg.txt", text))
        self.assertEqual(rc, code, so + se)

    def events(self, out: Path) -> list[dict[str, Any]]:
        return [json.loads(x)["msg"] for x in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_a_start_wait_completed(self) -> None:
        tid, out = self.start("hello")
        self.assertIn("completed (상태: 완료)", self.wait(tid, 0))
        task = json.loads((out / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (task["id"], task["sandbox"], task["round"], task["codex_version"]), (tid, "read-only", 1, "9.9.9-fake")
        )
        self.assertEqual(task["workdir"], str(self.workdir))
        self.assertTrue((out / "round-1" / "last.md").read_text(encoding="utf-8").startswith("echo: hello"))
        evs = self.events(out)
        self.assertEqual(task["events_line"], len(evs))
        start = next(e for e in evs if e.get("method") == "thread/start")
        self.assertEqual(start["params"]["approvalPolicy"], "never")
        self.assertTrue(start["params"]["config"]["features.default_mode_request_user_input"])
        self.assertFalse([e for e in evs if "delta" in e.get("method", "")])
        self.assertFalse([e for e in evs if e.get("method") == "turn/interrupt"])
        if os.name != "nt":  # Windows 의 st_mode 는 읽기 전용 여부만 담는다
            self.assertEqual(oct(os.stat(self.home / "endpoint.json").st_mode & 0o777), "0o600")
            self.assertEqual(oct(os.stat(self.home).st_mode & 0o777), "0o700")

    def test_bad_token_rejected(self) -> None:
        self.assertEqual(self.run_cli("status")[0], 0)  # 데몬을 띄운다
        ep = json.loads((self.home / "endpoint.json").read_text(encoding="utf-8"))
        for token in ("wrong", None):
            req = {"cmd": "status"} if token is None else {"cmd": "status", "token": token}
            with socket.create_connection(("127.0.0.1", ep["port"])) as s:
                s.sendall((json.dumps(req) + "\n").encode())
                resp = json.loads(s.makefile("rb").readline())
            self.assertEqual(resp, {"code": 3, "out": "relay rejected request: bad token"}, token)
        self.assertNotIn(ep["token"], (self.home / "daemon.log").read_text(encoding="utf-8"))

    def _silent_connection(self, env: dict[str, str]) -> socket.socket:
        """데몬을 띄우고, 요청 줄 (개행) 을 끝내 보내지 않는 연결을 연다."""
        self.assertEqual(self.run_cli("status", env=env)[0], 0)
        ep = json.loads((self.home / "endpoint.json").read_text(encoding="utf-8"))
        s = socket.create_connection(("127.0.0.1", ep["port"]))
        self.addCleanup(s.close)
        s.sendall(b'{"cmd": "status"')
        return s

    def test_silent_connection_dropped_after_read_timeout(self) -> None:
        s = self._silent_connection({**self.env, "CODEX_RELAY_READ_TIMEOUT": "1"})
        s.settimeout(10)
        try:
            data = s.recv(1)
        except ConnectionResetError:
            data = b""
        self.assertEqual(data, b"")  # 응답 없이 끊긴다
        self.assertIn("no request line within", (self.home / "daemon.log").read_text(encoding="utf-8"))

    def test_silent_connection_does_not_block_idle_exit(self) -> None:
        self._silent_connection({**self.env, "CODEX_RELAY_IDLE": "1", "CODEX_RELAY_READ_TIMEOUT": "60"})
        pid = int((self.home / "daemon.pid").read_text(encoding="utf-8"))
        self.assertTrue(_wait_dead(pid, 15), "unauthenticated connection kept daemon alive")

    def test_b_request_user_input(self) -> None:
        tid, out = self.start("ASK me")
        so = self.wait(tid, 5)
        self.assertIn("[color] Color: which color?", so)
        self.assertTrue((out / "question.json").exists())
        self.send(tid, "blue\n")
        self.assertIn("completed", self.wait(tid, 0))
        self.assertIn('"color": {"answers": ["blue"]}', (out / "last.md").read_text(encoding="utf-8"))
        self.assertFalse((out / "question.json").exists())
        self.assertEqual(json.loads((out / "task.json").read_text(encoding="utf-8"))["round"], 1)

    def test_b_multiple_questions_need_json(self) -> None:
        tid, out = self.start("ASK2")
        self.wait(tid, 5)
        self.send(tid, "just text", code=2)
        self.send(tid, '{"a": "x"}', code=2)
        self.send(tid, '{"a": "x", "b": "y"}')
        self.wait(tid, 0)
        self.assertIn('"b": {"answers": ["y"]}', (out / "last.md").read_text(encoding="utf-8"))

    def test_c_result_block_question_new_turn(self) -> None:
        tid, out = self.start("QBLOCK")
        self.assertIn("상태: 질문", self.wait(tid, 5))
        self.send(tid, "use sqlite")
        self.wait(tid, 0)
        self.assertEqual(json.loads((out / "task.json").read_text(encoding="utf-8"))["round"], 2)
        self.assertTrue((out / "round-1" / "last.md").exists())
        self.assertIn("echo: use sqlite", (out / "round-2" / "last.md").read_text(encoding="utf-8"))

    def test_d_turn_failed(self) -> None:
        tid, _ = self.start("FAIL")
        so = self.wait(tid, 3)
        self.assertIn("model exploded", so)
        self.assertIn("send", so)

    def test_e_timeout_interrupts(self) -> None:
        tid, out = self.start("SLOW", env={**self.env, "CODEX_TIMEOUT": "1"})
        self.wait(tid, 124)
        self.assertTrue(any(e.get("method") == "turn/interrupt" for e in self.events(out)))

    def test_f_stop(self) -> None:
        tid, _ = self.start("SLOW")
        rc, so, se = self.run_cli("stop", tid)
        self.assertEqual(rc, 6, so + se)
        self.wait(tid, 6)

    def test_g_daemon_restart_resumes_thread(self) -> None:
        tid, out = self.start("SLOW")
        thread = json.loads((out / "task.json").read_text(encoding="utf-8"))["thread_id"]
        _kill(int((self.home / "daemon.pid").read_text(encoding="utf-8")))
        first_fake = self._fake_pids()[0]
        self.assertTrue(_wait_dead(first_fake))
        self.assertIn("turn lost", self.wait(tid, 3))  # 이 호출이 데몬을 다시 띄운다
        self.send(tid, "again")
        self.wait(tid, 0)
        self.assertIn("echo: again", (out / "last.md").read_text(encoding="utf-8"))
        resume = [e for e in self.events(out) if e.get("method") == "thread/resume"]
        self.assertEqual([r["params"]["threadId"] for r in resume], [thread])
        self.assertEqual(json.loads((out / "task.json").read_text(encoding="utf-8"))["thread_id"], thread)

    def test_h_other_server_requests_rejected(self) -> None:
        tid, out = self.start("APPROVAL")
        self.wait(tid, 0)
        self.assertIn("approval reply had error: True", (out / "last.md").read_text(encoding="utf-8"))
        self.assertTrue(any("error" in e and "id" in e for e in self.events(out)))

    def test_steer_running_turn(self) -> None:
        tid, _ = self.start("SLOW")
        self.send(tid, "hurry")
        self.assertIn("completed", self.wait(tid, 0))

    def test_status_values(self) -> None:
        tid, _ = self.start("PARTIAL")
        self.assertIn("부분 완료", self.wait(tid, 0))
        tid2, _ = self.start("NOBLOCK", out="out2")
        self.wait(tid2, 4)
        self.assertEqual(self.run_cli("wait", "t-nope")[0], 2)
        self.assertEqual(self.run_cli("start", "exec", str(self.workdir), "x", "y")[0], 2)
        rc, so, _ = self.run_cli("status")
        self.assertEqual((rc, len(so.splitlines())), (0, 2))

    def _foreign_records(self) -> dict[str, dict[str, Any]]:
        """다른 버전이 쓴 색인 기록 — 모르는 mode 하나, 아는 mode 에 모르는 필드 하나."""
        base = {"workdir": str(self.workdir), "brief": "/nonexistent/b.md", "status": "failed", "round": 1,
                "events_line": 0, "codex_version": None, "timeout_s": 60, "effort": None, "turn_id": None,
                "result": None, "error": "x", "question": None}
        return {
            "t-future": {**base, "id": "t-future", "thread_id": "th-f", "mode": "future-mode",
                         "out_dir": str(self.tmp / "of"), "field_from_newer_version": 1},
            "t-extra": {**base, "id": "t-extra", "thread_id": "th-e", "mode": "read",
                        "out_dir": str(self.tmp / "oe"), "field_from_newer_version": [1, 2]},
            # 생성자는 받지만 저장 스키마 밖인 내부 필드 — 일반 작업으로 들이면 저장 때 사라진다
            "t-internal": {**base, "id": "t-internal", "thread_id": "th-i", "mode": "read",
                           "out_dir": str(self.tmp / "oi"), "elapsed": 123.5},
        }

    def _assert_foreign_refused(self, foreign: dict[str, dict[str, Any]]) -> None:
        for tid, rec in foreign.items():
            for args in (("send", tid, self.write("m.txt", "again")), ("wait", tid), ("status", tid)):
                rc, so, se = self.run_cli(*args)
                self.assertEqual(rc, 2, so + se)
                self.assertNotIn("unknown task id", so + se)
                self.assertIn(f"recorded by another codex-relay version (mode {rec['mode']!r})", so + se)
        rc, so, se = self.run_cli("status")
        self.assertEqual(rc, 0, se)
        self.assertFalse([t for t in foreign if t in so], so)

    def test_foreign_index_records_survive_save(self) -> None:
        # 다른 버전 데몬이 쓴 기록이 이 데몬의 색인 저장으로 지워지면 안 된다 (#5)
        foreign = self._foreign_records()
        self.home.mkdir(parents=True)
        (self.home / "tasks.v2.json").write_text(json.dumps(foreign), encoding="utf-8")
        tid, _ = self.start("hello")
        self.wait(tid, 0)
        index = json.loads((self.home / "tasks.v2.json").read_text(encoding="utf-8"))
        self.assertEqual({k: index.get(k) for k in foreign}, foreign)
        self.assertEqual(index[tid]["status"], COMPLETED)
        self._assert_foreign_refused(foreign)

    def test_legacy_tasks_json_imported_and_left_untouched(self) -> None:
        old, _ = self.start("hello")
        self.wait(old, 0)
        _kill(int((self.home / "daemon.pid").read_text(encoding="utf-8")))
        legacy = json.loads((self.home / "tasks.v2.json").read_text(encoding="utf-8"))
        foreign = self._foreign_records()
        # 0.2.0 이하가 남긴 상태 — 구 색인만 있다
        (self.home / "tasks.v2.json").unlink()
        raw = json.dumps({**legacy, **foreign})
        (self.home / "tasks.json").write_text(raw, encoding="utf-8")
        self.send(old, "again")  # 이 호출이 데몬을 다시 띄운다
        self.wait(old, 0)
        index = json.loads((self.home / "tasks.v2.json").read_text(encoding="utf-8"))
        self.assertEqual(index[old]["round"], 2)
        self.assertEqual({k: index.get(k) for k in foreign}, foreign)
        self.assertEqual((self.home / "tasks.json").read_text(encoding="utf-8"), raw)
        self._assert_foreign_refused(foreign)

    def test_legacy_import_written_without_state_change(self) -> None:
        # 가져온 뒤 저장 없이 내려가면, 그 사이 구 데몬이 tasks.json 을 다시 써 기록이 사라진다 (#5)
        old, _ = self.start("hello", mode="render")
        self.wait(old, 0)
        _kill(int((self.home / "daemon.pid").read_text(encoding="utf-8")))
        v2 = self.home / "tasks.v2.json"
        raw = v2.read_text(encoding="utf-8")
        v2.rename(self.home / "tasks.json")
        self.assertEqual(self.run_cli("status")[0], 0)  # 데몬을 띄워 가져오기만 한다
        self.assertEqual(json.loads(v2.read_text(encoding="utf-8")), json.loads(raw))
        _kill(int((self.home / "daemon.pid").read_text(encoding="utf-8")))
        (self.home / "tasks.json").unlink()  # 구 데몬이 render 기록을 지운 상황
        rc, so, se = self.run_cli("status", old)
        self.assertEqual(rc, 0, so + se)
        self.assertIn(f"{old} completed", so)

    def test_missing_codex_binary_fails_fast(self) -> None:
        env = {**self.env, "CODEX_RELAY_CODEX_BIN": str(self.tmp / "no-such-codex")}
        rc, so, se = self.run_cli(
            "start", "read", str(self.workdir), self.write("b.md", "hi"), str(self.tmp / "o"), env=env
        )
        self.assertEqual(rc, 3, so + se)
        self.assertIn("cannot start app-server", se)
        task_id = json.loads((self.home / "tasks.v2.json").read_text(encoding="utf-8")).popitem()[0]
        self.assertIn("start failed: cannot start app-server", self.wait(task_id, 3))

    def test_question_wait_excluded_from_timeout(self) -> None:
        # 질문 전후 실행 1.5 + 1.5 = 3초 < 4초. 질문에서 3초 머문 시간까지 세면 초과한다
        tid, _ = self.start("ASKLATE", env={**self.env, "CODEX_TIMEOUT": "4"})
        self.wait(tid, 5)
        time.sleep(3)
        self.send(tid, "blue")
        self.wait(tid, 0)

    def test_running_time_before_question_counts(self) -> None:
        # 질문 전후 실행 합 3초 > 2초 — 질문 전 실행 시간을 잊으면 각 구간 1.5초라 초과하지 않는다
        tid, _ = self.start("ASKLATE", env={**self.env, "CODEX_TIMEOUT": "2"})
        self.wait(tid, 5)
        self.send(tid, "blue")
        self.wait(tid, 124)

    def kill_daemon(self) -> None:
        pid = int((self.home / "daemon.pid").read_text(encoding="utf-8"))
        _kill(pid)
        self.assertTrue(_wait_dead(pid))

    def thread_config(self, mode: str, out: str, resume: bool = False) -> dict[str, Any]:
        tid, path = self.start("hello", out=out, mode=mode)
        self.wait(tid, 0)
        if resume:
            self.kill_daemon()
            self.send(tid, "again")  # 새 데몬이 색인에서 복구해 resume 한다
            self.wait(tid, 0)
        method = "thread/resume" if resume else "thread/start"
        params = next(e for e in self.events(path) if e.get("method") == method)["params"]
        self.assertEqual(params["sandbox"], "workspace-write" if mode != "read" else "read-only")
        return params["config"]

    def test_render_mode_enables_network(self) -> None:
        self.assertTrue(self.thread_config("render", "r1")["sandbox_workspace_write.network_access"])

    def test_render_mode_network_survives_daemon_restart(self) -> None:
        self.assertTrue(self.thread_config("render", "r2", resume=True)["sandbox_workspace_write.network_access"])

    def test_read_and_write_modes_have_no_network_key(self) -> None:
        for mode in ("read", "write"):
            for resume in (False, True):
                config = self.thread_config(mode, f"n-{mode}-{int(resume)}", resume=resume)
                self.assertNotIn("sandbox_workspace_write.network_access", config)

    def test_web_search_in_start_and_resume_config(self) -> None:
        self.env["CODEX_WEB_SEARCH"] = "live"
        self.assertEqual(self.thread_config("read", "w1")["web_search"], "live")
        self.assertEqual(self.thread_config("read", "w2", resume=True)["web_search"], "live")

    def test_web_search_unset_has_no_key(self) -> None:
        for resume in (False, True):
            self.assertNotIn("web_search", self.thread_config("read", f"nw{int(resume)}", resume=resume))

    def test_web_search_invalid_value_rejected(self) -> None:
        env = {**self.env, "CODEX_WEB_SEARCH": "yes"}
        rc, _, se = self.run_cli("start", "read", str(self.workdir), self.write("b.md", "x"), str(self.tmp / "o"), env=env)
        self.assertEqual(rc, 2, se)
        self.assertIn("CODEX_WEB_SEARCH", se)

    def test_web_search_empty_value_rejected(self) -> None:
        env = {**self.env, "CODEX_WEB_SEARCH": ""}
        rc, _, se = self.run_cli("start", "read", str(self.workdir), self.write("b.md", "x"), str(self.tmp / "o"), env=env)
        self.assertEqual(rc, 2, se)

    def test_unparseable_daemon_version_is_unverified(self) -> None:
        for bad in ("0.2.x", "0.3.0rc1", "", 2):
            with self.subTest(version=bad):
                self._set_daemon_version(bad)
                rc, _, se = self.run_cli("status")
                self.assertEqual(rc, 0, se)
                self.assertIn(f"relay daemon version {bad!r} differs", se)
                env = {**self.env, "CODEX_WEB_SEARCH": "live"}
                rc, _, se = self.run_cli("start", "read", str(self.workdir), self.write("b.md", "x"), str(self.tmp / "o"), env=env)
                self.assertEqual(rc, 2, se)

    def _set_daemon_version(self, version: object) -> None:
        self.assertEqual(self.run_cli("status")[0], 0)  # 데몬을 띄운다
        path = self.home / "endpoint.json"
        ep = json.loads(path.read_text(encoding="utf-8"))
        ep.pop("version", None)
        if version is not None:
            ep["version"] = version
        path.write_text(json.dumps(ep), encoding="utf-8")

    def test_old_daemon_rejects_web_search_request(self) -> None:
        self._set_daemon_version(None)  # 0.2.1 데몬 흉내 — endpoint 에 버전이 없다
        env = {**self.env, "CODEX_WEB_SEARCH": "live"}
        rc, so, se = self.run_cli("start", "read", str(self.workdir), self.write("b.md", "x"), str(self.tmp / "o"), env=env)
        self.assertEqual(rc, 2, so + se)
        self.assertIn("restart the daemon when no task is running", se)
        self.assertFalse((self.tmp / "o").exists())  # 데몬에 요청이 가지 않았다

    def test_daemon_version_mismatch_warns_but_proceeds(self) -> None:
        self._set_daemon_version("0.2.2.1")
        tid, _ = self.start("hello")
        self.wait(tid, 0)
        rc, _, se = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("relay daemon version '0.2.2.1' differs from CLI", se)

    def test_same_daemon_version_no_warning(self) -> None:
        self.assertEqual(self.run_cli("status")[0], 0)
        rc, _, se = self.run_cli("status")
        self.assertEqual((rc, se), (0, ""))

    def test_index_record_without_web_search_loads(self) -> None:
        # 0.2.1 이 쓴 기록 — web_search 필드가 없어도 정상 작업이다
        tid, _ = self.start("hello")
        self.wait(tid, 0)
        self.kill_daemon()
        path = self.home / "tasks.v2.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data[tid]["web_search"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.send(tid, "again")
        self.wait(tid, 0)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved[tid]["web_search"], None)

    def test_tampered_copy_does_not_widen_sandbox(self) -> None:
        tid, out = self.start("SLOW")
        self.kill_daemon()
        copy = json.loads((out / "task.json").read_text(encoding="utf-8"))
        copy.update(sandbox="danger-full-access", mode="write", workdir="/")
        (out / "task.json").write_text(json.dumps(copy))
        self.send(tid, "again")  # 새 데몬이 색인에서 복구해 resume 한다
        self.wait(tid, 0)
        resume = next(e for e in self.events(out) if e.get("method") == "thread/resume")
        self.assertEqual((resume["params"]["sandbox"], resume["params"]["cwd"]), ("read-only", str(self.workdir)))
        self.assertEqual(json.loads((out / "task.json").read_text(encoding="utf-8"))["sandbox"], "read-only")

    def test_out_dir_reuse_rejected(self) -> None:
        tid, out = self.start("hello")
        self.wait(tid, 0)
        rc, so, se = self.run_cli("start", "read", str(self.workdir), self.write("b2.md", "x"), str(out))
        self.assertEqual(rc, 2, so + se)
        self.assertIn(f"out-dir already used by task {tid}", se)

    def test_unconfirmed_stop_restarts_app_server(self) -> None:
        env = {**self.env, "CODEX_RELAY_GRACE": "1"}
        tid, out = self.start("STUCK", env=env)
        waiter = subprocess.Popen(
            [sys.executable, "-I", str(ENTRY), "wait", tid], env=self.env, stdout=subprocess.PIPE, creationflags=NO_WINDOW
        )
        rc, so, se = self.run_cli("stop", tid)
        self.assertEqual(rc, 6, so + se)
        self.assertIn("app-server restarted", so + se)
        self.assertEqual(waiter.wait(30), 6)
        assert waiter.stdout is not None
        waiter.stdout.close()
        self.send(tid, "again")  # wait 가 풀린 직후 — 재시작은 이미 끝나 있어야 한다
        self.assertTrue(_wait_dead(self._fake_pids()[0]), "stuck app-server not killed")
        self.assertTrue(_alive(int((self.home / "daemon.pid").read_text(encoding="utf-8"))), "daemon should survive restart")
        self.wait(tid, 0)
        self.assertTrue(any(e.get("method") == "thread/resume" for e in self.events(out)))
        self.assertNotIn(" running ", self.run_cli("status")[1])

    def test_unconfirmed_timeout_restarts_app_server(self) -> None:
        tid, _ = self.start("STUCK", env={**self.env, "CODEX_RELAY_GRACE": "1", "CODEX_TIMEOUT": "1"})
        self.assertIn("app-server restarted", self.wait(tid, 124))
        self.send(tid, "again")
        self.wait(tid, 0)
        self.assertTrue(_wait_dead(self._fake_pids()[0]), "stuck app-server not killed")
        self.assertNotIn(" running ", self.run_cli("status")[1])

    def test_unwritable_out_dir_fails_task(self) -> None:
        tid, out = self.start("ASKLATE")  # 질문이 1.5초 뒤에 온다 — 그 사이 out-dir 를 없앤다
        _rmtree(out)
        self.assertIn("No such file or directory", self.wait(tid, 3))

    def test_commentary_only_is_no_result(self) -> None:
        tid, out = self.start("COMMENTARY")
        self.wait(tid, 4)
        self.assertFalse((out / "last.md").exists())

    def test_idle_exit(self) -> None:
        tid, _ = self.start("hello", env={**self.env, "CODEX_RELAY_IDLE": "1"})
        self.wait(tid, 0)
        pid = int((self.home / "daemon.pid").read_text(encoding="utf-8"))
        self.assertTrue(_wait_dead(pid, 15), "daemon did not exit when idle")
        self.assertFalse((self.home / "endpoint.json").exists())


class LateTurnCompletionTest(unittest.TestCase):
    def test_ended_turn_completion_ignored(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="codex-relay-unit-"))
        self.addCleanup(_rmtree, tmp)
        relay = Relay(tmp, ["unused"], 60)
        task = Task(
            id="t-1",
            mode="read",
            sandbox="read-only",
            workdir=str(tmp),
            brief="b",
            out_dir=str(tmp),
            timeout_s=60,
            effort=None,
            thread_id="th",
        )
        relay.tasks[task.id], relay.by_thread["th"] = task, task
        task.begin_round(time.monotonic())
        task.turn_id = "old"
        task.end(STOPPED, "interrupt not confirmed by app-server")  # stop 의 강제 종료 경로
        task.begin_round(time.monotonic())  # send 로 새 turn — turn id 는 아직 모른다

        def completed(turn_id: str) -> dict[str, Any]:
            turn = {
                "id": turn_id,
                "status": "completed",
                "items": [{"type": "agentMessage", "text": "## 결과\n상태: 완료"}],
            }
            return {"method": "turn/completed", "params": {"threadId": "th", "turn": turn}}

        relay._on_message(completed("old"))
        self.assertEqual(task.status, RUNNING)
        relay._on_message(completed("new"))
        self.assertEqual(task.status, COMPLETED)


class StubApp:
    """app-server 대역 — 호출을 (이름, 메서드) 로 기록한다."""

    def __init__(self, name: str, calls: list[tuple[str, str]], delay: float = 0.0) -> None:
        self.name, self.calls, self.delay = name, calls, delay

    def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((self.name, method))
        time.sleep(self.delay)
        return {"thread": {"id": f"th-{len(self.calls)}"}, "turn": {"id": f"turn-{len(self.calls)}"}}

    def respond(self, rid: Any, result: Any = None, error: Any = None) -> None:
        self.calls.append((self.name, "respond"))

    def close(self) -> None:
        self.calls.append((self.name, "close"))


def _stub_relay(case: unittest.TestCase, calls: list[tuple[str, str]], delay: float = 0.0) -> tuple[Relay, Path, Any]:
    """Relay 와 tmp 디렉토리, 그리고 relay.app 이 비면 다음 StubApp 을 띄우는 get_app 대역."""
    tmp = Path(tempfile.mkdtemp(prefix="codex-relay-unit-"))
    case.addCleanup(_rmtree, tmp)
    relay = Relay(tmp, ["unused"], 60, grace_s=0.1)
    names = iter("ABCDEFGH")

    def get_app() -> AppServer:
        with relay.app_lock:
            if relay.app is None:
                relay.app = cast(AppServer, StubApp(next(names), calls, delay))
            return relay.app

    return relay, tmp, get_app


class RestartOrderTest(unittest.TestCase):
    """강제 중단 뒤 wait 가 풀리자마자 온 send 가 옛 app-server 에 turn 을 시작하지 않아야 한다."""

    def _running_task(self, relay: Relay, tmp: Path) -> Task:
        task = Task(
            id="t-1",
            mode="read",
            sandbox="read-only",
            workdir=str(tmp),
            brief="b",
            out_dir=str(tmp),
            timeout_s=60,
            effort=None,
            thread_id="th",
            turn_id="turn-0",
            round=1,
            on_save=relay.save_index,
        )
        relay.tasks[task.id], relay.by_thread["th"] = task, task
        relay.loaded.add("th")
        return task

    def _check(self, force: Any) -> None:
        calls: list[tuple[str, str]] = []
        relay, tmp, get_app = _stub_relay(self, calls)
        task = self._running_task(relay, tmp)
        get_app()  # A 를 띄워 둔다
        real_restart = relay.restart_app

        def slow_restart(cause: str) -> None:
            time.sleep(0.3)  # 상태 공개와 재시작 사이에 끼어들 틈을 넓힌다
            real_restart(cause)

        results: list[tuple[int, str]] = []

        def waiter() -> None:
            commands.wait(relay, task)
            results.append(commands.send(relay, task, "go"))

        with (
            mock.patch.object(relay, "get_app", side_effect=get_app),
            mock.patch.object(relay, "restart_app", side_effect=slow_restart),
        ):
            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.05)
            force(relay, task)
            t.join(10)
        self.assertNotIn(("A", "turn/start"), calls)
        self.assertEqual([r[0] for r in results], [0], results)
        self.assertIn(("B", "thread/resume"), calls)
        self.assertIn(("B", "turn/start"), calls)

    def test_stop_path(self) -> None:
        self._check(lambda relay, task: commands.stop(relay, task))

    def test_timeout_path(self) -> None:
        def force(relay: Relay, task: Task) -> None:
            with relay.cv:
                task.timing_out, task.interrupt_at = True, time.monotonic() - 10
            relay._watch_once()

        self._check(force)


class ConcurrentStartTest(unittest.TestCase):
    def test_same_out_dir_reserved_once(self) -> None:
        calls: list[tuple[str, str]] = []
        relay, tmp, get_app = _stub_relay(self, calls, delay=0.05)
        brief = tmp / "brief.md"
        brief.write_text("hi")
        with mock.patch.object(relay, "get_app", side_effect=get_app):
            for i in range(20):
                req = {
                    "mode": "read",
                    "workdir": str(tmp),
                    "brief": str(brief),
                    "out_dir": str(tmp / f"out{i}"),
                    "timeout_s": 60,
                    "effort": None,
                }
                gate = threading.Barrier(2)
                codes: list[int] = []

                threads = [threading.Thread(target=self._start_at, args=(relay, req, gate, codes)) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(10)
                self.assertEqual(sorted(codes), [0, 2], f"iteration {i}")

    @staticmethod
    def _start_at(relay: Relay, req: dict[str, Any], gate: threading.Barrier, codes: list[int]) -> None:
        gate.wait()
        codes.append(commands.start(relay, req)[0])


class ConcurrentSendTest(unittest.TestCase):
    def test_two_sends_start_one_turn(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="codex-relay-unit-"))
        self.addCleanup(_rmtree, tmp)
        relay = Relay(tmp, ["unused"], 60)
        task = Task(
            id="t-1",
            mode="read",
            sandbox="read-only",
            workdir=str(tmp),
            brief="b",
            out_dir=str(tmp),
            timeout_s=60,
            effort=None,
            thread_id="th",
            status=COMPLETED,
            on_save=relay.save_index,
        )
        relay.tasks[task.id], relay.by_thread["th"] = task, task
        relay.loaded.add("th")
        calls: list[str] = []

        class SlowApp:
            def call(self, method: str, params: dict[str, Any]) -> Any:
                calls.append(method)
                time.sleep(0.3)
                return {"turn": {"id": "new"}}

        results: list[tuple[int, str]] = []
        with mock.patch.object(relay, "get_app", return_value=SlowApp()):
            threads = [
                threading.Thread(target=lambda: results.append(commands.send(relay, task, "x"))) for _ in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(calls, ["turn/start"])
        self.assertEqual(sorted(code for code, _ in results), [0, 3])
        self.assertIn("already starting", next(msg for code, msg in results if code == 3))


class CodexArgvTest(unittest.TestCase):
    def test_windows_quotes_stripped(self) -> None:
        cases = {
            '"C:/Program Files/nodejs/codex.cmd"': ["C:/Program Files/nodejs/codex.cmd"],
            "'C:/Program Files/nodejs/codex.cmd'": ["C:/Program Files/nodejs/codex.cmd"],
            r"C:\tools\codex.cmd --flag": [r"C:\tools\codex.cmd", "--flag"],
            "codex": ["codex"],
        }
        for raw, want in cases.items():
            self.assertEqual(codex_argv(raw, windows=True), want, raw)

    def test_round_trip_with_spaces(self) -> None:
        for windows, argv in (
            (True, [r"C:\Program Files\Python312\python.exe", "-I", r"C:\a b\fake_app_server.py"]),
            (False, ["/opt/my python/bin/python3", "-I", "/tmp/a b/fake_app_server.py"]),
        ):
            self.assertEqual(codex_argv(_cmdline(argv, windows), windows=windows), argv, windows)


class ResultBlockTest(unittest.TestCase):
    def test_values(self) -> None:
        cases = {
            "## 결과\n상태: 완료": "완료",
            "## 결과\n상태: 부분 완료 — 질문 남김": "부분 완료",
            "## 결과\n상태: 질문": "질문",
            "## 결과\n상태: 완료됨": None,
            "상태: 완료": None,
            "## 결과\n상태: 질문\n\n## 결과\n상태: 차단": "차단",
            "## 결과물 목록\n상태: 완료": None,
            "## 결과\n상태 누락\n## 참고\n상태: 완료": None,
            "## 결과\n상태: 완료\n\n## 결과\n상태: 질문": "질문",
            "## 결과  \n상태: 완료": "완료",
        }
        for text, want in cases.items():
            self.assertEqual(result_block(text)[0], want, text)


if __name__ == "__main__":
    unittest.main()
