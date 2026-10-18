#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Collect bounded reports and logs, rechecking successful build artifacts."""

import argparse
import json
from pathlib import Path
import shutil

from lab_io import digest


def collect(workspace, output):
    workspace = workspace.resolve()
    output = output.resolve()
    if output.is_relative_to(workspace) or workspace.is_relative_to(output):
        raise ValueError("Evidence destination must be outside the build workspace")
    output.mkdir(exist_ok=False, parents=True)
    summary = json.loads((workspace / "result.json").read_text())
    selected = {file for file in workspace.glob("*.json") if file.is_file()}
    selected.update(workspace.glob("logs/*.log"))
    selected.update(workspace.glob("previous-results/*.json"))
    module_directories = sorted(workspace.glob("modules*"))
    artifacts_verified = 0
    for directory in module_directories:
        if not directory.is_dir():
            continue
        selected.update(directory.glob("*.json"))
        selected.update(directory.glob("*.log"))
        selected.update(directory.glob("compiled-layout*/*.json"))
        selected.update(directory.glob("compiled-layout*/*.c"))
        selected.update(directory.glob("compiled-layout*/*.log"))
        result = directory / "result.json"
        if not result.is_file():
            continue
        report = json.loads(result.read_text())
        for phase in report["phases"]:
            if digest(directory / phase["log"]) != phase["log_sha256"]:
                raise ValueError("Module command log changed: " + phase["name"])
        if summary["passed"] and directory.name == summary["module_output"]:
            if not report["passed"] or len(report["artifacts"]) != 20:
                raise ValueError("Incomplete successful module report")
            for name, artifact in report["artifacts"].items():
                kind, relative = name.split("/", 1)
                root = "backports-" + kind if kind in ("base", "candidate") else kind
                if digest(directory / root / relative) != artifact["sha256"]:
                    raise ValueError("Compiled module changed: " + name)
                artifacts_verified += 1
    for command in json.loads((workspace / "commands.json").read_text()):
        if digest(workspace / command["log"]) != command["log_sha256"]:
            raise ValueError("Top-level command log changed: " + command["name"])
    if summary["passed"] and artifacts_verified != 20:
        raise ValueError("Successful report did not identify twenty real modules")
    for file in sorted(selected):
        destination = output / file.relative_to(workspace)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
    (output / "collection.json").write_text(
        json.dumps(
            {
                "build_passed": summary["passed"],
                "module_artifacts_rehashed": artifacts_verified,
                "modules_or_firmware_included": False,
                "source_configuration_included": False,
                "files": len(selected),
            },
            indent=2,
        )
        + "\n"
    )
    files = sorted(file for file in output.rglob("*") if file.is_file())
    (output / "SHA256SUMS").write_text(
        "".join(digest(file) + "  " + str(file.relative_to(output)) + "\n" for file in files)
    )
    return artifacts_verified


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("Module artifacts rehashed:", collect(args.workspace, args.output))
