# SPDX-License-Identifier: GPL-2.0-only
"""Reconstruct a shallow Git snapshot and verify its complete original tree.

The archive is content-pinned. Rehash every source blob using Git itself, then
require both the original root tree and original commit object identifiers.
No alternate object database, previous workspace, credentials or network Git
fetch is needed. The original raw commit is public pinned metadata.
"""

import hashlib
import os
from pathlib import Path
import subprocess


def restore_git(directory: Path, raw_commit: bytes, commit: str, branch="main-nss"):
    if (
        hashlib.sha1(b"commit " + str(len(raw_commit)).encode() + b"\0" + raw_commit).hexdigest()
        != commit
    ):
        raise ValueError("Pinned commit metadata hash mismatch")
    if (directory / ".git").exists():
        raise ValueError("Refuse an existing Git repository")
    expected_tree = raw_commit.splitlines()[0].decode().removeprefix("tree ")
    if len(expected_tree) != 40:
        raise ValueError("Invalid pinned Git root tree")
    env = dict(
        os.environ,
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_ATTR_NOSYSTEM="1",
    )
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(name, None)
    for name in list(env):
        if name == "GIT_CONFIG_COUNT" or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name)

    def git(*args, data=None):
        return (
            subprocess.check_output(
                ["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
                input=data,
                env=env,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            .decode()
            .strip()
        )

    git("init", "--template=", "-q", "-b", branch)
    git("-c", "core.autocrlf=false", "-c", "core.fileMode=true", "add", "-f", ".")
    actual_tree = git("write-tree")
    if actual_tree != expected_tree:
        raise ValueError("Extracted archive Git tree mismatch: " + actual_tree)
    actual_commit = git("hash-object", "-w", "-t", "commit", "--stdin", data=raw_commit)
    if actual_commit != commit:
        raise ValueError("Restored commit object mismatch")
    git("update-ref", "refs/heads/" + branch, commit)
    (directory / ".git/shallow").write_text(commit + "\n")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Reconstructed source snapshot is not clean")
    return {
        "commit": commit,
        "tree": actual_tree,
        "complete_tree_verified": True,
        "shallow_boundary": commit,
        "previous_object_database_used": False,
    }
