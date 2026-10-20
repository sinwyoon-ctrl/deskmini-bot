from __future__ import annotations

import unittest

from adapters.telegram import TelegramError
from app.polling import PollingHealth


class PollingHealthTests(unittest.TestCase):
    def fail(self, health, n, **kw):
        result = None
        for _ in range(n):
            result = health.failure(TelegramError("URLError", **kw))
        return result

    def test_backoff_and_levels(self):
        h = PollingHealth()
        got = [h.failure(TelegramError("x")) for _ in range(5)]
        self.assertEqual(got, [("info", 3), ("info", 6), ("warning", 12), ("info", 24), ("info", 30)])

    def test_warning_every_ten_after_third(self):
        h = PollingHealth()
        levels = [h.failure(TelegramError("x"))[0] for _ in range(23)]
        warn = [i + 1 for i, v in enumerate(levels) if v == "warning"]
        self.assertEqual(warn, [3, 13, 23])
        self.assertEqual(h.failure(TelegramError("x")), ("info", 30))

    def test_retry_after_wins_capped(self):
        h = PollingHealth()
        self.assertEqual(h.failure(TelegramError("x", retry_after=7)), ("info", 7))
        self.assertEqual(h.failure(TelegramError("x", retry_after=99))[1], 30)

    def test_recovery_message_and_reset(self):
        now = [100.0]
        h = PollingHealth(clock=lambda: now[0])
        self.fail(h, 3)
        now[0] = 145.0
        self.assertEqual(h.success(), "Telegram polling recovered after 3 failures (45 s)")
        self.assertEqual(h.failures, 0)
        self.assertIsNone(h.success())
        self.assertEqual(h.failure(TelegramError("x")), ("info", 3))

    def test_no_message_under_three(self):
        h = PollingHealth()
        self.fail(h, 2)
        self.assertIsNone(h.success())
        self.assertEqual(h.failures, 0)


if __name__ == "__main__":
    unittest.main()
