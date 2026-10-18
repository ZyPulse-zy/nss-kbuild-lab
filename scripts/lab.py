#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Reproduce complete NSS Kbuild regressions from content-pinned public inputs."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from lab_io import Evidence, acquire, digest, extract_archive, tree_digest
from restore_git import restore_git

COMPONENT = Path(__file__).resolve().parents[1]
PATCH_GROUPS = [
    "build",
    "subsys",
    "ath",
    "ath5k",
    "ath9k",
    "ath10k",
    "ath11k",
    "ath12k",
    "rt2x00",
    "mt7601u",
    "mwl",
    "brcm",
    "rtl",
    "nss/subsys",
    "nss/ath10k",
    "nss/ath11k",
]
DUPLICATE_HEADERS = [
    "include/linux/ssb",
    "include/linux/bcma",
    "include/net/bluetooth",
    "include/linux/cordic.h",
    "include/linux/crc8.h",
    "include/linux/eeprom_93cx6.h",
    "include/linux/wl12xx.h",
    "include/linux/mhi.h",
    "include/net/ieee80211.h",
    "backport-include/linux/bcm47xx_nvram.h",
]


def prepare_sources(manifest, workspace, evidence, archives):
    source = workspace / "openwrt"
    extract_archive(archives["source.tar.gz"], source)
    source_proof = restore_git(
        source,
        (COMPONENT / manifest["source"]["git_commit_file"]).read_bytes(),
        manifest["source"]["commit"],
    )
    feed = workspace / "nss-feed"
    extract_archive(archives["nss-feed.tar.gz"], feed)
    feed_proof = restore_git(
        feed,
        (COMPONENT / manifest["nss_feed"]["git_commit_file"]).read_bytes(),
        manifest["nss_feed"]["commit"],
        branch="NSS-12.5-K6.x",
    )
    sdk = workspace / "sdk"
    omitted_sdk_links = extract_archive(
        archives["sdk.tar.zst"], sdk, manifest["sdk"]["absolute_host_links"]
    )
    if (sdk / "staging_dir/host/.prereq-build").exists():
        raise ValueError("SDK unexpectedly contains a completed host-prerequisite stamp")
    shutil.copytree(feed, source / "package/nss-feed")
    (source / ".config").write_bytes((COMPONENT / manifest["config"]).read_bytes())
    (source / "staging_dir").mkdir()
    for name in ("host", manifest["sdk"]["toolchain"]):
        (source / "staging_dir" / name).symlink_to(
            sdk / "staging_dir" / name, target_is_directory=True
        )
    (source / "dl").mkdir()
    shutil.copyfile(
        archives["kernel.tar.xz"],
        source / "dl" / ("linux-" + manifest["kernel"]["version"] + ".tar.xz"),
    )
    dependency_proofs = []
    (workspace / "dependencies").mkdir()
    for item in manifest["dependencies"]:
        target = workspace / "dependencies" / item["name"]
        extract_archive(archives[item["name"] + ".tar.gz"], target)
        patches = sorted((source / item["recipe"] / "patches").glob("*.patch"))
        actual = [{"path": patch.name, "sha256": digest(patch)} for patch in patches]
        if actual != item["patches"]:
            raise ValueError("Package patch series changed: " + item["name"])
        for patch in patches:
            evidence.run(
                "patch-" + item["name"] + "-" + patch.stem,
                [
                    "patch",
                    "--batch",
                    "--fuzz=0",
                    "--no-backup-if-mismatch",
                    "-p1",
                    "-d",
                    str(target),
                    "-i",
                    str(patch),
                ],
                timeout=30,
            )
        dependency_proofs.append(
            {
                "name": item["name"],
                "source_commit": item["source_commit"],
                "archive_sha256": item["archive_sha256"],
                "patches": actual,
            }
        )
    candidate_root = workspace / "candidate"
    candidate_recipe = candidate_root / "package/kernel/mac80211"
    shutil.copytree(source / "package/kernel/mac80211", candidate_recipe)
    patch = COMPONENT / manifest["combined_patch"]
    if digest(patch) != manifest["combined_patch_sha256"]:
        raise ValueError("Candidate patch digest mismatch")
    evidence.run(
        "candidate-patch",
        ["patch", "--batch", "--fuzz=0", "-p1", "-d", str(candidate_root), "-i", str(patch)],
        timeout=30,
    )
    prepared = workspace / "prepared"
    prepared.mkdir()
    backports_proofs = []
    for variant, recipe in [
        ("base", source / "package/kernel/mac80211"),
        ("candidate", candidate_recipe),
    ]:
        target = prepared / ("backports-" + variant)
        extract_archive(archives["backports.tar.zst"], target)
        patches = []
        for group in PATCH_GROUPS:
            for file in sorted((recipe / "patches" / group).glob("*.patch")):
                evidence.run(
                    "backports-" + variant + "-" + group.replace("/", "-") + "-" + file.stem,
                    ["patch", "--batch", "--fuzz=0", "-p1", "-d", str(target), "-i", str(file)],
                    timeout=30,
                )
                patches.append({"path": str(file.relative_to(recipe)), "sha256": digest(file)})
        for relative in DUPLICATE_HEADERS:
            file = target / relative
            if file.is_symlink() or file.is_file():
                file.unlink()
            elif file.is_dir():
                shutil.rmtree(file)
        release = re.search(r"^PKG_RELEASE:=(\d+)$", (recipe / "Makefile").read_text(), re.M).group(
            1
        )
        (target / "compat_version").write_text(
            "compat-wireless-"
            + manifest["backports"]["version"]
            + "-"
            + release
            + "-lab-"
            + manifest["source"]["commit"][:7]
            + "\n"
        )
        backports_proofs.append(
            {
                "variant": variant,
                "patch_count": len(patches),
                "patches": patches,
                "source_sha256": {
                    name: digest(target / name)
                    for name in ("drivers/net/wireless/ath/ath11k/nss.c", "net/mac80211/tx.c")
                },
            }
        )
        actual = backports_proofs[-1]
        expected = manifest["expected_prepared_sources"][variant]
        if (
            actual["patch_count"] != expected["patch_count"]
            or actual["source_sha256"] != expected["source_sha256"]
        ):
            raise ValueError("Prepared backports differs from the pinned recipe output: " + variant)
    proof = {
        "source": source_proof,
        "nss_feed": feed_proof,
        "dependencies": dependency_proofs,
        "backports": backports_proofs,
        "candidate_patch_sha256": digest(patch),
        "sdk_builder_host_links_omitted": omitted_sdk_links,
        "original_workspace_used": False,
        "immutable_trees": {
            name: tree_digest(workspace / name)
            for name in ("dependencies", "prepared", "candidate", "openwrt/package/nss-feed")
        },
    }
    (workspace / "source-proof.json").write_text(json.dumps(proof, indent=2) + "\n")


