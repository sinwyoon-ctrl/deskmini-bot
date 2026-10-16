# 사용자 조작 없는 Telegram Codex 자동 시험

2026-10-16 CEO: “내가 직접 할 필요 없이 직접 여러가지 해볼 수는 없을까?”
서버에서 가능한 시험은 에이전트가 직접 실행하고 결과를 보고한다. 실제 사용자 판단이나 물리적인 휴대폰 화면 확인이 필요한 경우만 별도로 구분한다.

## 실행·결과

DeskMini의 독립 systemd transient 작업 `telegram-autonomous-20261016.service`로 실행했다. 실행을 요청한 SSH 연결은 종료됐고 작업은 서버에서 계속 진행됐다. 실제 Codex app-server와 임시 Git 작업공간·임시 SQLite DB·FakeTelegram을 사용했다. 운영 봇/DB/프로젝트에는 시험 입력을 보내지 않았다.

**두 차례 실행 모두 14개 시나리오 통과.** 첫 실행은 약 55.5초이며 증거는 `telegram-autonomous-run-20261016.jsonl`·`.json`이다. 아스트라 검토 후 판정 기준을 보강한 두 번째 실행(`telegram-autonomous-verified-20261016.service`)의 최종 근거는 `telegram-autonomous-verified-20261016.jsonl`·`.json`이다.

두 번째 실행에서는 원본 테스트 파일의 해시 불변을 확인하고, 모델 작업공간 밖에서 별도 Python 검사 3개도 실행했다. 중복 클릭에 대한 실제 WebSocket 응답 프레임을 계수해 1개임을 확인했으며, 같은 대화·같은 turn 유지 조건도 명시적으로 단언했다. 봇 객체를 재생성할 때도 격리 경로를 그대로 주입했다.

| 자동 시나리오 | 확인한 결과 |
|---|---|
| 자연어로 코드 수정 | 실제 합계 버그 수정, 원본 테스트 불변, unittest 3개와 외부 독립 검사 3개 통과 |
| 같은 대화 후속 지시 | README에서 읽은 임의 작업번호를 문맥으로 기억 |
| 동일 Telegram update 재수신 | 추가 명령 0 |
| 봇 객체 재생성 | 같은 임시 DB에서 선택 대화와 문맥 유지 |
| 비인가 사용자 버튼 | 질문 미소비 |
| 선택 질문·중복 클릭 | 실제 blocking 질문 답변 후 같은 turn 완료, 실제 응답 프레임 1개 측정 |
| 질문 메시지에 자유 답장 | gamma 답변이 같은 thread에 전달 |
| 명령 실행 승인 | 승인 전 파일 없음, 승인 후 예상 내용의 파일 생성 |
| 명령 실행 거절 | 거절한 파일 없음 |
| 두 대화와 선택 전환 | 접수 당시 작업 고정, 서로 다른 thread 문맥 분리 |
| backend 실제 프로세스 재시작 | UNKNOWN 상태, 복구 전 일반 지시 미실행 |
| 재시작 전 버튼 재사용 | 닫힌 질문 재사용 거절 |
| 명시 복구 | 동일 thread·문맥·파일 해시 유지, 승인 파일 수정 시각 유지 |
| outbox 처리 | 모의 Telegram 메시지 67건 처리, 실제 Telegram API 호출 0 |

완료 후 운영 세 서비스 active, 기존 실제 trial 대화 c-adfc0e69는 IDLE/최종 alpha 유지. 임시 시험 socket 잔존 0.

## 재사용

```bash
cd /home/ionenet/deskmini-bot
LIVE_CODEX_AUTONOMOUS=1 .venv/bin/python tests/autonomous_codex_bridge.py \
  --report /home/ionenet/work/telegram-autonomous-새이름.json
```

보고서와 같은 이름의 `.jsonl`도 생성한다. 기존 증거를 덮어쓰지 않으므로 재실행할 때 새 이름을 쓴다. 실제 로그인된 Codex의 모델 사용을 포함한다. 운영 서비스 재시작 없이 테스트용 app-server 프로세스만 재시작하고, 시험이 만든 프로세스 그룹만 종료한다.

## 판정 범위

이번 14개 시험의 Telegram 전송·버튼 클릭은 모의다. 실제 휴대폰 버튼 왕복은 앞선 `docs/telegram-phone-pass-20261016.md`에서 별도로 통과했다. 봇 객체 재생성은 OS 서비스 재시작과 구분한다. backend 프로세스 재시작은 실제 수행했다. 메인 PC 전원을 실제로 끄는 시험이나 기존 Orca 독립 세션 이관은 수행하지 않았다.

복구의 과거 명령 보존은 기존 ACKED 행과 파일 해시·승인 파일 수정 시각을 대조한 근거다. 과거의 모든 읽기 동작이 절대 반복되지 않았다는 전수 추적까지 주장하지 않는다.
