# Telegram ↔ Codex 실시간 대화 연결

2026-10-16 · CEO 승인: 기존 봇을 살리고 공개 구현과 공식 app-server 규격을 참고해 연결. 아스트라 설계·코드 보완 검토.

기존 비서 대화·Orca 작업 조회·알림·파일·작업 발주 기능을 유지한다. 선택한 Codex 대화에 자연어 지시를 보내고, 진행·최종 응답·구조화된 질문·명령/파일/권한 승인을 같은 Telegram 대화에서 받는 경로를 추가했다.

`Telegram → 기존 인증/중복방지 → 같은 SQLite의 명령 → 별도 codex bridge → 전용 Unix WebSocket app-server`

질문과 진행은 기존 durable outbox를 통해 돌아온다. Telegram polling 주기를 최대 2초로 제한해 수신이 없어도 outbox를 전달한다. 기존 chief sandbox 권한은 확장하지 않는다. Codex 연결은 별도 서비스 하나와 `websockets==15.0.1`을 사용한다. 외부 포트·추가 봇 토큰·CLI shim은 없다. CLI `app-server proxy`는 raw byte 중계라 Unix WebSocket 변환에 사용하지 않는다.

## 사용

- `코덱스로 연결시험 README 읽고 요약해줘` → 등록 프로젝트를 골라 새 대화 연결. 이후 일반 문장은 같은 작업으로 전달.
- `/codex chat` 또는 `/codex status` → 연결·작업 상태와 선택 ID.
- `/codex new trial 지시 내용` → 새 연결. `/codex use c-…` → 기존 연결 선택.
- 구조화된 질문 → 선택 버튼, 질문 메시지에 답장, 또는 `/codex answer q-… 답변`.
- 실행 승인 → 해당 질문의 승인·거절/중단 버튼. 일반 “응”을 실행 승인으로 사용하지 않는다.
- `/codex stop` → 접수할 때 실행 중이던 turn만 중단.
- `/assistant` 또는 `/비서` → 기존 비서 대화. Codex 작업은 유지.
- bridge 재시작으로 UNKNOWN이 된 연결: `/codex recover c-…`로 대화만 복구한 뒤 다음 지시. 이전 지시 자동 재실행 없음.
- 인자 없는 기존 `/codex` 사용량 조회와 `/status`, `/orca` 등 기존 명령은 유지.

현재 등록 프로젝트는 **격리 시험 저장소 `trial`**이다. 추가 실사용 프로젝트는 `config/codex.toml`에 개발 작업공간과 범위를 등록한다. 운영·관리자 화면은 계속 읽기 전용. 기존 독립 실행 Orca 세션은 상태 조회 대상으로 유지하며, 파일에서 thread ID를 찾았다는 이유로 자동 재개하지 않는다. 같은 전용 socket에 TUI가 연결되는 Orca 화면 공유·양쪽 동시 승인 경합은 아직 검증 완료가 아니다.

## 중복·승인 처리

메시지 접수 때 session과 expected turn을 고정한다. `QUEUED → SENDING → ACKED`; 응답 유실은 UNKNOWN으로 남기며 자동 재전송하지 않는다. 질문은 server epoch/request/thread/turn에 연결한다. 중복 클릭은 DB에서 원자적으로 차단한다. 다른 작업을 선택한 뒤에도 질문 메시지에 답장하면 원래 질문을 대상으로 한다. `serverRequest/resolved`는 요청 정리를 포함하므로 승인 실행 완료로 단정하지 않는다.

명령·작업 경로·추가 권한·네트워크 대상·파일 변경 전체가 화면 한도 안에 들어갈 때만 승인 버튼을 제공한다. 범위가 잘리면 상세 확인을 안내하고 승인은 제공하지 않는다. 요청이 허용한 decision만 사용한다. 비밀값 질문은 Telegram으로 답하지 않는다. 질문은 10분 후 만료되어 해당 turn에 중단을 요청한다.

## 검증

`tests/test_codex_bridge.py`는 대상 고정, 질문 답장, 승인 중복·세대·만료, 승인 범위 표시, 요청 상태 경합, 중단 대상 경합, 응답 유실과 재생을 점검한다.

`LIVE_CODEX_SMOKE=1 .venv/bin/python tests/smoke_codex_bridge.py`는 실제 로그인된 Codex를 사용하되 **FakeTelegram·임시 DB·임시 Git 작업공간**으로 격리한다. Plan 모드에서 구조화된 질문을 강제하고, untrusted 승인 정책으로 명령 승인·거절을 강제한다. 운영 기본 정책은 on-request다. 실제 휴대폰 왕복과 구분한다.

초기 실측에서 파일 쓰기 후 별도 확인 명령도 승인을 요구해 시험이 기다렸다. 이후 한 명령에 쓰기/확인을 묶고, 후속 승인도 분리 처리하도록 시험을 보완했다. 거절 선택지가 `decline`이 아닌 `cancel`인 요청도 실제 확인해 버튼과 시험을 수정했다.

## 근거

- [OpenAI app-server 규격](https://developers.openai.com/codex/app-server/): thread/turn, 진행 이벤트, 승인·사용자 질문.
- [codex-tg](https://github.com/Arslan223/codex-tg): 세션 공유·승인 중계 참고. 특정 alpha 버전 gate 때문에 설치하지 않음.
- [codex-telegram](https://github.com/alendit/codex-telegram): 자연어 대화와 프로젝트/세션 선택 방식 참고. 코드 복사 없이 공식 프로토콜로 구현.
