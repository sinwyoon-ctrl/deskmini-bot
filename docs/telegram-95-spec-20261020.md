# 텔레그램 봇 90→95% — 판단과 구현 명세 (2026-10-20, Fable 설계 / Sonnet 구현)

작업 위치: 격리 사본 `~/work/deskmini-bot-fable`, 브랜치 `fable/telegram-90`(기준 `0e856a2`).
공통 제약: 운영 사본 `~/deskmini-bot`·운영 DB(`~/.local/state/deskmini-chief`)·systemd·실제 텔레그램 전송·push·sudo 금지.
공용 도구 `~/bin/notify`, `~/bin/hub`, `~/agent-hub` 는 **수정하지 않는다**(필요 변경은 제안문으로). 비밀값은 코드·시험·로그에 넣지 않는다.
시험: `cd ~/work/deskmini-bot-fable && XDG_RUNTIME_DIR=/run/user/$(id -u) ~/deskmini-bot/.venv/bin/python -m unittest discover -s tests`
(현재 570개 통과, pytest 없음). 시험은 `tests/test_watch.py:BotFixture`(임시 홈·FakeTelegram) 위에 쓴다. 구현자는 커밋하지 않고 diff 와 시험 결과를 돌려준다.

## 0. 후보 판단

| 후보 | 결정 | 근거 |
|---|---|---|
| A 외부 승인 요청을 봇 승인 체계로 | **채택 · 1순위** | 1차 거절 5건 중 3건(“APRIL-READ-01 승인”×2, “해제 승인”)이 다른 에이전트의 notify/hub 알림에 답장한 승인이었다. 지금은 어떤 경로로도 승인이 전달되지 않는다. `~/bin/notify` 는 단방향(텔레그램/ntfy 송신, 회신 채널 없음)이라 봇이 받을 수 있는 **파일 계약**(agent-hub 폴더)이 필요하다. 봇 서비스는 이미 `~/agent-hub` 쓰기 권한이 있다(`deploy/deskmini-chief.service` ReadWritePaths). |
| D 폴링 URLError 내성 | **채택 · 2순위(소)** | chief 로그에 시간당 1-2회 WARNING. 원인: codex 연결이 켜져 있으면 long-poll 이 2초(`bot.run`: `min(timeout,2)`)라 시간당 약 1,800회 요청 중 1-2회 일시 실패(0.1%, 네트워크 흔들림)다. 유실·지연 기록 없음(inbox 23건 PROCESSED). 즉 **고장이 아니라 로그 수준 문제**. 연속 실패 횟수 기반 백오프와 로그 등급 조정만 한다. |
| B 일반 대화에 서버 상황 주입 | **채택 · 3순위(축소)** | `app/context.py:build_snapshot` 이 이미 host·bot_tasks·orca_sessions·orca_tasks·codex_limits·agent_hub_board/locks/recent_log·repos·jobs·alerts 를 넣는다(약 7천 자 제한). 빠진 것은 **텔레그램 라우팅 상태**(지금 고른 모델·그 작업·미답 질문·열린 되묻기 카드·대기 중 외부 승인)다. 1차의 「비서(봇)에게 묻기」 버튼이 쓸모 있으려면 비서가 이것을 알아야 한다. 이 절만 추가한다. A 의 테이블을 읽으므로 A 뒤에 한다. |
| C 대화 중 “이거 해줘” → 작업 카드 | **기각(이번 회차)** | 봇 발주 경로에는 이미 있다(`create_task` 제안 → 🚀 착수 버튼 카드, README 「대화 응답층」). 코덱스/클로드 **세션**으로 넘기는 제안을 새로 열면 모델이 만든 지시문이 사람 한 번 탭으로 세션에 들어가는 새 쓰기 경로가 생긴다. 1차 되묻기 카드가 같은 자리를 사람 선택으로 이미 덮는다. 폰 확인 뒤 필요가 확인되면 다음 회차. |
| E 실제 작업공간 연결 | **구현 안 함 · 스위치 존재 확인** | `config/orca.toml` `[projects.*] ownership_ready = false` 가 그 스위치다(`ionenet-web` 가 꺼진 상태로 등록돼 있음). 트레이더 읽기 전용 등록 초안은 `telegram-95-plan` 문서에 제안문으로만 적는다. CEO 결정 대기. |

