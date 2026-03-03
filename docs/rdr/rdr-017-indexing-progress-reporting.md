---
title: "Indexing Progress Reporting: tqdm-Based Progress Bar for nx index"
type: enhancement
status: accepted
priority: P2
author: Hal Hildebrand
date: 2026-03-03
accepted_date: 2026-03-03
reviewed_by: "self"
related_issues:
  - nexus-4iti
---

# RDR-017: Indexing Progress Reporting: tqdm-Based Progress Bar for nx index

## Problem

The `nx index repo` command silently processes thousands of files for 45–90 minutes with
no user-visible feedback. The only output is:

```
Indexing /path/to/repo…
Done.
```

The existing `-v` / debug flag exposes structlog debug output including full Voyage AI
embed JSON payloads and ChromaDB startup spam — far too noisy for routine monitoring.
This makes it impossible to tell whether the process is progressing normally, stalled,
or hitting errors.

**Concrete failure mode:** `nx index repo ~/git/ART --force` (4014 files) runs for
~60 minutes with zero feedback. Users cannot estimate completion time, verify correctness,
or detect a stall.

This affects all four `nx index` subcommands:
- `repo`: iterates thousands of files — worst offender
- `rdr`: iterates multiple markdown files
- `pdf`: processes a single PDF in N chunks
- `md`: processes a single markdown in N chunks

## Decision

Add a tqdm-based progress bar to all four `nx index` subcommands.

### 1. Default behavior

A tqdm progress bar replaces silence:
- `repo` / `rdr`: per-file bar — `[current/total, %, rate, ETA, current_filename]`
- `pdf` / `md`: no bar — the existing `Indexed N chunk(s).` line is sufficient for
  single-file operations that typically complete in seconds

Non-TTY (CI, piped output): tqdm with `disable=None` auto-suppresses; no output,
no control codes. This is correct — CI pipelines do not benefit from progress bars.

### 2. `--monitor` flag (added to all four subcommands)

**In TTY:** same tqdm bar plus per-file detail lines via `tqdm.write()` scrolling above:
```
  [142/4014] HybridPredictorImpl.java — 37 chunks  (1.2s)
  [143/4014] ART2FullCategorySelector.java — skipped  (0.0s)
```
Skipped files (content hash unchanged) appear with `— skipped` to prevent false stall
appearance on incremental runs.

**In non-TTY:** no tqdm bar; emit plain `click.echo()` lines per file (no `\r` control
codes). This makes `--monitor` useful for `nx -v index repo ... --monitor 2>&1 | tee run.log`.

For `pdf` / `md`: `--monitor` prints chunking metadata after completion (page range,
title, author extracted during indexing) — same information as `--dry-run` but with
actual cloud writes.

### 3. Architecture — two-hook interface

```python
on_start: Callable[[int], None] | None = None
# Fires once before iteration with total file count.
# CLI uses this to create the tqdm bar with the correct `total=`.

on_file: Callable[[Path, int, float], None] | None = None
# Fires after each file: (file_path, chunks_produced, elapsed_s).
# chunks=0 means skipped or failed. CLI updates bar and optionally writes monitor line.
```

The indexer records `time.monotonic()` before/after each file helper call to compute
`elapsed_s`. No tqdm import in `indexer.py` or `doc_indexer.py`.

For `index_repo_cmd`: `on_start` fires from inside `_run_index` with
`total = len(code_files) + len(prose_files) + len(pdf_files)`. RDR files are indexed
after the main loop completes and are reported separately by the existing
`click.echo("  RDR documents: N indexed")` output — they are not counted in the
main progress bar.

For `index_rdr_cmd`: CLI passes `on_file` to `batch_index_markdowns` with the tqdm
bar created before the call using `total=len(rdr_files)` (known in the command layer).
`batch_index_markdowns` calls `on_file` internally after each markdown file completes.

### 4. Chunk-level progress for pdf/md — not implemented

Phase 3 (chunk-level callback for `index_pdf` / `index_markdown`) is **dropped**.
`_index_document()` does a single bulk embed call followed by a single bulk upsert —
there is no per-chunk loop to attach a callback to without restructuring the entire
embedding pipeline. The marginal UX value for single-file commands that complete in
5–15 seconds does not justify this architectural cost. Revisit if PDF indexing of
very large documents becomes a bottleneck.

### 5. `-v` (debug) flag — unchanged

Retains raw embed payloads and full structlog debug output for deep debugging.

## Alternatives Considered

### A — `click.progressbar()` (no new dependency)

Built into click. Shows `[####   ] 142/4014  3%  00:42:18`.

**Rejected:** Cannot easily show current filename in the bar. More critically, ETA
assumes uniform item duration — code files vary 10× in processing time. The
callback-based API is also less ergonomic than tqdm's postfix mechanism.

### B — tqdm (chosen)

