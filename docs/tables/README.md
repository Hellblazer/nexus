# docs/tables/

Repo-only RDR-201 closed-vocabulary tables: consumed by scripts that never
ship (e.g. the release-choreography table, RDR-201 Phase 2), as opposed to
the tables under `src/nexus/tables/` that ship inside the package because
their consumers are installed CLIs.

`release-choreography.toml` has exactly one reader: `scripts/
release_choreography.py`, which both `scripts/check_engine_release_floor.py`
and `scripts/check_client_release_precondition.py` route their release
decisions through (one table, one cache). Its message text
lives in `scripts/release_messages.py`, keyed by row id; the parity harness
`tests/scripts/test_release_table_parity.py` pins table, catalog, and both
scripts to each other cell by cell.

`tests/test_tables_lint.py` globs `*.toml` here (in addition to
`src/nexus/tables/`) and lints every table it finds. This file exists so
the directory has something to hold before the first repo-only table
lands.
