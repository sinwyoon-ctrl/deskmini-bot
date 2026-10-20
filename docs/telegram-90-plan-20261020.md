# 텔레그램 봇 90% 계획·판정 — 2026-10-20

작업 위치: 격리 사본 `~/work/deskmini-bot-fable`, 브랜치 `fable/telegram-90`, 기준 `c516b7f`(운영 main 과 동일).
운영 사본 `~/deskmini-bot` 과 운영 DB·서비스·텔레그램은 **읽기만** 했다(Codex 잠금 10/15 유지). push 없음, 로컬 커밋만.

## 1. 90% 의 기준표와 판정

"90%" = 대표가 폰에서 하루 동안 쓰는 동작이 **거절·침묵·엉뚱한 대상 없이** 끝나고, 실패해도 다음 행동이 한 줄로 보이는 상태.
항목별 판정은 운영 DB 의 실제 입력 기록(10/16-10/20, 23건)과 격리 시험으로 했다.

| # | 기준(되면 90%) | 전(10/20 아침) | 후(이 브랜치) | 근거 |
|---|---|---|---|---|
| 1 | 이름으로 모델을 고르고 이름 없는 후속이 그 모델로 간다 | 됨 | 됨 | 기존 시험 27개 유지 |
| 2 | 애매한 문장이 거절되지 않고 어디로 보낼지 한 번에 고를 수 있다 | **안 됨** — 실제 5건 전부 같은 거절 문구(10/16 1건, 10/20 4건) | 됨 — 되묻기 카드 3버튼, 한 번만 실행 | `test_telegram_90.AskInsteadOfRefuseTests` 7개, 재생 전사 §5 |
| 3 | 미답 질문이 있을 때 애매한 문장이 질문을 몰래 소비하지 않는다 | 됨(거절로) | 됨(카드에 [질문 답변으로] 버튼, 누를 때만 전달) | `test_pending_question_offers_answer_choice…` |
| 4 | 며칠 전 고른 모델이 모든 메시지를 가로채지 않는다 | **안 됨** — 10/16 선택이 10/20 알림 답장 4건을 코덱스로 끌고 가 거절 | 됨 — 12시간 지나면 인사·일반 질문은 비서, 애매문은 비서 버튼 우선. 작업·조회는 그대로 | `LapsedSelectionTests` 8개 |
| 5 | "코덱스 한테 …", "… 해줘 코덱스" 도 호출로 인식 | **안 됨** — "코덱스 한테 전달해" → 본문 "한테 전달해" | 됨 | `test_particle_after_space_and_trailing_name…` |
| 6 | 클로드 사용량(5시간/주간·초기화) 폰 조회 | 됨 — 10/16 19:16 KST 새 조회(CLI) → 수치 SENT, 18초 뒤 재조회는 캐시로 즉시 SENT | 됨(변경 없음) | 운영 outbox `agent-conversation:chat-23bd…`, `agent-route:telegram:…377` 모두 SENT |
| 7 | 코덱스 사용량 폰 조회 | 부분 — `/codex` 와 "코덱스야 쿼터…"는 됨, 클로드 선택 중 "코덱스 … 얼마나 남았어"는 **클로드 수치** | 됨 — 문장 속 모델 이름을 따라 읽음, 선택은 유지 | `NamedUsageAndVocativeTests` 4개 |
| 8 | 진행 조회가 질문·승인·명령을 바꾸지 않는다 | 됨 | 됨 | 기존 447건 회귀 유지 |
| 9 | 질문 카드 답장·버튼·자유형 답변 | 됨(10/16 폰 통과) | 됨 | `telegram-phone-pass-20261016.md` |
| 10 | 거절·차단 문구에 다음 행동이 있다 | 부분 — "다시 풀어서 보내 주시거나 원래 질문 카드…" 한 문장 | 됨 — 답장/버튼/다시 보내기 + 기다리는 질문 본문 | `test_api_failure_still_refuses_work_with_actionable_text` |
| 11 | 실제 작업공간(연결시험 trial 외) 등록·독립 Orca 세션 이관 | 안 됨(설계상 opt-in 잠금) | **설계·절차만**(§7). 기본값 꺼짐 유지 | — |
| 12 | 폴링·아웃박스 복원력 | 됨 — URLError 경고 시간당 1-2회 있으나 자동 재시도, 10/16 이후 inbox 23건 전부 PROCESSED, outbox 73건 전부 SENT | 됨(변경 없음) | journal·DB 읽기 |

