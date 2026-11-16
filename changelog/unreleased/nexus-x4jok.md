### Fixed

- **`nx ci wait` now waits on a release or feature branch.** The board has CI topics for develop and main only, so `--branch release/v8.3.0` failed at once with exit 5 (UnknownSubspaceError). For a branch the board does not watch, or a topic the engine refuses as unknown, it skips the board and reads the sha's check-runs from GitHub (all events, so the pull request's CI counts) once a minute until all have completed or `--timeout` passes, with the same exit codes. The result line says "GitHub only: the board was not used". The develop and main path is unchanged (nexus-x4jok).
