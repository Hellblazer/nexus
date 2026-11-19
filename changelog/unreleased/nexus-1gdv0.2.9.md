### Changed

- **The gate-record census in `nx memory curate --t2` reads only the repository's own project and only the tag `kind:gate`.** It no longer reads `<repo>_rdr` (the RDR gate flow's rows are never given a TTL, so they would be flagged for ever) and no longer counts a row tagged only `gate`, which `nx memory gate-rollup` does not list (RDR-209 Phase 3 Step 3).
