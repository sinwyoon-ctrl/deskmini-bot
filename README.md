# DeskMini AI 비서실장

Telegram 개인 채팅을 주 인터페이스로 사용해 DeskMini 상태 확인, Claude Code 작업 발주, 진행 보고,
중단, 검증, 승인형 병합·push·스테이징 배포를 제공한다.

## 현재 정책

- 단일 대표 개인 채팅만 허용
- 활성 작업 1개
- 사용자 입력의 임의 셸 실행 금지
- 대화 응답은 Claude API(`claude-sonnet-5-5`, effort low) 우선, 키가 없거나 실패하면 Gemini(`~/.config/ione/gemini_rest.py` REST) 폴백
- LLM은 답변과 동작 *제안*만 한다. 착수·중단·병합·push·배포는 정책 코드와 일회용 승인 버튼을 거친다
- Claude는 전용 worktree, `--restricted`, 파일 도구만 사용하며 SSH·Gemini/Telegram 설정·상태 DB·Docker 소켓을 차단
- 검증 명령은 Bubblewrap의 읽기 전용 루트·빈 홈·네트워크 비공유 환경에서 실행
- 병합·push·배포 각각 별도 일회용 승인
- 운영사이트 자동 변경 금지

## 경로

```text
/home/ionenet/deskmini-bot                 코드
/home/ionenet/.local/state/deskmini-chief  DB·작업·증거·로그
```

## 점검

```bash
cd /home/ionenet/deskmini-bot
uv venv .venv && uv pip install --python .venv/bin/python anthropic   # 최초 1회
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python -m unittest discover -s tests -v
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python -m app.preflight
```

## 실행

```bash
mkdir -p ~/.config/systemd/user
cp deploy/deskmini-chief.service ~/.config/systemd/user/
cp deploy/deskmini-chief-maintenance.service ~/.config/systemd/user/
cp deploy/deskmini-chief-maintenance.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now deskmini-chief.service deskmini-chief-maintenance.timer
systemctl --user status deskmini-chief.service
```

재부팅 후 사용자 서비스 자동 시작에는 `loginctl show-user ionenet -p Linger`가 `yes`여야 한다.
현재 계정에서 활성화 권한이 없다면 관리자가 한 번만 다음을 실행한다.

```bash
sudo loginctl enable-linger ionenet
```

## Telegram 명령

- `/status`: 서버와 관리 작업 상태
- `/tasks`: 최근 작업
- `/task T-...`: 작업 상세
- `/new 작업내용`: 작업 발주 카드 생성
- `/stop T-...`: 등록 작업 중단
- `/hub`: agent-hub BOARD 요약, 현재 잠금, 최근 Codex·Claude 작업 기록 5건
- `/git`: `~/agent-hub/hosts/ionenet.conf` 저장소별 최근 커밋 3개와 미커밋 변경 수
- `/work`: `~/work` 아래 최근 수정된 프로젝트 폴더
- `/model`: 지금 쓰는 대화 모델(키 값은 표시하지 않음)

- `/jobs`: 장시간 작업(렌더 폴더) 상태 — 마커 기준
- `/files [작업]`: 작업 결과물 목록 버튼(인자 없으면 결과물 있는 최근 작업들), 버튼을 누르면 전송
- `/logs [작업]`: `run.log`·`postprocess*.log` 끝 30줄(ANSI·진행 막대 제거, 마스킹)
- `/quiet 2h` (`30m`, `1h30m`, `90분`, 최대 12시간) · `/quiet off`: 잠시 조용히
- `/alerts`: 알림 설정·최근 알림 · `/alerts on|off` (on 은 조용히도 해제, off 여도 치명적인 알림·아침 요약은 옴)
- `/brief`: 아침 요약 미리 보기

- `/retry T-...`: 실패·차단 작업의 이유와 [다시 시도] 버튼(또는 한도 해제 시각 예약 버튼)을 다시 띄운다
- `/todo codex 할 일` · `/todo claude …` · `/todo any …`: agent-hub 할 일 남기기(커밋·push), `/todo` 목록, `/todo done 번호|ID`

`/new`, `/task`, `/stop`, `/retry` 을 인자 없이 보내면 사용법을 답한다. 슬래시 명령은 LLM을 부르지 않는다.

## 프로젝트 지정

`config/projects.toml` 의 첫 항목(deskmini)이 기본 프로젝트다. 다른 프로젝트는 `/new sandbox: 할 일`(또는 `/new sandbox 할 일`,
`작업: sandbox: 할 일`)처럼 앞에 ID 를 붙인다. 대화 발주에서 모델이 프로젝트를 정하지 않으면(null) 기본 프로젝트로 발주 카드가 뜨고,
카드에 프로젝트가 보이므로 착수 전에 취소할 수 있다. 등록되지 않은 ID 를 모델이 내면 발주하지 않는다.

