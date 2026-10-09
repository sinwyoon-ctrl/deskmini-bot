from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.collector import CommandResult, run_fixed
from app.config import ProjectConfig, Settings
from app.ids import TASK_ID_RE, canonical_task_id
from app.state import ConflictError, StateStore, utc_now


UNIT_RE = re.compile(r"^deskmini-chief-t-\d{8}-[0-9a-f]{6}$")


class RunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnitStatus:
    found: bool
    active_state: str
    sub_state: str
    result: str
    exit_code: int | None
    invocation_id: str | None
    query_ok: bool = True


def _git(project: ProjectConfig, *args: str, timeout: float = 20) -> CommandResult:
    return run_fixed(("git", *args), timeout=timeout, cwd=project.repo_path)


def _read_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


class TaskRunner:
    def __init__(self, settings: Settings, store: StateStore):
        self.settings = settings
        self.store = store

    def start(self, task_id: str) -> dict[str, Any]:
        task_id = canonical_task_id(task_id)
        if not TASK_ID_RE.fullmatch(task_id):
            raise RunnerError("Invalid task id")
        if self.store.active_task_count() >= self.settings.max_active_tasks:
            raise RunnerError("Another task is active")
        task = self.store.get_task(task_id)
        if task["state"] != "AWAITING_START":
            raise RunnerError(f"Task is {task['state']}")
        project = self.settings.projects[task["project_id"]]
        self._validate_repo(project)
        base_sha = self._base_sha(project)
        branch_name = f"chief/{task_id.lower()}"
        worktree = project.worktree_root / task_id
        if worktree.exists():
            raise RunnerError(f"Worktree already exists: {worktree}")
        project.worktree_root.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.store.transition(
            task_id,
            expected_state="AWAITING_START",
            new_state="STARTING",
            expected_version=task["version"],
            fields={
                "base_sha": base_sha,
                "branch_name": branch_name,
                "worktree_path": str(worktree),
            },
        )
        create = run_fixed(
            ("git", "worktree", "add", "-b", branch_name, str(worktree), base_sha),
            timeout=30,
            cwd=project.repo_path,
        )
        if not create.ok:
            self.store.transition(task_id, expected_state="STARTING", new_state="FAILED")
            raise RunnerError(f"Worktree creation failed: {create.stdout}")

        task_dir = self.settings.state_dir / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        prompt_path = task_dir / "mission.json"
        result_path = task_dir / "result.json"
        heartbeat_path = task_dir / "heartbeat.json"
        prompt_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "project_id": task["project_id"],
                    "original_request": task["request_text"],
                    "specification": task["specification"],
                    "base_sha": base_sha,
                    "rules": [
                        "worktree 밖 파일을 읽거나 수정하지 않는다",
                        "Bash·셸·네트워크·배포 도구를 사용하지 않는다",
                        "운영 사이트와 관리자 화면을 변경하지 않는다",
                        "요구 범위 밖 기능과 리팩터링을 추가하지 않는다",
                        "완료 시 변경 요약과 미검증 항목을 보고한다",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        unit_name = "deskmini-chief-" + task_id.lower()
        run = self.store.create_run(
            task_id,
            unit_name=unit_name,
            requested_model=self._requested_model(),
            boot_id=_read_boot_id(),
        )
        command = self._systemd_command(
            unit_name=unit_name,
            task_id=task_id,
            run_id=run["id"],
            worktree=worktree,
            mission=prompt_path,
            result=result_path,
            heartbeat=heartbeat_path,
        )
        launched = run_fixed(command, timeout=20)
        if not launched.ok:
            self.store.update_run(
                run["id"],
                state="FAILED",
                ended_at=utc_now(),
                result_json={"error": launched.stdout},
            )
            self.store.transition(task_id, expected_state="STARTING", new_state="FAILED")
            raise RunnerError(f"systemd launch failed: {launched.stdout}")
        status = self.unit_status(unit_name)
        self.store.update_run(
            run["id"],
            state="RUNNING",
            invocation_id=status.invocation_id,
            heartbeat_at=utc_now(),
        )
        started = self.store.transition(task_id, expected_state="STARTING", new_state="RUNNING")
        self.store.add_event(
            task_id,
            run["id"],
            "RUN_STARTED",
            "runner",
            {"unit": unit_name, "invocation_id": status.invocation_id},
        )
        return started

    def stop(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task["state"] not in {"STARTING", "RUNNING", "VERIFYING", "BLOCKED"}:
            raise RunnerError(f"Task cannot be stopped from {task['state']}")
        task = self.store.transition(
            task_id,
            expected_state=("STARTING", "RUNNING", "VERIFYING", "BLOCKED"),
            new_state="STOP_REQUESTED",
        )
        run = self.store.latest_run(task_id)
        if not run or not run.get("unit_name") or not UNIT_RE.fullmatch(run["unit_name"]):
            self.store.transition(task_id, expected_state="STOP_REQUESTED", new_state="INTERRUPTED")
            raise RunnerError("Managed unit not found")
        self.store.transition(task_id, expected_state="STOP_REQUESTED", new_state="STOPPING")
        stopped = run_fixed(
            ("systemctl", "--user", "stop", run["unit_name"]),
            timeout=20,
        )
        status = self.unit_status(run["unit_name"])
        if (
            not stopped.ok
            or not status.query_ok
            or status.active_state not in {"inactive", "failed", "not-found"}
        ):
            self.store.transition(task_id, expected_state="STOPPING", new_state="INTERRUPTED")
            raise RunnerError("Unit termination could not be confirmed")
        self.store.update_run(run["id"], state="CANCELLED", ended_at=utc_now())
        return self.store.transition(task_id, expected_state="STOPPING", new_state="CANCELLED")

    def unit_status(self, unit_name: str) -> UnitStatus:
        if not UNIT_RE.fullmatch(unit_name):
            raise RunnerError("Invalid unit name")
        result = run_fixed(
            (
                "systemctl",
                "--user",
                "show",
                unit_name,
                "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus,InvocationID",
                "--no-pager",
            ),
            timeout=3,
        )
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        found = values.get("LoadState") not in {None, "not-found"}
        query_ok = result.ok or values.get("LoadState") == "not-found"
        exit_raw = values.get("ExecMainStatus")
        return UnitStatus(
            found=found,
            active_state=values.get("ActiveState", "unknown"),
            sub_state=values.get("SubState", "unknown"),
            result=values.get("Result", "unknown"),
            exit_code=int(exit_raw) if exit_raw and exit_raw.isdigit() else None,
            invocation_id=values.get("InvocationID") or None,
            query_ok=query_ok,
        )

    def reconcile(self, task_id: str) -> str:
        task = self.store.get_task(task_id)
        run = self.store.latest_run(task_id)
        if task["state"] in {"STOP_REQUESTED", "STOPPING"}:
            if run and run.get("unit_name") and UNIT_RE.fullmatch(run["unit_name"]):
                stop_result = run_fixed(("systemctl", "--user", "stop", run["unit_name"]), timeout=20)
                status = self.unit_status(run["unit_name"])
                stopped = (
                    stop_result.ok
                    and status.query_ok
                    and status.active_state in {"inactive", "failed", "not-found"}
                )
            else:
                stopped = True
            if stopped:
                if run:
                    self.store.update_run(run["id"], state="CANCELLED", ended_at=utc_now())
                self.store.transition(
                    task_id,
                    expected_state=("STOP_REQUESTED", "STOPPING"),
                    new_state="CANCELLED",
                )
                self.store.add_event(
                    task_id, run["id"] if run else None, "STOP_RECOVERED", "recovery", {}
                )
                return "CANCELLED"
            self.store.transition(
                task_id,
                expected_state=("STOP_REQUESTED", "STOPPING"),
                new_state="INTERRUPTED",
            )
            return "INTERRUPTED"
        if not run:
            if task["state"] == "STARTING":
                self.store.transition(task_id, expected_state="STARTING", new_state="INTERRUPTED")
                self.store.add_event(
                    task_id,
                    None,
                    "START_INTERRUPTED",
                    "recovery",
                    {"reason": "no run record after restart"},
                )
                return "INTERRUPTED"
            return task["state"]
        unit_name = run.get("unit_name")
        status = self.unit_status(unit_name) if unit_name and UNIT_RE.fullmatch(unit_name) else None
        task_dir = self.settings.state_dir / "tasks" / task_id
        heartbeat_path = task_dir / "heartbeat.json"
        result_path = task_dir / "result.json"
        if heartbeat_path.is_file():
            try:
                heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                observed_at = (
                    heartbeat.get("observed_at")
                    if heartbeat.get("task_id") == task_id and heartbeat.get("run_id") == run["id"]
                    else None
                )
            except (OSError, json.JSONDecodeError):
                observed_at = None
            if isinstance(observed_at, str) and observed_at:
                self.store.update_run(run["id"], heartbeat_at=observed_at)
        if run.get("boot_id") != _read_boot_id() and not result_path.is_file():
            self.store.update_run(run["id"], state="INTERRUPTED", ended_at=utc_now())
            self.store.transition(task_id, expected_state=task["state"], new_state="INTERRUPTED")
            return "INTERRUPTED"
        if (
            status
            and status.active_state in {"active", "activating"}
            and run.get("invocation_id")
            and status.invocation_id
            and status.invocation_id != run["invocation_id"]
        ):
            self.store.update_run(run["id"], state="INTERRUPTED", ended_at=utc_now())
            self.store.transition(task_id, expected_state=task["state"], new_state="INTERRUPTED")
            return "INTERRUPTED"
        if status and status.active_state in {"active", "activating"}:
            return task["state"]
        if task["state"] not in {"RUNNING", "STARTING", "STOP_REQUESTED", "STOPPING"}:
            return task["state"]
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result = {"success": False, "error": "Invalid worker result"}
            if result.get("task_id") != task_id or result.get("run_id") != run["id"]:
                result = {
                    "success": False,
                    "classification": "INVALID_RESULT_IDENTITY",
                    "error": "Worker result identity did not match the registered run",
                }
            success = bool(result.get("success"))
            blocked = result.get("classification") == "LIMIT_BLOCKED"
            new_state = "VERIFYING" if success else ("BLOCKED" if blocked else "FAILED")
            self.store.update_run(
                run["id"],
                state="COMPLETED" if success else new_state,
                ended_at=utc_now(),
                exit_code=result.get("exit_code"),
                session_id=result.get("session_id"),
                actual_model=result.get("actual_model"),
                result_json=result,
                usage_json=result.get("usage"),
            )
            self.store.transition(task_id, expected_state=task["state"], new_state=new_state)
            self.store.add_event(task_id, run["id"], "RUN_FINISHED", "worker", result)
            return new_state
        self.store.update_run(run["id"], state="INTERRUPTED", ended_at=utc_now())
        self.store.transition(task_id, expected_state=task["state"], new_state="INTERRUPTED")
        return "INTERRUPTED"

    def _validate_repo(self, project: ProjectConfig) -> None:
        if not project.repo_path.is_dir():
            raise RunnerError(f"Repository missing: {project.repo_path}")
        inside = _git(project, "rev-parse", "--is-inside-work-tree")
        if not inside.ok or inside.stdout.strip() != "true":
            raise RunnerError("Registered path is not a Git worktree")
        branch = _git(project, "branch", "--show-current")
        if not branch.ok or branch.stdout.strip() != project.default_branch:
            raise RunnerError("Registered repository is not on its default branch")
        dirty = _git(project, "status", "--porcelain=v1")
        if not dirty.ok or dirty.stdout.strip():
            raise RunnerError("Registered repository has uncommitted changes")

    def _base_sha(self, project: ProjectConfig) -> str:
        result = _git(project, "rev-parse", "HEAD")
        if not result.ok or not re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()):
            raise RunnerError("Could not resolve base SHA")
        return result.stdout.strip()

    def _requested_model(self) -> str:
        path = self.settings.root / "config" / "models.toml"
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return str((data.get("claude") or {}).get("model", "sonnet"))

    def _systemd_command(
        self,
        *,
        unit_name: str,
        task_id: str,
        run_id: str,
        worktree: Path,
        mission: Path,
        result: Path,
        heartbeat: Path,
    ) -> tuple[str, ...]:
        command = (
            "systemd-run",
            "--user",
            f"--unit={unit_name}",
            "--collect",
            "--property=CPUQuota=600%",
            "--property=MemoryMax=16G",
            "--property=TasksMax=256",
            f"--property=RuntimeMaxSec={self.settings.max_runtime_seconds}",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=15s",
            "--property=NoNewPrivileges=yes",
            "--property=RestrictSUIDSGID=yes",
            "--property=LockPersonality=yes",
            "--property=UMask=0077",
            f"--property=WorkingDirectory={self.settings.root}",
            sys.executable,
            "-m",
            "app.worker",
            "--task-id",
            task_id,
            "--run-id",
            run_id,
            "--worktree",
            str(worktree),
            "--mission",
            str(mission),
            "--result",
            str(result),
            "--heartbeat",
            str(heartbeat),
            "--model",
            self._requested_model(),
            "--timeout",
            str(self.settings.max_runtime_seconds),
        )
        return command
