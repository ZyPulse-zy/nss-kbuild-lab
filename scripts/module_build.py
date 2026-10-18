#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compile actual upstream recipe targets against its complete 6.12 NSS kernel.

No framework/header stubs or kernel compatibility adapters are used. All stages
and module symbol tables stay in a new private output directory.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import platform
import shutil
import signal
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--jobs", type=int, choices=range(1, 9), default=4)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--sdk", type=Path, required=True)
parser.add_argument("--prepared", type=Path, required=True)
parser.add_argument("--sources", type=Path, required=True)
parser.add_argument("--candidate-recipe", type=Path, required=True)
parser.add_argument("--manifest", type=Path, required=True)
args = parser.parse_args()
component = Path(__file__).resolve().parents[1]
manifest = json.loads(args.manifest.read_text())
source = args.source.resolve()
sdk = args.sdk.resolve() / "staging_dir"
version = manifest["kernel"]["version"]
matches = list(
    source.glob(
        "build_dir/target-*/linux-"
        + manifest["target"]["board"]
        + "_"
        + manifest["target"]["subtarget"]
        + "/linux-"
        + version
    )
)
assert len(matches) == 1, "Require exactly one prepared target kernel"
kernel = matches[0]
for required in ("Module.symvers", "arch/arm64/boot/Image", "include/generated/autoconf.h"):
    assert (kernel / required).is_file(), required
out = args.output.resolve()
out.mkdir(exist_ok=False)
(out / "stage/usr/include").mkdir(parents=True)
(out / "symvers").mkdir()
(out / "evaluation").mkdir()
assert platform.system() == "Linux"
report = {
    "kernel": version,
    "openwrt_source": manifest["source"]["commit"],
    "nss_feed_source": manifest["nss_feed"]["commit"],
    "framework_stubs": False,
    "header_stubs": False,
    "kernel_source_adapters": [],
    "strict_modpost": True,
    "kernel_module_loaded": False,
    "passed": False,
    "phases": [],
    "artifacts": {},
}
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
kernel_inputs = {
    p: digest(kernel / p) for p in (".config", "Module.symvers", "include/generated/autoconf.h")
}
report["kernel_inputs"] = kernel_inputs
tool_names = {
    key: shutil.which(name) for key, name in [("YACC", "bison"), ("LEX", "flex"), ("M4", "m4")]
}
assert all(tool_names.values()), "Native bison, flex and m4 must be on PATH"
chain = sdk / manifest["sdk"]["toolchain"] / "bin"
cross = str(chain / manifest["sdk"]["cross"])
env = os.environ.copy()
env.update(
    PATH=str(chain) + ":" + str(sdk / "host/bin") + ":" + env["PATH"],
    STAGING_DIR=str(out / "stage"),
    KBUILD_MODPOST_WARN="",
)
for name in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL"):
    env.pop(name, None)
wrapper = out / "evaluation/recipe.mk"
wrapper.write_text("""include $(RECIPE)
define LAB_RECIPE_TARGETS
.PHONY: configure-module compile-module stage-headers export-variables
configure-module:
\t$(Build/Configure)
compile-module:
\t$(Build/Compile)
stage-headers:
\t$(call Build/InstallDev,$(HEADER_STAGE))
endef
$(eval $(LAB_RECIPE_TARGETS))
export-variables:
\t$(foreach var,$(VARIABLES),$(file >$(EXPORT_DIR)/$(var),$($(var))))
\t@true
""")
common = [
    "make",
    "-f",
    str(wrapper),
    "TOPDIR=" + str(source),
    "STAGING_DIR=" + str(out / "stage"),
    "STAGING_DIR_HOST=" + str(sdk / "host"),
    "PKG_SYMVERS_DIR=" + str(out / "symvers"),
    "LINUX_DIR=" + str(kernel),
    "KERNEL_BUILD_DIR=" + str(out / "kernel-metadata"),
    "KernelPackage=",
    "SOURCE_DATE_EPOCH=" + str(manifest["source_date_epoch"]),
    "HOST_OS=" + platform.system(),
    "HOST_ARCH=" + platform.machine(),
    "GNU_HOST_NAME="
    + subprocess.check_output([str(source / "scripts/config.guess")], text=True).strip(),
    "HEADER_STAGE=" + str(out / "stage"),
    "KBUILD_MODPOST_WARN=",
    "PKG_JOBS=-j" + str(args.jobs),
    *[key + "=" + value for key, value in tool_names.items()],
    *(["BISON_PKGDATADIR=" + env["BISON_PKGDATADIR"]] if env.get("BISON_PKGDATADIR") else []),
]