def build_kernel(manifest, workspace, evidence, jobs):
    source = workspace / "openwrt"
    env = os.environ.copy()
    for name in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL"):
        env.pop(name, None)
    tools = {
        key: shutil.which(value)
        for key, value in [("YACC", "bison"), ("LEX", "flex"), ("M4", "m4")]
    }
    if not all(tools.values()):
        raise ValueError("Native bison, flex and m4 are required on PATH")
    command = [
        "make",
        "-C",
        str(source),
        "SOURCE_DATE_EPOCH=" + str(manifest["source_date_epoch"]),
        *[key + "=" + value for key, value in tools.items()],
    ]
    if env.get("BISON_PKGDATADIR"):
        command += ["BISON_PKGDATADIR=" + env["BISON_PKGDATADIR"]]
    evidence.run("defconfig", command + ["defconfig"], env=env, timeout=180)
    configuration = (source / ".config").read_text()
    for required in (
        "CONFIG_ATH11K_NSS_SUPPORT=y",
        "CONFIG_NSS_FIRMWARE_VERSION_12_5=y",
        'CONFIG_GCC_VERSION="14.3.0"',
        "CONFIG_TARGET_"
        + manifest["target"]["board"]
        + "_"
        + manifest["target"]["subtarget"]
        + "=y",
    ):
        if required not in configuration:
            raise ValueError("Required build option missing: " + required)
    evidence.run(
        "kernel-prepare",
        command + ["target/linux/prepare", "V=s", "-j" + str(jobs)],
        env=env,
        timeout=600,
    )
    evidence.run(
        "kernel-compile",
        command + ["target/linux/compile", "V=s", "-j" + str(jobs)],
        env=env,
        timeout=1800,
    )
    pattern = (
        "build_dir/target-*/linux-"
        + manifest["target"]["board"]
        + "_"
        + manifest["target"]["subtarget"]
        + "/linux-"
        + manifest["kernel"]["version"]
    )
    matches = list(source.glob(pattern))
    if len(matches) != 1:
        raise ValueError("Expected exactly one complete target kernel")
    kernel = matches[0]
    proof = {
        "passed": True,
        "openwrt_config_sha256": digest(source / ".config"),
        "kernel": str(kernel.relative_to(workspace)),
        "inputs": {
            name: digest(kernel / name)
            for name in (
                ".config",
                "Module.symvers",
                "include/generated/autoconf.h",
                "arch/arm64/boot/Image",
            )
        },
    }
    (workspace / "kernel-proof.json").write_text(json.dumps(proof, indent=2) + "\n")


