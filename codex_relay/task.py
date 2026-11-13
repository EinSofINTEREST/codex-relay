"""작업 하나 (= Codex thread 하나) 의 상태 · 산출물 파일 · wait 요약."""

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUNNING = "running"
QUESTION = "question"
COMPLETED = "completed"
NO_RESULT = "no_result"
FAILED = "failed"
LOST = "lost"
TIMEOUT = "timeout"
STOPPED = "stopped"
STATUSES = (RUNNING, QUESTION, COMPLETED, NO_RESULT, FAILED, LOST, TIMEOUT, STOPPED)
SANDBOX = {"read": "read-only", "write": "workspace-write", "render": "workspace-write"}
# render 는 write 와 같은 쓰기 범위에 네트워크만 더한다 (headless 브라우저 렌더 확인용)
NETWORK_MODES = frozenset({"render"})
# wait 종료 코드 — codex-run.sh 계약 (0 · 3 · 4 · 124) 에 질문 5 · 중단 6 을 더한다
EXIT_CODES = {COMPLETED: 0, NO_RESULT: 4, QUESTION: 5, FAILED: 3, LOST: 3, TIMEOUT: 124, STOPPED: 6}
PERSISTED = (
    "id",
    "thread_id",
    "mode",
    "sandbox",
    "workdir",
    "brief",
    "out_dir",
    "status",
    "round",
    "events_line",
    "codex_version",
    "timeout_s",
    "effort",
    "turn_id",
    "result",
    "error",
    "question",
)
_HEADING = re.compile(r"## 결과\s*")
_STATE_VALUE = re.compile(r"(부분 완료|완료|차단|질문)(?!\w)")


