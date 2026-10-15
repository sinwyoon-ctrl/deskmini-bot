from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from app.collector import bytes_human, collect_host_status, run_fixed
from app.jobs import STATUS_LABELS, JobsConfig, discover_jobs
from app.redact import redact_text

# 서버 시계는 UTC 다. 사용자에게 보이는 시각은 알림 설정과 같은 한국 시간으로 맞춘다.
DISPLAY_TZ = ZoneInfo("Asia/Seoul")


ACTIVE_STATES = (
    "QUEUED",
    "STARTING",
    "RUNNING",
    "VERIFYING",
    "STOP_REQUESTED",
    "STOPPING",
)
SNAPSHOT_LIMIT = 7_000
JOBS_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "jobs.toml"


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
    return datetime.fromtimestamp(stamp, UTC).astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M")


def _read(path: Path, limit: int) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            # 끝에 걸친 토큰이 잘린 채 남지 않게 조금 더 읽어 마스킹한 뒤 자른다.
            text = handle.read(limit + 512)
    except OSError:
        return ""
    text = redact_text(text.strip(), limit + 513)
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
        "commits": [redact_text(line, len(line) + 1)[:120] for line in log.stdout.splitlines()[:commits]] if log.ok else [],
        "uncommitted": len(status.stdout.splitlines()) if status.ok else None,
        "error": None if log.ok else redact_text(log.stdout, len(log.stdout) + 1)[:200],
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


def project_summary(projects: dict[str, Any] | None, home: Path) -> dict[str, Any]:
    """발주 가능한 등록 프로젝트 ID → 저장소 경로. 하나뿐이면 모델이 되묻지 않고 그것을 쓴다.
    default 는 프로젝트를 말하지 않은 발주가 가는 기본 프로젝트(projects.toml 첫 항목)."""
    items = {
        str(project_id): home_relative(Path(getattr(project, "repo_path", "") or ""), home)
        for project_id, project in (projects or {}).items()
    }
    descriptions = {
        str(project_id): str(getattr(project, "description", "") or "")
        for project_id, project in (projects or {}).items()
    }
    return {
        "registered": items,
        "only": next(iter(items)) if len(items) == 1 else None,
        "default": next(iter(items), None),
        # 능력 지도: 코드 작업은 아래 저장소 파일 수정만 가능하다. 작업자는 영상·이미지 렌더나 외부 도구를 쓰지 못한다.
        "descriptions": descriptions,
    }


def build_snapshot(
    store: Any, paths: WorkspacePaths | None = None, projects: dict[str, Any] | None = None
) -> str:
    """LLM 에 줄 읽기 전용 상태 요약. 모든 값은 마스킹 후 길이를 제한한다."""
    paths = paths or WorkspacePaths.default()
    sections: dict[str, Any] = {}
    if projects is not None:
        sections["projects"] = project_summary(projects, paths.home)
    try:
        sections["host"] = host_summary()
    except Exception as exc:  # 관찰 실패는 맥락에서 '미확인'으로 드러낸다.
        sections["host"] = f"미확인: {type(exc).__name__}"
    sections["bot_tasks"] = task_summaries(store)
    try:
        with store.connect() as db:
            sections["orca_tasks"] = [dict(r) for r in db.execute(
                "SELECT id,project,agent,model,state,summary,error,updated_at FROM orca_requests ORDER BY rowid DESC LIMIT 5")]
    except Exception:
        sections["orca_tasks"] = "미확인"
    try:
        from app.codex_usage import latest_codex_limits
        limits = latest_codex_limits(paths.home)
        if limits:
            sections["codex_limits"] = {
                key: ({"used_percent": w["used_percent"], "window_minutes": w["window_minutes"],
                       "resets_at": w["resets_at"].astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M") if w["resets_at"] else None}
                      if (w := limits.get(key)) else None)
                for key in ("primary", "secondary")
            } | {"observed_at": limits["observed_at"].astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M"), "note": "마지막 DeskMini Codex 실행 기준"}
    except Exception:
        sections["codex_limits"] = "미확인"
    sections["agent_hub_board"] = board_text(paths, 1_500)
    sections["agent_hub_locks"] = hub_locks(paths)
    sections["agent_hub_recent_log"] = recent_hub_logs(paths, 5, 200)
    sections["repos"] = {
        name: repo_summary(path) for name, path in registered_repos(paths)
    }
    sections["work_dirs_recent"] = recent_work_dirs(paths)
    sections["jobs_recent"] = jobs_summary(paths)
    sections["alerts"] = alerts_summary(store)
    sections["alerts_recent"] = recent_alerts(store)
    text = json.dumps(sections, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > SNAPSHOT_LIMIT:
        # Preserve section names and valid JSON; targeted lookups retrieve omitted evidence.
        for name in ("agent_hub_board", "repos", "agent_hub_recent_log", "orca_tasks", "bot_tasks", "host"):
            if len(text) <= SNAPSHOT_LIMIT:
                break
            raw = json.dumps(sections[name], ensure_ascii=False, default=str)
            sections[name] = {"excerpt": redact_text(raw, 600), "truncated": True}
            text = json.dumps(sections, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > SNAPSHOT_LIMIT:
        # Pathological configuration/records: still explicitly expose every source, never cut JSON mid-token.
        sections = {name: {"excerpt": redact_text(json.dumps(value, ensure_ascii=False, default=str), 300),
                           "truncated": True} for name, value in sections.items()}
    from app.redact import redact
    return json.dumps(redact(sections), ensure_ascii=False, separators=(",", ":"), default=str)


def jobs_summary(paths: WorkspacePaths, count: int = 6) -> list[dict[str, Any]]:
    """장시간 작업(렌더 폴더) 최근 상태. 마커 파일로만 추정한 값이다."""
    try:
        jobs = discover_jobs(JobsConfig.load(JOBS_CONFIG_PATH), paths.home)
    except Exception:
        return []
    items = []
    for job in jobs[:count]:
        status = STATUS_LABELS.get(job.status, job.status)
        if job.postprocess_missing:
            status += "(후처리 미완)"
        items.append(
            {
                "job": job.label,
                "status": status,
                "deliverables": job.has_deliverables,
                "last_activity": datetime.fromtimestamp(job.last_activity, UTC).astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M"),
            }
        )
    return items


def alerts_summary(store: Any) -> dict[str, Any]:
    try:
        from app.alerts import AlertStore

        prefs = AlertStore(store).prefs()
    except Exception:
        return {}
    return {
        "enabled": prefs.enabled,
        "muted_until": prefs.muted_until.isoformat(timespec="minutes") if prefs.muted_until else None,
    }


def recent_alerts(store: Any, count: int = 5) -> list[dict[str, str]]:
    """봇이 최근 기록한 알림(새것부터). 시각·등급·짧은 내용·상태(SENT 보냄, QUEUED 보류 등). 마스킹 후 짧게."""
    try:
        from app.alerts import AlertStore

        rows = AlertStore(store).list_alerts(statuses=("SENT", "QUEUED", "DIGESTED", "BRIEFED"), limit=count)
    except Exception:
        return []
    items = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row["created_at"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            when = stamp.astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            when = "?"
        text = str(row.get("title") or "")
        if row.get("body"):
            text += " — " + str(row["body"])
        text = " ".join(redact_text(text, len(text) + 1).split())
        items.append(
            {
                "at": when,
                "severity": str(row.get("severity") or ""),
                "text": text if len(text) <= 120 else text[:120].rstrip() + " …",
                "status": str(row.get("status") or ""),
            }
        )
    return items


def home_relative(path: Path, home: Path | None = None) -> str:
    home = home or Path.home()
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)
