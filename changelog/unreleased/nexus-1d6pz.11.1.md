### Added

- **The `board-ci` template declares `sha` and `outcome` dims on the `--done` row.** The CI board adapter can stamp the full head sha and the fold's outcome (`success`, `failure`, `cancelled`; absent while any run's conclusion is unknown), and the body documents the per-run `runs` entries with their conclusions and the total `n`. Both dims are optional, and the adapter sends them only once its `fold_result_dims` flag is on (nexus-1d6pz.11.1).