def result_block(text: str) -> tuple[str | None, str]:
    """마지막 '## 결과' 블록의 상태 값과 블록 원문. 상태 줄이 없거나 모르는 값이면 None.

    codex-run.sh 와 같은 규칙: 제목은 '## 결과' 정확 일치 (뒤 공백만 허용), 블록은 다음 '## ' 제목에서 끝난다.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _HEADING.fullmatch(line)]
    if not starts:
        return None, ""
    block = lines[starts[-1] :]
    end = next((i for i, line in enumerate(block) if i > 0 and line.startswith("## ")), len(block))
    block = block[:end]
    for line in block[1:]:
        if line.startswith("상태:"):
            # 값 단위 비교 — '부분 완료 — 질문 남김' 은 질문이 아니다
            m = _STATE_VALUE.match(line[len("상태:") :].strip())
            return (m.group(1) if m else None), "\n".join(block)
    return None, "\n".join(block)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Task:
    id: str
    mode: str
    sandbox: str
    workdir: str
    brief: str
    out_dir: str
    timeout_s: int
    effort: str | None
    thread_id: str | None = None
    status: str = RUNNING
    round: int = 0
    events_line: int = 0
    codex_version: str | None = None
    turn_id: str | None = None
    result: str | None = None
    error: str | None = None
    question: dict[str, Any] | None = None
    # 아래는 데몬 프로세스 안에서만 의미가 있어 저장하지 않는다
    final_text: str | None = field(default=None, repr=False)
    run_started: float = field(default=0.0, repr=False)
    elapsed: float = field(default=0.0, repr=False)
    stopping: bool = field(default=False, repr=False)
    timing_out: bool = field(default=False, repr=False)
    interrupt_at: float = field(default=0.0, repr=False)
    # 끝난 turn — 강제 종료 뒤 늦게 온 이전 turn 의 알림을 새 turn 의 것으로 오판하지 않게
    ended_turns: set[str] = field(default_factory=set, repr=False)
    # 데몬 색인 갱신 — 색인 (상태 디렉토리) 이 원본이고 out-dir 의 task.json 은 사람이 보는 사본이다
    on_save: Callable[[], None] | None = field(default=None, repr=False)

    @property
    def out(self) -> Path:
        return Path(self.out_dir)

    def record(self) -> dict[str, Any]:
        """색인에 저장하는 값. sandbox 는 mode 에서 다시 계산하므로 넣지 않는다."""
        return {k: getattr(self, k) for k in PERSISTED if k != "sandbox"}

    def save(self) -> None:
        if self.on_save:
            self.on_save()  # 원본 먼저 — out-dir 쓰기가 실패해도 색인은 맞다
        data = {k: getattr(self, k) for k in PERSISTED}
        _atomic_write(self.out / "task.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def log_event(self, direction: str, msg: dict[str, Any]) -> None:
        method = str(msg.get("method", ""))
        if "delta" in method.lower():
            return
        line = {"ts": time.time(), "dir": direction, "msg": msg}
        with open(self.out / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.events_line += 1

    def input_pending(self) -> bool:
        return self.status == QUESTION and self.question is not None and self.question["kind"] == "input"

    def begin_round(self, now: float) -> None:
        self.round += 1
        self.status, self.turn_id, self.final_text = RUNNING, None, None
        self.result = self.error = None
        self.run_started, self.elapsed = now, 0.0
        self.stopping = self.timing_out = False
        self.clear_question()
        self.save()

    def set_question(self, question: dict[str, Any], now: float) -> None:
        if self.status == RUNNING:
            # 질문 대기 시간은 실행 시간 제한에서 뺀다
            self.elapsed += now - self.run_started
        self.status, self.question = QUESTION, question
        _atomic_write(self.out / "question.json", json.dumps(question, ensure_ascii=False, indent=2) + "\n")
        self.save()

    def resume_running(self, now: float) -> None:
        self.clear_question()
        self.status, self.run_started = RUNNING, now
        self.save()

    def clear_question(self) -> None:
        self.question = None
        (self.out / "question.json").unlink(missing_ok=True)

    def end(self, status: str, error: str | None = None) -> None:
        if self.turn_id:
            self.ended_turns.add(self.turn_id)
        self.status, self.error = status, error
        if status != QUESTION:
            self.clear_question()
        self.save()

    def write_answer(self, text: str) -> None:
        rdir = self.out / f"round-{self.round}"
        rdir.mkdir(exist_ok=True)
        _atomic_write(rdir / "last.md", text)
        _atomic_write(self.out / "last.md", text)

    def summary(self) -> tuple[int, str]:
        head = f"task {self.id} round {self.round}: {self.status}"
        last = f"last.md: {self.out / 'last.md'}"
        s = self.status
        if s == COMPLETED:
            return 0, f"{head} (상태: {self.result})\n{last}"
        if s == NO_RESULT:
            return 4, f"{head} — no result block (## 결과 + 상태:) in final answer\n{last}"
        if s == QUESTION and self.question:
            return 5, f"{head}\n{_format_question(self.question)}\nanswer with: codex-relay send {self.id} <msg-file>"
        if s in (FAILED, LOST):
            return 3, f"{head} — {self.error}\ncan continue with: codex-relay send {self.id} <msg-file>"
        if s == TIMEOUT:
            return 124, f"{head} — interrupted after {self.timeout_s}s ({self.error or 'timeout'})"
        if s == STOPPED:
            return 6, f"{head}{' — ' + self.error if self.error else ''}"
        return 3, f"{head} — unexpected state"


def _format_question(q: dict[str, Any]) -> str:
    if q["kind"] == "result":
        return f"result block asks:\n{q['text']}"
    out = []
    for item in q["questions"]:
        out.append(f"[{item['id']}] {item.get('header') or ''}: {item['question']}")
        for opt in item.get("options") or []:
            out.append(f"  - {opt['label']}: {opt.get('description') or ''}")
        if item.get("isOther"):
            out.append("  - (free-form answer allowed)")
    if len(q["questions"]) > 1:
        out.append('multiple questions: msg-file must be a JSON object {"<id>": "<answer>"}')
    return "\n".join(out)


def build_answers(question: dict[str, Any], text: str) -> dict[str, Any]:
    """send 의 메시지 파일 내용을 requestUserInput 응답으로. 형식이 틀리면 ValueError."""
    ids = [item["id"] for item in question["questions"]]
    if len(ids) == 1:
        return {"answers": {ids[0]: {"answers": [text]}}}
    try:
        parsed = json.loads(text)
    except ValueError as e:
        raise ValueError(f"multiple questions {ids}: msg-file must be a JSON object ({e})") from e
    if not isinstance(parsed, dict) or set(parsed) != set(ids) or not all(isinstance(v, str) for v in parsed.values()):
        raise ValueError(f"msg-file must be a JSON object with exactly the keys {ids} and string values")
    return {"answers": {k: {"answers": [v]} for k, v in parsed.items()}}
