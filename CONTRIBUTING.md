# Contributing

Keep new profiles and fixes narrow enough to reproduce from public inputs.
Do not add a profile by changing a target name without completing its actual
kernel, dependency and wireless builds.

1. Pin each source/archive hash and original Git commit metadata. Record all
   applied package patches and the public compile configuration.
2. Keep the original baseline and candidate separate. Explain the baseline
   failure or ABI difference and which part the candidate changes.
3. Run `make test`, `make format-check` with Black 26.3.1, and the complete lab.
   Include a CI link or the collected reports and checksum manifest.
4. Preserve failed attempts. Distinguish complete Kbuild results, reduced
   fixtures, firmware observations and hardware tests.
5. Never include addresses, credentials, private device configuration or
   production artifacts in profiles or evidence.

Sign commits with your name and email using `Signed-off-by:`. Describe any AI
assistance in the contribution and explain the verification you performed.
