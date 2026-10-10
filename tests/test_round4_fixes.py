"""4라운드 검증(450) 후속 수정: 거절된 승인 부활 방지, 한도 해제 후 즉시 재시도, 콜백 응답 실패, 거절 안내,
프로젝트 지정, hub 할 일 문구, 첨부 재생 중복, 예약 승인 유실, 검증 재시도 한도, 검토자 사슬, 토큰 마스킹."""
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adapters.gemini_rest import InterpretationUnavailable, ReviewResult
from adapters.telegram import TelegramError
from app.policy import Policy
from app.redact import contains_secret, redact_text
from app.retry import parse_reset_time, reset_pending
from app.runner import VERIFY_RETRY_LIMIT, RunnerError
from app.todo import HubTodo
from app.verify import FallbackReviewer, build_reviewer, review_settings, run_review

from test_round4 import PNG, FakeRunner, git  # noqa: F401
from test_watch import BotFixture, kst


ROOT = Path(__file__).resolve().parents[1]


class BusyRunner(FakeRunner):
    """다른 작업이 진행 중이라 거절하는 실행기(재시도·착수 모두)."""

    def __init__(self):
        super().__init__()
        self.busy = True
        self.started = []

    def retry(self, task_id, expected_version=None):
        if self.busy:
            raise RunnerError("Another task is active")
        return super().retry(task_id, expected_version)

    def start(self, task_id):
        if self.busy:
            raise RunnerError("Another task is active")
        self.started.append(task_id)
        return {"id": task_id, "state": "RUNNING"}

    def verify_retry_budget(self, task):
        return (0, VERIFY_RETRY_LIMIT)


class ApprovalFixture(BotFixture):
    def setUp(self):
        super().setUp()
        self.runner = BusyRunner()
        self.bot.runner = self.runner
        self.bot._task_text = lambda task_id: f"task {task_id}"
        self.bot._update_task_card = lambda *args, **kwargs: None

    def failed_task(self, error="boom", state="FAILED", ended_at=None):
        task = self.bot.store.create_task(owner_user_id=100, chat_id=100, project_id="deskmini", request_text="x",
                                          specification={}, policy_hash="h", budget={"retries": 1})
        self.bot.store.transition(task["id"], expected_state="AWAITING_START", new_state="STARTING")
        run = self.bot.store.create_run(task["id"], unit_name="u", requested_model="m", boot_id="b")
        classification = "LIMIT_BLOCKED" if state == "BLOCKED" else "EXECUTION_FAILED"
        fields = {"state": state, "result_json": {"classification": classification, "error": error}}
        if ended_at is not None:
            fields["ended_at"] = ended_at.isoformat(timespec="seconds")
        self.bot.store.update_run(run["id"], **fields)
        self.bot.store.transition(task["id"], expected_state="STARTING", new_state=state)
        return task["id"]

    def new_task(self):
        self.bot._handle_update(self.message("/new 메모리 API 추가", update_id=self.next_update()))
        return self.bot.store.list_tasks(1)[0]["id"]

    _update = 100

    def next_update(self):
        ApprovalFixture._update += 1
        return ApprovalFixture._update

    def all_buttons(self, prefix):
        return [b["callback_data"] for m in self.tg.sent for row in (m.get("reply_markup") or {}).get("inline_keyboard", [])
                for b in row if b["callback_data"].startswith(prefix)]

    def texts(self, since=0):
        return "\n".join(m["text"] or "" for m in self.tg.sent[since:])

    def approval(self, approval_id):
        return self.bot.store.get_approval(approval_id)

    def restart(self):
        self.bot._recover_state()