Standard Python progress library (~50KB). Shows:
```
ART: 142/4014 [02:13<41:07, 1.1 file/s, now=HybridPredictorImpl.java]
```

Auto-adapts to terminal width. Rate smoothed via exponential moving average.
Non-TTY fallback built-in. `tqdm.write()` allows clean `--monitor` lines
without breaking the bar. Handles resize, interrupt, and nested bars.

**Selected.**

### C — Hand-rolled `\r` line (no new dependency)

Write `\r[142/4014] 3.5% ████░░ 1.1 f/s ~41m left HybridPredictorImpl.java`
using carriage-return overwriting.

**Rejected:** Must hand-implement rate calculation, ETA, terminal-width truncation,
non-TTY detection, Windows compatibility. More code to test, more edge cases.
tqdm exists precisely so we don't do this.

## Research Findings

### RF-1: tqdm is already present as a transitive dependency (Confirmed — empirical)

`uv pip show tqdm` confirms tqdm 4.67.3 is installed in the nexus venv, required by
`chromadb`, `huggingface-hub`, `llama-index-core`, and `nltk`. No new entry in
`pyproject.toml` is needed. We should add an explicit `tqdm>=4.65` constraint to
document the direct usage, but no `uv add` is required.

**Evidence:** `uv pip show tqdm` in the nexus venv, 2026-03-03.

### RF-2: tqdm produces no output in non-TTY with `disable=None` (Confirmed — empirical)

When `tqdm(total=N, disable=None)` is used and `sys.stdout.isatty()` returns `False`
(as in CI, piped output, or CliRunner tests), tqdm generates zero bytes of output.
CliRunner's invoke stream is confirmed non-TTY (`sys.stdout.isatty() → False`).

**Implication:** Default mode bars are silent in tests and CI — correct behaviour.
For `--monitor` mode, use `disable=False` to force output even in non-TTY, so that
log-to-file monitoring pipelines still see per-file lines.

**Evidence:** Empirical probe with `io.StringIO()` stream, 2026-03-03.

### RF-3: `tqdm.write()` correctly interleaves with the live bar (Confirmed — empirical)

`tqdm.write(msg, file=bar_file)` clears the current bar line, writes the message with
a newline, then redraws the bar. The output buffer shows the correct sequence:
`\r<clear>`, `<message>\n`, `\r<bar>`. This produces clean per-file monitor lines
scrolling above a persistent bar — exactly the curl-like aesthetic desired.

**Evidence:** Empirical probe, 2026-03-03.

### RF-4: No existing callback interface in indexer.py (Confirmed — code review)

`indexer.py` has no `progress_callback`, `progress_fn`, or similar parameter in any
of `index_repository()`, `_run_index()`, `_discover_and_index_rdrs()`, or the per-file
helpers. The `force`, `frecency_only`, and `chunk_lines` keyword params established the
threading pattern we will follow. `doc_indexer.py` likewise has no callback interface.

**Evidence:** `grep -n "callback\|Callable" src/nexus/indexer.py src/nexus/doc_indexer.py` → no matches.

### RF-5: File counts are known before iteration begins in `_run_index()` (Confirmed — code review)

`_run_index()` builds `code_files`, `prose_files`, and `pdf_files` lists (lines 988–1038)
before the iteration loops begin. Total = `len(code_files) + len(prose_files) + len(pdf_files)`.
This means the tqdm `total=` can be set accurately before any file is processed, enabling
correct ETA from the first update.

**Evidence:** Reading `indexer.py:988–1038`.

### RF-6: Per-file helpers return `bool`, not `int` — must be refactored (Confirmed — code review)

`_index_code_file()`, `_index_prose_file()`, and `_index_pdf_file()` all return `bool`
(`True` = indexed, `False` = skipped/failed). They do NOT return chunk count. The return
values are currently discarded at all three call sites in `_run_index()`. To surface
chunk counts to the `on_file` callback, all three helpers must be refactored from
`-> bool` to `-> int` (returning chunks produced, 0 for skipped/failed). The `0`
return is falsy, preserving any future callers that check truthiness.

**Evidence:** `indexer.py:445` (`-> bool`), `indexer.py:572` (`-> bool`),
`indexer.py:744` (`-> bool`). Call sites at lines 1083, 1094, 1105 discard return value.

### RF-7: RDR file count is not available before `_discover_and_index_rdrs` runs (Confirmed — code review)

`rdr_abs_paths` in `_run_index()` is a set of RDR *directories*, not files. The actual
RDR markdown files are discovered inside `_discover_and_index_rdrs()`. Pre-counting
them would require duplicating the glob logic. Therefore the main tqdm bar covers only
`code_files + prose_files + pdf_files`, and RDR indexing is reported separately via
the existing `click.echo("  RDR documents: ...")` output.

**Evidence:** Reading `indexer.py:982–985` (builds `rdr_abs_paths` as a dir set) and
`indexer.py:848–869` (`_discover_and_index_rdrs` discovers files internally).