구현 순서: **A → D → B → F**(A·D 가 `app/bot.py` 를 서로 다른 구역에서 건드리므로 순서대로; B 는 A 의 테이블을 읽음; F(버튼 UI 통일, CEO 추가 요구)는 A·B 의 카드를 고치므로 마지막).

---

## A. 외부 승인 요청 — agent-hub 파일 계약과 봇 승인 카드

### A-1. 계약 (요청하는 쪽: 메인 PC Claude/Codex, DeskMini 의 다른 세션)

폴더 `~/agent-hub/approvals/` (GitHub 로 두 PC 동기화되는 저장소 안). **봇은 이 폴더가 있을 때만 동작한다**(없으면 완전히 비활성 = 기본 꺼짐).

요청 파일 `approvals/<ID>.md` — ID 는 파일 이름과 같아야 하고 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, `.decision` 으로 끝나지 않음.

```
---
id: APRIL-READ-01
from: claude@main-pc
created: 2026-10-20T18:30:00+09:00
expires_hours: 24
summary: 추가 브라우저 조사(Production Read-only)를 진행해도 되는지
---
무엇을 승인하는지 · 영향 범위 · 하지 않으면 어떻게 되는지 (자유 본문, 20KB 이하)
```

결정 파일 `approvals/<ID>.decision.md` — **봇만 쓴다**(임시 파일 + `os.replace`).

```
---
id: APRIL-READ-01
decision: approved | rejected | expired
decided_at: 2026-10-20T18:41:07+09:00
by: telegram
---
```

요청한 쪽은 결정 파일이 생길 때까지 기다린다(없으면 미결, `expired` 면 거절로 취급). 동기화는 agent-hub 의 기존 방식(`hub read` 의 pull, DeskMini `hub sweep` 15분 주기 커밋·push)을 그대로 쓴다 → 메인 PC 요청은 최대 약 15분+ 지연. 봇은 git 을 돌리지 않는다.

### A-2. 봇 구현 (`app/external_approvals.py` 신설)

- `scan(bot)` — `bot._maybe_watch()` 안에서 다른 tick 처럼 try/except 로 호출(60초 주기). `bot.paths.agent_hub/'approvals'` 가 없으면 즉시 return.
  - `*.md` 중 `.decision.md` 가 아닌 파일마다: 이름 규칙·크기(≤20KB) 검사 → 불합격은 1회만 `[봇 안내] 승인 요청 파일 무시: <이름> (이유)` 알림(dedupe key `xapproval-bad:<이름>`) 하고 건너뜀.
  - 헤더 파싱: 첫 `---`…`---` 사이 `key: value` 줄. `id` 가 파일 이름과 다르면 무시(알림 1회). `created` 없으면 파일 mtime, `expires_hours` 기본 24(1~168 로 제한).
  - `external_approvals` 테이블에 없는 ID 면 행 삽입(state `PENDING`) 후 카드 전송:
    `bot._notify('xapproval:'+id, text, reply_markup=inline_keyboard([[('✅ 승인','xa:<rowid>:a'),('⛔ 거절','xa:<rowid>:r')]]))`.
    카드 본문: `[승인 요청 · <ID>]\n요청: <from>\n요약: <summary ≤500자>\n\n<본문 앞 800자>\n\n유효: <만료 시각 KST> · 결정은 agent-hub approvals/<ID>.decision.md 에 기록됩니다`. 모든 텍스트는 `app.redact.redact_text` 를 거친 뒤 `html.escape`.
  - 이미 결정 파일이 있는 요청(다른 경로로 결정됨)은 행을 `DECIDED_EXTERNAL` 로만 기록, 카드 없음.
  - `PENDING` 인데 만료 시각이 지났으면 결정 파일 `expired` 를 쓰고 state `EXPIRED`, 알림 1회(`[봇 안내] 승인 요청 <ID> 유효시간 지남 → expired 기록`).
