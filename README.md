# nss-kbuild-lab

Reproduce NSS driver and ath11k integration builds from pinned public OpenWrt
sources. This lab compiles the complete target kernel, evaluates the actual
package recipes, builds their dependencies, and links both baseline and patched
Wi-Fi modules with strict modpost checks.

Small callback tests can miss real header, compiler-definition and symbol-table
problems. This lab supplies the complete build context without needing a
pre-existing firmware checkout, private device configuration, or NSS hardware.

## Validated profiles

| CLI profile | Public compile configuration | NSS firmware interface |
| --- | --- | --- |
| `ipq60xx` (default) | JDCloud RE-CS-02 / IPQ6010 | 12.5 |
| `ipq807x` | Dynalink DL-WRX36 / IPQ8072 | 12.5 |
| `ipq807x-mesh` | Dynalink DL-WRX36 / IPQ8072 | 11.4 with mesh |

Pins in [ipq60xx.json](pins/ipq60xx.json) and
[ipq807x.json](pins/ipq807x.json) identify every source archive, original Git
commit, package patch and each target's separately verified official SDK.
The lab rejects a resolved Kconfig that silently falls back to another device
or subtarget, and records the selected options in the kernel proof.

The [mesh profile](pins/ipq807x-mesh.json) follows the actual Kconfig requirement
for NSS 11.4. It pins driver `53e586323efa906f5769e16e118b7c02226d9de3`
and clients `c4049d17ddda323d4514f053bc2d72e2443f9d23`, with their 41 and 40
legacy recipe patches. Each dependency's actual make-exported source revision
and patch directory must match its manifest before it is compiled. The
following table describes the normal NSS 12.5 inputs.

| Input | Pin |
| --- | --- |
| qosmio/openwrt-ipq | `92a2d104145c8d265851c4b388a41bd8e9c21cd9` |
| qosmio/nss-packages | `0d970dbf0185e3f53709bd803e8a466598023c57` |
| OpenWrt SDK | 25.12.0, AArch64/musl, GCC 14.3.0 |
| Target kernel | 6.12.92, with the source tree's actual NSS patches |
| qca-nss-drv | `d5ee67bd84d1ee7ed81eb6758e62a6db5a0cf4c7` |
| Wireless backports | 6.18.26; baseline 256 patches, candidate 259 |

The lab builds four dependency modules, then eight Wi-Fi modules for each
variant: **20 complete AArch64 modules**. The NSS clients subset is
`wifi-meshmgr=m`; other optional clients are outside this profile.

