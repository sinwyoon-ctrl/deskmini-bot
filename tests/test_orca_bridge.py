import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.orca_bridge import OrcaBridge, OrcaQueue, NativeOrca
from app.state import StateStore
from test_watch import BotFixture

ROOT = Path(__file__).resolve().parents[1]


class FakeNative:
    def __init__(self):
        self.calls = []
        self.tasks = []
        self.workers = []
        self.messages = []
        self.fail_start = False
        self.fail_create_before = False
        self.fail_start_before = False
        self.launches = 0
        self.create_replays = {}
        self.start_replays = {}
        self.fail_release = False
        self.pending_release = False
        self.fail_ack = False

    def call(self, *args):
        self.calls.append(args)
        cmd = args[1]
        if cmd == "task-create":
            if self.fail_create_before:
                raise subprocess.TimeoutExpired("orca", 65)
            replay = args[args.index("--retry-request")+1]
            if replay in self.create_replays:
                return self.create_replays[replay]
            title = args[args.index("--task-title") + 1]
            task = "task_"+str(len(self.tasks)+1)
            self.tasks.append({"id": task, "task_title": title, "run_id": "run_1"})
            result = {"task": {"id": task}}
            self.create_replays[replay] = result
            return result
        if cmd == "worker-start":
            if self.fail_start_before:
                raise subprocess.TimeoutExpired("orca", 65)
            replay = args[args.index("--retry-request")+1]
            if replay in self.start_replays:
                return self.start_replays[replay]
            self.launches += 1
            task = args[args.index("--task")+1]
            dispatch = "ctx_"+str(self.launches)
            self.workers.append({"dispatchId": dispatch, "taskId": task, "runId": "run_1",
                "agentTerminalHandle": "term_"+str(self.launches), "workerState": "working", "dispatchStatus": "running",
                "terminalState": "owned"})
            result = {"state": "ready", "runId": "run_1", "taskId": task, "dispatchId": dispatch}
            self.start_replays[replay] = result
            if self.fail_start:
                raise subprocess.TimeoutExpired("orca", 65)
            return result
        if cmd == "reply":
            return {}
        if cmd == "task-list":
            return {"tasks": self.tasks}
        if cmd == "worker-list":
            return {"workers": self.workers, "page": {"hasMore": False}}
        if cmd == "worker-release":
            if self.fail_release:
                raise RuntimeError("offline")
            if self.pending_release:
                return {"state": "release_pending"}
            next(w for w in self.workers if w["dispatchId"] == args[args.index("--dispatch")+1])["terminalState"] = "released"
            return {"state": "released"}
        if cmd == "check":
            if "--ack" in args:
                if self.fail_ack:
                    raise RuntimeError("offline")
                self.messages = []
            return {"deliveryId": "delivery_1" if self.messages else None, "messages": self.messages}
        raise AssertionError(args)

    def complete(self, outcome="succeeded", index=0):
        worker = self.workers[index]
        worker.update(workerState=outcome, dispatchStatus="completed")
        self.messages = [{"type": "worker_done", "run_id": "run_1", "from_handle": worker["agentTerminalHandle"], "body": "checked files",
            "payload": json.dumps({"taskId": worker["taskId"], "dispatchId": worker["dispatchId"], "outcome": outcome})}]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state/db.sqlite", ROOT / "schema.sql")
        self.config = {"enabled": True, "run_id": "run_1", "coordinator": "term_coord",
            "hub_root": str(self.root / "hub"), "projects": {"trial": {"path": "/trial",
            "worktree": "id:repo::/trial", "scope": "REPORT.md only"}}}
        self.queue = OrcaQueue(self.store, self.config)
        self.native = FakeNative()
        self.bridge = OrcaBridge(self.queue, self.native)

    def submit(self, source="telegram:7", chat_id=12):
        return self.queue.submit(source, "trial", "codex", "read report; $(touch forbidden)", chat_id)

    def test_duplicate_source_returns_same_id_even_after_restart(self):
        row = self.submit()
        again = OrcaQueue(StateStore(self.store.db_path, ROOT / "schema.sql"), self.config)
        self.assertEqual(row["id"], again.submit("telegram:7", "trial", "codex", row["request_text"], 12)["id"])
        with self.assertRaises(ValueError):
            again.submit("telegram:7", "trial", "codex", "changed")

    def test_unregistered_path_agent_and_disabled_rejected(self):
        for project, agent in [("/etc", "codex"), ("trial", "bash")]:
            with self.assertRaises(ValueError):
                self.queue.submit("x", project, agent, "x")
        self.config["enabled"] = False
        with self.assertRaises(ValueError):
            self.submit()

    def test_starts_once_and_never_creates_legacy_task(self):
        row = self.submit()
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "RUNNING")
        self.assertEqual(sum(c[1] == "worker-start" for c in self.native.calls), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        todo = (self.root / "hub/todo" / (row["id"] + ".md")).read_text()
        self.assertIn("assignee: orca", todo)
        self.assertNotIn("assignee: codex", todo)
        self.assertIn("$(touch forbidden)", self.native.calls[0][self.native.calls[0].index("--spec")+1])

    def test_timeout_adopts_native_identity_without_second_launch(self):
        self.submit()
        self.native.fail_start = True
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "UNKNOWN")
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["dispatch_id"], "ctx_1")
        self.assertEqual(sum(c[1] == "worker-start" for c in self.native.calls), 1)

    def test_absent_worker_does_not_retry_or_release(self):
        row = self.submit()
        self.queue.update(row["id"], state="STARTING", run_id="run_1", task_id="task_1")
        self.submit("telegram:8")
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "UNKNOWN")
        self.assertFalse(any(c[1] in {"worker-start", "worker-release"} for c in self.native.calls))

    def test_completion_durable_once_moves_hub_todo_and_releases(self):
        row = self.submit()
        self.bridge.tick()
        self.native.complete()
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "SUCCEEDED")
        self.assertFalse((self.root / "hub/todo" / (row["id"] + ".md")).exists())
        self.assertEqual(sum("SUCCEEDED" in o["dedupe_key"] for o in self.store.pending_outbox()), 1)
        self.assertEqual(sum(c[1] == "worker-release" for c in self.native.calls), 1)

    def test_wrong_dispatch_sender_run_or_native_state_never_settles(self):
        self.submit()
        self.bridge.tick()
        for field, value in [("from_handle", "fake"), ("run_id", "wrong")]:
            self.native.complete()
            self.native.messages[0][field] = value
            self.bridge.tick()
            self.assertNotIn(self.queue.rows()[0]["state"], {"SUCCEEDED", "FAILED"})
        self.native.complete()
        self.native.messages[0]["payload"] = json.dumps({"taskId": "task_1", "dispatchId": "wrong", "outcome": "succeeded"})
        self.bridge.tick()
        self.assertNotIn(self.queue.rows()[0]["state"], {"SUCCEEDED", "FAILED"})
        with self.store.connect() as db:
            self.assertGreater(db.execute("SELECT count(*) FROM orca_anomalies").fetchone()[0], 0)

    def test_question_batch_not_acked_and_not_relaunched(self):
        self.submit()
        self.bridge.tick()
        self.native.messages = [{"id": "msg_q1", "type": "question", "from_handle": "term_1", "run_id": "run_1",
            "payload": {"taskId": "task_1", "dispatchId": "ctx_1"}}]
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "ATTENTION")
        self.assertFalse(any("--ack" in c for c in self.native.calls))

    def test_answered_question_completes_and_next_request_starts(self):
        row = self.submit()
        self.bridge.tick()
        question = {"id": "msg_q1", "type": "question", "from_handle": "dispatch:ctx_1", "run_id": "run_1",
            "body": "Which file?", "payload": {"taskId": "task_1", "dispatchId": "ctx_1"}}
        self.native.messages = [question]
        self.bridge.tick()
        self.queue.answer(row["id"], "telegram:answer1", "REPORT.md")
        self.queue.answer(row["id"], "telegram:answer1", "REPORT.md")
        self.native.complete()
        self.native.messages.insert(0, question)
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "SUCCEEDED")
        self.assertEqual(self.native.messages, [])
        self.assertEqual(sum(c[1] == "reply" for c in self.native.calls), 1)
        self.submit("telegram:next")
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[1]["state"], "RUNNING")
        self.assertEqual(self.native.launches, 2)
        self.assertNotEqual(self.queue.rows()[0]["task_id"], self.queue.rows()[1]["task_id"])
        self.native.complete(index=1)
        self.bridge.tick()
        self.assertEqual([r["state"] for r in self.queue.rows()], ["SUCCEEDED", "SUCCEEDED"])

    def test_old_question_never_regresses_terminal_state(self):
        row = self.submit()
        self.bridge.tick()
        self.native.complete()
        self.bridge.tick()
        self.native.messages = [{"id": "msg_old", "type": "question", "run_id": "run_1", "from_handle": "term_1",
            "payload": {"taskId": "task_1", "dispatchId": "ctx_1"}}]
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "SUCCEEDED")
        self.assertEqual(self.native.messages, [])

    def test_crash_after_release_before_ack_replays_without_duplicate_notification(self):
        self.submit()
        self.bridge.tick()
        self.native.complete()
        self.native.fail_ack = True
        with self.assertRaises(RuntimeError):
            self.bridge.tick()
        self.native.fail_ack = False
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "SUCCEEDED")
        self.assertEqual(sum("SUCCEEDED" in o["dedupe_key"] for o in self.store.pending_outbox()), 1)
        self.assertEqual(sum(c[1] == "worker-release" for c in self.native.calls), 1)
        self.assertEqual(self.native.messages, [])

    def test_failed_completion_is_not_success(self):
        self.submit()
        self.bridge.tick()
        self.native.complete("failed")
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "FAILED")

    def test_hub_request_does_not_send_telegram(self):
        self.submit("hub:test", None)
        self.bridge.tick()
        self.native.complete()
        self.bridge.tick()
        self.assertEqual(self.store.pending_outbox(), [])

    def test_release_failure_preserves_unacked_delivery(self):
        self.submit()
        self.bridge.tick()
        self.native.complete()
        self.native.fail_release = True
        with self.assertRaises(RuntimeError):
            self.bridge.tick()
        self.assertNotEqual(self.queue.rows()[0]["state"], "SUCCEEDED")
        self.assertFalse(any("--ack" in c for c in self.native.calls))

    def test_malformed_payloads_do_not_block_valid_completion(self):
        row = self.submit()
        self.bridge.tick()
        self.native.complete()
        self.native.messages[:0] = [{"id": "bad_1", "payload": "not-json"},
            {"id": "bad_2", "payload": "[1,2]"}]
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "SUCCEEDED")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM orca_anomalies").fetchone()[0], 2)
        self.bridge.tick()
        self.assertEqual(sum("SUCCEEDED" in o["dedupe_key"] for o in self.store.pending_outbox()), 1)
        self.assertTrue((self.root / "hub/orca/ATTENTION.md").exists())

    def test_recovery_before_task_create_replays_persisted_scope(self):
        row = self.submit()
        self.native.fail_create_before = True
        self.bridge.tick()
        original = next(c for c in self.native.calls if c[1] == "task-create")
        self.native.fail_create_before = False
        self.config["projects"]["trial"]["scope"] = "CHANGED CONFIG MUST NOT ALTER REPLAY"
        self.queue.recover(row["id"])
        self.bridge.tick()
        self.assertEqual(len(self.native.tasks), 1)
        self.assertEqual(self.native.launches, 1)
        self.assertEqual([c for c in self.native.calls if c[1] == "task-create"][-1], original)
        self.assertEqual(self.queue.rows()[0]["state"], "RUNNING")

    def test_recovery_after_task_before_start_keeps_task_and_retry_id(self):
        row = self.submit()
        self.native.fail_start_before = True
        self.bridge.tick()
        original = next(c for c in self.native.calls if c[1] == "worker-start")
        self.native.fail_start_before = False
        self.queue.recover(row["id"])
        self.bridge.tick()
        self.assertEqual(len(self.native.tasks), 1)
        self.assertEqual(self.native.launches, 1)
        self.assertEqual([c for c in self.native.calls if c[1] == "worker-start"][-1], original)

    def test_recovery_lost_start_response_never_creates_second_worker(self):
        row = self.submit()
        self.native.fail_start = True
        self.bridge.tick()
        self.queue.recover(row["id"])
        self.queue.recover(row["id"])
        self.bridge.tick()
        self.assertEqual(self.native.launches, 1)
        self.assertEqual(len(self.native.tasks), 1)
        self.assertEqual(self.queue.rows()[0]["state"], "RUNNING")

    def test_completion_arriving_after_running_snapshot_is_replayed(self):
        self.submit()
        self.bridge.tick()
        self.native.complete()
        self.native.workers[0].update(workerState="working", dispatchStatus="running")
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "RUNNING")
        self.assertTrue(self.native.messages)
        self.native.workers[0].update(workerState="succeeded", dispatchStatus="completed")
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "SUCCEEDED")
        self.assertEqual(sum(c[1] == "worker-release" for c in self.native.calls), 1)
        self.assertEqual(sum("SUCCEEDED" in o["dedupe_key"] for o in self.store.pending_outbox()), 1)

    def test_pending_release_waits_before_ack_and_keeps_replay_id(self):
        self.submit()
        self.bridge.tick()
        self.native.complete()
        self.native.pending_release = True
        self.bridge.tick()
        self.assertEqual(self.queue.rows()[0]["state"], "RUNNING")
        self.assertFalse(any("--ack" in c for c in self.native.calls))
        self.native.pending_release = False
        self.bridge.tick()
        releases = [c for c in self.native.calls if c[1] == "worker-release"]
        self.assertEqual(releases[0], releases[1])
        self.assertEqual(self.queue.rows()[0]["state"], "SUCCEEDED")

    def test_native_runtime_mismatch_refuses_response(self):
        config = {"cli": "/fixed/orca", "cwd": "/tmp", "runtime_id": "expected"}
        with patch("app.orca_bridge.subprocess.run") as run:
            run.return_value.stdout = json.dumps({"ok": True, "_meta": {"runtimeId": "wrong"}, "result": {}})
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                NativeOrca(config).call("status")
            self.assertIsInstance(run.call_args.args[0], list)


class TelegramBridgeTests(BotFixture):
    def test_orca_command_queues_with_update_dedup_and_status_without_llm(self):
        self.bot._handle_update(self.message("/orca new trial codex read report", 9001))
        self.bot._orca_command("/orca new trial codex read report", 9001)
        queue = OrcaQueue.load(self.bot.store, self.settings.root)
        self.assertEqual(len(queue.rows()), 1)
        self.assertEqual(queue.rows()[0]["source_key"], "telegram:9001")
        self.bot._orca_command("/orca status", 9002)


if __name__ == "__main__":
    unittest.main()
