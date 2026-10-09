"""먼저 알리기: 알림 설정·전달 규칙·저장소·문구.

전달 규칙(decide_delivery)은 순수 함수다. 순서:
1. 같은 dedupe_key 가 창 안에 이미 보냈거나(보류 포함) 봇이 직접 알린 적 있으면 → SUPPRESSED(duplicate)
2. critical 은 항상 즉시 SENT (조용한 시간·조용히·끄기·상한 무시)
3. /alerts off → QUEUED(off), /quiet 중 → QUEUED(muted), 조용한 시간 → QUEUED(quiet)
4. 지난 1시간 SENT 가 상한 이상 → QUEUED(cap)
5. 그 외 SENT
QUEUED(quiet/off) 는 아침 요약에, QUEUED(muted/cap) 는 조용히가 풀리면 묶음 한 건으로 보낸다.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.redact import redact, redact_text


SEVERITIES = ("critical", "warn", "info")
SEVERITY_ICONS = {"critical": "🚨", "warn": "⚠️", "info": "ℹ️"}
DEDUPE_STATUSES = ("SENT", "QUEUED", "BOT_SENT", "DIGESTED", "BRIEFED")
MAX_QUIET_SECONDS = 12 * 3600
DURATION_RE = re.compile(r"^(?:(\d{1,3})\s*(?:h|hr|hrs|hour|hours|시간))?\s*(?:(\d{1,4})\s*(?:m|min|mins|minute|minutes|분))?$")


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = True
    interval_seconds: int = 60
    timezone: str = "Asia/Seoul"
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"
    dedupe_minutes: int = 60
    hourly_cap: int = 8
    brief_enabled: bool = True
    brief_time: str = "08:30"
    brief_window_hours: int = 4
    disk_free_percent: float = 10.0
    disk_free_gb: float = 20.0
    memory_available_percent: float = 10.0
    memory_consecutive: int = 3
    containers: tuple[str, ...] = ("portainer", "deskmini")
    tmux_session: str = "rc"

    @classmethod
    def load(cls, path: Path) -> "AlertConfig":
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            raw = {}
        merged: dict[str, Any] = {}
        for section in ("watch", "alerts", "brief"):
            value = raw.get(section)
            if isinstance(value, dict):
                merged.update(value)
        default = cls()
        values: dict[str, Any] = {}
        for name, current in default.__dict__.items():
            if name not in merged:
                continue
            value = merged[name]
            try:
                if isinstance(current, bool):
                    values[name] = bool(value)
                elif isinstance(current, int):
                    values[name] = int(value)
                elif isinstance(current, float):
                    values[name] = float(value)
                elif isinstance(current, tuple):
                    values[name] = tuple(str(item) for item in value)
                else:
                    values[name] = str(value)
            except (TypeError, ValueError):
                continue
        config = cls(**{**default.__dict__, **values})
        for clock in (config.quiet_start, config.quiet_end, config.brief_time):
            parse_clock(clock)  # 잘못된 값이면 시작 시 바로 드러나게 한다
        ZoneInfo(config.timezone)
        return config

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class AlertEvent:
    key: str
    severity: str
    kind: str  # job | task | docker | disk | memory | tmux
    title: str
    body: str = ""
    ref: dict[str, Any] = field(default_factory=dict)
    dedupe_seconds: int | None = None  # None 이면 설정의 dedupe_minutes


@dataclass(frozen=True)
class AlertPrefs:
    enabled: bool = True
    muted_until: datetime | None = None

    def muted(self, now: datetime) -> bool:
        return self.muted_until is not None and now < self.muted_until


def parse_clock(value: str) -> dtime:
    hour, minute = value.strip().split(":", 1)
    return dtime(int(hour), int(minute))


def in_quiet_hours(now: datetime, config: AlertConfig) -> bool:
    local = now.astimezone(config.tz).time()
    start, end = parse_clock(config.quiet_start), parse_clock(config.quiet_end)
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def parse_duration(text: str) -> int | None:
    """'2h', '30m', '1h30m', '90분', '2시간' → 초. 0·형식 오류는 None. 상한은 호출자가 본다."""
    normalized = (text or "").strip().lower().replace(" ", "")
    if not normalized:
        return None
    if normalized.isdigit():  # 숫자만 오면 분
        seconds = int(normalized) * 60
        return seconds or None
    match = DURATION_RE.fullmatch(normalized)
    if not match or not (match.group(1) or match.group(2)):
        return None
    seconds = int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60
    return seconds or None


def format_duration(seconds: int) -> str:
    hours, minutes = divmod(max(0, int(seconds)) // 60, 60)
    if hours and minutes:
        return f"{hours}시간 {minutes}분"
    if hours:
        return f"{hours}시간"
    return f"{minutes}분"


def decide_delivery(
    event: AlertEvent,
    *,
    now: datetime,
    config: AlertConfig,
    prefs: AlertPrefs,
    last_same_key_at: datetime | None,
    sent_last_hour: int,
) -> tuple[str, str]:
    window = event.dedupe_seconds if event.dedupe_seconds is not None else config.dedupe_minutes * 60
    if last_same_key_at is not None and now - last_same_key_at < timedelta(seconds=window):
        return "SUPPRESSED", "duplicate"
    if event.severity == "critical":
        return "SENT", "critical"
    if not prefs.enabled:
        return "QUEUED", "off"
    if prefs.muted(now):
        return "QUEUED", "muted"
    if in_quiet_hours(now, config):
        return "QUEUED", "quiet"
    if sent_last_hour >= config.hourly_cap:
        return "QUEUED", "cap"
    return "SENT", "normal"


def brief_due(now: datetime, config: AlertConfig, last_date: str | None) -> str | None:
    """아침 요약을 보낼 때면 현지 날짜 문자열, 아니면 None. 하루 한 번, brief_time 부터 window 시간 안에서만."""
    if not config.brief_enabled:
        return None
    local = now.astimezone(config.tz)
    start = datetime.combine(local.date(), parse_clock(config.brief_time), tzinfo=config.tz)
    if not (start <= local < start + timedelta(hours=config.brief_window_hours)):
        return None
    today = local.date().isoformat()
    return None if last_date == today else today


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class AlertStore:
    """watch_state·alerts·jobs·deliverables 테이블 접근. StateStore 의 연결을 그대로 쓴다."""

    def __init__(self, store: Any):
        self.store = store

    # ---- key-value 상태
    def get_state(self, name: str) -> Any:
        with self.store.connect() as connection:
            row = connection.execute("SELECT value_json FROM watch_state WHERE name=?", (name,)).fetchone()
        return json.loads(row["value_json"]) if row else None

    def set_state(self, name: str, value: Any, now: datetime | None = None) -> None:
        stamp = _iso(now or datetime.now(UTC))
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO watch_state(name, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (name, json.dumps(value, ensure_ascii=False, default=str), stamp),
            )

    def prefs(self, default_enabled: bool = True) -> AlertPrefs:
        raw = self.get_state("alert_prefs") or {}
        return AlertPrefs(
            enabled=bool(raw.get("enabled", default_enabled)),
            muted_until=_parse_time(raw.get("muted_until")),
        )

    def set_prefs(self, prefs: AlertPrefs, now: datetime | None = None) -> None:
        self.set_state(
            "alert_prefs",
            {
                "enabled": prefs.enabled,
                "muted_until": _iso(prefs.muted_until) if prefs.muted_until else None,
            },
            now,
        )

    # ---- 알림 기록
    def last_key_time(self, key: str) -> datetime | None:
        placeholders = ",".join("?" for _ in DEDUPE_STATUSES)
        with self.store.connect() as connection:
            row = connection.execute(
                f"SELECT created_at FROM alerts WHERE dedupe_key=? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (key, *DEDUPE_STATUSES),
            ).fetchone()
        return _parse_time(row["created_at"]) if row else None

    def sent_since(self, since: datetime) -> int:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM alerts WHERE status='SENT' AND sent_at >= ?",
                (_iso(since),),
            ).fetchone()
        return int(row["count"])

    def record(
        self, event: AlertEvent, *, status: str, reason: str, now: datetime
    ) -> int:
        with self.store.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO alerts(dedupe_key, severity, kind, title, body, ref_json, status, reason, created_at, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.key,
                    event.severity,
                    event.kind,
                    redact_text(event.title, 300),
                    redact_text(event.body, 1_500),
                    json.dumps(redact(event.ref), ensure_ascii=False),
                    status,
                    reason,
                    _iso(now),
                    _iso(now) if status == "SENT" else None,
                ),
            )
            return int(cursor.lastrowid)

    def record_bot_notice(self, key: str, now: datetime | None = None) -> None:
        """봇이 다른 경로(검증 결과 카드 등)로 이미 알린 것. 감시기가 같은 내용을 다시 보내지 않게 한다."""
        self.record(
            AlertEvent(key=key, severity="info", kind="task", title="bot notice"),
            status="BOT_SENT",
            reason="bot",
            now=now or datetime.now(UTC),
        )

    def get_alert(self, alert_id: int) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["ref"] = json.loads(item.pop("ref_json") or "{}")
        return item

    def list_alerts(
        self,
        *,
        statuses: tuple[str, ...] = ("SENT", "QUEUED", "DIGESTED", "BRIEFED"),
        since: datetime | None = None,
        reasons: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = [f"status IN ({','.join('?' for _ in statuses)})"]
        params: list[Any] = list(statuses)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(_iso(since))
        if reasons is not None:
            clauses.append(f"reason IN ({','.join('?' for _ in reasons)})")
            params.extend(reasons)
        params.append(limit)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM alerts WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["ref"] = json.loads(item.pop("ref_json") or "{}")
            result.append(item)
        return result

    def mark(self, alert_ids: list[int], status: str) -> None:
        if not alert_ids:
            return
        with self.store.connect() as connection:
            connection.execute(
                f"UPDATE alerts SET status=? WHERE id IN ({','.join('?' for _ in alert_ids)})",
                (status, *alert_ids),
            )

    # ---- 작업·결과물 행 (버튼은 경로 대신 이 행 번호만 싣는다)
    def upsert_job(self, key: str, path: str, label: str, status: str, now: datetime) -> int:
        stamp = _iso(now)
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(job_key, path, label, status, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_key) DO UPDATE SET
                    path=excluded.path, label=excluded.label,
                    updated_at=CASE WHEN jobs.status IS excluded.status THEN jobs.updated_at ELSE excluded.updated_at END,
                    status=excluded.status
                """,
                (key, path, label, status, stamp, stamp),
            )
            row = connection.execute("SELECT id FROM jobs WHERE job_key=?", (key,)).fetchone()
        return int(row["id"])

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def job_by_key(self, key: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def upsert_deliverable(self, job_id: int | None, path: str, size: int, now: datetime) -> int:
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO deliverables(job_id, path, size, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET size=excluded.size, job_id=excluded.job_id
                """,
                (job_id, path, size, _iso(now)),
            )
            row = connection.execute("SELECT id FROM deliverables WHERE path=?", (path,)).fetchone()
        return int(row["id"])

    def get_deliverable(self, deliverable_id: int) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM deliverables WHERE id=?", (deliverable_id,)).fetchone()
        return dict(row) if row else None


def keyboard_rows(
    alert_id: int, *, job_id: int | None, has_deliverables: bool
) -> list[list[tuple[str, str]]]:
    """알림 버튼. callback_data 는 짧은 행 번호만 싣는다(경로 금지, 64바이트 미만)."""
    first = [("자세히", f"al:{alert_id}")]
    if job_id is not None and has_deliverables:
        first.append(("결과물 받기", f"jf:{job_id}"))
    rows = [first]
    second = []
    if job_id is not None:
        second.append(("로그 보기", f"jl:{job_id}"))
    second.append(("1시간 조용히", "q:60"))
    rows.append(second)
    return rows


def alert_text(event: AlertEvent, now: datetime, config: AlertConfig) -> str:
    """짧은 한국어 알림. HTML 이스케이프는 호출자가 아닌 여기서 한다."""
    import html

    local = now.astimezone(config.tz).strftime("%H:%M")
    lines = [f"{SEVERITY_ICONS.get(event.severity, 'ℹ️')} <b>{html.escape(event.title)}</b>"]
    if event.body:
        lines.append(html.escape(redact_text(event.body, 600)))
    lines.append(f"<i>{local} 관측</i>")
    return "\n".join(lines)
