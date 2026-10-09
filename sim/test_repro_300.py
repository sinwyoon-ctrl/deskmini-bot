"""300건 시뮬레이션에서 찾은 운영 코드 결함의 최소 재현(현재 실패하는 테스트). 운영 코드는 고치지 않았다.

  cd ~/deskmini-bot && .venv/bin/python -m unittest sim/test_repro_300.py -v
네트워크 없음(FakeTelegram), 임시 폴더만 쓴다.
"""
from __future__ import annotations

import html
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import bot as bot_module  # noqa: E402
from app.jobs import clean_log_lines  # noqa: E402
from app.redact import redact_text  # noqa: E402
from test_watch import BotFixture  # noqa: E402

TOKEN = "123456789:AAHfakeTGtokenFAKEfake0123456789xy"
TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a", "code", "pre", "blockquote"}


class PureRepro(unittest.TestCase):
    def test_bug1_double_redaction_emits_unsupported_html_tag(self):
        """bot._send/_notify 가 HTML 이스케이프된 본문을 다시 redact → '<redacted>' 원시 태그 → Telegram 400."""
        once = redact_text(f"TELEGRAM_BOT_TOKEN={TOKEN}")  # 'TELEGRAM_BOT_TOKEN=<redacted>'
        sent = redact_text(f"<pre>{html.escape(once)}</pre>")  # bot.py:1298 와 같은 순서
        self.assertNotIn("<redacted>", sent, sent)

    def test_bug2_log_line_truncated_before_redaction_leaks_token_prefix(self):
        """jobs.clean_log_lines 가 200자로 자른 뒤 마스킹 → 줄 끝 토큰 앞부분(봇 ID + 19자) 노출."""
        line = "x" * 170 + " " + TOKEN
        out = clean_log_lines(line + "\n")[0]
        self.assertNotIn("AAHfakeTG", out, out[-40:])


class BotRepro(BotFixture):
    def setUp(self):
        super().setUp()
        (self.job_dir / "run.log").write_text(f"start\nTELEGRAM_BOT_TOKEN={TOKEN}\nend\n")

    def test_bug1b_logs_message_has_raw_redacted_tag(self):
        self.bot._handle_update(self.message("/logs full-v1"))
        text = self.tg.sent[-2]["text"]
        import re
        bad = {t for t in re.findall(r"<\s*/?\s*([A-Za-z][\w-]*)", text) if t not in TG_TAGS}
        self.assertEqual(bad, set(), text)

    def test_bug3_dangling_postprocess_symlink_breaks_logs(self):
        (self.job_dir / "postprocess-old.log").symlink_to(Path(self.temp.name) / "missing.log")
        self.bot._handle_update(self.message("/logs full-v1"))
        self.assertFalse(any("요청 처리 실패" in (m["text"] or "") for m in self.tg.sent), self.tg.sent[-1]["text"])

    def test_bug5_swap_after_check_before_read(self):
        """_deliver_file: check_deliverable 직후 path.read_bytes() 가 심볼릭 링크를 다시 따라간다(같은 크기면 통과)."""
        self.bot._handle_update(self.message("/files full-v1"))
        data = dict(self.buttons())
        video = next(v for k, v in data.items() if "hero.mp4" in k)
        target = self.job_dir / "web" / "hero.mp4"
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"LOOT-7B")  # hero.mp4 와 같은 7바이트
        original = bot_module.check_deliverable

        def swap(raw, roots, *a, **k):
            result = original(raw, roots, *a, **k)
            target.unlink()
            target.symlink_to(outside)
            return result
        bot_module.check_deliverable = swap
        try:
            self.press(video)
        finally:
            bot_module.check_deliverable = original
        self.assertFalse(any(f["content"] == b"LOOT-7B" for f in self.tg.files))

    def test_bug6_hardlink_to_outside_file_is_offered(self):
        outside = Path(self.temp.name) / "secret-copy.mp4"
        outside.write_bytes(b"OUTSIDE")
        os.link(outside, self.job_dir / "web" / "leak.mp4")
        self.bot._handle_update(self.message("/files full-v1"))
        self.assertNotIn("leak.mp4", " ".join(label for label, _ in self.buttons()))

    def test_bug7_secret_shaped_filename_in_button_and_caption(self):
        name = "sk-ant-api03-FAKEantKEYfakeKEY0123456789.mp4"
        (self.job_dir / "web" / name).write_bytes(b"MP4")
        self.bot._handle_update(self.message("/files full-v1"))
        labels = " ".join(label for label, _ in self.buttons())
        self.assertNotIn("FAKEantKEY", labels)


class ChatUnmuteRepro(BotFixture):
    def test_bug4_unmute_via_chat_is_impossible(self):
        from test_watch import FakeResponder
        self.bot.alerts.set_prefs(__import__("app.alerts", fromlist=["AlertPrefs"]).AlertPrefs(
            enabled=True, muted_until=self.now + timedelta(hours=2)), self.now)
        # 프롬프트가 허용하는 값 중 '켜기'에 해당하는 on 을 모델이 고른 경우(A07 실제 claude 응답과 같음)
        self.bot.chat = FakeResponder({"type": "set_alerts", "alerts": "on"})
        self.bot._handle_update(self.message("조용히 모드 풀어줘"))
        self.assertFalse(self.bot.alerts.prefs().muted(self.now), "on 을 보냈는데 조용히가 그대로(봇은 '켰습니다'라고 답함)")


if __name__ == "__main__":
    unittest.main()


class NewAfterFixRepro(BotFixture):
    """재검증(수정 후)에서 새로 찾은 것. 서버 TZ=UTC(서비스 파일에 TZ 없음)에서 대화 스냅샷 시각이 UTC 로 나간다."""

    def test_bug11_snapshot_times_are_server_local_not_kst(self):
        import os
        import time as _time
        from app.alerts import AlertEvent
        from app.context import recent_alerts
        old = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        _time.tzset()
        try:
            at = datetime(2026, 10, 9, 22, 19, tzinfo=self.bot.alert_config.tz)  # 알림 문구는 'KST 22:19 관측'
            self.bot.alerts.record(AlertEvent(key="k", severity="warn", kind="job", title="t"), status="SENT",
                                   reason="normal", now=at)
            items = recent_alerts(self.bot.store)
            self.assertIn("22:19", json_dump(items), "스냅샷은 13:19(UTC)로 줘서 모델이 KST 알림과 다른 시각을 말함")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            _time.tzset()


def json_dump(value):
    import json
    return json.dumps(value, ensure_ascii=False)
