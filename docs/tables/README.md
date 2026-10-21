# docs/tables/

Home for repo-only RDR-201 closed-vocabulary tables, consumed by scripts that
never ship. It holds one: `cc-version-answers.toml` (nexus-qsmc5), the Claude
Code version -> observed answer table that cc-validation scenarios 06 and 07
compare themselves with (`tests/cc-validation/lib/version_answers.py` reads it
with the stdlib only, so the container's python can). The tables under
`src/nexus/tables/` ship inside the package because their consumers are
installed CLIs.

`tests/test_tables_lint.py` globs `*.toml` here (in addition to
`src/nexus/tables/`) and lints every table it finds.

## `[[impossible]]` blocks (nexus-q9u2n)

The checker proves coverage and overlap over the full cross-product of a
group's guard domains, which assumes the dimensions are independent. When
two values cannot co-occur (a probe result is never examined on the branch
where the pin check blocks), an author used to have two outs: write a row
for the phantom cell, or keep the value out of the domain (short-circuit-by-omission). The third is to
name the dependence:

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
