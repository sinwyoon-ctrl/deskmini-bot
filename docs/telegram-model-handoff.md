# Telegram 수동 모델 인계 — 구현·검증 보고

이 변경은 기존 Orca 큐/타이머를 확장한다. 새 스케줄러, 자동 한도 전환, CLI 승인 우회, 서비스·운영 사이트 변경은 없다. 기존 `/new`와 `/todo`는 그대로다. 홈페이지 파일, Claude 메모리, 자격 증명은 구현·테스트 중 변경하지 않았다. 미커밋/자동 커밋 Telegram 알림 코드는 변경하지 않았다.

## 사용자 경로

- `홈페이지 작업 코덱스로 이어줘`, `클로드 한도 찼으니 코덱스로 넘겨줘`, `이 작업은 클로드로 바꿔서 계속해`: 대화 API 없이 정책 → 명시적 업무 선택 → SQLite 인계 저널. 모호하면 실행 없이 작업 ID를 묻는다.
- `지금 누가 작업 중이야`: Orca 상태 조회. `/status`와 대화 스냅샷에도 Orca 작업이 포함된다.
- `/orca switch 업무ID codex|claude|astra|sol [다음 지시]`: 확실한 수동 전환. astra=`gpt-6-astra`, sol=`gpt-6.1-sol`; codex/claude는 사용자 기본 모델을 상속한다. 임의 모델·명령·경로는 허용하지 않는다. 표시 모델은 선택값이며 실제 requested/effective launch 증거는 `orca_attempts.receipt_json`에 보존한다.
- `/orca new 프로젝트 모델 목표`, `/orca import 프로젝트 모델 목표`, `/orca projects`, `/orca context 프로젝트`, `/orca status`, `/orca reply 업무ID 답변`, `/orca recover 업무ID`.
- 답변 대기 질문이 하나면 `응`, `첫 번째로 진행해`, `답변: …` 같은 명확한 답변을 전달한다. 여러 질문·일반 대화는 대상을 묻는다. 기존 `상태`, `작업 목록`, `도움말`과 슬래시 명령이 답변보다 우선한다.
- 접수·대기·중단 확인·차단·실행·미확인·완료와 질문/오류를 기존 outbox로 보낸다. 실제 Telegram 송신은 worker 테스트에서 하지 않았다.

## 소유권·복구 계약

업무 ID는 `orca_requests.id`로 고정된다. 최초 요청은 별도 submission 값으로 보존하므로 모델 변경 뒤 같은 Telegram update가 재생되어도 원래 접수가 중복되지 않는다. 인계는 source_key별 `orca_changes`에 old/new Task·Dispatch, 정확한 argv·replay ID·단계·문맥을 기록한다. 단일 미완료 인계 unique index, DB transaction, bridge flock, 최초 시작 CAS가 같은 업무의 경쟁 시작을 막는다.

명시적인 사용자 전환만 현재 **네이티브 권한이 확인된 supervised 소유자**를 중단할 수 있다. `worker-stop`의 성공 반환이나 fence만으로 후속 실행하지 않는다. 다시 `worker-list --include-remote`로 같은 Run/Task/Dispatch의 settled 상태와 `projection.liveness.verdict=exited`를 확인해야 한다. 미확인·연결 유실·다른 소유자는 차단한다. 종료된 이전 supervised 시도는 고정 replay ID의 worker-release로 정리하고 released/already_released/retained 수신을 확인한 뒤 새 시도를 시작한다. 정리 결과가 미확인이면 멈추고 같은 요청을 복구한다. `worker-abandon`, 위조 worker_done, 임의 terminal close는 사용하지 않는다.

실패/중단 시 native 같은 Task의 `--retry-of`를 쓴다. 완료 업무의 새 지시는 같은 업무 ID 아래 새 native Task/Dispatch다. 구버전 Task의 불변 spec에 문맥 포인터가 없으면 **기존 소유자의 종료를 증명한 후에만** 새 native Task로 이관한다. 기존 native 재시도 circuit breaker는 우회하지 않는다. 모델/한도 실패는 실패 또는 미확인으로 남고 다른 모델을 자동 선택하지 않는다.

문맥은 `state/orca-context/<업무ID>.json`과 인계별 불변 스냅샷, DB에 남긴다. 최초 spec은 매 시도에서 이 고정 경로를 먼저 읽도록 지시한다. task-update result가 prompt에 주입된다고 가정하지 않는다. 원래 목표·개발 범위·repo/worktree/branch·git HEAD·dirty 파일/통계·AGENTS/HANDOFF/REPORT·hub 잠금/인계·최근 체크포인트/검증/다음 행동·native 시도 및 출처를 보존한다. 비밀 파일 본문·원문 diff·개인 Claude 대화는 읽지 않는다. 보고되지 않은 결정·검증은 `미확인/미보고`로 유지한다. 런타임/작업공간 식별자와 등록 범위를 재검증한다.

