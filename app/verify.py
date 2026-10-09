from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from adapters.gemini_rest import GeminiInterpreter, InterpretationUnavailable
from app.collector import run_fixed
from app.config import Settings
from app.redact import contains_secret, redact, redact_text
from app.state import StateStore, utc_now


GIT_SAFE_PREFIX = (
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
)


class VerificationError(RuntimeError):
    pass


class Verifier:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        reviewer: GeminiInterpreter,
    ):
        self.settings = settings
        self.store = store
        self.reviewer = reviewer
        self._cancelled: set[str] = set()
        self._cancel_lock = threading.Lock()
        self._commit_locks: dict[str, threading.Lock] = {}

    def cancel(self, task_id: str) -> None:
        lock = self._task_commit_lock(task_id)
        with lock:
            with self._cancel_lock:
                self._cancelled.add(task_id)
        task = self.store.get_task(task_id)
        project = self.settings.projects[task["project_id"]]
        for index in range(len(project.checks)):
            run_fixed(("systemctl", "--user", "stop", self._check_unit(task_id, index)), timeout=10)

    def _is_cancelled(self, task_id: str) -> bool:
        with self._cancel_lock:
            return task_id in self._cancelled

    def _still_verifying(self, task_id: str) -> bool:
        return not self._is_cancelled(task_id) and self.store.get_task(task_id)["state"] == "VERIFYING"

    def _task_commit_lock(self, task_id: str) -> threading.Lock:
        with self._cancel_lock:
            return self._commit_locks.setdefault(task_id, threading.Lock())

    def verify(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task["state"] != "VERIFYING":
            raise VerificationError(f"Task is {task['state']}")
        project = self.settings.projects[task["project_id"]]
        worktree = Path(task["worktree_path"] or "").resolve()
        if not worktree.is_dir() or worktree.parent != project.worktree_root.resolve():
            return self._finish_failure(task, "Registered worktree is missing or outside the allowed root")
        if not self._valid_worktree_metadata(worktree, project.repo_path):
            return self._finish_failure(task, "Worktree Git metadata is not trusted")

        status = run_fixed((*GIT_SAFE_PREFIX, "status", "--porcelain=v1"), cwd=worktree, timeout=10)
        if not status.ok:
            return self._finish_failure(task, "Could not inspect worktree")
        if not status.stdout.strip():
            recovered = self._recover_verified_commit(task, worktree)
            return recovered or self._finish_failure(task, "Worker produced no file changes")

        if self._candidate_changes_git_metadata(worktree):
            return self._finish_failure(task, "Changes to Git control files require manual review")

        add = run_fixed((*GIT_SAFE_PREFIX, "add", "--all"), cwd=worktree, timeout=20)
        staged_check = run_fixed((*GIT_SAFE_PREFIX, "diff", "--cached", "--check"), cwd=worktree, timeout=20)
        if not add.ok or not staged_check.ok:
            return self._finish_failure(task, "Could not freeze the candidate tree")
        tree_before = run_fixed((*GIT_SAFE_PREFIX, "write-tree"), cwd=worktree, timeout=20)
        if not tree_before.ok or not re.fullmatch(r"[0-9a-f]{40}", tree_before.stdout):
            return self._finish_failure(task, "Could not identify the candidate tree")

        diff_ok, raw_diff, _ = self._capture_raw(
            (*GIT_SAFE_PREFIX, "diff", "--cached", "--no-ext-diff", "--binary", "--full-index", task["base_sha"]),
            cwd=worktree,
            timeout=20,
            limit=55_000,
        )
        if not diff_ok:
            return self._finish_failure(task, "Could not produce a complete review diff")
        if "GIT binary patch" in raw_diff or "Binary files " in raw_diff:
            return self._finish_failure(task, "Binary changes require manual review")
        if contains_secret(raw_diff):
            return self._finish_failure(task, "Potential secret detected in the diff")

        checks: list[dict[str, Any]] = []
        all_passed = True
        for index, command in enumerate(project.checks):
            if not self._still_verifying(task_id):
                return self.store.get_task(task_id)
            result = self._run_isolated_check(task_id, index, command, worktree, project.repo_path)
            check = {
                "argv": list(command),
                "passed": result.ok,
                "returncode": result.returncode,
                "elapsed_ms": result.elapsed_ms,
                "output": result.stdout[-4_000:],
            }
            checks.append(check)
            all_passed = all_passed and result.ok
        if not all_passed:
            return self._finish_failure(task, "One or more registered checks failed", checks=checks)

        restage = run_fixed((*GIT_SAFE_PREFIX, "add", "--all"), cwd=worktree, timeout=20)
        tree_after = run_fixed((*GIT_SAFE_PREFIX, "write-tree"), cwd=worktree, timeout=20)
        if not restage.ok or not tree_after.ok or tree_after.stdout != tree_before.stdout:
            return self._finish_failure(task, "Registered checks changed the candidate tree", checks=checks)
        if not self._still_verifying(task_id):
            return self.store.get_task(task_id)

        try:
            review = self.reviewer.review_diff(
                task_specification=task["specification"],
                diff_text=raw_diff,
                checks=checks,
            )
        except InterpretationUnavailable as exc:
            evidence = self._write_evidence(
                task,
                {
                    "verdict": "UNVERIFIED",
                    "reason": f"Independent review unavailable: {exc}",
                    "checks": checks,
                },
            )
            self.store.add_event(task_id, None, "REVIEW_UNAVAILABLE", "verify", evidence)
            return self.store.transition(task_id, expected_state="VERIFYING", new_state="BLOCKED")
        if review.verdict != "PASS":
            return self._finish_failure(
                task,
                "Independent review did not pass",
                checks=checks,
                extra={
                    "review_verdict": review.verdict,
                    "review_summary": review.summary,
                    "review_findings": review.findings,
                },
            )

        candidate_evidence = self._write_evidence(
            task,
            {
                "verdict": "PASS_PENDING_COMMIT",
                "stage": "CANDIDATE_VERIFIED",
                "verified_at": utc_now(),
                "base_sha": task["base_sha"],
                "reviewed_tree_sha": tree_before.stdout,
                "checks": checks,
                "review": {
                    "verdict": review.verdict,
                    "summary": review.summary,
                    "findings": review.findings,
                },
            },
        )
        self.store.add_event(task_id, None, "CANDIDATE_VERIFIED", "verify", candidate_evidence)

        with self._task_commit_lock(task_id):
            if not self._still_verifying(task_id):
                return self.store.get_task(task_id)
            commit = run_fixed(
                (*GIT_SAFE_PREFIX, "commit", "--no-verify", "-m", f"chief({task_id}): complete approved task"),
                cwd=worktree,
                timeout=60,
            )
            if not commit.ok:
                return self._finish_failure(task, "Trusted commit step failed", checks=checks)
            head = run_fixed((*GIT_SAFE_PREFIX, "rev-parse", "HEAD"), cwd=worktree, timeout=10)
            committed_tree = run_fixed((*GIT_SAFE_PREFIX, "rev-parse", "HEAD^{tree}"), cwd=worktree, timeout=10)
            clean = run_fixed((*GIT_SAFE_PREFIX, "status", "--porcelain=v1"), cwd=worktree, timeout=10)
            if (
                not head.ok
                or len(head.stdout.strip()) != 40
                or not committed_tree.ok
                or committed_tree.stdout != tree_before.stdout
                or not clean.ok
                or clean.stdout.strip()
            ):
                return self._finish_failure(task, "Final SHA or clean worktree could not be proven", checks=checks)

            evidence = self._write_evidence(
                task,
                {
                    "verdict": "PASS",
                    "stage": "COMMIT_VERIFIED",
                    "verified_at": utc_now(),
                    "base_sha": task["base_sha"],
                    "head_sha": head.stdout.strip(),
                    "reviewed_tree_sha": tree_before.stdout,
                    "checks": checks,
                    "review": {
                        "verdict": review.verdict,
                        "summary": review.summary,
                        "findings": review.findings,
                    },
                },
            )
            ready = self.store.transition(
                task_id,
                expected_state="VERIFYING",
                new_state="READY",
                fields={"head_sha": head.stdout.strip()},
            )
            self.store.add_event(task_id, None, "VERIFICATION_PASSED", "verify", evidence)
        return ready

    def _valid_worktree_metadata(self, worktree: Path, repo_path: Path) -> bool:
        marker = worktree / ".git"
        try:
            if marker.is_symlink() or not marker.is_file():
                return False
            line = marker.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir: ") or "\n" in line:
                return False
            raw = Path(line.removeprefix("gitdir: "))
            git_dir = (raw if raw.is_absolute() else marker.parent / raw).resolve(strict=True)
            allowed_parent = (repo_path.resolve() / ".git" / "worktrees").resolve(strict=True)
            return git_dir.parent == allowed_parent and not git_dir.is_symlink()
        except (OSError, RuntimeError):
            return False

    def _candidate_changes_git_metadata(self, worktree: Path) -> bool:
        commands = (
            (*GIT_SAFE_PREFIX, "diff", "HEAD", "--name-only", "-z"),
            (*GIT_SAFE_PREFIX, "ls-files", "--others", "--exclude-standard", "-z"),
        )
        names: list[str] = []
        for command in commands:
            ok, output, _ = self._capture_raw(command, cwd=worktree, timeout=20, limit=1_000_000)
            if not ok:
                return True
            names.extend(name for name in output.split("\0") if name)
        for name in names:
            parts = Path(name).parts
            if ".git" in parts or Path(name).name in {".gitattributes", ".gitmodules"}:
                return True
        return False

    def _recover_verified_commit(self, task: dict[str, Any], worktree: Path) -> dict[str, Any] | None:
        path = self.settings.state_dir / "evidence" / f"{task['id']}-verification.json"
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if evidence.get("stage") not in {"CANDIDATE_VERIFIED", "COMMIT_VERIFIED"}:
            return None
        head = run_fixed((*GIT_SAFE_PREFIX, "rev-parse", "HEAD"), cwd=worktree, timeout=10)
        tree = run_fixed((*GIT_SAFE_PREFIX, "rev-parse", "HEAD^{tree}"), cwd=worktree, timeout=10)
        parent = run_fixed((*GIT_SAFE_PREFIX, "rev-parse", "HEAD^"), cwd=worktree, timeout=10)
        if (
            not head.ok
            or not tree.ok
            or not parent.ok
            or parent.stdout != task["base_sha"]
            or tree.stdout != evidence.get("reviewed_tree_sha")
        ):
            return None
        ready = self.store.transition(
            task["id"], expected_state="VERIFYING", new_state="READY", fields={"head_sha": head.stdout}
        )
        self.store.add_event(task["id"], None, "VERIFICATION_RECOVERED", "recovery", evidence)
        return ready

    def _run_isolated_check(
        self,
        task_id: str,
        index: int,
        command: tuple[str, ...],
        worktree: Path,
        repo_path: Path,
    ):
        unit = self._check_unit(task_id, index)
        directory_args: list[str] = []
        created: set[Path] = set()
        for exposed in (repo_path.resolve(), worktree.resolve()):
            for parent in reversed(exposed.parents):
                if parent == Path("/") or parent in created:
                    continue
                if parent.parts and parent.parts[1:2] in (("home",), ("tmp",)):
                    directory_args.extend(("--dir", str(parent)))
                    created.add(parent)
        directory_args.extend(("--dir", str(repo_path.resolve())))
        sandbox = (
            "bwrap",
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--symlink",
            "usr/sbin",
            "/sbin",
            "--dir",
            "/etc",
            "--dir",
            "/etc/ssl",
            "--ro-bind",
            "/etc/ssl/certs",
            "/etc/ssl/certs",
            "--ro-bind",
            "/etc/ld.so.cache",
            "/etc/ld.so.cache",
            "--ro-bind",
            "/etc/passwd",
            "/etc/passwd",
            "--ro-bind",
            "/etc/group",
            "/etc/group",
            "--ro-bind",
            "/etc/nsswitch.conf",
            "/etc/nsswitch.conf",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--dir",
            "/tmp",
            *directory_args,
            "--ro-bind",
            str((repo_path.resolve() / ".git")),
            str((repo_path.resolve() / ".git")),
            "--bind",
            str(worktree.resolve()),
            str(worktree.resolve()),
            "--ro-bind",
            str((worktree / ".git").resolve()),
            str((worktree / ".git").resolve()),
            "--dir",
            "/tmp/chief-home",
            "--clearenv",
            "--setenv",
            "HOME",
            "/tmp/chief-home",
            "--setenv",
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "SSL_CERT_DIR",
            "/etc/ssl/certs",
            "--chdir",
            str(worktree.resolve()),
            "--",
            *command,
        )
        argv = (
            "systemd-run",
            "--user",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            f"--unit={unit}",
            "--property=Type=exec",
            "--property=NoNewPrivileges=yes",
            "--property=CPUQuota=600%",
            "--property=MemoryMax=8G",
            "--property=TasksMax=256",
            "--property=RuntimeMaxSec=180",
            "--property=KillMode=control-group",
            "--property=UMask=0077",
            "--",
            *sandbox,
        )
        return run_fixed(argv, timeout=180)

    @staticmethod
    def _check_unit(task_id: str, index: int) -> str:
        return f"deskmini-chief-v-{task_id.lower()}-{index}"

    def _finish_failure(
        self,
        task: dict[str, Any],
        reason: str,
        *,
        checks: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "verdict": "FAIL",
            "reason": reason,
            "checks": checks or [],
            **(extra or {}),
        }
        evidence = self._write_evidence(task, payload)
        self.store.add_event(task["id"], None, "VERIFICATION_FAILED", "verify", evidence)
        return self.store.transition(task["id"], expected_state="VERIFYING", new_state="FAILED")

    def _write_evidence(self, task: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        safe = redact(payload)
        evidence_dir = self.settings.state_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = evidence_dir / f"{task['id']}-verification.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return {"path": str(path), **safe}

    @staticmethod
    def _capture_raw(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
        limit: int,
    ) -> tuple[bool, str, str]:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                start_new_session=os.name == "posix",
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                returncode = process.returncode
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=2)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        pass
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=3)
                return False, "", "timeout"
        except OSError as exc:
            return False, "", type(exc).__name__
        if len(stdout) > limit:
            return False, "", "diff exceeds review limit"
        return returncode == 0, stdout, redact_text(stderr, 2_000)
