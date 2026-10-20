"""텔레그램 폴링 일시 오류 내성: 연속 실패 횟수로 백오프와 로그 등급을 정한다."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

MAX_DELAY = 30.0
BASE_DELAY = 3.0
WARN_FIRST = 3
WARN_EVERY = 10


class PollingHealth:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.failures = 0
        self.first_failure_at: float | None = None

    def failure(self, exc: Any) -> tuple[str, float]:
        if self.failures == 0:
            self.first_failure_at = self.clock()
        self.failures += 1
        n = self.failures
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            delay = float(min(retry_after, MAX_DELAY))
        else:
            delay = float(min(BASE_DELAY * 2 ** (n - 1), MAX_DELAY))
        warn = n >= WARN_FIRST and (n - WARN_FIRST) % WARN_EVERY == 0
        return ("warning" if warn else "info"), delay

    def success(self) -> str | None:
        n = self.failures
        started = self.first_failure_at
        self.failures = 0
        self.first_failure_at = None
        if n < WARN_FIRST or started is None:
            return None
        return f"Telegram polling recovered after {n} failures ({self.clock() - started:.0f} s)"