The candidate combines four independently submitted fixes:
[per-peer drops](https://github.com/qosmio/openwrt-ipq/pull/129),
[host queue initialization](https://github.com/qosmio/openwrt-ipq/pull/130),
[unready-context cleanup](https://github.com/qosmio/openwrt-ipq/pull/131), and
[selected firmware layout](https://github.com/qosmio/openwrt-ipq/pull/132).
The combined patch is an integration specimen, not an assertion that these
open PRs have been accepted upstream.

## Run

Use an x86-64 Linux host with Python 3.12 or newer, at least 8 GiB RAM and
approximately 15 GiB of free disk. Builds use a new directory without spaces.
No root permissions are needed after installing the native build dependencies:

```sh
sudo apt-get install build-essential bison flex m4 gawk libncurses-dev \
  zlib1g-dev libssl-dev file unzip zstd rsync gettext python3 perl git curl patch kmod
make test
python3 -u scripts/lab.py --workspace "$PWD/build-ipq60xx" --profile ipq60xx --jobs 4
python3 scripts/collect_evidence.py --workspace "$PWD/build-ipq60xx" \
  --output "$PWD/evidence-ipq60xx"
```

For IPQ807x use `--profile ipq807x`, or `--profile ipq807x-mesh` for the
validated NSS 11.4 mesh build. Each profile needs a separate new workspace.

The build downloads roughly 411 MiB of public archives and verifies every
SHA256 before extraction. An optional `--archive-cache DIR` reads files named
by their SHA256; it never writes to that cache and rejects corrupted entries.
Read-only cache entries may be input symlinks; the extractor resolves them
before invoking zstd, without changing cache bytes, permissions or links.
Each build workspace is private to one invocation. Existing directories are
rejected rather than reset.

After a module-stage failure, `--modules-only` can reuse the same kernel:

```sh
python3 -u scripts/lab.py --workspace "$PWD/build-ipq60xx" --modules-only --jobs 4
```

Reuse requires matching manifest/profile hashes, unchanged prepared source
trees, unchanged kernel configuration/generated header/Image/symbols, and a
clean tracked OpenWrt source tree. Module attempts get new directories and logs;
previous failure reports remain available. Earlier source or kernel failures
require a fresh workspace. A cache can avoid repeating downloads.

## What is checked

- Reconstruct the original public Git commit and root tree from archive blobs;
  fail if content, executable modes, symlinks or original metadata differ.
  No previous Git object database or authenticated Git fetch is required.
- Extract with Python's data filter. The pinned SDK includes 20 absolute
  builder-host command links: omit only those exact path/target pairs, record
  them, and let stock OpenWrt prerequisite checks recreate local tools. All
  other absolute links remain rejected.
- Apply complete package/backports patch series with zero fuzz. Require the
  expected source hashes for the prepared NSS callback and mac80211 transmit
  path.
- Compile the complete NSS-enabled kernel; run actual `Build/Configure`,
  `Build/Compile` and `Build/InstallDev` through the real OpenWrt make framework.
  There are no framework/header stubs or kernel compatibility adapters.
- Require strict module symbol resolution, AArch64 ELF headers, matching kernel
  vermagic, and hashes of all 20 modules. Existing compiler warnings are retained.
- Replay the actual Kbuild compiler arguments against complete headers. The
  NSS 12.5 ABI probe reports driver/baseline/candidate peer-sync sizes
  **212 / 196 / 212 bytes**, and retry offsets **188 / 180 / 188**. This catches
  the baseline firmware macro mismatch without copied structure definitions.
  The NSS 11.4 header predates that retry structure; its complete-header probe
  reports the common peer row as **176 / 176 / 176 bytes**, preserving the
  matching legacy layout. These remain source-interface checks.
- Retain command arguments, return codes, elapsed times, log hashes and failure
  attempts. Evidence collection rehashes the successful modules and command logs.

CI runs three complete public-input builds independently on Ubuntu 24.04, plus
30 unit tests on each of Python 3.12 and 3.14. The
[verified three-profile run](https://github.com/ZyPulse-zy/nss-kbuild-lab/actions/runs/39104699710)
builds research revision `3b7858da1529561d91a4fde2ca4d91ecf717ddf6`; all 27
release source files are identical to `541dcf4b18c97bf50f8f3b25981af58162f3ee5f`.
Only independent qosify research CI files are additional in that run.
Each profile passes 14 actual configure/compile/header stages plus four
make-exported dependency identity checks, and rehashes all 20 real modules on
the runner. Downloaded evidence contains 619 checksum-verified files per normal
profile and 641 for mesh, including the additional legacy patch logs. Only
bounded JSON reports, logs and probe source are uploaded; firmware images and
kernel modules are not distributed as deployment artifacts.

## Evidence boundary

Successful compilation demonstrates source/header/symbol compatibility for the
pinned profile. It does not demonstrate module loading, NSS firmware behavior,
packet forwarding, recovery on hardware, or game latency. The lab does not
modify host network configuration or contact a production router. It compiles
third-party source, so run untrusted candidate revisions in an isolated CI runner.

Additional firmware versions, mesh modes and SoCs require their own validated
profile and actual builds. Mesh support here is demonstrated only for the
explicit NSS 11.4 IPQ807x profile; NSS 12.5 profiles keep mesh disabled.

## License and contributions

Lab code is GPL-2.0-only. Embedded patch content retains upstream licensing.
See [CONTRIBUTING.md](CONTRIBUTING.md) for evidence and profile requirements.

Developed with AI assistance during read-only Athena NSS investigations. Public
source pins, regression results and complete build evidence are used to check
the implementation; no production source or device state is modified by the lab.
