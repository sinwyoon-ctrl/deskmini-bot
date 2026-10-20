# 텔레그램 봇 90→95% 계획·판정 — 2026-10-20 (2차)

작업 위치: 격리 사본 `~/work/deskmini-bot-fable`, 브랜치 `fable/telegram-90`, 기준 `c516b7f`(운영 main). 1차 보고 `telegram-90-plan-20261020.md`, 설계·명세 `telegram-95-spec-20261020.md`.
방식: Fable 이 판단·명세·검토, Sonnet 하위 에이전트가 구현·시험 작성(항목별 worktree, 검토 뒤 cherry-pick). 운영 사본·DB·서비스·텔레그램·공용 도구(`~/bin/notify`, `~/bin/hub`, `~/agent-hub`)는 읽기만, push 없음.

## 1. 기준표 — 1차 12항목 + 2차 추가 항목, 전/후

| # | 기준 | 1차 후(0e856a2) | 2차 후(b66a153) | 근거 |
|---|---|---|---|---|
| 1 | 이름 호출·후속 라우팅 | 됨 | 됨 | 기존 시험 유지 |
| 2 | 애매한 문장 → 보낼 곳 선택 | 됨(긴 라벨 버튼 3개) | 됨 — `1) 2) 3)` 목록 + `[1][2][3]`, 탭 뒤 `✅ N번 선택` 카드 수정 | `test_cards_integration`(ar) 3, `test_telegram_90` 8 |
| 3 | 미답 질문 중 애매문이 질문을 소비 안 함 | 됨 | 됨 | 유지 |
| 4 | 오래된 모델 선택이 메시지를 가로채지 않음 | 됨(12h) | 됨 | 유지 |
| 5 | "코덱스 한테…", "…해줘 코덱스" 인식 | 됨 | 됨 | 유지 |
| 6 | 클로드 사용량 폰 조회 | 됨 | 됨 | 유지 |
| 7 | 코덱스 사용량(이름 붙여) | 됨 | 됨 | 유지 |
| 8 | 진행 조회가 상태를 바꾸지 않음 | 됨 | 됨 | 유지 |
| 9 | 질문 카드 답장·버튼·자유형 | 됨 | 됨 + 번호 버튼·`[✏️ 직접 입력]` | `test_cards_integration`(cx) 11 |
| 10 | 거절·차단 문구에 다음 행동 | 됨 | 됨 | 유지 |
| 11 | 실제 작업공간 등록·Orca 세션 이관 | 설계만 | **설계만**(E, CEO 결정 대기; 스위치 `ownership_ready=false` 존재 확인) | §4-E |
| 12 | 폴링·아웃박스 복원력 | 됨(경고 잦음) | 됨 — 연속 횟수 백오프, 3회째부터 WARNING | `test_polling` 5 |
| **13** | 외부 에이전트 승인 요청을 버튼으로 결정(1차 거절 5건 중 3건) | **안 됨**(경로 없음) | 됨 — `approvals/<ID>.md` → 카드 → `decision.md`. 글자 "승인"은 실행 안 함(등록건은 카드 재게시, 미등록은 경로 안내) | `test_external_approvals` 15, xa 통합 12 |
| **14** | 승인·선택 카드 UI 한 규칙(첫 줄·영향줄·번호 버튼·탭 뒤 수정·재탭/만료 안내·2단계) | 안 됨(카드마다 다름, 탭 뒤 새 메시지) | 됨(F-1: xa·ar·cx·clp). F-2(다중 선택 토글·9개↑ 페이지·만료 시 카드 수정) 다음 회차 | `test_cards` 13, 통합 29 |
| **15** | 비서가 라우팅 상태(고른 모델·미답 질문·열린 카드·대기 승인)를 앎 | 안 됨 | 됨 — 스냅샷 `telegram_routing`(≤1,200자, 마스킹) | `test_context_routing` 6 |
| **16** | 대화 중 "이거 해줘" → 세션 작업 카드 제안(C) | 봇 발주 경로만 | **기각**(이번 회차) | §4-C |

추정: 1차 후 서버 측 약 90%(폰 확인 전 85) → 2차 후 서버 측 약 95%. 폰 확인(§6) 전까지는 90% 로 적는다. 남은 5%: 폰 실측, F-2, E(실제 작업공간).

## 2. 채택·기각 근거(요약; 상세는 spec §0)

