### Changed

- **`preflight` covers the daemon tests and reads its own results more robustly** (nexus-qo7ie.36 follow-ups; nexus repo only, `.nexus/procedures/preflight.lua`).
  - A change under `src/nexus/daemon/` now runs the whole `tests/daemon` directory on a lab host, because a daemon module's tests are spread over several files.
  - Every pytest command turns colour off, so `FORCE_COLOR` no longer hides the summary line. The summary is read even when the log holds a binary byte.
  - A passing run's log is removed. A failed `mktemp` says so.
  - A clean summary with a non-zero exit is now tested to read red.
  - The changed-tests step no longer fires for helper files whose names merely contain `test_`.
  - The vw594 fence gate runs for any Python change under `src/nexus/`, which is what it scans.
  - The integration row builds the gate jar on the lab host first.
  - A change under `src/nexus/mcp/` also runs the tuple shakeout census, which pins two tool descriptions there.
