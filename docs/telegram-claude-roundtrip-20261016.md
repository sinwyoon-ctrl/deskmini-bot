# 실제 Claude 질문·답변 왕복 — 2026-10-16

CEO가 코딩 지원용 Opus 5.5 사용을 허용한 뒤 별도 Orca worktree·Run·coordinator를 만들어 검증했다. 운영 봇 DB·기존 작업자·서비스는 변경하지 않았다.

## 확인한 것

1. 도구를 끈 Claude CLI 호출의 실제 `modelUsage`에서 **claude-opus-5-5**를 확인했다. 별도 네이티브 worker의 launch requested/effective도 같은 모델명이다. 둘은 별개 근거이며 네이티브 provider projection의 model은 null이었다.
2. 실제 Claude가 `orca orchestration ask`로 alpha/beta 선택 질문을 보냈다. Telegram 어댑터의 격리 DB에 PENDING으로 도착했으며 이 시점에는 결과 파일이 없었다.
3. FakeTelegram을 통해 `/orca reply`로 **“alpha나 beta 대신 gamma로 해줘. 출력 문구는 대문자 GAMMA로 써줘.”**를 전달했다.
4. 같은 작업자가 `CLAUDE_PROBE.json`에 chosen=gamma, rendered=GAMMA, 받은 답변 원문을 저장했다. 드라이버가 JSON을 읽어 외부 단언으로 확인했다.
5. 네이티브 worker_done succeeded와 해당 dispatch의 completed/succeeded/released를 확인했다. 전용 worker는 해제됐고 별도 coordinator도 종료했다. 다른 작업자의 종료·재시작은 없다.

근거: `telegram-opus-model-check-20261016.json`, `telegram-claude-native-20261016.json`, `telegram-claude-observed-20261016.json`. 사용자 판단 입력과 전송은 모의이며 실제 Telegram API 호출은 0회다.

## 중요한 제한

**Claude 업무 질문과 CLI 실행 권한 승인은 서로 다른 경로다.** 이번 업무 질문 왕복은 실제로 통과했지만, 아래 CLI 승인 **5건은 Codex가 명령과 범위를 확인한 뒤 전용 터미널에서 건별 처리했다.** Telegram 승인 카드로 전달된 것이 아니다.

- 격리된 handoff context 읽기
- native coordinator ask 실행
- 시험 결과 파일 쓰기와 Python 검증
- 자신의 coordinator 후속 메시지 확인
- 자신의 worker_done 전송

자동 모드 전환·전역 허용·권한 확인 생략은 사용하지 않았다. 따라서 “사용자 직접 조작 없음”과 “승인까지 완전 무인 처리”를 구분한다. 메인을 끈 상황에서 Claude CLI 권한 승인을 Telegram으로 처리하는 기능은 **아직 미구현**이다. 현재 자연어 진행 조회 개선과 실제 `bot` 개발 공간 등록은 Codex 직접 연결에 적용된다.

## 재사용 드라이버

`LIVE_CLAUDE_ORCA=1 .venv/bin/python tests/probe_claude_orca.py --report <새 경로>.json`

실제 Opus를 시작하므로 명시적 opt-in이다. 동일 보고서 이름은 시작 전에 리소스를 예약해 재실행을 거절한다. 최종 검증·정리까지 성공해야 PASS이며, 실패하면 fixture DB·hub와 native 리소스 참조를 보존한다. 빈 worker 목록을 종료 증거로 쓰지 않는다. 초기 실행 뒤 이 자료 보존·해제 판정을 보강했고, 기존 성공 run에서 정확한 dispatch의 양성 해제 증거를 별도로 재조회했다. 보강 코드의 실패 경로는 회귀 시험으로 검증했다.

다음 구현 대상은 Claude CLI 권한 요청의 정식 전달·승인 경로다. 임의 화면 문자열에 자동으로 Yes를 입력하는 방식으로 운영 기능을 만들지 않는다.
