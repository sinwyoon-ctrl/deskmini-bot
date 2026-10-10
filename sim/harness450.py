"""450건 검증(4라운드) 공용 하네스. harness300 위에 얹는다.

- SimRunner: 실제 TaskRunner 의 재시도 규칙(can_retry·retry_budget·retry_mode·retry)은 그대로 쓰고,
  저장소·systemd 에 닿는 부분(start 의 git, _launch 의 systemd-run)만 기록기로 바꾼다. 실행 기록 = exec_log.
- app.runner.run_fixed / _git 는 호출되면 위반으로 기록하고 실패한다(~/deskmini 저장소를 절대 건드리지 않게).
- SyncExecutor: 검증·반영 future 를 즉시 실행(실제 스레드 없음).
- FakeTG450: 첨부 get_file/download_file, 전송 실패 주입(TelegramError), answer_callback 실패 주입.
- CapturingClient: 대화 모델 입력(system+context+messages)을 전부 기록해 비밀값·이미지 바이트 유입을 독립 판정.
- restart(): 같은 DB·같은 기록기로 새 ChiefBot 을 만든다(봇 재시작 흉내).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import harness300 as H
import run_sim as base

import app.runner as runner_module
from adapters.telegram import TelegramError
from app.bot import ChiefBot
from app.runner import RunnerError, TaskRunner
from app.state import utc_now

CHAT = base.CHAT_ID
EXEC_VIOLATIONS: list[str] = []


def _forbidden_git(*args: Any, **kwargs: Any) -> Any:
    EXEC_VIOLATIONS.append(f"repo command attempted: {args[:2]}")
    raise RunnerError("sim: repository commands are blocked")


runner_module.run_fixed = _forbidden_git  # type: ignore[assignment]
runner_module._git = _forbidden_git  # type: ignore[assignment]

# 추가 비밀값 조각(첨부 텍스트에 심는다)
PEM_BODY = "PEMSENTINELb3BlbnNzaC1rZXktdjEAAAAABG5vbmU"
FAKE_GH = "ghp_FAKEghTOKENfake0123456789abcdef"
EXTRA_SECRET_TEXT = "\n".join([
    *H.SECRET_LINES,
    f"github={FAKE_GH}",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    PEM_BODY,
    "-----END OPENSSH PRIVATE KEY-----",
])
EXTRA_FRAGMENTS = ["PEMSENTINEL", "FAKEghTOKEN"]
# 마스킹 규칙이 모르는 형식(보고만, 안전 위반으로 세지 않음)
PROBE_SECRETS = {"aws": "AKIAFAKEAWS0123456789", "slack": "xox" "b-1111111111-FAKESLACKTOKENabc"}
IMG_SENTINEL = b"IMGSENTINEL450"


def all_leaks(text: str) -> list[str]:
    return [f for f in H.LEAK_FRAGMENTS + EXTRA_FRAGMENTS if f in (text or "")]


class ExecLog:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, kind: str, task_id: str, **extra: Any) -> None:
        self.items.append({"kind": kind, "task_id": task_id, **extra})

    def count(self, kind: str | None = None, task_id: str | None = None) -> int:
        return sum(1 for i in self.items if (kind is None or i["kind"] == kind) and (task_id is None or i["task_id"] == task_id))


class SimRunner(TaskRunner):
    def __init__(self, settings: Any, store: Any, log: ExecLog, fixture_root: Path):
        super().__init__(settings, store)
        self.log = log
        self.fixture_root = fixture_root
        self.outcomes: dict[str, list[dict[str, Any]]] = {}
        self.stops: list[str] = []

    def _validate_repo(self, project: Any) -> None:  # 저장소 검사 없음
        return None

    def _base_sha(self, project: Any) -> str:
        return "b" * 40

    def fake_worktree(self, task_id: str) -> Path:
        wt = self.fixture_root / "worktrees" / task_id
        wt.mkdir(parents=True, exist_ok=True)
        (wt / ".git").write_text("gitdir: /nonexistent\n")
        return wt

    def start(self, task_id: str) -> dict[str, Any]:
        if self.store.active_task_count() >= self.settings.max_active_tasks:
            raise RunnerError("Another task is active")
        task = self.store.get_task(task_id)
        if task["state"] != "AWAITING_START":
            raise RunnerError(f"Task is {task['state']}")
        wt = self.fake_worktree(task_id)
        self.store.transition(task_id, expected_state="AWAITING_START", new_state="STARTING",
                              expected_version=task["version"],
                              fields={"base_sha": "b" * 40, "branch_name": f"chief/{task_id.lower()}", "worktree_path": str(wt)})
        return self._launch(self.store.get_task(task_id), wt, "b" * 40, previous=None)

    def _launch(self, task: dict[str, Any], worktree: Path, base_sha: str, *, previous: dict[str, Any] | None) -> dict[str, Any]:
        run = self.store.create_run(task["id"], unit_name="sim-" + task["id"].lower(), requested_model="sim", boot_id="sim")
        self.store.update_run(run["id"], state="RUNNING")
        self.log.add("worker_start", task["id"], attempt=run["attempt"], retry=previous is not None,
                     previous_attempt=(previous or {}).get("attempt"))
        return self.store.transition(task["id"], expected_state="STARTING", new_state="RUNNING")

    def reconcile(self, task_id: str) -> str:
        task = self.store.get_task(task_id)
        queue = self.outcomes.get(task_id) or []
        if task["state"] == "RUNNING" and queue:
            out = queue.pop(0)
            run = self.store.latest_run(task_id)
            self.store.update_run(run["id"], state=out.get("run_state", "COMPLETED"), ended_at=utc_now(),
                                  result_json=out.get("result", {"classification": "SUCCESS"}))
            self.store.transition(task_id, expected_state="RUNNING", new_state=out["state"])
        return self.store.get_task(task_id)["state"]

    def stop(self, task_id: str) -> dict[str, Any]:
        self.stops.append(task_id)
        self.log.add("stop", task_id)
        return self.store.get_task(task_id)


class SimVerifier:
    def __init__(self, store: Any, log: ExecLog):
        self.store = store
        self.log = log
        self.outcome: dict[str, str] = {}

    def verify(self, task_id: str) -> dict[str, Any]:
        self.log.add("verify", task_id)
        new_state = self.outcome.get(task_id, "READY")
        return self.store.transition(task_id, expected_state="VERIFYING", new_state=new_state)

    def cancel(self, task_id: str) -> None:
        self.log.add("verify_cancel", task_id)


class SimRelease(base.ReleaseRecorder):
    def __init__(self, settings: Any, store: Any, rec: Any, log: ExecLog):
        super().__init__(settings, store, rec)
        self.log = log

    def execute(self, approval: dict) -> dict:
        self.log.add("release", approval.get("task_id"), action=approval.get("action"), approval=approval.get("id"))
        raise RuntimeError("sim: release recorded, not executed")


class SyncExecutor:
    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True) -> None:
        return None


class FakeTG450(H.FakeTG):
    def __init__(self, rec: H.Rec):
        super().__init__(rec)
        self.files: dict[str, dict[str, Any]] = {}
        self.get_file_calls = 0
        self.download_calls = 0
        self.send_failures: list[TelegramError] = []
        self.call_failures: list[TelegramError] = []
        self.edit_failures: list[TelegramError] = []
        self.answer_failures: list[TelegramError] = []
        self.call_attempts = 0

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None, **kw: Any) -> dict:
        if self.send_failures:
            raise self.send_failures.pop(0)
        return super().send_message(chat_id, text, reply_markup, **kw)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None, **kw: Any) -> dict:
        if self.edit_failures:
            raise self.edit_failures.pop(0)
        return super().edit_message_text(chat_id, message_id, text, reply_markup, **kw)

    def call_with_retry(self, method: str, payload: dict, attempts: int = 1) -> dict:
        self.call_attempts += 1
        if self.call_failures:
            raise self.call_failures.pop(0)
        return super().call_with_retry(method, payload, attempts)

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        if self.answer_failures:
            raise self.answer_failures.pop(0)
        return super().answer_callback(callback_query_id, text)

    def get_file(self, file_id: str) -> dict[str, Any]:
        self.get_file_calls += 1
        f = self.files[file_id]
        return {"file_id": file_id, "file_size": f.get("reported", len(f["data"])), "file_path": f.get("path", f"documents/{file_id}.bin")}

    def download_file(self, file_path: str, max_bytes: int) -> bytes:
        self.download_calls += 1
        for f in self.files.values():
            if f.get("path", "") == file_path or file_path.endswith(f"/{f.get('id', '')}.bin"):
                if f.get("fail"):
                    raise TelegramError("Telegram download failed: URLError")
                data = f["data"]
                if len(data) > max_bytes:
                    raise TelegramError("File exceeds the size limit", error_code=413)
                return data
        raise TelegramError("unknown file")


class CapturingClient:
    """대화 모델 대역(또는 실제 CLI 래퍼). 입력 전부를 기록한다."""

    def __init__(self, inner: Any = None, action: dict[str, Any] | None = None, reply: str = "(스텁) 확인했습니다."):
        self.inner = inner
        self.action = action or {"type": "none"}
        self.reply = reply
        self.inputs: list[str] = []
        self.calls = 0

    def complete(self, **kw: Any) -> Any:
        self.calls += 1
        self.inputs.append(json.dumps({k: v for k, v in kw.items() if k != "api_key"}, ensure_ascii=False))
        if self.inner is not None:
            return self.inner.complete(**kw)
        return base.ClaudeResult(data={"reply": self.reply, "action": {**self.action}}, model="stub")


class RecordingHub:
    """HubTodo 의 subprocess 대역: argv 만 기록하고 아무것도 실행하지 않는다."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        sub = argv[3] if len(argv) > 3 else ""
        out = "할 일: 20261009-120000-deskmini-codex" if sub == "add" else ("1. [codex] 예시 할 일" if sub == "list" else "닫음: x")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def wire(bot: ChiefBot, rec: H.Rec, tg: FakeTG450, log: ExecLog, info: dict[str, Any], chat_client: Any) -> dict[str, Any]:
    """Env300.new_bot 이 만든 봇(또는 재시작한 봇)에 4라운드용 대역을 끼운다."""
    from dataclasses import replace

    from app.chat import ChatConfig, ChatResponder
    from app.context import WorkspacePaths

    bot.executor = SyncExecutor()  # type: ignore[assignment]
    bot.telegram = tg  # type: ignore[assignment]
    sim_runner = SimRunner(bot.settings, bot.store, log, info["case_dir"])
    bot.runner = sim_runner  # type: ignore[assignment]
    verifier = SimVerifier(bot.store, log)
    bot.verifier = verifier  # type: ignore[assignment]
    bot.release = SimRelease(bot.settings, bot.store, rec, log)  # type: ignore[assignment]
    guard = base.InterpreterGuard(rec)  # type: ignore[arg-type]
    bot.gemini = guard  # type: ignore[assignment]
    bot.policy.interpreter = guard  # type: ignore[assignment]
    bot.paths = WorkspacePaths(home=info["home"], agent_hub=info["home"] / "agent-hub", work=info["work"])
    holder = info.get("clock")
    if holder is not None:
        bot.clock = lambda: holder["now"]
    cfg = replace(ChatConfig.load(H.ROOT / "config" / "models.toml"), providers=("claude",))
    bot.chat_config = cfg
    bot.chat = ChatResponder(cfg, env_path=bot.settings.env_path, gemini=guard, claude=chat_client)  # type: ignore[arg-type]
    bot.observer = lambda now: (_ for _ in ()).throw(RuntimeError("sim: observer"))  # type: ignore[assignment]
    hub = RecordingHub()
    from app.todo import HubTodo
    bot._hub_todo = HubTodo(info["home"] / "agent-hub", runner=hub)  # type: ignore[attr-defined]
    (info["home"] / "agent-hub" / "bin").mkdir(parents=True, exist_ok=True)
    (info["home"] / "agent-hub" / "bin" / "hub").write_text("#!/bin/false\n")
    return {"runner": sim_runner, "verifier": verifier, "hub": hub}


