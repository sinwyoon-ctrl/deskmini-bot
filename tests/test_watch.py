import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adapters.telegram import build_multipart, safe_filename
from app.alerts import (
    AlertConfig,
    AlertEvent,
    AlertPrefs,
    brief_due,
    decide_delivery,
    in_quiet_hours,
    keyboard_rows,
    parse_duration,
)
from app.bot import ChiefBot
from app.chat import RESPONSE_SCHEMA, SYSTEM_PROMPT, ChatAction, ChatResult
from app.config import ProjectConfig, Settings
from app.context import WorkspacePaths
from app.files import MAX_UPLOAD_BYTES, check_deliverable, resolve_allowed, secret_like_name
from app.jobs import JobObservation, JobsConfig, clean_log_lines, derive_status, discover_jobs, match_job
from app.policy import Policy
from app.watch import Observation, detect


ROOT = Path(__file__).resolve().parents[1]
CONFIG = AlertConfig()
GB = 1024**3


def kst(hour, minute=0, day=9):
    """2026-10-<day> hour:minute Asia/Seoul → UTC aware datetime."""
    return datetime(2026, 10, day, hour, minute, tzinfo=CONFIG.tz).astimezone(UTC)


def job(key="work/p/render/a", status="running", missing=False, deliverables=False, path="/nonexistent"):
    return JobObservation(
        key=key, path=path, label=key.split("/")[-1], status=status, postprocess_missing=missing,
        markers=("STARTED",), last_activity=0.0, has_deliverables=deliverables,
    )


def obs(at=None, **kwargs):
    base = dict(
        containers={"portainer": "running", "deskmini": "running"},
        disk_free=200 * GB, disk_total=1000 * GB,
        mem_available=8 * GB, mem_total=16 * GB,
        tmux="active", tasks={}, jobs={},
    )
    base.update(kwargs)
    return Observation(at=at or kst(12), **base)


def keys(events):
    return [event.key for event in events]


