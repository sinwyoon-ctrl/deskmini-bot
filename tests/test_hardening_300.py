"""300건 검증에서 찾은 운영 결함 수정의 회귀 테스트(마스킹 순서·outbox 영구 실패·로그 선택·알림 설정·결과물 경로)."""
import html
import os
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from adapters.telegram import TelegramError
from app.alerts import AlertEvent, AlertPrefs, AlertStore
from app.bot import esc
from app.chat import FALLBACK_TEXT, SYSTEM_PROMPT, ChatAction, ChatResult
from app.context import recent_alerts
from app.files import check_deliverable, open_checked, read_redacted_text, secret_like_name
from app.jobs import clean_log_lines, match_job
from app.redact import redact_html, redact_text
from app.state import OUTBOX_MAX_ATTEMPTS, StateStore

from test_watch import BotFixture, FakeTelegram, job, kst

TOKEN = "123456789:AAHfakeTGtokenFAKEfake0123456789xy"
TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a", "code", "pre",
           "blockquote"}
SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"


def bad_tags(text):
    return {tag for tag in re.findall(r"<\s*/?\s*([A-Za-z][\w-]*)", text or "") if tag not in TG_TAGS}


class RedactionOrderTests(unittest.TestCase):
    def test_redact_text_does_not_rematch_marker_plain_or_escaped(self):
        once = redact_text(f"TELEGRAM_BOT_TOKEN={TOKEN}")
        self.assertEqual(redact_text(once), once)
        escaped = html.escape(once)
        self.assertEqual(redact_text(escaped), escaped)
        url = html.escape(redact_text("https://user:pw123@example.com"))
        self.assertEqual(redact_text(url), url)

    def test_redact_html_inserts_escaped_marker_and_keeps_tags(self):
        raw = f"<pre>{html.escape('token=' + TOKEN + ' next')}</pre>"
        out = redact_html(raw)
        self.assertEqual(bad_tags(out), set())
        self.assertNotIn("AAHfake", out)
        self.assertIn("&lt;redacted&gt;", out)
        self.assertTrue(out.startswith("<pre>") and out.endswith("</pre>"))

    def test_esc_redacts_before_escaping(self):
        out = esc('password="hunter22 secret"')
        self.assertNotIn("hunter22", out)
        self.assertEqual(bad_tags(f"<pre>{out}</pre>"), set())

    def test_log_line_redacted_before_truncation(self):
        out = clean_log_lines("x" * 170 + " " + TOKEN + "\n")[0]
        self.assertNotIn("AAHfake", out)
        self.assertLessEqual(len(out), 200)

    def test_text_file_redacted_before_byte_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.txt"
            path.write_text("y" * 90 + TOKEN)
            text, truncated = read_redacted_text(path, limit_bytes=100)
        self.assertTrue(truncated)
        self.assertNotIn("AAHfake", text)


class OutboxFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "chief.sqlite", SCHEMA)

    def tearDown(self):
        self.temp.cleanup()

    def queue(self, key="k", text="hi"):
        self.store.queue_outbox(dedupe_key=key, chat_id=1, method="sendMessage",
                                payload={"chat_id": 1, "text": text, "parse_mode": "HTML"})
        return self.store.pending_outbox()[-1]

    def state(self, item_id):
        with self.store.connect() as connection:
            return connection.execute("SELECT state FROM outbox WHERE id=?", (item_id,)).fetchone()["state"]

    def test_permanent_400_marks_failed(self):
        item = self.queue()
        self.assertEqual(self.store.update_outbox(item["id"], success=False, permanent=True), "FAILED")
        self.assertEqual(self.state(item["id"]), "FAILED")
        self.assertEqual(self.store.pending_outbox(), [])

    def test_transient_failures_stop_after_max_attempts(self):
        item = self.queue()
        results = [self.store.update_outbox(item["id"], success=False, retry_after_seconds=0)
                   for _ in range(OUTBOX_MAX_ATTEMPTS)]
        self.assertEqual(results[:-1], ["PENDING"] * (OUTBOX_MAX_ATTEMPTS - 1))
        self.assertEqual(results[-1], "FAILED")

    def test_queued_html_payload_has_no_raw_marker(self):
        item = self.queue(text=f"<pre>TOKEN={TOKEN}</pre>")
        self.assertEqual(bad_tags(item["payload"]["text"]), set())
        self.assertNotIn("AAHfake", item["payload"]["text"])

    def test_telegram_error_permanent_flag(self):
        self.assertTrue(TelegramError("Bad Request: can't parse entities", error_code=400).permanent)
        self.assertFalse(TelegramError("Too Many Requests", retry_after=3, error_code=429).permanent)
        self.assertFalse(TelegramError("network").permanent)


class RejectingTelegram(FakeTelegram):
    def send_message(self, chat_id, text, reply_markup=None):
        raise TelegramError("Bad Request: can't parse entities", error_code=400)

    def call_with_retry(self, method, payload, attempts=1):
        raise TelegramError("Bad Request: can't parse entities", error_code=400)


