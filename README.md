# DeskMini AI 비서실장

Telegram 개인 채팅을 주 인터페이스로 사용해 DeskMini 상태 확인, Claude Code 작업 발주, 진행 보고,
중단, 검증, 승인형 병합·push·스테이징 배포를 제공한다.

## 현재 정책

- 단일 대표 개인 채팅만 허용
- 활성 작업 1개
- 사용자 입력의 임의 셸 실행 금지
- Gemini는 `~/.config/ione/gemini_rest.py` REST 경로만 사용
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
python3 -m unittest discover -s tests -v
python3 -m app.preflight
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

자연어 입력도 지원하지만 Gemini 장애 시 고정 명령은 계속 동작한다.

## 배포 등록 조건

기본 설정은 `deploy_enabled = false`다. 스테이징 배포를 등록할 때만 환경을 정확히 `staging`으로
지정하고, argv 배열 형식의 배포·건강검사·롤백·배포 버전 조회 명령을 모두 등록한다. 버전 조회는
현재 스테이징에 실제로 떠 있는 40자리 Git SHA 하나를 출력해야 한다. 운영 대상은 등록하지 않는다.

## 보존·복구

- SQLite 백업 7세대, 로그 7일, 증거 90일을 유지한다.
- 미완료 작업 디렉터리와 worktree는 자동 삭제하지 않는다.
- 재시작 시 미처리 Telegram 업데이트는 최대 3회 재생한다.
- 중단된 릴리스 작업은 성공으로 추측하지 않고 `UNKNOWN`으로 수렴한다.
