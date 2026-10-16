# Codex 연결 보완 검토·실측

설계/코드 검토: 아스트라(gpt-6-astra), SSH 읽기 전용. 직접 구현한 주체와 분리. 초기 필수 6건을 보완했고 최종 읽기 검토에서 격리 시험 범위의 새 필수 결함 없음. 리뷰어는 테스트를 재실행하지 않았으며 아래 실측은 구현 세션에서 수행했다.

- turn/start 응답이 후속 질문/완료 이벤트 상태를 되돌리지 않음.
- resolved는 현재 요청과 대기 질문을 확인해 상태를 갱신.
- create 재생 시 원래 focus 복구. 메시지 접수 당시 session/turn 고정.
- Telegram 질문 메시지에 답장하면 focus가 다른 작업이어도 원래 질문으로 연결.
- stop은 접수 당시 turn만 대상으로 함.
- 동시 질문 만료는 CAS·현재 turn 확인·예외 격리.
- 승인 범위 전체 표시; 표시 불가한 승인은 비활성화.
- 독립 heartbeat, 명시적 대화 연결 복구 추가.

실제 설치 CLI 0.162.1 / websockets 15.0.1, 전용 Unix socket initialize 통과.

실제 Codex + FakeTelegram + 임시 DB/작업공간 최종 시험:

| 항목 | 실측 |
|---|---|
| request_user_input 선택 질문 → Telegram callback → 같은 thread의 같은 요청 재개 | PASS, 최종 alpha |
| commandExecution/requestApproval → callback accept → 파일 생성 | PASS |
| 승인 전 파일 없음 / 승인 후 TELEGRAM_CODEX_APPROVED 확인 | PASS |
| 명령 거절/중단 → denied.txt 생성 없음 | PASS |
| 진행·질문·완료 이벤트 수신 | PASS |
| 실제 Telegram 전송 | 수행하지 않음, FakeTelegram 사용 |
| 기존 Orca 독립 세션 자동 이동 | 수행하지 않음 |
| Orca와 Telegram 양쪽 승인 경합 | 미검증 |

전체 단위/회귀 시험 407건 통과. 실행 출력: docs/telegram-codex-tests-20261016.txt. 이전 두 smoke 실행은 시험이 한 번의 승인만 가정하거나 decline만 가정해 대기 시간 초과. 후속 명령 승인 및 서버 availableDecisions의 cancel을 처리하도록 시험/버튼 보완 후 위 전체 왕복 성공. 이전 격리 시험 프로세스는 해당 임시 socket endpoint를 확인해 정리했다. 운영 작업 프로세스에는 입력/중단을 수행하지 않았다.
