#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run production qosify interface lifecycle code with real TC/BPF/CAKE."""

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--object", type=Path, required=True)
    parser.add_argument("--bpftool", type=Path, required=True)
    parser.add_argument("--packet-util", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expect-failure", action="store_true")
    args = parser.parse_args()
    for ns in ("net", "mnt"):
        parent = os.environ.get("QOSIFY_PARENT_" + ns.upper() + "NS")
        if os.geteuid() or not parent or parent == os.readlink("/proc/self/ns/" + ns):
            raise SystemExit(
                "Refusing interface test without private root network and mount namespaces"
            )

    def run(*words):
        return subprocess.run(
            [str(w) for w in words], text=True, capture_output=True, check=True
        )

    spec = importlib.util.spec_from_file_location("packet_util", args.packet_util)
    util = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(util)
    obj = args.object.resolve(strict=True).read_bytes()
    records, failures = [], []
    run("mount", "--make-rprivate", "/")
    with tempfile.TemporaryDirectory(prefix="qosify-rebind-") as directory:
        temp = Path(directory)
        pins = temp / "pins"
        pins.mkdir()
        run("mount", "-t", "bpf", "bpf", pins)
        run("mount", "--bind", pins, "/sys/fs/bpf")
        try:
            for flags in range(4):
                selected = temp / f"mode{flags}.o"
                selected.write_bytes(util.set_flags(obj, flags))
                run(
                    args.bpftool,
                    "prog",
                    "load",
                    selected,
                    pins / f"prog{flags}",
                    "type",
                    "classifier",
                )
            key = struct.pack("=I", socket.htons(12345))
            run(
                args.bpftool,
                "map",
                "update",
                "pinned",
                pins / "udp_ports",
                "key",
                "hex",
                *[f"{v:02x}" for v in key],
                "value",
                "hex",
                "2e",
            )

            for case, modes in enumerate(
                [("eth", "eth"), ("eth", "tun"), ("tun", "eth"), ("tun", "tun")]
            ):
                devices = []
                for index, mode in enumerate(modes):
                    name = f"w{case}{index}"
                    data = dict(name=name, mode=mode)
                    if mode == "eth":
                        peer = name + "p"
                        run(
                            "ip",
                            "link",
                            "add",
                            name,
                            "type",
                            "veth",
                            "peer",
                            "name",
                            peer,
                        )
                        run("ip", "link", "set", peer, "up")
                        receiver = socket.socket(
                            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)
                        )
                        receiver.bind((peer, 0))
                        receiver.setblocking(False)
                        sender = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
                        sender.bind((name, 0))
                        data.update(peer=peer, receiver=receiver, sender=sender)
                    else:
                        tun = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
                        fcntl.ioctl(
                            tun,
                            0x400454CA,
                            struct.pack("16sH", name.encode(), 1 | 0x1000),
                        )
                        local = f"192.0.{2+case*2+index}.1"
                        remote = f"192.0.{2+case*2+index}.2"
                        run("ip", "address", "add", local, "peer", remote, "dev", name)
                        data.update(tun=tun, local=local, remote=remote)
                    run("ip", "link", "set", name, "up")
                    run("tc", "qdisc", "add", "dev", name, "clsact")
                    run(
                        "tc",
                        "filter",
                        "add",
                        "dev",
                        name,
                        "egress",
                        "prio",
                        "1234",
                        "matchall",
                        "action",
                        "pass",
                    )
                    devices.append(data)

                def probe(device, version, ecn):
                    if device["mode"] == "eth":
                        recv = device["receiver"]
                        send = device["sender"]
                        while select.select([recv], [], [], 0)[0]:
                            recv.recv(65535)
                        packet, offset, _ = util.packet(version, "udp", 1, ecn, 0)
                        send.send(packet)
                        while True:
                            if not select.select([recv], [], [], 2)[0]:
                                raise AssertionError("No frame received on veth peer")
                            data = recv.recv(65535)
                            if data[12:14] == struct.pack(
                                "!H", 0x0800 if version == 4 else 0x86DD
                            ):
                                break
                        ip = data[offset:]
                    else:
                        tun = device["tun"]
                        while select.select([tun], [], [], 0)[0]:
                            os.read(tun, 65535)
                        if version == 6:
                            # Exact per-device IPv6 route, no DAD/wait required.
                            local = f"2001:db8:{case+1}:{devices.index(device)+1}::1"
                            remote = f"2001:db8:{case+1}:{devices.index(device)+1}::2"
                            current = run(
                                "ip", "-6", "address", "show", "dev", device["name"]
                            ).stdout
                            if local not in current:
                                run(
                                    "ip",
                                    "-6",
                                    "address",
                                    "add",
                                    local + "/64",
                                    "dev",
                                    device["name"],
                                    "nodad",
                                )
                        else:
                            local, remote = device["local"], device["remote"]
                        family = socket.AF_INET if version == 4 else socket.AF_INET6
                        with socket.socket(family, socket.SOCK_DGRAM) as udp:
                            udp.bind((local, 23456))
                            udp.setsockopt(
                                (
                                    socket.IPPROTO_IP
                                    if version == 4
                                    else socket.IPPROTO_IPV6
                                ),
                                socket.IP_TOS if version == 4 else socket.IPV6_TCLASS,
                                ecn,
                            )
                            udp.sendto(b"x", (remote, 12345))
                            while True:
                                if not select.select([tun], [], [], 2)[0]:
                                    raise AssertionError("No packet received on TUN")
                                ip = os.read(tun, 65535)
                                if (
                                    ip[0] >> 4 == version
                                    and ip[(9 if version == 4 else 6)] == 17
                                ):
                                    break
                    traffic = (
                        ip[1]
                        if version == 4
                        else (struct.unpack_from("!I", ip)[0] >> 20) & 255
                    )
                    assert traffic & 3 == ecn, "ECN changed"
                    return traffic >> 2

                log_path = args.report.with_name(
                    args.report.stem + f"-{case}-commands.log"
                )
                with log_path.open("w") as log:
                    proc = subprocess.Popen(
                        [args.binary.resolve(), pins],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=log,
                        text=True,
                    )
                    try:
                        assert proc.stdout.readline().strip() == "ready"
                        phases = [
                            ("initial", 0),
                            ("same", 0),
                            ("switch", 1),
                            ("same-new", 1),
                            ("lost", None),
                            ("reconnect", 1),
                            ("return", 0),
                            ("lost-again", None),
                        ]
                        previous_identity = None
                        for phase, active in phases:
                            binding = (
                                "lost" if active is None else devices[active]["name"]
                            )
                            proc.stdin.write(binding + "\n")
                            proc.stdin.flush()
                            state = json.loads(proc.stdout.readline())
                            assert state == dict(
                                ifname="" if active is None else binding,
                                active=active is not None,
                            )
                            for index, device in enumerate(devices):
                                name = device["name"]
                                expected_active = index == active
                                qdiscs = json.loads(
                                    run("tc", "-j", "qdisc", "show", "dev", name).stdout
                                )
                                filters = {
                                    direction: json.loads(
                                        run(
                                            "tc",
                                            "-j",
                                            "filter",
                                            "show",
                                            "dev",
                                            name,
                                            direction,
                                        ).stdout
                                    )
                                    for direction in ["ingress", "egress"]
                                }
                                cake = [q for q in qdiscs if q["kind"] == "cake"]
                                ours = {
                                    direction: [f for f in data if f.get("pref") == 272]
                                    for direction, data in filters.items()
                                }
                                assert any(
                                    f.get("pref") == 1234 for f in filters["egress"]
                                ), "Foreign filter removed"
                                identity = dict(cake=cake, filters=ours)
                                if phase in ["same", "same-new"] and expected_active:
                                    assert (
                                        identity == previous_identity
                                    ), "Same binding restarted kernel objects"
                                if phase in ["initial", "switch"] and expected_active:
                                    previous_identity = identity
                                probes = []
                                for version in [4, 6]:
                                    for ecn in range(4):
                                        dscp = probe(device, version, ecn)
                                        probes.append(
                                            dict(
                                                version=version,
                                                ecn=ecn,
                                                dscp=dscp,
                                                expected_dscp=(
                                                    46 if expected_active else 0
                                                ),
                                            )
                                        )
                                mismatch = (
                                    bool(cake) != expected_active
                                    or any(
                                        bool(value) != expected_active
                                        for value in ours.values()
                                    )
                                    or any(
                                        p["dscp"] != p["expected_dscp"] for p in probes
                                    )
                                )
                                record = dict(
                                    case=case,
                                    modes=modes,
                                    phase=phase,
                                    binding=binding,
                                    device=name,
                                    expected_active=expected_active,
                                    state=state,
                                    qdiscs=qdiscs,
                                    filters=filters,
                                    probes=probes,
                                    mismatch=mismatch,
                                )
                                records.append(record)
                                if mismatch:
                                    failures.append(
                                        dict(case=case, phase=phase, device=name)
                                    )
                        proc.stdin.write("quit\n")
                        proc.stdin.flush()
                        assert proc.wait(timeout=10) == 0
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait()
                        proc.stdin.close()
                        proc.stdout.close()
                for device in devices:
                    if device["mode"] == "eth":
                        device["sender"].close()
                        device["receiver"].close()
                        run("ip", "link", "del", device["name"])
                    else:
                        os.close(device["tun"])
        finally:
            run("umount", "/sys/fs/bpf")
            run("umount", pins)
    report = dict(
        passed=not failures,
        object_sha256=hashlib.sha256(obj).hexdigest(),
        binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        kernel=os.uname().release,
        cases=4,
        phases_per_case=8,
        device_checks=len(records),
        packets=sum(len(r["probes"]) for r in records),
        mismatches=failures,
        results=records,
        scope="Full production interface.c + main.c command runner; fixture netifd resolver and pinned production program FDs; real BPF/TC/CAKE/veth/TUN; egress shaping only, no daemon/ubus/IFB/hardware claims",
    )
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ["results", "mismatches"]},
            indent=2,
        )
    )
    print("Mismatches:", len(failures))
    if args.expect_failure:
        assert len(failures) == 32, failures
        assert all(
            row["phase"]
            in ["switch", "same-new", "lost", "reconnect", "return", "lost-again"]
            for row in failures
        ), failures
    elif failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