def restart(bot: ChiefBot, rec: H.Rec, tg: FakeTG450, log: ExecLog, info: dict[str, Any], chat_client: Any,
            env: H.Env300) -> tuple[ChiefBot, dict[str, Any]]:
    new = ChiefBot(bot.settings)
    new.executor.shutdown(wait=False)
    parts = wire(new, rec, tg, log, info, chat_client)
    with H._SNAP_LOCK:
        H._SNAPSHOTS.pop(str(info["home"]), None)
    env.register_snapshot(new, info.get("host", "healthy"))
    return new, parts


def tick(bot: ChiefBot) -> None:
    bot._process_futures()
    bot._reconcile_tasks()
    bot._run_scheduled_retries()
    bot._flush_outbox()


def msg_update(update_id: int, text: str | None = None, *, chat_id: int = CHAT, user_id: int = CHAT,
               chat_type: str = "private", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {"message_id": update_id, "chat": {"type": chat_type, "id": chat_id}, "from": {"id": user_id}}
    if text is not None:
        m["text"] = text
    m.update(extra or {})
    return {"update_id": update_id, "message": m}


def cb_update(update_id: int, data: str, *, user_id: int = CHAT, chat_id: int = CHAT, chat_type: str = "private") -> dict[str, Any]:
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "data": data, "from": {"id": user_id},
                                                        "message": {"message_id": 1, "chat": {"type": chat_type, "id": chat_id}}}}