- **A 채택(1순위)**: 10/20 실제 거절 5건 중 3건이 다른 에이전트 알림에 답장한 "승인". `~/bin/notify` 는 단방향이라 agent-hub 파일 계약으로 설계. 폴더가 없으면 비활성(기본 꺼짐).
- **D 채택(소)**: URLError 는 codex 연결 시 long-poll 2초 → 시간당 ~1,800회 중 1-2회 실패(0.1%), 유실 0 → 로그 등급·백오프 문제.
- **B 채택(축소)**: 스냅샷에 host·hub·orca·limits 는 이미 있음. 라우팅 상태만 추가(A 의 테이블 포함).
- **C 기각**: 봇 발주엔 이미 create_task 카드. 세션으로 넘기는 모델 제안은 새 쓰기 경로이고 1차 되묻기 카드가 사람 선택으로 덮음. 폰 확인 뒤 재검토.
- **E 확인만**: `config/orca.toml [projects.*] ownership_ready=false` 가 스위치. 트레이더 읽기 전용 초안은 §4-E.
- **F 추가(CEO)**: 승인·선택 UI 통일. F-1 구현, F-2 다음 회차.

## 3. 커밋(`fable/telegram-90`, c516b7f 기준 12개, 27파일 +2,961/−155)

| 커밋 | 내용 | 구현/검토 |
|---|---|---|
| f752a56 · b9a11f4 · 0e856a2 | 1차(되묻기 카드·선택 만료·이름 사용량·호격, 재생 sim, 1차 보고서) | Fable |
| e2cbaf4 · b3e7be8 | 2차 명세(A~E 판단, F UI 규칙·목업) | Fable |
| 5c2ca98 | **A** 외부 승인: `app/external_approvals.py`, `schema.sql` external_approvals, bot 훅 3곳, 시험 15 | Sonnet / Fable(반려 1건 수정 후 승인) |
| ab0c384 | **D** `app/polling.py` PollingHealth, `bot.run` 적용, 시험 5 | Sonnet / Fable |
| 01af138 | **B** `context.routing_summary`, `chat.py` 지침, 시험 6 | Sonnet / Fable(반려 5건 수정 후 승인) |
| 6131816 · 20a77de · a3b4054 | **F-1** `app/cards.py`·`app/direct_inputs.py`·`telegram_direct_inputs`·impact 2단계, xa/ar/cx/clp 적용, 시험 41, FakeTelegram.edit_message_text | Sonnet / Fable(승인; 후속 1건) |
| b66a153 | F-1 후속: 슬래시 명령이 직접 입력 대기를 취소(안내 문구와 일치), sim 번호 버튼 대응·전사 재생성 | Sonnet / Fable |
| (이 문서) | README 절·이 보고서 | Fable |

F-1 검토에서 확인한 것: 옛 콜백(`xa:N:a|r`, `ar:N:key`, `cx:key:N|accept|decline|cancel`, `clp:allow|deny:key`) 그대로 처리 · 돈/되돌림 불가 승인의 첫 탭은 파일·DB 를 쓰지 않고 확인 버튼만 띄움 · 직접 입력은 채팅당 1건·10분·슬래시 제외(이제 취소)·만료 뒤 평소 처리 · 카드 수정 실패(48h 경과·전송 계층 미지원)는 새 안내로 대체 · `test_telegram_90` 단언 수정은 라벨→번호 목록 이동에 따른 것이며 기능 후퇴 없음 · 마이그레이션은 `CREATE IF NOT EXISTS` + 열 존재 검사 `ALTER` 라 기존 DB 에 안전.

## 4. 시험

- 전체 회귀(README 방식) **638/638 통과**(1차 570 → A 585 → D 590 → B 596 → F-1 637 → 후속 638). `git diff --check` 통과(전사 txt 의 봇 출력 공백만 지적).
- 재생 sim `sim/phone_check_90.py` exit 0, 전사 `docs/telegram-90-sim-20261020.txt`(번호 버튼·재탭 안내 반영). FakeTelegram 콜백에는 message_id 가 없어 전사에는 카드 수정 대신 대체 안내("N번을 선택했습니다")가 찍힌다 — 실제 텔레그램에서는 카드가 수정된다(통합 시험에서 `edit_message_text` 호출 확인).
- 하지 않은 것: 실제 Claude API intent·실제 텔레그램·운영 DB 재생.

### 4-E. 실제 작업공간(E) — 구현 안 함, 제안 초안

