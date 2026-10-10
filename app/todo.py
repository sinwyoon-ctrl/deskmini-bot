"""agent-hub 할 일(todo) 연결. `hub todo …` 를 고정 argv 로 부른다(셸 없음).

봇 서비스는 ~/agent-hub 에 쓸 수 있어야 한다(deploy/deskmini-chief.service 의 ReadWritePaths).
hub 스크립트가 커밋·push 까지 한다. 출력은 마스킹 후 짧게 자른다.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.redact import redact_text


ASSIGNEES = ("codex", "claude", "any")
TODO_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,60}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class HubResult:
    ok: bool
    output: str


class HubTodo:
    def __init__(self, hub_root: Path, *, timeout: float = 60.0, runner=subprocess.run):
        self.hub_root = hub_root
        self.script = hub_root / "bin" / "hub"
        self.timeout = timeout
        self.runner = runner

    def available(self) -> bool:
        return self.script.is_file()

    def _run(self, *args: str) -> HubResult:
        if not self.available():
            return HubResult(False, "agent-hub 스크립트를 찾지 못했습니다")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR"}
        }
        environment["AGENT_HUB"] = str(self.hub_root)
        environment["HUB_NO_NOTIFY"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        try:
            completed = self.runner(
                ["/bin/bash", str(self.script), *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=environment,
                cwd=str(self.hub_root),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return HubResult(False, type(exc).__name__)
        return HubResult(completed.returncode == 0, redact_text(completed.stdout or "", 3_000).strip())

    def add(self, assignee: str, text: str) -> HubResult:
        if assignee not in ASSIGNEES:
            return HubResult(False, "대상은 codex·claude·any 중 하나입니다")
        clean = " ".join(CONTROL_RE.sub(" ", redact_text(text, 500)).split())[:500]
        if not clean:
            return HubResult(False, "할 일 내용이 비었습니다")
        return self._run("todo", "add", assignee, clean, "--from", "telegram")

    def list(self) -> HubResult:
        return self._run("todo", "list")

    def done(self, key: str) -> HubResult:
        if not (TODO_ID_RE.fullmatch(key) or re.fullmatch(r"\d{1,3}", key)):
            return HubResult(False, "번호나 ID 형식이 아닙니다")
        return self._run("todo", "done", key, "telegram")
