# SPDX-License-Identifier: GPL-2.0-only
"""Content-pinned acquisition, safe extraction, and retained command evidence."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import signal
import tarfile
import time


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_digest(directory):
    """Fingerprint prepared inputs, including symlinks and executable modes."""
    root = Path(directory)
    result = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            kind, value, executable = "link", str(path.readlink()), 0
        elif path.is_file():
            kind, value, executable = "file", digest(path), path.stat().st_mode & 0o111
        elif path.is_dir():
            kind, value, executable = "directory", "", 0
        else:
            raise ValueError("Unsupported source object: " + str(path))
        result.update(
            json.dumps([str(relative), kind, executable, value], ensure_ascii=True).encode()
        )
        result.update(b"\n")
    return result.hexdigest()


class Evidence:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.logs = self.workspace / "logs"
        self.logs.mkdir(exist_ok=True)
        self.path = self.workspace / "commands.json"
        self.records = json.loads(self.path.read_text()) if self.path.exists() else []
        for record in self.records:
            log = self.workspace / record["log"]
            if (
                not log.resolve().is_relative_to(self.logs.resolve())
                or digest(log) != record["log_sha256"]
            ):
                raise ValueError("Previous command evidence changed: " + str(log))

    def run(self, name, argv, cwd=None, env=None, timeout=900):
        log = self.logs / (name + ".log")
        attempt = 2
        while log.exists():
            log = self.logs / (name + "-attempt" + str(attempt) + ".log")
            attempt += 1
        started = time.monotonic()
        print(name, flush=True)
        code = None
        error = None
        child = None
        try:
            with log.open("w") as output:
                child = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                code = child.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
        finally:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
        record = {
            "name": name,
            "argv": list(map(str, argv)),
            "cwd": str(cwd),
            "exit": code,
            "error": error,
            "seconds": round(time.monotonic() - started, 3),
            "log": str(log.relative_to(self.workspace)),
            "log_sha256": digest(log),
        }
        self.records.append(record)
        temporary = self.path.with_suffix(".new")
        temporary.write_text(json.dumps(self.records, indent=2) + "\n")
        temporary.replace(self.path)
        if code != 0 or error:
            raise RuntimeError(name + " failed; retained log: " + str(log))
        return record


def acquire(spec, name, workspace, evidence, cache=None):
    folder = workspace / "downloads"
    folder.mkdir(exist_ok=True)
    output = folder / name
    expected = spec["sha256"]
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Invalid pinned SHA256: " + name)
    if output.exists():
        if digest(output) != expected:
            raise ValueError("Existing download hash mismatch: " + str(output))
        return output
    if cache and (cache / expected).is_file():
        candidate = cache / expected
        if digest(candidate) != expected:
            raise ValueError("Read-only archive cache hash mismatch: " + str(candidate))
        import shutil

        shutil.copyfile(candidate, output)
        return output
    failure = None
    for attempt in range(1, 4):
        suffix = attempt
        partial = folder / (name + ".attempt" + str(suffix))
        while partial.exists():
            suffix += 1
            partial = folder / (name + ".attempt" + str(suffix))
        try:
            evidence.run(
                "download-" + name + "-" + str(attempt),
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--http1.1",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "300",
                    spec["url"],
                    "-o",
                    str(partial),
                ],
                timeout=310,
            )
            if digest(partial) != expected:
                raise ValueError("Downloaded archive hash mismatch: " + name)
            partial.replace(output)
            return output
        except (RuntimeError, ValueError) as exc:
            failure = exc
    raise RuntimeError("Could not acquire verified " + name) from failure


def extract_archive(archive, output, sdk_host_links=None):
    """Strip one root; omit only manifest-pinned SDK builder-host links.

    OpenWrt's prerequisite checks recreate those host commands locally. Never
    extract an absolute link, including an explicitly recognized SDK link.
    """
    expected_links = sdk_host_links or {}
    for name, target in expected_links.items():
        path = PurePosixPath(name)
        if (
            path.parts[:3] != ("staging_dir", "host", "bin")
            or len(path.parts) != 4
            or ".." in path.parts
            or not PurePosixPath(target).is_absolute()
        ):
            raise ValueError("Invalid SDK host-link pin: " + name)
    omitted_links = {}
    output.mkdir(exist_ok=False)
    process = None
    try:
        if str(archive).endswith(".zst"):
            process = subprocess.Popen(
                ["zstd", "-qdc", str(archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stream = tarfile.open(fileobj=process.stdout, mode="r|")
        else:
            stream = tarfile.open(archive)
        root = None
        with stream:
            for member in stream:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError("Unsafe archive member: " + member.name)
                if root is None:
                    root = path.parts[0]
                if path.parts[0] != root:
                    raise ValueError("Archive has multiple roots")
                if len(path.parts) == 1:
                    if not member.isdir():
                        raise ValueError("Expected an archive root directory")
                    continue
                member.name = str(PurePosixPath(*path.parts[1:]))
                if member.name in expected_links:
                    if (
                        not member.issym()
                        or member.linkname != expected_links[member.name]
                        or member.name in omitted_links
                    ):
                        raise ValueError("SDK host-link pin mismatch: " + member.name)
                    omitted_links[member.name] = member.linkname
                    continue
                if member.islnk():
                    link = PurePosixPath(member.linkname)
                    if not link.parts or link.parts[0] != root:
                        raise ValueError("Hardlink outside archive root")
                    member.linkname = str(PurePosixPath(*link.parts[1:]))
                stream.extract(member, output, filter="data")
        if root is None:
            raise ValueError("Empty archive")
        if omitted_links != expected_links:
            raise ValueError("SDK archive is missing pinned host links")
        if process:
            process.stdout.close()
            error = process.stderr.read().decode(errors="replace")
            code = process.wait(timeout=120)
            if code:
                raise RuntimeError("zstd decompression failed: " + error)
        return omitted_links
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
