import tempfile
import unittest
from pathlib import Path

from app.state import ConflictError, StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        schema = Path(__file__).resolve().parents[1] / "schema.sql"
        self.store = StateStore(root / "chief.sqlite", schema)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_task(self) -> dict:
        return self.store.create_task(
            owner_user_id=100,
            chat_id=100,
            project_id="deskmini",
            request_text="테스트",
            specification={"objective": "test"},
            policy_hash="abc",
            budget={"runtime_seconds": 30},
        )

    def test_approval_is_bound_and_single_use(self) -> None:
        task = self.create_task()
        payload = {"task_id": task["id"], "action": "start", "task_version": task["version"]}
        approval_id = self.store.create_approval(
            task_id=task["id"],
            action="start",
            payload=payload,
            owner_user_id=100,
            chat_id=100,
            task_version=task["version"],
            ttl_seconds=600,
        )
        approval = self.store.consume_approval(
            approval_id=approval_id,
            action="start",
            owner_user_id=100,
            chat_id=100,
        )
        self.assertEqual(approval["payload"], payload)
        with self.assertRaises(ConflictError):
            self.store.consume_approval(
                approval_id=approval_id,
                action="start",
                owner_user_id=100,
                chat_id=100,
            )

    def test_approval_is_invalid_after_task_version_changes(self) -> None:
        task = self.create_task()
        approval_id = self.store.create_approval(
            task_id=task["id"],
            action="start",
            payload={"task_id": task["id"]},
            owner_user_id=100,
            chat_id=100,
            task_version=task["version"],
            ttl_seconds=600,
        )
        self.store.transition(
            task["id"],
            expected_state="AWAITING_START",
            new_state="CANCELLED",
        )
        with self.assertRaises(ConflictError):
            self.store.consume_approval(
                approval_id=approval_id,
                action="start",
                owner_user_id=100,
                chat_id=100,
            )

    def test_update_deduplication(self) -> None:
        self.assertTrue(self.store.claim_update(1, {"update_id": 1, "token": "secret"}))
        self.store.finish_update(1)
        self.assertFalse(self.store.claim_update(1, {"update_id": 1}))
        self.assertEqual(self.store.next_update_offset(), 2)

    def test_interrupted_update_is_replayed(self) -> None:
        payload = {"update_id": 5, "message": {"text": "/status"}}
        self.assertTrue(self.store.claim_update(5, payload))
        self.store.requeue_interrupted_updates()
        self.assertEqual(self.store.pending_updates(), [payload])
        self.assertEqual(self.store.next_update_offset(), 5)

    def test_task_creation_is_idempotent_per_update(self) -> None:
        kwargs = {
            "owner_user_id": 100,
            "chat_id": 100,
            "project_id": "deskmini",
            "request_text": "password=do-not-store",
            "specification": {"objective": "token=do-not-store"},
            "policy_hash": "abc",
            "budget": {},
            "source_update_id": 77,
        }
        first = self.store.create_task(**kwargs)
        second = self.store.create_task(**{**kwargs, "request_text": "different"})
        self.assertEqual(first["id"], second["id"])
        self.assertNotIn("do-not-store", first["request_text"])


if __name__ == "__main__":
    unittest.main()
