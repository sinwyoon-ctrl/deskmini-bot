# 클로드 실행 허용을 텔레그램에서 처리

새로 등록한 `bot-claude` 전용 개발 작업공간에서 시작하는 Claude 작업에 적용한다. 기존 코덱스 세션, 다른 클로드 세션, 전역 설정은 변경하지 않는다.

사용 예: `/orca new bot-claude claude README를 읽고 내용을 요약해줘`

클로드가 명령 실행이나 파일 수정을 허락받으려 하면 텔레그램으로 대상 작업, 실행 위치, 명령/파일 내용을 보낸다. **이번 실행 허용 / 거절** 버튼으로 원래 요청 하나에만 답한다. 일반 대화나 “응”을 실행 승인으로 처리하지 않는다. 승인 접수와 실제 실행 결과는 별개다. 결정 질문은 기존 native coordinator ask/reply로 처리하며 선택지 밖의 구술 답변도 전달한다. `/orca status`에 승인 대기도 표시한다.

## 구현과 경계

- 공식 `PermissionRequest` command hook → 전용 SQLite 요청 + 기존 outbox 원자 등록 → 기존 인증 callback → 원래 훅에 `allow` 또는 `deny` JSON 반환. 새 서버·토큰·권한 우회·항상 허용 규칙은 없다.
- 네이티브 Dispatch/Task/Run, 실제 Claude ancestor PID와 시작 시각, 프로세스 환경의 Orca terminal/worktree, 실제 cwd를 확인한다. 첫 SessionStart로 바인딩을 선점할 수 없다. 훅 PID도 확인해 종료된 훅의 버튼을 거절한다.
- 요청마다 새 nonce. 중복 클릭, 다른 사용자/채팅, 교체된 Dispatch, 만료, 봇 재시작은 승인 불가. 승인 전과 반환 직전에 소유권을 다시 확인한다.
- 승인 대기는 최대 10분이며 전체 훅은 시작부터 660초 예산, 호스트 제한은 720초다. 네이티브 조회·등록 지연에 쓴 시간을 승인 대기에서 뺀다. 봇 heartbeat가 60초 넘게 없으면 거절한다.
- 비밀 마스킹 또는 내용 생략이 필요한 요청은 허용 버튼을 제공하지 않는다. 원시 입력은 저장하지 않고 해시와 마스킹한 표시문만 기록한다. `DELIVERED`는 허용 JSON 반환 처리 기록이며 도구 실행 성공 증거가 아니다.
- opt-in 개발 worktree의 `.claude/settings.json`만 생성한다. 이전 설치 digest와 다르면 덮어쓰지 않는다. Git worktree의 `settings.local.json`은 메인 checkout에 연결될 수 있어 사용하지 않는다.

설치된 Claude Code 2.1.292에서 정상 오류·거절·만료는 명시적인 deny와 interrupt를 반환한다. 훅 자체 강제 종료/스크립트 부재는 CLI의 일반 권한 화면으로 돌아갈 수 있다. 자동 허용하지 않지만 휴대폰만으로 그 예외를 해소한다고 보장하지 않는다. 샌드박스 네트워크 승인과 프로젝트 신뢰 화면은 이 훅의 대상이 아니다. 거절로 멈춘 작업은 자동 재개하지 않으며, 네이티브 작업 종료와 후속 착수는 별도로 확인한다.

공식 근거: [Hooks reference](https://code.claude.com/docs/en/hooks#permissionrequest), [Settings](https://code.claude.com/docs/en/settings).

## 검증

- 기존 447건 + 신규 13건: 인증/중복/거절/만료/재시작/죽은 훅/Dispatch 교체/외부 최초 선점 거부/다른 terminal 거부/조회 deadline/불완전 표시/설정 보존.
- `claude-permission-final-20261016.json`: 실제 Opus 5.5, 실제 Orca worker, 공식 훅 승인 4회, 질문 전 결과 파일 없음, 구술 답변 gamma/GAMMA와 원문 보존, native succeeded와 worker release 확인.
- `claude-permission-final-deny-20261016.json`: 실제 파일 쓰기 거절, 결과 파일 없음, 실제 중단 transcript 확인 후 시험 작업자 정리.
- Telegram 전송은 FakeTelegram이다. 실제 봇 토큰으로 시험 메시지를 보내거나 두 번째 polling을 실행하지 않았다. production DB/서비스는 시험 중 변경하지 않았다.
- 첫 시험은 harness의 중복 update 호출 오류, 두 번째는 기존 reply retry UUID의 질문 간 충돌을 발견했다. harness 수정 및 질문 ID를 포함한 retry UUID로 보완한 뒤 최종 시험을 수행했다. 연결 중단 시험에서는 다음 도구가 거절되고 파일이 생성되지 않았다.
- Astra의 읽기 검토에서 최초 native 바인딩과 전체 timeout 보완을 반영했고 재검토에서 추가 필수 수정 없음. 테스트용 terminal은 종료/해제했다.

## 적용 확인

구현 커밋 `cadb417`을 main에 반영하고 chief만 재시작했다. 3개 서비스가 active이고 승인 runtime heartbeat가 신선함을 확인했다. 기존 코덱스 세션 전체 행의 해시, alpha.txt 크기/수정시각, 전역·메인 Claude 설정의 해시가 적용 전과 같다. 배포 증거: `claude-permission-deployed-20261016.json`. 실제 사용자 채팅에는 시험 요청을 넣지 않았다.
