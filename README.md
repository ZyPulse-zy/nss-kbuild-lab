# nss-kbuild-lab

Reproduce NSS driver and ath11k integration builds from pinned OpenWrt sources.
The lab compiles the target kernel, evaluates package recipes, builds
dependencies, and compares baseline and patched Wi-Fi modules using strict
symbol resolution and real-header ABI checks.

## Profiles

| Profile | Device / SoC | NSS firmware interface |
| --- | --- | --- |
| `ipq60xx` (default) | JDCloud RE-CS-02 / IPQ6010 | 12.5 |
| `ipq807x` | Dynalink DL-WRX36 / IPQ8072 | 12.5 |
| `ipq807x-mesh` | Dynalink DL-WRX36 / IPQ8072 | 11.4 with mesh |

Source revisions, archive checksums, package patches and SDK versions are
recorded in [ipq60xx.json](pins/ipq60xx.json),
[ipq807x.json](pins/ipq807x.json) and
[ipq807x-mesh.json](pins/ipq807x-mesh.json). Mesh is enabled only in the
NSS 11.4 profile.

Each profile builds four dependency modules and eight Wi-Fi modules for each
variant: 20 AArch64 modules in total. The candidate combines the fixes in
[PR #129](https://github.com/qosmio/openwrt-ipq/pull/129),
[PR #130](https://github.com/qosmio/openwrt-ipq/pull/130),
[PR #131](https://github.com/qosmio/openwrt-ipq/pull/131) and
[PR #132](https://github.com/qosmio/openwrt-ipq/pull/132).

## Requirements

An x86-64 Linux host with Python 3.12+, at least 8 GiB RAM and approximately
15 GiB free disk. Each profile requires a new workspace whose path contains
no whitespace. Initial downloads total roughly 411 MiB.

On Ubuntu, install the build dependencies:

```sh
sudo apt-get install build-essential bison flex m4 gawk libncurses-dev \
  zlib1g-dev libssl-dev file unzip zstd rsync gettext python3 perl git curl patch kmod
```

## Run

```sh
make test
python3 -u scripts/lab.py --workspace "$PWD/build-ipq60xx" --profile ipq60xx --jobs 4
python3 scripts/collect_evidence.py --workspace "$PWD/build-ipq60xx" \
  --output "$PWD/evidence-ipq60xx"
```

Select `--profile ipq807x` or `--profile ipq807x-mesh` for the other targets,
with a separate workspace for each. The evidence output must be a new directory
outside the build workspace.

Optional arguments:

- `--archive-cache DIR`: read SHA256-named archives from an existing cache.
  Downloads and cache entries are checksum-verified.
- `--modules-only`: retry a module-stage failure using the same workspace,
  profile and verified kernel. Changed inputs are rejected, and previous
  attempts remain available.

## Validation and reports

The build checks source snapshots, patch application, selected Kconfig options,
package dependency identities, module symbols, AArch64 ELF headers, kernel
vermagic and NSS structure layouts. It uses the actual OpenWrt make framework
and complete target headers.

Evidence includes command logs, return codes, timings, probe results and a
`SHA256SUMS` manifest. Collection rechecks module and log hashes. After
extracting an evidence archive, verify it with:

```sh
sha256sum -c SHA256SUMS
```

The [three-profile CI run](https://github.com/ZyPulse-zy/nss-kbuild-lab/actions/runs/39104699710)
contains complete builds and collected evidence. See
[tests/README.md](tests/README.md) for the test layout.

Compilation checks source, header and symbol compatibility for the pinned
profiles. Module loading, firmware behavior and packet forwarding require
hardware tests. Additional devices or firmware versions need their own profiles
and complete builds.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for profile and validation requirements.
Lab code is GPL-2.0-only; see [LICENSE](LICENSE). Embedded patches retain their
upstream licensing. Developed with AI assistance.
