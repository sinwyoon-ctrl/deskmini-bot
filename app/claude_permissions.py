"""One-request Claude PermissionRequest decisions through the existing durable outbox."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import sqlite3
import sys
import time

from app.redact import redact
from app.state import utc_now

TTL = 600


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def claude_process():
    pid = os.getppid()
    for _ in range(12):
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        name = Path(f"/proc/{pid}/comm").read_text().strip().lower()
        if "claude" in name:
            return pid, process_identity(pid)
        pid = int(stat[1])
        if pid <= 1:
            break
    raise ValueError("No live Claude ancestor")


def connect(path):
    db = sqlite3.connect(f"file:{Path(path).resolve()}?mode=rw", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def start_runtime(store):
    epoch = secrets.token_hex(16)
    with store.connect() as db:
        db.execute("UPDATE claude_permissions SET state='EXPIRED' WHERE state IN ('PENDING','APPROVED')")
        db.execute("INSERT OR REPLACE INTO claude_permission_runtime VALUES(1,?,?)", (epoch, time.time()))
    return epoch


def heartbeat(store, epoch):
    with store.connect() as db:
        db.execute("UPDATE claude_permission_runtime SET heartbeat=? WHERE id=1 AND epoch=?", (time.time(), epoch))
        db.execute("UPDATE claude_permissions SET state='EXPIRED' WHERE expires_at<=? AND state IN ('PENDING','APPROVED')", (time.time(),))


def install(store, project, request_id, code_root):
    """Only explicit opt-in, dedicated project workspaces; never settings.local.json."""
    if not project.get("claude_permissions"):
        return
    root = Path(project["path"]).resolve()
    path = root / ".claude/settings.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Claude settings must be local regular files")
    settings = json.loads(path.read_text()) if path.exists() else {}
    with store.connect() as db:
        previous = db.execute("SELECT digest FROM claude_permission_installs WHERE workspace=?", (str(root),)).fetchone()
    if path.exists() and (not previous or hashlib.sha256(path.read_bytes()).hexdigest() != previous[0]):
        raise ValueError("Existing Claude project settings require review before hook installation")
    python = Path(sys.executable).resolve()
    command = shlex.join([str(python), "-m", "app.claude_permissions", "--db", str(store.db_path.resolve()),
                          "--request", request_id, "--workspace", str(root)])
    command = "cd " + shlex.quote(str(Path(code_root).resolve())) + " && " + command
    settings = {
        "permissions": {"defaultMode": "default", "ask": ["Bash", "Write", "Edit"]},
        "hooks": {event: [{"hooks": [{"type": "command", "command": command, "timeout": TTL + 120}]}]
                  for event in ("SessionStart", "PermissionRequest")}}
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("x") as out:
        json.dump(settings, out)
    os.replace(temporary, path)
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO claude_permission_installs VALUES(?,?)",
                   (str(root), hashlib.sha256(path.read_bytes()).hexdigest()))


def session_row(db, key, workspace):
    row = db.execute("SELECT * FROM orca_requests WHERE id=?", (key,)).fetchone()
    if not row or row["agent"] != "claude" or row["state"] not in {"STARTING", "RUNNING", "ATTENTION"}:
        raise ValueError("Inactive Claude request")
    if not Path(workspace).is_dir():
        raise ValueError("Missing workspace")
    return row


def native_identity(pid, workspace):
    environment = dict(part.split('=',1) for part in Path(f'/proc/{pid}/environ').read_bytes().decode().split('\0') if '=' in part)
    terminal = environment.get('ORCA_TERMINAL_HANDLE')
    worktree = environment.get('ORCA_WORKTREE_ID')
    if not terminal or not worktree or worktree.split('::',1)[-1] != str(Path(workspace).resolve()):
        raise ValueError('Claude is not in the managed workspace terminal')
    if Path(f'/proc/{pid}/cwd').resolve() != Path(workspace).resolve():
        raise ValueError('Claude working directory mismatch')
    return terminal, worktree


def live_worker(db, row, workspace, code_root, pid, deadline):
    from app.orca_bridge import NativeOrca, OrcaQueue
    queue = OrcaQueue.load(None, Path(code_root))
    # Test drivers can supply an isolated config beside their isolated database.
    config_file = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent / "claude-permission-orca.json"
    if config_file.is_file():
        queue.config = json.loads(config_file.read_text())
    project = queue.config.get("projects", {}).get(row["project"], {})
    if not project.get("claude_permissions") or Path(project.get("path", "/")).resolve() != Path(workspace).resolve():
        raise ValueError("Workspace is not opted in")
    workers, cursor = [], None
    lookup_deadline = min(deadline, time.monotonic()+15)
    for _ in range(10):
        args = ["orchestration", "worker-list", "--run", row["run_id"], "--include-remote"]
        if cursor:
            args += ["--cursor", cursor]
        remaining = lookup_deadline-time.monotonic()
        if remaining <= 0:
            raise ValueError('Worker identity lookup expired')
        page = NativeOrca(queue.config).call(*args, timeout=min(5,remaining))
        workers.extend(page["workers"])
        if not page.get("page", {}).get("hasMore"):
            break
        cursor = page["page"]["nextCursor"]
    else:
        raise ValueError("Worker identity exceeds lookup budget")
    matches = [w for w in workers if w.get("dispatchId") == row["dispatch_id"] and w.get("taskId") == row["task_id"]
               and w.get("runId") == row["run_id"]]
    if len(matches) != 1:
        raise ValueError("Unproven native worker")
    worker = matches[0]
    resource = worker.get("resource") or {}
    terminal, worktree = native_identity(pid, workspace)
    if (resource.get("worktreeId") != project["worktree"].removeprefix("id:")
        or worker.get('agentTerminalHandle') != terminal or resource.get('worktreeId') != worktree
        or resource.get("ownershipState") != "owned"
        or worker.get("projection", {}).get("liveness", {}).get("verdict") != "live"):
        raise ValueError("Native worker is not live and owned")


def register(db, key, workspace, payload, pid, started):
    row = session_row(db, key, workspace)
    if Path(payload["cwd"]).resolve() != Path(workspace).resolve() or not payload.get("session_id") or not started:
        raise ValueError("Session identity mismatch")
    db.execute("INSERT OR IGNORE INTO claude_permission_sessions VALUES(?,?,?,?,?,?)",
               (key, payload["session_id"], pid, started, str(Path(workspace).resolve()),row['dispatch_id']))
    binding = db.execute("SELECT * FROM claude_permission_sessions WHERE request_id=?", (key,)).fetchone()
    expected = (payload["session_id"], pid, started, str(Path(workspace).resolve()))
    if binding['dispatch_id'] != row['dispatch_id'] and (process_identity(binding['pid']) != binding['process_start'] or tuple(binding)[1:5] == expected):
        db.execute('UPDATE claude_permission_sessions SET session_id=?,pid=?,process_start=?,workspace=?,dispatch_id=? WHERE request_id=?',
                   (*expected,row['dispatch_id'],key))
        binding = db.execute('SELECT * FROM claude_permission_sessions WHERE request_id=?',(key,)).fetchone()
    if tuple(binding)[1:5] != expected or binding['dispatch_id'] != row['dispatch_id']:
        raise ValueError("Another Claude process owns the request")
    return row


def enqueue(db, row, payload, pid, started, ttl=TTL):
    runtime = db.execute("SELECT * FROM claude_permission_runtime WHERE id=1").fetchone()
    if not runtime or time.time() - runtime["heartbeat"] > 60:
        raise ValueError("Telegram bot is unavailable")
    original = {"cwd": payload["cwd"], "tool_name": payload["tool_name"], "tool_input": payload["tool_input"]}
    raw = json.dumps(original, ensure_ascii=False, sort_keys=True)
    safe = json.dumps(redact(original), ensure_ascii=False, sort_keys=True)
    complete = safe == raw and len(safe) <= 2500
    key = secrets.token_hex(12)
    expires = time.time() + ttl
    db.execute("""INSERT INTO claude_permissions
        (id,request_id,session_id,dispatch_id,epoch,pid,process_start,hook_pid,hook_start,digest,display,state,can_allow,expires_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?,?)""",
        (key,row["id"],payload["session_id"],row["dispatch_id"],runtime["epoch"],pid,started,
         os.getpid(),process_identity(os.getpid()),hashlib.sha256(raw.encode()).hexdigest(),safe[:2500],int(complete),expires,utc_now()))
    buttons = [{"text": "이번 실행 허용", "callback_data": "clp:allow:" + key}] if complete else []
    buttons.append({"text": "거절", "callback_data": "clp:deny:" + key})
    details = safe[:2500]
    if payload['tool_name'] == 'Bash' and isinstance(original['tool_input'], dict) and complete:
        inputs = dict(original['tool_input'])
        command = inputs.pop('command', '')
        reason = inputs.pop('description', '')
        details = f"하려는 일: {reason or '셸 명령 실행'}\n실행 위치: {payload['cwd']}\n\n실행할 명령:\n{command}"
        if inputs:
            details += "\n추가 조건: " + json.dumps(inputs, ensure_ascii=False)
    body = ("🔐 클로드가 실행 허용을 기다립니다\n작업: " + row["project"] + "\n\n" + details
        + ("" if complete else "\n⚠️ 내용을 전부 표시할 수 없어 원격 허용을 막았습니다.")
        + f"\n\n{ttl//60}분 안에 선택해 주세요. 이 요청 한 번에만 적용합니다.\n요청: " + key)
    outgoing = {"chat_id": row["chat_id"], "text": body, "reply_markup": {"inline_keyboard": [buttons]}}
    db.execute("""INSERT INTO outbox(dedupe_key,chat_id,method,payload_json,state,next_attempt_at,created_at)
        VALUES(?,?,'sendMessage',?,'PENDING',?,?)""",
        ("clp:"+key,row["chat_id"],json.dumps(outgoing,ensure_ascii=False),utc_now(),utc_now()))
    return key


def decide(bot, data):
    try:
        _, action, key = data.split(":")
        if action not in {"allow", "deny"}:
            raise ValueError("Unknown decision")
        with bot.store.transaction() as db:
            row = db.execute("SELECT * FROM claude_permissions WHERE id=?", (key,)).fetchone()
            runtime = db.execute("SELECT * FROM claude_permission_runtime WHERE id=1").fetchone()
            if (not row or not runtime or row["epoch"] != runtime["epoch"] or row["state"] != "PENDING"
                or row["expires_at"] <= time.time() or time.time()-runtime["heartbeat"] > 60
                or process_identity(row["pid"]) != row["process_start"]
                or process_identity(row["hook_pid"]) != row["hook_start"]
                or (action == "allow" and not row["can_allow"])):
                raise ValueError("Expired or already answered")
            owner = db.execute("SELECT * FROM orca_requests WHERE id=?", (row["request_id"],)).fetchone()
            if not owner or owner["chat_id"] != bot.settings.allowed_chat_id or owner["dispatch_id"] != row["dispatch_id"]:
                raise ValueError("Wrong owner")
            db.execute("UPDATE claude_permissions SET state=? WHERE id=? AND state='PENDING'",
                       ("APPROVED" if action == "allow" else "DENIED", key))
        bot._send("허용을 접수했습니다. 클로드에 전달할 조건을 확인합니다." if action == "allow" else "거절을 접수했습니다.")
    except (ValueError, KeyError):
        bot._send("이 승인 요청은 만료되었거나 이미 처리했습니다. 새 요청을 확인해 주세요.")


def run_hook(db_path, key, workspace, payload, code_root):
    deadline = time.monotonic()+TTL+60
    pid, started = claude_process()
    native_identity(pid, workspace)
    if payload.get("hook_event_name") == "SessionStart":
        return None
    if payload.get("hook_event_name") != "PermissionRequest" or payload.get("permission_mode") in {"bypassPermissions", "dontAsk"}:
        raise ValueError("Unsupported permission event")
    # worker-start may return only after the SessionStart hook; never wait in that hook.
    end = min(deadline, time.monotonic() + 65)
    while True:
        with connect(db_path) as db:
            row = session_row(db, key, workspace)
        if row["dispatch_id"]:
            break
        if time.monotonic() >= end:
            raise ValueError("Dispatch registration timeout")
        time.sleep(.25)
    with connect(db_path) as db:
        live_worker(db, row, workspace, code_root, pid, deadline)
        register(db, key, workspace, payload, pid, started)
        remaining = int(min(TTL,deadline-time.monotonic()-20))
        if remaining <= 0:
            raise ValueError('No approval time remaining')
        approval = enqueue(db, row, payload, pid, started, remaining)
    while True:
        with connect(db_path) as db:
            item = db.execute("SELECT * FROM claude_permissions WHERE id=?", (approval,)).fetchone()
            runtime = db.execute("SELECT * FROM claude_permission_runtime WHERE id=1").fetchone()
            valid = (runtime and runtime["epoch"] == item["epoch"] and time.time()-runtime["heartbeat"] <= 60
                     and time.monotonic() < deadline and time.time() < item["expires_at"] and process_identity(pid) == started)
            if not valid or item["state"] not in {"PENDING", "APPROVED"}:
                db.execute("UPDATE claude_permissions SET state='DENIED' WHERE id=?", (approval,))
                return False
            if item["state"] == "APPROVED":
                owner = session_row(db, key, workspace)
                if owner["dispatch_id"] != item["dispatch_id"]:
                    raise ValueError("Dispatch changed")
                live_worker(db, owner, workspace, code_root, pid, deadline)
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute("SELECT * FROM claude_permissions WHERE id=?", (approval,)).fetchone()
                runtime = db.execute("SELECT * FROM claude_permission_runtime WHERE id=1").fetchone()
                current = session_row(db, key, workspace)
                if (fresh['state'] != 'APPROVED' or runtime['epoch'] != fresh['epoch']
                    or time.time()-runtime['heartbeat'] > 60 or time.time() >= fresh['expires_at']
                    or time.monotonic() >= deadline
                    or current['dispatch_id'] != fresh['dispatch_id'] or process_identity(pid) != started):
                    raise ValueError("Approval changed during validation")
                db.execute("UPDATE claude_permissions SET state='DELIVERED' WHERE id=? AND state='APPROVED'", (approval,))
                return True
        time.sleep(.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.read(1_000_001))
        allowed = run_hook(args.db, args.request, args.workspace, payload, Path(__file__).resolve().parents[1])
        if allowed is None:
            return
    except Exception as exc:
        print("Claude permission bridge refused: " + type(exc).__name__, file=sys.stderr)
        allowed = False
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow" if allowed else "deny", **({} if allowed else {"message": "원격 승인이 없거나 만료되어 실행을 거절했습니다.", "interrupt": True})}}}))


if __name__ == "__main__":
    main()
