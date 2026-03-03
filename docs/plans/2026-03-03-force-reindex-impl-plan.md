# Implementation Plan: `--force` Flag on All `nx index` Subcommands

**Epic**: nexus-dp08 (feat: --force flag on nx index repo/pdf/md/rdr)
**Date**: 2026-03-03
**Status**: Approved (audit PASSED with corrections applied)

## Executive Summary

Add a `--force` flag to all four `nx index` subcommands (`repo`, `pdf`, `md`, `rdr`) that
bypasses the content_hash + embedding_model staleness check. This allows re-indexing after
logic changes (e.g., RDR-016 AST line-range fix, context prefix changes) without deleting
the entire collection. The flag follows the existing `frecency_only` pattern: a boolean
keyword argument threaded through the call chain from CLI to leaf-level indexing functions.

## Problem Statement

`nx index` subcommands have a per-file staleness check: if `content_hash` AND
`embedding_model` both match what is stored, the file is silently skipped. There is no way
to override this without deleting the collection. When indexing *logic* changes but file
content does not, users are stuck with stale embeddings.

## Design Decisions

1. **`force: bool = False` parameter** threaded through the entire call chain (same pattern as `frecency_only`)
2. **Bypass is surgical**: only the staleness guard (`if content_hash == stored and model == stored: return`) is skipped. All other logic (classification, chunking, embedding, upsert) runs normally.
3. **In-place upsert**: `--force` does NOT delete and recreate the collection. It re-embeds via the existing upsert path.
4. **Mutual exclusion**: `--force` and `--frecency-only` are mutually exclusive on `nx index repo` (frecency-only skips embedding; force re-runs embedding).
5. **Each subcommand gets its own independent `--force` flag** (not a global flag).
6. **Stats output** distinguishes force mode via messaging (e.g., "Force-indexing" vs "Indexing").

## Dependency Graph

```
Phase 1 (nexus-jazw) ──┐
                        ├── Phase 3 (nexus-5aoy) ── Phase 4 (nexus-wk85)
Phase 2 (nexus-mj98) ──┘
```

**Critical path**: Phase 1 (or 2) -> Phase 3 -> Phase 4
**Parallelization**: Phase 1 and Phase 2 can run concurrently (different files, different tests).

## Phase 1: Force Bypass in indexer.py Per-File Helpers

**Bead**: nexus-jazw (P1, unblocked)
**Files**: `src/nexus/indexer.py`, `tests/test_doc_indexer_hash_sync.py`
**Parallelizable with**: Phase 2

### Scope

Add `force: bool = False` parameter to three functions in `indexer.py` that perform
staleness checks:

| Function | Staleness Check Lines | Change |
|----------|----------------------|--------|
| `_index_code_file()` | 470-473 | Wrap in `if not force:` |
| `_index_prose_file()` | 596-599 | Wrap in `if not force:` |
| `_index_pdf_file()` | 761-764 | Wrap in `if not force:` |

### TDD Tests (Red Phase)

Add to `tests/test_doc_indexer_hash_sync.py`:

1. **`test_force_bypasses_staleness_code_file`**: Call `_index_code_file(force=True)` with a
   mock col.get() returning matching hash+model. Assert: returns True (indexed), Voyage
   embed IS called. (Contrast with existing `test_unchanged_file_skips_embed`.)

2. **`test_force_bypasses_staleness_prose_file`**: Same pattern for `_index_prose_file`.
   Mock `_embed_with_fallback` to return dummy embeddings. Assert: returns True when
   force=True even with matching hash.

3. **`test_force_bypasses_staleness_pdf_file`**: Same pattern for `_index_pdf_file`. Mock
   `_pdf_chunks` and `_embed_with_fallback`. Assert: returns True when force=True.

### Implementation (Green Phase)

For each of the three functions, change the staleness check from:

```python
if existing["metadatas"]:
    stored = existing["metadatas"][0]
    if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
        return False
```

To:

```python
if not force and existing["metadatas"]:
    stored = existing["metadatas"][0]
    if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
        return False
```

Add `force: bool = False` to each function signature.

### Acceptance Criteria