class BotOutputTests(BotFixture):
    def test_logs_with_secret_have_only_telegram_tags(self):
        (self.job_dir / "run.log").write_text(f"start\nTELEGRAM_BOT_TOKEN={TOKEN}\nend\n")
        self.bot._handle_update(self.message("/logs full-v1"))
        for message in self.tg.sent:
            self.assertEqual(bad_tags(message["text"]), set(), message["text"])
            self.assertNotIn("AAHfake", message["text"])

    def test_rejected_notification_is_not_retried_forever(self):
        self.bot.telegram = RejectingTelegram()
        self.bot._notify("n1", "hello")
        self.assertEqual(self.bot.store.pending_outbox(), [])

    def test_dangling_and_outside_log_symlinks_are_skipped(self):
        (self.job_dir / "postprocess-old.log").symlink_to(Path(self.temp.name) / "missing.log")
        outside = Path(self.temp.name) / "outside.log"
        outside.write_text("OUTSIDE-SECRET\n")
        (self.job_dir / "postprocess-new.log").symlink_to(outside)
        self.bot._handle_update(self.message("/logs full-v1"))
        texts = " ".join(m["text"] for m in self.tg.sent)
        self.assertNotIn("요청 처리 실패", texts)
        self.assertNotIn("OUTSIDE-SECRET", texts)
        self.assertIn("run.log", texts)

    def test_help_mentions_brief_and_critical_alerts(self):
        self.bot._handle_update(self.message("/help"))
        self.assertIn("/brief", self.tg.sent[-1]["text"])
        self.assertIn("치명적인 알림", self.tg.sent[-1]["text"])


class ReplyResponder:
    def __init__(self, action, reply):
        self.action, self.reply = action, reply

    def respond(self, text, history, snapshot):
        return ChatResult(reply=self.reply, action=ChatAction.from_dict(self.action), provider="claude", model="m",
                          latency_ms=1)

    def active_providers(self):
        return []


class AlertSettingTests(BotFixture):
    def mute(self):
        self.bot.alerts.set_prefs(AlertPrefs(enabled=True, muted_until=self.now + timedelta(hours=2)), self.now)

    update_id = 0

    def chat(self, value, reply="바꿀게요"):
        self.update_id += 1
        self.bot.chat = ReplyResponder({"type": "set_alerts", "alerts": value}, reply)
        self.bot._handle_update(self.message("알림 설정 바꿔줘", update_id=self.update_id))

    def test_chat_on_enables_and_clears_quiet_and_reports_db_state(self):
        self.mute()
        self.bot.alerts.set_prefs(AlertPrefs(enabled=False, muted_until=self.now + timedelta(hours=2)), self.now)
        self.chat("on")
        prefs = self.bot.alerts.prefs()
        self.assertTrue(prefs.enabled)
        self.assertFalse(prefs.muted(self.now))
        self.assertIn("조용히: 꺼짐", self.tg.sent[-1]["text"])

    def test_chat_unquiet_only_clears_quiet(self):
        self.bot.alerts.set_prefs(AlertPrefs(enabled=False, muted_until=self.now + timedelta(hours=2)), self.now)
        self.chat("unquiet")
        prefs = self.bot.alerts.prefs()
        self.assertFalse(prefs.enabled)
        self.assertFalse(prefs.muted(self.now))
        self.assertIn("꺼짐(치명적인 알림만", self.tg.sent[-1]["text"])

    def test_chat_off_keeps_quiet_and_quiet_duration_mutes(self):
        self.chat("quiet 2h")
        self.assertTrue(self.bot.alerts.prefs().muted(self.now))
        self.chat("off")
        prefs = self.bot.alerts.prefs()
        self.assertFalse(prefs.enabled)
        self.assertTrue(prefs.muted(self.now))

    def test_approval_claim_replaced_for_immediate_actions(self):
        self.chat("on", reply="승인 카드를 띄웠어요. 승인하시면 적용됩니다.")
        texts = [m["text"] for m in self.tg.sent]
        self.assertFalse(any("승인" in text for text in texts), texts)
        self.bot.chat = ReplyResponder({"type": "send_files", "job_hint": "full-v1"}, "승인 카드를 띄웠어요")
        self.bot._handle_update(self.message("결과물 보내줘", update_id=99))
        self.assertFalse(any("승인" in m["text"] for m in self.tg.sent))

    def test_prompt_limits_approval_wording_and_documents_alerts(self):
        self.assertIn("create_task·stop_task·merge·push·deploy 만 승인", SYSTEM_PROMPT)
        self.assertIn("unquiet", SYSTEM_PROMPT)
        self.assertIn("/brief", SYSTEM_PROMPT)
        self.assertIn("치명적인 알림", SYSTEM_PROMPT)
        self.assertIn("/brief", FALLBACK_TEXT)


