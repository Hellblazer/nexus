### Added

- **`nx ci wait --latest-attempt` follows a re-run.** By default the wait returns the newest `--done` row for the sha already on the board, which after a re-run starts is still the row of the attempt being replaced. With `--latest-attempt` a row counts only when GitHub's push runs show no newer attempt of any run in it; otherwise the wait goes on for the newer attempt's row, reading GitHub at most once a minute, and at the deadline reports the latest attempts from GitHub or times out while one still runs. It cannot be combined with `--fresh`.