- [ ] Three new tests written and initially failing (TDD red)
- [ ] `force: bool = False` added to all three function signatures
- [ ] All three new tests pass (TDD green)
- [ ] All existing tests in test_doc_indexer_hash_sync.py still pass (no regression)
- [ ] Code compiles including all tests

### Context for Executing Agent

- **Search**: `nx search "staleness check content_hash" --corpus code --n 5`
- **Pattern**: Look at `test_unchanged_file_skips_embed` in test_doc_indexer_hash_sync.py for mock setup
- **Key insight**: The `force` parameter wraps the `if existing["metadatas"]:` block, NOT individual conditions
- Use `mcp__sequential-thinking__sequentialthinking` if the mock setup is unclear

---

## Phase 2: Force Bypass in doc_indexer.py Pipeline

**Bead**: nexus-mj98 (P1, unblocked)
**Files**: `src/nexus/doc_indexer.py`, `tests/test_doc_indexer.py`
**Parallelizable with**: Phase 1

### Scope

The `_index_document()` shared pipeline has a single staleness check (lines 190-194).
Thread `force` through the public API functions that call it.

| Function | Change |
|----------|--------|
| `_index_document()` | Add `force: bool = False`, wrap staleness check |
| `index_pdf()` | Add `force: bool = False`, pass to `_index_document` |
| `index_markdown()` | Add `force: bool = False`, pass to `_index_document` |

### TDD Tests (Red Phase)

Add to `tests/test_doc_indexer.py`:

1. **`test_force_bypasses_staleness_pdf`**: Set up a PDF that is already indexed (mock col.get
   returning matching hash+model). Call `index_pdf(force=True)`. Assert: returns > 0 (chunks
   indexed, not skipped).

2. **`test_force_bypasses_staleness_markdown`**: Same pattern for `index_markdown(force=True)`.
   Assert: returns > 0.

3. **`test_force_default_false_still_skips`**: Verify that without force=True, the existing
   staleness skip behavior is preserved. (This may already be covered by existing tests;
   include as a regression guard.)

### Implementation (Green Phase)

In `_index_document()`, change lines 190-194 from:

```python
if existing["metadatas"]:
    stored_hash = existing["metadatas"][0].get("content_hash", "")
    stored_model = existing["metadatas"][0].get("embedding_model", "")
    if stored_hash == content_hash and stored_model == target_model:
        return 0
```

To:

```python
if not force and existing["metadatas"]:
    stored_hash = existing["metadatas"][0].get("content_hash", "")
    stored_model = existing["metadatas"][0].get("embedding_model", "")
    if stored_hash == content_hash and stored_model == target_model:
        return 0
```

Add `force: bool = False` as a **keyword-only** parameter (after `*`) to signatures of
`_index_document`, `index_pdf`, `index_markdown` (these functions already have `*`-separated
keyword-only sections). Pass `force=force` in each delegation call.

### Acceptance Criteria

- [ ] New tests written and initially failing (TDD red)
- [ ] `force: bool = False` added to _index_document, index_pdf, index_markdown signatures
- [ ] force= threaded through all delegation calls
- [ ] All new tests pass (TDD green)
- [ ] All existing tests in test_doc_indexer.py still pass
- [ ] Code compiles including all tests

### Context for Executing Agent

- **Search**: `nx search "index_document staleness" --corpus code --n 5`
- **Pattern**: Look at existing test_doc_indexer.py for mock patterns (monkeypatch credentials, mock make_t3, mock Voyage client)
- **Key file**: doc_indexer.py line 150-226 (`_index_document`)
- Use `mcp__sequential-thinking__sequentialthinking` for mock wiring if needed

---

## Phase 3: Force Plumbing Through Orchestration Layer

**Bead**: nexus-5aoy (P1, blocked by nexus-jazw + nexus-mj98)
**Files**: `src/nexus/indexer.py`, `src/nexus/doc_indexer.py`, `tests/test_indexer.py`

### Scope

Thread `force` through the orchestration functions that coordinate per-file indexing.