`sandbox`(`~/sandbox-chief`, 원격 없음, 배포 없음)는 실제 처리 경로 시험용 연습 저장소다. 병합까지 해도 운영과 무관하다.

## 재시도

FAILED·BLOCKED 가 되면 이유(작업자 오류, 실패한 검증 명령 끝부분, 검토 요약)와 버튼을 바로 보낸다. 버튼은 작업 버전에 묶인 일회용 승인이다.

- 작업자 단계 실패·검증 실패: [🔁 다시 시도] → 같은 worktree 를 이어서 쓰는 새 실행 차수(runs.attempt+1). 이전 실패 요약을
  mission 의 `previous_attempt` 로 넘긴다. 작업 `budget.retries`(기본 1)를 넘으면 버튼 대신 "새로 발주" 안내.
- 작업자는 끝났는데 독립 검토를 못 한 경우(검토 제공자 응답 없음·UNVERIFIED): [🔁 검증 다시 시도] → 작업자를 다시 돌리지 않고 검증만.
- 사용량 한도(LIMIT_BLOCKED): 오류 문구의 `resets 3pm (Asia/Seoul)`·`resets 3am (UTC)`·`resets Oct 10, 10:30pm (…)`·`resets in 2 hours`
  에서 풀리는 시각을 읽어 알려 주고, 그 전에는 [⏰ 시각+5분 에 다시 시도 예약] 버튼만 준다. 누르면 `scheduled_retries` 에 저장되고
  메인 루프가 그 시각에 한 번 실행한다(그 사이 작업 버전이 바뀌면 실행하지 않음). 한도 차단 실행은 재시도 횟수에 세지 않는다.

## 사진·문서로 지시

사진(가장 큰 크기)·문서를 받으면 `getFile` 로 내려받아 `state/inbox/<날짜>/` 에 0600 으로 저장하고 `attachments` 테이블에 남긴다.

- 20MB 이하, jpg·png·webp·pdf·txt·md·csv·json 만. zip·실행 파일·스크립트·오피스 문서는 거부. 내용 서명(매직 바이트, UTF-8·NUL 없음)이
  확장자와 맞아야 하고 ELF·PE·셔뱅·zip 서명은 확장자와 무관하게 거부. 이름은 정리하고 숨김·비밀처럼 보이는 이름은 거부
- 캡션이 있으면 캡션 + `[첨부: 이름, 형식, 크기]`(텍스트는 마스킹된 앞부분 1,500자)를 대화층에 준다. 캡션이 `/new …` 면 바로 발주 카드
- 캡션이 없으면 무엇을 할지 묻고, 30분 안의 다음 지시(발주)에 붙인다
- 발주 카드에 첨부 목록이 보이고, 착수 때 worktree `.chief-inbox/` 에 읽기 전용(0444)으로 복사한다(텍스트는 마스킹 사본).
  Git 공용 `info/exclude` 에 `/.chief-inbox/` 를 넣어 커밋되지 않는다. mission 에 `attachments` 로 알린다. 독립 검토도 텍스트 첨부 내용을 받는다
- 이미지는 이번 라운드에서 대화 모델(Gemini·Claude API)에 보내지 않는다(메타데이터만). 작업자(Claude CLI)는 worktree 의 첨부를 읽을 수 있다

## 작업 배분(agent-hub 할 일)

`/todo codex 할 일` 이나 자연어("코덱스한테 ○○ 남겨 둬" → 대화 동작 `dispatch_todo`)는 승인 없이 `~/agent-hub/bin/hub todo add <대상> "<내용>" --from telegram`
을 고정 argv 로 부른다(셸 없음, 마스킹·한 줄·500자). hub 가 `todo/<시각>-<호스트>-<대상>.md` 를 쓰고 커밋·push 한다. 다른 PC 의 Codex·Claude 는
`hub read` 맨 위 "열린 할 일"에서 본다. 봇 서비스는 `~/agent-hub` 에 쓸 수 있어야 한다(ReadWritePaths).

## 독립 검토 제공자

`config/models.toml` `[review] provider` = `gemini`(기본) | `claude_cli`. `claude_cli` 는 로컬 Claude Code CLI(구독, API 키 없음)를 작업자와 같은
Bubblewrap 격리(빈 임시 홈, 자격 증명 파일만 읽기 전용, 도구 없음)에서 `systemd-run --user` 로 실행한다. 검토를 못 하면 READY 가 아니라 BLOCKED.

## 실제 처리 경로 시험(E2E)

