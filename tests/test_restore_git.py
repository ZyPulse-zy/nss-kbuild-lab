# SPDX-License-Identifier: GPL-2.0-only
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from restore_git import restore_git


class RestoreGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="git-archive-proof-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "source snapshot"
        self.snapshot.mkdir()
        (self.snapshot / "plain.txt").write_bytes(b"original\n")
        executable = self.snapshot / "run.sh"
        executable.write_text("#!/bin/sh\nprintf pass\\n\n")
        executable.chmod(0o755)
        (self.snapshot / "link").symlink_to("plain.txt")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        self.git("init", "--template=", "-q", "-b", "main-nss")
        self.git("-c", "core.autocrlf=false", "add", "-f", ".")
        self.tree = self.git("write-tree")
        self.raw = (
            "tree " + self.tree + "\nauthor Test <test@example.invalid> 1 +0000\n"
            "committer Test <test@example.invalid> 1 +0000\n\nPinned fixture\n"
        ).encode()
        self.commit = hashlib.sha1(
            b"commit " + str(len(self.raw)).encode() + b"\0" + self.raw
        ).hexdigest()
        self.restored = self.root / "restored source"
        shutil.copytree(
            self.snapshot, self.restored, symlinks=True, ignore=shutil.ignore_patterns(".git")
        )

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.snapshot), *args],
            env=self.env,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()

    def test_restores_exact_commit_tree_modes_and_symlink_without_old_objects(self):
        shutil.rmtree(self.snapshot)
        proof = restore_git(self.restored, self.raw, self.commit)
        self.assertEqual(proof["tree"], self.tree)
        self.assertEqual(proof["commit"], self.commit)
        self.assertFalse(proof["previous_object_database_used"])
        self.assertTrue((self.restored / "run.sh").stat().st_mode & 0o100)
        self.assertEqual((self.restored / "link").readlink(), Path("plain.txt"))
        result = subprocess.check_output(
            ["git", "-C", str(self.restored), "log", "-1", "--format=%H"], text=True
        ).strip()
        self.assertEqual(result, self.commit)

    def test_rejects_changed_file(self):
        (self.restored / "plain.txt").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "tree mismatch"):
            restore_git(self.restored, self.raw, self.commit)
        self.assertEqual((self.restored / "plain.txt").read_text(), "changed\n")

    def test_rejects_changed_mode(self):
        (self.restored / "run.sh").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "tree mismatch"):
            restore_git(self.restored, self.raw, self.commit)

    def test_rejects_changed_symlink(self):
        (self.restored / "link").unlink()
        (self.restored / "link").symlink_to("run.sh")
        with self.assertRaisesRegex(ValueError, "tree mismatch"):
            restore_git(self.restored, self.raw, self.commit)

    def test_rejects_extra_source_file(self):
        (self.restored / "extra").write_text("unlisted\n")
        with self.assertRaisesRegex(ValueError, "tree mismatch"):
            restore_git(self.restored, self.raw, self.commit)

    def test_rejects_bad_commit_before_creating_git(self):
        with self.assertRaisesRegex(ValueError, "metadata hash mismatch"):
            restore_git(self.restored, self.raw + b"changed", self.commit)
        self.assertFalse((self.restored / ".git").exists())

    def test_refuses_existing_repository(self):
        restore_git(self.restored, self.raw, self.commit)
        before = (self.restored / ".git/HEAD").read_bytes()
        with self.assertRaisesRegex(ValueError, "existing Git"):
            restore_git(self.restored, self.raw, self.commit)
        self.assertEqual((self.restored / ".git/HEAD").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
