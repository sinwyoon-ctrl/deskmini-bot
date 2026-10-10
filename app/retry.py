"""재시도 보조: 사용량 한도 메시지에서 풀리는 시각을 읽는다.

예) "You've hit your weekly limit · resets 3am (UTC)"
    "You've hit your limit · resets 3pm (Asia/Seoul)"
    "usage limit · resets Oct 10, 10:30pm (Asia/Seoul)"
    "rate limit · resets in 2 hours" / "resets in 45 minutes"
시간대가 없으면 UTC 로 본다. 알 수 없으면 None.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}
RESET_AT_RE = re.compile(
    r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"\s*(?:\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_]+)*)\))?",
    re.IGNORECASE,
)
RESET_IN_RE = re.compile(
    r"resets?\s+in\s+(?:(?P<hours>\d{1,3})\s*(?:h|hours?))?\s*(?:(?P<minutes>\d{1,3})\s*(?:m|min|minutes?))?",
    re.IGNORECASE,
)
# 풀린 직후는 아직 막혀 있는 경우가 있어 조금 늦춰 예약한다.
RETRY_MARGIN = timedelta(minutes=5)
# 풀리는 시각까지 이만큼도 안 남았으면 예약 대신 바로 [다시 시도]를 제안한다.
IMMEDIATE_WINDOW = timedelta(minutes=1)


def parse_reset_time(text: str, now: datetime) -> datetime | None:
    """한도가 풀리는 시각(UTC aware). now 이후의 가장 가까운 시각으로 맞춘다.

    now 에는 한도 메시지를 받은 시각(실행 종료 시각)을 넘겨야 한다. 지금 시각을 넘기면 이미 지난 "resets 9am" 이
    다음 날 9시로 읽힌다. 날짜 경계는 메시지의 시간대(예: Asia/Seoul) 기준으로 넘긴다."""
    if not text:
        return None
    relative = RESET_IN_RE.search(text)
    if relative and (relative.group("hours") or relative.group("minutes")):
        delta = timedelta(hours=int(relative.group("hours") or 0), minutes=int(relative.group("minutes") or 0))
        return (now + delta).astimezone(UTC)
    match = RESET_AT_RE.search(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower()
    if not ampm and not match.group("minute"):
        return None  # "resets 3" 처럼 시각이 아닌 숫자는 버린다
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    tz_name = match.group("tz") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    local_now = now.astimezone(tz)
    if match.group("mon") and match.group("day"):
        month = MONTHS.get(match.group("mon").lower()[:3])
        if not month:
            return None
        year = local_now.year
        try:
            candidate = local_now.replace(month=month, day=int(match.group("day")), hour=hour, minute=minute,
                                          second=0, microsecond=0)
        except ValueError:
            return None
        if candidate < local_now - timedelta(days=1):
            candidate = candidate.replace(year=year + 1)
        return candidate.astimezone(UTC)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def scheduled_time(reset_at: datetime) -> datetime:
    return reset_at + RETRY_MARGIN


def reset_pending(reset_at: datetime, now: datetime) -> bool:
    """아직 기다려야 하면 True(예약 제안). 이미 지났거나 1분 안이면 False(바로 다시 시도)."""
    return reset_at > now + IMMEDIATE_WINDOW
