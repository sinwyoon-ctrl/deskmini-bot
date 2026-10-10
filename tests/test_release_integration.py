import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from adapters.gemini_rest import InterpretationUnavailable, ReviewResult
from app.config import ProjectConfig, Settings
from app.release import ReleaseManager
from app.state import StateStore
from app.verify import FallbackReviewer, Verifier


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class UnavailableReviewer:
    def review_diff(self, **kwargs):
        raise InterpretationUnavailable("gemini quota exhausted")


class PassingReviewer:
    def review_diff(self, **kwargs):
        return ReviewResult(verdict="PASS", findings=[], summary="independent test reviewer pass")


@unittest.skipUnless(os.name == "posix", "release locking is Linux-only")
class ReleaseIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.worktree_root = self.root / "worktrees"
        self.state_dir = self.root / "state"
        self.repo.mkdir()
        subprocess.run(("git", "init", "--bare", str(self.remote)), check=True, stdout=subprocess.DEVNULL)
        git(self.repo.parent, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.name", "DeskMini Test")
        git(self.repo, "config", "user.email", "deskmini-test@invalid.local")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-u", "origin", "main")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        self.outside_secret = self.root / "outside-secret.txt"
        self.outside_secret.write_text("must-not-be-readable", encoding="utf-8")
        isolation_probe = (
            "import os,pathlib,socket; "
            "s=socket.socket(); s.settimeout(0.2); "
            "network=s.connect_ex(('1.1.1.1',53)); "
            f"outside=pathlib.Path({str(self.outside_secret)!r}).exists(); "
            "sockets=pathlib.Path(f'/run/user/{os.getuid()}/bus').exists() or pathlib.Path('/var/run/docker.sock').exists(); "
            "metadata_writable=True; "
            "\ntry:\n open(pathlib.Path.cwd()/'.git','a').close()\nexcept OSError:\n metadata_writable=False\n"
            "raise SystemExit(1 if outside or sockets or metadata_writable or network == 0 else 0)"
        )

        project = ProjectConfig(
            project_id="deskmini",
            repo_path=self.repo,
            worktree_root=self.worktree_root,
            default_branch="main",
            checks=(
                (os.path.realpath(sys.executable), "-c", isolation_probe),
                ("git", "diff", "--check"),
            ),
            deploy_enabled=True,
            deploy_environment="staging",
            deploy_command=("/usr/bin/true",),
            healthcheck_command=("/usr/bin/true",),
            rollback_command=("/usr/bin/true",),
            deployed_version_command=("git", "rev-parse", "HEAD"),
        )
        self.settings = Settings(
            root=Path(__file__).resolve().parents[1],
            state_dir=self.state_dir,
            db_path=self.state_dir / "chief.sqlite",
            telegram_token="test-token",
            allowed_chat_id=100,
            allowed_user_id=100,
            telegram_timeout_seconds=1,
            gemini_script=self.root / "missing-gemini.py",
            max_runtime_seconds=60,
            max_active_tasks=1,
            approval_ttl_seconds=600,
            projects={"deskmini": project},
        )
        self.store = StateStore(self.settings.db_path, self.settings.root / "schema.sql")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_verify_merge_push_and_deploy_are_separate_gates(self) -> None:
        task = self.store.create_task(
            owner_user_id=100,
            chat_id=100,
            project_id="deskmini",
            request_text="integration test",
            specification={"objective": "change readme"},
            policy_hash="test",
            budget={},
        )
        worktree = self.worktree_root / task["id"]
        self.worktree_root.mkdir()
        git(self.repo, "worktree", "add", "-b", f"chief/{task['id'].lower()}", str(worktree), self.base_sha)
        (worktree / "README.md").write_text("base\nverified change\n", encoding="utf-8")
        self.store.transition(
            task["id"],
            expected_state="AWAITING_START",
            new_state="STARTING",
            fields={
                "base_sha": self.base_sha,
                "branch_name": f"chief/{task['id'].lower()}",
                "worktree_path": str(worktree),
            },
        )
        self.store.transition(task["id"], expected_state="STARTING", new_state="RUNNING")
        self.store.transition(task["id"], expected_state="RUNNING", new_state="VERIFYING")
        # 첫 검토자(gemini 자리)가 할당량 소진으로 못 하면 다음 검토자가 판정하고, 증거에 누가 했는지 남는다.
        reviewer = FallbackReviewer([("gemini", UnavailableReviewer()), ("claude_cli", PassingReviewer())])
        ready = Verifier(self.settings, self.store, reviewer).verify(task["id"])
        self.assertEqual(
            ready["state"],
            "READY",
            self.store.recent_events(task["id"], limit=3),
        )
        self.assertNotEqual(ready["head_sha"], self.base_sha)
        evidence = json.loads((self.state_dir / "evidence" / f"{task['id']}-verification.json").read_text())
        self.assertEqual(evidence["review"]["reviewer"], "claude_cli")
        self.assertIn("quota exhausted", evidence["review"]["fallbacks"][0])

        manager = ReleaseManager(self.settings, self.store)
        for action in ("merge", "push", "deploy"):
            current = self.store.get_task(task["id"])
            payload = manager.approval_payload(task["id"], action)
            approval_id = self.store.create_approval(
                task_id=task["id"],
                action=action,
                payload=payload,
                owner_user_id=100,
                chat_id=100,
                task_version=current["version"],
                ttl_seconds=600,
            )
            approval = self.store.consume_approval(
                approval_id=approval_id,
                action=action,
                owner_user_id=100,
                chat_id=100,
            )
            operation = manager.execute(approval)
            self.assertEqual(operation["state"], "SUCCEEDED", operation)

        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), ready["head_sha"])
        remote_sha = git(self.repo, "ls-remote", "origin", "refs/heads/main").split()[0]
        self.assertEqual(remote_sha, ready["head_sha"])

        self.settings.projects["deskmini"] = replace(
            self.settings.projects["deskmini"],
            deploy_command=("/usr/bin/false",),
        )
        current = self.store.get_task(task["id"])
        payload = manager.approval_payload(task["id"], "deploy")
        approval_id = self.store.create_approval(
            task_id=task["id"],
            action="deploy",
            payload=payload,
            owner_user_id=100,
            chat_id=100,
            task_version=current["version"],
            ttl_seconds=600,
        )
        approval = self.store.consume_approval(
            approval_id=approval_id,
            action="deploy",
            owner_user_id=100,
            chat_id=100,
        )
        failed_deploy = manager.execute(approval)
        self.assertEqual(failed_deploy["state"], "FAILED")
        self.assertTrue(failed_deploy["after_evidence"]["rollback_performed"])
        self.assertTrue(failed_deploy["after_evidence"]["rollback_command_ok"])
        self.assertTrue(failed_deploy["after_evidence"]["rollback_healthcheck_ok"])
        self.assertTrue(failed_deploy["after_evidence"]["rollback_succeeded"])


if __name__ == "__main__":
    unittest.main()
