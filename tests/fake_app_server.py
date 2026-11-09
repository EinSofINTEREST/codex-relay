"""테스트용 가짜 `codex app-server`. 입력 텍스트의 키워드로 시나리오를 고른다.

ASK · ASK2 (requestUserInput 질문 1 · 2개), ASKLATE (질문 전후로 LATE_S 씩 실행 — 시간 초과 계산 확인용), QBLOCK (결과 블록 '상태: 질문'), FAIL, SLOW (interrupt · steer 까지 안 끝남),
APPROVAL (승인 요청을 보내 거부 응답을 받는다), STUCK (interrupt 를 무시), COMMENTARY (commentary 에만 결과 블록), NOBLOCK (결과 블록 없음), PARTIAL ('부분 완료 — 질문 남김'), 그 밖은 완료.
"""

import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

threads: set[str] = set()
turn: dict[str, Any] = {}  # 진행 중 turn 하나: id · thread · kind · req
next_req = [100]
LATE_S = 1.5
scheduled: list[Any] = []  # [마감 시각, 할 일] — ASKLATE 의 지연 동작 하나


def out(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def note(method: str, **params: Any) -> None:
    out({"method": method, "params": params})


def finish(
    status: str = "completed", text: str | None = None, error: str | None = None, items: list[Any] | None = None
) -> None:
    tid, uid = turn["thread"], turn["id"]
    if text is not None:
        note("item/agentMessage/delta", threadId=tid, turnId=uid, delta=text[:3])
        item = {"type": "agentMessage", "id": "m1", "text": text, "phase": "final_answer"}
        note("item/completed", threadId=tid, turnId=uid, item=item)
    err = {"message": error} if error else None
    note("turn/completed", threadId=tid, turn={"id": uid, "items": items or [], "status": status, "error": err})
    turn.clear()
    scheduled.clear()


def server_request(method: str, params: dict[str, Any]) -> None:
    next_req[0] += 1
    turn["req"] = next_req[0]
    out({"method": method, "id": next_req[0], "params": params})


def ask_color() -> None:
    base = {"threadId": turn["thread"], "turnId": turn["id"], "itemId": "call1"}
    server_request(
        "item/tool/requestUserInput",
        {
            **base,
            "questions": [
                {
                    "id": "color",
                    "header": "Color",
                    "question": "which color?",
                    "isOther": True,
                    "isSecret": False,
                    "options": [{"label": "blue", "description": "pick blue"}],
                }
            ],
        },
    )


def start_turn(tid: str, turn_id: str, text: str) -> None:
    turn.update(id=turn_id, thread=tid)
    note("turn/started", threadId=tid, turn={"id": turn["id"], "status": "inProgress"})
    note("account/rateLimits/updated", rateLimits={})
    base = {"threadId": tid, "turnId": turn["id"], "itemId": "call1"}
    if "ASKLATE" in text:
        turn["kind"] = "asklate"
        scheduled[:] = [time.monotonic() + LATE_S, ask_color]
    elif "ASK2" in text:
        turn["kind"] = "ask"
        server_request(
            "item/tool/requestUserInput",
            {
                **base,
                "questions": [
                    {
                        "id": "a",
                        "header": "A",
                        "question": "first?",
                        "isOther": True,
                        "isSecret": False,
                        "options": None,
                    },
                    {
                        "id": "b",
                        "header": "B",
                        "question": "second?",
                        "isOther": True,
                        "isSecret": False,
                        "options": None,
                    },
                ],
            },
        )
    elif "ASK" in text:
        turn["kind"] = "ask"
        ask_color()
    elif "APPROVAL" in text:
        turn["kind"] = "approval"
        server_request("item/commandExecution/requestApproval", {**base, "command": "rm -rf x"})
    elif "STUCK" in text:
        turn["kind"] = "stuck"
    elif "COMMENTARY" in text:
        msg = {"type": "agentMessage", "id": "c1", "text": "thinking\n\n## 결과\n상태: 완료", "phase": "commentary"}
        note("item/completed", threadId=tid, turnId=turn["id"], item=msg)
        finish(items=[msg])
    elif "SLOW" in text:
        turn["kind"] = "slow"
    elif "QBLOCK" in text:
        finish(text="need input\n\n## 결과\n상태: 질문\n질문: which db?")
    elif "FAIL" in text:
        finish("failed", error="model exploded")
    elif "NOBLOCK" in text:
        finish(text="no block here")
    elif "PARTIAL" in text:
        finish(text="x\n\n## 결과\n상태: 부분 완료 — 질문 남김")
    else:
        finish(text=f"echo: {text}\n\n## 결과\n상태: 완료")


def handle(msg: dict[str, Any]) -> None:
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if method is None:  # 우리 서버 요청에 대한 응답
        if turn.get("kind") == "asklate" and rid == turn.get("req"):
            note("serverRequest/resolved", threadId=turn["thread"], requestId=rid)
            scheduled[:] = [time.monotonic() + LATE_S, lambda: finish(text="late done\n\n## 결과\n상태: 완료")]
        elif turn.get("kind") == "ask" and rid == turn.get("req"):
            note("serverRequest/resolved", threadId=turn["thread"], requestId=rid)
            finish(text=f"answers: {json.dumps(msg['result'], ensure_ascii=False)}\n\n## 결과\n상태: 완료")
        elif turn.get("kind") == "approval" and rid == turn.get("req"):
            finish(text=f"approval reply had error: {'error' in msg}\n\n## 결과\n상태: 완료")
        return
    if method == "initialize":
        out({"id": rid, "result": {"userAgent": "codex-relay/9.9.9-fake (test)"}})
    elif method == "thread/start":
        tid = str(uuid.uuid4())
        threads.add(tid)
        out({"id": rid, "result": {"thread": {"id": tid}, "sandbox": params.get("sandbox")}})
    elif method == "thread/resume":
        threads.add(params["threadId"])  # 디스크에 있는 thread 를 다시 올린 것으로 친다
        out({"id": rid, "result": {"thread": {"id": params["threadId"]}}})
    elif method == "turn/start":
        if params["threadId"] not in threads:
            out({"id": rid, "error": {"code": -32600, "message": "thread not found"}})
            return
        turn_id = str(uuid.uuid4())
        out({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        start_turn(params["threadId"], turn_id, params["input"][0]["text"])
    elif method == "turn/steer":
        if not turn or params["expectedTurnId"] != turn["id"]:
            out({"id": rid, "error": {"code": -32600, "message": "no active turn to steer"}})
            return
        out({"id": rid, "result": {"turnId": turn["id"]}})
        finish(text=f"steered: {params['input'][0]['text']}\n\n## 결과\n상태: 완료")
    elif method == "turn/interrupt":
        out({"id": rid, "result": {}})
        if turn and turn["id"] == params["turnId"] and turn.get("kind") != "stuck":
            if "req" in turn:
                note("serverRequest/resolved", threadId=turn["thread"], requestId=turn["req"])
            finish("interrupted")
    elif rid is not None:
        out({"id": rid, "error": {"code": -32601, "message": f"fake: unknown {method}"}})


def _read_stdin(lines: "queue.Queue[str | None]") -> None:
    for line in sys.stdin:
        lines.put(line)
    lines.put(None)


def main() -> None:
    # 진짜 app-server 처럼 UTF-8 로 주고받는다 — Windows 파이프의 기본은 로캘 인코딩이다
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    home = os.environ.get("CODEX_RELAY_HOME")
    if home:  # 테스트가 남은 프로세스를 확인하도록 pid 를 남긴다
        with open(Path(home) / "fake.pids", "a") as f:
            f.write(f"{os.getpid()}\n")
    # 지연 동작 (scheduled) 을 기다리는 동안에도 interrupt 를 받아야 해서 읽기는 스레드로
    lines: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_read_stdin, args=(lines,), daemon=True).start()
    while True:
        timeout = max(0.0, scheduled[0] - time.monotonic()) if scheduled else None
        try:
            line = lines.get(timeout=timeout)
        except queue.Empty:
            action = scheduled[1]
            scheduled.clear()
            action()
            continue
        if line is None:
            return
        handle(json.loads(line))


if __name__ == "__main__":
    main()
