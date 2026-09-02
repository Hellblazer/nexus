# docs/tables/

Repo-only RDR-201 closed-vocabulary tables: consumed by scripts that never
ship (e.g. the release-choreography table, RDR-201 Phase 2), as opposed to
the tables under `src/nexus/tables/` that ship inside the package because
their consumers are installed CLIs.

`tests/test_tables_lint.py` globs `*.toml` here (in addition to
`src/nexus/tables/`) and lints every table it finds. This file exists so
the directory has something to hold before the first repo-only table
lands.
