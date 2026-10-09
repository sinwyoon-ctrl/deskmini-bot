# 공용 소통창구

DeskMini 와 메인 PC 에서 Codex 와 Claude 가 번갈아 작업한다. 작업 결과는 커밋으로 전달한다. 규칙은 `~/agent-hub/README.md` 를 따른다.

- 시작: `~/bin/hub read`
- 수정 전: `~/bin/hub claim claude deskmini-bot "할 일"`
- 끝: `~/bin/hub done claude deskmini-bot "한 일 · 테스트 결과 · 남은 일"` (커밋·push·로그·잠금 해제·알림)
- 테스트: `XDG_RUNTIME_DIR=/run/user/$(id -u) python3 -m unittest discover -s tests`
