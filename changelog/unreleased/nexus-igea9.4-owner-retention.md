### Changed

- **Rows on a `--owner` board topic live 35 days, not 3.** The calibration of the acknowledgement deadline (RDR-252 A6) reads the first 20 reds or 30 days of owner, clear and ack rows, and they expired after three days. The `board/ci/<topic>` template takes a new optional field `retention_by_suffix` (a topic suffix to seconds, the default and ceiling for matching subspaces; `GET /v1/tuples/registry` lists it) and sets `--owner` to 35 days; every other `board/ci` topic keeps three. Ships with the next engine release (RDR-252, nexus-igea9.4).
