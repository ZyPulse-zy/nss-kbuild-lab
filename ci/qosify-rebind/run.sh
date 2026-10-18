#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")" && pwd)
workspace=$(realpath "$1")
evidence="$workspace/evidence-rebind"
parent_net=$(readlink /proc/self/ns/net)
parent_mnt=$(readlink /proc/self/ns/mnt)
export ASAN_OPTIONS=detect_leaks=0:abort_on_error=1
export UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1
common=(--object "$workspace/evidence/original.o" --bpftool "$workspace/bpftool-build/bpftool" --packet-util "$root/../qosify-boundary/test-matrix.py")
# Test both Python and C guards without granting a private namespace.
if python3 "$root/test-rebind.py" --binary "$workspace/rebind/bin/fixed" "${common[@]}" --report "$evidence/forbidden.json" > "$evidence/guard-python.log" 2>&1; then
  echo 'Python namespace guard unexpectedly allowed execution' >&2
  exit 1
fi
grep -F 'Refusing interface test' "$evidence/guard-python.log"
if "$workspace/rebind/bin/fixed" /unused > "$evidence/guard-native.log" 2>&1; then
  echo 'C namespace guard unexpectedly allowed execution' >&2
  exit 1
fi
grep -F 'Refusing interface test' "$evidence/guard-native.log"
for label in original fixed original-san fixed-san; do
  expectation=()
  if [[ "$label" == original* ]]; then expectation=(--expect-failure); fi
  sudo -n env QOSIFY_PARENT_NETNS="$parent_net" QOSIFY_PARENT_MNTNS="$parent_mnt" ASAN_OPTIONS="$ASAN_OPTIONS" UBSAN_OPTIONS="$UBSAN_OPTIONS" \
    unshare --net --mount -- python3 "$root/test-rebind.py" --binary "$workspace/rebind/bin/$label" "${common[@]}" --report "$evidence/$label-kernel.json" "${expectation[@]}" > "$evidence/$label-kernel.log" 2>&1
done
python3 - "$evidence" <<'PY'
from pathlib import Path
import json,sys
root=Path(sys.argv[1])
for label in ['original','fixed','original-san','fixed-san']:
    report=json.loads((root/(label+'-kernel.json')).read_text())
    assert report['cases']==4 and report['device_checks']==64 and report['packets']==512
    if label.startswith('original'):
        assert not report['passed'] and len(report['mismatches'])==32
    else:
        assert report['passed'] and not report['mismatches']
print('Original failures exactly reproduced; fixed native and ASan/UBSan pass all 64 checks/512 packets each')
PY