추정: 전 약 72%(12항목 중 7 됨·2 부분·3 안 됨) → 후 서버 측 약 90%(11 됨·1 설계만). 폰에서 §6 문장을 확인하기 전까지는 85% 로 적는다.

## 2. 운영 기록에서 본 실제 실패 패턴 (읽기 전용)

| 시각(UTC) | 입력(폰) | 그때 응답 | 원인 |
|---|---|---|---|
| 10/16 08:21 | 클로드야 지금 주간 쿼터 얼마나 남았어 | 어느 프로젝트에서 작업할까요? | 수정 전 코드(이후 426e34f 로 해결됨) |
| 10/16 09:12 | 자 클로드 텔레그램 연동 최적화 작업 계속 할까 | 거절 문구 | API intent=ambiguous → 무조건 blocked |
| 10/16 10:16 | 아니 코덱스 비즈아이원 얼마나 남았어 | **Claude** 사용 한도 | 클로드 선택 중이라 이름 무시 |
| 10/20 09:34 | APRIL-READ-01 승인 (다른 에이전트의 notify 메시지에 답장) | 거절 문구 | 10/16 코덱스 선택이 4일째 유효 + ambiguous→blocked |
| 10/20 09:35 | 코덱스 한테 전달해 | 거절 문구 | 조사 앞 띄어쓰기로 본문이 "한테 전달해" |
| 10/20 09:35 | APRIL-READ-01 승인 (재시도) | 거절 문구 | 같음 |
| 10/20 11:16 | 해제 승인해줘 코덱스 | 거절 문구 | 문장 끝 호격 미인식 + ambiguous→blocked |
| 10/20 11:16 | 해제 승인 (hub 로그 알림에 답장) | 거절 문구 | 같음 |

그 밖에 오류·재시도 흔적 없음: `AGENT_ROUTE_INTERPRETATION_FAILED` 0건, inbox FAILED 0건, outbox FAILED 0건, chief 로그의 오류는 Telegram 폴링 URLError 경고(자동 재시도)만.

## 3. 변경 내용과 커밋

| 커밋 | 내용 |
|---|---|
| `f752a56` | `app/agent_router.py` ambiguous → `'unclear'`; `app/agent_routing.py` 되묻기 카드·버튼 실행(`ar:행번호:선택`)·선택 만료·이름 붙인 사용량·호출 인식(띄어쓴 조사, 문장 끝 호격)·차단 문구; `app/bot.py` `ar:` 콜백 연결; `app/chat.py`+`config/models.toml` `agent_focus_ttl_hours`(기본 12); `tests/test_telegram_90.py` 24개 |
| `b9a11f4` | `sim/phone_check_90.py` 실제 폰 문장 재생, 전사 `docs/telegram-90-sim-20261020.txt` |
| (이 문서) | `README.md` 사용자 안내 절, 이 보고서 |

설계 요점
- 되묻기 카드의 선택지는 **카드를 만들 때 미리 계획**해 `telegram_agent_inputs.plan` 에 같이 저장한다. 버튼은 저장된 계획만 실행하고(원문 재해석 없음), 첫 탭에서 `picked` 를 기록해 재탭·재배달은 "이미 처리" 안내만 한다. 비서 선택은 기존 `_chat_reply` 로 넘긴다.
- 선택 만료는 **사용자가 모델에 실제 보낸 마지막 메시지**(`telegram_agent_inputs` 중 clarify 가 아닌 것, 또는 카드에서 보내기를 고른 것)의 `inbox.received_at` 기준이다. 모델 응답·재시작 알림은 기준이 아니다. `agent_focus_ttl_hours = 0` 이면 이전 동작.
- 스키마·서비스 파일 변경 없음. 운영 DB 쓰기 없음.

## 4. 시험 결과

