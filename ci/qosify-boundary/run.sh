#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail
workspace=$(realpath "$1")
evidence="$workspace/evidence"
export PATH="$workspace/bpftool-build:$PATH"
bpftool version | tee "$evidence/bpftool-version.txt"
uname -r > "$evidence/runner-kernel.txt"
run_isolated() {
  sudo env PATH="$PATH" QOSIFY_PARENT_NETNS="$(readlink /proc/self/ns/net)" QOSIFY_PARENT_MNTNS="$(readlink /proc/self/ns/mnt)" unshare --net --mount python3 "$@"
}
for test in matrix tun; do
  if python3 "$evidence/test-$test.py" --object "$evidence/fixed.o" --report "$evidence/guard-unexpected.json" > "$evidence/guard-$test.log" 2>&1; then echo 'Guard accepted caller namespaces'; exit 1; fi
  grep -F 'Refusing ' "$evidence/guard-$test.log"
  run_isolated "$evidence/test-$test.py" --object "$evidence/original.o" --report "$evidence/original-$test.json" --expect-failure | tee "$evidence/original-$test.log"
  run_isolated "$evidence/test-$test.py" --object "$evidence/fixed.o" --report "$evidence/fixed-$test.json" | tee "$evidence/fixed-$test.log"
done
python3 - "$evidence" <<'PY'
from pathlib import Path
import json,sys
p=Path(sys.argv[1])
for mode,cases,bad in [('matrix',976,104),('tun',96,16)]:
 original=json.loads((p/f'original-{mode}.json').read_text());fixed=json.loads((p/f'fixed-{mode}.json').read_text())
 assert original['cases']==fixed['cases']==cases
 assert not original['passed'] and len(original['mismatches'])==bad
 assert fixed['passed'] and not fixed['mismatches']
print('Exact original failures and all 1072 fixed kernel/TC cases verified')
PY
