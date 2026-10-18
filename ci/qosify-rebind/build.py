#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build full qosify interface code from verified original/candidate Git roots."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from restore_git import restore_git


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--native-include", type=Path, default=Path("/usr/include/libnl3")
    )
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    build = workspace / "rebind"
    build.mkdir()
    (build / "bin").mkdir()
    evidence = workspace / "evidence-rebind"
    evidence.mkdir()
    pins = json.loads((HERE / "pins.json").read_text())
    shared = json.loads((workspace / "evidence/source-verification.json").read_text())
    assert shared["base"]["commit"] == pins["base_commit"]
    assert shared["base"]["complete_tree_verified"]
    assert len(shared["omitted_sdk_links"]) == 20
    sdk = workspace / "sdk/staging_dir/host"
    lib = build / "lib"
    lib.mkdir()
    shutil.copyfile(sdk / "lib/libubox.so", lib / "libubox.so")
    include = build / "include"
    include.mkdir()
    shutil.copytree(sdk / "include/libubox", include / "libubox")
    libbpf = workspace / "bpftool-build/libbpf"
    sources = {}

    def run(label, argv, **kwargs):
        with (evidence / (label + ".log")).open("w") as log:
            log.write(json.dumps([str(x) for x in argv]) + "\n")
            log.flush()
            subprocess.run(
                [str(x) for x in argv],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                **kwargs
            )

    for label in ["original", "fixed"]:
        src = build / label
        shutil.copytree(
            workspace / "original", src, ignore=shutil.ignore_patterns(".git")
        )
        if label == "fixed":
            patch = (HERE / "fix.patch").read_bytes()
            assert hashlib.sha256(patch).hexdigest() == pins["patch_sha256"]
            run(
                "apply-rebind-fix",
                ["patch", "--batch", "--fuzz=0", "-p1", "-i", HERE / "fix.patch"],
                cwd=src,
            )
        sources[label] = restore_git(
            src,
            (
                HERE / ("base.commit" if label == "original" else "candidate.commit")
            ).read_bytes(),
            pins["base_commit" if label == "original" else "candidate"],
            branch="master" if label == "original" else "fix/interface-rebind",
        )
        for sanitize in [False, True]:
            binary = label + ("-san" if sanitize else "")
            flags = (
                ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
                if sanitize
                else []
            )
            base = [
                "cc",
                "-std=gnu99",
                "-O1" if sanitize else "-O2",
                "-g",
                "-ffunction-sections",
                "-fdata-sections",
                "-I" + str(src),
                "-I" + str(include),
                "-I" + str(args.native_include),
                "-I" + str(libbpf / "include"),
                *flags,
            ]
            main = build / (binary + "-main.o")
            run(
                binary + "-main-compile",
                base
                + ["-Dmain=qosify_original_main", "-c", src / "main.c", "-o", main],
            )
            run(
                binary + "-interface-link",
                base
                + [
                    '-DQOSIFY_INTERFACE_SOURCE="' + str(src / "interface.c") + '"',
                    HERE / "harness.c",
                    main,
                    libbpf / "libbpf.a",
                    "-L" + str(lib),
                    "-Wl,-rpath," + str(lib),
                    "-Wl,--gc-sections",
                    "-lubox",
                    "-l:libnl-3.so.200",
                    "-l:libelf.so.1",
                    "-lz",
                    "-o",
                    build / "bin" / binary,
                ],
            )
    for name in [
        "harness.c",
        "test-rebind.py",
        "build.py",
        "run.sh",
        "pins.json",
        "fix.patch",
        "base.commit",
        "candidate.commit",
        "README.md",
    ]:
        shutil.copyfile(HERE / name, evidence / name)
    proof = dict(
        passed=True,
        sources=sources,
        shared_bpf_inputs=shared,
        libubox_sha256=hashlib.sha256((lib / "libubox.so").read_bytes()).hexdigest(),
        libbpf_sha256=hashlib.sha256((libbpf / "libbpf.a").read_bytes()).hexdigest(),
        fixture_scope=[
            "netifd resolver",
            "lookup of real pinned production program FDs",
            "abort-only guard for unused bridger callback retained by ASan globals",
        ],
        sanitizers="ASan/UBSan enabled; LSan disabled due to pre-existing nl_socket_get_cb reference leak; no leak-free claim",
        hardware_tested=False,
        daemon_ubus_tested=False,
        ingress_ifb_tested=False,
    )
    (evidence / "source-verification.json").write_text(
        json.dumps(proof, indent=2) + "\n"
    )
    print(
        "Both complete source Git roots restored; full interface.c + main.c built native and ASan/UBSan"
    )


if __name__ == "__main__":
    main()