| Function | File | Change |
|----------|------|--------|
| `index_repository()` | indexer.py:327 | Add `force: bool = False`, pass to `_run_index` |
| `_run_index()` | indexer.py:937 | Add `force: bool = False`, pass to all `_index_*` helpers and `_discover_and_index_rdrs` |
| `_discover_and_index_rdrs()` | indexer.py:822 | Add `force: bool = False`, pass to `batch_index_markdowns` |
| `batch_index_markdowns()` | doc_indexer.py:385 | Add `force: bool = False`, pass to `index_markdown` |
| `batch_index_pdfs()` | doc_indexer.py:364 | Add `force: bool = False`, pass to `index_pdf` |

### TDD Tests (Red Phase)

Add to `tests/test_indexer.py`:

1. **`test_index_repository_passes_force_to_run_index`**: Patch `_run_index`, call
   `index_repository(force=True)`. Assert `_run_index` was called with `force=True`.

2. **`test_index_repository_default_force_false`**: Call `index_repository()` (no force
   arg). Assert `_run_index` called with `force=False`.

3. **`test_run_index_passes_force_to_code_file_helper`**: Setup repo with one `.py`
   file, patch `_index_code_file`, call `_run_index(force=True)`. Assert mock called
   with `force=True`. (Guards against wiring _run_index but not calling helpers with it.)

4. **`test_run_index_passes_force_to_prose_file_helper`**: Same pattern for
   `_index_prose_file` (use a `.md` file in the repo fixture).

5. **`test_run_index_passes_force_to_pdf_file_helper`**: Same pattern for
   `_index_pdf_file` (use a `.pdf` file in the repo fixture).

6. **`test_run_index_passes_force_to_discover_rdrs`**: Patch `_discover_and_index_rdrs`,
   call `_run_index(force=True)`. Assert mock called with `force=True`.

Add to `tests/test_doc_indexer.py`:

7. **`test_batch_index_markdowns_passes_force`**: Patch `index_markdown`, call
   `batch_index_markdowns(force=True)`. Assert each `index_markdown` call received
   `force=True`.

8. **`test_batch_index_pdfs_passes_force`**: Patch `index_pdf`, call
   `batch_index_pdfs(force=True)`. Assert each `index_pdf` call received `force=True`.

### Implementation (Green Phase)

Add `force: bool = False` to each function signature listed above. In each function body,
pass `force=force` to the delegated call.

Key call sites in `_run_index()`:

```python
# Line ~1077-1081: _index_code_file call
_index_code_file(
    file, repo, code_collection, code_model, code_col, db,
    voyage_client, git_meta, now_iso, score,
    chunk_lines=chunk_lines,
    force=force,  # NEW
)

# Line ~1087-1090: _index_prose_file call
_index_prose_file(
    file, repo, docs_collection, docs_model, docs_col, db,
    voyage_key, git_meta, now_iso, score,
    force=force,  # NEW
)

# Line ~1096-1099: _index_pdf_file call
_index_pdf_file(
    file, repo, docs_collection, docs_model, docs_col, db,
    voyage_key, git_meta, now_iso, score,
    force=force,  # NEW
)

# Line ~1102-1104: _discover_and_index_rdrs call
_discover_and_index_rdrs(
    repo, rdr_abs_paths, db, voyage_key, now_iso,
    force=force,  # NEW
)
```

### Acceptance Criteria

- [ ] New tests written and initially failing (TDD red)
- [ ] `force: bool = False` added to all five function signatures
- [ ] force= threaded through all delegation calls in _run_index, _discover_and_index_rdrs, batch_index_markdowns, batch_index_pdfs
- [ ] All new tests pass (TDD green)
- [ ] All existing tests still pass
- [ ] Code compiles including all tests

### Context for Executing Agent

- **Search**: `nx search "index_repository _run_index" --corpus code --n 5`
- **Pattern**: Follow the `frecency_only` threading pattern exactly (same parameter position convention)
- **Key files**: indexer.py lines 327-371 (index_repository), 937-1124 (_run_index), 822-864 (_discover_and_index_rdrs)
- **Reminder**: SPAWN parallel agents if needed to conserve context
- Use `mcp__sequential-thinking__sequentialthinking` for verifying the plumbing is complete

---

## Phase 4: CLI Flags, Mutual Exclusion, and Stats Output

**Bead**: nexus-wk85 (P1, blocked by nexus-5aoy)
**Files**: `src/nexus/commands/index.py`, `tests/test_index_cmd.py`, `tests/test_index_rdr_cmd.py`