- 전체 회귀: `XDG_RUNTIME_DIR=/run/user/$(id -u) ~/deskmini-bot/.venv/bin/python -m unittest discover -s tests` → **570 통과**(기준 546 + 신규 24), 11.1초. `git diff --check` 통과.
- 신규 `tests/test_telegram_90.py` 24개: 되묻기 7(실제 문장·대화 선택·작업 선택·비서 선택·미답 질문·잘못된 버튼·API 실패), 선택 만료 8(TTL 안/밖, 0, 작업은 유지, 카드 우선순위, 카드/해명은 갱신 안 함, 보내기 선택은 갱신, 설정 읽기), 이름 붙인 사용량·호격 6, interpret 3.
- 재생 시뮬레이션 `sim/phone_check_90.py`: 실제 문장 9개 + 버튼 5회 = 14단계, 전부 기대 동작(전사 `docs/telegram-90-sim-20261020.txt`). 전송·API·CLI 호출 없음.
- 하지 않은 것: 실제 Claude API intent 호출(API 는 운영 기록의 intent 로 대체), 실제 텔레그램 전송, 운영 DB 상의 재생.

## 5. 운영 반영 절차 (잠금 해제 뒤, 사람 몫)

전제: Codex 가 `deskmini-bot.lock` 을 풀었고(10/20 hub todo 로 요청됨), 운영 체크아웃이 `c516b7f` 그대로인지 확인.

```bash
~/bin/hub read                                   # 잠금 해제 확인
~/bin/hub claim claude deskmini-bot "telegram-90 반영"
cd ~/deskmini-bot && git status --short && git log --oneline -1          # 깨끗 · c516b7f
git fetch ~/work/deskmini-bot-fable fable/telegram-90
git merge --ff-only FETCH_HEAD                   # ff 가 안 되면 멈추고 보고
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python -m unittest discover -s tests   # 570 OK
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python -m app.preflight
# 반영 직전 상태: 대기 승인·전달 중 지시가 없는지
.venv/bin/python -m app.orca_bridge status
systemctl --user restart deskmini-chief.service  # agent_routing/agent_router/bot/chat 은 chief 프로세스가 캐시하므로 재시작 필수
systemctl --user status deskmini-chief.service --no-pager | head -5
# codex bridge·orca bridge 는 변경 없음(재시작 불필요). 15초 oneshot 은 새 코드를 스스로 읽는다.
~/bin/hub done claude deskmini-bot "telegram-90 반영 · 570 통과 · 폰 확인 대기"
```

되돌리기: `git reset --hard c516b7f` 후 chief 재시작(스키마 변화 없어 DB 호환). `agent_focus_ttl_hours = 0` 으로만 두면 선택 만료만 꺼진다.

## 6. 폰에서 확인할 문장 (순서대로, 기대 결과)

1. `클로드야` → 「클로드를 선택했습니다. 작업할 프로젝트와 할 일을 알려 주세요.」
2. `텔레그램 연동 최적화 작업 계속 할까` → **버튼 카드**(클로드 작업에 지시로 / 클로드에게 대화로 묻기 / 비서에게 묻기). 거절 문구가 아니어야 함.
3. 「클로드에게 대화로 묻기」 탭 → 「답변을 준비하고 있습니다…」 뒤 `[클로드]` 답변 1건. 같은 버튼 다시 탭 → 「이미 처리한 선택입니다」만.
4. `지금 주간 쿼터 얼마나 남았어` → `[봇 안내 · 클로드] Claude 사용 한도 5시간/주간 … 관측: <지금 시각>`(60초 안 재질문은 즉시).
5. `코덱스 쿼터는 얼마나 남았어` (클로드 선택 중) → **DeskMini Codex 사용 한도**. 이어서 `클로드야`가 아닌 일반 문장이 여전히 클로드로 가야 함.
6. `코덱스 한테 README 첫 줄 읽어줘` → `[봇 안내 · 코덱스] 코덱스에 작업 요청을 접수했습니다`(연결시험 작업).
7. `README 첫 줄 다시 읽어줘 코덱스` → 같은 코덱스 작업으로 접수.
8. 12시간 뒤(또는 설정을 잠시 `agent_focus_ttl_hours = 0.01` 로) `안녕` → `[비서 답변]`. `README 읽어줘` 는 그대로 코덱스로.
9. 다른 에이전트의 notify 알림에 답장으로 `승인` → 거절 문구 대신 카드. 「비서(봇)에게 묻기」 탭 → 비서가 그 승인이 봇 밖의 요청임을 설명.
10. 코덱스 질문 카드가 떠 있을 때 `그냥 그걸로` → 카드에 「기다리는 질문의 답변으로 보내기」. 누르기 전 질문은 PENDING 유지.

