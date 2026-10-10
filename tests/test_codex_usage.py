from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.codex_usage import latest_codex_limits, window_label


def write_rollout(home: Path, events: list[str]) -> None:
    folder = home / ".codex" / "sessions" / "2026" / "10" / "10"
    folder.mkdir(parents=True)
    (folder / "rollout-2026-10-10T08-52-03-x.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")


class CodexUsageTest(unittest.TestCase):
    def test_reads_latest_rate_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old = {"timestamp": "2026-10-10T08:00:00Z", "payload": {"rate_limits": {"primary": {"used_percent": 10, "window_minutes": 10080, "resets_at": 1791955612}}}}
            new = {"timestamp": "2026-10-10T09:46:04Z", "payload": {"info": {}, "rate_limits": {"primary": {"used_percent": 38.0, "window_minutes": 10080, "resets_at": 1791955612}, "secondary": None, "plan_type": "prolite"}}}
            write_rollout(home, [json.dumps(old), "{broken json rate_limits", json.dumps(new)])
            limits = latest_codex_limits(home)
            self.assertEqual(limits["primary"]["used_percent"], 38.0)
            self.assertEqual(limits["primary"]["window_minutes"], 10080)
            self.assertIsNone(limits["secondary"])
            self.assertEqual(limits["plan_type"], "prolite")
            self.assertEqual(limits["observed_at"].isoformat(), "2026-10-10T09:46:04+00:00")

    def test_no_sessions_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(latest_codex_limits(Path(tmp)))

    def test_never_reads_auth_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex" / "auth.json").write_text('{"rate_limits": {"primary": {"used_percent": 99}}}', encoding="utf-8")
            self.assertIsNone(latest_codex_limits(home))

    def test_window_labels(self):
        self.assertEqual(window_label(10080), "주간")
        self.assertEqual(window_label(300), "5시간")
        self.assertEqual(window_label(None), "기간 미확인")


if __name__ == "__main__":
    unittest.main()