class DetectionTests(unittest.TestCase):
    def test_first_observation_is_silent_baseline(self):
        bad = obs(
            containers={"portainer": "exited"}, disk_free=1 * GB, tmux="inactive",
            tasks={"T-20261009-aaaaaa": {"state": "FAILED"}}, jobs={"k": job("k", "failed")},
        )
        events, state = detect(bad, None, CONFIG)
        self.assertEqual(events, [])
        self.assertTrue(state["disk_low"])
        self.assertEqual(state["jobs"]["k"]["status"], "failed")

    def test_no_change_no_events(self):
        _, state = detect(obs(), None, CONFIG)
        events, _ = detect(obs(), state, CONFIG)
        self.assertEqual(events, [])

    def test_container_down_and_up(self):
        _, state = detect(obs(), None, CONFIG)
        events, state = detect(obs(containers={"portainer": "running", "deskmini": "exited"}), state, CONFIG)
        self.assertEqual(keys(events), ["docker:deskmini:down"])
        self.assertEqual(events[0].severity, "critical")
        events, state = detect(obs(containers={"portainer": "running"}), state, CONFIG)
        self.assertEqual(events, [])  # exited → missing 은 둘 다 멈춤
        events, _ = detect(obs(), state, CONFIG)
        self.assertEqual(keys(events), ["docker:deskmini:up"])
        self.assertEqual(events[0].severity, "info")

    def test_docker_unknown_keeps_previous_without_events(self):
        _, state = detect(obs(), None, CONFIG)
        events, state = detect(obs(containers=None), state, CONFIG)
        self.assertEqual(events, [])
        self.assertEqual(state["containers"]["deskmini"], "running")
        events, _ = detect(obs(containers={"portainer": "running", "deskmini": "dead"}), state, CONFIG)
        self.assertEqual(keys(events), ["docker:deskmini:down"])

    def test_disk_thresholds(self):
        _, state = detect(obs(), None, CONFIG)
        events, state = detect(obs(disk_free=19 * GB), state, CONFIG)  # 20GB 미만
        self.assertEqual(keys(events), ["disk:low"])
        self.assertEqual(events[0].severity, "critical")
        events, state = detect(obs(disk_free=18 * GB), state, CONFIG)
        self.assertEqual(events, [])
        events, state = detect(obs(disk_free=200 * GB), state, CONFIG)
        self.assertEqual(keys(events), ["disk:ok"])
        events, _ = detect(obs(disk_free=90 * GB, disk_total=1000 * GB), state, CONFIG)  # 9% 미만
        self.assertEqual(keys(events), ["disk:low"])

    def test_memory_needs_three_consecutive_checks(self):
        _, state = detect(obs(), None, CONFIG)
        low = dict(mem_available=1 * GB, mem_total=16 * GB)
        events, state = detect(obs(**low), state, CONFIG)
        self.assertEqual(events, [])
        events, state = detect(obs(**low), state, CONFIG)
        self.assertEqual(events, [])
        events, state = detect(obs(**low), state, CONFIG)
        self.assertEqual(keys(events), ["memory:low"])
        events, state = detect(obs(**low), state, CONFIG)
        self.assertEqual(events, [])
        events, state = detect(obs(), state, CONFIG)
        self.assertEqual(keys(events), ["memory:ok"])

    def test_memory_streak_resets(self):
        _, state = detect(obs(), None, CONFIG)
        low = dict(mem_available=1 * GB, mem_total=16 * GB)
        for sample in (low, low, {}, low, low):
            events, state = detect(obs(**sample), state, CONFIG)
            self.assertEqual(events, [])

    def test_tmux_disappearance(self):
        _, state = detect(obs(), None, CONFIG)
        events, state = detect(obs(tmux="unknown"), state, CONFIG)
        self.assertEqual(events, [])
        events, state = detect(obs(tmux="inactive"), state, CONFIG)
        self.assertEqual(keys(events), ["tmux:gone"])
        self.assertEqual(events[0].severity, "warn")
        events, _ = detect(obs(tmux="active"), state, CONFIG)
        self.assertEqual(keys(events), ["tmux:back"])

    def test_task_transitions(self):
        task = "T-20261009-abcdef"
        _, state = detect(obs(tasks={task: {"state": "RUNNING"}}), None, CONFIG)
        events, state = detect(obs(tasks={task: {"state": "VERIFYING"}}), state, CONFIG)
        self.assertEqual(events, [])
        events, state = detect(obs(tasks={task: {"state": "FAILED"}}), state, CONFIG)
        self.assertEqual(keys(events), [f"task:{task}:FAILED"])
        self.assertEqual(events[0].ref, {"task_id": task})

    def test_limit_blocked_title(self):
        task = "T-20261009-abcdef"
        _, state = detect(obs(tasks={task: {"state": "RUNNING"}}), None, CONFIG)
        events, _ = detect(
            obs(tasks={task: {"state": "BLOCKED", "classification": "LIMIT_BLOCKED"}}), state, CONFIG
        )
        self.assertIn("한도", events[0].title)

    def test_busy_task_is_deferred(self):
        task = "T-20261009-abcdef"
        _, state = detect(obs(tasks={task: {"state": "VERIFYING"}}), None, CONFIG)
        events, state = detect(obs(tasks={task: {"state": "READY"}}), state, CONFIG, busy_tasks={task})
        self.assertEqual(events, [])
        self.assertEqual(state["tasks"][task], "VERIFYING")
        events, _ = detect(obs(tasks={task: {"state": "READY"}}), state, CONFIG)
        self.assertEqual(keys(events), [f"task:{task}:READY"])

    def test_job_transitions(self):
        _, state = detect(obs(jobs={"k": job("k", "running")}), None, CONFIG)
        events, state = detect(obs(jobs={"k": job("k", "stalled")}), state, CONFIG)
        self.assertEqual(keys(events), ["job:k:stalled"])
        events, state = detect(obs(jobs={"k": job("k", "complete")}), state, CONFIG)
        self.assertEqual(keys(events), ["job:k:complete"])
        events, state = detect(obs(jobs={"k": job("k", "complete", missing=True)}), state, CONFIG)
        self.assertEqual(keys(events), ["job:k:postprocess_missing"])
        events, state = detect(obs(jobs={"k": job("k", "postprocess_done")}), state, CONFIG)
        self.assertEqual(keys(events), ["job:k:postprocess_done"])
        events, _ = detect(obs(jobs={"k": job("k", "failed")}), state, CONFIG)
        self.assertEqual(keys(events), ["job:k:failed"])
        self.assertEqual(events[0].severity, "warn")

    def test_new_job_after_baseline_is_reported(self):
        _, state = detect(obs(), None, CONFIG)
        events, _ = detect(obs(jobs={"n": job("n", "running")}), state, CONFIG)
        self.assertEqual(keys(events), ["job:n:running"])
        self.assertEqual(events[0].title, "작업 시작")


class JobStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.now = time.time()

    def tearDown(self):
        self.temp.cleanup()

    def make(self, rel, files, age_minutes=0):
        path = self.home / rel
        path.mkdir(parents=True)
        stamp = self.now - age_minutes * 60
        for name in files:
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
            os.utime(target, (stamp, stamp))
        return path

    def test_derived_statuses_match_real_layouts(self):
        self.make("work/p/render/failed-v1", ["STARTED", "FAILED", "run.log"], 60)
        self.make("work/p/render/done-v2", ["STARTED", "COMPLETE", "RENDER_VALIDATED", "POSTPROCESS_COMPLETE",
                                            "postprocess.log", "web-scroll/a.mp4"], 60)
        self.make("work/p/revisions/r1/render/full-v1", ["STARTED", "COMPLETE", "run.log", "postprocess.log",
                                                         "web/a.mp4", "web/b.jpg"], 60)
        self.make("work/p/render/live", ["STARTED", "run.log"], 1)
        self.make("work/p/render/stuck", ["STARTED", "run.log"], 120)
        self.make("work/p/render/no-marker", ["run.log"], 1)
        jobs = {Path(item.key).name: item for item in discover_jobs(JobsConfig(), self.home, self.now)}
        self.assertNotIn("no-marker", jobs)
        self.assertEqual(jobs["failed-v1"].status, "failed")
        self.assertEqual(jobs["done-v2"].status, "postprocess_done")
        self.assertTrue(jobs["done-v2"].has_deliverables)
        full = jobs["full-v1"]
        self.assertEqual((full.status, full.postprocess_missing, full.has_deliverables), ("complete", True, True))
        self.assertEqual(full.label, "p · r1 · full-v1")
        self.assertEqual(jobs["live"].status, "running")
        self.assertEqual(jobs["stuck"].status, "stalled")
        self.assertFalse(jobs["live"].has_deliverables)

    def test_postprocess_not_missing_while_recent_or_absent(self):
        now = 10_000.0
        self.assertEqual(derive_status({"STARTED": 1, "COMPLETE": 2}, None, now, 1800), ("complete", False))
        self.assertEqual(
            derive_status({"STARTED": 1, "COMPLETE": 2}, None, now, 1800, postprocess_log_activity=now - 60),
            ("complete", False),
        )

    def test_rerun_after_failure_is_running(self):
        now = 10_000.0
        status, _ = derive_status({"FAILED": 100, "STARTED": 9_990}, 9_995, now, 1800)
        self.assertEqual(status, "running")

    def test_match_job(self):
        jobs = [job("work/p/revisions/r1/render/full-1080-v1"), job("work/p/render/scroll-v2")]
        self.assertEqual(match_job(jobs, "scroll-v2").key, "work/p/render/scroll-v2")
        self.assertEqual(match_job(jobs, "full 1080").key, "work/p/revisions/r1/render/full-1080-v1")
        self.assertEqual(match_job(jobs, "").key, jobs[0].key)
        self.assertIsNone(match_job(jobs, "없는작업"))

    def test_log_cleaning_and_redaction(self):
        raw = (
            "\x1b[32mstart\x1b[0m\n"
            " 45%|█████     | 45/100\n"
            "frame=  1 fps=0\rframe=  2 fps=1\n"
            "frame=  3 fps=1\n"
            "token=abcd1234secret\n"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
            "done\n"
        )
        lines = clean_log_lines(raw, 30)
        self.assertEqual(lines[0], "start")
        self.assertFalse(any("█" in line for line in lines))
        self.assertEqual([line for line in lines if line.startswith("frame=")], ["frame=  3 fps=1"])
        joined = "\n".join(lines)
        self.assertNotIn("abcd1234secret", joined)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", joined)
        self.assertEqual(lines[-1], "done")
        self.assertEqual(len(clean_log_lines("\n".join(f"line {i}" for i in range(100)), 30)), 30)