def db(bot: ChiefBot) -> sqlite3.Connection:
    con = sqlite3.connect(bot.settings.db_path)
    con.row_factory = sqlite3.Row
    return con


def make_task(bot: ChiefBot, *, state: str = "FAILED", project: str = "sandbox", runs: list[dict[str, Any]] | None = None,
              evidence: dict[str, Any] | None = None, retries: int = 1, worktree: Path | None = None) -> str:
    """원하는 상태의 작업을 DB 에 직접 만든다(실행 기록·검증 증거 포함)."""
    task = bot.store.create_task(owner_user_id=CHAT, chat_id=CHAT, project_id=project, request_text="sim 작업",
                                 specification={"objective": "sim 작업", "output_format": "코드 변경", "boundaries": [], "acceptance": []},
                                 policy_hash=bot.policy.policy_hash, budget={"runtime_seconds": 60, "retries": retries})
    tid = task["id"]
    wt = worktree if worktree is not None else bot.runner.fake_worktree(tid)  # type: ignore[attr-defined]
    for r in runs or []:
        run = bot.store.create_run(tid, unit_name="sim", requested_model="sim", boot_id="sim")
        bot.store.update_run(run["id"], state=r.get("run_state", "COMPLETED"), ended_at=r.get("ended_at", utc_now()),
                             result_json=r.get("result", {"classification": "SUCCESS"}))
    with db(bot) as con:
        con.execute("UPDATE tasks SET state=?, version=version+3, base_sha=?, branch_name=?, worktree_path=? WHERE id=?",
                    (state, "b" * 40, f"chief/{tid.lower()}", str(wt), tid))
    if evidence is not None:
        p = bot.settings.state_dir / "evidence" / f"{tid}-verification.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(evidence, ensure_ascii=False))
    return tid