### Scope

Add `--force` Click flag to all four subcommands. Implement mutual exclusion with
`--frecency-only` on `repo`. Update stats output messaging.

### TDD Tests (Red Phase)

Add to `tests/test_index_cmd.py`:

1. **`test_index_repo_force_flag_passed_through`**: Invoke `nx index repo <path> --force`.
   Assert `index_repository` called with `force=True`.

2. **`test_index_repo_force_frecency_mutual_exclusion`**: Invoke `nx index repo <path>
   --force --frecency-only`. Assert exit code != 0 and error message contains "mutually
   exclusive".

3. **`test_index_repo_force_output_message`**: Invoke with `--force`. Assert output
   contains "Force-indexing" (not just "Indexing").

4. **`test_index_pdf_force_flag`**: Invoke `nx index pdf <path> --force`. Assert
   `index_pdf` called with `force=True`.

5. **`test_index_pdf_force_dry_run_mutual_exclusion`**: Invoke `nx index pdf <path>
   --force --dry-run`. Assert exit code != 0 and error message contains "mutually
   exclusive".

6. **`test_index_md_force_flag`**: Invoke `nx index md <path> --force`. Assert
   `index_markdown` called with `force=True`.

Add to `tests/test_index_rdr_cmd.py`:

6. **`test_index_rdr_force_flag`**: Invoke `nx index rdr <path> --force`. Assert
   `batch_index_markdowns` called with `force=True`.

### Implementation (Green Phase)

#### `index_repo_cmd`

```python
@index.command("repo")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--frecency-only", is_flag=True, default=False, ...)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-indexing all files, bypassing staleness check (re-chunks and re-embeds in-place).",
)
def index_repo_cmd(path: Path, frecency_only: bool, force: bool) -> None:
    if force and frecency_only:
        raise click.UsageError("--force and --frecency-only are mutually exclusive.")
    ...
    label = "Force-indexing" if force else ("Updating frecency scores" if frecency_only else "Indexing")
    click.echo(f"{label} {path}...")
    stats = index_repository(path, reg, frecency_only=frecency_only, force=force)
```

#### `index_pdf_cmd`

```python
@index.command("pdf")
...
@click.option("--force", is_flag=True, default=False, help="Force re-indexing, bypassing staleness check.")
def index_pdf_cmd(path: Path, corpus: str, collection: str | None, dry_run: bool, force: bool) -> None:
    if force and dry_run:
        raise click.UsageError("--force and --dry-run are mutually exclusive.")
    ...
    n = index_pdf(path, corpus=corpus, collection_name=collection, force=force)
    label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{label} {n} chunk(s).")
```

#### `index_md_cmd`

```python
@index.command("md")
...
@click.option("--force", is_flag=True, default=False, help="Force re-indexing, bypassing staleness check.")
def index_md_cmd(path: Path, corpus: str, force: bool) -> None:
    ...
    n = index_markdown(path, corpus=corpus, force=force)
    label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{label} {n} chunk(s).")
```

#### `index_rdr_cmd`

```python
@index.command("rdr")
...
@click.option("--force", is_flag=True, default=False, help="Force re-indexing all RDR documents, bypassing staleness check.")
def index_rdr_cmd(path: Path, force: bool) -> None:
    ...
    results = batch_index_markdowns(rdr_files, corpus=basename, collection_name=collection, force=force)
    label = "Force re-indexed" if force else "Indexed"
    click.echo(f"{label} {indexed} of {len(rdr_files)} RDR document(s).")
```

### Acceptance Criteria

- [ ] All seven new tests written and initially failing (TDD red)
- [ ] --force flag added to all four Click commands
- [ ] Mutual exclusion check in index_repo_cmd raises UsageError
- [ ] force= threaded to the correct library function in each command
- [ ] Output messages distinguish force mode ("Force-indexing", "Force re-indexed")
- [ ] All new tests pass (TDD green)
- [ ] All existing CLI tests still pass
- [ ] Code compiles including all tests

### Context for Executing Agent

