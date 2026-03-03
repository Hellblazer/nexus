# Design: RDR-017 Phase 2 — CLI `--monitor` flag + tqdm bar

**Date:** 2026-03-03
**Bead:** nexus-p0me
**Status:** Approved (RDR-017 accepted 2026-03-03)

## Prior Art

- RDR-017 (accepted): `docs/rdr/rdr-017-indexing-progress-reporting.md`
- Phase 1 deps resolved: nexus-9c2x (on_start/on_file hooks), nexus-ph2g (tqdm dep), nexus-uj09 (return_metadata)

## Summary

Wire tqdm progress bars and `--monitor` flag into all four `nx index` subcommands
(`repo`, `rdr`, `pdf`, `md`). All hooks exist in the indexer layer.

## Approach

**Single approach — no alternatives needed** (design settled in RDR-017):

### `index_repo_cmd`
- Add `--monitor` flag
- Build `on_start(count)` → creates `tqdm(total=count, disable=None, ...)`
- Build `on_file(fpath, chunks, elapsed)` → updates bar, writes `[n/total]` line when `--monitor`
- Track `n` via counter (not `bar.n` — unreliable when `disable=True`)
- Pass closures to `index_repository(on_start=..., on_file=...)`
- Close bar after call

### `index_rdr_cmd`
- Add `--monitor` flag
- Create bar before `batch_index_markdowns` with `total=len(rdr_files)`
- Build `on_file` closure → updates bar, writes monitor line when `--monitor`
- `on_start` not needed (total known from `len(rdr_files)`)

### `index_pdf_cmd`
- Add `--monitor` flag (no tqdm bar — single file)
- When `--monitor`: call `index_pdf(..., return_metadata=True)` and print chunks/pages/title/author

### `index_md_cmd`
- Add `--monitor` flag (no tqdm bar — single file)
- When `--monitor`: call `index_markdown(..., return_metadata=True)` and print chunks/sections

## Non-TTY behaviour

- `disable=None` on tqdm auto-suppresses in CI/piped
- Non-TTY monitor lines use `click.echo()` (no `\r`)
- TTY monitor lines use `tqdm.write()` (scroll above bar)

## Files Changed

- `src/nexus/commands/index.py` — all four commands
- `tests/test_index_cmd.py` — 10 new tests
- `tests/test_index_rdr_cmd.py` — 1 new test

## Imports Needed

```python
import sys
from tqdm import tqdm
```
