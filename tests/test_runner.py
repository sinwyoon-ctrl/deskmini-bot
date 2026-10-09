import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.collector import CommandResult
from app.config import Settings
from app.runner import RunnerError, TaskRunner, UnitStatus, _read_boot_id
from app.state import StateStore


class RunnerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        app_root = Path(__file__).resolve().parents[1]
        self.settings = Settings(
            root=app_root,
            state_dir=root,
            db_path=root / "chief.sqlite",
            telegram_token="test",
            allowed_chat_id=1,
            allowed_user_id=1,
            telegram_timeout_seconds=1,
            gemini_script=root / "missing.py",
            max_runtime_seconds=60,
            max_active_tasks=1,
            approval_ttl_seconds=60,
            projects={},
        )
        for child in ("tasks", "evidence", "logs", "backups", "worktrees"):
            (root / child).mkdir(exist_ok=True)
        self.store = StateStore(self.settings.db_path, app_root / "schema.sql")
        self.runner = TaskRunner(self.settings, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def task(self) -> dict:
        return self.store.create_task(
            owner_user_id=1,
            chat_id=1,
            project_id="deskmini",
            request_text="test",
            specification={"objective": "test"},
            policy_hash="test",
            budget={},
        )

    def test_pending_stop_is_completed_after_restart(self) -> None:
        task = self.task()
        self.store.transition(
            task["id"], expected_state="AWAITING_START", new_state="STOP_REQUESTED"
        )
        self.assertEqual(self.runner.reconcile(task["id"]), "CANCELLED")

    def test_result_must_match_task_and_run_identity(self) -> None:
        task = self.task()
        self.store.transition(task["id"], expected_state="AWAITING_START", new_state="STARTING")
        run = self.store.create_run(
            task["id"],
            unit_name="deskmini-chief-" + task["id"].lower(),
            requested_model="sonnet",
            boot_id=_read_boot_id(),
        )
        self.store.transition(task["id"], expected_state="STARTING", new_state="RUNNING")
        task_dir = self.settings.state_dir / "tasks" / task["id"]
        task_dir.mkdir()
        (task_dir / "result.json").write_text(
            json.dumps({"task_id": task["id"], "run_id": "R-wrong", "success": True}),
            encoding="utf-8",
        )
        self.runner.unit_status = lambda _: UnitStatus(
            found=False,
            active_state="not-found",
            sub_state="dead",
            result="unknown",
            exit_code=None,
            invocation_id=None,
        )
        self.assertEqual(self.runner.reconcile(task["id"]), "FAILED")
        stored = self.store.latest_run(task["id"])
        self.assertEqual(stored["result"]["classification"], "INVALID_RESULT_IDENTITY")

    def test_stop_recovery_does_not_treat_systemd_failure_as_cancelled(self) -> None:
        task = self.task()
        self.store.transition(task["id"], expected_state="AWAITING_START", new_state="STARTING")
        self.store.create_run(
            task["id"],
            unit_name="deskmini-chief-" + task["id"].lower(),
            requested_model="sonnet",
            boot_id=_read_boot_id(),
        )
        self.store.transition(task["id"], expected_state="STARTING", new_state="STOP_REQUESTED")
        self.runner.unit_status = lambda _: UnitStatus(
            found=False,
            active_state="unknown",
            sub_state="unknown",
            result="unknown",
            exit_code=None,
            invocation_id=None,
            query_ok=False,
        )
        failure = CommandResult(ok=False, stdout="dbus unavailable", returncode=1, elapsed_ms=1)
        with patch("app.runner.run_fixed", return_value=failure):
            self.assertEqual(self.runner.reconcile(task["id"]), "INTERRUPTED")

    def test_direct_stop_does_not_treat_status_query_failure_as_cancelled(self) -> None:
        task = self.task()
        self.store.transition(task["id"], expected_state="AWAITING_START", new_state="STARTING")
        self.store.create_run(
            task["id"],
            unit_name="deskmini-chief-" + task["id"].lower(),
            requested_model="sonnet",
            boot_id=_read_boot_id(),
        )
        self.store.transition(task["id"], expected_state="STARTING", new_state="RUNNING")
        self.runner.unit_status = lambda _: UnitStatus(
            found=False,
            active_state="inactive",
            sub_state="dead",
            result="unknown",
            exit_code=None,
            invocation_id=None,
            query_ok=False,
        )
        stopped = CommandResult(ok=True, stdout="", returncode=0, elapsed_ms=1)
        with patch("app.runner.run_fixed", return_value=stopped):
            with self.assertRaises(RunnerError):
                self.runner.stop(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["state"], "INTERRUPTED")


if __name__ == "__main__":
    unittest.main()