- `handle_callback(bot, data)` — `_handle_callback` 에 `if data.startswith("xa:")` 분기 추가(기존 `ar:` 분기 옆).
  - `xa:<rowid>:a|r` 파싱 실패는 무시. 행이 없거나 chat 불일치 → `[봇 안내] 이 승인 카드의 요청을 찾지 못했습니다`.
  - state 가 `PENDING` 이 아니면 `[봇 안내] 이미 처리된 승인 요청입니다: <ID> · <decision>`(재탭·재배달 안전).
  - 만료 지났으면 `EXPIRED` 처리 후 `[봇 안내] 유효시간이 지나 승인하지 않았습니다`.
  - 정상: 결정 파일 원자적 기록 → 같은 트랜잭션에서 state `DECIDED`, decision, decided_at, decided_source(`telegram:callback`) → `[봇 안내] 승인 요청 <ID> 를 승인/거절했습니다. approvals/<ID>.decision.md 에 기록했고 요청한 쪽이 읽어 진행합니다(hub 동기화 최대 15분).`
  - **파일 기록이 실패하면 DB 를 바꾸지 않고** `[봇 안내] 결정을 기록하지 못했습니다(쓰기 오류). 다시 눌러 주세요` — 재시도 가능.
  - 1차의 `agent_routing.handle_callback` 과 같이 **DB 트랜잭션 안에서 `bot._send` 를 부르지 않는다**(잠금 오류).
- `handle_message(bot, message, update_id) -> bool` — `bot._handle_message` 에서 첨부·빈 텍스트 검사 **직후, `agent_message` 호출 전**에 둔다.
  - 매칭: 본문이 `^\s*(?:(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})\s+)?(승인|거절|approve[d]?|reject(ed)?|ok|오케이)\s*(해줘|해|합니다|요)?[.!]?\s*$`(대소문자 무시)이고, ID 는 본문 또는 `reply_to_message.text` 의 `\[승인 요청\s+(?P<id>…)\]` 에서 얻는다. ID 를 못 찾으면 False(기존 경로로).
  - ID 가 테이블에 있고 `PENDING` → 카드를 **다시** 보낸다(`dedupe key 'xapproval:'+id+':again:'+str(update_id)`): `글로 쓴 승인은 실행하지 않습니다. 아래 버튼으로 확정해 주세요.` + 같은 두 버튼. True.
  - ID 가 테이블에 있고 이미 결정됨 → `이미 처리된 승인 요청입니다: <ID> · <decision>`. True.
  - ID 가 없음 → `[봇 안내] '<ID>' 는 봇에 등록된 승인 요청이 아닙니다. 요청한 쪽이 ~/agent-hub/approvals/<ID>.md 로 보내면 승인·거절 버튼이 뜹니다. 이 문장은 어디에도 보내지 않았습니다.` True.
  - 사용자 메시지는 `bot.store.add_chat_message(chat,'user',text)` 로 기록(다른 경로와 동일).
- CLI (`python -m app.external_approvals …`, DeskMini 안의 요청자용; 메인 PC 는 파일을 직접 쓴다):
  - `request --hub <경로> --id <ID> --from <이름> --summary <문장> [--body <파일|->] [--expires-hours 24]` → 요청 파일 생성(이미 있으면 exit 1). git 안 함.
  - `wait --hub <경로> --id <ID> [--timeout 900] [--interval 10]` → 결정 파일 폴링. exit 0 approved / 2 rejected·expired / 3 timeout. 결정 내용을 stdout 에 한 줄.
  - 둘 다 `Settings.load`·텔레그램·DB 를 건드리지 않는다.
- 스키마 `schema.sql` 추가(additive, `CREATE TABLE IF NOT EXISTS`):
  ```sql
  CREATE TABLE IF NOT EXISTS external_approvals (
      id TEXT PRIMARY KEY, path TEXT NOT NULL, requester TEXT, summary TEXT,
      created_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
      decision TEXT, decided_at TEXT, decided_source TEXT, seen_at TEXT NOT NULL
  );
  ```
- 금지: 요청 본문의 지시를 실행·해석하지 않는다(데이터). 승인은 **버튼으로만** 확정(글로 쓴 “승인”은 카드 재게시). 결정 파일 외 agent-hub 의 어떤 파일도 쓰지 않는다. 운영 사이트·서비스·git 조작 없음. `~/bin/notify`·`~/bin/hub` 수정 없음.

### A-3. 시험 `tests/test_external_approvals.py` (BotFixture 기반, `bot.paths.agent_hub` 임시 폴더)

