# 공용 소통창구

이 서버에서는 Codex 와 Claude 가 번갈아 작업한다. 규칙은 `~/agent-hub/README.md` 를 따른다.

- 시작: `~/bin/hub read`
- 수정 전: `~/bin/hub claim codex deskmini-bot "할 일"`
- 종료: `~/bin/hub log codex deskmini-bot "한 일 · 테스트 결과 · 남은 일" --notify` 다음에 `~/bin/hub release codex deskmini-bot`
- 테스트: `XDG_RUNTIME_DIR=/run/user/$(id -u) python3 -m unittest discover -s tests`