`recover`는 저장된 같은 명령을 재관측/재생한다. 중단 후 크래시는 종료 증거를 다시 확인한다. launch 응답 유실은 같은 replay ID를 유지한다. 실행 중 상태와 그 outbox 알림을 같은 transaction으로 기록한다. 실제 native 완료는 기존 Run/Task/Dispatch·발신자·settlement 검증 뒤 처리한다. 과거 Dispatch의 늦은 질문/완료는 새 소유자를 바꾸지 않는다.

## 실제 홈페이지 개발 프로젝트 등록

정확한 등록값:

- 별칭: `ionenet-web`, `홈페이지`, `웹사이트`, `아이오넷 웹`
- DeskMini 경로: `/home/ionenet/work/ionenet-web`
- 브랜치: `hero-chapters-20261012`
- native: `id:690617b9-e3d5-4315-9288-402a958ae043::/home/ionenet/work/ionenet-web`

발견 당시 `ionenet-web__hero.lock`와 `ionenet-web__f1.lock`의 외부 Claude/Codex 담당자가 있었다. `ownership_ready=false`는 의도적인 초기 등록 잠금이다. 홈페이지의 자연어 `이어줘`가 아직 tracked native 업무와 연결되지 않으면 IMPORTED 문맥 요청을 만들며 **실행하거나 외부 작업자를 중단했다고 말하지 않는다**.

관리자의 1회 등록 절차:

1. `/orca context ionenet-web`로 잠금과 파일 기반 기록을 확인한다. 기존 담당자가 실제 작업을 중단하고 hub 인계를 남기게 한다. 한도 소진·사용자의 “멈췄다” 문장만으로 native 종료를 증명하지 않는다. 비관리 Desktop 세션은 관리자가 해당 실행 호스트에서 직접 종료/독점 소유권을 확인해야 한다.
2. 다른 담당자가 보유한 base/section 잠금을 **그 담당자의 정상 hub 절차로** 해제/정리한다. 봇은 외부 잠금을 지우지 않는다. 오래된 HANDOFF 잠금도 자동으로 훔치지 않는다.
3. 관리자가 정확한 repo/worktree/branch와 외부 실행기 독점 소유권을 확인한 뒤 `config/orca.toml`의 해당 `ownership_ready=true`를 적용한다. 봇 sandbox나 agent 전역 권한을 늘릴 필요가 없다.
4. `/orca new ionenet-web astra 구체적인 목표와 완료 기준`으로 managed 업무를 시작한다. IMPORTED 레코드는 외부 문맥 발견 기록으로 남는다. 이미 new 요청이 사전 차단되었다면 `/orca recover 업무ID`로 같은 접수를 재확인할 수 있다. 이후부터 같은 업무 ID의 `/orca switch`가 가능하다.

실행기는 프로젝트 base hub lock을 `O_EXCL`로 예약하고 업무 ID가 같은 토큰만 재사용/해제한다. 외부 base·section 잠금을 먼저 검사하고, 종료 미확인 동안 예약을 유지한다. 기존 hub의 외부 claim 구현 자체는 atomic compare-and-swap이 아니므로 여러 호스트가 동시에 수동 claim을 강행하는 경쟁까지 보장하지 않는다. 실제 프로젝트 등록은 관리자가 해당 경로를 managed bridge 전용으로 인계한 뒤 진행해야 한다. legacy dispatcher 변경은 없다.

## 자동 검증

기본 suite: 271개 실행, 270개 통과. 기존 `test_card_from_manual_request_is_announced_once` 1개는 worker sandbox의 읽기 전용 홈에 임시 폴더를 만들다가 실패했다. 해당 fixture나 sandbox를 변경하지 않았다. 코디네이터는 같은 기본 suite와 구현 checkpoint 전체 300개를 sandbox 밖에서 통과 확인했다.

최종 worker 실행: 전체 303개 중 302개 통과, 동일한 기존 홈 임시폴더 테스트 1개만 sandbox 실패. Orca 집중 테스트는 이후 보강을 포함해 54개가 모두 통과했고 `git diff --check`도 통과했다.

새 `tests/test_orca_handoff.py`는 가짜 Telegram과 simulated native로 수동 전환 → durable route → 중단/종료 확인 → 문맥을 읽은 새 시도 → 완료/outbox를 검증한다. 명령 중복, source 충돌, queued 상태 경쟁, 실행기 flock, hub 소유권, unknown owner, config 이동, 중단 직후 크래시, stop/start/reply 응답 유실, outbox transaction 실패, 완료 후 계속, 구버전 Task 이관, 잘못된 과거 질문, API 없이 한국어 해석, 무권한 사용자, 모델 실패를 포함한다.

```bash
XDG_RUNTIME_DIR=/run/user/1000 /home/ionenet/deskmini-bot/.venv/bin/python -m unittest discover -s tests -p 'test_orca*.py'
XDG_RUNTIME_DIR=/run/user/1000 /home/ionenet/deskmini-bot/.venv/bin/python -m unittest discover -s tests
```

