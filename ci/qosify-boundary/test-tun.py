#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise real L3 TC ingress/egress using an isolated, nonpersistent TUN."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import tempfile
import time

spec = importlib.util.spec_from_file_location(
    "matrix", Path(__file__).with_name("test-matrix.py")
)
util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(util)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expect-failure", action="store_true")
    args = parser.parse_args()
    for ns in ("net", "mnt"):
        parent = os.environ.get("QOSIFY_PARENT_" + ns.upper() + "NS")
        if (
            os.geteuid() != 0
            or not parent
            or parent == os.readlink("/proc/self/ns/" + ns)
        ):
            raise SystemExit(
                "Refusing TC test without private root network and mount namespaces"
            )

    def run(*words):
        return subprocess.run(
            [str(w) for w in words], text=True, capture_output=True, check=True
        )

    records = []
    failures = []
    obj = args.object.resolve(strict=True).read_bytes()
    with tempfile.TemporaryDirectory(prefix="qosify-tun-") as directory:
        tmp = Path(directory)
        pins = tmp / "pins"
        pins.mkdir()
        mounts = []
        tun = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
        try:
            actual = fcntl.ioctl(
                tun, 0x400454CA, struct.pack("16sH", b"qosify%d", 1 | 0x1000)
            )
            name = actual[:16].split(b"\0", 1)[0].decode()
            run("ip", "link", "set", name, "up")
            run("ip", "address", "add", "192.0.2.1", "peer", "192.0.2.2", "dev", name)
            run("ip", "-6", "address", "add", "2001:db8::1/64", "dev", name, "nodad")
            run("mount", "--make-rprivate", "/")
            for ingress in (False, True):
                run("mount", "-t", "bpf", "bpf", pins)
                mounts.append(str(pins))
                run("mount", "--bind", pins, "/sys/fs/bpf")
                mounts.append("/sys/fs/bpf")
                selected = tmp / "selected.o"
                selected.write_bytes(util.set_flags(obj, 2 | int(ingress)))
                run(
                    "bpftool",
                    "prog",
                    "load",
                    selected,
                    pins / "classifier",
                    "type",
                    "classifier",
                )
                key = struct.pack("=I", socket.htons(12345))
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
                    "2e",
                )
                run("tc", "qdisc", "add", "dev", name, "clsact")
                run(
                    "tc",
                    "filter",
                    "add",
                    "dev",
                    name,
                    "ingress" if ingress else "egress",
                    "bpf",
                    "da",
                    "pinned",
                    pins / "classifier",
                )
                try:
                    for version in (4, 6):
                        family = socket.AF_INET if version == 4 else socket.AF_INET6
                        local = "192.0.2.1" if version == 4 else "2001:db8::1"
                        peer = "192.0.2.2" if version == 4 else "2001:db8::2"
                        with socket.socket(family, socket.SOCK_DGRAM) as udp:
                            udp.bind((local, 23456))
                            udp.settimeout(2)
                            if ingress:
                                udp.setsockopt(
                                    (
                                        socket.IPPROTO_IP
                                        if version == 4
                                        else socket.IPPROTO_IPV6
                                    ),
                                    (
                                        socket.IP_RECVTOS
                                        if version == 4
                                        else socket.IPV6_RECVTCLASS
                                    ),
                                    1,
                                )
                            for payload in (0, 1, 64):
                                for ecn in range(4):
                                    for match in (False, True):
                                        if ingress:
                                            raw, offset, pseudo = util.packet(
                                                version,
                                                "udp" if match else "udp-unmatched",
                                                payload,
                                                ecn,
                                                0,
                                                True,
                                                src_addr=peer,
                                                dst_addr=local,
                                            )
                                            assert (
                                                os.write(tun, raw[offset:])
                                                == len(raw) - offset
                                            )
                                            data, ancillary, flags, address = (
                                                udp.recvmsg(4096, 128)
                                            )
                                            assert data == b"x" * payload
                                            values = [
                                                item[2]
                                                for item in ancillary
                                                if item[0]
                                                == (
                                                    socket.IPPROTO_IP
                                                    if version == 4
                                                    else socket.IPPROTO_IPV6
                                                )
                                                and item[1]
                                                == (
                                                    socket.IP_TOS
                                                    if version == 4
                                                    else socket.IPV6_TCLASS
                                                )
                                            ]
                                            assert len(values) == 1, ancillary
                                            dsfield = (
                                                values[0][0]
                                                if version == 4
                                                else struct.unpack("=i", values[0])[0]
                                            )
                                        else:
                                            udp.setsockopt(
                                                (
                                                    socket.IPPROTO_IP
                                                    if version == 4
                                                    else socket.IPPROTO_IPV6
                                                ),
                                                (
                                                    socket.IP_TOS
                                                    if version == 4
                                                    else socket.IPV6_TCLASS
                                                ),
                                                ecn,
                                            )
                                            port = 12345 if match else 12346
                                            assert (
                                                udp.sendto(b"x" * payload, (peer, port))
                                                == payload
                                            )
                                            deadline = time.monotonic() + 2
                                            while True:
                                                left = deadline - time.monotonic()
                                                assert (
                                                    left > 0
                                                    and select.select(
                                                        [tun], [], [], left
                                                    )[0]
                                                ), "No expected TUN packet"
                                                raw = os.read(tun, 65536)
                                                if raw[0] >> 4 != version:
                                                    continue
                                                offset = (
                                                    (raw[0] & 15) * 4
                                                    if version == 4
                                                    else 40
                                                )
                                                proto = (
                                                    raw[9] if version == 4 else raw[6]
                                                )
                                                if proto != 17 or len(raw) < offset + 8:
                                                    continue
                                                if struct.unpack(
                                                    "!HH", raw[offset : offset + 4]
                                                ) != (23456, port):
                                                    continue
                                                assert (
                                                    raw[offset + 8 :] == b"x" * payload
                                                )
                                                dsfield = (
                                                    raw[1]
                                                    if version == 4
                                                    else int.from_bytes(raw[:2], "big")
                                                    >> 4
                                                    & 255
                                                )
                                                if version == 4:
                                                    assert (
                                                        util.checksum(raw[:offset]) == 0
                                                    )
                                                break
                                        assert dsfield & 3 == ecn
                                        record = dict(
                                            ingress=ingress,
                                            ipv=version,
                                            payload=payload,
                                            ecn=ecn,
                                            matched=match,
                                            dscp=dsfield >> 2,
                                            expected_dscp=46 if match else 0,
                                        )
                                        records.append(record)
                                        if record["dscp"] != record["expected_dscp"]:
                                            failures.append(record)
                finally:
                    run("tc", "qdisc", "del", "dev", name, "clsact")
                run("umount", mounts.pop())
                run("umount", mounts.pop())
        finally:
            os.close(tun)
            for target in reversed(mounts):
                run("umount", target)
    args.report.write_text(
        json.dumps(
            dict(
                passed=not failures,
                cases=len(records),
                mismatches=failures,
                results=records,
                object_sha256=hashlib.sha256(obj).hexdigest(),
                modes=["L3 TC egress", "L3 TC ingress"],
                actual_tun_packets=True,
                hardware_tested=False,
            ),
            indent=2,
        )
        + "\n"
    )
    print(
        f"{len(records)} actual TUN/TC datagrams; {len(failures)} classification mismatches"
    )
    if args.expect_failure:
        assert failures and all(
            item["payload"] == 0 and item["matched"] for item in failures
        )
    else:
        assert not failures, failures[:3]


if __name__ == "__main__":
    main()
