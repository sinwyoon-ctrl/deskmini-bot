"""검증자(450) 독립 실제 완주: sandbox 전용, 별도 상태 폴더, Telegram 기록기, Gemini 가드, 검토 = claude_cli.

1) 새 작업 A: 발주 → 착수 버튼 → 실제 작업자 → 검증 → 독립 검토(claude_cli) → READY → 병합 버튼
2) 새 작업 B: 작업자 모델 이름을 일부러 없는 이름으로 바꿔 실패 유도 → FAILED/BLOCKED 카드(이유 + [다시 시도])
3) 봇 재시작(같은 상태 폴더로 새 ChiefBot) → 모델 복구 → 카드의 [다시 시도] 를 두 번 누름(두 번째는 거절돼야 함)
   → 실제 작업자 2차 → READY → 병합 버튼
작업자 실행은 최대 3회(A 1, B 실패 1, B 재시도 1).

사용: XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python sim/e2e_verify450.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))

from adapters.claude_cli import ClaudeCliReviewer  # noqa: E402
from app.bot import ChiefBot  # noqa: E402
from app.config import Settings, load_projects  # noqa: E402
from e2e_real import CHAT, TERMINAL, NoGemini, RecTelegram, callback, message  # noqa: E402

TASK_A = "calc 패키지에 subtract(a, b) 함수(a-b 반환)를 추가하고 tests/test_calc.py 에 그 테스트를 추가"
TASK_B = "README 의 '이 문장에는 오타가 있습니다: ' 라는 군더더기 문구를 지워 '계산 함수는 calc 패키지에 있다. 덧셈 함수를 제공합니다.' 처럼 자연스럽게 고쳐"
BAD_MODEL = "claude-nonexistent-model-v450"


def make_bot(state: Path, tg: RecTelegram) -> tuple[ChiefBot, ClaudeCliReviewer]:
    projects = {k: v for k, v in load_projects(ROOT / "config" / "projects.toml", state).items() if k == "sandbox"}
    settings = Settings(
        root=ROOT, state_dir=state, db_path=state / "chief.sqlite", telegram_token="e2e-offline",
        allowed_chat_id=CHAT, allowed_user_id=CHAT, telegram_timeout_seconds=1,
        gemini_script=state / "no-gemini.py", max_runtime_seconds=900, max_active_tasks=1,
        approval_ttl_seconds=600, projects=projects, env_path=state / "none.env",
    )
    bot = ChiefBot(settings)
    bot.telegram = tg  # type: ignore[assignment]
    guard = NoGemini()
    bot.gemini = guard  # type: ignore[assignment]
    bot.policy.interpreter = guard  # type: ignore[assignment]
    reviewer = ClaudeCliReviewer(work_root=state / "review")
    bot.verifier.reviewer = reviewer
    return bot, reviewer


def main() -> int:
    state = Path.home() / ".local/state/deskmini-chief-e2e" / ("v450-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    for child in ("tasks", "evidence", "logs", "backups", "worktrees"):
        (state / child).mkdir(exist_ok=True, mode=0o700)
    tg = RecTelegram()
    bot, reviewer = make_bot(state, tg)
    upd = [int(time.time())]
    log: dict[str, Any] = {"state": str(state), "steps": []}
    worker_runs = 0
    review_calls = 0

    def send(obj: dict[str, Any]) -> None:
        bot._handle_update(obj)

    def press(prefix: str, *, which: str | None = None) -> str | None:
        for data in reversed(tg.buttons()):
            if data.startswith(prefix) and (which is None or data == which):
                upd[0] += 1
                send(callback(upd[0], data))
                return data
        return None

    def pump(task_id: str, until: set[str], limit: float = 1500) -> str:
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            bot._process_futures()
            bot._reconcile_tasks()
            now_state = bot.store.get_task(task_id)["state"]
            if now_state in until and not bot.verification_futures and not bot.release_futures:
                bot._process_futures()
                return now_state
            time.sleep(3)
        return bot.store.get_task(task_id)["state"]

    def summary(task_id: str, label: str, t0: float) -> dict[str, Any]:
        run = bot.store.latest_run(task_id) or {}
        result = run.get("result") or {}
        ev_path = state / "evidence" / f"{task_id}-verification.json"
        ev = json.loads(ev_path.read_text()) if ev_path.is_file() else {}
        ops = bot.store.list_operations_for_task(task_id)
        item = {
            "label": label, "task_id": task_id, "state": bot.store.get_task(task_id)["state"],
            "seconds": round(time.monotonic() - t0, 1), "attempt": run.get("attempt"),
            "runs": len(bot.store.list_runs(task_id)),
            "worker": {k: result.get(k) for k in ("classification", "exit_code", "duration_seconds", "actual_model")},
            "worker_error": str(result.get("error") or "")[:300],
            "verdict": ev.get("verdict"), "reason": ev.get("reason"),
            "review_verdict": ev.get("review_verdict"), "review_summary": str(ev.get("review_summary") or "")[:300],
            "checks": [(c.get("argv"), c.get("passed")) for c in ev.get("checks", [])],
            "operations": [(o["kind"], o["state"]) for o in ops],
        }
        log["steps"].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        return item

    # ---- A
    t0 = time.monotonic()
    upd[0] += 1
    send(message(upd[0], f"/new sandbox: {TASK_A}"))
    a_id = bot.store.list_tasks(1)[0]["id"]
    assert bot.store.get_task(a_id)["project_id"] == "sandbox"
    if not press("approve:start:"):
        print("no start button for A"); return 2
    worker_runs += 1
    final = pump(a_id, TERMINAL)
    if final == "READY":
        press("approve:merge:")
        pump(a_id, {"READY", "MERGED", "DONE", "PUSH_READY"}, 180)
    summary(a_id, "A fresh", t0)

    # ---- B (유도 실패)
    t0 = time.monotonic()
    real_model = bot.runner._requested_model
    bot.runner._requested_model = lambda: BAD_MODEL  # type: ignore[method-assign]
    upd[0] += 1
    send(message(upd[0], f"/new sandbox: {TASK_B}"))
    b_id = bot.store.list_tasks(1)[0]["id"]
    if not press("approve:start:"):
        print("no start button for B"); return 2
    worker_runs += 1
    final_b = pump(b_id, TERMINAL, 600)
    summary(b_id, "B induced failure", t0)
    retry_cards = [m for m in tg.sent if any(f"approve:retry:" in d or "approve:schedule_retry:" in d
                                               for row in (m.get("markup") or {}).get("inline_keyboard", [])
                                               for d in [b["callback_data"] for b in row])]
    log["failure_card"] = retry_cards[-1]["text"][:1500] if retry_cards else None
    print("failure card:", (log["failure_card"] or "NONE")[:600], flush=True)
    review_calls += reviewer.calls
    bot.runner._requested_model = real_model  # type: ignore[method-assign]
    del real_model

    # ---- 재시작
    bot.executor.shutdown(wait=True)
    bot, reviewer = make_bot(state, tg)
    bot._recover_state()
    log["after_restart_state"] = bot.store.get_task(b_id)["state"]
    retry_data = None
    for data in reversed(tg.buttons()):
        if data.startswith("approve:retry:"):
            retry_data = data
            break
    if retry_data is None:
        print("no retry button"); log["error"] = "no retry button"
    else:
        t0 = time.monotonic()
        n_before = len(tg.sent)
        press("approve:retry:", which=retry_data)
        worker_runs += 1
        n_mid = len(tg.sent)
        press("approve:retry:", which=retry_data)  # 같은 버튼 두 번째: 거절돼야 한다
        log["double_tap_reply"] = [m["text"][:200] for m in tg.sent[n_mid:]]
        log["runs_after_double_tap"] = len(bot.store.list_runs(b_id))
        final_b = pump(b_id, TERMINAL)
        if final_b == "READY":
            press("approve:merge:")
            pump(b_id, {"READY", "MERGED", "DONE", "PUSH_READY"}, 180)
        summary(b_id, "B retry after restart", t0)
        log["retry_messages"] = [m["text"][:300] for m in tg.sent[n_before:n_before + 3]]
    review_calls += reviewer.calls
    log["worker_runs"] = worker_runs
    log["review_calls"] = review_calls
    log["messages"] = tg.sent
    (state / "e2e_verify450.json").write_text(json.dumps(log, ensure_ascii=False, indent=1))
    out = ROOT / "sim" / "out" / "e2e_verify450.json"
    out.write_text(json.dumps({k: v for k, v in log.items() if k != "messages"}, ensure_ascii=False, indent=1))
    print("worker_runs", worker_runs, "review_calls", review_calls, "state", state)
    bot.executor.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
