"""DeskMini Codex 의 사용 한도를 세션 기록(~/.codex/sessions/**/rollout-*.jsonl)에서 읽는다.

Codex 는 매 응답마다 token_count 이벤트에 rate_limits 를 남긴다. 로그인 파일(auth.json)은 읽지 않는다.
값은 마지막 Codex 실행 시점 기준이므로 관측 시각을 함께 돌려준다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TAIL_BYTES = 262_144


def _latest_rollouts(sessions: Path, limit: int = 5) -> list[Path]:
    if not sessions.is_dir():
        return []
    files = [p for p in sessions.glob("*/*/*/rollout-*.jsonl") if p.is_file() and not p.is_symlink()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def _last_rate_limits(path: Path) -> tuple[dict[str, Any], str | None] | None:
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - TAIL_BYTES))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _find_key(event, "rate_limits")
        if isinstance(found, dict) and found.get("primary"):
            return found, event.get("timestamp")
    return None


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            hit = _find_key(item, key)
            if hit is not None:
                return hit
    elif isinstance(value, list):
        for item in value:
            hit = _find_key(item, key)
            if hit is not None:
                return hit
    return None


def _window(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        used = float(raw.get("used_percent"))
    except (TypeError, ValueError):
        return None
    minutes = raw.get("window_minutes")
    resets = raw.get("resets_at")
    return {
        "used_percent": round(used, 1),
        "window_minutes": int(minutes) if isinstance(minutes, (int, float)) else None,
        "resets_at": datetime.fromtimestamp(int(resets), UTC) if isinstance(resets, (int, float)) else None,
    }


def latest_codex_limits(home: Path) -> dict[str, Any] | None:
    """가장 최근 Codex 세션의 한도. 기록이 없으면 None."""
    for path in _latest_rollouts(home / ".codex" / "sessions"):
        try:
            hit = _last_rate_limits(path)
        except OSError:
            continue
        if not hit:
            continue
        limits, stamp = hit
        observed = None
        if isinstance(stamp, str):
            try:
                observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                observed = None
        return {
            "primary": _window(limits.get("primary")),
            "secondary": _window(limits.get("secondary")),
            "plan_type": str(limits.get("plan_type") or "")[:40] or None,
            "limit_reached": limits.get("rate_limit_reached_type"),
            "observed_at": observed or datetime.fromtimestamp(path.stat().st_mtime, UTC),
        }
    return None


def window_label(minutes: int | None) -> str:
    if minutes == 10080:
        return "주간"
    if minutes == 300:
        return "5시간"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}일"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}시간"
    return f"{minutes}분" if minutes else "기간 미확인"
