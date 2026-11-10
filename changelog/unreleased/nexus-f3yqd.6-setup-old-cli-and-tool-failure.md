### Fixed

- **`/conexus:setup` says more when it cannot go on** (nexus-f3yqd.6, F2, F4). For a CLI too old to have the setup tools, the skill now warns that `nx self install` can sit silent for up to ten minutes while it clears uv's package cache, and that it is not stuck. When the setup process itself fails before it answers, the tool now quotes the last line it printed to standard error (a crash's exception line) instead of only its exit code.