class AlertPolicyTests(unittest.TestCase):
    def decide(self, severity="warn", now=None, prefs=AlertPrefs(), last=None, sent=0, key="k"):
        event = AlertEvent(key=key, severity=severity, kind="job", title="t")
        return decide_delivery(event, now=now or kst(12), config=CONFIG, prefs=prefs,
                               last_same_key_at=last, sent_last_hour=sent)

    def test_quiet_hours_boundaries(self):
        self.assertTrue(in_quiet_hours(kst(23, 0), CONFIG))
        self.assertTrue(in_quiet_hours(kst(3), CONFIG))
        self.assertTrue(in_quiet_hours(kst(6, 59), CONFIG))
        self.assertFalse(in_quiet_hours(kst(7, 0), CONFIG))
        self.assertFalse(in_quiet_hours(kst(22, 59), CONFIG))

    def test_quiet_hours_queue_non_critical_and_bypass_critical(self):
        self.assertEqual(self.decide("warn", kst(23, 30)), ("QUEUED", "quiet"))
        self.assertEqual(self.decide("info", kst(2)), ("QUEUED", "quiet"))
        self.assertEqual(self.decide("critical", kst(2)), ("SENT", "critical"))
        self.assertEqual(self.decide("warn", kst(12)), ("SENT", "normal"))

    def test_dedupe_window(self):
        now = kst(12)
        self.assertEqual(self.decide(last=now - timedelta(minutes=10)), ("SUPPRESSED", "duplicate"))
        self.assertEqual(self.decide("critical", last=now - timedelta(minutes=10)), ("SUPPRESSED", "duplicate"))
        self.assertEqual(self.decide(last=now - timedelta(minutes=61)), ("SENT", "normal"))

    def test_hourly_cap(self):
        self.assertEqual(self.decide(sent=CONFIG.hourly_cap), ("QUEUED", "cap"))
        self.assertEqual(self.decide(sent=CONFIG.hourly_cap - 1), ("SENT", "normal"))
        self.assertEqual(self.decide("critical", sent=100), ("SENT", "critical"))

    def test_mute_and_off(self):
        now = kst(12)
        muted = AlertPrefs(enabled=True, muted_until=now + timedelta(hours=1))
        self.assertEqual(self.decide(prefs=muted), ("QUEUED", "muted"))
        self.assertEqual(self.decide("critical", prefs=muted), ("SENT", "critical"))
        expired = AlertPrefs(enabled=True, muted_until=now - timedelta(minutes=1))
        self.assertEqual(self.decide(prefs=expired), ("SENT", "normal"))
        self.assertEqual(self.decide(prefs=AlertPrefs(enabled=False)), ("QUEUED", "off"))
        self.assertEqual(self.decide("critical", prefs=AlertPrefs(enabled=False)), ("SENT", "critical"))

    def test_parse_duration(self):
        cases = {"2h": 7200, "30m": 1800, "1h30m": 5400, "90분": 5400, "2시간": 7200, "45": 2700,
                 "12h": 43200, "13h": 46800, "": None, "0m": None, "abc": None, "2d": None, "-1h": None}
        for text, expected in cases.items():
            self.assertEqual(parse_duration(text), expected, text)

    def test_brief_due_once_per_day_within_window(self):
        self.assertIsNone(brief_due(kst(8, 29), CONFIG, None))
        self.assertEqual(brief_due(kst(8, 30), CONFIG, None), "2026-10-09")
        self.assertIsNone(brief_due(kst(9, 0), CONFIG, "2026-10-09"))
        self.assertIsNone(brief_due(kst(13, 0), CONFIG, None))
        self.assertEqual(brief_due(kst(8, 31, day=10), CONFIG, "2026-10-09"), "2026-10-10")

    def test_callback_data_is_short_and_pathless(self):
        rows = keyboard_rows(999_999_999, job_id=999_999_999, has_deliverables=True)
        data = [value for row in rows for _, value in row]
        self.assertEqual(data, ["al:999999999", "jf:999999999", "jl:999999999", "q:60"])
        for value in data:
            self.assertLess(len(value.encode()), 64)
            self.assertNotIn("/", value)
        no_job = [value for row in keyboard_rows(1, job_id=None, has_deliverables=False) for _, value in row]
        self.assertEqual(no_job, ["al:1", "q:60"])


class FileSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.work = base / "work"
        self.outside = base / "outside"
        (self.work / "p" / "web").mkdir(parents=True)
        self.outside.mkdir()
        self.roots = (self.work,)
        self.video = self.work / "p" / "web" / "hero.mp4"
        self.video.write_bytes(b"\x00" * 10)

    def tearDown(self):
        self.temp.cleanup()

    def refused(self, path):
        result = check_deliverable(path, self.roots)
        self.assertFalse(result.ok, path)
        return result

    def test_allowed_video(self):
        result = check_deliverable(self.video, self.roots)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "video")

    def test_traversal_and_relative_paths(self):
        secret = self.outside / "data.txt"
        secret.write_text("x")
        self.refused(f"{self.work}/p/../../outside/data.txt")
        self.refused("p/web/hero.mp4")
        self.refused(str(self.outside / "data.txt"))
        self.refused(str(self.work))

    def test_symlink_escape(self):
        target = self.outside / "loot.mp4"
        target.write_bytes(b"x")
        link = self.work / "p" / "web" / "link.mp4"
        link.symlink_to(target)
        self.assertIn("밖", self.refused(link).reason)
        dir_link = self.work / "p" / "escape"
        dir_link.symlink_to(self.outside, target_is_directory=True)
        self.refused(dir_link / "loot.mp4")

    def test_symlink_inside_root_is_fine(self):
        link = self.work / "p" / "alias.mp4"
        link.symlink_to(self.video)
        self.assertTrue(check_deliverable(link, self.roots).ok)

    def test_hidden_files_and_dirs(self):
        (self.work / "p" / ".hidden").mkdir()
        (self.work / "p" / ".hidden" / "a.png").write_bytes(b"x")
        (self.work / "p" / ".b.png").write_bytes(b"x")
        self.refused(self.work / "p" / ".hidden" / "a.png")
        self.refused(self.work / "p" / ".b.png")

    def test_secret_like_names(self):
        for name in (".env", "prod.env.txt", "id_rsa.txt", "api_token.json", "server.pem.txt", "credentials.csv",
                     "my-secret.md", "aws_key.txt", "password.txt", "private_key.pdf"):
            path = self.work / "p" / name
            path.write_text("x")
            self.refused(path)
        (self.work / "secrets").mkdir()
        (self.work / "secrets" / "a.png").write_bytes(b"x")
        self.refused(self.work / "secrets" / "a.png")
        self.assertFalse(secret_like_name("keyframe-01.png"))
        self.assertFalse(secret_like_name("hero-option1-text-free-seek.mp4"))

    def test_bad_extensions(self):
        for name in ("run.sh", "tool.py", "data.sqlite", "noext", "archive.zip", "x.pem"):
            path = self.work / "p" / name
            path.write_text("x")
            self.refused(path)

    def test_size_limit(self):
        big = self.work / "p" / "web" / "big.mp4"
        with big.open("wb") as handle:
            handle.truncate(MAX_UPLOAD_BYTES + 1)
        result = self.refused(big)
        self.assertTrue(result.too_large)
        self.assertEqual(result.size, MAX_UPLOAD_BYTES + 1)
        self.assertEqual(result.path, big.resolve())

    def test_missing_file(self):
        self.refused(self.work / "p" / "nope.mp4")
        path, reason = resolve_allowed("", self.roots)
        self.assertIsNone(path)