def expire_approval(bot: ChiefBot, approval_id: str) -> None:
    with db(bot) as con:
        con.execute("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (approval_id,))


def approval_ids(bot: ChiefBot, task_id: str, action: str | None = None) -> list[str]:
    with db(bot) as con:
        q = "SELECT id FROM approvals WHERE task_id=?" + (" AND action=?" if action else "") + " ORDER BY created_at, rowid"
        return [r["id"] for r in con.execute(q, (task_id, action) if action else (task_id,))]


def consumed(bot: ChiefBot, approval_id: str) -> bool:
    with db(bot) as con:
        row = con.execute("SELECT consumed_at FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return bool(row and row["consumed_at"])


def scheduled(bot: ChiefBot, task_id: str) -> list[dict[str, Any]]:
    with db(bot) as con:
        return [dict(r) for r in con.execute("SELECT * FROM scheduled_retries WHERE task_id=? ORDER BY id", (task_id,))]


def buttons_of(rec: H.Rec) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in rec.messages:
        out += H.button_list(m)
    return out


def last_button(rec: H.Rec, prefix: str) -> str | None:
    for label, data in reversed(buttons_of(rec)):
        if data.startswith(prefix):
            return data
    return None


def texts(rec: H.Rec, since: int = 0) -> str:
    return "\n".join((m.get("text") or "") for m in rec.messages[since:])


def jpeg(extra: bytes = b"") -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + IMG_SENTINEL + extra + b"\x00" * 64 + b"\xff\xd9"


def png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + IMG_SENTINEL + b"\x00" * 64
