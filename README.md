# codex-relay

Codex `app-server` 를 상주 데몬으로 붙잡고, 작업을 시작 · 대기 · 이어 보내기 · 중단하는 대화형 중계기다.
질문 (`requestUserInput` 또는 결과 블록 `상태: 질문`) 이 오면 끝내지 않고 답을 받아 같은 thread 에서 이어간다.
Claude Code 플러그인으로 설치하면 `bin/` 이 PATH 에 들어가 `codex-relay` 를 이름만으로 실행할 수 있다. Windows · Linux · macOS 지원.

## 설치

```bash
claude plugin marketplace add EinsofINTEREST/codex-relay
claude plugin install codex-relay@codex-relay
```

## 전제

- [uv](https://docs.astral.sh/uv/) — 진입점이 `uv run --script` shebang 이다. 외부 PyPI 의존성은 없다 (Python 3.12 이상).
- `codex` CLI 가 PATH 에 있고 로그인돼 있을 것.

## 명령

```text
codex-relay start <read|write|render> <workdir> <brief-file> <out-dir>   작업 시작, 작업 id 출력 후 즉시 반환
codex-relay wait <id>                                              질문 · 완료 · 실패까지 대기
codex-relay send <id> <msg-file>                                   답 · 다음 turn · 추가 지시
codex-relay stop <id>                                              중단
codex-relay status [id]                                            조회
```

`wait` 종료 코드: 0 완료 · 2 인자 오류 · 3 turn 실패 · 4 결과 블록 없음 · 5 질문 대기 · 6 중단 · 124 시간 초과.
`<out-dir>` 은 작업마다 새로 써야 한다. 보고는 `<out-dir>/last.md` 에 남는다.

## 모드 · 샌드박스

| 모드 | 쓰기 범위 |
|---|---|
| `read` | 읽기 전용 샌드박스 |
| `write` | `<workdir>` 와 `/tmp` 아래만 |
| `render` | `write` 와 같고 네트워크 허용 — UI 렌더 확인용 (headless 브라우저 · 로컬 서버). 외부 접속 금지는 브리프로 제한한다 |

## 환경 변수

| 변수 | 뜻 |
|---|---|
| `CODEX_TIMEOUT` | turn 단위 실행 제한 (초, 질문 대기 제외, 기본 1800) |
| `CODEX_EFFORT` | 추론 강도 |
| `CODEX_RELAY_IDLE` | 데몬 idle 종료 (초) |
| `CODEX_RELAY_GRACE` | 종료 유예 (초) |
| `CODEX_RELAY_READ_TIMEOUT` | app-server 읽기 제한 (초) |
| `CODEX_RELAY_CODEX_BIN` | `codex` 실행 파일 (기본 `codex`) |
| `CODEX_RELAY_HOME` | 상태 디렉토리 |

## 상태 디렉토리

기본 `~/.local/state/codex-relay` (플러그인 변수는 Bash 도구 환경에 없어 고정). Codex 대화는 `~/.codex/sessions` 에 남으므로 브리프 · 메시지에 비밀을 넣지 않는다.

작업 색인 원본은 `tasks.v2.json` 이다. 0.2.0 이하가 쓰던 `tasks.json` 은 `tasks.v2.json` 이 없을 때 한 번 가져오고 지우거나 고치지 않는다. 이 버전이 모르는 mode · 필드의 기록 (다른 버전이 쓴 작업) 은 원문 그대로 보존하고, 그 id 를 `send` · `wait` · `status` 로 부르면 그 버전으로 다루라는 오류 (코드 2) 를 낸다. 전체 `status` 목록에는 나오지 않는다.

## 테스트

```bash
uv run python -B -m unittest discover -s tests -v
```
