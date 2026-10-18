# qosify logical-interface rebinding regression

The candidate stops an active logical interface before replacing its resolved
device name. The baseline leaves CAKE and BPF on the old device, skips setup on
the new one, and later cleans the wrong device on a down notification.

`build.py` reuses the hash-verified public SDK, Linux 6.12.92, native bpftool and
complete baseline classifier from `ci/qosify-boundary/prepare.py`. It restores
both complete qosify Git roots and builds the unchanged production interface.c
and main.c translation units. The command runner forks the real /bin/sh; all TC,
BPF, CAKE, ioctl and rtnetlink operations are real. Fixtures resolve the next
netifd l3_device and return FDs for real pinned production BPF programs. An
otherwise unused bridger callback, retained by ASan global metadata, aborts if
called. No daemon, ubus service, DNS IFB or ingress shaping is simulated as
successful.

Four veth/TUN pair combinations each exercise initial bind, unchanged bind,
A to B, unchanged B, loss, reconnect B, B to A, and final loss. Each phase
checks both devices' qdiscs/ingress and egress filters, retains a foreign TC
filter at priority 1234, and probes IPv4/IPv6 UDP with all four ECN values.
These are 64 device checks and 512 actual egress packets per binary. Original
and fixed binaries use the same original BPF object, avoiding a dependency on
the independent short-packet fix. Both program directions are attached, while
packet probes cover egress only.

Native and ASan/UBSan repeat the complete matrix: exactly 32 original device
failures, zero fixed failures. LSan is disabled for these runs because an
initial unmodified-source run found a separate pre-existing 224-byte libnl
callback reference leak in qosify_iface_init (nl_socket_get_cb needs a matching
nl_cb_put). This does not claim production leak freedom. The initial failure
is retained locally; no leak is silently counted as a pass.

Both Python and C refuse to operate without root and separately unshared network
and mount namespaces. BPF pins are on private bpffs, also bound over /sys/fs/bpf
to confine libbpf auto-pinning. TUN devices are nonpersistent. All router sources,
configuration and runtime remain untouched. Firmware, performance, packet
latency, same-name ifindex replacement and IFB-name collisions are outside scope.

Run from the research checkout on Linux with native build tools, libnl-3-dev,
libelf-dev and zlib1g-dev installed:

```sh
python3 ci/qosify-boundary/prepare.py --workspace /tmp/qosify-rebind
python3 ci/qosify-rebind/build.py --workspace /tmp/qosify-rebind
bash ci/qosify-rebind/run.sh /tmp/qosify-rebind
```

Developed with AI assistance; complete public source roots and actual kernel
results make the two-line candidate independently reviewable.