확인되면 `docs/telegram-90-plan-20261020.md` §1 의 "85%" 를 "90%" 로 고치고 hub 로그에 한 줄 남긴다.

## 7. 실제 작업공간 등록·독립 Orca 세션 이관 — 설계안 (구현 안 함, opt-in)

현재 연결 가능한 곳은 `trial`(연결시험)과 `bot-claude`(봇 개발 worktree)뿐이다. 실제 제품 작업공간을 폰에서 쓰려면:

1. **등록은 설정 파일로만**: `config/codex.toml`/`config/orca.toml` 에 프로젝트를 추가한다(경로·브랜치·별칭·scope). 텔레그램이 경로를 줄 수 없다는 규칙은 유지. 기본 `ownership_ready = false` 로 넣고, 봇은 `/orca context <프로젝트>` 로 잠금·HANDOFF 를 읽기만 한다.
2. **소유권 인계 확인 뒤 켬**: 해당 프로젝트의 hub 잠금을 가진 담당자(메인 Codex/Claude)가 `hub pause` 로 인계 메모를 남기고 잠금을 풀었음을 `hub read` 로 확인한 뒤에만 `ownership_ready = true`. 봇은 외부 잠금을 지우지 않는다(기존 계약).
3. **첫 작업은 읽기 전용 목표**: `/orca new <프로젝트> codex "README 와 HANDOFF 요약"` 처럼 파일을 바꾸지 않는 목표로 1회 돌려 경로·브랜치·dirty 보존을 확인한다.
4. **독립 Orca 세션 공유**: 메인 PC 에서 시작한 세션을 "가져오기"는 하지 않는다. 대신 `/orca import <프로젝트> <모델> <목표>` 로 IMPORTED 문맥 기록을 만들고, 실제 실행은 DeskMini 의 새 Task 로 시작한다(기존 `telegram-model-handoff.md` 계약). 공유 메모리는 `state/orca-context/<업무ID>.json` + hub 인계 메모다.
5. **되묻기 카드와의 관계**: 프로젝트가 여러 개 등록되면 "어느 프로젝트에서 작업할까요?" 가 늘어난다. 다음 단계로 그 질문도 버튼 카드(프로젝트 선택지)로 바꾸면 폰 입력이 한 번 줄어든다(이번 범위 밖).

운영 영향이 커서 이번에는 설정 기본값(꺼짐)과 절차만 남긴다.

## 8. 남은 위험·미확인

- **폰 미확인**: 모든 검증은 FakeTelegram·모의 API 다. 실제 Claude API 가 운영에서 같은 문장에 다른 intent 를 줄 수 있다(그래도 결과는 카드/대화/작업 중 하나이며 거절은 API 실패 때만).
- 문장 끝 호격 인식으로, 예전엔 비서가 답하던 "… 알려줘 클로드" 류가 클로드 CLI 대화로 간다(의도된 변경이지만 체감이 다를 수 있음).
- 선택 만료 12시간은 추정값이다. 밤새 작업을 이어 쓰는 날엔 아침 첫 인사가 비서로 간다. 작업 지시는 영향 없음. `agent_focus_ttl_hours` 로 조정.
- 되묻기 카드의 「작업에 지시로」는 애매한 문장을 그대로 코덱스 세션에 보낸다. 사람이 버튼으로 고른 명시적 행동이지만, 코덱스가 그 문장을 어떻게 해석할지는 보장하지 않는다(기존 자연어 지시와 같은 수준).
- `APRIL-READ-01 승인` 류 — 다른 에이전트가 notify 로 보낸 승인 요청은 이 봇의 승인 체계가 아니다. 카드가 떠도 승인은 되지 않는다. 그런 요청은 요청한 쪽이 봇의 승인 카드(버튼)로 보내게 해야 근본 해결이다(별도 과제).
- hub: `deskmini-bot` 이 Codex 잠금이라 `hub claim/done` 을 하지 않았다. 반영 시 §5 순서로 사람이 한다.
- Telegram 폴링 URLError 경고가 시간당 1-2회 꾸준히 있다. 메시지 유실·지연은 기록상 없으나 네트워크 쪽 점검 과제로 남긴다.
