### Fixed

- **A generation build that fails for an unrecognised reason says what to do** (nexus-f3yqd.6, F22). `nx self install` (and the plugin setup's build) used to print `generation build failed:` and uv's raw output when none of its specific hints matched. It now puts a line above that output: uv's first `error:` line names the cause, and adding `--dry-run` to the same command prints the build command to run by hand.
