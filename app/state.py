from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from app.ids import canonical_task_id
from app.redact import redact, redact_text


TASK_STATES = {
    "DRAFT",
    "AWAITING_START",
    "QUEUED",
    "STARTING",
    "RUNNING",
    "VERIFYING",
    "READY",
    "BLOCKED",
    "FAILED",
    "INTERRUPTED",
    "STOP_REQUESTED",
    "STOPPING",
    "CANCELLED",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3)}"


class ConflictError(RuntimeError):
    pass


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class StateStore:
    def __init__(self, db_path: Path, schema_path: Path):
        self.db_path = db_path
        self.schema_path = schema_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as connection:
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        if "source_update_id" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN source_update_id INTEGER")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source_update ON tasks(source_update_id)"
            )
        inbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(inbox)")}
        if "attempts" not in inbox_columns:
            connection.execute("ALTER TABLE inbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "last_error" not in inbox_columns:
            connection.execute("ALTER TABLE inbox ADD COLUMN last_error TEXT")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=5,
            isolation_level=None,
            factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def claim_update(self, update_id: int, payload: dict[str, Any]) -> bool:
        safe_payload = json.dumps(redact(payload), ensure_ascii=False)
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO inbox(update_id, received_at, payload_json, state) VALUES (?, ?, ?, 'RECEIVED')",
                (update_id, utc_now(), safe_payload),
            )
            row = connection.execute(
                "SELECT state, attempts FROM inbox WHERE update_id=?", (update_id,)
            ).fetchone()
            if row["state"] in {"PROCESSED", "IGNORED", "DEAD"}:
                return False
            if int(row["attempts"]) >= 3:
                connection.execute(
                    "UPDATE inbox SET state='DEAD', processed_at=? WHERE update_id=?",
                    (utc_now(), update_id),
                )
                return False
            connection.execute(
                "UPDATE inbox SET state='PROCESSING', attempts=attempts+1, payload_json=? WHERE update_id=?",
                (safe_payload, update_id),
            )
            return True

    def finish_update(self, update_id: int, state: str = "PROCESSED", error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE inbox SET state=?, processed_at=?, last_error=? WHERE update_id=?",
                (state, utc_now() if state in {"PROCESSED", "IGNORED", "DEAD"} else None, redact_text(error or "", 500) or None, update_id),
            )

    def requeue_interrupted_updates(self) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE inbox SET state='RECEIVED' WHERE state='PROCESSING'")

    def pending_updates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM inbox WHERE state IN ('RECEIVED','FAILED') ORDER BY update_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def next_update_offset(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(
                    MIN(CASE WHEN state IN ('RECEIVED','PROCESSING','FAILED') THEN update_id END),
                    COALESCE(MAX(update_id), -1) + 1
                ) AS offset FROM inbox
                """
            ).fetchone()
            return int(row["offset"])

    def create_task(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        project_id: str,
        request_text: str,
        specification: dict[str, Any],
        policy_hash: str,
        budget: dict[str, Any],
        source_update_id: int | None = None,
    ) -> dict[str, Any]:
        if source_update_id is not None:
            existing = self.task_for_update(source_update_id)
            if existing:
                return existing
        task_id = new_id("T")
        safe_request = redact_text(request_text, 4_000)
        safe_specification = redact(specification)
        safe_budget = redact(budget)
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, owner_user_id, chat_id, project_id, request_text,
                    specification_json, state, policy_hash, budget_json, source_update_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'AWAITING_START', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    owner_user_id,
                    chat_id,
                    project_id,
                    safe_request,
                    json.dumps(safe_specification, ensure_ascii=False),
                    policy_hash,
                    json.dumps(safe_budget, ensure_ascii=False),
                    source_update_id,
                    now,
                    now,
                ),
            )
        self.add_event(task_id, None, "TASK_CREATED", "policy", {"specification": safe_specification})
        return self.get_task(task_id)

    def task_for_update(self, update_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM tasks WHERE source_update_id=?", (update_id,)).fetchone()
        return self.get_task(row["id"]) if row else None

    def get_task(self, task_id: str) -> dict[str, Any]:
        task_id = canonical_task_id(task_id)
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = dict(row)
        task["specification"] = json.loads(task.pop("specification_json"))
        task["budget"] = json.loads(task.pop("budget_json"))
        return task

    def list_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, project_id, state, version, created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_tasks_in_states(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM tasks WHERE state IN ({placeholders}) ORDER BY created_at",
                states,
            ).fetchall()
        return [self.get_task(row["id"]) for row in rows]

    def active_task_count(self) -> int:
        active = ("QUEUED", "STARTING", "RUNNING", "VERIFYING", "STOP_REQUESTED", "STOPPING")
        placeholders = ",".join("?" for _ in active)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM tasks WHERE state IN ({placeholders})",
                active,
            ).fetchone()
        return int(row["count"])

    def transition(
        self,
        task_id: str,
        *,
        expected_state: str | tuple[str, ...],
        new_state: str,
        expected_version: int | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if new_state not in TASK_STATES:
            raise ValueError(f"Unknown state: {new_state}")
        expected = (expected_state,) if isinstance(expected_state, str) else expected_state
        fields = fields or {}
        allowed_fields = {
            "base_sha",
            "head_sha",
            "branch_name",
            "worktree_path",
            "telegram_message_id",
        }
        if set(fields) - allowed_fields:
            raise ValueError("Unsupported transition field")
        with self.transaction() as connection:
            row = connection.execute("SELECT state, version FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] not in expected:
                raise ConflictError(f"Expected {expected}, got {row['state']}")
            if expected_version is not None and int(row["version"]) != expected_version:
                raise ConflictError("Task version changed")
            assignments = ["state=?", "version=version+1", "updated_at=?"]
            params: list[Any] = [new_state, utc_now()]
            for name, value in fields.items():
                assignments.append(f"{name}=?")
                params.append(value)
            params.append(task_id)
            connection.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?", params)
        self.add_event(task_id, None, "STATE_CHANGED", "state", {"to": new_state})
        return self.get_task(task_id)

    def create_approval(
        self,
        *,
        task_id: str,
        action: str,
        payload: dict[str, Any],
        owner_user_id: int,
        chat_id: int,
        task_version: int,
        ttl_seconds: int,
    ) -> str:
        approval_id = "A-" + secrets.token_urlsafe(12)
        now = datetime.now(UTC)
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals(
                    id, task_id, action, payload_json, payload_hash, owner_user_id, chat_id,
                    task_version, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    task_id,
                    action,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                    payload_hash,
                    owner_user_id,
                    chat_id,
                    task_version,
                    (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
        return approval_id

    def consume_approval(
        self,
        *,
        approval_id: str,
        action: str,
        owner_user_id: int,
        chat_id: int,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, t.version AS current_version, t.state AS task_state
                FROM approvals a JOIN tasks t ON t.id=a.task_id
                WHERE a.id=?
                """,
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ConflictError("Approval not found")
            approval = dict(row)
            approval["payload"] = json.loads(approval.pop("payload_json"))
            if approval["action"] != action:
                raise ConflictError("Approval action mismatch")
            if approval["owner_user_id"] != owner_user_id or approval["chat_id"] != chat_id:
                raise ConflictError("Approval owner mismatch")
            if approval["consumed_at"] is not None:
                raise ConflictError("Approval already used")
            if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(UTC):
                raise ConflictError("Approval expired")
            if int(approval["task_version"]) != int(approval["current_version"]):
                raise ConflictError("Task changed after approval")
            consumed_at = utc_now()
            connection.execute(
                "UPDATE approvals SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (consumed_at, approval_id),
            )
            approval["consumed_at"] = consumed_at
            return approval

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def operation_for_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM operations WHERE approval_id=? ORDER BY created_at DESC LIMIT 1",
                (approval_id,),
            ).fetchone()
        return self.get_operation(row["id"]) if row else None

    def recoverable_consumed_approvals(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.* FROM approvals a
                JOIN tasks t ON t.id=a.task_id
                LEFT JOIN operations o ON o.approval_id=a.id
                WHERE a.consumed_at IS NOT NULL
                  AND o.id IS NULL
                  AND (
                    (a.action='start' AND t.state='AWAITING_START') OR
                    (a.action IN ('merge','push','deploy') AND t.state='READY')
                  )
                ORDER BY a.created_at
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def create_operation(
        self,
        *,
        task_id: str,
        kind: str,
        approval_id: str,
        target: dict[str, Any],
        expected_before: dict[str, Any],
        desired_after: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = new_id("O")
        idempotency_key = hashlib.sha256(
            json.dumps(
                {
                    "task_id": task_id,
                    "kind": kind,
                    "approval_id": approval_id,
                    "desired_after": desired_after,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO operations(
                    id, task_id, kind, idempotency_key, approval_id,
                    target_json, expected_before_json, desired_after_json,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'EXECUTING', ?, ?)
                """,
                (
                    operation_id,
                    task_id,
                    kind,
                    idempotency_key,
                    approval_id,
                    json.dumps(target, ensure_ascii=False),
                    json.dumps(expected_before, ensure_ascii=False),
                    json.dumps(desired_after, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(operation_id)
        item = dict(row)
        for key in (
            "target_json",
            "expected_before_json",
            "desired_after_json",
            "before_evidence_json",
            "after_evidence_json",
        ):
            item[key.removesuffix("_json")] = json.loads(item.pop(key) or "null")
        return item

    def list_operations_in_state(self, state: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM operations WHERE state=? ORDER BY created_at", (state,)
            ).fetchall()
        return [self.get_operation(row["id"]) for row in rows]

    def finish_operation(
        self,
        operation_id: str,
        *,
        state: str,
        before_evidence: dict[str, Any],
        after_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if state not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ValueError("Invalid operation state")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE operations SET state=?, before_evidence_json=?, after_evidence_json=?, updated_at=?
                WHERE id=? AND state='EXECUTING'
                """,
                (
                    state,
                    json.dumps(redact(before_evidence), ensure_ascii=False),
                    json.dumps(redact(after_evidence), ensure_ascii=False),
                    utc_now(),
                    operation_id,
                ),
            )
        return self.get_operation(operation_id)

    def set_operation_before_evidence(
        self, operation_id: str, before_evidence: dict[str, Any]
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE operations SET before_evidence_json=?, updated_at=?
                WHERE id=? AND state='EXECUTING'
                """,
                (json.dumps(redact(before_evidence), ensure_ascii=False), utc_now(), operation_id),
            )
        return self.get_operation(operation_id)

    def create_run(
        self,
        task_id: str,
        *,
        unit_name: str,
        requested_model: str,
        boot_id: str,
    ) -> dict[str, Any]:
        run_id = new_id("R")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt FROM runs WHERE task_id=?",
                (task_id,),
            ).fetchone()
            attempt = int(row["attempt"])
            connection.execute(
                """
                INSERT INTO runs(
                    id, task_id, attempt, unit_name, boot_id, requested_model,
                    state, started_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'STARTING', ?, ?)
                """,
                (run_id, task_id, attempt, unit_name, boot_id, requested_model, utc_now(), utc_now()),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        for key in ("result_json", "usage_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key) or "null")
        return result

    def latest_run(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self.get_run(row["id"]) if row else None

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "unit_name",
            "invocation_id",
            "session_id",
            "actual_model",
            "state",
            "started_at",
            "ended_at",
            "heartbeat_at",
            "exit_code",
            "result_json",
            "usage_json",
        }
        if not fields or set(fields) - allowed:
            raise ValueError("Unsupported run field")
        values: dict[str, Any] = {}
        for name, value in fields.items():
            values[name] = json.dumps(value, ensure_ascii=False) if name.endswith("_json") else value
        assignments = ", ".join(f"{name}=?" for name in values)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE id=?",
                (*values.values(), run_id),
            )

    def add_event(
        self,
        task_id: str | None,
        run_id: str | None,
        event_type: str,
        source: str,
        payload: dict[str, Any],
    ) -> None:
        safe_payload = redact(payload)
        encoded = json.dumps(safe_payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO events(task_id, run_id, created_at, event_type, source, payload_json, evidence_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, run_id, utc_now(), event_type, source, encoded, digest),
            )

    def set_task_fields(self, task_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"base_sha", "head_sha", "branch_name", "worktree_path", "telegram_message_id"}
        if not fields or set(fields) - allowed:
            raise ValueError("Unsupported task field")
        assignments = [f"{name}=?" for name in fields]
        assignments.append("updated_at=?")
        with self.connect() as connection:
            connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?",
                (*fields.values(), utc_now(), task_id),
            )
        return self.get_task(task_id)

    def queue_outbox(
        self,
        *,
        dedupe_key: str,
        chat_id: int,
        method: str,
        payload: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    dedupe_key, chat_id, method, payload_json, state, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (dedupe_key, chat_id, method, json.dumps(redact(payload), ensure_ascii=False), utc_now(), utc_now()),
            )

    def pending_outbox(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox
                WHERE state='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id LIMIT ?
                """,
                (utc_now(), limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def update_outbox(self, item_id: int, *, success: bool, retry_after_seconds: int = 10) -> None:
        with self.connect() as connection:
            if success:
                connection.execute(
                    "UPDATE outbox SET state='SENT', sent_at=?, attempts=attempts+1 WHERE id=?",
                    (utc_now(), item_id),
                )
            else:
                next_attempt = (datetime.now(UTC) + timedelta(seconds=retry_after_seconds)).isoformat(
                    timespec="seconds"
                )
                connection.execute(
                    "UPDATE outbox SET attempts=attempts+1, next_attempt_at=? WHERE id=?",
                    (next_attempt, item_id),
                )

    def recent_events(self, task_id: str, limit: int = 8) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT created_at, event_type, source, payload_json, evidence_hash
                FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def add_chat_message(
        self,
        chat_id: int,
        role: str,
        text: str,
        provider: str | None = None,
        *,
        keep: int = 200,
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported chat role")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO chat_messages(chat_id, role, text, provider, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, role, redact_text(text, 4_000), provider, utc_now()),
            )
            connection.execute(
                """
                DELETE FROM chat_messages WHERE chat_id=? AND id NOT IN (
                    SELECT id FROM chat_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?
                )
                """,
                (chat_id, chat_id, keep),
            )

    def recent_chat_messages(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT role, text, provider, created_at FROM chat_messages
                WHERE chat_id=? ORDER BY id DESC LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_events_by_type(self, event_type: str, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT created_at, event_type, source, payload_json FROM events
                WHERE event_type=? ORDER BY id DESC LIMIT ?
                """,
                (event_type, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result
