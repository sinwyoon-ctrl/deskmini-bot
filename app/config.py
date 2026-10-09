from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME_RE.fullmatch(name):
            continue
        values[name] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    repo_path: Path
    worktree_root: Path
    default_branch: str
    checks: tuple[tuple[str, ...], ...]
    deploy_enabled: bool
    deploy_environment: str | None
    deploy_command: tuple[str, ...] | None
    healthcheck_command: tuple[str, ...] | None
    rollback_command: tuple[str, ...] | None
    deployed_version_command: tuple[str, ...] | None


@dataclass(frozen=True)
class Settings:
    root: Path
    state_dir: Path
    db_path: Path
    telegram_token: str
    allowed_chat_id: int
    allowed_user_id: int
    telegram_timeout_seconds: int
    gemini_script: Path
    max_runtime_seconds: int
    max_active_tasks: int
    approval_ttl_seconds: int
    projects: dict[str, ProjectConfig]

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        project_root = (root or Path(__file__).resolve().parents[1]).resolve()
        home = Path.home()
        env_path = Path(
            os.environ.get(
                "DESKMINI_CHIEF_ENV",
                str(home / ".config" / "notify.env"),
            )
        ).expanduser()
        file_env = load_env_file(env_path)

        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, file_env.get(name, default))

        token = env("TELEGRAM_BOT_TOKEN")
        chat_raw = env("TELEGRAM_CHAT_ID")
        user_raw = env("TELEGRAM_USER_ID", chat_raw)
        if not token:
            raise RuntimeError(f"TELEGRAM_BOT_TOKEN missing in {env_path}")
        try:
            chat_id = int(chat_raw)
            user_id = int(user_raw)
        except ValueError as exc:
            raise RuntimeError("Telegram chat/user id must be numeric") from exc

        state_dir = Path(
            env(
                "DESKMINI_CHIEF_STATE_DIR",
                str(home / ".local" / "state" / "deskmini-chief"),
            )
        ).expanduser().resolve()
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in ("tasks", "evidence", "logs", "backups", "worktrees"):
            (state_dir / child).mkdir(exist_ok=True, mode=0o700)

        projects = load_projects(project_root / "config" / "projects.toml", state_dir)
        return cls(
            root=project_root,
            state_dir=state_dir,
            db_path=state_dir / "chief.sqlite",
            telegram_token=token,
            allowed_chat_id=chat_id,
            allowed_user_id=user_id,
            telegram_timeout_seconds=int(env("TELEGRAM_POLL_TIMEOUT", "10")),
            gemini_script=Path(
                env(
                    "GEMINI_REST_SCRIPT",
                    str(home / ".config" / "ione" / "gemini_rest.py"),
                )
            ).expanduser(),
            max_runtime_seconds=int(env("DESKMINI_TASK_TIMEOUT", "1800")),
            max_active_tasks=int(env("DESKMINI_MAX_ACTIVE_TASKS", "1")),
            approval_ttl_seconds=int(env("DESKMINI_APPROVAL_TTL", "600")),
            projects=projects,
        )


def _command(value: Any, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise RuntimeError(f"{field} must be a non-empty argv array")
    return tuple(value)


def load_projects(path: Path, state_dir: Path) -> dict[str, ProjectConfig]:
    if not path.is_file():
        raise RuntimeError(f"Project registry missing: {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    projects: dict[str, ProjectConfig] = {}
    for project_id, data in (raw.get("projects") or {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,40}", project_id):
            raise RuntimeError(f"Invalid project id: {project_id}")
        repo = Path(data["repo_path"]).expanduser().resolve()
        checks_raw = data.get("checks") or []
        checks = tuple(
            _command(command, f"projects.{project_id}.checks") or ()
            for command in checks_raw
        )
        deploy_enabled = bool(data.get("deploy_enabled", False))
        deploy_environment = data.get("deploy_environment")
        if deploy_enabled and deploy_environment != "staging":
            raise RuntimeError(f"projects.{project_id} may deploy only to staging")
        projects[project_id] = ProjectConfig(
            project_id=project_id,
            repo_path=repo,
            worktree_root=Path(
                data.get("worktree_root", str(state_dir / "worktrees"))
            ).expanduser().resolve(),
            default_branch=data.get("default_branch", "main"),
            checks=checks,
            deploy_enabled=deploy_enabled,
            deploy_environment=deploy_environment,
            deploy_command=_command(data.get("deploy_command"), "deploy_command"),
            healthcheck_command=_command(data.get("healthcheck_command"), "healthcheck_command"),
            rollback_command=_command(data.get("rollback_command"), "rollback_command"),
            deployed_version_command=_command(
                data.get("deployed_version_command"), "deployed_version_command"
            ),
        )
    if not projects:
        raise RuntimeError("At least one registered project is required")
    return projects