class MultipartTests(unittest.TestCase):
    def test_body_construction(self):
        body, content_type = build_multipart(
            {"chat_id": "100", "caption": "영상 · 2MB"}, "video", "hero.mp4", b"\x00\x01DATA", boundary="BOUND"
        )
        self.assertEqual(content_type, "multipart/form-data; boundary=BOUND")
        self.assertTrue(body.startswith(b"--BOUND\r\n"))
        self.assertTrue(body.endswith(b"--BOUND--\r\n"))
        self.assertIn(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n100\r\n', body)
        self.assertIn("영상 · 2MB".encode(), body)
        self.assertIn(b'name="video"; filename="hero.mp4"\r\nContent-Type: video/mp4\r\n\r\n\x00\x01DATA\r\n', body)

    def test_filename_is_sanitized(self):
        self.assertEqual(safe_filename('a"b\r\nc/../d.png'), "a_b_c_.._d.png")
        body, _ = build_multipart({}, "document", 'x"\r\ny.txt', b"t", boundary="B")
        self.assertIn(b'filename="x_y.txt"', body)


# ---------------------------------------------------------------- 봇 통합 (네트워크 없음)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.files = []
        self.answered = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def call_with_retry(self, method, payload, attempts=1):
        self.sent.append({"text": payload.get("text"), "reply_markup": payload.get("reply_markup"), "method": method})
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_id, text=""):
        self.answered.append(callback_id)

    def send_file(self, chat_id, kind, filename, content, caption="", reply_markup=None):
        self.files.append({"kind": kind, "filename": filename, "content": content, "caption": caption})
        return {"message_id": 1}


class FakeResponder:
    def __init__(self, action):
        self.action = action

    def respond(self, text, history, snapshot):
        return ChatResult(reply="확인할게요", action=ChatAction.from_dict(self.action), provider="gemini",
                          model="m", latency_ms=1)

    def active_providers(self):
        return []


class BotFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.home = base / "home"
        self.work = self.home / "work"
        self.job_dir = self.work / "hero" / "revisions" / "r1" / "render" / "full-v1"
        (self.job_dir / "web").mkdir(parents=True)
        (self.job_dir / "frames").mkdir()
        for name in ("STARTED", "COMPLETE"):
            (self.job_dir / name).write_text("2026-10-08T08:06:48+00:00\n")
        (self.job_dir / "web" / "hero.mp4").write_bytes(b"MP4DATA")
        (self.job_dir / "web" / "poster.jpg").write_bytes(b"JPG")
        (self.job_dir / "web" / "notes.json").write_text('{"api_key": "sk-abcdefghijklmnopqrstuvwx"}')
        (self.job_dir / "frames" / "0001.png").write_bytes(b"PNG")
        (self.job_dir / "run.log").write_text("render start\ntoken=supersecretvalue\nframes=420\n")
        (self.job_dir / "postprocess.log").write_text("encode\r 50%|█████ |\rdone\n")
        env_path = base / "notify.env"
        env_path.write_text("TELEGRAM_BOT_TOKEN=x\n")
        state_dir = base / "state"
        project = ProjectConfig(
            project_id="deskmini", repo_path=base, worktree_root=state_dir / "worktrees", default_branch="main",
            checks=(), deploy_enabled=False, deploy_environment=None, deploy_command=None,
            healthcheck_command=None, rollback_command=None, deployed_version_command=None,
        )
        self.settings = Settings(
            root=ROOT, state_dir=state_dir, db_path=state_dir / "chief.sqlite", telegram_token="test-token",
            allowed_chat_id=100, allowed_user_id=100, telegram_timeout_seconds=1,
            gemini_script=base / "missing.py", max_runtime_seconds=60, max_active_tasks=1,
            approval_ttl_seconds=600, projects={"deskmini": project}, env_path=env_path,
        )
        self.bot = ChiefBot(self.settings)
        self.bot.executor.shutdown(wait=False)
        self.bot.paths = WorkspacePaths(home=self.home, agent_hub=self.home / "agent-hub", work=self.work)
        self.bot.telegram = FakeTelegram()
        self.now = kst(12)
        self.bot.clock = lambda: self.now
        self.tg = self.bot.telegram

    def tearDown(self):
        self.temp.cleanup()

    def message(self, text, update_id=1):
        return {"update_id": update_id,
                "message": {"chat": {"type": "private", "id": 100}, "from": {"id": 100}, "text": text}}

    def press(self, data, update_id=50, chat_id=100, user_id=100, chat_type="private"):
        self.bot._handle_update({"update_id": update_id, "callback_query": {
            "id": f"cb{update_id}", "data": data, "from": {"id": user_id},
            "message": {"chat": {"type": chat_type, "id": chat_id}}}})

    def buttons(self, index=-1):
        markup = self.tg.sent[index].get("reply_markup") or {}
        return [(b["text"], b["callback_data"]) for row in markup.get("inline_keyboard", []) for b in row]

    def observation(self, jobs=None, **kwargs):
        return obs(at=self.now, jobs=jobs if jobs is not None else {}, **kwargs)


class BotWatchTests(BotFixture):
    def test_tick_baseline_then_alert_with_buttons_then_dedupe(self):
        key = "work/hero/revisions/r1/render/full-v1"
        running = job(key, "running", path=str(self.job_dir))
        self.assertEqual(self.bot.watch_tick(self.now, self.observation({key: running})), [])
        self.assertEqual(self.tg.sent, [])
        done = job(key, "complete", deliverables=True, path=str(self.job_dir))
        results = self.bot.watch_tick(self.now, self.observation({key: done}))
        self.assertEqual([(r["key"], r["status"]) for r in results], [(f"job:{key}:complete", "SENT")])
        labels = [label for label, _ in self.buttons()]
        self.assertEqual(labels, ["자세히", "결과물 받기", "로그 보기", "1시간 조용히"])
        for _, data in self.buttons():
            self.assertLess(len(data.encode()), 64)
            self.assertNotIn("/", data)
        # 같은 상태가 다시 관측돼도 새 알림 없음, 상태가 왔다 갔다 해도 창 안에서는 묶음
        self.assertEqual(self.bot.watch_tick(self.now, self.observation({key: done})), [])
        self.bot.watch_tick(self.now, self.observation({key: running}))
        again = self.bot.watch_tick(self.now, self.observation({key: done}))
        self.assertEqual(again[0]["status"], "SUPPRESSED")

    def test_quiet_hours_queue_then_morning_brief_once(self):
        self.now = kst(23, 30, day=8)
        self.bot.watch_tick(self.now, self.observation())
        results = self.bot.watch_tick(self.now, self.observation(tmux="inactive", disk_free=5 * GB))
        status = {r["key"]: (r["status"], r["reason"]) for r in results}
        self.assertEqual(status["tmux:gone"], ("QUEUED", "quiet"))
        self.assertEqual(status["disk:low"], ("SENT", "critical"))
        self.assertEqual(len(self.tg.sent), 1)
        self.now = kst(8, 30, day=9)
        self.assertTrue(self.bot.brief_tick(self.now))
        brief = self.tg.sent[-1]["text"]
        self.assertIn("아침 요약", brief)
        self.assertIn("Remote Control 세션 사라짐", brief)
        self.assertIn("보류됐던 것 1건", brief)
        self.now = kst(9, 0, day=9)
        self.assertFalse(self.bot.brief_tick(self.now))
        self.assertEqual(sum("아침 요약" in (m["text"] or "") for m in self.tg.sent), 1)
        self.assertEqual(self.bot.alerts.list_alerts(statuses=("QUEUED",)), [])

    def test_mute_then_digest_when_mute_ends(self):
        self.bot.watch_tick(self.now, self.observation())
        self.bot._handle_update(self.message("/quiet 30m"))
        self.assertIn("30분", self.tg.sent[-1]["text"])
        results = self.bot.watch_tick(self.now, self.observation(tmux="inactive"))
        self.assertEqual(results[0]["reason"], "muted")
        self.now = self.now + timedelta(minutes=31)
        results = self.bot.watch_tick(self.now, self.observation(tmux="inactive"))
        self.assertEqual(results[-1]["kind"], "digest")
        self.assertIn("보류됐던 알림 1건", self.tg.sent[-1]["text"])

    def test_bot_sent_task_result_is_not_repeated(self):
        task = "T-20261009-abcdef"
        self.bot.watch_tick(self.now, self.observation(tasks={task: {"state": "VERIFYING"}}))
        self.bot.alerts.record_bot_notice(f"task:{task}:READY", self.now)
        results = self.bot.watch_tick(self.now, self.observation(tasks={task: {"state": "READY"}}))
        self.assertEqual(results[0]["status"], "SUPPRESSED")
        self.assertEqual(self.tg.sent, [])

    def test_quiet_and_alerts_commands(self):
        self.bot._handle_update(self.message("/quiet 13h", 1))
        self.assertIn("최대 12시간", self.tg.sent[-1]["text"])
        self.bot._handle_update(self.message("/quiet 2h", 2))
        self.assertTrue(self.bot.alerts.prefs().muted(self.now))
        self.bot._handle_update(self.message("/quiet off", 3))
        self.assertFalse(self.bot.alerts.prefs().muted(self.now))
        self.bot._handle_update(self.message("/alerts off", 4))
        self.assertFalse(self.bot.alerts.prefs().enabled)
        self.bot._handle_update(self.message("/alerts", 5))
        self.assertIn("꺼짐", self.tg.sent[-1]["text"])
        self.bot._handle_update(self.message("/alerts on", 6))
        self.assertTrue(self.bot.alerts.prefs().enabled)

    def test_quiet_button(self):
        self.press("q:60")
        self.assertTrue(self.bot.alerts.prefs().muted(self.now + timedelta(minutes=59)))
        self.assertFalse(self.bot.alerts.prefs().muted(self.now + timedelta(minutes=61)))
        self.press("q:9999", update_id=51)
        self.assertIn("최대 12시간", self.tg.sent[-1]["text"])