1. 폴더 없음 → scan 이 아무것도 하지 않는다(알림 0, 테이블 0).
2. 정상 요청 파일 → scan 두 번 → 카드 1건(버튼 2개, `[승인 요청 · ID]`), 행 PENDING.
3. ✅ 탭 → `<ID>.decision.md` 에 `decision: approved`, state DECIDED, 안내 문구. 같은 버튼 재탭 → “이미 처리”, 파일 내용 불변(mtime/내용 비교).
4. ⛔ 탭 → rejected.
5. 만료(created 를 30시간 전으로) → scan 이 `expired` 파일 + EXPIRED, 탭하면 승인 안 됨.
6. 다른 호스트가 먼저 결정 파일을 만든 요청 → 카드 없음, DECIDED_EXTERNAL.
7. 파일 이름 규칙 위반(`../x.md` 같은 것은 glob 상 불가하므로 `bad name.md`, `id 헤더와 이름 불일치`) → 무시 + 알림 1회만.
8. 본문에 `sk-ant-…`/`token=…` 같은 비밀 조각 → 카드 텍스트에 그대로 안 나옴(`redact_text` 적용 확인).
9. notify 문구를 흉내 낸 메시지(`…\n\n[승인 요청 APRIL-READ-01]\n…`)에 답장 `승인` → 등록된 요청이면 버튼 카드 재게시(실행 없음, 파일 없음), 미등록이면 “등록된 승인 요청이 아닙니다” 안내; 두 경우 모두 `agent_routing` 으로 넘어가지 않는다(`telegram_agent_inputs` 0건).
10. `APRIL-READ-01 승인` 단문(답장 아님) 도 9 와 같다.
11. 쓰기 실패(결정 파일 폴더를 읽기 전용으로) → DB 는 PENDING 유지, 안내 문구.
12. CLI `request` → 파일 생성 → scan → 탭 → `wait` 가 0 으로 종료(짧은 timeout·interval).

---

## D. 텔레그램 폴링 일시 오류 내성 (`app/polling.py` 신설, `app/bot.py:run` 수정)

- `class PollingHealth`:
  - `failure(exc) -> tuple[str, float]` : (로그 등급 `'info'|'warning'`, 대기 초). 연속 실패 n 회째: 대기 = `exc.retry_after` 가 있으면 `min(retry_after, 30)`, 없으면 `min(3 * 2**(n-1), 30)`(3, 6, 12, 24, 30…). 등급 = n < 3 이면 `info`, n == 3 또는 그 뒤 10회마다(13, 23…) `warning`, 그 외 `info`.
  - `success() -> str | None` : 직전 연속 실패가 3회 이상이었으면 `'Telegram polling recovered after N failures (X s)'` 문장을 돌려주고(INFO 로 남김) 카운터 초기화; 아니면 None.
  - 상태: 연속 실패 수, 첫 실패 시각(단조 시계 주입 가능 `clock=time.monotonic`).
- `bot.run()` 폴링 except 블록: `level, delay = self.polling.failure(exc)`; `LOG.log(logging.INFO if level=='info' else logging.WARNING, "Telegram polling failed (%d consecutive): %s", n, redact_text(str(exc),500))`; `time.sleep(delay)`. 성공 경로(`updates` 받음)에서 `note = self.polling.success()` 가 있으면 `LOG.info(note)`.
- `adapters/telegram.py` 는 바꾸지 않는다(원인 문자열 `URLError`/`ConnectionResetError` 는 지금도 메시지에 들어 있음).
- 금지: 폴링 timeout·codex 2초 규칙·오프셋 처리·`_handle_update` 변경 금지. sleep 상한 30초 유지(알림 지연 상한).
- 시험 `tests/test_polling.py`(순수 단위): 1·2회 실패 info/3·6초, 3회째 warning/12초, 4·5회 info/24·30초, 13회째 warning, retry_after 우선, 성공 시 복구 문장(≥3) / None(<3), 복구 뒤 카운터 0.

---

## B. 대화 스냅샷에 텔레그램 라우팅 상태 추가 (`app/context.py`, `app/chat.py` 지침 한 줄)

