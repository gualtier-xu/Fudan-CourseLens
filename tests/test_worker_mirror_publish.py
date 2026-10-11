from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.publish_worker_mirror import PublishError, _failure_message, publish_snapshot


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        list(args), cwd=cwd, check=True, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


class WorkerMirrorPublishTests(unittest.TestCase):
    def test_workflow_permission_push_failure_is_actionable_and_sanitized(self):
        raw = (
            "refusing to allow a GitHub App to create or update workflow "
            ".github/workflows/ci.yml without workflows permission secret=value"
        )
        message = _failure_message(("git", "push"), raw)
        self.assertIn("Workflows write permissions", message)
        self.assertNotIn("secret=value", message)

    def fixture(self, root: Path):
        seed = root / "seed"
        seed.mkdir()
        run(seed, "git", "init", "--initial-branch=main")
        run(seed, "git", "config", "user.name", "test")
        run(seed, "git", "config", "user.email", "test@example.invalid")
        (seed / "README.md").write_text("old public tree\n", encoding="utf-8")
        run(seed, "git", "add", "README.md")
        run(seed, "git", "commit", "-m", "public base")
        bare = root / "public.git"
        run(root, "git", "clone", "--bare", str(seed), str(bare))
        snapshot = root / "snapshot"
        snapshot.mkdir()
        (snapshot / "README.md").write_text("generated\n", encoding="utf-8")
        (snapshot / "worker-mirror.manifest.json").write_text(
            json.dumps({"schema": "test"}) + "\n", encoding="utf-8"
        )
        return bare, snapshot

    def test_orphan_snapshot_push_is_idempotent_and_has_only_public_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, snapshot = self.fixture(root)
            source = "f" * 40
            first = publish_snapshot(
                snapshot, public_url=str(bare), branch="generated/test",
                source_commit=source, skip_pr=True,
            )
            second = publish_snapshot(
                snapshot, public_url=str(bare), branch="generated/test",
                source_commit=source, skip_pr=True,
            )
            self.assertTrue(first["pushed"])
            self.assertFalse(second["pushed"])
            self.assertEqual(first["generated_tree"], second["generated_tree"])
            parents = run(bare, "git", "rev-list", "--parents", "-n", "1", "generated/test").split()
            self.assertEqual(len(parents), 2)
            self.assertNotEqual(parents[1], source)
            missing = subprocess.run(
                ["git", "cat-file", "-e", f"{source}^{{commit}}"], cwd=bare,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(missing.returncode, 0)

    def test_existing_generated_branch_with_different_tree_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, snapshot = self.fixture(root)
            publish_snapshot(
                snapshot, public_url=str(bare), branch="generated/test",
                source_commit="e" * 40, skip_pr=True,
            )
            (snapshot / "README.md").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(PublishError, "different tree"):
                publish_snapshot(
                    snapshot, public_url=str(bare), branch="generated/test",
                    source_commit="d" * 40, skip_pr=True,
                )


if __name__ == "__main__":
    unittest.main()
