### Fixed

- **An aura that matches `from = "engine"` no longer fires or clears on a row a client posted.** `from` is a dimension the writer types, so any session could post `from=engine` on `board/ci/nexus-develop--owner` and set or clear the `ci_develop_red` block. The aura runtime now also requires the row's stamp: the engine renders `sender_session` and the engine's own rows carry none, so a row with a session stamped on it, or from an engine that renders no stamp, is ignored (RDR-252 critique S3, nexus-igea9).