## 코디네이터용 실제 native smoke (Telegram·홈페이지 쓰기 없음)

`sim/orca_handoff_smoke.py`는 production DB/Run/hub를 쓰지 않고 `/tmp/orca-handoff-smoke-*` 아래 별도 상태와 hub를 쓴다. 오직 trial 등록을 복사하고 모든 요청의 chat_id는 null이다. 설정·비밀 파일을 읽는 Settings.load나 Telegram client를 부르지 않는다. trial 안에서도 작업자에게 파일 편집·hub 명령을 금지한다.

1. production coordinator와 다른 전용 terminal을 만든 뒤 그 terminal에서 managed CLI로 `orchestration run-create --objective 'read-only Telegram model handoff smoke' --json`을 실행한다. 새 Run과 그 terminal handle을 기록한다. production `run_69aff5d0572f`를 쓰지 않는다. 한 coordinator terminal은 한 Run에 묶이므로 production handle을 테스트 Run으로 rebind하지 않는다.
2. 아래 RUN/HANDLE/ID는 방금 받은 실제 값으로 바꾼다. 명령은 격리 작업트리 루트에서 실행한다.

```bash
/home/ionenet/deskmini-bot/.venv/bin/python sim/orca_handoff_smoke.py init --state /tmp/orca-handoff-smoke-20261015-a --run RUN --coordinator HANDLE
/home/ionenet/deskmini-bot/.venv/bin/python sim/orca_handoff_smoke.py start --state /tmp/orca-handoff-smoke-20261015-a --agent sol
/home/ionenet/deskmini-bot/.venv/bin/python sim/orca_handoff_smoke.py tick --state /tmp/orca-handoff-smoke-20261015-a
/home/ionenet/deskmini-bot/.venv/bin/python sim/orca_handoff_smoke.py status --state /tmp/orca-handoff-smoke-20261015-a
```

3. 실제 worker가 `SMOKE_WAIT_741` 질문으로 대기하는지 확인한다(필요하면 tick 한 번 더). 업무 ID를 ID에 넣고 모델을 수동 변경한다.

```bash
/home/ionenet/deskmini-bot/.venv/bin/python sim/orca_handoff_smoke.py switch --state /tmp/orca-handoff-smoke-20261015-a --id ID --agent astra
/home/ionenet/deskmini-bot/.venv/bin/python sim/orca_handoff_smoke.py tick --state /tmp/orca-handoff-smoke-20261015-a
/home/ionenet/deskmini-bot/.venv/bin/python sim/orca_handoff_smoke.py status --state /tmp/orca-handoff-smoke-20261015-a
```

4. bridge 종료 증거와 실제 astra worker의 문맥 marker `SMOKE_CONTINUE_741` 보고를 확인한다. native `worker-list --run RUN --include-remote --json`과 필요한 `worker-read --dispatch DISPATCH --limit 30 --json`을 사용한다. tick으로 worker_done 회수 후 SUCCEEDED, 동일 업무 ID, 같은 native Task의 retry-of, 다른 Dispatch, `orca_changes`, `orca_attempts`, context JSON을 확인한다. 순수 Python/SQLite로 조회 가능하며 Telegram 알림은 없어야 한다.
5. 완료 후 `--source hub:smoke-switch-2 --agent sol`로 switch → tick을 한 번 더 시험하면 동일 업무 아래 새 native Task의 continuation을 확인할 수 있다.
6. 실제 Claude 성공은 현재 주간 한도 소진으로 검증할 수 없다. 필요하면 **별도 새 smoke state/Run**에서 `--agent claude`를 선택하고 UNKNOWN/FAILED 및 자동 fallback 부재만 확인한다. 소진을 성공으로 보고하지 않는다.

UNKNOWN이면 같은 ID에 recover → tick으로 저장된 요청을 확인한다. 소유권이 미확인일 때 새 smoke 업무나 임의 terminal close로 우회하지 않는다. 작업 중단/정리도 버전 일치 Orca recovery 문서의 정확한 Dispatch로만 한다. 코디네이터 실제 smoke에서 Sol → Astra 전환의 동일 업무 ID·동일 native Task·이전 Dispatch stopped/exited·새 Dispatch의 실제 gpt-6-astra 실행과 최종 SUCCEEDED를 확인했다. Astra는 인계 파일의 BLUEBIRD_741을 직접 읽어 보고했고, 정확히 2개 시도·중복 업데이트 추가 실행 0건이었다. trial git diff --check/status는 깨끗했고 홈페이지는 기존 untracked node 외에 변함없었다. 실제 Telegram 대신 FakeTelegram만 사용했다. 이 검증 후 추가한 old-worker release 단계는 simulated native 회귀로 검증했으며 코디네이터의 최종 native 재확인 대상이다. 현재 worker는 실제 stop/switch나 Telegram을 실행하지 않았으며, 최종 live smoke·코드 검토·scoped commit/push·서비스 적용은 코디네이터 담당이다.
