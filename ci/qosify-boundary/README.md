# qosify packet-boundary validation

This research branch adds an isolated validation workflow to nss-kbuild-lab.
The published NSS profiles/main are unchanged. The upstream candidate is the
single signed-off commit 73d3bfe0c62186f80956b8982bbefcb4ce3ed4b4,
based on qosify 143d71d2479176ad5fdb0f9ce191affa2dc9640d.

The complete original and fixed classifiers are compiled with the pinned
OpenWrt 25.12.0 GCC14.3 SDK LLVM21, verified Linux6.12.92 headers and actual
OpenWrt bpf-headers bounds/stdarg files. Both full Git trees are reconstructed
and matched to their exact original commit objects. Archive extraction reuses
the lab's tested guard, omitting only its exact 20 SDK builder-host link pins.

The two Ethernet modes each run real BPF_PROG_TEST_RUN packets. Together they
cover 976 IPv4/IPv6, VLAN, ECN, UDP/TCP/ICMP, DSCP fallback and truncated-header
cases. The two L3 modes additionally classify 96 actual TUN/TC datagrams with
normal UDP send/receive and received TOS/TCLASS metadata. The four-byte rodata
flags are initialized just as qosify_fill_rodata does; the program code is
unchanged. Direct map configuration is used, without the daemon/ubus path.

The original must produce exactly 104 Ethernet and 16 TUN mismatches, all at
valid zero-payload boundaries. The fixed classifier must pass all 1072 cases;
truncated headers, ECN, checksums and unmatched ports are checked separately.
Both classifiers must first pass the actual kernel verifier.

Tests require root in private network and mount namespaces. They refuse the
caller namespaces before mounting or creating interfaces. A private bpffs also
contains the source object's automatic map pins; TUN devices are nonpersistent.
There are no production interfaces, router configurations, NSS firmware, hardware
or performance claims. No host verifier debug addresses are exported.

Run after installing the workflow's standard build prerequisites:

```sh
python3 ci/qosify-boundary/prepare.py --workspace /tmp/qosify-boundary
bash ci/qosify-boundary/run.sh /tmp/qosify-boundary
```

Preparation downloads pinned public inputs and builds native bpftool from that
same Linux tree. Tests perform all privileged actions inside their guarded
namespaces. Public evidence is in the workflow artifact; the patch and both
source commit objects remain in this directory for independent reproduction.
