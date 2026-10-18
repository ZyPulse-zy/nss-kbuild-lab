#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the complete qosify object against real kernel BPF test-run packets."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    value = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while value >> 16:
        value = (value & 65535) + (value >> 16)
    return (~value) & 65535


def packet(
    version,
    kind,
    payload,
    ecn,
    vlans,
    ingress=False,
    dscp=0,
    src_addr=None,
    dst_addr=None,
):
    source, dest = (12345, 23456) if ingress else (23456, 12345)
    if kind == "udp-unmatched":
        if ingress:
            source += 1
        else:
            dest += 1
    proto = {
        "udp": 17,
        "udp-unmatched": 17,
        "tcp": 6,
        "icmp": 1 if version == 4 else 58,
    }[kind]
    if kind.startswith("udp"):
        transport = struct.pack("!HHHH", source, dest, 8 + payload, 0) + b"x" * payload
        check_offset = 6
    elif kind == "tcp":
        transport = (
            struct.pack("!HHIIBBHHH", source, dest, 1, 0, 0x50, 0x10, 4096, 0, 0)
            + b"x" * payload
        )
        check_offset = 16
    else:
        transport = (
            struct.pack("!BBHHH", 8 if version == 4 else 128, 0, 0, 1, 1)
            + b"x" * payload
        )
        check_offset = 2
    if version == 4:
        src, dst = (
            socket.inet_pton(socket.AF_INET, ip)
            for ip in (src_addr or "192.0.2.1", dst_addr or "198.51.100.1")
        )
        pseudo = src + dst + struct.pack("!BBH", 0, proto, len(transport))
        header = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            dscp << 2 | ecn,
            20 + len(transport),
            123,
            0,
            64,
            proto,
            0,
            src,
            dst,
        )
        header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
        etype = 0x0800
    else:
        src, dst = (
            socket.inet_pton(socket.AF_INET6, ip)
            for ip in (src_addr or "2001:db8::1", dst_addr or "2001:db8::2")
        )
        pseudo = src + dst + struct.pack("!I3xB", len(transport), proto)
        header = struct.pack(
            "!IHBB16s16s",
            6 << 28 | (dscp << 2 | ecn) << 20 | 0xABCDE,
            len(transport),
            proto,
            64,
            src,
            dst,
        )
        etype = 0x86DD
    prefix = b"" if version == 4 and kind == "icmp" else pseudo
    value = checksum(prefix + transport) or 65535
    transport = (
        transport[:check_offset]
        + struct.pack("!H", value)
        + transport[check_offset + 2 :]
    )
    tags = [0x88A8, 0x8100][:vlans] if vlans == 2 else [0x8100] * vlans
    ethernet = bytes.fromhex("020000000002020000000001") + struct.pack(
        "!H", tags[0] if tags else etype
    )
    for index, tag in enumerate(tags):
        ethernet += struct.pack(
            "!HH", 100 + index, tags[index + 1] if index + 1 < len(tags) else etype
        )
    return ethernet + header + transport, len(ethernet), prefix


