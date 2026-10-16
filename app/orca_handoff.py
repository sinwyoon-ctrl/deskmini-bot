"""Manual handoff journal. Native authority is required; absence never grants ownership."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import uuid
from pathlib import Path

from app.redact import redact_text
from app.state import utc_now

CHOICES = {
    "codex": ("codex", ""), "코덱스": ("codex", ""),
    "claude": ("claude", ""), "클로드": ("claude", ""),
    "astra": ("codex", "gpt-6-astra"), "아스트라": ("codex", "gpt-6-astra"),
    "sol": ("codex", "gpt-6.1-sol"), "솔": ("codex", "gpt-6.1-sol"),
}
LABELS = {"QUEUED": "대기", "CREATING": "접수", "STARTING": "시작 확인 중",
    "RUNNING": "실행 중", "UNKNOWN": "확인 불가", "ATTENTION": "질문·확인 필요",
    "STOPPING": "중단 확인 중", "HANDOFF": "인계 대기", "BLOCKED": "인계 차단",
    "IMPORTED": "외부 작업 문맥 확인", "SUCCEEDED": "완료", "FAILED": "실패"}
OPEN_CHANGES = ("PENDING", "STOPPING", "STOPPED", "STARTING", "UNKNOWN", "BLOCKED")


def replay(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "orca-handoff:" + key))


def choice(value):
    if value.lower() not in CHOICES:
        raise ValueError("모델은 codex·claude·astra·sol 중 골라 주세요")
    return CHOICES[value.lower()]


def safe_read(path, limit=4000):
    if path.is_symlink() or not path.is_file():
        return ""
    with path.open() as stream:
        return redact_text(stream.read(limit), limit)


def project_context(config, project_id):
    """Bounded, file-based provenance only; never read provider conversations or diffs."""
    project = config["projects"][project_id]
    root, hub = Path(project["path"]), Path(config["hub_root"])
    key = project.get("hub_project", project_id)
    locks = {p.name: safe_read(p, 1800) for p in sorted((hub / "locks").glob(key + "*.lock"))
             if p.name == key + ".lock" or p.name.startswith(key + "__")}
    notes = {str(p.relative_to(hub)): safe_read(p) for p in sorted((hub / "handoffs").glob(key + "*.md"))[:5]
             if p.stem == key or p.stem.startswith(key + "__")}
    def git(*args):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10, check=False)
        if result.returncode:
            raise ValueError("등록 개발 저장소를 읽을 수 없습니다")
        return redact_text(result.stdout, 4000).strip()
    snapshot = {"project": project_id, "repo": str(root), "worktree": project["worktree"],
        "scope": project["scope"], "locks": locks, "hub_handoffs": notes,
        "observed_at": utc_now(), "decisions": {}, "tests": "미확인: 자동 실행하지 않음",
        "next_step": "이전 체크포인트와 요청을 확인하고 범위 내 작업 계속"}
    if root.exists():
        snapshot.update(branch=git("branch", "--show-current"), head=git("rev-parse", "HEAD"),
            dirty_files=git("status", "--porcelain=v1", "--untracked-files=normal"),
            diff_summary=git("diff", "--no-ext-diff", "--no-textconv", "--stat", "HEAD"))
        snapshot["decisions"] = {name: safe_read(root / name) for name in ("AGENTS.md", "HANDOFF.md", "REPORT.md")}
    else:
        snapshot["unavailable"] = True
    return snapshot


class Handoff:
    def __init__(self, queue):
        self.q = queue

    def changes(self, key):
        with self.q.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM orca_changes WHERE request_id=? ORDER BY rowid", (key,))]

    def request(self, key, source, target, text=""):
        with (self.q.store.db_path.parent / "orca-bridge.lock").open("a") as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("실행기 상태 갱신 중입니다. 잠시 후 같은 요청을 다시 보내 주세요")
            return self._request(key,source,target,text)

    def _request(self, key, source, target, text=""):
        agent, model = choice(target)
        if not self.q.config.get("enabled"):
            raise ValueError("Orca 연결이 꺼져 있습니다")
        if not source or len(source)>160:
            raise ValueError("인계 출처 ID를 확인해 주세요")
        text = redact_text(text, 2000)
        with self.q.store.transaction() as db:
            old = db.execute("SELECT * FROM orca_changes WHERE source_key=?", (source,)).fetchone()
            if old:
                if (old["request_id"], old["agent"], old["model"], old["instruction"]) != (key, agent, model, text):
                    raise ValueError("같은 출처 ID의 인계 내용을 바꿀 수 없습니다")
                return dict(old)
            row = db.execute("SELECT * FROM orca_requests WHERE id=?", (key,)).fetchone()
            if not row:
                raise ValueError("작업 ID를 확인해 주세요: /orca status")
            if row["state"] in {"CREATING","STARTING","UNKNOWN"}:
                raise ValueError("이전 실행 결과가 미확인입니다. /orca recover 작업ID로 먼저 확인해 주세요")
            if any(c["state"] != "DONE" for c in self.changes(key)):
                raise ValueError("이미 인계 확인 중입니다. /orca recover 작업ID로 같은 요청을 확인해 주세요")
            db.execute("""INSERT INTO orca_changes(source_key,request_id,agent,model,instruction,state,old_task,old_dispatch,created_at)
                VALUES(?,?,?,?,?,'PENDING',?,?,?)""", (source,key,agent,model,text,row["task_id"],row["dispatch_id"],utc_now()))
            if row['chat_id'] is not None:
                from app.shared_conversation import snapshot
                conversation = snapshot(self.q.store,row['chat_id'])
                db.execute('UPDATE orca_changes SET context_json=? WHERE source_key=?',(json.dumps({'telegram_conversation':conversation},ensure_ascii=False),source))
            # This transaction fences a queued initial start as well as another switch.
            db.execute("UPDATE orca_requests SET state='HANDOFF',updated_at=? WHERE id=?", (utc_now(),key))
            return dict(db.execute("SELECT * FROM orca_changes WHERE source_key=?", (source,)).fetchone())

    def save(self, change, **fields):
        allowed = {"state", "context_json", "create_args", "start_args", "error", "new_task", "new_dispatch", "stop_args"}
        if set(fields) - allowed:
            raise ValueError("invalid handoff field")
        with self.q.store.connect() as db:
            db.execute("UPDATE orca_changes SET " + ",".join(k+"=?" for k in fields) + " WHERE source_key=?",
                       (*fields.values(),change["source_key"]))
        change.update(fields)

    def block(self, bridge, row, change, reason, state="BLOCKED"):
        self.save(change, state=state, error=reason)
        self.q.update(row["id"], state="BLOCKED" if state == "BLOCKED" else "UNKNOWN", error=reason)
        bridge.notify(row, change["source_key"]+":"+state, "인계 차단: " + reason)

    @staticmethod
    def exited(worker):
        return (worker.get("projection", {}).get("liveness", {}).get("verdict") == "exited"
                and worker.get("dispatchStatus") in {"failed", "completed"}
                and worker.get("workerState") in {"stopped", "failed", "succeeded"})

    def advance(self, bridge, row, change):
        """Each mutation has immutable argv + replay ID, persisted before calling native."""
        if change["state"] == "UNKNOWN" and change["new_dispatch"]:
            try:
                worker = next((w for w in bridge.workers() if w.get("dispatchId") == change["new_dispatch"]
                    and w.get("taskId") == change["new_task"] and w.get("runId") == self.q.config["run_id"]),None)
                if worker and self.exited(worker) and worker.get("workerState") in {"failed","stopped"}:
                    self.save(change,state="DONE",error="native 실행 실패·종료 확인")
                    self.q.update(row["id"],state="FAILED",error="선택한 모델 실행 실패·종료 확인. 자동 전환 없음")
                    bridge.notify(row,change["source_key"]+":FAILED","선택한 모델 실행 실패·종료 확인 · 자동 전환 없음")
                    bridge.release_claim(row)
            except (OSError,ValueError,KeyError,RuntimeError,subprocess.TimeoutExpired):
                pass
            return
        if change["state"] in {"DONE", "BLOCKED", "UNKNOWN"}:
            return
        key, source = row["id"], change["source_key"]
        try:
            project = self.q.config["projects"][row["project"]]
            if row.get("imported"):
                self.block(bridge,row,change,"외부 Claude/Desktop 작업입니다. 기존 담당자의 중단·잠금 인계를 확인한 뒤 관리자가 개발 작업공간을 등록해야 합니다")
                return
            bridge.guard_project(row)
            workers = bridge.workers() if change["old_dispatch"] else []
            old = next((w for w in workers if w.get("dispatchId") == change["old_dispatch"]
                and w.get("taskId") == change["old_task"] and w.get("runId") == row["run_id"]), None)
            if change["old_task"] and not change["old_dispatch"]:
                self.block(bridge,row,change,"이전 시작 결과의 Dispatch가 미확인입니다. Orca에서 소유권을 확인해 주세요")
                return
            if change["old_dispatch"] and change["state"] in {"PENDING", "STOPPING"}:
                if not old:
                    self.block(bridge,row,change,"이전 작업자의 권한·종료를 확인할 수 없습니다")
                    return
                if not self.exited(old):
                    live = old.get("projection", {}).get("liveness", {}).get("verdict")
                    resource = old.get("resource") or {}
                    # Explicit user switch authorizes stopping this proven live supervised owner only.
                    if live not in {"live", "exited"} or resource.get("ownerDispatchId") != change["old_dispatch"] or resource.get("ownershipState") != "owned":
                        self.block(bridge,row,change,"이전 작업자 상태가 미확인 또는 관리 권한 밖입니다. 연결 복구·소유권 확인이 필요합니다")
                        return
                    if not change["stop_args"]:
                        self.save(change, stop_args=json.dumps(["worker-stop", "--dispatch", change["old_dispatch"],
                            "--retry-request", replay(source+":stop")]))
                    self.save(change, state="STOPPING")
                    self.q.update(key,state="STOPPING")
                    bridge.notify(row,source+":STOPPING","중단 확인 중 · 이전 작업자의 종료를 확인한 뒤 인계합니다")
                    bridge.call(*json.loads(change["stop_args"]))
                    # Neither a successful RPC nor fencing alone proves process exit.
                    workers = bridge.workers()
                    old = next((w for w in workers if w.get("dispatchId") == change["old_dispatch"]
                        and w.get("taskId") == change["old_task"] and w.get("runId") == row["run_id"]), None)
                    if not old or not self.exited(old):
                        self.block(bridge,row,change,"중단 요청 후 실제 종료가 미확인입니다. /orca recover 작업ID로 다시 확인해 주세요", "UNKNOWN")
                        return
                self.save(change,state="STOPPED")
            elif not change["old_dispatch"] and change["state"] == "PENDING":
                self.save(change,state="STOPPED")
            if change["state"] == "STOPPED":
                # Recheck native exit on restart, not just our durable local checkpoint.
                if change["old_dispatch"] and (not old or not self.exited(old)):
                    self.block(bridge,row,change,"기존 작업자의 종료 증거를 다시 확인할 수 없습니다")
                    return
                cleanup = {"state":"not_needed"}
                if old and old.get("terminalState") != "released":
                    cleanup = bridge.call("worker-release","--dispatch",change["old_dispatch"],
                        "--retry-request",replay(source+":release-old"))
                    if cleanup.get("state") not in {"released","already_released","retained"}:
                        self.block(bridge,row,change,"이전 작업자 종료는 확인됐지만 native 자원 정리 결과가 미확인입니다. 같은 요청을 recover 해 주세요","UNKNOWN")
                        return
                snapshot = project_context(self.q.config,row["project"])
                if change.get('context_json'):
                    prior_context = json.loads(change['context_json'])
                    if 'telegram_conversation' in prior_context:
                        snapshot['telegram_conversation'] = prior_context['telegram_conversation']
                snapshot["old_terminal_cleanup"] = cleanup
                snapshot.update(objective=row["request_text"], checkpoint=row["summary"] or row["error"] or "작업자 체크포인트 미보고",
                    instruction=change["instruction"], previous_task=change["old_task"], previous_dispatch=change["old_dispatch"],
                    previous_agent=row["agent"], previous_model=row.get("model", ""), source=source,
                    tests=row["summary"] or "미보고: 완료·검증을 추정하지 말 것")
                # Previous decisions/specs remain available to every later attempt, without truncating history.
                with self.q.store.connect() as db:
                    snapshot["attempts"] = [dict(a) for a in db.execute("SELECT dispatch_id,task_id,agent,model,source_key,outcome,summary,created_at FROM orca_attempts WHERE request_id=?",(key,))]
                    snapshot["questions_and_answers"] = [dict(a) for a in db.execute(
                        "SELECT message_id,body,state,reply_text,reply_source FROM orca_questions WHERE request_id=?",(key,))]
                self.save(change,context_json=json.dumps(snapshot,ensure_ascii=False))
                context_path = self.q.store.db_path.parent / "orca-context" / (key+".json")
                bridge.atomic(context_path, change["context_json"])
                bridge.atomic(context_path.parent / (replay(source)+".json"),change["context_json"])
                continuing = bool(old and old.get("workerState") == "succeeded")
                if change["old_task"] and not continuing:
                    with self.q.store.connect() as db:
                        intent = db.execute("SELECT context_path FROM orca_task_contracts WHERE task_id=?",(change["old_task"],)).fetchone()
                    if not intent or str(context_path) != intent["context_path"]:
                        # Existing pre-upgrade Task specs cannot deliver the new context pointer safely.
                        continuing = True
                        snapshot["native_task_migration"] = "Pre-upgrade Task has no context pointer; old owner positively exited"
                        self.save(change,context_json=json.dumps(snapshot,ensure_ascii=False))
                        bridge.atomic(context_path,change["context_json"])
                if not change["create_args"]:
                    spec = bridge.spec(row)
                    # Retry keeps native Task; completed work gets a new native Task under the same business ID.
                    if not change["old_task"] or continuing:
                        create = ["task-create","--run",self.q.config["run_id"],"--from",self.q.config["coordinator"],
                            "--task-title",key,"--spec",spec,"--retry-request",replay(source+":create")]
                    else:
                        create = []
                    args = ["worker-start","--run",self.q.config["run_id"],"--from",self.q.config["coordinator"],
                        "--task","{task_id}","--worktree",project["worktree"],"--agent",change["agent"],
                        "--timeout-ms","60000","--retry-request",replay(source+":start")]
                    if change["model"]:
                        args += ["--model",change["model"]]
                    if change["old_task"] and not continuing:
                        args += ["--retry-of",change["old_dispatch"]]
                    self.save(change,create_args=json.dumps(create),start_args=json.dumps(args),state="STARTING")
            if change["state"] == "STARTING":
                if change["old_dispatch"] and (not old or not self.exited(old)):
                    self.block(bridge,row,change,"기존 소유자의 종료 증거가 없어 재시작을 보류합니다")
                    return
                # The immutable Task spec points at the stable context file written before launch.
                create = json.loads(change["create_args"])
                task = change["new_task"]
                if not task:
                    if create:
                        task = bridge.call(*create)["task"]["id"]
                        bridge.record_contract(key,task)
                    else:
                        task = change["old_task"]
                    self.save(change,new_task=task)
                args = [task if a == "{task_id}" else a for a in json.loads(change["start_args"])]
                result = bridge.call(*args)
                if result.get("taskId") != task or result.get("runId") != self.q.config["run_id"] or not result.get("dispatchId"):
                    raise RuntimeError("handoff launch identity mismatch")
                dispatch = result["dispatchId"]
                if change["new_dispatch"] and change["new_dispatch"] != dispatch:
                    raise RuntimeError("handoff replay identity mismatch")
                state = "RUNNING" if result.get("state") == "ready" else "UNKNOWN"
                self.save(change,new_dispatch=dispatch)
                bridge.record_attempt(key,result,change["agent"],change["model"],source,change["context_json"])
                with self.q.store.transaction() as db:
                    db.execute("UPDATE orca_requests SET agent=?,model=?,task_id=?,dispatch_id=?,run_id=?,state=?,error='',updated_at=? WHERE id=?",
                        (change["agent"],change["model"],task,dispatch,self.q.config["run_id"],state,utc_now(),key))
                    db.execute("UPDATE orca_changes SET state=? WHERE source_key=?",("DONE" if state == "RUNNING" else "UNKNOWN",source))
                    db.execute("UPDATE orca_questions SET state='SUPERSEDED' WHERE request_id=? AND state IN ('PENDING','ANSWERING')",(key,))
                    fresh = dict(row,agent=change["agent"],model=change["model"])
                    bridge.notify(fresh,source+":"+state,"인계 실행 중" if state == "RUNNING" else "인계 시작 결과 확인 불가 · 자동 모델 전환 없음",db)
        except (OSError,ValueError,KeyError,RuntimeError,subprocess.TimeoutExpired) as exc:
            self.block(bridge,row,change,redact_text(str(exc),500) or type(exc).__name__,"UNKNOWN")
