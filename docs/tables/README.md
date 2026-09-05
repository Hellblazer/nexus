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

## `[[impossible]]` blocks (nexus-q9u2n)

The checker proves coverage and overlap over the full cross-product of a
group's guard domains, which assumes the dimensions are independent. When
two values cannot co-occur (a probe result is never examined on the branch
where the pin check blocks), an author used to have two outs: write a row
for the phantom cell, or keep the value out of the domain (the release
table's short-circuit-by-omission). The third is to name the dependence:

```toml
[[impossible]]
"check_floor_bare.pin_currency" = "passes"
"check_floor_bare.probe" = "n/a"
```

Exactly two enum guard dimensions, one value each; both are validated
against their declared domains at load, like any other literal. Every cell
matching a pair is subtracted from the product before coverage and overlap
are judged, in every group whose dimensions include both. A row whose every
accepted cell is impossible earns the non-blocking `dead-row` advisory: it
can never fire, so either the row or the block is wrong.