```toml
# config/orca.toml (제안, 아직 넣지 않음)
[projects.trader-ro]
path = "/home/ionenet/work/ione-trader"
worktree = "id:<orca workspace id>::/home/ionenet/work/ione-trader"
branch = "master"
hub_project = "ione-trader"
aliases = ["트레이더", "아이원 트레이더"]
verify_workspace = true
ownership_ready = false        # CEO 결정 뒤 true
claude_permissions = false     # 읽기 전용 단계: Claude 파일 도구 없음
scope = "Read-only: README, docs, LOG.md 요약·질문 답변만. 파일 수정·커밋·push·배포·운영 DB 금지."
```
절차는 1차 보고서 §7 과 같다(설정 등록 → hub 인계 확인 뒤 `ownership_ready=true` → 첫 작업은 읽기 전용 목표).

### 4-C. 기각한 C 를 다시 열 조건
폰 확인에서 "이거 해줘"가 비서 답변으로만 끝나 불편이 확인되면: 비서의 `create_task` 제안을 Codex/Claude **세션**으로도 보낼 수 있게 하되, 카드 본문에 보낼 지시문 원문을 그대로 보여 주고 `[✅ 이 지시로 보내기]` 한 번 탭으로만 전달(F-1 규칙).

## 5. 운영 반영 절차 — 1차와 합친 단일 절차 (잠금 해제 뒤, 사람 몫)

재시작 대상은 **deskmini-chief 하나**(라우팅·카드·폴링·스냅샷 모듈을 chief 프로세스가 캐시). codex bridge·orca bridge 는 변경 없음(15초 oneshot 이 새 코드를 스스로 읽음).

```bash
~/bin/hub read                                              # deskmini-bot.lock 해제 확인
~/bin/hub claim claude deskmini-bot "telegram-90/95 반영"
cd ~/deskmini-bot && git status --short && git log --oneline -1    # 깨끗 · c516b7f
git fetch ~/work/deskmini-bot-fable fable/telegram-90
git merge --ff-only FETCH_HEAD                               # ff 불가면 멈추고 보고
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python -m unittest discover -s tests   # 638 OK
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python -m app.preflight
.venv/bin/python -m app.orca_bridge status                   # 대기 승인·전달 중 지시 없음 확인
cp ~/.local/state/deskmini-chief/chief.sqlite ~/backups/chief-$(date +%Y%m%d-%H%M).sqlite   # 스키마 추가 전 백업(additive 지만)
systemctl --user restart deskmini-chief.service
systemctl --user status deskmini-chief.service --no-pager | head -5
journalctl --user -u deskmini-chief -n 20 --no-pager         # 시작 알림·오류 없음
mkdir -p ~/agent-hub/approvals                               # 외부 승인 켜기(선택, 기본 꺼짐). 만들지 않으면 A 는 비활성
~/bin/hub done claude deskmini-bot "telegram-90/95 반영 · 638 통과 · 폰 확인 대기"
```

되돌리기: `git reset --hard c516b7f` + chief 재시작. 새 테이블(external_approvals, telegram_direct_inputs)과 열(impact, card_text)은 옛 코드가 읽지 않으므로 그대로 둬도 된다. `agent_focus_ttl_hours = 0` 이면 선택 만료만 꺼진다. `~/agent-hub/approvals` 를 지우면 외부 승인만 꺼진다.

공용 도구 제안(수정 안 함): `notify --approval <ID> [--summary] [--body-file]` 가 송신과 함께 `approvals/<ID>.md` 를 원자적으로 쓰고 문구에 `[승인 요청 <ID>]` 를 넣음; `hub approval-wait <ID>`; `hub sweep` 이 `approvals/*.md` 를 커밋·push 대상에 포함하는지 확인.

## 6. 폰에서 확인할 문장 (1차 10개 + 2차)

1차 §6 의 1~10 은 그대로(2 번은 이제 `1) 2) 3)` 목록 + `[1][2][3]` 버튼, 탭 뒤 카드가 `✅ 2번 선택 · 시각 · 대표` 로 바뀌고 버튼이 사라져야 함).

