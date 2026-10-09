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

`/new`, `/task`, `/stop` 을 인자 없이 보내면 사용법을 답한다. 슬래시 명령은 LLM을 부르지 않는다.

## 대화 응답층

슬래시 명령이 아닌 메시지는 `app/chat.py` 가 답한다. 매 턴 다음을 모델에 준다.

- 고정 시스템 지침(비서실장 역할, 근거 없으면 "확인 불가", 비밀값 금지) — Claude 에서는 프롬프트 캐시
- 읽기 전용 상태 스냅샷(`app/context.py`): 서버 부하·메모리·디스크·Docker·rc tmux, 봇 작업, agent-hub
  BOARD·잠금·최근 기록, 등록 저장소 최근 커밋, `~/work` 최근 폴더. 마스킹 후 약 7천 자 이하
- 최근 대화 10턴(`chat_messages` 테이블, 마스킹 저장, 200행 유지)

모델은 JSON `{reply, action}` 을 돌려준다. `reply` 는 HTML 이스케이프·마스킹 후 보내고, `action`
(create_task / stop_task / task_detail / merge / push / deploy)은 `Policy.from_chat_action` 이 검사해
기존 경로로 보낸다. 작업 발주는 🚀 착수 버튼 카드, 중단은 중단 버튼, 병합·push·배포는 승인 카드가 뜰 뿐
모델 응답만으로 실행되지 않는다. 매 응답의 제공자·모델·제안 동작·지연·토큰 사용량은 `events` 에
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