class BotFileTests(BotFixture):
    def file_buttons(self):
        self.bot._handle_update(self.message("/files full-v1"))
        return dict(self.buttons())

    def test_files_lists_only_deliverables_and_sends_video(self):
        buttons = self.file_buttons()
        names = " ".join(buttons)
        self.assertIn("hero.mp4", names)
        self.assertIn("poster.jpg", names)
        self.assertNotIn("0001.png", names)  # frames/ 는 결과물 아님
        self.assertNotIn("run.log", names)
        for data in buttons.values():
            self.assertRegex(data, r"^f:\d+$")
        video = next(data for label, data in buttons.items() if "hero.mp4" in label)
        self.press(video)
        self.assertEqual(self.tg.files[-1]["kind"], "video")
        self.assertEqual(self.tg.files[-1]["content"], b"MP4DATA")

    def test_text_file_is_redacted(self):
        buttons = self.file_buttons()
        notes = next(data for label, data in buttons.items() if "notes.json" in label)
        self.press(notes)
        text = self.tg.sent[-1]["text"]
        self.assertIn("redacted", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", text)
        self.assertEqual(self.tg.files, [])

    def test_large_text_file_is_uploaded_after_redaction(self):
        big = self.job_dir / "web" / "big.txt"
        big.write_text("password=hunter2hunter2\n" + "x" * 5000)
        buttons = self.file_buttons()
        self.press(next(data for label, data in buttons.items() if "big.txt" in label))
        uploaded = self.tg.files[-1]
        self.assertEqual(uploaded["kind"], "document")
        self.assertNotIn(b"hunter2hunter2", uploaded["content"])

    def test_file_swapped_for_symlink_after_listing_is_refused(self):
        buttons = self.file_buttons()
        video = next(data for label, data in buttons.items() if "hero.mp4" in label)
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"LOOT")
        target = self.job_dir / "web" / "hero.mp4"
        target.unlink()
        target.symlink_to(outside)
        self.press(video)
        self.assertEqual(self.tg.files, [])
        self.assertIn("보내지 않았습니다", self.tg.sent[-1]["text"])

    def test_oversize_reports_size_and_path_only(self):
        big = self.job_dir / "web" / "huge.mp4"
        with big.open("wb") as handle:
            handle.truncate(MAX_UPLOAD_BYTES + 10)
        self.bot._handle_update(self.message("/files full-v1"))
        text = self.tg.sent[-1]["text"]
        self.assertIn("huge.mp4", text)
        self.assertIn("50MB 초과", text)
        self.assertIn("~/work/hero", text)
        self.assertFalse(any("huge" in label for label, _ in self.buttons()))

    def test_unknown_or_forged_callbacks(self):
        for index, data in enumerate(("f:999", "f:../../etc/passwd", "jl:abc", "zz:1", "al:")):
            self.press(data, update_id=60 + index)
        self.assertEqual(self.tg.files, [])
        self.assertTrue(all("passwd" not in (m["text"] or "") for m in self.tg.sent))

    def test_callbacks_from_other_chats_are_ignored(self):
        buttons = self.file_buttons()
        sent_before = len(self.tg.sent)
        video = next(iter(buttons.values()))
        self.press(video, update_id=70, chat_id=999)
        self.press(video, update_id=71, user_id=999)
        self.press(video, update_id=72, chat_type="group")
        self.assertEqual(self.tg.files, [])
        self.assertEqual(len(self.tg.sent), sent_before)
        self.assertEqual(self.tg.answered, [])

    def test_logs_are_trimmed_and_redacted(self):
        self.bot._handle_update(self.message("/logs full-v1"))
        texts = [m["text"] for m in self.tg.sent[-2:]]
        self.assertIn("run.log", texts[0])
        self.assertIn("frames=420", texts[0])
        self.assertNotIn("supersecretvalue", texts[0])
        self.assertIn("postprocess.log", texts[1])
        self.assertNotIn("█", texts[1])

    def test_jobs_command(self):
        self.bot._handle_update(self.message("/jobs"))
        text = self.tg.sent[-1]["text"]
        self.assertIn("hero · r1 · full-v1", text)
        self.assertIn("렌더 완료", text)

    def test_files_outside_allowed_roots_is_refused(self):
        outside = Path(self.temp.name) / "elsewhere" / "render" / "x"
        outside.mkdir(parents=True)
        (outside / "a.mp4").write_bytes(b"x")
        job_id = self.bot.alerts.upsert_job("elsewhere", str(outside), "elsewhere", "complete", self.now)
        self.press(f"jf:{job_id}")
        self.assertIn("보낼 수 없습니다", self.tg.sent[-1]["text"])

    def test_alert_detail_button_for_job(self):
        key = "work/hero/revisions/r1/render/full-v1"
        self.bot.watch_tick(self.now, self.observation({key: job(key, "running", path=str(self.job_dir))}))
        self.bot.watch_tick(self.now, self.observation({key: job(key, "failed", path=str(self.job_dir))}))
        detail = dict(self.buttons())["자세히"]
        self.press(detail)
        text = self.tg.sent[-1]["text"]
        self.assertIn("작업 실패", text)
        self.assertIn("~/work/hero", text)