def save():
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def run(name, command, cwd=None, timeout=900):
    print(name, flush=True)
    log = out / (name + ".log")
    index = 2
    while log.exists():
        log = out / (name + "-attempt" + str(index) + ".log")
        index += 1
    began = time.monotonic()
    with log.open("w") as stream:
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=timeout)
        except BaseException:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise
    text = log.read_text(errors="replace")
    unresolved = [
        s
        for s in text.splitlines()
        if "modpost" in s and ("undefined" in s.lower() or "ERROR:" in s)
    ]
    report["phases"].append(
        {
            "name": name,
            "argv": command,
            "exit": code,
            "seconds": round(time.monotonic() - began, 3),
            "log": log.name,
            "log_sha256": digest(log),
            "unresolved_symbols": unresolved,
        }
    )
    save()
    if code or unresolved:
        raise RuntimeError(name + " failed; see " + str(log) + "\n" + text[-3000:])


def recipe_command(recipe, directory, goal, extras=None):
    return common + [
        "RECIPE=" + str(recipe / "Makefile"),
        "PKG_BUILD_DIR=" + str(directory),
        *(extras or []),
        goal,
    ]


def module_artifact(name, file):
    text = subprocess.check_output([cross + "readelf", "-h", str(file)], text=True)
    magic = subprocess.check_output(
        ["/usr/sbin/modinfo", "-F", "vermagic", str(file)], text=True
    ).strip()
    assert manifest["target"]["elf_machine"] in text and magic.startswith(version + " ")
    report["artifacts"][name] = {
        "sha256": digest(file),
        "bytes": file.stat().st_size,
        "vermagic": magic,
    }


try:
    report["dependency_pins"] = {x["name"]: x["source_commit"] for x in manifest["dependencies"]}
    for name, recipe_path in [
        ("qca-ssdk", "package/kernel/qca-ssdk"),
        ("qca-nss-dp", "package/kernel/qca-nss-dp"),
        ("qca-nss-drv", "package/nss-feed/qca-nss-drv"),
        ("qca-nss-clients", "package/nss-feed/qca-nss-clients"),
    ]:
        directory = out / name
        if not directory.exists():
            shutil.copytree(args.sources.resolve() / name, directory)
        recipe = source / recipe_path
        extras = []
        if name == "qca-nss-clients":
            extras = ["NSS_CLIENTS_MAKE_OPTS=wifi-meshmgr=m"]
            report["client_subset"] = "wifi-meshmgr=m; other optional client modules excluded"
        run(
            name + "-configure",
            recipe_command(recipe, directory, "configure-module", extras),
            recipe,
        )
        run(name + "-compile", recipe_command(recipe, directory, "compile-module", extras), recipe)
        run(name + "-headers", recipe_command(recipe, directory, "stage-headers", extras), recipe)
        symbols = directory / "Module.symvers"
        assert symbols.is_file() and symbols.stat().st_size > 0, str(symbols)
        shutil.copy2(symbols, out / "symvers" / (name + ".symvers"))
        modules = list(directory.rglob("*.ko"))
        assert modules, name
        for file in modules:
            module_artifact(name + "/" + str(file.relative_to(directory)), file)
    for variant, recipe in [
        ("base", source / "package/kernel/mac80211"),
        ("candidate", args.candidate_recipe.resolve()),
    ]:
        directory = out / ("backports-" + variant)
        if not directory.exists():
            shutil.copytree(args.prepared.resolve() / ("backports-" + variant), directory)
        # Source prepared with all recipe patches and duplicate-header removal;
        # no reverse-IOMMU or KCFLAGS adapter is applied on this native 6.12 stack.
        run(variant + "-compile", recipe_command(recipe, directory, "compile-module"), recipe)
        required = [
            "compat/compat.ko",
            "net/mac80211/mac80211.ko",
            "net/wireless/cfg80211.ko",
            "drivers/net/wireless/ath/ath11k/ath11k.ko",
            "drivers/net/wireless/ath/ath11k/ath11k_ahb.ko",
            "drivers/net/wireless/ath/ath11k/ath11k_pci.ko",
        ]
        assert all((directory / file).is_file() for file in required)
        for file in directory.rglob("*.ko"):
            module_artifact(variant + "/" + str(file.relative_to(directory)), file)
        command = directory / "drivers/net/wireless/ath/ath11k/.nss.o.cmd"
        defines = command.read_text().splitlines()[0]
        expected = variant == "candidate"
        assert ("-DNSS_FIRMWARE_VERSION_12_5" in defines) == expected
        report.setdefault("actual_nss_define", {})[variant] = expected
    assert kernel_inputs == {p: digest(kernel / p) for p in kernel_inputs}
    from layout_probe import probe_layout

    report["compiled_header_layout"] = probe_layout(out, env, kernel)
    report.update(passed=True, kernel_inputs_unchanged=True)
    save()
    print(
        "Actual upstream-default recipes, complete headers and strict module links passed",
        flush=True,
    )
except BaseException as exc:
    report["failure"] = str(exc)
    save()
    raise
