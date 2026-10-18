# Test layout

`make test` runs the Git snapshot, archive/cache/log, and evidence-export regression suites with Python 3.12 or newer. `make format-check` uses Black 26.3.1.

The GitHub Actions Kbuild job also compiles the complete pinned target kernel and twenty NSS/Wi-Fi modules, checks real-header layouts, and collects the successful modules' hashes. This is separate from the fast unit suite.

Evidence exports contain the named public reports plus command/probe logs. They exclude unrelated workspace JSON and the hidden workspace marker: its manifest/profile hashes already appear in `result.json`. Every file named in `SHA256SUMS` must be present in the uploaded archive. After extracting an artifact, run `sha256sum -c SHA256SUMS` from its root.

Failed attempts stay in their original workspace. Inspect their result and command logs before rerunning; a failure must never be reported as a passing test.