class DeliverableSafetyTests(BotFixture):
    def test_hardlink_is_refused_and_not_listed(self):
        outside = Path(self.temp.name) / "secret-copy.mp4"
        outside.write_bytes(b"OUTSIDE")
        os.link(outside, self.job_dir / "web" / "leak.mp4")
        check = check_deliverable(self.job_dir / "web" / "leak.mp4", self.bot.file_roots)
        self.assertFalse(check.ok)
        self.assertIn("하드링크", check.reason)
        self.bot._handle_update(self.message("/files full-v1"))
        self.assertNotIn("leak.mp4", " ".join(label for label, _ in self.buttons()))

    def test_secret_shaped_names_are_refused(self):
        name = "sk-ant-api03-FAKEantKEYfakeKEY0123456789.mp4"
        self.assertTrue(secret_like_name(name))
        (self.job_dir / "web" / name).write_bytes(b"MP4")
        self.assertFalse(check_deliverable(self.job_dir / "web" / name, self.bot.file_roots).ok)
        self.bot._handle_update(self.message("/files full-v1"))
        self.assertNotIn("FAKEantKEY", " ".join(label for label, _ in self.buttons()))

    def test_open_checked_rejects_swap_to_symlink_or_other_file(self):
        target = self.job_dir / "web" / "hero.mp4"
        check = check_deliverable(target, self.bot.file_roots)
        self.assertTrue(check.ok)
        with open_checked(check, self.bot.file_roots) as handle:
            self.assertEqual(handle.read(), b"MP4DATA")
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"LOOT-7B")
        original_inode = target.stat().st_ino
        # 원래 파일을 열어 둬 inode 가 해제·재사용되지 않게 한다(ext4 는 지운 inode 번호를 바로 다시 쓸 수 있다).
        keep_alive = open(target, "rb")  # noqa: SIM115
        self.addCleanup(keep_alive.close)
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(OSError):
            open_checked(check, self.bot.file_roots)
        target.unlink()
        target.write_bytes(b"OTHER7B")  # 같은 크기의 다른 일반 파일(inode 다름)
        self.assertNotEqual(target.stat().st_ino, original_inode)
        with self.assertRaises(OSError):
            open_checked(check, self.bot.file_roots)

    def test_swap_after_check_is_not_uploaded(self):
        import app.bot as bot_module

        self.bot._handle_update(self.message("/files full-v1"))
        video = next(data for label, data in self.buttons() if "hero.mp4" in label)
        target = self.job_dir / "web" / "hero.mp4"
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"LOOT-7B")
        original = bot_module.check_deliverable

        def swap(raw, roots, *args, **kwargs):
            result = original(raw, roots, *args, **kwargs)
            target.unlink()
            target.symlink_to(outside)
            return result

        bot_module.check_deliverable = swap
        try:
            self.press(video)
        finally:
            bot_module.check_deliverable = original
        self.assertEqual(self.tg.files, [])
        self.assertIn("파일 전송 실패", self.tg.sent[-1]["text"])


class MatchJobTests(unittest.TestCase):
    def test_prefers_job_with_deliverables_for_file_requests(self):
        running = job("work/hero/revisions/r1/render/scroll-no-blur", "running")
        done = job("work/hero/revisions/r1/render/full-1080", "complete", deliverables=True)
        jobs = [running, done]  # 최근 활동 순: 결과물 없는 진행 중 작업이 앞
        self.assertIs(match_job(jobs, "hero"), running)
        self.assertIs(match_job(jobs, "hero", prefer_deliverables=True), done)
        self.assertIs(match_job(jobs, "scroll-no-blur", prefer_deliverables=True), running)  # 정확한 이름이 우선
        self.assertIs(match_job(jobs, "", prefer_deliverables=True), done)
        self.assertIsNone(match_job(jobs, "nothing", prefer_deliverables=True))


class SnapshotAlertsTests(BotFixture):
    def test_recent_alerts_in_snapshot_are_redacted_and_compact(self):
        store = AlertStore(self.bot.store)
        store.record(AlertEvent(key="job:a:failed", severity="warn", kind="job", title="final-cut 후처리 미완",
                                body=f"token={TOKEN} " + "x" * 300), status="SENT", reason="normal", now=kst(9))
        store.record(AlertEvent(key="disk", severity="critical", kind="disk", title="디스크 부족", body=""),
                     status="QUEUED", reason="quiet", now=kst(10))
        items = recent_alerts(self.bot.store)
        self.assertEqual([item["status"] for item in items], ["QUEUED", "SENT"])
        self.assertEqual(items[0]["severity"], "critical")
        self.assertIn("final-cut", items[1]["text"])
        self.assertNotIn("AAHfake", items[1]["text"])
        self.assertLessEqual(len(items[1]["text"]), 125)


if __name__ == "__main__":
    unittest.main()


class DisplayAndNameRulesTest(unittest.TestCase):
    def test_media_deliverables_with_common_words_are_allowed(self):
        from app.files import secret_like_name

        for name in ("key-visual.png", "cookie-banner.png", "private-beta-teaser.mp4"):
            self.assertFalse(secret_like_name(name), name)
        for name in ("private.key", "cookies.txt", "server.pem", "api_key.png", "token.mp4", ".env"):
            self.assertTrue(secret_like_name(name), name)

    def test_snapshot_times_use_kst(self):
        from app import context

        self.assertEqual(str(context.DISPLAY_TZ), "Asia/Seoul")