## Implementation Plan

### Phase 1 — Helper refactor + callback interface (TDD)

**Files:** `pyproject.toml`, `src/nexus/indexer.py`, `src/nexus/doc_indexer.py`

**1a — Helper return type refactor** (`indexer.py`):
- Change `_index_code_file()`, `_index_prose_file()`, `_index_pdf_file()` from `-> bool`
  to `-> int` (chunks produced; 0 for skipped/failed, which is falsy)
- Update all internal `return True` → `return <chunk_count>`, `return False` → `return 0`

**1b — Add `tqdm>=4.65` to `pyproject.toml`** (explicit direct dep; already transitive)

**1c — Add two-hook interface** to orchestration functions:
```python
on_start: Callable[[int], None] | None = None
on_file: Callable[[Path, int, float], None] | None = None
```
Add to: `index_repository()`, `_run_index()`, `_discover_and_index_rdrs()`,
`batch_index_markdowns()`, `batch_index_pdfs()`

`batch_index_markdowns` gains `on_file` and calls it inside its per-path loop after
each `index_markdown` call: `on_file(path, chunks, elapsed_s)`. Both call paths use it:
`_discover_and_index_rdrs` (via `index_repo_cmd`) and `index_rdr_cmd` (direct call).

**1d — Wire hooks in `_run_index()`**:
- After building `code_files`, `prose_files`, `pdf_files`: call `on_start(len(code_files) + len(prose_files) + len(pdf_files))`
- Per file: record `t0 = time.monotonic()`, call helper, record `elapsed = time.monotonic() - t0`,
  call `on_file(path, chunks, elapsed)`
- Thread `on_file` through `_discover_and_index_rdrs` → `batch_index_markdowns`

**Tests:** `tests/test_indexer.py`, `tests/test_doc_indexer.py`
- `on_start` called once with correct total (code+prose+pdf, not RDR count)
- `on_file` called once per file with correct `(path, int, float)` signature
- `chunks=0` emitted for skipped files (staleness check passes)
- `on_start=None` / `on_file=None` safe defaults — no change to existing callers
- Helper return type: `_index_code_file` etc. return `int ≥ 0`

### Phase 2 — CLI integration (TDD)

**Files:** `src/nexus/commands/index.py`

**`index_repo_cmd`:**
- Add `--monitor` flag
- In `on_start` closure: create tqdm bar with `total=N, disable=None, desc=repo.name`
- In `on_file` closure:
  - Call `bar.update(1)` and `bar.set_postfix(now=path.name)`
  - If `--monitor` AND TTY: `tqdm.write(f"  [{bar.n}/{bar.total}] {path.name} — {chunks} chunks  ({elapsed:.1f}s)")` or `— skipped  (0.0s)` when `chunks == 0`
  - If `--monitor` AND not TTY: `click.echo(f"  [{n}/{total}] {path.name} — ...")` (plain text, no bar)
  - **Implementation note**: In the non-TTY branch, `n` must be tracked as a separate counter
    variable (e.g., `n = 0; n += 1` at each `on_file` invocation) rather than using `bar.n`,
    which is unreliable when `disable=True`. `total` can be captured from the `on_start` closure.
- Pass `on_start` and `on_file` to `index_repository()`

**`index_rdr_cmd`:**
- Add `--monitor` flag
- Create tqdm bar with `total=len(rdr_files)` before calling `batch_index_markdowns`
- Pass `on_file` closure that updates bar; writes monitor line when `--monitor`
- `on_start` not needed (total known in command layer from `rdr_files`)

**`index_pdf_cmd` and `index_md_cmd`:**
- Add `--monitor` flag
- No tqdm bar — single file, short duration
- With `--monitor`: after indexing completes, print chunk metadata:
  page range, source title/author extracted during indexing (requires `index_pdf` /
  `index_markdown` to return metadata or store it for retrieval)
- **API change**: `index_pdf` gains `return_metadata: bool = False` optional parameter.
  When `False` (default), returns `int` as today — no break for `batch_index_pdfs` or existing callers.
  When `True`, returns `dict` with `{"chunks": N, "pages": [...], "title": str, "author": str}`.
  `index_pdf_cmd` passes `return_metadata=True` only when `--monitor` is set.
  `index_markdown` follows the same pattern with `{"chunks": N, "sections": N}`.
  No change to `batch_index_pdfs` or `batch_index_markdowns` — they do not pass `return_metadata`.

**Tests:** `tests/test_index_cmd.py`, `tests/test_index_rdr_cmd.py`
- `--monitor` flag accepted by all four commands
- `on_start` closure creates tqdm bar (mock tqdm or inspect callback args)
- `on_file` closure calls `bar.update` with correct args
- Non-TTY `--monitor` emits `click.echo` lines without `\r` characters
- `index_rdr_cmd`: bar created with `total=len(rdr_files)`; callback passed to `batch_index_markdowns` mock