11. `mkdir -p ~/agent-hub/approvals` 뒤 DeskMini 에서 `cd ~/deskmini-bot && .venv/bin/python -m app.external_approvals request --hub ~/agent-hub --id TEST-01 --from test --summary "확인용 승인"` → 60초 안에 `[승인 요청 · TEST-01]` 카드(첫 줄 "확인용 승인", `⏳ … 까지`, `[✅ 승인][⛔ 거절]`).
12. `✅ 승인` 탭 → 카드 마지막 줄이 `✅ 승인됨 · 시각 · 대표 → approvals/TEST-01.decision.md` 로 바뀌고 버튼 없음; `cat ~/agent-hub/approvals/TEST-01.decision.md` 에 `decision: approved`.
13. (재배달 흉내) 같은 카드가 남아 있다면 재탭 → "이미 처리됨" 표시만, 파일 불변.
14. 요청 파일에 `impact: money` 를 넣어 TEST-02 생성 → 카드 둘째 줄 `영향: 비용 발생`; `✅ 승인` 탭 → 버튼이 `[정말 승인 — 비용 발생] [취소]` 로 바뀌고 decision 파일 없음; `취소` → 원래 버튼 복귀; 다시 승인 → 확인 탭 → 파일 생성.
15. 알림에 답장으로 `승인`(미등록 ID) → "등록된 승인 요청이 아닙니다 … approvals/<ID>.md" 안내, 어디에도 전달 안 됨. 등록된 TEST-03 에 글자 `TEST-03 승인` → 버튼 카드 재게시(파일 없음).
16. 코덱스 선택 질문(연결시험에서 "alpha/beta 중 고르는 질문을 버튼으로 물어봐") → 카드에 `1) alpha 2) beta`, 버튼 `[1 · alpha] [2 · beta] [✏️ 직접 입력]`; `[2 · beta]` 탭 → 카드 `✅ 2번 선택`, 코덱스가 beta 로 진행.
17. 같은 질문을 다시 띄워 `[✏️ 직접 입력]` 탭 → "다음 답장을 … 10분" 안내; `gamma` 전송 → 코덱스에 gamma 전달, 카드 `✅ 직접 입력`. 한 번 더 띄워 `[✏️ 직접 입력]` 뒤 `/status` → 대기 취소, 이어진 `안녕` 은 평소 처리.
18. 클로드 실행 허용 카드(1차 10/16 처럼) → 둘째 줄 `영향: 도구 Bash · 실행 위치 …`, `[✅ 이번 실행 허용][⛔ 거절]`; 탭 뒤 카드 수정.
19. `/model` 뒤 비서에게 "지금 어떤 모델 골라져 있고 답 기다리는 질문 있어?" → `[비서 답변]` 이 telegram_routing 근거로 답함(모델·미답 질문 수·대기 승인).
20. 다음 날 chief 로그: `journalctl --user -u deskmini-chief --since yesterday | grep -c "polling failed"` 가 INFO 로 내려가 WARNING 은 연속 3회 이상 때만.

확인되면 §1 의 "90%" 를 "95%" 로 고치고 hub 로그에 한 줄.

## 7. 남은 위험·미확인

- **폰 미확인**: 전부 FakeTelegram·모의 API. 카드 수정(`editMessageText`)은 실제 텔레그램에서만 검증된다. 48시간 지난 카드는 수정이 안 되어 안내 메시지로 대체된다(설계된 대체 경로).
- **외부 승인 동기화 지연**: 메인 PC 요청은 agent-hub git 동기화(최대 ~15분+)를 거친다. 즉시성이 필요하면 DeskMini 안에서 CLI 로 요청하거나 `notify --approval`(제안) 이 필요.
- **외부 승인 요청 본문은 신뢰하지 않는 데이터**다. 카드에 그대로 보이므로(마스킹 후) 요청자가 과장할 수 있다. 결정은 사람이 한다.
- **F-2 미구현**: 다중 선택 토글, 9개 이상 페이지(구조만 있음, 호출부는 8개 제한), 만료 시 카드 자동 수정(outbox message_id 보존 필요), 작업 발주 카드 문구 정렬.
- **Codex 질문의 추천(★)**은 Codex 가 추천 정보를 주지 않아 실제로 표시되지 않는다(구조만).
- 되묻기 카드 탭 뒤 하위 계획 실행이 ConflictError 로 끝나면 카드는 이미 `✅ N번 선택` 으로 바뀐 뒤 오류 안내가 따로 온다(순서상 작은 불일치, F-2 에서 실행 성공 뒤 수정으로 바꿀 것).
- Claude native 질문(`orca_bridge`)은 선택지 형식이 없어 글자 답장 그대로다(F-1 에서 직접 입력 미적용, 다음 회차).
- hub claim/done·notify 미실행(잠금·전송 금지). 운영 반영은 §5 순서로 사람이.
