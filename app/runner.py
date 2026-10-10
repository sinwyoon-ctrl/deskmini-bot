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
from app.redact import redact_text
from app.state import ConflictError, StateStore, utc_now


UNIT_RE = re.compile(r"^deskmini-chief-t-\d{8}-[0-9a-f]{6}$")
RETRYABLE_STATES = ("FAILED", "BLOCKED")
# 검증만 다시 하기(검토 불가 → 검증 재시도) 허용 횟수. 작업자 재시도 예산(budget.retries)과 따로 센다.
VERIFY_RETRY_LIMIT = 2


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
        return self._launch(self.store.get_task(task_id), worktree, base_sha, previous=None)

    # ------------------------------------------------------------------ 재시도

    def retry_budget(self, task: dict[str, Any]) -> tuple[int, int]:
        """(쓴 재시도 수, 허용 재시도 수). 한도 차단(LIMIT_BLOCKED)으로 끝난 실행은 토큰을 쓰지 않아 세지 않는다."""
        allowed = int((task.get("budget") or {}).get("retries", 1) or 0)
        runs = self.store.list_runs(task["id"])
        counted = [
            run
            for run in runs
            if ((run.get("result") or {}).get("classification") != "LIMIT_BLOCKED")
        ]
        return max(0, len(counted) - 1), allowed

    def retry_mode(self, task: dict[str, Any]) -> str:
        """'verify' = 작업자는 성공했고 검토만 못 한 경우(검증만 다시), 'run' = 작업자 재실행."""
        run = self.store.latest_run(task["id"])
        if not run or run.get("state") != "COMPLETED":
            return "run"
        if task["state"] == "BLOCKED":
            return "verify"
        evidence_path = self.settings.state_dir / "evidence" / f"{task['id']}-verification.json"
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "run"
        # 검토자가 판단하지 못한 경우(UNVERIFIED)는 작업자를 다시 돌리지 않고 검증만 다시 한다.
        return "verify" if evidence.get("review_verdict") == "UNVERIFIED" else "run"

    def verify_retry_budget(self, task: dict[str, Any]) -> tuple[int, int]:
        """(쓴 검증 재시도 수, 허용 수). 검증만 다시 하기도 검토 모델 호출 한 번이라 따로 작은 한도를 둔다."""
        return self.store.count_task_events(task["id"], "RETRY_VERIFY"), VERIFY_RETRY_LIMIT

    def can_retry(self, task: dict[str, Any]) -> tuple[bool, str]:
        if task["state"] not in RETRYABLE_STATES:
            return False, f"상태가 {task['state']} 라서 다시 시도할 수 없습니다"
        if self.retry_mode(task) == "verify":
            used, allowed = self.verify_retry_budget(task)
            if used >= allowed:
                return False, f"검증 재시도 한도({allowed}회)를 다 썼습니다. 검토 제공자 상태를 확인한 뒤 새로 발주해 주세요"
            return True, ""
        used, allowed = self.retry_budget(task)
        real_runs = [
            run for run in self.store.list_runs(task["id"])
            if (run.get("result") or {}).get("classification") != "LIMIT_BLOCKED"
        ]
        if real_runs and used >= allowed:
            return False, f"재시도 한도({allowed}회)를 다 썼습니다. 새로 발주해 주세요"
        return True, ""

    def retry(self, task_id: str, *, expected_version: int | None = None) -> dict[str, Any]:
        """FAILED/BLOCKED 작업의 새 실행 차수. 이전 worktree 가 있으면 이어서 쓰고(변경 보존),
        이전 실패 요약을 mission 에 넣는다. 작업자는 성공했고 검토만 못 했으면 검증만 다시 한다."""
        task_id = canonical_task_id(task_id)
        if not TASK_ID_RE.fullmatch(task_id):
            raise RunnerError("Invalid task id")
        task = self.store.get_task(task_id)
        allowed, reason = self.can_retry(task)
        if not allowed:
            raise RunnerError(reason)
        if expected_version is not None and int(task["version"]) != int(expected_version):
            raise RunnerError("Task changed after the retry was approved")
        if self.retry_mode(task) == "verify":
            verifying = self.store.transition(
                task_id,
                expected_state=task["state"],
                new_state="VERIFYING",
                expected_version=task["version"],
            )
            self.store.add_event(task_id, None, "RETRY_VERIFY", "runner", {})
            return verifying
        if self.store.active_task_count() >= self.settings.max_active_tasks:
            raise RunnerError("Another task is active")
        project = self.settings.projects[task["project_id"]]
        previous = self.store.latest_run(task_id)
        worktree = Path(task["worktree_path"]) if task.get("worktree_path") else project.worktree_root / task_id
        base_sha = task.get("base_sha")
        branch_name = task.get("branch_name") or f"chief/{task_id.lower()}"
        reuse = worktree.is_dir() and (worktree / ".git").is_file() and bool(base_sha)
        if not reuse:
            self._validate_repo(project)
            base_sha = self._base_sha(project)
            if worktree.exists():
                raise RunnerError(f"Worktree path exists but is not a usable worktree: {worktree}")
        self.store.transition(
            task_id,
            expected_state=task["state"],
            new_state="STARTING",
            expected_version=task["version"],
            fields={"base_sha": base_sha, "branch_name": branch_name, "worktree_path": str(worktree)},
        )
        if not reuse:
            project.worktree_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            exists = _git(project, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}").ok
            args = (
                ("git", "worktree", "add", str(worktree), branch_name)
                if exists
                else ("git", "worktree", "add", "-b", branch_name, str(worktree), str(base_sha))
            )
            create = run_fixed(args, timeout=30, cwd=project.repo_path)
            if not create.ok:
                self.store.transition(task_id, expected_state="STARTING", new_state="FAILED")
                raise RunnerError(f"Worktree creation failed: {create.stdout}")
        self.store.add_event(
            task_id, None, "RETRY_STARTED", "runner", {"reused_worktree": reuse, "previous_run": (previous or {}).get("id")}
        )
        return self._launch(self.store.get_task(task_id), worktree, str(base_sha), previous=previous)

    def _launch(
        self,
        task: dict[str, Any],
        worktree: Path,
        base_sha: str,
        *,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        task_id = task["id"]
        task_dir = self.settings.state_dir / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        prompt_path = task_dir / "mission.json"
        result_path = task_dir / "result.json"
        heartbeat_path = task_dir / "heartbeat.json"
        if previous is not None:
            # 이전 차수 결과는 보존하고, 새 차수 결과와 섞이지 않게 옮긴다.
            for path in (result_path, heartbeat_path):
                if path.is_file():
                    path.replace(path.with_name(f"{path.stem}-attempt{previous.get('attempt')}{path.suffix}"))
        try:
            attachments = self._stage_attachments(task, worktree)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.store.transition(task_id, expected_state="STARTING", new_state="FAILED")
            self.store.add_event(task_id, None, "ATTACHMENT_STAGING_FAILED", "runner", {"error": type(exc).__name__})
            raise RunnerError(f"Attachment staging failed: {type(exc).__name__}") from exc
        mission: dict[str, Any] = {
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
        }
        if attachments:
            mission["attachments"] = attachments
            mission["rules"].append(
                "첨부 파일은 .chief-inbox/ 에 읽기 전용으로 있다. 커밋되지 않으니 요구가 있을 때만 필요한 내용을 저장소 경로로 옮긴다"
            )
        if previous is not None:
            mission["previous_attempt"] = self._previous_summary(task_id, previous)
            mission["rules"].append(
                "이전 시도의 변경이 worktree 에 남아 있다. previous_attempt 의 실패 이유를 먼저 고친다"
            )
        prompt_path.write_text(json.dumps(mission, ensure_ascii=False, indent=2), encoding="utf-8")
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
            {"unit": unit_name, "invocation_id": status.invocation_id, "attempt": run["attempt"]},
        )
        return started

    def _previous_summary(self, task_id: str, previous: dict[str, Any]) -> dict[str, Any]:
        result = previous.get("result") or {}
        summary: dict[str, Any] = {
            "attempt": previous.get("attempt"),
            "worker_classification": result.get("classification"),
            "worker_error": redact_text(str(result.get("error") or ""), 1_500),
        }
        evidence_path = self.settings.state_dir / "evidence" / f"{task_id}-verification.json"
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence = {}
        if evidence.get("verdict") == "FAIL":
            summary["verification_reason"] = evidence.get("reason")
            summary["failed_checks"] = [
                {"argv": check.get("argv"), "output_tail": redact_text(str(check.get("output") or ""), 1_500)[-1_500:]}
                for check in evidence.get("checks", [])
                if not check.get("passed")
            ][:3]
            if evidence.get("review_findings"):
                summary["review_findings"] = evidence.get("review_findings")[:10]
        return summary

    def _stage_attachments(self, task: dict[str, Any], worktree: Path) -> list[dict[str, Any]]:
        """작업에 연결된 첨부를 worktree/.chief-inbox/ 로 복사(읽기 전용)하고 Git 제외 목록에 넣는다.
        텍스트 파일은 마스킹한 사본을 넣는다(작업자 LLM 이 원문 비밀값을 보지 않게)."""
        items = self.store.attachments_for_task(task["id"])
        if not items:
            return []
        from app.inbox import stage_into_worktree

        return stage_into_worktree(items, worktree, self.settings.state_dir / "inbox")

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
