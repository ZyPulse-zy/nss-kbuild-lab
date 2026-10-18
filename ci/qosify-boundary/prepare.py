#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Prepare exact public qosify sources and an OpenWrt-style BPF build."""

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
from lab_io import extract_archive
from restore_git import restore_git


def run(log, argv, **kwargs):
    with log.open("w") as output:
        subprocess.run(
            [str(x) for x in argv],
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            **kwargs,
        )


CACHE = None


def acquire(spec, path):
    cached = CACHE / path.name if CACHE else None
    if (
        cached
        and cached.is_file()
        and hashlib.sha256(cached.read_bytes()).hexdigest() == spec["sha256"]
    ):
        path.symlink_to(cached.resolve())
        return

    if (
        not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != spec["sha256"]
    ):
        run(
            path.with_suffix(".download.log"),
            [
                "curl",
                "--fail",
                "--location",
                "--retry",
                "3",
                "--connect-timeout",
                "15",
                "--max-time",
                "300",
                spec["url"],
                "-o",
                path,
            ],
        )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    global CACHE
    CACHE = args.cache_dir.resolve() if args.cache_dir else None
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    evidence = workspace / "evidence"
    evidence.mkdir()
    pins = json.loads((HERE / "pins.json").read_text())
    for name, spec in [
        ("sdk.tar.zst", pins["sdk"]),
        ("kernel.tar.xz", pins["kernel"]),
        ("source.tar.gz", pins["qosify"]),
    ]:
        acquire(spec, workspace / name)
    sdk, kernel = workspace / "sdk", workspace / "kernel"
    omitted = extract_archive(
        workspace / "sdk.tar.zst", sdk, pins["sdk"]["absolute_host_links"]
    )
    extract_archive(workspace / "kernel.tar.xz", kernel)
    original, fixed = workspace / "original", workspace / "fixed"
    extract_archive(workspace / "source.tar.gz", original)
    shutil.copytree(original, fixed)
    base_proof = restore_git(
        original,
        (HERE / "base.commit").read_bytes(),
        pins["base_commit"],
        branch="master",
    )
    run(
        evidence / "apply-fix.log",
        ["patch", "--batch", "--fuzz=0", "-p1", "-i", HERE / "fix.patch"],
        cwd=fixed,
    )
    fixed_proof = restore_git(
        fixed,
        (HERE / "candidate.commit").read_bytes(),
        pins["candidate"],
        branch="fix/packet-boundary",
    )
    include = workspace / "include"
    (include / "bpf").mkdir(parents=True)
    for spec in pins["headers"]:
        path = include / spec["name"]
        acquire(spec, path)
    (kernel / "include/generated").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(include / "bounds.h", kernel / "include/generated/bounds.h")
    config = (kernel / "arch/mips/configs/generic_defconfig").read_text().splitlines()
    import re

    config = [
        line for line in config if not re.search(r"CONFIG_(CPU_.*ENDIAN|HZ)", line)
    ]
    (kernel / ".config").write_text(
        "\n".join(config)
        + "\nCONFIG_CPU_MIPS64_R2=y\nCONFIG_64BIT=y\nCONFIG_CPU_LITTLE_ENDIAN=y\nCONFIG_HZ_250=y\nCONFIG_HZ=250\n"
    )
    llvm = sdk / "staging_dir/host/llvm-bpf-21.1.6.Linux-x86_64/bin"
    make = [
        "make",
        "-C",
        kernel,
        "ARCH=mips",
        "KBUILD_SYM32=n",
        f"LLVM={llvm}/",
        f"CC={llvm}/clang --target=bpfel",
        "HOSTCC=gcc",
    ]
    run(evidence / "headers-configure.log", make + ["olddefconfig"])
    run(evidence / "headers-archprepare.log", make + ["-j4", "archprepare"])
    with (kernel / "include/generated/timeconst.h").open("w") as output:
        subprocess.run(
            ["bc", "-q", str(kernel / "kernel/time/timeconst.bc")],
            input="250\n",
            text=True,
            stdout=output,
            check=True,
        )
    with (include / "bpf/bpf_helper_defs.h").open("w") as output:
        subprocess.run(
            [
                sys.executable,
                str(kernel / "scripts/bpf_doc.py"),
                "--header",
                "--file",
                str(kernel / "tools/include/uapi/linux/bpf.h"),
            ],
            stdout=output,
            check=True,
        )
    for name in ["bpf_helpers.h", "bpf_endian.h"]:
        shutil.copyfile(kernel / "tools/lib/bpf" / name, include / "bpf" / name)
    toolchain = sdk / "staging_dir" / pins["sdk"]["toolchain"]
    options = [
        "-g",
        "-target",
        "mips64el-linux-gnu",
        "-nostdinc",
        "-isystem",
        toolchain / "lib/gcc/aarch64-openwrt-linux-musl/14.3.0/include",
        "-isystem",
        toolchain / "include",
    ]
    for path in [
        include,
        kernel / "arch/mips/include",
        kernel / "arch/mips/include/asm/mach-generic",
        kernel / "arch/mips/include/generated",
        kernel / "include",
        kernel / "arch/mips/include/uapi",
        kernel / "arch/mips/include/generated/uapi",
        kernel / "include/uapi",
        kernel / "include/generated/uapi",
        kernel / "tools/lib",
        kernel / "tools/testing/selftests",
        kernel / "samples/bpf",
    ]:
        options += ["-I", path]
    options += [
        "-include",
        "linux/kconfig.h",
        "-include",
        "asm_goto_workaround.h",
        "-D__KERNEL__",
        "-D__BPF_TRACING__",
        "-DCONFIG_GENERIC_CSUM",
        "-D__TARGET_ARCH_mips",
        "-mlittle-endian",
        "-fno-stack-protector",
        "-fno-asynchronous-unwind-tables",
        "-Wall",
        "-Wno-unused-value",
        "-Wno-pointer-sign",
        "-Wno-compare-distinct-pointer-types",
        "-Wno-gnu-variable-sized-type-not-at-end",
        "-Wno-address-of-packed-member",
        "-Wno-tautological-compare",
        "-Wno-unknown-warning-option",
        "-Wno-uninitialized",
        "-Wno-unused-variable",
        "-Wno-unused-label",
        "-O2",
        "-emit-llvm",
        "-Xclang",
        "-disable-llvm-passes",
    ]
    for label, source in [("original", original), ("fixed", fixed)]:
        bc, opt, obj = (evidence / (label + suffix) for suffix in [".bc", ".opt", ".o"])
        run(
            evidence / (label + "-clang.log"),
            [
                llvm / "clang",
                *options,
                "-I",
                source,
                "-c",
                source / "qosify-bpf.c",
                "-o",
                bc,
            ],
        )
        with bc.open("rb") as inp, opt.open("wb") as out:
            subprocess.run(
                [str(llvm / "opt"), "-O2", "-mtriple=bpfel"],
                stdin=inp,
                stdout=out,
                check=True,
            )
        with opt.open("rb") as inp:
            run(
                evidence / (label + "-llc.log"),
                [llvm / "llc", "-march=bpfel", "-mcpu=v3", "-filetype=obj", "-o", obj],
                stdin=inp,
            )
    # Build the CLI from the exact same verified Linux source. It does not
    # depend on a runner-specific /usr/sbin/bpftool wrapper or running-kernel ABI.
    bpftool = workspace / "bpftool-build"
    bpftool.mkdir()
    run(
        evidence / "bpftool-build.log",
        [
            "make",
            "-C",
            kernel / "tools/bpf/bpftool",
            f"OUTPUT={bpftool}/",
            "CC=gcc",
            "HOSTCC=gcc",
            "-j4",
        ],
    )
    for name in ["test-matrix.py", "test-tun.py"]:
        shutil.copyfile(HERE / name, evidence / name)
    shutil.copyfile(HERE / "pins.json", evidence / "pins.json")
    (evidence / "source-verification.json").write_text(
        json.dumps(
            dict(
                base=base_proof,
                candidate=fixed_proof,
                omitted_sdk_links=omitted,
                source_commit=pins["candidate"],
                hardware_tested=False,
            ),
            indent=2,
        )
        + "\n"
    )
    print(
        "Exact original/candidate Git trees, complete BPF objects and native bpftool built"
    )


if __name__ == "__main__":
    main()
