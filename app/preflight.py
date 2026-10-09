from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from adapters.gemini_rest import GeminiInterpreter, InterpretationUnavailable
from adapters.telegram import TelegramAPI, TelegramError
from app.collector import collect_host_status, run_fixed
from app.config import Settings
from app.state import StateStore


def result(ok: bool, evidence: Any, blocking: bool = False) -> dict[str, Any]:
    return {"ok": ok, "blocking": blocking and not ok, "evidence": evidence}


def run_preflight(settings: Settings) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    python_path = shutil.which("python3") or shutil.which("python")
    checks["python"] = result(bool(python_path), {"executable": python_path}, blocking=True)
    for executable in ("git", "systemd-run", "systemctl", "bwrap", "claude", "tmux"):
        path = shutil.which(executable)
        checks[f"binary:{executable}"] = result(bool(path), {"path": path}, blocking=True)

    try:
        me = TelegramAPI(settings.telegram_token, request_timeout=15).get_me()
        checks["telegram"] = result(
            True,
            {"bot_id": me.get("id"), "username": me.get("username")},
        )
    except TelegramError as exc:
        checks["telegram"] = result(False, type(exc).__name__, blocking=True)

    gemini = GeminiInterpreter(settings.gemini_script)
    try:
        interpretation = gemini.interpret("/status", list(settings.projects))
        gemini_ok = interpretation.intent == "status"
        gemini_evidence: Any = {
            "script": str(settings.gemini_script),
            "intent": interpretation.intent,
        }
    except InterpretationUnavailable as exc:
        gemini_ok = False
        gemini_evidence = {"error": type(exc).__name__}
    checks["gemini_adapter"] = result(gemini_ok, gemini_evidence, blocking=True)

    for project_id, project in settings.projects.items():
        repo = project.repo_path
        git_status = run_fixed(("git", "status", "--porcelain=v1", "--branch"), cwd=repo, timeout=10)
        checks[f"project:{project_id}"] = result(
            repo.is_dir() and git_status.ok,
            {"repo": str(repo), "status": git_status.stdout},
            blocking=True,
        )

    systemd = run_fixed(("systemctl", "--user", "is-system-running"), timeout=5)
    checks["systemd_user"] = result(
        systemd.ok or systemd.stdout in {"running", "degraded"},
        systemd.stdout,
        blocking=True,
    )
    host = collect_host_status()
    StateStore(settings.db_path, settings.root / "schema.sql")
    checks["sqlite"] = result(settings.db_path.is_file(), {"path": str(settings.db_path)}, blocking=True)
    return {
        "passed": not any(item["blocking"] for item in checks.values()),
        "checks": checks,
        "host": host,
    }


def main() -> None:
    settings = Settings.load()
    report = run_preflight(settings)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
