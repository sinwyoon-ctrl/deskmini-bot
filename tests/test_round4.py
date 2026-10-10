"""4라운드: 실제 처리 경로 수정, 재시도, 첨부로 지시, agent-hub 할 일."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from adapters.claude_cli import ClaudeCliReviewer, parse_cli_review
from adapters.gemini_rest import InterpretationUnavailable, review_instruction
from adapters.telegram import FILE_PATH_RE, TelegramAPI, TelegramError
from app.chat import RESPONSE_SCHEMA, SYSTEM_PROMPT, ChatAction, ChatResult
from app.config import ProjectConfig, Settings
from app.inbox import (
    AttachmentRejected,
    check_content,
    describe,
    incoming_from_message,
    sanitize_name,
    stage_into_worktree,
)
from app.policy import Policy, todo_decision
from app.release import ReleaseManager, ReleaseRefused
from app.retry import parse_reset_time, scheduled_time
from app.runner import RunnerError, TaskRunner, UnitStatus
from app.state import StateStore
from app.todo import HubTodo
from app.verify import build_reviewer
from app.worker import claude_sandbox

from test_watch import BotFixture, kst


ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def git(cwd, *args):
    return subprocess.run(("git", *args), cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(root, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@invalid.local")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "base")
    return repo


# ---------------------------------------------------------------- Part 1: 실제 경로 수정

class ReviewProviderTests(unittest.TestCase):
    def test_cli_review_parses_structured_output(self):
        stdout = json.dumps({"is_error": False, "structured_output": {"verdict": "pass", "findings": ["ok"], "summary": "s"}})
        result = parse_cli_review(stdout, "", 0)
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.findings, ["ok"])

    def test_cli_review_falls_back_to_result_text(self):
        stdout = json.dumps({"result": '```json\n{"verdict":"FAIL","findings":[],"summary":"x"}\n```'})
        self.assertEqual(parse_cli_review(stdout, "", 0).verdict, "FAIL")

    def test_cli_review_errors_become_unavailable(self):
        with self.assertRaises(InterpretationUnavailable):
            parse_cli_review("not json", "bwrap: boom", 1)
        with self.assertRaises(InterpretationUnavailable):
            parse_cli_review(json.dumps({"is_error": True, "result": "You've hit your limit"}), "", 1)

    def test_cli_review_argv_is_sandboxed_without_tools_or_api_key(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / ".credentials.json").write_text("{}")
            reviewer = ClaudeCliReviewer(home=home)
            argv = reviewer.argv(Path(temp) / "work", "deskmini-chief-r-abc")
        self.assertEqual(argv[:2], ["systemd-run", "--user"])
        self.assertIn("bwrap", argv)
        self.assertIn("--clearenv", argv)
        tools = argv.index("--tools")
        self.assertEqual(argv[tools + 1], "")
        self.assertNotIn("ANTHROPIC_API_KEY", " ".join(argv))
        credentials = str(home / ".claude" / ".credentials.json")
        index = argv.index(credentials)
        self.assertEqual(argv[index - 1], "--ro-bind")

    def test_cli_reviewer_passes_attachments_in_prompt(self):
        captured = {}

        def runner(argv, **kwargs):
            captured["input"] = kwargs["input"]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"structured_output": {"verdict": "PASS", "findings": [], "summary": ""}}), "")

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / ".credentials.json").write_text("{}")
            reviewer = ClaudeCliReviewer(home=home, runner=runner, work_root=Path(temp) / "review")
            with patch("adapters.claude_cli.shutil.which", return_value="/usr/bin/claude"):
                result = reviewer.review_diff(task_specification={"objective": "x"}, diff_text="d", checks=[],
                                              attachments=[{"path": ".chief-inbox/a.csv", "content_redacted": "a,1"}])
        self.assertEqual(result.verdict, "PASS")
        self.assertIn(".chief-inbox/a.csv", captured["input"])

    def test_review_instruction_attachments_optional(self):
        self.assertNotIn("attachments", review_instruction({}, "d", []))
        data = review_instruction({}, "d", [], [{"content_redacted": "token=abcdefghijklmnop"}])
        self.assertIn("attachments", data)
        self.assertNotIn("abcdefghijklmnop", json.dumps(data, ensure_ascii=False))

    def test_build_reviewer_reads_models_toml(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "models.toml"
            sentinel = object()
            path.write_text('[review]\nprovider = "claude_cli"\n')
            self.assertIsInstance(build_reviewer(path, sentinel, Path(temp)), ClaudeCliReviewer)
            path.write_text('[review]\nprovider = "gemini"\n')
            self.assertIs(build_reviewer(path, sentinel, Path(temp)), sentinel)
            path.write_text('[review]\nprovider = "rm -rf"\n')
            self.assertIs(build_reviewer(path, sentinel, Path(temp)), sentinel)

    def test_live_review_config_is_gemini_then_claude_cli(self):
        from app.verify import FallbackReviewer

        sentinel = object()
        reviewer = build_reviewer(ROOT / "config" / "models.toml", sentinel, Path("/nonexistent"))
        self.assertIsInstance(reviewer, FallbackReviewer)
        self.assertEqual([name for name, _ in reviewer.reviewers], ["gemini", "claude_cli"])
        self.assertIs(reviewer.reviewers[0][1], sentinel)

    def test_claude_sandbox_binds_only_workdir_writable(self):
        argv = claude_sandbox(workdir=Path("/home/u/w"), home=Path("/home/u"), credentials=Path("/home/u/.claude/.credentials.json"))
        binds = [argv[i + 1] for i, item in enumerate(argv) if item == "--bind"]
        self.assertEqual(binds, ["/home/u/w"])
        self.assertIn("--clearenv", argv)


class ProjectSelectionTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy(None, ["deskmini", "sandbox"])

    def test_new_with_project_prefix(self):
        for text in ("/new sandbox: README 고쳐", "/new sandbox README 고쳐", "작업: sandbox: README 고쳐"):
            decision = self.policy.decide_fixed(text)
            self.assertEqual(decision.project_id, "sandbox", text)
            self.assertEqual(decision.specification["objective"], "README 고쳐")

    def test_new_without_project_uses_default(self):
        decision = self.policy.decide_fixed("/new 메모리 API 추가")
        self.assertEqual(decision.project_id, "deskmini")
        # 등록되지 않은 'other:' 는 기본 프로젝트로 몰래 보내지 않는다(아래 UnknownProjectSelectorTests).
        plain = self.policy.decide_fixed("/new README: 실행 방법 추가")
        self.assertEqual(plain.project_id, "deskmini")
        self.assertIn("README:", plain.specification["objective"])

    def test_registered_projects_are_both_listed(self):
        from app.config import load_projects

        with tempfile.TemporaryDirectory() as temp:
            projects = load_projects(ROOT / "config" / "projects.toml", Path(temp))
        self.assertEqual(list(projects)[0], "deskmini")
        self.assertIn("sandbox", projects)
        self.assertFalse(projects["sandbox"].deploy_enabled)

    def test_service_unit_can_write_registered_repos_and_hub(self):
        unit = (ROOT / "deploy" / "deskmini-chief.service").read_text()
        line = next(item for item in unit.splitlines() if item.startswith("ReadWritePaths="))
        for path in ("/home/ionenet/sandbox-chief", "/home/ionenet/agent-hub", "/home/ionenet/deskmini",
                     "/home/ionenet/.local/state/deskmini-chief"):
            self.assertIn(path, line.split("=", 1)[1].split())


class NoOriginPushTests(unittest.TestCase):
    def test_push_card_is_refused_politely_without_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_repo(root)
            project = ProjectConfig(project_id="sandbox", repo_path=repo, worktree_root=root / "wt", default_branch="main",
                                    checks=(), deploy_enabled=False, deploy_environment=None, deploy_command=None,
                                    healthcheck_command=None, rollback_command=None, deployed_version_command=None)
            settings = Settings(root=ROOT, state_dir=root / "state", db_path=root / "state" / "c.sqlite", telegram_token="t",
                                allowed_chat_id=1, allowed_user_id=1, telegram_timeout_seconds=1, gemini_script=root / "x",
                                max_runtime_seconds=60, max_active_tasks=1, approval_ttl_seconds=60,
                                projects={"sandbox": project})
            store = StateStore(settings.db_path, ROOT / "schema.sql")
            task = store.create_task(owner_user_id=1, chat_id=1, project_id="sandbox", request_text="x",
                                     specification={}, policy_hash="h", budget={})
            store.transition(task["id"], expected_state="AWAITING_START", new_state="READY",
                             fields={"head_sha": git(repo, "rev-parse", "HEAD"), "base_sha": git(repo, "rev-parse", "HEAD")})
            with self.assertRaises(ReleaseRefused) as caught:
                ReleaseManager(settings, store).approval_payload(task["id"], "push")
            self.assertIn("origin", str(caught.exception))


# ---------------------------------------------------------------- Part 2: 재시도

class ResetParseTests(unittest.TestCase):
    NOW = datetime(2026, 10, 9, 6, 15, tzinfo=UTC)

    def test_formats(self):
        cases = {
            "You've hit your weekly limit · resets 3am (UTC)": datetime(2026, 10, 10, 3, 0, tzinfo=UTC),
            "You've hit your limit · resets 3pm (Asia/Seoul)": datetime(2026, 10, 10, 6, 0, tzinfo=UTC),
            "resets 4pm (Asia/Seoul)": datetime(2026, 10, 9, 7, 0, tzinfo=UTC),
            "usage limit · resets Oct 10, 10:30pm (Asia/Seoul)": datetime(2026, 10, 10, 13, 30, tzinfo=UTC),
            "rate limit · resets in 2 hours": self.NOW + timedelta(hours=2),
            "resets 14:30 (UTC)": datetime(2026, 10, 9, 14, 30, tzinfo=UTC),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_reset_time(text, self.NOW), expected, text)

    def test_unparseable(self):
        for text in ("", "tool failed", "resets 3", "resets 25pm (UTC)", "resets 3pm (Mars/Base)", "resets 13pm"):
            self.assertIsNone(parse_reset_time(text, self.NOW), text)

    def test_margin(self):
        reset = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
        self.assertGreater(scheduled_time(reset), reset)


class RunnerRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        self.state = self.root / "state"
        for child in ("tasks", "evidence", "worktrees", "inbox"):
            (self.state / child).mkdir(parents=True)
        self.project = ProjectConfig(project_id="sandbox", repo_path=self.repo, worktree_root=self.state / "worktrees",
                                     default_branch="main", checks=(), deploy_enabled=False, deploy_environment=None,
                                     deploy_command=None, healthcheck_command=None, rollback_command=None,
                                     deployed_version_command=None)
        self.settings = Settings(root=ROOT, state_dir=self.state, db_path=self.state / "c.sqlite", telegram_token="t",
                                 allowed_chat_id=1, allowed_user_id=1, telegram_timeout_seconds=1,
                                 gemini_script=self.root / "x", max_runtime_seconds=60, max_active_tasks=1,
                                 approval_ttl_seconds=60, projects={"sandbox": self.project})
        self.store = StateStore(self.settings.db_path, ROOT / "schema.sql")
        self.runner = TaskRunner(self.settings, self.store)
        self.runner._systemd_command = lambda **kwargs: ("true",)
        self.runner.unit_status = lambda name: UnitStatus(True, "active", "running", "success", None, "inv", True)

    def tearDown(self):
        self.temp.cleanup()

    def failed_task(self, classification="SUCCESS", run_state="COMPLETED", final="FAILED", evidence=None, retries=1):
        task = self.store.create_task(owner_user_id=1, chat_id=1, project_id="sandbox", request_text="x",
                                      specification={"objective": "x"}, policy_hash="h", budget={"retries": retries})
        started = self.runner.start(task["id"])
        self.assertEqual(started["state"], "RUNNING")
        run = self.store.latest_run(task["id"])
        self.store.update_run(run["id"], state=run_state, result_json={"classification": classification, "error": "boom"})
        (self.state / "tasks" / task["id"] / "result.json").write_text("{}")
        self.store.transition(task["id"], expected_state="RUNNING", new_state=final)
        if evidence is not None:
            (self.state / "evidence" / f"{task['id']}-verification.json").write_text(json.dumps(evidence))
        return self.store.get_task(task["id"])

    def test_failed_after_checks_reruns_worker_in_same_worktree_with_feedback(self):
        task = self.failed_task(evidence={"verdict": "FAIL", "reason": "One or more registered checks failed",
                                          "checks": [{"argv": ["t"], "passed": False, "output": "AssertionError: 6 != 5"}]})
        (Path(task["worktree_path"]) / "README.md").write_text("changed\n")
        self.assertEqual(self.runner.retry_mode(task), "run")
        self.assertEqual(self.runner.retry_budget(task), (0, 1))
        started = self.runner.retry(task["id"], expected_version=task["version"])
        self.assertEqual(started["state"], "RUNNING")
        self.assertEqual(self.store.latest_run(task["id"])["attempt"], 2)
        self.assertEqual((Path(task["worktree_path"]) / "README.md").read_text(), "changed\n")
        mission = json.loads((self.state / "tasks" / task["id"] / "mission.json").read_text())
        self.assertIn("6 != 5", json.dumps(mission["previous_attempt"], ensure_ascii=False))
        self.assertTrue((self.state / "tasks" / task["id"] / "result-attempt1.json").is_file())
        self.assertFalse((self.state / "tasks" / task["id"] / "result.json").exists())

    def test_budget_exhausted(self):
        task = self.failed_task(retries=0)
        allowed, reason = self.runner.can_retry(task)
        self.assertFalse(allowed)
        self.assertIn("한도", reason)
        with self.assertRaises(RunnerError):
            self.runner.retry(task["id"])

    def test_limit_blocked_runs_do_not_consume_budget(self):
        task = self.failed_task(classification="LIMIT_BLOCKED", run_state="BLOCKED", final="BLOCKED", retries=0)
        self.assertEqual(self.runner.retry_budget(task), (0, 0))
        self.assertTrue(self.runner.can_retry(task)[0])

    def test_review_unavailable_retries_verification_only(self):
        task = self.failed_task(final="BLOCKED", evidence={"verdict": "UNVERIFIED"})
        self.assertEqual(self.runner.retry_mode(task), "verify")
        result = self.runner.retry(task["id"])
        self.assertEqual(result["state"], "VERIFYING")
        self.assertEqual(len(self.store.list_runs(task["id"])), 1)

    def test_unverified_review_failure_retries_verification_only(self):
        task = self.failed_task(evidence={"verdict": "FAIL", "review_verdict": "UNVERIFIED"})
        self.assertEqual(self.runner.retry_mode(task), "verify")

    def test_version_mismatch_and_wrong_state(self):
        task = self.failed_task()
        with self.assertRaises(RunnerError):
            self.runner.retry(task["id"], expected_version=task["version"] - 1)
        other = self.store.create_task(owner_user_id=1, chat_id=1, project_id="sandbox", request_text="x",
                                       specification={}, policy_hash="h", budget={})
        with self.assertRaises(RunnerError):
            self.runner.retry(other["id"])

    def test_recoverable_retry_approval(self):
        task = self.failed_task()
        approval_id = self.store.create_approval(task_id=task["id"], action="retry", payload={"task_version": task["version"]},
                                                 owner_user_id=1, chat_id=1, task_version=task["version"], ttl_seconds=60)
        self.store.consume_approval(approval_id=approval_id, action="retry", owner_user_id=1, chat_id=1)
        self.assertEqual([item["id"] for item in self.store.recoverable_consumed_approvals()], [approval_id])
        self.runner.retry(task["id"])
        self.assertEqual(self.store.recoverable_consumed_approvals(), [])


class FakeRunner:
    def __init__(self, mode="run", allowed=True, budget=(0, 1)):
        self.mode = mode
        self.allowed = allowed
        self.budget = budget
        self.retried = []

    def retry_mode(self, task):
        return self.mode

    def can_retry(self, task):
        return (self.allowed, "" if self.allowed else "재시도 한도(1회)를 다 썼습니다. 새로 발주해 주세요")

    def retry_budget(self, task):
        return self.budget

    def retry(self, task_id, expected_version=None):
        self.retried.append((task_id, expected_version))
        return {"id": task_id, "state": "RUNNING"}


class BotRetryTests(BotFixture):
    def setUp(self):
        super().setUp()
        self.fake = FakeRunner()
        self.bot.runner = self.fake
        self.task = self.bot.store.create_task(owner_user_id=100, chat_id=100, project_id="deskmini", request_text="x",
                                               specification={}, policy_hash="h", budget={"retries": 1})
        self.bot.store.transition(self.task["id"], expected_state="AWAITING_START", new_state="STARTING")
        self.run = self.bot.store.create_run(self.task["id"], unit_name="u", requested_model="m", boot_id="b")

    def finish(self, state, result):
        self.bot.store.update_run(self.run["id"], state=state, result_json=result)
        self.bot.store.transition(self.task["id"], expected_state="STARTING", new_state=state)

    def buttons(self):
        return [b["callback_data"] for m in self.tg.sent for row in (m.get("reply_markup") or {}).get("inline_keyboard", [])
                for b in row]

    def press(self, data, update_id=50):
        self.bot._handle_update({"update_id": update_id, "callback_query": {
            "id": "c", "data": data, "from": {"id": 100}, "message": {"chat": {"type": "private", "id": 100}}}})

    def test_failed_card_has_retry_button_and_press_retries_once(self):
        self.finish("FAILED", {"classification": "EXECUTION_FAILED", "error": "bwrap: denied"})
        self.bot._offer_retry(self.task["id"])
        self.assertIn("bwrap: denied", self.tg.sent[-1]["text"])
        data = [b for b in self.buttons() if b.startswith("approve:retry:")]
        self.assertEqual(len(data), 1)
        self.press(data[0])
        self.assertEqual(self.fake.retried, [(self.task["id"], self.bot.store.get_task(self.task["id"])["version"])])
        self.press(data[0], update_id=51)  # 같은 버튼 다시 누름 → 일회용
        self.assertEqual(len(self.fake.retried), 1)

    def test_budget_exhausted_has_no_button(self):
        self.fake.allowed = False
        self.finish("FAILED", {"classification": "EXECUTION_FAILED", "error": "x"})
        self.bot._offer_retry(self.task["id"])
        self.assertEqual(self.buttons(), [])
        self.assertIn("한도", self.tg.sent[-1]["text"])

    def test_limit_blocked_offers_schedule_then_runs_at_time(self):
        self.now = kst(12)
        self.finish("BLOCKED", {"classification": "LIMIT_BLOCKED", "error": "You've hit your limit · resets 3pm (Asia/Seoul)"})
        self.bot._offer_retry(self.task["id"])
        text = self.tg.sent[-1]["text"]
        self.assertIn("15:00", text)
        data = self.buttons()
        self.assertEqual(len(data), 1)
        self.assertTrue(data[0].startswith("approve:schedule_retry:"))
        self.press(data[0])
        self.assertEqual(self.fake.retried, [])
        pending = self.bot.store.pending_retry_for_task(self.task["id"])
        self.assertEqual(datetime.fromisoformat(pending["run_at"]), kst(15, 5))
        self.bot._run_scheduled_retries()
        self.assertEqual(self.fake.retried, [])  # 아직 시각 전
        self.now = kst(15, 6)
        self.bot._run_scheduled_retries()
        self.bot._run_scheduled_retries()  # 두 번 실행하지 않는다
        self.assertEqual(len(self.fake.retried), 1)

    def test_scheduled_retry_skipped_when_task_changed(self):
        self.now = kst(12)
        self.finish("BLOCKED", {"classification": "LIMIT_BLOCKED", "error": "resets 3pm (Asia/Seoul)"})
        self.bot._offer_retry(self.task["id"])
        self.press(self.buttons()[0])
        self.bot.store.transition(self.task["id"], expected_state="BLOCKED", new_state="STOP_REQUESTED")
        self.now = kst(16)
        self.bot._run_scheduled_retries()
        self.assertEqual(self.fake.retried, [])
        self.assertIsNone(self.bot.store.pending_retry_for_task(self.task["id"]))

    def test_limit_without_reset_time_offers_immediate_retry(self):
        self.finish("BLOCKED", {"classification": "LIMIT_BLOCKED", "error": "usage limit"})
        self.bot._offer_retry(self.task["id"])
        self.assertTrue(self.buttons()[0].startswith("approve:retry:"))

    def test_retry_command(self):
        self.finish("FAILED", {"classification": "EXECUTION_FAILED", "error": "x"})
        self.bot._handle_update(self.message(f"/retry {self.task['id']}", update_id=7))
        self.assertTrue(any(b.startswith("approve:retry:") for b in self.buttons()))
        self.bot._handle_update(self.message("/retry", update_id=8))
        self.assertIn("작업 ID", self.tg.sent[-1]["text"])


# ---------------------------------------------------------------- Part 3: 첨부

class InboxRuleTests(unittest.TestCase):
    def test_sanitize_name(self):
        self.assertEqual(sanitize_name("../../etc/가격 표.csv"), "가격_표.csv")
        self.assertEqual(sanitize_name("photo", "image/png"), "photo.png")
        self.assertEqual(sanitize_name(".hidden.txt"), "hidden.txt")
        for bad in ("a.zip", "run.sh", "x.exe", "doc.docx", "noext", "a.txt.zip"):
            with self.assertRaises(AttachmentRejected, msg=bad):
                sanitize_name(bad)
        with self.assertRaises(AttachmentRejected):
            sanitize_name("api_key.txt")

    def test_content_signatures(self):
        self.assertEqual(check_content("a.png", PNG), "image")
        self.assertEqual(check_content("a.txt", "가격 표".encode()), "text")
        self.assertEqual(check_content("a.pdf", b"%PDF-1.7\n..."), "pdf")
        for name, data in (("a.txt", b"\x7fELF\x02\x01"), ("a.png", b"GIF89a...."), ("a.jpg", PNG),
                           ("a.txt", b"a\x00b"), ("a.txt", b"\xff\xfe\xfa"), ("a.csv", b"#!/bin/sh\nrm -rf /"),
                           ("a.json", b"PK\x03\x04zip"), ("a.txt", b"")):
            with self.assertRaises(AttachmentRejected, msg=(name, data)):
                check_content(name, data)

    def test_largest_photo_is_chosen(self):
        message = {"photo": [{"file_id": "s", "file_size": 10, "width": 90}, {"file_id": "l", "file_size": 900, "width": 1280},
                             {"file_id": "m", "file_size": 100, "width": 320}]}
        incoming = incoming_from_message(message)
        self.assertEqual(incoming.file_id, "l")
        self.assertEqual(incoming.kind, "photo")
        self.assertIsNone(incoming_from_message({"text": "hi"}))

    def test_telegram_file_path_rules(self):
        self.assertTrue(FILE_PATH_RE.fullmatch("documents/file_12.csv"))
        api = TelegramAPI("123:abc")
        for bad in ("../x", "/etc/passwd", "a/../../b", ""):
            with self.assertRaises(TelegramError):
                api.download_file(bad, 10)

    def test_stage_into_worktree_redacts_and_excludes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_repo(root)
            worktree = root / "wt"
            git(repo, "worktree", "add", "-q", "-b", "chief/t", str(worktree))
            inbox = root / "inbox" / "2026-10-09"
            inbox.mkdir(parents=True)
            source = inbox / "120000-aa-prices.csv"
            source.write_text("name,price\nkey,token=abcdefghijklmnopqrstuvwx\n")
            image = inbox / "120001-bb-shot.png"
            image.write_bytes(PNG)
            outside = root / "outside.txt"
            outside.write_text("secret")
            items = [
                {"id": 1, "stored_path": str(source), "stored_name": "prices.csv", "caption": "c"},
                {"id": 2, "stored_path": str(image), "stored_name": "shot.png"},
                {"id": 3, "stored_path": str(outside), "stored_name": "outside.txt"},
            ]
            staged = stage_into_worktree(items, worktree, root / "inbox")
            self.assertEqual([item["path"] for item in staged], [".chief-inbox/prices.csv", ".chief-inbox/shot.png"])
            copied = (worktree / ".chief-inbox" / "prices.csv").read_text()
            self.assertNotIn("abcdefghijklmnopqrstuvwx", copied)
            self.assertEqual((worktree / ".chief-inbox" / "shot.png").read_bytes(), PNG)
            self.assertEqual(os.stat(worktree / ".chief-inbox" / "prices.csv").st_mode & 0o777, 0o444)
            self.assertEqual(git(worktree, "status", "--porcelain"), "")
            again = stage_into_worktree(items[:1], worktree, root / "inbox")  # 재시도: 같은 사본 재사용
            self.assertEqual(again[0]["path"], ".chief-inbox/prices.csv")
            evil = root / "evil-wt"
            git(repo, "worktree", "add", "-q", "-b", "chief/e", str(evil))
            (root / "victim").mkdir()
            os.symlink(root / "victim", evil / ".chief-inbox")
            with self.assertRaises(RuntimeError):
                stage_into_worktree(items[:1], evil, root / "inbox")
            self.assertEqual(list((root / "victim").iterdir()), [])
            exclude = Path(git(worktree, "rev-parse", "--git-common-dir"))
            exclude = (exclude if exclude.is_absolute() else worktree / exclude) / "info" / "exclude"
            self.assertEqual(exclude.read_text().count("/.chief-inbox/"), 1)


class AttachmentTelegram:
    def __init__(self, base, files):
        self.base = base
        self.files = files
        self.downloads = []

    def get_file(self, file_id):
        return {"file_id": file_id, "file_size": len(self.files[file_id]), "file_path": f"documents/{file_id}"}

    def download_file(self, file_path, max_bytes):
        self.downloads.append(file_path)
        return self.files[file_path.split("/")[-1]]

    def __getattr__(self, name):
        return getattr(self.base, name)


class CapturingResponder:
    def __init__(self, action=None):
        self.action = action or {"type": "none"}
        self.texts = []

    def respond(self, text, history, snapshot):
        self.texts.append(text)
        return ChatResult(reply="알겠습니다", action=ChatAction.from_dict(self.action), provider="gemini", model="m", latency_ms=1)

    def active_providers(self):
        return []


class BotAttachmentTests(BotFixture):
    def setUp(self):
        super().setUp()
        self.files = {"doc1": "상품,가격\n사과,1000\npassword=hunter2hunter2\n".encode(), "png1": PNG,
                      "zip1": b"PK\x03\x04", "elf1": b"\x7fELF...."}
        self.bot.telegram = AttachmentTelegram(self.tg, self.files)
        self.responder = CapturingResponder()
        self.bot.chat = self.responder

    def document(self, file_id, name, caption=None, update_id=10, size=None):
        message = {"chat": {"type": "private", "id": 100}, "from": {"id": 100},
                   "document": {"file_id": file_id, "file_name": name, "file_size": size or len(self.files.get(file_id, b"x"))}}
        if caption is not None:
            message["caption"] = caption
        return {"update_id": update_id, "message": message}

    def test_no_caption_asks_what_to_do_and_stores_file(self):
        self.bot._handle_update(self.document("doc1", "가격.csv"))
        self.assertIn("무엇을 할까요", self.tg.sent[-1]["text"])
        stored = list((self.settings.state_dir / "inbox").rglob("*.csv"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(os.stat(stored[0]).st_mode & 0o777, 0o600)
        # 다음 지시가 대화로 가면 대기 중 첨부 메타데이터와 마스킹된 내용이 함께 간다.
        self.bot._handle_update(self.message("이걸로 README 정리해줘", update_id=11))
        self.assertIn("[대기 중 첨부: 가격.csv", self.responder.texts[-1])
        self.assertIn("사과,1000", self.responder.texts[-1])
        self.assertNotIn("hunter2hunter2", self.responder.texts[-1])

    def test_caption_new_creates_task_with_attachment(self):
        self.bot._handle_update(self.document("doc1", "가격.csv", caption="/new 첨부 표를 README 에 정리"))
        tasks = self.bot.store.list_tasks(5)
        self.assertEqual(len(tasks), 1)
        attachments = self.bot.store.attachments_for_task(tasks[0]["id"])
        self.assertEqual([item["stored_name"] for item in attachments], ["가격.csv"])
        card = next(m["text"] for m in self.tg.sent if "작업 발주 확인" in (m["text"] or ""))
        self.assertIn("첨부", card)
        self.assertIn("가격.csv", card)

    def test_caption_natural_language_goes_to_chat_with_metadata_only_for_images(self):
        self.responder.action = {"type": "create_task", "objective": "스크린샷대로 버튼 색 바꾸기"}
        message = {"update_id": 12, "message": {"chat": {"type": "private", "id": 100}, "from": {"id": 100},
                                                "photo": [{"file_id": "png1", "file_size": len(PNG), "width": 10}],
                                                "caption": "이 화면처럼 버튼 색 바꿔줘"}}
        self.bot._handle_update(message)
        self.assertIn("[첨부: photo-", self.responder.texts[-1])
        self.assertIn(".png", self.responder.texts[-1])
        self.assertNotIn("첨부 내용", self.responder.texts[-1])
        task = self.bot.store.list_tasks(1)[0]
        self.assertEqual(len(self.bot.store.attachments_for_task(task["id"])), 1)

    def test_rejections_do_not_store(self):
        self.bot._handle_update(self.document("zip1", "a.zip", caption="/new 풀어줘", update_id=20))
        self.assertIn("받지 않았습니다", self.tg.sent[-1]["text"])
        self.bot._handle_update(self.document("elf1", "notes.txt", update_id=21))
        self.assertIn("받지 않았습니다", self.tg.sent[-1]["text"])
        self.bot._handle_update(self.document("doc1", "big.csv", update_id=22, size=30 * 1024 * 1024))
        self.assertIn("20MB", self.tg.sent[-1]["text"])
        self.assertEqual(self.bot.telegram.downloads, ["documents/elf1"])
        self.assertFalse(list((self.settings.state_dir / "inbox").rglob("*.*")) if (self.settings.state_dir / "inbox").exists() else [])
        self.assertEqual(self.bot.store.list_tasks(5), [])

    def test_describe(self):
        self.assertEqual(describe({"stored_name": "a.csv", "kind": "document", "size": 2048}), "[첨부: a.csv, csv, 2KB]")


# ---------------------------------------------------------------- Part 4: 할 일

class TodoPolicyTests(unittest.TestCase):
    def test_todo_command_forms(self):
        self.assertEqual(todo_decision("").intent, "todo_list")
        added = todo_decision("codex 로그인 문구 정리")
        self.assertEqual((added.intent, added.project_id, added.argument), ("todo_add", "codex", "로그인 문구 정리"))
        self.assertEqual(todo_decision("done 2").intent, "todo_done")
        self.assertEqual(todo_decision("done 20261009-143522-ionenet-codex").argument, "20261009-143522-ionenet-codex")
        for bad in ("done ../x", "bob 할 일", "codex", "done"):
            self.assertEqual(todo_decision(bad).intent, "usage", bad)

    def test_chat_dispatch_todo(self):
        policy = Policy(None, ["deskmini"])
        decision = policy.from_chat_action(ChatAction.from_dict({"type": "dispatch_todo", "assignee": "Claude",
                                                                 "todo_text": "테스트\n보강"}), "x")
        self.assertEqual((decision.intent, decision.project_id, decision.argument), ("todo_add", "claude", "테스트 보강"))
        self.assertIsNone(policy.from_chat_action(ChatAction.from_dict({"type": "dispatch_todo", "assignee": "root",
                                                                        "todo_text": "x"}), "x"))
        self.assertIsNone(policy.from_chat_action(ChatAction.from_dict({"type": "dispatch_todo", "assignee": "codex"}), "x"))

    def test_schema_and_prompt(self):
        props = RESPONSE_SCHEMA["properties"]["action"]["properties"]
        self.assertIn("dispatch_todo", props["type"]["enum"])
        self.assertIn("assignee", RESPONSE_SCHEMA["properties"]["action"]["required"])
        self.assertIn("dispatch_todo", SYSTEM_PROMPT)


class HubTodoTests(unittest.TestCase):
    def test_fixed_argv_without_shell(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "할 일: 20261009-000000-h-codex\n", "")

        with tempfile.TemporaryDirectory() as temp:
            hub = Path(temp)
            (hub / "bin").mkdir()
            (hub / "bin" / "hub").write_text("#!/bin/bash\n")
            todo = HubTodo(hub, runner=runner)
            self.assertTrue(todo.add("codex", "a; rm -rf / $(id)\n둘째 token=abcdefghijklmnop").ok)
            self.assertFalse(todo.add("root", "x").ok)
            self.assertFalse(todo.add("codex", "  \n ").ok)
            self.assertFalse(todo.done("../x").ok)
            self.assertTrue(todo.done("3").ok)
        argv, kwargs = calls[0]
        self.assertEqual(argv[:5], ["/bin/bash", str(hub / "bin" / "hub"), "todo", "add", "codex"])
        self.assertEqual(argv[6:], ["--from", "telegram"])
        self.assertNotIn("\n", argv[5])
        self.assertNotIn("abcdefghijklmnop", argv[5])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["env"]["HUB_NO_NOTIFY"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
        self.assertEqual(calls[1][0][2:], ["todo", "done", "3", "telegram"])

    @unittest.skipUnless(Path.home().joinpath("agent-hub", "bin", "hub").is_file() and shutil.which("git"), "agent-hub 없음")
    def test_real_hub_script_in_scratch_clone(self):
        with tempfile.TemporaryDirectory() as temp:
            clone = Path(temp) / "hub"
            git(Path(temp), "clone", "-q", str(Path.home() / "agent-hub"), str(clone))
            git(clone, "remote", "remove", "origin")
            git(clone, "config", "user.name", "T")
            git(clone, "config", "user.email", "t@invalid.local")
            shutil.copy(Path.home() / "agent-hub" / "bin" / "hub", clone / "bin" / "hub")
            if (clone / "todo").is_dir():
                shutil.rmtree(clone / "todo")  # 임시 복제본의 실제 할 일은 지우고 시작(원본은 건드리지 않음)
            git(clone, "add", "-A")
            git(clone, "commit", "-qm", "test script", "--allow-empty")
            todo = HubTodo(clone)
            added = todo.add("codex", "문구 정리")
            self.assertTrue(added.ok, added.output)
            listed = todo.list()
            self.assertIn("[codex] 문구 정리", listed.output)
            read = subprocess.run(["/bin/bash", str(clone / "bin" / "hub"), "read", "1"], env={**os.environ, "AGENT_HUB": str(clone)},
                                  stdout=subprocess.PIPE, text=True, check=True).stdout
            self.assertTrue(read.startswith("## 열린 할 일"))
            self.assertIn("문구 정리", read.split("# BOARD")[0])
            self.assertTrue(todo.done("1").ok)
            self.assertIn("없음", todo.list().output)
            self.assertIn("todo done", git(clone, "log", "-1", "--format=%s"))


class FakeHub:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def add(self, assignee, text):
        from app.todo import HubResult

        self.calls.append(("add", assignee, text))
        return HubResult(self.ok, "할 일: 20261009-000000-ionenet-codex" if self.ok else "커밋 실패")

    def list(self):
        from app.todo import HubResult

        self.calls.append(("list",))
        return HubResult(True, "1. [codex] 문구 정리")

    def done(self, key):
        from app.todo import HubResult

        self.calls.append(("done", key))
        return HubResult(True, "닫음: x")


class BotTodoTests(BotFixture):
    def setUp(self):
        super().setUp()
        self.hub = FakeHub()
        self.bot._hub_todo = self.hub

    def test_todo_commands(self):
        self.bot._handle_update(self.message("/todo codex 로그인 문구 정리", update_id=1))
        self.assertEqual(self.hub.calls[-1], ("add", "codex", "로그인 문구 정리"))
        self.assertIn("남겼습니다", self.tg.sent[-1]["text"])
        self.bot._handle_update(self.message("/todo", update_id=2))
        self.assertIn("문구 정리", self.tg.sent[-1]["text"])
        self.bot._handle_update(self.message("/todo done 1", update_id=3))
        self.assertEqual(self.hub.calls[-1], ("done", "1"))

    def test_chat_dispatch_todo_needs_no_approval_but_confirms(self):
        self.bot.chat = CapturingResponder({"type": "dispatch_todo", "assignee": "codex", "todo_text": "로그 정리"})
        self.bot._handle_update(self.message("코덱스한테 로그 정리 남겨줘", update_id=4))
        self.assertEqual(self.hub.calls, [("add", "codex", "로그 정리")])
        texts = [m["text"] for m in self.tg.sent]
        self.assertTrue(any("남겼습니다" in text for text in texts))
        self.assertFalse(any("approve:" in json.dumps(m.get("reply_markup") or {}) for m in self.tg.sent))

    def test_hub_failure_is_reported(self):
        self.hub.ok = False
        self.bot._handle_update(self.message("/todo any 무언가", update_id=5))
        self.assertIn("남기지 못했습니다", self.tg.sent[-1]["text"])


if __name__ == "__main__":
    unittest.main()
