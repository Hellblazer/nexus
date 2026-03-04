---
rdr: RDR-017
title: "Indexing Progress Reporting: tqdm-Based Progress Bar for nx index"
closed_date: 2026-03-04
close_reason: implemented
---

# Post-Mortem: RDR-017 — Indexing Progress Reporting

## RDR Summary

`nx index repo` ran silently for 45–90 minutes with no user feedback. Added tqdm progress bars to all four `nx index` subcommands via a two-hook callback interface (`on_start`/`on_file`), plus a `--monitor` flag for per-file detail lines.

## Implementation Status

Phase 1 (callback interface + helper refactor) and Phase 2 (CLI integration) fully implemented. Phase 3 (chunk-level PDF/MD callbacks) was explicitly dropped in the RDR as not worth the architectural cost.

## Implementation vs. Plan

| Item | Planned | Delivered | Drift |
|------|---------|-----------|-------|
| Helper `-> bool` → `-> int` refactor | `_index_code_file`, `_index_prose_file`, `_index_pdf_file` | ✓ Implemented; returned chunk count | None |
| `on_start` / `on_file` callbacks | Added to `index_repository`, `_run_index`, `batch_index_markdowns` | ✓ Threaded through all call paths | None |
| tqdm bar for `repo`/`rdr` | `total=`, `disable=None`, rate + ETA + filename postfix | ✓ Implemented | None |
| `--monitor` flag | Per-file detail lines via `tqdm.write()` or `click.echo()` | ✓ TTY and non-TTY branches | None |
| `return_metadata` for `pdf`/`md` | `--monitor` prints page/title/author metadata | ✓ `index_pdf(return_metadata=True)` and `index_markdown` | None |
| Non-TTY counter for `--monitor` | Separate `n` counter (not `bar.n`) | ✓ Implemented per RF-2 finding | None |
| `tqdm>=4.65` in `pyproject.toml` | Add explicit dep | ✓ Added | None |

## Drift Classification

None. All planned work delivered. Phase 3 drop was pre-approved in the RDR.

## RDR Quality Assessment

- Research findings (RF-1 through RF-7) were accurate and complete — implementation matched all empirical discoveries
- RF-6 (helper return type refactor) was the right call to surface up front rather than discover mid-implementation
- The non-TTY counter note (RF-2 / Phase 2 implementation note) prevented a subtle bug: `bar.n` is unreliable when `disable=True`

## Key Takeaways

- The two-hook interface (`on_start`, `on_file`) is a clean separation: the indexer knows nothing about tqdm, the CLI knows nothing about indexer internals
- `disable=None` is the right default for tqdm in CLI tools: silent in CI/tests, visible in TTY
- Documenting the empirical research findings in the RDR before implementation saved real debugging time
