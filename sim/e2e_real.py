"""실제 처리 경로 완주 시험(가짜 없음): 발주 → 착수 승인 → systemd-run + bwrap + claude 작업자 → 격리 검증 →
독립 검토(Claude CLI, 구독) → READY → 병합 승인 → sandbox main fast-forward.

- 별도 상태 폴더(~/.local/state/deskmini-chief-e2e/<시각>)를 써서 운영 봇 DB·작업에 영향이 없다.
- Telegram 은 기록기로 바꾼다(실제 API 호출 없음). Gemini 는 호출되면 예외가 나는 가드로 바꾼다.
- 대상은 sandbox 프로젝트만 허용한다. 승인 버튼은 봇이 띄운 카드의 callback_data 를 그대로 눌러서 확인한다.

사용:
  XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python sim/e2e_real.py "할 일" ["할 일2" ...] [--no-merge]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.claude_cli import ClaudeCliReviewer  # noqa: E402
from app.bot import ChiefBot  # noqa: E402
from app.config import Settings, load_projects  # noqa: E402

CHAT = 4242
TERMINAL = {"READY", "FAILED", "BLOCKED", "CANCELLED", "INTERRUPTED"}


class RecTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def _rec(self, text: str, reply_markup: Any) -> dict[str, Any]:
        self.sent.append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "text": text, "markup": reply_markup})
        return {"message_id": len(self.sent)}

    def send_message(self, chat_id: int, text: str, *, reply_markup: Any = None, parse_mode: str = "HTML") -> dict[str, Any]:
        return self._rec(text, reply_markup)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, *, reply_markup: Any = None, parse_mode: str = "HTML") -> dict[str, Any]:
        return self._rec("[edit] " + text, reply_markup)

    def call_with_retry(self, method: str, payload: dict[str, Any], attempts: int = 1) -> dict[str, Any]:
        return self._rec(str(payload.get("text")), payload.get("reply_markup"))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None

    files: dict[str, bytes] = {}

    def get_file(self, file_id: str) -> dict[str, Any]:
        data = self.files[file_id]
        return {"file_id": file_id, "file_size": len(data), "file_path": f"documents/{file_id}.bin"}

    def download_file(self, file_path: str, max_bytes: int) -> bytes:
        data = self.files[file_path.split("/")[-1].removesuffix(".bin")]
        if len(data) > max_bytes:
            raise RuntimeError("too large")
        return data

    def buttons(self) -> list[str]:
        out = []
        for item in self.sent:
            for row in (item.get("markup") or {}).get("inline_keyboard", []):
                out.extend(button["callback_data"] for button in row)
        return out


class NoGemini:
    model = "none"

    def available(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"e2e: Gemini must not be called ({name})")


def message(update_id: int, text: str) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"type": "private", "id": CHAT}, "from": {"id": CHAT}, "text": text}}


def callback(update_id: int, data: str) -> dict[str, Any]:
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "data": data, "from": {"id": CHAT},
                                                        "message": {"chat": {"type": "private", "id": CHAT}}}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("requests", nargs="+")
    parser.add_argument("--no-merge", action="store_true")
    parser.add_argument("--state", default="")
    parser.add_argument("--attach", default="", help="첫 요청에 문서로 붙일 로컬 파일(캡션 = /new sandbox: 요청)")
    parser.add_argument("--retry", default="", help="이미 있는 상태 폴더의 FAILED/BLOCKED 작업 ID 를 [다시 시도] 로 재실행")
    args = parser.parse_args()

    state = Path(args.state) if args.state else Path.home() / ".local/state/deskmini-chief-e2e" / datetime.now().strftime("%Y%m%d-%H%M%S")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    for child in ("tasks", "evidence", "logs", "backups", "worktrees"):
        (state / child).mkdir(exist_ok=True, mode=0o700)
    projects = {k: v for k, v in load_projects(ROOT / "config" / "projects.toml", state).items() if k == "sandbox"}
    if "sandbox" not in projects:
        raise SystemExit("sandbox project is not registered")
    settings = Settings(
        root=ROOT, state_dir=state, db_path=state / "chief.sqlite", telegram_token="e2e-offline",
        allowed_chat_id=CHAT, allowed_user_id=CHAT, telegram_timeout_seconds=1,
        gemini_script=state / "no-gemini.py", max_runtime_seconds=900, max_active_tasks=1,
        approval_ttl_seconds=600, projects=projects, env_path=state / "none.env",
    )
    bot = ChiefBot(settings)
    tg = RecTelegram()
    bot.telegram = tg  # type: ignore[assignment]
    guard = NoGemini()
    bot.gemini = guard  # type: ignore[assignment]
    bot.policy.interpreter = guard  # type: ignore[assignment]
    reviewer = ClaudeCliReviewer(work_root=state / "review")
    bot.verifier.reviewer = reviewer
    update = [int(time.time())]

    def send(update_obj: dict[str, Any]) -> None:
        bot._handle_update(update_obj)

    def press(prefix: str) -> bool:
        for data in reversed(tg.buttons()):
            if data.startswith(prefix):
                update[0] += 1
                send(callback(update[0], data))
                return True
        return False

    def pump(task_id: str, until: set[str], limit: float = 1200) -> str:
        started = time.monotonic()
        while time.monotonic() - started < limit:
            bot._process_futures()
            bot._reconcile_tasks()
            state_now = bot.store.get_task(task_id)["state"]
            if state_now in until and not bot.verification_futures and not bot.release_futures:
                bot._process_futures()
                return state_now
            time.sleep(3)
        return bot.store.get_task(task_id)["state"]

    report: list[dict[str, Any]] = []
    jobs: list[tuple[str, str | None]] = [(text, None) for text in args.requests]
    if args.retry:
        jobs = [("", args.retry)]
    for text, retry_id in jobs:
        t0 = time.monotonic()
        if retry_id:
            task_id = retry_id
            bot._offer_retry(task_id)
            if not press("approve:retry:"):
                print("no retry button", tg.sent[-1]["text"][:300])
                return 2
        elif args.attach and text == args.requests[0]:
            source = Path(args.attach)
            tg.files["f1"] = source.read_bytes()
            update[0] += 1
            send({"update_id": update[0], "message": {
                "chat": {"type": "private", "id": CHAT}, "from": {"id": CHAT},
                "document": {"file_id": "f1", "file_name": source.name, "mime_type": "text/csv", "file_size": source.stat().st_size},
                "caption": f"/new sandbox: {text}"}})
            task_id = bot.store.list_tasks(1)[0]["id"]
            if not press("approve:start:"):
                print("no start button", tg.sent[-1]["text"][:500])
                return 2
        else:
            update[0] += 1
            send(message(update[0], f"/new sandbox: {text}"))
            task_id = bot.store.list_tasks(1)[0]["id"]
            if not press("approve:start:"):
                print("no start button", tg.sent[-1]["text"][:300])
                return 2
        final = pump(task_id, TERMINAL)
        merge_state = None
        if final == "READY" and not args.no_merge:
            if press("approve:merge:"):
                pump(task_id, {"READY"}, 120)
                ops = [op for op in bot.store.list_operations_for_task(task_id)] if hasattr(bot.store, "list_operations_for_task") else []
                merge_state = ops[-1]["state"] if ops else None
        events = bot.store.recent_events(task_id, 60)
        run = bot.store.latest_run(task_id) or {}
        result = run.get("result") or {}
        evidence_path = state / "evidence" / f"{task_id}-verification.json"
        evidence = json.loads(evidence_path.read_text()) if evidence_path.is_file() else {}
        report.append({
            "task_id": task_id,
            "request": text or f"retry {retry_id}",
            "final": final,
            "merge": merge_state,
            "seconds": round(time.monotonic() - t0, 1),
            "attempt": run.get("attempt"),
            "states": [e["payload"].get("to") for e in reversed(events) if e["event_type"] == "STATE_CHANGED"],
            "state_times": [(e["created_at"][11:19], e["payload"].get("to")) for e in reversed(events) if e["event_type"] == "STATE_CHANGED"],
            "worker": {k: result.get(k) for k in ("classification", "exit_code", "duration_seconds", "actual_model")},
            "worker_error": (result.get("error") or "")[:400],
            "verdict": evidence.get("verdict"), "reason": evidence.get("reason"),
            "checks": [(c.get("argv"), c.get("passed"), (c.get("output") or "")[-300:]) for c in evidence.get("checks", [])],
            "review": evidence.get("review") or {k: evidence.get(k) for k in ("review_verdict", "review_summary")},
        })
        print(json.dumps(report[-1], ensure_ascii=False, indent=1), flush=True)
    (state / "e2e_report.json").write_text(json.dumps({"report": report, "messages": tg.sent, "review_calls": reviewer.calls},
                                                      ensure_ascii=False, indent=1))
    print("state:", state, "review_calls:", reviewer.calls)
    bot.executor.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