- `routing_summary(store) -> dict` 신설, `build_snapshot` 의 `sections["telegram_routing"]` 로 추가(`orca_tasks` 다음). 실패는 `"미확인"`.
  - `selected_agent`: `telegram_agent_focus` 의 agent 또는 None.
  - `targets`: `telegram_agent_targets` 각 agent 의 target_id 와, codex 는 `codex_sessions.(project,state)`, claude 는 `orca_requests.(project,state)`.
  - `pending_questions`: `{"codex": n, "claude": n}` (`codex_questions.state='PENDING'`, `orca_questions.state='PENDING'`).
  - `open_ask_cards`: `telegram_agent_inputs` 에서 `json_extract(plan,'$.ask')=1 AND json_extract(plan,'$.picked') IS NULL` 인 최근 3건의 `body` 앞 80자.
  - `external_approvals_pending`: `external_approvals` 중 `PENDING` 최근 5건 `{id, summary(≤120), expires_at}`; 테이블이 없으면 `[]`.
  - 전체를 `redact_text` 로 1,200자 이내.
- `build_snapshot` 의 길이 초과 축약 목록에 `"telegram_routing"` 을 넣는다(마지막 순서).
- `app/chat.py` SYSTEM_PROMPT 의 “스냅샷 키” 줄에 `telegram_routing=지금 고른 모델·그 작업·미답 질문·열린 선택 카드·대기 중 외부 승인 요청` 을 덧붙인다. 다른 지침 변경 금지.
- 금지: 스냅샷에 비밀값·chat id·토큰 금지(기존 `redact` 통과). SNAPSHOT_LIMIT(7,000) 변경 금지. DB 쓰기 금지.
- 시험 `tests/test_context_routing.py`(RoutingTests 재사용): 코덱스 선택·클로드 미답 질문·열린 카드·외부 승인 PENDING 1건을 만든 뒤 `build_snapshot` JSON 의 `telegram_routing` 에 각각 보이는지; 테이블 비어 있으면 빈 값·None; `external_approvals` 테이블 없음(스키마 없는 임시 DB)에도 예외 없음; 길이 ≤1,200.

---

## F. 승인·선택 버튼 UI 통일 규칙 (CEO 추가 요구, 2026-10-20 저녁)

원칙: **사람에게 결정을 받을 때는 항상 인라인 버튼이다.** 글자로 "승인"·"1번"을 치게 하지 않는다(글자 답은 보조 경로로만 남긴다). 봇이 보내는 모든 결정 카드는 아래 한 규칙을 따른다.

### F-1. 카드 규칙

```
<보낸이 표시>                      ← 기존 관례 유지: [봇 안내 · 클로드] / [승인 요청 · ID] / [코덱스]
1줄: 무엇을 결정하는지 (≤60자)      ← 필수
2줄: 영향 — 돈 · 외부 전송 · 되돌림 가능 여부 ← 하나라도 있을 때만
(본문: 근거·상세, 필요한 만큼. 선택 질문이면 "1) …  2) …  3) …" 번호 목록, 추천이 있으면 그 줄 끝에 ★ 와 이유 한 줄)
마지막 줄: ⏳ 유효 시간 (예: 10분 안에 · 10/21 09:00 까지)
버튼 1줄: 승인형 [✅ 승인] [⛔ 거절] (+필요시 [자세히])
        선택형 [1] [2] [3] [4]  ·  짧은 이름이면 [1 · alpha] [2 · beta]  ·  추천은 [1 ★]
        + 자유 답이 가능한 질문에는 [✏️ 직접 입력]
```