class RefusedApprovalNotResurrectedTests(ApprovalFixture):
    def test_refused_retry_records_outcome_and_restart_does_not_run_it(self):
        task_id = self.failed_task()
        self.bot._offer_retry(task_id)
        data = self.all_buttons("approve:retry:")[-1]
        n = len(self.tg.sent)
        self.press(data, update_id=self.next_update())
        text = self.texts(n)
        self.assertIn("다시 시도하지 못했습니다", text)
        self.assertIn("진행 중", text)
        self.assertNotIn("RunnerError", text)
        self.assertEqual(self.approval(data.split(":", 2)[2])["outcome"], "REFUSED")
        self.runner.busy = False  # 다른 작업이 끝난 뒤 재시작
        self.assertEqual(self.bot.store.recoverable_consumed_approvals(), [])
        self.restart()
        self.assertEqual(self.runner.retried, [])

    def test_refused_start_offers_new_button_and_restart_does_not_start(self):
        task_id = self.new_task()
        data = self.all_buttons("approve:start:")[-1]
        n = len(self.tg.sent)
        self.press(data, update_id=self.next_update())
        text = self.texts(n)
        self.assertIn("다른 작업(", text)
        self.assertIn("진행 중이라 착수하지 못했습니다", text)
        self.assertEqual(self.approval(data.split(":", 2)[2])["outcome"], "REFUSED")
        fresh = self.all_buttons("approve:start:")[-1]
        self.assertNotEqual(fresh, data)
        self.runner.busy = False
        self.restart()
        self.assertEqual(self.runner.started, [])
        self.press(fresh, update_id=self.next_update())  # 새 버튼은 정상 착수
        self.assertEqual(self.runner.started, [task_id])
        self.assertEqual(self.approval(fresh.split(":", 2)[2])["outcome"], "EXECUTED")

    def test_crash_window_within_ttl_is_recovered_once(self):
        self.runner.busy = False
        task_id = self.failed_task()
        self.bot._offer_retry(task_id)
        approval_id = self.all_buttons("approve:retry:")[-1].split(":", 2)[2]
        self.bot.store.consume_approval(approval_id=approval_id, action="retry", owner_user_id=100, chat_id=100)
        self.restart()
        self.restart()
        self.assertEqual(len(self.runner.retried), 1)
        self.assertEqual(self.approval(approval_id)["outcome"], "EXECUTED")

    def test_crash_window_after_ttl_is_abandoned_with_one_notice(self):
        self.runner.busy = False
        task_id = self.failed_task()
        self.bot._offer_retry(task_id)
        approval_id = self.all_buttons("approve:retry:")[-1].split(":", 2)[2]
        self.bot.store.consume_approval(approval_id=approval_id, action="retry", owner_user_id=100, chat_id=100)
        with self.bot.store.connect() as connection:
            connection.execute("UPDATE approvals SET expires_at=? WHERE id=?",
                               ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds"), approval_id))
        n = len(self.tg.sent)
        self.restart()
        self.restart()
        self.assertEqual(self.runner.retried, [])
        self.assertEqual(self.approval(approval_id)["outcome"], "ABANDONED")
        notices = [m for m in self.tg.sent[n:] if "재시작 전에 처리되지 못한 승인은 폐기했습니다" in (m["text"] or "")]
        self.assertEqual(len(notices), 1)
        self.assertIn(task_id, notices[0]["text"])

    def test_consumed_schedule_approval_without_schedule_is_told_not_lost_silently(self):
        self.now = kst(12)
        task_id = self.failed_task("You've hit your limit · resets 3pm (Asia/Seoul)", state="BLOCKED", ended_at=kst(11, 50))
        self.bot._offer_retry(task_id)
        approval_id = self.all_buttons("approve:schedule_retry:")[-1].split(":", 2)[2]
        self.bot.store.consume_approval(approval_id=approval_id, action="schedule_retry", owner_user_id=100, chat_id=100)
        n = len(self.tg.sent)
        self.restart()
        self.assertIn("재시작 전에 처리되지 못한 승인은 폐기했습니다", self.texts(n))
        self.assertEqual(self.approval(approval_id)["outcome"], "ABANDONED")
        self.assertIsNone(self.bot.store.pending_retry_for_task(task_id))
        self.now = kst(16)
        self.bot._run_scheduled_retries()
        self.assertEqual(self.runner.retried, [])


class RefusalMessageTests(ApprovalFixture):
    def inbox_state(self, update_id):
        with self.bot.store.connect() as connection:
            return connection.execute("SELECT state FROM inbox WHERE update_id=?", (update_id,)).fetchone()["state"]

    def test_expired_approval_clear_message_once_despite_redelivery(self):
        self.new_task()
        data = self.all_buttons("approve:start:")[-1]
        with self.bot.store.connect() as connection:
            connection.execute("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (data.split(":", 2)[2],))
        n = len(self.tg.sent)
        update_id = self.next_update()
        for _ in range(3):
            self.press(data, update_id=update_id)
        self.assertEqual(len(self.tg.sent) - n, 1)
        self.assertIn("승인 유효시간이 지났습니다. 상태 카드에서 다시 요청해 주세요", self.tg.sent[-1]["text"])
        self.assertEqual(self.inbox_state(update_id), "PROCESSED")
        self.assertEqual(self.runner.started, [])

    def test_double_tap_says_already_handled(self):
        self.runner.busy = False
        self.new_task()
        data = self.all_buttons("approve:start:")[-1]
        self.press(data, update_id=self.next_update())
        self.press(data, update_id=self.next_update())
        self.assertIn("이미 처리된 버튼", self.tg.sent[-1]["text"])
        self.assertNotIn("ConflictError", self.tg.sent[-1]["text"])

    def test_unexpected_exception_keeps_generic_error_and_failed_state(self):
        def boom(*args, **kwargs):
            raise ValueError("unexpected")

        self.bot._tasks_text = boom
        update_id = self.next_update()
        self.bot._handle_update(self.message("/tasks", update_id=update_id))
        self.assertIn("요청 처리 실패", self.tg.sent[-1]["text"])
        self.assertEqual(self.inbox_state(update_id), "FAILED")


class AnswerCallbackBestEffortTests(ApprovalFixture):
    def test_press_processed_when_answer_callback_fails(self):
        self.runner.busy = False
        task_id = self.failed_task()
        self.bot._offer_retry(task_id)
        data = self.all_buttons("approve:retry:")[-1]

        def too_old(callback_id, text=""):
            raise TelegramError("Bad Request: query is too old", error_code=400)

        self.tg.answer_callback = too_old
        n = len(self.tg.sent)
        self.press(data, update_id=self.next_update())
        self.assertEqual(len(self.runner.retried), 1)
        self.assertNotIn("요청 처리 실패", self.texts(n))


class ResetPassedRetryTests(ApprovalFixture):
    def setUp(self):
        super().setUp()
        self.runner.busy = False

    def offered(self):
        return self.all_buttons("approve:")[-1].split(":")[1]

    def test_reset_already_passed_offers_immediate_retry(self):
        self.now = kst(10)
        task_id = self.failed_task("You've hit your limit · resets 9am (Asia/Seoul)", state="BLOCKED", ended_at=kst(8, 50))
        self.bot._offer_retry(task_id)
        self.assertEqual(self.offered(), "retry")
        self.assertIn("이미 지났습니다", self.tg.sent[-1]["text"])

    def test_reset_in_future_still_schedules(self):
        self.now = kst(8, 55)
        task_id = self.failed_task("resets 9am (Asia/Seoul)", state="BLOCKED", ended_at=kst(8, 50))
        self.bot._offer_retry(task_id)
        self.assertEqual(self.offered(), "schedule_retry")

    def test_reset_within_a_minute_is_immediate(self):
        self.now = kst(8, 59) + timedelta(seconds=30)
        task_id = self.failed_task("resets 9am (Asia/Seoul)", state="BLOCKED", ended_at=kst(8, 50))
        self.bot._offer_retry(task_id)
        self.assertEqual(self.offered(), "retry")

    def test_date_rollover_in_seoul(self):
        # 23:50 에 받은 "resets 1am" 은 다음 날 01:00 이다. 00:30 엔 예약, 01:30 엔 바로 다시 시도.
        ended = kst(23, 50)
        self.now = ended + timedelta(minutes=40)
        task_id = self.failed_task("resets 1am (Asia/Seoul)", state="BLOCKED", ended_at=ended)
        self.bot._offer_retry(task_id)
        self.assertEqual(self.offered(), "schedule_retry")
        self.assertIn("01:05", [b["text"] for m in self.tg.sent[-1:] for row in m["reply_markup"]["inline_keyboard"] for b in row][0])
        self.now = ended + timedelta(minutes=100)
        self.bot._offer_retry(task_id)
        self.assertEqual(self.offered(), "retry")

    def test_second_retry_after_proposed_time_does_not_roll_to_next_day(self):
        self.now = kst(10)
        task_id = self.failed_task("resets 3pm (Asia/Seoul)", state="BLOCKED")  # 종료 시각 기록 없음(지금 기준)
        with self.bot.store.connect() as connection:
            connection.execute("UPDATE runs SET ended_at=NULL WHERE task_id=?", (task_id,))
        self.bot._offer_retry(task_id)
        self.assertEqual(self.offered(), "schedule_retry")
        self.now = kst(15, 1)
        self.bot._offer_retry(task_id)
        self.assertEqual(self.offered(), "retry")

    def test_parse_relative_to_message_time_and_pending_window(self):
        reset = parse_reset_time("resets 9am (Asia/Seoul)", kst(8, 50))
        self.assertEqual(reset, kst(9))
        self.assertFalse(reset_pending(reset, kst(10)))
        self.assertFalse(reset_pending(reset, kst(8, 59) + timedelta(seconds=30)))
        self.assertTrue(reset_pending(reset, kst(8, 58)))
        # 지금(10:00) 기준으로 읽으면 다음 날 9시가 된다 — 그래서 봇은 실행 종료 시각을 기준으로 넘긴다.
        self.assertEqual(parse_reset_time("resets 9am (Asia/Seoul)", kst(10)), kst(9) + timedelta(days=1))


class VerifyRetryCapTests(unittest.TestCase):
    def test_verify_only_retry_has_its_own_cap(self):
        from test_round4 import RunnerRetryTests

        case = RunnerRetryTests("setUp")
        case.setUp()
        try:
            task = case.store.create_task(owner_user_id=1, chat_id=1, project_id="sandbox", request_text="x",
                                          specification={"objective": "x"}, policy_hash="h", budget={"retries": 1})
            case.runner.start(task["id"])
            run = case.store.latest_run(task["id"])
            case.store.update_run(run["id"], state="COMPLETED", result_json={"classification": "SUCCESS"})
            case.store.transition(task["id"], expected_state="RUNNING", new_state="BLOCKED")
            for attempt in range(VERIFY_RETRY_LIMIT):
                current = case.store.get_task(task["id"])
                self.assertEqual(case.runner.retry_mode(current), "verify")
                self.assertTrue(case.runner.can_retry(current)[0], attempt)
                case.runner.retry(task["id"])
                case.store.transition(task["id"], expected_state="VERIFYING", new_state="BLOCKED")
            current = case.store.get_task(task["id"])
            allowed, why = case.runner.can_retry(current)
            self.assertFalse(allowed)
            self.assertIn("검증 재시도 한도", why)
            with self.assertRaises(RunnerError):
                case.runner.retry(task["id"])
            self.assertEqual(case.runner.verify_retry_budget(current), (VERIFY_RETRY_LIMIT, VERIFY_RETRY_LIMIT))
        finally:
            case.tearDown()

    def test_verify_card_shows_verify_budget(self):
        fixture = ApprovalFixture("setUp")
        fixture.setUp()
        try:
            fixture.runner.busy = False
            fixture.runner.mode = "verify"
            task_id = fixture.failed_task()
            fixture.bot._offer_retry(task_id)
            self.assertIn(f"검증 재시도 0/{VERIFY_RETRY_LIMIT}회", fixture.tg.sent[-1]["text"])
        finally:
            fixture.tearDown()


class ProjectSelectorTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy(interpreter=None, project_ids=["deskmini", "sandbox"])

    def test_unknown_selector_is_rejected_with_list(self):
        for text in ("/new unknownproj: 기능 추가", "/new other：무언가", "개발: blog-site: 글 추가"):
            decision = self.policy.decide_fixed(text)
            self.assertEqual(decision.intent, "unknown", text)
            self.assertIn("등록되지 않은 프로젝트입니다", decision.message)
            self.assertIn("가능: deskmini, sandbox", decision.message)

    def test_registered_project_is_case_insensitive(self):
        for text in ("/new Sandbox: 테스트 추가", "/new SANDBOX 테스트 추가", "/new sandbox: 테스트 추가"):
            decision = self.policy.decide_fixed(text)
            self.assertEqual((decision.intent, decision.project_id), ("create_task", "sandbox"), text)
            self.assertEqual(decision.specification["objective"], "테스트 추가")

    def test_ordinary_colon_text_is_not_a_selector(self):
        for text in ("/new README: 실행 방법 추가", "/new fix: 로그인 버그", "/new 메모: 정리", "/new docs: 사용법",
                     "/new 시간 10:30 에 알림", "/new readme 오타: 고쳐"):
            decision = self.policy.decide_fixed(text)
            self.assertEqual((decision.intent, decision.project_id), ("create_task", "deskmini"), text)
            self.assertIn(text.split(" ", 1)[1], decision.specification["objective"])


class HubTodoDashTextTests(unittest.TestCase):
    HUB = Path.home() / "agent-hub" / "bin" / "hub"

    @unittest.skipUnless(HUB.is_file() and shutil.which("git"), "agent-hub 없음")
    def test_dash_options_are_kept_as_text(self):
        with tempfile.TemporaryDirectory() as temp:
            hub = Path(temp) / "hub"
            (hub / "bin").mkdir(parents=True)
            shutil.copy(self.HUB, hub / "bin" / "hub")
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=hub, check=True, env=env)
            git(hub, "config", "user.name", "T")
            git(hub, "config", "user.email", "t@invalid.local")
            (hub / "BOARD.md").write_text("# board\n")
            git(hub, "add", "-A")
            git(hub, "commit", "-qm", "init")
            todo = HubTodo(hub)
            for body in ("-n", "-e", "-E \\t 탭"):
                self.assertTrue(todo.add("codex", body).ok, body)
            bodies = sorted(path.read_text().split("\n\n", 1)[1].rstrip("\n") for path in (hub / "todo").glob("*.md"))
            self.assertEqual(bodies, sorted(["-n", "-e", "-E \\t 탭"]))


class AttachmentReplayTests(BotFixture):
    def setUp(self):
        super().setUp()
        from test_round4 import AttachmentTelegram

        self.bot.telegram = AttachmentTelegram(self.tg, {"doc1": "a,b\n1,2\n".encode()})

    def test_replayed_update_does_not_store_twice(self):
        message = {"chat": {"type": "private", "id": 100}, "from": {"id": 100},
                   "document": {"file_id": "doc1", "file_unique_id": "uniq1", "file_name": "a.csv", "file_size": 8}}
        self.bot._handle_message(message, 77)  # 처리 중 크래시 → 재시작 재생
        self.bot._handle_message(message, 77)
        rows = self.bot.store.attachments_for_update(77)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_unique_id"], "uniq1")
        self.assertEqual(len(list((self.settings.state_dir / "inbox").rglob("*.csv"))), 1)
        self.assertEqual(self.bot.telegram.downloads, ["documents/doc1"])

    def test_different_update_same_file_is_a_new_attachment(self):
        message = {"chat": {"type": "private", "id": 100}, "from": {"id": 100},
                   "document": {"file_id": "doc1", "file_name": "a.csv", "file_size": 8}}
        self.bot._handle_message(message, 78)
        self.bot._handle_message(message, 79)
        self.assertEqual(len(self.bot.store.attachments_for_update(78)), 1)
        self.assertEqual(len(self.bot.store.attachments_for_update(79)), 1)


class FakeReviewer:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    def review_diff(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class ReviewFallbackTests(unittest.TestCase):
    PASS = ReviewResult(verdict="PASS", findings=[], summary="ok")
    FAIL = ReviewResult(verdict="FAIL", findings=["x"], summary="no")

    def test_falls_back_when_first_unavailable(self):
        gemini = FakeReviewer("gemini", error=InterpretationUnavailable("429 quota"))
        claude = FakeReviewer("claude_cli", result=self.PASS)
        result, used, errors = run_review(FallbackReviewer([("gemini", gemini), ("claude_cli", claude)]), diff_text="d")
        self.assertEqual((result.verdict, used), ("PASS", "claude_cli"))
        self.assertIn("gemini: 429 quota", errors[0])

    def test_first_verdict_wins_even_if_fail(self):
        gemini = FakeReviewer("gemini", result=self.FAIL)
        claude = FakeReviewer("claude_cli", result=self.PASS)
        result, used, errors = run_review(FallbackReviewer([("gemini", gemini), ("claude_cli", claude)]))
        self.assertEqual((result.verdict, used, errors, claude.calls), ("FAIL", "gemini", [], 0))

    def test_all_unavailable_raises_unavailable(self):
        chain = FallbackReviewer([("gemini", FakeReviewer("gemini", error=InterpretationUnavailable("quota"))),
                                  ("claude_cli", FakeReviewer("claude_cli", error=OSError("no cli")))])
        with self.assertRaises(InterpretationUnavailable) as caught:
            chain.review_diff(diff_text="d")
        self.assertIn("claude_cli: no cli", str(caught.exception))

    def test_single_reviewer_reports_its_name(self):
        result, used, errors = run_review(FakeReviewer("claude_cli", result=self.PASS))
        self.assertEqual((used, errors), ("claude_cli", []))

    def test_config_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "models.toml"
            path.write_text("[review]\nmodel = \"sonnet\"\n")
            self.assertEqual(review_settings(path)["providers"], ["gemini", "claude_cli"])  # 기본 사슬
            path.write_text('[review]\nproviders = ["claude_cli", "rm -rf", "gemini", "gemini"]\n')
            self.assertEqual(review_settings(path)["providers"], ["claude_cli", "gemini"])
            path.write_text('[review]\nproviders = ["nope"]\n')
            self.assertEqual(review_settings(path)["providers"], ["gemini"])
            path.write_text('[review]\nprovider = "claude_cli"\n')  # 예전 단일 설정
            self.assertEqual(review_settings(path)["providers"], ["claude_cli"])
            sentinel = object()
            path.write_text('[review]\nproviders = ["gemini", "claude_cli"]\n')
            chain = build_reviewer(path, sentinel, Path(temp))
            self.assertIsInstance(chain, FallbackReviewer)
            self.assertEqual([name for name, _ in chain.reviewers], ["gemini", "claude_cli"])
            self.assertIs(chain.reviewers[0][1], sentinel)


class ExtraRedactionTests(unittest.TestCase):
    def test_aws_and_slack_tokens_are_masked(self):
        for secret in ("AKIAIOSFODNN7EXAMPLE", "ASIAY34FZKBOKMUTVV7A", "AKIAFAKEAWS0123456789",
                       "xox" "b-1111111111-FAKESLACKTOKENabc", "xox" "p-123456789012-abcdefABCDEF", "xox" "a-2-abcdefghijkl"):
            masked = redact_text(f"key {secret} end")
            self.assertNotIn(secret, masked)
            self.assertIn("<redacted>", masked)

    def test_ordinary_words_are_not_masked(self):
        for text in ("AKIA 는 AWS 키 접두사", "xox-abc", "xoxz-1234567890abc", "ASIAN food", "akiaiosfodnn7example"):
            self.assertFalse(contains_secret(text), text)


if __name__ == "__main__":
    unittest.main()
