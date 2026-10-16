# Telegram 선택 질문 보완 · Claude 인계 후 Codex

2026-10-16 CEO 요청: 검증 안내가 불명확해 Codex가 Claude 인계서를 받아 직접 처리.
기준 main b95488f. Claude 정본 보고서: agent-hub/reports/20261016-telegram-codex-acceptance-claude.md.

## 문제·변경

기존 smoke는 Plan 모드에서 질문을 강제했다. 실제 기본 모드에서는 blocking request_user_input이 비활성이라 도구 호출이 실패했고, 모델이 request_user_input_async를 대신 사용했다. 이 도구는 agentMessage.questions 배열을 출력한다. bridge가 그 배열을 처리하지 않아 선택 버튼이 생기지 않았다.

- 설치된 Codex 0.162.1에서 확인한 default_mode_request_user_input 기능을 **전용 app-server 실행 인자에만** 활성화한다. 다른 Codex/Orca나 전역 설정은 바꾸지 않는다. 이 기능은 현재 under development라 버전 변경 시 동일 smoke로 확인해야 한다.
- 생성·복구 시 같은 질문 지침을 주입한다. 작업을 멈춰야 하는 선택은 blocking 도구를 사용한다. 도구 오류 시 글로 묻고 멈추며 버튼 전송을 주장하지 않는다.
- 비동기 agentMessage.questions도 실제 선택 버튼으로 변환한다. 답은 같은 thread에 전달하고 원래 turn이 진행 중이면 exact turn에 steer, 완료되었으면 후속 turn을 시작한다. 비동기 질문을 blocking 질문이라고 표시하지 않는다.
- 비동기 질문이 turn 종료 후에도 남아 있으면 WAITING으로 표시한다. 만료 후 미처리 질문/활성 turn이 없으면 IDLE로 복귀한다.
- 후속 turn이 시작되면 이전 미답 질문과 큐에 남은 답을 무효화한다. 그 답변 자체가 시작한 새 turn은 제외한다. 중복·오래된 버튼과 응답 유실 시 자동 재전송 금지는 유지한다.

## 검증

- tests/smoke_codex_bridge.py의 Plan 강제를 제거하고 서비스와 같은 실행 인자로 시험했다.
- 실제 기본 모드의 구조화 질문 → FakeTelegram callback → **동일 turn** 재개, 최종 alpha 통과.
- 같은 smoke에서 명령 승인 전 파일 없음/승인 후 생성, 거절 후 파일 없음도 통과. 이 smoke의 Telegram은 모의이며 실제 폰과 구분한다.
- 신규 회귀: 비동기 버튼 생성·turn 완료 후 유지·동일 thread 재개·정확한 steer·오래된 질문·전송 실패·만료 후 유휴 복귀·후속 turn 완료 후 오래된 버튼·큐 답변 무효화·자기 답변이 유발한 새 turn 보존.
- Claude가 확인한 실제 폰 승인/거절 성공은 그대로 인정한다. 선택 버튼 결함만 다시 검증한다.
- Claude 재시작 시험 뒤 실제 trial c-adfc0e69가 UNKNOWN인 상태에서 일반 지시를 API 경로로 시도: 거절되고 codex_commands 건수 불변. alpha.txt 0 bytes, mtime_ns 1792122628898985243. 배포 후 명시 복구와 문맥·mtime 보존을 추가 확인한다.

검토: 아스트라의 읽기 보완 검토에서 비동기 만료 후 영구 WAITING과 후속 turn 완료 후 옛 질문 재활성화 2건을 발견해 회귀 시험과 함께 보완.

## 운영·남은 경계

기존 Orca 독립 세션은 이동/재개하지 않는다. 실제 프로젝트 등록과 Orca 동일 서버 공유는 별도 범위다. 실제 폰에서는 마지막 선택 버튼 한 번만 확인하도록 안내하고 단계 코드나 여러 명령을 한꺼번에 요구하지 않는다.

근거: [OpenAI app-server](https://developers.openai.com/codex/app-server/), [비동기 질문 이벤트 생성 코드](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/request_user_input_async.rs).