- 번호 버튼 배치: 5개 이하 한 줄. 6~8개는 두 줄(4+4). 9개 이상은 처음 7개 + `[더 보기 ▸]`(다음 쪽 7개 + `[◂ 이전]`).
- "번호 · 짧은 이름" 형식 기준: 모든 선택지 이름이 8자 이하이고 한 줄 버튼 글자 합이 30자 이하일 때만. 아니면 번호만 쓰고 이름은 본문 번호 목록에 둔다.
- `[✏️ 직접 입력]`: 누르면 "다음 답장을 이 질문의 답으로 받습니다(10분)" 안내 + `telegram_direct_inputs(chat_id, kind, key, expires_at, source)` 에 대기 기록. 10분 안의 다음 **텍스트 메시지**가 그 질문의 답으로 들어가고(기존 answer 경로 호출), 들어간 뒤 카드에 `✅ 직접 입력 · 시각` 표시. 만료되면 다음 메시지는 평소대로 처리. 슬래시 명령·버튼·첨부는 소비하지 않는다.
- 다중 선택(F-2): 토글 `[☑ 1] [☐ 2] [☐ 3]` + `[완료]`. 토글 상태는 DB(해당 질문 행의 draft 컬럼)에 두고 `editMessageReplyMarkup` 으로 다시 그린다. `[완료]` 가 실제 전달이다.
- **누른 뒤**: 원래 카드를 수정해 마지막 줄을 `✅ 승인됨 · 10/20 21:03 · 대표` / `⛔ 거절됨 · …` / `✅ 2번 선택 · …` / `✅ 직접 입력 대기 · …` 로 바꾸고 **버튼을 지운다**(`editMessageText`, 콜백의 `message.message_id` 사용). 수정이 실패하면(48시간 지난 메시지·전송 계층 미지원) 지금처럼 짧은 안내 메시지를 보낸다. 재탭·재배달은 "이미 처리됨 · 결과" 안내, 만료는 "⌛ 만료" 안내(F-2: 만료 시 카드도 수정).
- **2단계 확인**: 돈이 들거나 되돌릴 수 없는 결정(외부 승인 헤더 `impact: money|irreversible`, 배포·push·운영 변경)은 `[✅ 승인]` → 카드가 `[정말 승인 — 되돌릴 수 없음] [취소]` 로 바뀌고 두 번째 탭이 실행이다. 기존 merge/push/deploy 일회용 승인과 트레이더 `[동작 검토]→[동작 확인]` 은 이미 2단계이므로 그대로 둔다.
- 버튼 데이터는 64바이트 이하, 행 번호·키만 싣는다(기존 규칙). 카드 본문은 `redact_text` → `html.escape`.

### F-2. 지금 글자 답장만 받는(또는 규칙과 다른) 경로 — 목록과 판단

| 경로 | 지금 | 판단 |
|---|---|---|
| 외부 승인(A, `xa:`) | 버튼 2개, 탭 뒤 안내 메시지 | **F-1 적용**: 1줄·영향줄(`impact` 헤더)·탭 뒤 카드 수정·2단계(impact 가 money/irreversible) |
| 되묻기 카드(1차, `ar:`) | 버튼 3개(긴 문구) | **F-1 적용**: 본문에 `1) 코덱스 작업에 지시로 2) 코덱스에게 대화로 3) 비서에게` 번호 목록, 버튼 `[1] [2] [3]`, 탭 뒤 `✅ 2번 선택` 수정 |
| Codex 선택 질문(`cx:` requestUserInput, 질문 1개) | 선택지 이름이 버튼, 한 줄에 하나씩 최대 8개 | **F-1 적용**: 번호 목록 + 번호 버튼(짧으면 `1 · alpha`), `[✏️ 직접 입력]` 추가(자유형 답은 기존 `sessions.answer` 로), 탭 뒤 수정 |
| Codex 선택 질문(질문 여러 개) | 답장에 "1. 답 / 2. 답" 글자 | 유지(복수 질문은 글자). 카드에 `[✏️ 직접 입력]` 만 붙여 다음 답장을 받는다 |
| Codex 실행 승인(`cx:` accept/decline/cancel) | 버튼 3개 | **F-1 적용**: 1줄·영향줄(파일 변경/셸 실행/네트워크)·탭 뒤 수정. 문구 `[✅ 이번만 허용] [⛔ 허용 안 함] [⛔ 거절하고 중단]` |
| Claude 실행 허용(`clp:`) | 버튼 2개 | **F-1 적용**: 영향줄(도구·실행 위치)·탭 뒤 수정 |
| Claude native 질문(`orca_bridge` "질문 번호 … 답장") | 글자 답장만 | 선택지 형식이 없어 자유형 유지. `[✏️ 직접 입력]` 버튼만 추가(답장 대신 다음 메시지로 받기) |
| `/orca reply`, "응/첫 번째로" 자연어 답 | 글자 | 보조 경로로 유지(버튼이 1순위) |
| 작업 발주·merge/push/deploy·재시도(`approve:`) | 버튼, 탭 뒤 `_update_task_card` 로 카드 수정 | 이미 규칙에 가장 가까움. 문구만 `[✅ …]/[⛔ …]` 로 맞춤(선택) |
| 트레이더(`ta:`) | 2단계 버튼 | 유지 |