- **Search**: `nx search "click option frecency_only" --corpus code --n 5`
- **Pattern**: Follow `--frecency-only` flag pattern exactly for `--force` (same decorator style)
- **Key file**: commands/index.py (all four Click commands)
- **Mutual exclusion**: Use `click.UsageError`, not `click.BadParameter`
- **Stats note**: For `repo`, force count is implicit (all files re-indexed); for pdf/md/rdr, chunk count suffices
- Use `mcp__sequential-thinking__sequentialthinking` for verifying all call sites are wired

---

## Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Existing tests break | Low | Medium | `force` defaults to `False`; no behavior change without flag |
| Missed staleness check location | Low | High | Audit: exactly 4 locations identified and verified by line number |
| Voyage AI cost with --force | Medium | Low | User opt-in; CLI help text explains behavior |
| Race condition with concurrent indexing | Low | Low | Upsert is idempotent; same as current behavior |

## Parallelization Opportunities

- **Phase 1 and Phase 2** run concurrently (different source files, different test files)
- Within Phase 4, the four CLI commands are independent changes but share one test file (test_index_cmd.py) so sequential is safer

## Function Change Summary

| # | Function | File | Line | Change |
|---|----------|------|------|--------|
| 1 | `_index_code_file` | indexer.py | 431 | Add `force: bool = False` param, wrap staleness |
| 2 | `_index_prose_file` | indexer.py | 558 | Add `force: bool = False` param, wrap staleness |
| 3 | `_index_pdf_file` | indexer.py | 729 | Add `force: bool = False` param, wrap staleness |
| 4 | `_discover_and_index_rdrs` | indexer.py | 822 | Add `force: bool = False`, pass through |
| 5 | `_run_index` | indexer.py | 937 | Add `force: bool = False`, pass to all helpers |
| 6 | `index_repository` | indexer.py | 327 | Add `force: bool = False`, pass to `_run_index` |
| 7 | `_index_document` | doc_indexer.py | 150 | Add `force: bool = False`, wrap staleness |
| 8 | `index_pdf` | doc_indexer.py | 323 | Add `force: bool = False`, pass through |
| 9 | `index_markdown` | doc_indexer.py | 346 | Add `force: bool = False`, pass through |
| 10 | `batch_index_markdowns` | doc_indexer.py | 385 | Add `force: bool = False`, pass through |
| 11 | `batch_index_pdfs` | doc_indexer.py | 364 | Add `force: bool = False`, pass through |
| 12 | `index_repo_cmd` | commands/index.py | 23 | Add --force flag + mutual exclusion |
| 13 | `index_pdf_cmd` | commands/index.py | 63 | Add --force flag |
| 14 | `index_md_cmd` | commands/index.py | 143 | Add --force flag |
| 15 | `index_rdr_cmd` | commands/index.py | 159 | Add --force flag |

## Test Summary

| Test File | New Tests | Phase |
|-----------|-----------|-------|
| tests/test_doc_indexer_hash_sync.py | 3 (force bypass for code, prose, pdf) | Phase 1 |
| tests/test_doc_indexer.py | 2-3 (force bypass for index_pdf, index_markdown) | Phase 2 |
| tests/test_indexer.py | 6 (index_repository→_run_index + _run_index→4 helpers) | Phase 3 |
| tests/test_doc_indexer.py | 2 (batch_index_markdowns + batch_index_pdfs pass-through) | Phase 3 |
| tests/test_index_cmd.py | 6 (CLI flags, mutual exclusions ×2, output) | Phase 4 |
| tests/test_index_rdr_cmd.py | 1 (rdr --force flag) | Phase 4 |

**Total new tests**: ~22

## Beads

| Bead | Phase | Status | Blocks | Blocked By |
|------|-------|--------|--------|------------|
| nexus-dp08 | Epic | open | — | — |
| nexus-jazw | Phase 1 | open | nexus-5aoy | — |
| nexus-mj98 | Phase 2 | open | nexus-5aoy | — |
| nexus-5aoy | Phase 3 | open | nexus-wk85 | nexus-jazw, nexus-mj98 |
| nexus-wk85 | Phase 4 | open | — | nexus-5aoy |

## Related Beads

- **nexus-4iti** (P2): RDR-016 force-reindex of affected collections. This becomes trivial once --force is implemented (just run `nx index repo --force`).
