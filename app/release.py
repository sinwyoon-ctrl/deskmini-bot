from __future__ import annotations

import json
import hashlib
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.collector import run_fixed
from app.config import ProjectConfig, Settings
from app.redact import contains_secret
from app.state import StateStore


SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseError(RuntimeError):
    pass


class ReleaseRefused(ReleaseError):
    """정책상 승인 카드를 만들지 않는 경우. 메시지는 사용자에게 그대로 보여 줄 한국어 문장이다."""


class ReleaseManager:
    def __init__(self, settings: Settings, store: StateStore):
        self.settings = settings
        self.store = store

    def approval_payload(self, task_id: str, action: str) -> dict[str, Any]:
        if action not in {"merge", "push", "deploy"}:
            raise ReleaseError("Unsupported release action")
        task = self.store.get_task(task_id)
        project = self.settings.projects[task["project_id"]]
        if action == "deploy":
            if not project.deploy_enabled:
                raise ReleaseRefused(
                    f"{task['project_id']} 프로젝트는 배포가 꺼져 있어(deploy_enabled=false) 배포 승인 카드를 만들지 않습니다."
                )
            if project.deploy_environment != "staging":
                raise ReleaseRefused("배포는 스테이징만 허용되어 배포 승인 카드를 만들지 않습니다.")
        if task["state"] != "READY" or not SHA_RE.fullmatch(task.get("head_sha") or ""):
            raise ReleaseError("Only a verified READY task can be released")
        payload = {
            "task_id": task_id,
            "task_version": task["version"],
            "project_id": task["project_id"],
            "action": action,
            "base_sha": task["base_sha"],
            "head_sha": task["head_sha"],
        }
        if action == "push":
            payload.update(
                {
                    "remote": "origin",
                    "remote_fingerprint": self._remote_fingerprint(project),
                    "ref": f"refs/heads/{project.default_branch}",
                }
            )
        if action == "deploy":
            payload.update(
                {
                    "environment": project.deploy_environment,
                    "deploy_enabled": project.deploy_enabled,
                    "remote_fingerprint": self._remote_fingerprint(project),
                }
            )
        return payload

    def execute(self, approval: dict[str, Any]) -> dict[str, Any]:
        action = approval["action"]
        payload = approval["payload"]
        task = self.store.get_task(approval["task_id"])
        self._validate_payload(task, action, payload)
        project = self.settings.projects[task["project_id"]]
        if action == "merge":
            return self._merge(task, project, approval)
        if action == "push":
            return self._push(task, project, approval)
        if action == "deploy":
            return self._deploy(task, project, approval)
        raise ReleaseError("Unsupported release action")

    def _merge(
        self,
        task: dict[str, Any],
        project: ProjectConfig,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        with self._release_lock():
            branch = self._git(project, "branch", "--show-current")
            status = self._git(project, "status", "--porcelain=v1")
            before = self._sha(project, "HEAD")
            before_evidence = {"branch": branch, "clean": not status, "main_sha": before}
            operation = self.store.create_operation(
                task_id=task["id"],
                kind="merge",
                approval_id=approval["id"],
                target={"repo": str(project.repo_path), "branch": project.default_branch},
                expected_before={"main_sha": task["base_sha"]},
                desired_after={"main_sha": task["head_sha"]},
            )
            self.store.set_operation_before_evidence(operation["id"], before_evidence)
            if branch != project.default_branch or status or before != task["base_sha"]:
                return self.store.finish_operation(
                    operation["id"],
                    state="FAILED",
                    before_evidence=before_evidence,
                    after_evidence={"reason": "repository state changed; merge not attempted"},
                )
            result = run_fixed(
                ("git", "merge", "--ff-only", task["head_sha"]),
                cwd=project.repo_path,
                timeout=30,
            )
            after = self._sha(project, "HEAD")
            success = result.ok and after == task["head_sha"]
            final = self.store.finish_operation(
                operation["id"],
                state="SUCCEEDED" if success else "FAILED",
                before_evidence=before_evidence,
                after_evidence={"main_sha": after, "command_ok": result.ok, "output": result.stdout},
            )
            self.store.add_event(task["id"], None, "MERGE_FINISHED", "release", final)
            return final

    def _push(
        self,
        task: dict[str, Any],
        project: ProjectConfig,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        with self._release_lock():
            branch = self._git(project, "branch", "--show-current")
            local_sha = self._sha(project, "HEAD")
            push_url = self._push_url(project)
            remote_before = self._remote_sha(project, push_url)
            remote_fingerprint = hashlib.sha256(push_url.encode("utf-8")).hexdigest()
            operation = self.store.create_operation(
                task_id=task["id"],
                kind="push",
                approval_id=approval["id"],
                target={
                    "remote": "origin",
                    "remote_fingerprint": remote_fingerprint,
                    "ref": f"refs/heads/{project.default_branch}",
                },
                expected_before={"local_sha": task["head_sha"]},
                desired_after={"remote_sha": task["head_sha"]},
            )
            self.store.set_operation_before_evidence(
                operation["id"],
                {
                    "branch": branch,
                    "local_sha": local_sha,
                    "remote_sha": remote_before,
                    "remote_fingerprint": remote_fingerprint,
                },
            )
            if (
                branch != project.default_branch
                or local_sha != task["head_sha"]
                or remote_fingerprint != approval["payload"]["remote_fingerprint"]
            ):
                return self.store.finish_operation(
                    operation["id"],
                    state="FAILED",
                    before_evidence={
                        "branch": branch,
                        "local_sha": local_sha,
                        "remote_sha": remote_before,
                        "remote_fingerprint": remote_fingerprint,
                    },
                    after_evidence={"reason": "local branch, SHA, or remote changed after approval"},
                )
            result = run_fixed(
                (
                    "git",
                    "push",
                    "--porcelain",
                    push_url,
                    f"{task['head_sha']}:refs/heads/{project.default_branch}",
                ),
                cwd=project.repo_path,
                timeout=90,
            )
            remote_after = self._remote_sha(project, push_url)
            success = result.ok and remote_after == task["head_sha"]
            final = self.store.finish_operation(
                operation["id"],
                state="SUCCEEDED" if success else "FAILED",
                before_evidence={
                    "branch": branch,
                    "local_sha": local_sha,
                    "remote_sha": remote_before,
                    "remote_fingerprint": remote_fingerprint,
                },
                after_evidence={"remote_sha": remote_after, "command_ok": result.ok, "output": result.stdout},
            )
            self.store.add_event(task["id"], None, "PUSH_FINISHED", "release", final)
            return final

    def _deploy(
        self,
        task: dict[str, Any],
        project: ProjectConfig,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        with self._release_lock():
            operation = self.store.create_operation(
                task_id=task["id"],
                kind="deploy",
                approval_id=approval["id"],
                target={"environment": project.deploy_environment},
                expected_before={"approved_sha": task["head_sha"]},
                desired_after={"healthy_sha": task["head_sha"]},
            )
            if (
                not project.deploy_enabled
                or project.deploy_environment != "staging"
                or not project.deploy_command
                or not project.healthcheck_command
                or not project.rollback_command
                or not project.deployed_version_command
            ):
                return self.store.finish_operation(
                    operation["id"],
                    state="FAILED",
                    before_evidence={"deploy_enabled": project.deploy_enabled},
                    after_evidence={"reason": "staging deployment and rollback are not fully registered"},
                )
            local_sha = self._sha(project, "HEAD")
            push_url = self._push_url(project)
            remote_sha = self._remote_sha(project, push_url)
            remote_fingerprint = self._remote_fingerprint(project)
            before_version = self._deployed_version(project)
            if (
                local_sha != task["head_sha"]
                or remote_sha != task["head_sha"]
                or remote_fingerprint != approval["payload"]["remote_fingerprint"]
                or before_version is None
            ):
                return self.store.finish_operation(
                    operation["id"],
                    state="FAILED",
                    before_evidence={
                        "local_sha": local_sha,
                        "remote_sha": remote_sha,
                        "remote_fingerprint": remote_fingerprint,
                    },
                    after_evidence={
                        "reason": "local SHA, remote SHA, remote target, or current staging version is unproven"
                    },
                )
            self.store.set_operation_before_evidence(
                operation["id"],
                {
                    "local_sha": local_sha,
                    "remote_sha": remote_sha,
                    "remote_fingerprint": remote_fingerprint,
                    "deployed_sha": before_version,
                },
            )
            deployed = run_fixed(project.deploy_command, cwd=project.repo_path, timeout=180)
            health = run_fixed(project.healthcheck_command, cwd=project.repo_path, timeout=30)
            after_version = self._deployed_version(project)
            success = deployed.ok and health.ok and after_version == task["head_sha"]
            rollback_attempted = False
            rollback_command_ok = False
            rollback_healthcheck_ok = False
            rollback_version: str | None = None
            rollback_succeeded = False
            rollback_output = ""
            if not success:
                rollback_attempted = True
                rolled_back = run_fixed(project.rollback_command, cwd=project.repo_path, timeout=180)
                rollback_health = run_fixed(project.healthcheck_command, cwd=project.repo_path, timeout=30)
                rollback_version = self._deployed_version(project)
                rollback_command_ok = rolled_back.ok
                rollback_healthcheck_ok = rollback_health.ok
                rollback_succeeded = (
                    rolled_back.ok
                    and rollback_health.ok
                    and before_version is not None
                    and rollback_version == before_version
                )
                rollback_output = rolled_back.stdout
            final = self.store.finish_operation(
                operation["id"],
                state="SUCCEEDED" if success else "FAILED",
                before_evidence={
                    "local_sha": local_sha,
                    "remote_sha": remote_sha,
                    "deployed_sha": before_version,
                },
                after_evidence={
                    "deploy_command_ok": deployed.ok,
                    "deploy_output": deployed.stdout,
                    "healthcheck_ok": health.ok,
                    "healthcheck_output": health.stdout,
                    "deployed_sha": after_version,
                    "rollback_performed": rollback_attempted,
                    "rollback_command_ok": rollback_command_ok,
                    "rollback_healthcheck_ok": rollback_healthcheck_ok,
                    "rollback_sha": rollback_version,
                    "rollback_succeeded": rollback_succeeded,
                    "rollback_output": rollback_output,
                },
            )
            self.store.add_event(task["id"], None, "DEPLOY_FINISHED", "release", final)
            return final

    def reconcile_interrupted(self, operation: dict[str, Any]) -> dict[str, Any]:
        task = self.store.get_task(operation["task_id"])
        project = self.settings.projects[task["project_id"]]
        desired = operation.get("desired_after") or {}
        before = operation.get("before_evidence") or {}
        evidence: dict[str, Any] = {"reason": "reconciled after bot restart"}
        state = "UNKNOWN"
        with self._release_lock():
            if operation["kind"] == "merge":
                current = self._sha(project, "HEAD")
                evidence["main_sha"] = current
                if current == desired.get("main_sha"):
                    state = "SUCCEEDED"
            elif operation["kind"] == "push":
                push_url = self._push_url(project)
                fingerprint = hashlib.sha256(push_url.encode("utf-8")).hexdigest()
                current = self._remote_sha(project, push_url)
                evidence.update({"remote_sha": current, "remote_fingerprint": fingerprint})
                if (
                    current == desired.get("remote_sha")
                    and fingerprint == (operation.get("target") or {}).get("remote_fingerprint")
                ):
                    state = "SUCCEEDED"
            elif operation["kind"] == "deploy" and project.deploy_environment == "staging":
                current = self._deployed_version(project)
                health = (
                    run_fixed(project.healthcheck_command, cwd=project.repo_path, timeout=30)
                    if project.healthcheck_command
                    else None
                )
                evidence.update(
                    {"deployed_sha": current, "healthcheck_ok": bool(health and health.ok)}
                )
                if current == desired.get("healthy_sha") and health and health.ok:
                    state = "SUCCEEDED"
                elif current == before.get("deployed_sha") and health and health.ok:
                    state = "FAILED"
            final = self.store.finish_operation(
                operation["id"],
                state=state,
                before_evidence=before,
                after_evidence=evidence,
            )
            self.store.add_event(
                task["id"], None, "OPERATION_RECONCILED", "recovery", final
            )
            return final

    def _validate_payload(self, task: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
        expected = self.approval_payload(task["id"], action)
        if payload != expected:
            raise ReleaseError("Approved payload no longer matches current task")

    def _git(self, project: ProjectConfig, *args: str) -> str:
        result = run_fixed(("git", *args), cwd=project.repo_path, timeout=20)
        if not result.ok:
            raise ReleaseError(result.stdout or "Git command failed")
        return result.stdout.strip()

    def _sha(self, project: ProjectConfig, ref: str) -> str:
        value = self._git(project, "rev-parse", ref)
        if not SHA_RE.fullmatch(value):
            raise ReleaseError("Invalid SHA")
        return value

    def _remote_sha(self, project: ProjectConfig, push_url: str | None = None) -> str | None:
        push_url = push_url or self._push_url(project)
        result = run_fixed(
            ("git", "ls-remote", push_url, f"refs/heads/{project.default_branch}"),
            cwd=project.repo_path,
            timeout=30,
        )
        if not result.ok or not result.stdout:
            return None
        value = result.stdout.split()[0]
        return value if SHA_RE.fullmatch(value) else None

    def _remote_fingerprint(self, project: ProjectConfig) -> str:
        return hashlib.sha256(self._push_url(project).encode("utf-8")).hexdigest()

    def _push_url(self, project: ProjectConfig) -> str:
        try:
            result = subprocess.run(
                ("git", "remote", "get-url", "--push", "--all", "origin"),
                cwd=project.repo_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError(type(exc).__name__) from exc
        remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or len(remotes) != 1:
            raise ReleaseError("Exactly one origin push URL is required")
        remote = remotes[0]
        if re.search(r"(?i)^https?://[^/@]+@", remote) or contains_secret(remote):
            raise ReleaseError("Credentials must not be embedded in the push URL")
        return remote

    def _deployed_version(self, project: ProjectConfig) -> str | None:
        if not project.deployed_version_command:
            return None
        result = run_fixed(project.deployed_version_command, cwd=project.repo_path, timeout=30)
        value = result.stdout.strip().splitlines()[-1] if result.ok and result.stdout.strip() else ""
        return value if SHA_RE.fullmatch(value) else None

    @contextmanager
    def _release_lock(self) -> Iterator[None]:
        import fcntl

        path = self.settings.state_dir / "release.lock"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
