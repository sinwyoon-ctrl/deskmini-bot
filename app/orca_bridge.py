"""Durable Telegram/hub queue for supervised Orca work; no shell or fallback runner."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.redact import redact_text
from app.state import StateStore, utc_now

TERMINAL = {"SUCCEEDED", "FAILED"}
ACTIVE = {"CREATING", "STARTING", "RUNNING", "UNKNOWN", "ATTENTION"}
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-orca-[a-f0-9]{12}$")


class OrcaQueue:
    def __init__(self, store: StateStore, config: dict):
        self.store, self.config = store, config

    @classmethod
    def load(cls, store, root):
        path = root / "config/orca.toml"
        return cls(store, tomllib.loads(path.read_text()) if path.is_file() else {})

    def submit(self, source_key, project, agent, text, chat_id=None):
        if not self.config.get("enabled"):
            raise ValueError("Orca 연결이 꺼져 있습니다")
        if project not in self.config.get("projects", {}) or agent not in {"codex", "claude"}:
            raise ValueError("등록된 프로젝트와 codex·claude만 지정할 수 있습니다")
        text = redact_text(text.strip(), 3000)
        if not text or len(source_key) > 160:
            raise ValueError("요청 내용 또는 출처 ID를 확인해 주세요")
        digest = hashlib.sha256(source_key.encode()).hexdigest()[:12]
        key = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-orca-" + digest
        with self.store.transaction() as db:
            db.execute("""INSERT OR IGNORE INTO orca_requests
                (id,source_key,project,agent,request_text,chat_id,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?, 'QUEUED',?,?)""",
                (key, source_key, project, agent, text, chat_id, utc_now(), utc_now()))
            row = dict(db.execute("SELECT * FROM orca_requests WHERE source_key=?", (source_key,)).fetchone())
            if (row["project"], row["agent"], row["request_text"]) != (project, agent, text):
                raise ValueError("같은 출처 ID에 다른 요청을 등록할 수 없습니다")
        return row

    def rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM orca_requests ORDER BY rowid")]

    def update(self, key, **fields):
        if set(fields) - {"state", "run_id", "task_id", "dispatch_id", "summary", "error"}:
            raise ValueError("unsupported fields")
        fields["updated_at"] = utc_now()
        with self.store.connect() as db:
            db.execute("UPDATE orca_requests SET " + ",".join(k+"=?" for k in fields) + " WHERE id=?",
                       (*fields.values(), key))

    def recover(self, key):
        with self.store.transaction() as db:
            row = db.execute("SELECT state FROM orca_requests WHERE id=?", (key,)).fetchone()
            if not row or row["state"] not in {"UNKNOWN", "CREATING", "STARTING"}:
                raise ValueError("결과 확인이 필요한 요청만 복구할 수 있습니다")
            found = db.execute("UPDATE orca_intents SET recover_requested=1 WHERE request_id=?", (key,))
            if not found.rowcount:
                raise ValueError("저장된 실행 의도가 없습니다. Orca에서 직접 확인해 주세요")

    def answer(self, key, source, text):
        text = redact_text(text.strip(), 2000)
        if not text:
            raise ValueError("답변을 입력해 주세요")
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM orca_questions WHERE reply_source=?", (source,)).fetchone()
            if existing:
                if existing["request_id"] != key or existing["reply_text"] != text:
                    raise ValueError("같은 답변 ID에 다른 내용을 저장할 수 없습니다")
                return
            question = db.execute("SELECT * FROM orca_questions WHERE request_id=? AND state='PENDING' ORDER BY rowid LIMIT 1", (key,)).fetchone()
            row = db.execute("SELECT state FROM orca_requests WHERE id=?", (key,)).fetchone()
            if not question or not row or row["state"] in TERMINAL:
                raise ValueError("답변을 기다리는 질문이 없습니다")
            db.execute("UPDATE orca_questions SET reply_text=?, reply_source=?,state='ANSWERING' WHERE message_id=?",
                (text,source,question["message_id"]))

    def describe(self, key=""):
        rows = self.rows()
        if key:
            rows = [r for r in rows if r["id"] == key]
        if not rows:
            return "Orca 등록 작업이 없습니다."
        return "\n\n".join(f"{r['id']} · {r['state']} · {r['agent']} / {r['project']}\n"
            f"{r['request_text'][:180]}\nRun: {r['run_id'] or '-'} · Task: {r['task_id'] or '-'} · Dispatch: {r['dispatch_id'] or '-'}\n"
            f"최근 확인: {r['updated_at']}\n{r['summary'] or r['error'] or ''}" for r in rows[-8:])


class NativeOrca:
    def __init__(self, config):
        self.config = config

    def call(self, *args):
        # The selected managed CLI is fixed; never fall back to another executable or host.
        proc = subprocess.run([self.config["cli"], *args, "--json"], cwd=self.config["cwd"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=65, check=False)
        data = json.loads(proc.stdout)
        if (data.get("_meta") or {}).get("runtimeId") != self.config["runtime_id"]:
            raise RuntimeError("Orca runtime identity mismatch")
        if not data.get("ok"):
            raise RuntimeError((data.get("error") or {}).get("code", "orca_error"))
        return data["result"]


class OrcaBridge:
    def __init__(self, queue, client=None):
        self.queue = queue
        self.config = queue.config
        self.client = client or NativeOrca(self.config)

    def call(self, command, *args):
        return self.client.call("orchestration", command, *args)

    def notify(self, row, state, text):
        if row["chat_id"] is None:
            return
        self.queue.store.queue_outbox(dedupe_key=f"orca:{row['id']}:{state}", chat_id=row["chat_id"],
            method="sendMessage", payload={"chat_id": row["chat_id"],
                "text": redact_text(f"Orca {row['id']} · {state}\n{text}", 3500)})

    def mirror(self):
        hub = Path(self.config["hub_root"])
        with self.queue.store.connect() as db:
            anomalies = [dict(r) for r in db.execute("SELECT * FROM orca_anomalies")]
        if anomalies:
            self.atomic(hub / "orca/ATTENTION.md", "Native inbox inspection required:\n" +
                "\n".join(f"{r['message_id']}: {r['reason']} ({r['observed_at']})" for r in anomalies))
        for row in self.queue.rows():
            text = "\n".join(f"{k}: {row.get(k) or '-'}" for k in
                ("id", "state", "agent", "project", "run_id", "task_id", "dispatch_id", "updated_at"))
            body = f"assignee: orca\nexecutor: orca\nfrom: {row['source_key'].split(':')[0]}\ncreated: {row['created_at']}\n\n{row['request_text'].replace(chr(10),' ')}\n\n{text}\n\n{row['summary'] or row['error'] or ''}\n"
            report = hub / "orca" / (row["id"] + ".md")
            self.atomic(report, body)
            todo = hub / "todo" / (row["id"] + ".md")
            if row["state"] in TERMINAL:
                self.atomic(hub / "todo/done" / row["created_at"][:7] / todo.name, body)
                todo.unlink(missing_ok=True)
            else:
                self.atomic(todo, body)

    @staticmethod
    def atomic(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.read_text() == text:
            return
        fd, tmp = tempfile.mkstemp(prefix=".orca-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def tick(self):
        if not self.config.get("enabled"):
            return
        # Shared by manual invocation and the timer; an overlapping tick never launches.
        with (self.queue.store.db_path.parent / "orca-bridge.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                self.reconcile()
                rows = self.queue.rows()
                with self.queue.store.connect() as db:
                    recoveries = [dict(r) for r in db.execute("SELECT * FROM orca_intents WHERE recover_requested=1")]
                for intent in recoveries:
                    row = next(r for r in rows if r["id"] == intent["request_id"])
                    if row["state"] not in TERMINAL:
                        self.execute_intent(row, intent)
                    with self.queue.store.connect() as db:
                        db.execute("UPDATE orca_intents SET recover_requested=0 WHERE request_id=?", (row["id"],))
                rows = self.queue.rows()
                if not any(r["state"] in ACTIVE for r in rows) and not self.queue.store.active_task_count():
                    pending = next((r for r in rows if r["state"] == "QUEUED"), None)
                    if pending:
                        self.start(pending)
            finally:
                self.mirror()

    def start(self, row):
        key = row["id"]
        run, coordinator = self.config["run_id"], self.config["coordinator"]
        project = self.config["projects"][row["project"]]
        spec = (f"Business request ID: {key}. Target: {project['path']}. "
            f"Authorized scope: {project['scope']}. Change requested: {row['request_text']}\n"
            "Read repository AGENTS.md and HANDOFF.md first. Preserve task context and previous decisions. "
            "Do not touch other repositories, credentials, settings, services, production, merge, push, or deploy. "
            "Do not commit. Report tests/evidence and changed files via your native worker_done; never claim unrun checks.")
        create_args = ["task-create", "--run", run, "--from", coordinator,
            "--task-title", key, "--spec", spec,
            "--retry-request", str(uuid.uuid5(uuid.NAMESPACE_URL, "orca-task:"+key))]
        start_template = ["worker-start", "--run", run, "--from", coordinator,
            "--task", "{task_id}", "--worktree", project["worktree"], "--agent", row["agent"],
            "--timeout-ms", "20000", "--retry-request", str(uuid.uuid5(uuid.NAMESPACE_URL, "orca-start:"+key))]
        # Persist the exact scope, host arguments and replay IDs before any native mutation.
        with self.queue.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO orca_intents(request_id,create_args,start_template) VALUES(?,?,?)",
                (key,json.dumps(create_args),json.dumps(start_template)))
            intent = dict(db.execute("SELECT * FROM orca_intents WHERE request_id=?", (key,)).fetchone())
        self.execute_intent(row, intent)

    def execute_intent(self, row, intent):
        key = row["id"]
        create_args = json.loads(intent["create_args"])
        run = create_args[create_args.index("--run")+1]
        self.queue.update(key, state="CREATING", run_id=run)
        try:
            result = self.call(*create_args)
            task = result["task"]["id"]
            if row["task_id"] and row["task_id"] != task:
                raise RuntimeError("Orca task replay identity mismatch")
            self.queue.update(key, task_id=task, state="STARTING")
            args = [task if a == "{task_id}" else a for a in json.loads(intent["start_template"])]
            result = self.call(*args)
            if result.get("taskId") != task or result.get("runId") != run or not result.get("dispatchId"):
                raise RuntimeError("Orca launch identity mismatch")
            if row["dispatch_id"] and row["dispatch_id"] != result["dispatchId"]:
                raise RuntimeError("Orca dispatch replay identity mismatch")
            state = "RUNNING" if result.get("state") == "ready" else "UNKNOWN"
            self.queue.update(key, dispatch_id=result["dispatchId"], state=state, error="")
            self.notify(row, state, f"Task: {task} · Dispatch: {result['dispatchId']}")
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.queue.update(key, state="UNKNOWN", error=type(exc).__name__ + ": 실행 결과 확인 필요. 자동 재실행하지 않습니다.")
            self.notify(row, "UNKNOWN", "실행 결과를 확인 중입니다. 중복 실행하지 않습니다.")

    def reconcile(self):
        rows = self.queue.rows()
        with self.queue.store.connect() as db:
            answers = [dict(r) for r in db.execute("SELECT * FROM orca_questions WHERE state='ANSWERING'")]
        for answer in answers:
            self.call("reply", "--id", answer["message_id"], "--body", answer["reply_text"],
                "--run", self.config["run_id"], "--from", self.config["coordinator"],
                "--retry-request", str(uuid.uuid5(uuid.NAMESPACE_URL, "orca-reply:"+answer["reply_source"])))
            with self.queue.store.connect() as db:
                db.execute("UPDATE orca_questions SET state='ANSWERED' WHERE message_id=?", (answer["message_id"],))
        if not any(r["state"] != "QUEUED" for r in rows):
            return
        run = self.config["run_id"]
        tasks = self.call("task-list", "--run", run, "--brief")["tasks"]
        workers, cursor = [], None
        while True:
            args = ["--run", run] + (["--cursor", cursor] if cursor else [])
            page = self.call("worker-list", *args)
            workers.extend(page["workers"])
            if not page.get("page", {}).get("hasMore"):
                break
            cursor = page["page"]["nextCursor"]
        for row in rows:
            if row["state"] not in ACTIVE:
                continue
            matching_tasks = [t for t in tasks if t.get("task_title") == row["id"] and t.get("run_id") == run]
            if not row["task_id"] and len(matching_tasks) == 1:
                row["task_id"] = matching_tasks[0]["id"]
                self.queue.update(row["id"], task_id=row["task_id"])
            candidates = [w for w in workers if w.get("taskId") == row["task_id"] and w.get("runId") == run]
            if not row["dispatch_id"] and len(candidates) == 1:
                row["dispatch_id"] = candidates[0]["dispatchId"]
                self.queue.update(row["id"], dispatch_id=row["dispatch_id"])
            observed = next((w for w in candidates if w.get("dispatchId") == row["dispatch_id"]), None)
            if observed:
                # A fresh native observation is recorded without treating missing liveness as process death.
                self.queue.update(row["id"], state=row["state"])
            # Absence is unknown, not death. Keep authority blocked until an operator inspects it.
            if not row["dispatch_id"]:
                self.queue.update(row["id"], state="UNKNOWN", error="Task/Dispatch 확인 필요. 자동 재실행 없음")

        delivery = self.call("check", "--run", run, "--terminal", self.config["coordinator"])
        all_processed = True
        for msg in delivery.get("messages", []):
            payload = msg.get("payload") or {}
            try:
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, dict):
                    raise ValueError("non-object payload")
            except (ValueError, TypeError):
                with self.queue.store.connect() as db:
                    db.execute("INSERT OR IGNORE INTO orca_anomalies(message_id,reason,observed_at) VALUES(?,?,?)",
                        (str(msg.get("id") or "missing-id"), "invalid orchestration payload quarantined: "+redact_text(json.dumps(msg,ensure_ascii=False),3000), utc_now()))
                continue
            row = next((r for r in self.queue.rows() if r["task_id"] == payload.get("taskId")
                and r["dispatch_id"] == payload.get("dispatchId") and r["run_id"] == run), None)
            worker = next((w for w in workers if row and w["dispatchId"] == row["dispatch_id"]), None)
            identity_ok = (row and worker and msg.get("from_handle") in {worker.get("agentTerminalHandle"), "dispatch:"+worker["dispatchId"]}
                and msg.get("run_id") == run)
            if msg.get("type") == "question" and identity_ok and msg.get("id"):
                with self.queue.store.connect() as db:
                    db.execute("INSERT OR IGNORE INTO orca_questions(message_id,request_id,body) VALUES(?,?,?)",
                        (msg["id"], row["id"], redact_text(msg.get("body", ""), 2000)))
                    question = db.execute("SELECT * FROM orca_questions WHERE message_id=?", (msg["id"],)).fetchone()
                if row["state"] in TERMINAL:
                    with self.queue.store.connect() as db:
                        db.execute("UPDATE orca_questions SET state='SUPERSEDED' WHERE message_id=? AND state!='ANSWERED'", (msg["id"],))
                    continue
                if question["state"] == "ANSWERED":
                    if row["state"] == "ATTENTION":
                        self.queue.update(row["id"], state="RUNNING", error="")
                    continue
                if row["state"] not in TERMINAL:
                    self.queue.update(row["id"], state="ATTENTION", error=question["body"])
                    self.notify(row, "question:"+msg["id"], question["body"]+
                        "\n답변: /orca reply "+row["id"]+" 답변 내용")
                all_processed = False
                continue
            if msg.get("type") == "heartbeat" and identity_ok:
                continue
            outcome = payload.get("outcome")
            if (msg.get("type") == "worker_done" and identity_ok and outcome in {"succeeded", "failed"}
                and (worker.get("dispatchStatus") not in {"completed", "failed"} or worker.get("workerState") != outcome)):
                # The list snapshot may predate a valid completion in this same tick.
                # Keep the delivery for the next native observation; do not quarantine or consume it.
                all_processed = False
                continue
            if (msg.get("type") != "worker_done" or not row or not worker
                or not identity_ok
                or msg.get("run_id") != run or outcome not in {"succeeded", "failed"}
                or worker.get("dispatchStatus") not in {"completed", "failed"}
                or worker.get("workerState") != outcome):
                with self.queue.store.connect() as db:
                    db.execute("INSERT OR IGNORE INTO orca_anomalies(message_id,reason,observed_at) VALUES(?,?,?)",
                        (str(msg.get("id") or "missing-id"), "unmatched or exceptional message quarantined: "+
                         redact_text(json.dumps(msg,ensure_ascii=False),3000), utc_now()))
                if row and row["state"] not in TERMINAL:
                    self.queue.update(row["id"], state="ATTENTION", error="작업자 질문·예외 보고를 Orca에서 확인해 주세요")
                    self.notify(row, "ATTENTION", "Orca에서 작업자 질문·예외 보고를 확인해 주세요.")
                continue
            # Completion and notification survive crashes before release or inbox ack.
            state = outcome.upper()
            summary = redact_text(msg.get("body", ""), 2500)
            if worker.get("terminalState") != "released":
                released = self.call("worker-release", "--dispatch", row["dispatch_id"],
                    "--retry-request", str(uuid.uuid5(uuid.NAMESPACE_URL, "orca-release:"+row["dispatch_id"])))
                if released.get("state") not in {"released", "already_released", "retained"}:
                    all_processed = False
                    continue
            self.queue.update(row["id"], state=state, summary=summary, error="")
            self.notify(row, state, summary)
        if delivery.get("deliveryId") and all_processed:
            self.call("check", "--run", run, "--terminal", self.config["coordinator"], "--ack", delivery["deliveryId"])


def main():
    from app.config import Settings
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["tick", "status", "submit", "reply", "recover"])
    parser.add_argument("--id")
    parser.add_argument("--source")
    parser.add_argument("--project", default="trial")
    parser.add_argument("--agent", choices=["codex", "claude"], default="codex")
    parser.add_argument("--text")
    args = parser.parse_args()
    settings = Settings.load()
    queue = OrcaQueue.load(StateStore(settings.db_path, settings.root / "schema.sql"), settings.root)
    if args.command == "submit":
        if not args.source or not args.source.startswith("hub:") or not args.text:
            parser.error("submit requires --source hub:<unique-request-id> and --text")
        print(queue.submit(args.source, args.project, args.agent, args.text)["id"])
    elif args.command == "recover":
        if not args.id:
            parser.error("recover requires --id")
        queue.recover(args.id)
        print("저장된 동일 실행의 복구를 접수했습니다")
    elif args.command == "reply":
        if not args.id or not args.source or not args.source.startswith("hub:") or not args.text:
            parser.error("reply requires --id, --source hub:<unique-id>, --text")
        queue.answer(args.id, args.source, args.text)
        print("답변 접수")
    elif args.command == "status":
        print(queue.describe())
    else:
        OrcaBridge(queue).tick()


if __name__ == "__main__":
    main()
