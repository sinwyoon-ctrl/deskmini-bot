from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.collector import bytes_human, collect_host_status, run_fixed
from app.redact import redact_text


ACTIVE_STATES = (
    "QUEUED",
    "STARTING",
    "RUNNING",
    "VERIFYING",
    "STOP_REQUESTED",
    "STOPPING",
)
SNAPSHOT_LIMIT = 7_000


@dataclass(frozen=True)
class WorkspacePaths:
    """읽기 전용으로 관찰하는 경로. 테스트에서는 임시 디렉터리로 바꾼다."""

    home: Path
    agent_hub: Path
    work: Path

    @classmethod
    def default(cls) -> "WorkspacePaths":
        home = Path.home()
        return cls(home=home, agent_hub=home / "agent-hub", work=home / "work")


def _stamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _mtime(path: Path) -> str:
    stamp = _stamp(path)
    if not stamp:
        return "?"
    return datetime.fromtimestamp(stamp, UTC).astimezone().strftime("%m-%d %H:%M")


def _read(path: Path, limit: int) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(limit + 1)
    except OSError:
        return ""
    text = redact_text(text.strip(), limit + 1)
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def board_text(paths: WorkspacePaths, limit: int = 1_500) -> str:
    return _read(paths.agent_hub / "BOARD.md", limit)


def hub_locks(paths: WorkspacePaths) -> list[str]:
    lines = []
    for lock in sorted((paths.agent_hub / "locks").glob("*.lock"))[:10]:
        content = _read(lock, 200).replace("\n", " ")
        lines.append(f"{lock.stem}: {content}")
    return lines


def recent_hub_logs(paths: WorkspacePaths, count: int = 5, limit: int = 240) -> list[dict[str, str]]:
    files = [item for item in (paths.agent_hub / "log").glob("*/*") if item.is_file()]
    if not files:
        return []
    files.sort(key=lambda item: (_stamp(item), item.name), reverse=True)
    return [
        {"file": item.name, "text": _read(item, limit).replace("\n", " ")}
        for item in files[:count]
    ]


def registered_repos(paths: WorkspacePaths) -> list[tuple[str, Path]]:
    conf = paths.agent_hub / "hosts" / "ionenet.conf"
    try:
        raw_lines = conf.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    repos = []
    for raw in raw_lines:
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) < 2:
            continue
        name, location = parts[0], parts[1]
        location = location.replace("${HOME}", str(paths.home)).replace("$HOME", str(paths.home))
        repos.append((name, Path(location).expanduser()))
    return repos[:10]


def repo_summary(path: Path, commits: int = 3) -> dict[str, Any]:
    if not (path / ".git").exists():
        return {"ok": False, "error": "git 저장소 아님 또는 접근 불가"}
    log = run_fixed(
        ("git", "-C", str(path), "log", f"-{commits}", "--format=%h %cd %s", "--date=format-local:%m-%d %H:%M"),
        timeout=3.0,
    )
    status = run_fixed(
        ("git", "--no-optional-locks", "-C", str(path), "status", "--porcelain=v1"),
        timeout=3.0,
    )
    return {
        "ok": log.ok,
        "commits": [line[:120] for line in log.stdout.splitlines()[:commits]] if log.ok else [],
        "uncommitted": len(status.stdout.splitlines()) if status.ok else None,
        "error": None if log.ok else log.stdout[:200],
    }


def recent_work_dirs(paths: WorkspacePaths, count: int = 5) -> list[dict[str, str]]:
    try:
        dirs = [item for item in paths.work.iterdir() if item.is_dir() and not item.name.startswith(".")]
    except OSError:
        return []
    dirs.sort(key=_stamp, reverse=True)
    return [{"name": item.name, "modified": _mtime(item)} for item in dirs[:count]]


def task_summaries(store: Any, recent: int = 5) -> dict[str, list[dict[str, Any]]]:
    def compact(task: dict[str, Any]) -> dict[str, Any]:
        spec = task.get("specification") or {}
        return {
            "id": task["id"],
            "state": task["state"],
            "project": task["project_id"],
            "objective": redact_text(str(spec.get("objective", "")), 100),
            "updated_at": task.get("updated_at"),
        }

    return {
        "active": [compact(task) for task in store.list_tasks_in_states(ACTIVE_STATES)],
        "recent": [compact(task) for task in store.list_tasks(recent)],
    }


def host_summary() -> dict[str, Any]:
    status = collect_host_status()
    docker = status["docker"]
    return {
        "observed_at": status["observed_at"],
        "load": [round(value, 2) for value in status["load_average"]],
        "memory_available": bytes_human(status["memory"].get("available")),
        "memory_total": bytes_human(status["memory"].get("total")),
        "disk_free": bytes_human(status["disk"]["free"]),
        "remote_control_tmux": status["remote_control"]["tmux_session"],
        "docker": (
            [f"{item.get('name')}: {item.get('status') or item.get('state')}" for item in docker["containers"][:8]]
            if docker.get("ok")
            else f"미확인: {str(docker.get('error') or '')[:120]}"
        ),
    }


def build_snapshot(store: Any, paths: WorkspacePaths | None = None) -> str:
    """LLM 에 줄 읽기 전용 상태 요약. 모든 값은 마스킹 후 길이를 제한한다."""
    paths = paths or WorkspacePaths.default()
    sections: dict[str, Any] = {}
    try:
        sections["host"] = host_summary()
    except Exception as exc:  # 관찰 실패는 맥락에서 '미확인'으로 드러낸다.
        sections["host"] = f"미확인: {type(exc).__name__}"
    sections["bot_tasks"] = task_summaries(store)
    sections["agent_hub_board"] = board_text(paths, 1_500)
    sections["agent_hub_locks"] = hub_locks(paths)
    sections["agent_hub_recent_log"] = recent_hub_logs(paths, 5, 200)
    sections["repos"] = {
        name: repo_summary(path) for name, path in registered_repos(paths)
    }
    sections["work_dirs_recent"] = recent_work_dirs(paths)
    text = json.dumps(sections, ensure_ascii=False, separators=(",", ":"), default=str)
    return redact_text(text, SNAPSHOT_LIMIT)


def home_relative(path: Path, home: Path | None = None) -> str:
    home = home or Path.home()
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)