```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python sim/e2e_real.py "할 일" ["할 일2" …] [--no-merge] [--attach 파일]
XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python sim/e2e_real.py --state <상태폴더> --retry T-... x
```

sandbox 프로젝트만 대상으로, 별도 상태 폴더(`~/.local/state/deskmini-chief-e2e/<시각>`)에서 실제 runner·worker·verify·release 를 돈다.
Telegram 은 기록기, Gemini 는 호출 시 예외, 검토는 Claude CLI. 버튼은 봇이 띄운 callback_data 를 그대로 눌러 승인 경로를 확인한다.
작업자 Claude 실행 1회씩 쓰므로 구독 한도를 확인하고 돌린다.

## 먼저 알리기(감시기)

`app/watch.py` 가 메인 루프에서 `config/alerts.toml` 의 `interval_seconds`(기본 60초)마다 한 번 관측한다.
관측은 읽기 전용이다: `docker ps`·`tmux has-session`(고정 argv, 짧은 타임아웃), `/proc/meminfo`, 디스크 사용량,
봇 DB, `~/work` 마커 파일. 처음 관측은 기준선으로만 저장하고(`watch_state`), 이후 바뀐 것만 알린다.

| 대상 | 알림 | 등급 |
|---|---|---|
| 컨테이너 portainer·deskmini 실행↔멈춤 | 멈춤 / 다시 실행 | critical / info |
| 디스크 여유 10% 미만 또는 20GB 미만 | 부족 / 회복 | critical / info |
| 메모리 사용 가능 10% 미만 3회 연속 | 부족 / 회복 | critical / info |
| Remote Control tmux 세션(rc) | 사라짐 / 다시 있음 | warn / info |
| 봇 작업 READY·FAILED·BLOCKED(한도 차단 구분)·INTERRUPTED | 전이 | info / warn |
| 장시간 작업 | 시작·멈춤 의심·렌더 완료·후처리 완료·실패·후처리 미완 | info / warn |

봇이 이미 보낸 검증 결과(병합 승인 카드·실패 카드)는 감시기가 다시 보내지 않는다.

장시간 작업은 `config/jobs.toml` 의 패턴(기본 `~/work/*/render/*`, `~/work/*/revisions/*/render/*`) 아래
`STARTED` 마커가 있는 폴더다. `FAILED` → 실패, `POSTPROCESS_COMPLETE` → 후처리까지 완료, `COMPLETE` → 렌더 완료,
`STARTED` 만 있으면 로그가 `stalled_minutes`(기본 30분) 안에 갱신됐는지로 진행 중/멈춤 의심. `COMPLETE` 인데
`POSTPROCESS_COMPLETE` 가 없고 `postprocess*.log` 가 멈춰 있으면 "후처리 미완" 경고. 프로세스 생존은 보지 않는다.

알림 규칙(`app/alerts.py`): 같은 키는 `dedupe_minutes`(60분) 안에 다시 보내지 않고(봇 작업은 7일),
critical 은 항상 즉시 보낸다. 그 외는 `/alerts off`·`/quiet`·조용한 시간(23:00–07:00 Asia/Seoul)·시간당 상한
(`hourly_cap`, 8건)에 걸리면 보류한다. 조용한 시간·끔으로 보류된 것은 08:30 아침 요약에, `/quiet`·상한으로
보류된 것은 풀리는 대로 한 건으로 묶어 보낸다. 알림은 outbox 를 거쳐 실패하면 재시도한다(Telegram 400 은 즉시, 그 밖은 20번 실패하면 FAILED 로 멈추고 로그에 남긴다).
알림 버튼: [자세히] [결과물 받기] [로그 보기] [1시간 조용히]. 버튼 데이터는 DB 행 번호(`al:12`, `jf:3`, `jl:3`,
`f:7`, `q:60`)만 싣고 경로는 싣지 않는다. 버튼도 등록된 개인 채팅·사용자만 처리한다.

아침 요약은 매일 `brief_time`(08:30) 에 한 번(날짜별 중복 방지, 놓치면 4시간 안에서만), 템플릿으로 만든다:
지난 24시간 알림(보류분 표시), 장시간 작업 상태 수, 봇 작업 변화, 마지막 감시 기준 서버 상태.

## 결과물 받기

`/files`, 알림의 [결과물 받기], 자연어("영상 보내줘" → 대화 동작 `send_files`)는 결과물 목록 버튼만 띄우고,
파일은 버튼을 눌러야 간다. 결과물 = 작업 폴더의 `deliverable_dirs`(`web*`, `out*`, `final*`, `export*`) 하위 파일과
작업 폴더 바로 아래 파일(`frames/`·`layer/` 같은 중간 산출물 제외). 경로 규칙은 `app/files.py`:

- 허용 루트: `~/work`, 봇 state 의 `evidence/` 만. 절대 경로만, `..` 거부, `resolve()` 후 루트 포함 여부 확인(심볼릭 링크 탈출 차단)
- 숨김 파일·폴더, 비밀처럼 보이는 이름(env, secret, token, key, credential, password, id_rsa, .pem 등) 거부
- 확장자 허용 목록: mp4 webm mov jpg jpeg png gif webp pdf txt md json csv log
- 50MB 초과는 보내지 않고 크기와 경로만 알린다. 전송 직전에 같은 검사를 다시 한다
- txt·md·json·csv·log 는 마스킹한 본문으로 보낸다(짧으면 메시지, 길면 마스킹된 내용을 문서로 업로드)
- mp4 → sendVideo, jpg·png·webp(10MB 이하) → sendPhoto, 그 외 → sendDocument. 표준 라이브러리 multipart 업로드

## 대화 응답층

슬래시 명령이 아닌 메시지는 `app/chat.py` 가 답한다. 매 턴 다음을 모델에 준다.

- 고정 시스템 지침(비서실장 역할, 근거 없으면 "확인 불가", 비밀값 금지) — Claude 에서는 프롬프트 캐시
- 읽기 전용 상태 스냅샷(`app/context.py`): 서버 부하·메모리·디스크·Docker·rc tmux, 봇 작업, agent-hub
  BOARD·잠금·최근 기록, 등록 저장소 최근 커밋, `~/work` 최근 폴더. 마스킹 후 약 7천 자 이하
- 최근 대화 10턴(`chat_messages` 테이블, 마스킹 저장, 200행 유지)

모델은 JSON `{reply, action}` 을 돌려준다. `reply` 는 HTML 이스케이프·마스킹 후 보내고, `action`
(create_task / stop_task / task_detail / merge / push / deploy / send_files / set_alerts)은 `Policy.from_chat_action` 이 검사해
기존 경로로 보낸다. 작업 발주는 🚀 착수 버튼 카드, 중단은 중단 버튼, 병합·push·배포는 승인 카드가 뜰 뿐
모델 응답만으로 실행되지 않는다. `send_files`(작업 힌트)는 결과물 목록 버튼, `set_alerts`(on/off/기간)는 알림 설정으로
가며 둘 다 읽기 전용·되돌릴 수 있는 동작이라 승인 카드가 없다. 스냅샷에는 `jobs_recent`(장시간 작업 상태)와 `alerts`(설정)가 들어간다. 매 응답의 제공자·모델·제안 동작·지연·토큰 사용량은 `events` 에
`CHAT_DECISION` 으로, 전부 실패하면 `CHAT_UNAVAILABLE` 로 남는다. 두 제공자가 모두 실패하면 고정 명령
안내를 보낸다. 설정은 `config/models.toml` 의 `[chat]`(제공자 순서·모델·effort)과 `[gemini]`(모델 사다리 또는 고정 모델).

### Claude API 켜기

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/notify.env
```

키는 메시지마다 env 파일에서 다시 읽으므로 **재시작이 필요 없다**. `/model` 로 전환을 확인한다.
키를 지우면 다시 Gemini 로 돌아간다.

> 경고: 이 키를 `~/.bashrc`, `~/.profile`, `/etc/environment` 등 전역 환경변수에 넣지 않는다.
> 그러면 이 서버의 대화형 Claude Code 세션이 구독 대신 API 키로 과금된다. 봇은 키를 `os.environ` 에
> 넣지 않고 SDK 클라이언트에만 직접 넘긴다.

Gemini 무료 키는 모델당 하루 약 20회라 대화가 잦으면 Claude 키를 넣는 것을 권한다.

## 배포 등록 조건

기본 설정은 `deploy_enabled = false`다. 스테이징 배포를 등록할 때만 환경을 정확히 `staging`으로
지정하고, argv 배열 형식의 배포·건강검사·롤백·배포 버전 조회 명령을 모두 등록한다. 버전 조회는
현재 스테이징에 실제로 떠 있는 40자리 Git SHA 하나를 출력해야 한다. 운영 대상은 등록하지 않는다.

## 보존·복구

- SQLite 백업 7세대, 로그 7일, 증거 90일을 유지한다.
- 미완료 작업 디렉터리와 worktree는 자동 삭제하지 않는다.
- 재시작 시 미처리 Telegram 업데이트는 최대 3회 재생한다.
- 중단된 릴리스 작업은 성공으로 추측하지 않고 `UNKNOWN`으로 수렴한다.