### F-3. 구현 범위

**F-1 회차(이번)**: 공용 모듈 `app/cards.py` 신설 + `xa:`·`ar:`·`cx:`(단일 질문·실행 승인)·`clp:` 적용.
- `app/cards.py`(순수 함수, I/O 없음 + `mark_decided` 하나):
  - `approval_card(sender, title, *, impact=None, body='', expires=None) -> str` (HTML escape 포함)
  - `approval_buttons(yes, no, *, detail=None) -> reply_markup` — `yes/no/detail` 는 `(라벨, callback_data)`
  - `choice_card(sender, title, options, *, recommended=None, reason=None, impact=None, body='', expires=None) -> str` — options 는 라벨(·설명) 목록, 본문에 `1) …` 번호 목록 생성
  - `choice_buttons(count, callback, *, labels=None, recommended=None, other=None, page=0) -> reply_markup` — 위 배치 규칙(5/8/페이지, 짧은 이름 기준, ★, `[✏️ 직접 입력]`=`other`)
  - `verdict_line(kind, when, who='대표') -> str` 와 `decided_text(original_html, verdict) -> str`(마지막 줄 교체 또는 추가)
  - `mark_decided(bot, message, verdict)`: `message` 는 콜백의 `query['message']`; `bot.telegram.edit_message_text(chat_id, message_id, decided_text(message['text'], verdict), reply_markup=None)` 를 try 하고 실패(`TelegramError`, `AttributeError`, message 없음)면 `bot._send(verdict)` 로 대체. 반환값 `'edited'|'sent'`.
- `app/bot.py::_handle_callback`: 각 핸들러에 `message=query.get('message')` 를 키워드로 넘긴다(핸들러 시그니처에 `message=None` 추가, 기존 위치 인자 호환).
- `app/external_approvals.py`: `_card` → `cards.approval_card`, `_buttons` → `cards.approval_buttons`; 헤더 `impact` 지원(`none|external|money|irreversible`, 기본 none; 영향줄 문구: external=“외부 전송 있음”, money=“비용 발생”, irreversible=“되돌릴 수 없음”); money/irreversible 는 2단계(첫 탭 → `xa:<rowid>:a2` 확인 버튼과 `[취소]`, 두 번째 탭이 기록); 탭 뒤 `mark_decided`.
- `app/agent_routing.py`: 되묻기 카드를 `choice_card/choice_buttons` 로(번호 목록·`[1] [2] [3]`, callback 은 기존 `ar:<rowid>:<key>` 유지 — 번호↔key 는 plan 의 `order` 로 매핑), `handle_callback` 끝에 `mark_decided`.
- `app/codex_bridge.py::question`: 단일 질문 선택지는 `choice_card/choice_buttons`(callback `cx:<key>:<i+1>` 유지) + `[✏️ 직접 입력]`(`cx:<key>:free`); 실행 승인은 `approval_card` + 세 버튼. `app/codex_session.py::handle_callback`: `free` 는 `telegram_direct_inputs` 에 대기 기록 + 안내; 다른 답은 기존 처리 + `mark_decided`.
- `app/claude_permissions.py`: 카드 `approval_card`(영향줄: 도구·실행 위치), `decide` 끝에 `mark_decided`.
- `app/bot.py::_handle_message`: 첨부·빈 텍스트 검사 뒤 **외부 승인보다 먼저** `direct_inputs.consume(bot, message, update_id)` — 만료되지 않은 대기가 있으면 이 텍스트를 그 질문의 답으로 보내고(기존 `sessions.answer` / `queue.answer`), 카드 수정은 `mark_decided`(message_id 를 대기 행에 저장) 후 True. 슬래시 명령은 소비하지 않는다.
- 스키마: `CREATE TABLE IF NOT EXISTS telegram_direct_inputs (chat_id INTEGER NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, message_id INTEGER, expires_at TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(chat_id));` — 채팅당 하나만(새 대기가 이전 대기를 덮어쓴다).
- 시험 파일 `tests/test_cards.py`(순수: 배치 5/8/9개·짧은 이름 기준·★·other·decided_text), `tests/test_cards_integration.py`(BotFixture: xa/ar/cx/clp 각 카드의 1줄·버튼 문구·탭 뒤 `edit_message_text` 호출 1회와 버튼 제거, FakeTelegram 에 `edit_message_text` 기록 메서드 추가 — `tests/test_watch.py:FakeTelegram` 수정은 **이 메서드 추가만** 허용; 직접 입력: 버튼→다음 텍스트가 답으로 전달·만료 뒤 미소비·슬래시 미소비; 2단계 승인: 첫 탭은 기록 없음, 두 번째 탭이 기록).
- 금지: 콜백 데이터에 경로·본문 싣기 금지. 모델 응답으로 버튼을 만들지 않는다(선택지는 코드가 받은 options 만). 자동 실행 없음 — 모든 전달은 탭 또는 직접 입력 소비 때만. 기존 글자 답장 경로(질문 카드 답장·`/orca reply`)는 지우지 않는다. `_update_task_card`·트레이더 카드는 건드리지 않는다.

