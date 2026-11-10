### Fixed

- **`nx upgrade --skip-t3` no longer announces a restart it skips** (nexus-f3yqd.6). It printed "Restart running services on the new version" and timed a step that did nothing, and its precondition step promised downloads and restarts. The restart step is no longer listed under `--skip-t3`, and the precondition step says it only checks.