class ChatActionRoutingTests(BotFixture):
    def test_policy_routes_new_actions_without_approval(self):
        policy = Policy(None, ["deskmini"])
        decision = policy.from_chat_action(ChatAction(type="send_files", job_hint="../../etc full-v1"), "영상 보내줘")
        self.assertEqual(decision.intent, "files")
        self.assertFalse(decision.needs_confirmation)
        self.assertNotIn("/", decision.argument)
        self.assertEqual(policy.from_chat_action(ChatAction(type="set_alerts", alerts="off"), "x").intent, "alerts")
        quiet = policy.from_chat_action(ChatAction(type="set_alerts", alerts="2h"), "x")
        self.assertEqual((quiet.intent, quiet.argument), ("quiet", "2h"))
        self.assertEqual(policy.decide_fixed("/files@bot full").intent, "files")
        self.assertEqual(policy.decide_fixed("/logs").intent, "logs")
        self.assertEqual(policy.decide_fixed("/jobs").intent, "jobs")

    def test_schema_and_prompt_know_new_actions(self):
        enum = RESPONSE_SCHEMA["properties"]["action"]["properties"]["type"]["enum"]
        self.assertIn("send_files", enum)
        self.assertIn("set_alerts", enum)
        for phrase in ("send_files", "set_alerts", "/files", "/quiet", "jobs_recent"):
            self.assertIn(phrase, SYSTEM_PROMPT)
        action = ChatAction.from_dict({"type": "send_files", "job_hint": "full-1080"})
        self.assertEqual(action.job_hint, "full-1080")

    def test_natural_language_send_files_shows_buttons_only(self):
        self.bot.chat = FakeResponder({"type": "send_files", "job_hint": "full-v1"})
        self.bot._handle_update(self.message("영상 보내줘"))
        # Only the real result receipt is shown; no model preface before the handler succeeds.
        self.assertIn("결과물 — 받을 파일을 누르세요", self.tg.sent[0]["text"])
        self.assertTrue(all(data.startswith("f:") for _, data in self.buttons()))
        self.assertEqual(self.tg.files, [])

    def test_natural_language_set_alerts(self):
        self.bot.chat = FakeResponder({"type": "set_alerts", "alerts": "1h"})
        self.bot._handle_update(self.message("한 시간만 조용히 해줘"))
        self.assertTrue(self.bot.alerts.prefs().muted(self.now + timedelta(minutes=30)))


if __name__ == "__main__":
    unittest.main()