def build_modules(manifest_path, workspace, evidence, jobs):
    source_proof = json.loads((workspace / "source-proof.json").read_text())
    if any(
        tree_digest(workspace / name) != expected
        for name, expected in source_proof["immutable_trees"].items()
    ):
        raise ValueError("Prepared source inputs changed after verification")
    kernel_proof = json.loads((workspace / "kernel-proof.json").read_text())
    kernel = workspace / kernel_proof["kernel"]
    if not kernel_proof["passed"] or any(
        digest(kernel / name) != expected for name, expected in kernel_proof["inputs"].items()
    ):
        raise ValueError("Cannot reuse changed or incomplete kernel inputs")
    if digest(workspace / "openwrt/.config") != kernel_proof["openwrt_config_sha256"]:
        raise ValueError("OpenWrt profile changed after kernel compilation")
    evidence.run(
        "verify-tracked-framework",
        [
            "git",
            "-C",
            str(workspace / "openwrt"),
            "-c",
            "core.hooksPath=/dev/null",
            "diff",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
        ],
        timeout=30,
    )
    output = workspace / "modules"
    attempt = 2
    while output.exists():
        output = workspace / ("modules-attempt" + str(attempt))
        attempt += 1
    evidence.run(
        "complete-modules",
        [
            sys.executable,
            str(COMPONENT / "scripts/module_build.py"),
            "--manifest",
            str(manifest_path),
            "--source",
            str(workspace / "openwrt"),
            "--sdk",
            str(workspace / "sdk"),
            "--sources",
            str(workspace / "dependencies"),
            "--prepared",
            str(workspace / "prepared"),
            "--candidate-recipe",
            str(workspace / "candidate/package/kernel/mac80211"),
            "--output",
            str(output),
            "--jobs",
            str(jobs),
        ],
        timeout=1800,
    )
    report = json.loads((output / "result.json").read_text())
    if not report["passed"] or len(report["artifacts"]) != 20:
        raise ValueError("Incomplete module build proof")
    return str(output.relative_to(workspace))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--profile", choices=["ipq60xx"], default="ipq60xx")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--archive-cache", type=Path, help="read-only cache with SHA256 filenames")
    parser.add_argument(
        "--modules-only",
        action="store_true",
        help="reuse only a previously verified kernel; keep new module attempt logs",
    )
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if any(char.isspace() for char in str(workspace)):
        raise ValueError("OpenWrt requires a workspace path without whitespace")
    manifest_path = COMPONENT / "pins" / (args.profile + ".json")
    manifest = json.loads(manifest_path.read_text())
    context = {
        "schema": 1,
        "manifest_sha256": digest(manifest_path),
        "profile_sha256": digest(COMPONENT / manifest["config"]),
    }
    if args.modules_only:
        if json.loads((workspace / ".nss-kbuild-lab.json").read_text()) != context:
            raise ValueError("Workspace does not match this pinned profile")
    else:
        workspace.mkdir(exist_ok=False)
        (workspace / ".nss-kbuild-lab.json").write_text(json.dumps(context, indent=2) + "\n")
    evidence = Evidence(workspace)
    report = {
        "passed": False,
        "profile": args.profile,
        "context": context,
        "harness_sha256": {
            str(file.relative_to(COMPONENT)): digest(file)
            for file in sorted((COMPONENT / "scripts").glob("*.py"))
        },
        "modules_loaded": False,
        "firmware_or_hardware_tested": False,
    }
    try:
        if not args.modules_only:
            specs = [
                ("source.tar.gz", manifest["source"]),
                ("nss-feed.tar.gz", manifest["nss_feed"]),
                ("sdk.tar.zst", manifest["sdk"]),
                ("kernel.tar.xz", manifest["kernel"]),
                ("backports.tar.zst", manifest["backports"]),
            ]
            specs += [
                (item["name"] + ".tar.gz", {"url": item["url"], "sha256": item["archive_sha256"]})
                for item in manifest["dependencies"]
            ]
            archives = {}
            # Each download has separate logs and immutable destinations. Evidence
            # recording is sequential so command history cannot race or lose entries.
            for name, spec in specs:
                archives[name] = acquire(spec, name, workspace, evidence, args.archive_cache)
            prepare_sources(manifest, workspace, evidence, archives)
            build_kernel(manifest, workspace, evidence, args.jobs)
        report["module_output"] = build_modules(manifest_path, workspace, evidence, args.jobs)
        report["passed"] = True
    except BaseException as exc:
        report["failure"] = str(exc)
        raise
    finally:
        destination = workspace / "result.json"
        if destination.exists():
            history = workspace / "previous-results"
            history.mkdir(exist_ok=True)
            import time

            shutil.copy2(destination, history / (str(time.time_ns()) + ".json"))
        destination.write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: complete pinned NSS Kbuild and source-interface regressions", flush=True)


if __name__ == "__main__":
    main()