**F-2 회차(다음)**: 다중 선택 토글+[완료], 9개 이상 페이지, 만료 시 카드 수정(outbox 전송 결과의 message_id 보존 테이블 필요), 작업 발주 카드 문구 정렬.

### F-4. 휴대폰 화면 예시 (텍스트 목업)

외부 승인 요청(일반):
```
[승인 요청 · APRIL-READ-01]
추가 브라우저 조사(Production Read-only) 진행 승인
영향: 외부 전송 없음 · 되돌릴 수 있음
요청: claude@main-pc
가상 마이크 설치와 자동 음성 입력 독립 테스트는 성공했습니다. 추가 조사는 …
⏳ 10/21 18:30 까지
[ ✅ 승인 ]  [ ⛔ 거절 ]
```
탭 뒤(버튼 사라짐):
```
[승인 요청 · APRIL-READ-01]
추가 브라우저 조사(Production Read-only) 진행 승인
…
✅ 승인됨 · 10/20 21:03 · 대표 → approvals/APRIL-READ-01.decision.md
```
돈·되돌림 불가(2단계):
```
[승인 요청 · PAY-0042]
광고 집행 예산 30만 원 결제 승인
영향: 비용 발생 · 되돌릴 수 없음
…
[ ✅ 승인 ]  [ ⛔ 거절 ]            → 탭 →   [ 정말 승인 — 되돌릴 수 없음 ]  [ 취소 ]
```
선택 질문(Codex requestUserInput, 짧은 이름):
```
[코덱스] 연결시험
시험 파일 이름을 고르세요
1) alpha ★ — 기존 문서와 이름 규칙이 같음
2) beta
3) gamma
⏳ 10분 안에
[ 1 ★ · alpha ]  [ 2 · beta ]  [ 3 · gamma ]  [ ✏️ 직접 입력 ]
```
선택 질문(이름이 길 때) 와 되묻기 카드:
```
[봇 안내 · 코덱스]
이 문장을 어디로 보낼까요 — "해제 승인해줘"
1) 코덱스 작업에 지시로 보내기
2) 코덱스에게 대화로 묻기(읽기 전용)
3) 비서(봇)에게 묻기
고르기 전에는 어디에도 보내지 않습니다 · ⏳ 24시간
[ 1 ]  [ 2 ]  [ 3 ]
```
탭 뒤: 마지막 줄이 `✅ 2번 선택 · 10/20 21:05` 로 바뀌고 버튼 없음.
다중 선택(F-2): `[ ☑ 1 ]  [ ☐ 2 ]  [ ☑ 3 ]  [ 완료 ]`.

## 검토 기준 (Fable)

- 전체 회귀 통과 수가 570 + 신규 이상이고 기존 시험 변경이 없다(변경이 필요하면 이유 명시).
- `git diff --check` 통과, 비밀값·chat id 없음, 운영 경로(`/home/ionenet/deskmini-bot`, `~/.local/state/deskmini-chief`) 하드코딩 없음.
- 승인은 버튼으로만 실행되고 재탭·재배달이 두 번 실행되지 않는다. 파일 쓰기 실패가 DB 상태를 앞서지 않는다.
- 공용 도구(`~/bin/*`, `~/agent-hub`) 무변경.