def set_flags(data, flags):
    """Initialize exactly the same four-byte rodata as qosify_fill_rodata()."""
    result = bytearray(data)
    assert result[:6] == b"\x7fELF\x02\x01"
    section_offset = struct.unpack_from("<Q", result, 40)[0]
    section_size, count, names_index = struct.unpack_from("<HHH", result, 58)
    sections = [
        struct.unpack_from("<IIQQQQIIQQ", result, section_offset + i * section_size)
        for i in range(count)
    ]
    names = sections[names_index]
    strings = result[names[4] : names[4] + names[5]]
    found = 0
    for section in sections:
        name = strings[section[0] :].split(b"\0", 1)[0]
        if name == b".rodata":
            assert section[5] == 4
            result[section[4] : section[4] + 4] = struct.pack("<I", flags)
            found += 1
    assert found == 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expect-failure", action="store_true")
    args = parser.parse_args()
    for namespace in ("net", "mnt"):
        parent = os.environ.get("QOSIFY_PARENT_" + namespace.upper() + "NS")
        if (
            os.geteuid() != 0
            or not parent
            or parent == os.readlink("/proc/self/ns/" + namespace)
        ):
            raise SystemExit(
                "Refusing BPF test without private root network and mount namespaces"
            )
    obj = args.object.resolve(strict=True).read_bytes()
    records = []
    failures = []

    def run(*words):
        return subprocess.run(
            [str(w) for w in words], text=True, check=True, capture_output=True
        )

    with tempfile.TemporaryDirectory(prefix="qosify-matrix-") as directory:
        tmp = Path(directory)
        pins = tmp / "pins"
        pins.mkdir()
        mounted = []
        try:
            run("mount", "--make-rprivate", "/")
            for ingress in (False, True):
                # A fresh bpffs prevents map reuse across modes and confines
                # the source object's real LIBBPF_PIN_BY_NAME maps.
                run("mount", "-t", "bpf", "bpf", pins)
                mounted.append(str(pins))
                run("mount", "--bind", pins, "/sys/fs/bpf")
                mounted.append("/sys/fs/bpf")
                selected = tmp / "selected.o"
                selected.write_bytes(set_flags(obj, int(ingress)))
                try:
                    loaded = run(
                        "bpftool",
                        "prog",
                        "load",
                        selected,
                        pins / "classifier",
                        "type",
                        "classifier",
                    )
                except subprocess.CalledProcessError as error:
                    args.report.with_suffix(".load-failure.log").write_text(
                        error.stderr
                    )
                    raise
                assert loaded.returncode == 0
                # No verifier debug output or host kernel addresses are exported.
                key = struct.pack("=I", socket.htons(12345))
                for name in ("udp_ports", "tcp_ports"):
                    run(
                        "bpftool",
                        "map",
                        "update",
                        "pinned",
                        pins / name,
                        "key",
                        "hex",
                        *[f"{b:02x}" for b in key],
                        "value",
                        "hex",
                        "2e",
                    )
                run(
                    "bpftool",
                    "map",
                    "update",
                    "pinned",
                    pins / "config",
                    "key",
                    "hex",
                    "00",
                    "00",
                    "00",
                    "00",
                    "value",
                    "hex",
                    "2e",
                )

                def check_packet(
                    version,
                    kind,
                    payload,
                    ecn,
                    vlans,
                    cut=None,
                    initial_dscp=0,
                    expected=None,
                ):
                    raw, offset, pseudo = packet(
                        version, kind, payload, ecn, vlans, ingress, initial_dscp
                    )
                    if cut is not None:
                        raw = raw[: offset + (20 if version == 4 else 40) + cut]
                    inp, out = tmp / "input", tmp / "output"
                    inp.write_bytes(raw)
                    result = json.loads(
                        run(
                            "bpftool",
                            "-j",
                            "prog",
                            "run",
                            "pinned",
                            pins / "classifier",
                            "data_in",
                            inp,
                            "data_out",
                            out,
                            "data_size_out",
                            "4096",
                        ).stdout
                    )
                    data = out.read_bytes()
                    assert len(raw) == len(data)
                    assert result["retval"] == 0xFFFFFFFF
                    dsfield = (
                        data[offset + 1]
                        if version == 4
                        else int.from_bytes(data[offset : offset + 2], "big") >> 4 & 255
                    )
                    assert dsfield & 3 == ecn
                    if version == 4:
                        assert checksum(data[offset : offset + 20]) == 0
                    else:
                        assert (
                            int.from_bytes(data[offset : offset + 4], "big") & 0xFFFFF
                            == 0xABCDE
                        )
                    if cut is None:
                        transport = data[offset + (20 if version == 4 else 40) :]
                        assert checksum(pseudo + transport) == 0
                    if expected is None:
                        expected = (
                            0 if cut is not None or kind == "udp-unmatched" else 46
                        )
                    record = dict(
                        ingress=ingress,
                        ipv=version,
                        protocol=kind,
                        payload=payload,
                        vlans=vlans,
                        ecn=ecn,
                        truncated_transport=cut,
                        initial_dscp=initial_dscp,
                        dscp=dsfield >> 2,
                        expected_dscp=expected,
                        bytes=len(data),
                    )
                    records.append(record)
                    if dsfield >> 2 != expected:
                        failures.append(record)
                    elif expected == 0:
                        assert data == raw

                for version in (4, 6):
                    for vlans in (0, 1, 2):
                        for ecn in range(4):
                            for length in (0, 1, 64):
                                for kind in ("udp", "udp-unmatched", "tcp", "icmp"):
                                    check_packet(version, kind, length, ecn, vlans)
                            for cut in range(8):
                                check_packet(version, "udp", 0, ecn, vlans, cut=cut)
                # Existing DSCP fallback semantics must also hold at the boundary.
                run(
                    "bpftool",
                    "map",
                    "update",
                    "pinned",
                    pins / "udp_ports",
                    "key",
                    "hex",
                    *[f"{b:02x}" for b in key],
                    "value",
                    "hex",
                    "6e",
                )
                for version in (4, 6):
                    for length in (0, 1):
                        for initial in (0, 20):
                            check_packet(
                                version,
                                "udp",
                                length,
                                3,
                                0,
                                initial_dscp=initial,
                                expected=initial or 46,
                            )
                run("umount", mounted.pop())
                run("umount", mounted.pop())
        finally:
            for mount in reversed(mounted):
                run("umount", mount)
    report = dict(
        object_sha256=hashlib.sha256(obj).hexdigest(),
        cases=len(records),
        mismatches=failures,
        results=records,
        passed=not failures,
        real_kernel_bpf=True,
        hardware_tested=False,
        modes=["Ethernet egress", "Ethernet ingress"],
    )
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"{len(records)} actual BPF packet cases; {len(failures)} classification mismatches"
    )
    if args.expect_failure:
        assert failures and all(
            x["payload"] == 0
            and x["truncated_transport"] is None
            and x["protocol"] in ("udp", "icmp")
            for x in failures
        )
    else:
        assert not failures, failures[:3]


if __name__ == "__main__":
    main()
