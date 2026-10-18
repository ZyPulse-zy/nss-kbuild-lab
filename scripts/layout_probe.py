# SPDX-License-Identifier: GPL-2.0-only
"""Compile complete source headers with recorded real Kbuild flags.

These are source-interface layout checks, not firmware wire-format evidence.
"""

import hashlib
from pathlib import Path
import shlex
import struct
import subprocess


def probe_layout(build: Path, env, kernel: Path, mode="retry"):
    if mode not in ("retry", "peer"):
        raise ValueError("Unknown source-interface probe mode")
    output = build / "compiled-layout-probes"
    attempt = 2
    while output.exists():
        output = build / ("compiled-layout-probes-attempt" + str(attempt))
        attempt += 1
    output.mkdir(exist_ok=False)
    records = {}
    for label, folder, command, includes in [
        (
            "driver",
            build / "qca-nss-drv",
            ".nss_wifili.o.cmd",
            [
                "nss_tx_rx_common.h",
                "nss_wifili_stats.h",
                "nss_wifili_log.h",
                "nss_wifili_strings.h",
            ],
        ),
        (
            "base",
            build / "backports-base",
            "drivers/net/wireless/ath/ath11k/.nss.o.cmd",
            ["drivers/net/wireless/ath/ath11k/core.h", "drivers/net/wireless/ath/ath11k/nss.h"],
        ),
        (
            "candidate",
            build / "backports-candidate",
            "drivers/net/wireless/ath/ath11k/.nss.o.cmd",
            ["drivers/net/wireless/ath/ath11k/core.h", "drivers/net/wireless/ath/ath11k/nss.h"],
        ),
    ]:
        raw = (folder / command).read_text().splitlines()[0].split(" := ", 1)[1]
        argv = shlex.split(raw)
        flags = [a for a in argv[: argv.index("-c")] if not a.startswith("-Wp,-MMD,")]
        # Older Kbuild compiles external modules from the kernel directory;
        # newer Kbuild uses the external module directory with absolute kernel
        # include paths. Preserve the recorded relative-path interpretation.
        cwd = kernel if "-I./arch/arm64/include" in flags else folder
        for i, flag in enumerate(flags[:-1]):
            if flag == "-include":
                assert (cwd / flags[i + 1]).is_file(), flags[i + 1]
        source = output / (label + ".c")
        text = "".join('#include "' + str(folder / path) + '"\n' for path in includes)
        text += (
            "static const unsigned int nss_layout_probe[] "
            '__attribute__((used, section(".nss_layout_probe"))) = {\n'
            "sizeof(struct nss_wifili_peer_ctrl_stats),\n"
        )
        # NSS 11.4 predates the separate retry structure. Probe the common
        # peer row instead of inventing fields absent from the actual header.
        if mode == "retry":
            text += (
                "__builtin_offsetof(struct nss_wifili_peer_ctrl_stats, retry),\n"
                "sizeof(struct nss_wifili_retry_ctrl_stats),\n"
            )
        text += "};\n"
        source.write_text(text)
        obj = output / (label + ".o")
        result = subprocess.run(
            flags + ["-c", "-o", str(obj), str(source)],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        (output / (label + ".log")).write_text(result.stdout)
        result.check_returncode()
        data = output / (label + ".bin")
        objcopy = argv[0].removesuffix("gcc") + "objcopy"
        subprocess.run(
            [objcopy, "--dump-section", ".nss_layout_probe=" + str(data), str(obj)],
            check=True,
            capture_output=True,
            timeout=10,
            env=env,
        )
        values = struct.unpack("<III" if mode == "retry" else "<I", data.read_bytes())
        records[label] = {
            "row_bytes": values[0],
            "selected_12_5": "-DNSS_FIRMWARE_VERSION_12_5" in flags,
            "saved_kbuild_command_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "probe_object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest(),
            "complete_headers": True,
            "firmware_validated": False,
            "probe_mode": mode,
        }
        if mode == "retry":
            records[label].update(retry_offset=values[1], retry_bytes=values[2])
        records[label]["compile_working_directory"] = str(cwd)
        records[label]["probe_relative_directory"] = str(output.relative_to(build))
    if mode == "retry":
        assert records["driver"]["row_bytes"] == records["candidate"]["row_bytes"] == 212
        assert records["base"]["row_bytes"] == 196
        assert records["base"]["retry_offset"] == 180
        assert records["candidate"]["retry_offset"] == records["driver"]["retry_offset"] == 188
    else:
        assert records["driver"]["row_bytes"] > 0
        assert len({record["row_bytes"] for record in records.values()}) == 1
    return records
