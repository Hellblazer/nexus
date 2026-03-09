# RDR-029: Pipeline Versioning Implementation Plan

**RDR**: docs/rdr/rdr-029-pipeline-versioning.md (status: accepted)
**Epic bead**: nexus-8wnp (in_progress)
**Estimated LOC**: ~175 (implementation) + ~120 (tests)
**Branch**: `feature/nexus-8wnp-pipeline-versioning`

## Executive Summary

Add pipeline version awareness to the nexus indexing system. Collections are
stamped with `PIPELINE_VERSION` after force-indexing, staleness warnings fire
when stored version differs from current, `--force-stale` auto-forces only
stale collections, and `nx doctor` reports version mismatches.

The `--force` flag is ALREADY IMPLEMENTED on all 4 `nx index` subcommands.
This plan covers the remaining scope: version constant, stamp/check logic,
`--force-stale` flag, and doctor integration.

## Dependency Graph

```
nexus-m4ek  Phase 1: PIPELINE_VERSION + helpers (root)
    |
    +--- nexus-0yr0  Phase 2: Wire stamp into _run_index + CLI  (depends: Phase 1)
    |        |
    |        +--- nexus-suhl  Phase 4: --force-stale flag  (depends: Phase 1 + 2)
    |
    +--- nexus-nrqr  Phase 3: Staleness warning  (depends: Phase 1)
    |
    +--- nexus-nias  Phase 5: nx doctor check  (depends: Phase 1)
```

**Critical path**: Phase 1 -> Phase 2 -> Phase 4

**Parallelizable after Phase 1**: Phases 2, 3, 5 are independent of each other.

## Key Implementation Decisions

1. **Metadata merge, not replace**: `col.modify(metadata=...)` REPLACES all
   metadata. Every stamp call must merge: `{**(col.metadata or {}), "pipeline_version": PIPELINE_VERSION}`.

2. **Stamp only on force**: Non-force runs must NOT advance the version stamp.
   A partial incremental run would mark stale chunks as current.

3. **New collection guard**: `pipeline_version=None` on new collections.
   Skip warning to avoid false alarms on first index.

4. **Stamp location**: Inside `_run_index()` for `nx index repo` (has collection
   refs). At CLI level for standalone `pdf`/`md`/`rdr` commands.

5. **force_stale granularity**: Coarse-grained (any stale collection -> force all).
   Pipeline changes affect all file types equally.

---

## Phase 1: PIPELINE_VERSION Constant + Helper Functions

**Bead**: nexus-m4ek
**Files**: `src/nexus/indexer.py`, `tests/test_pipeline_version.py`
**LOC**: ~25 impl + ~40 tests

### Context

- Search keywords: `PIPELINE_VERSION`, `stamp_collection_version`, `col.modify`
- The constant goes in `indexer.py` near the top (after imports, before `DEFAULT_IGNORE`)
- Helper functions are pure logic on ChromaDB collection objects (mockable)

### Step 1: Write failing tests

Create `tests/test_pipeline_version.py` with these tests:

```python
# Test 1: PIPELINE_VERSION constant exists and is "4"
def test_pipeline_version_constant():
    from nexus.indexer import PIPELINE_VERSION
    assert PIPELINE_VERSION == "4"
    assert isinstance(PIPELINE_VERSION, str)

# Test 2: stamp_collection_version writes pipeline_version with merge semantics
def test_stamp_merges_metadata():
    from unittest.mock import MagicMock
    from nexus.indexer import stamp_collection_version, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = {"existing_key": "existing_value"}

    stamp_collection_version(col)

    col.modify.assert_called_once_with(
        metadata={"existing_key": "existing_value", "pipeline_version": PIPELINE_VERSION}
    )

# Test 3: stamp_collection_version handles None metadata
def test_stamp_handles_none_metadata():
    from unittest.mock import MagicMock
    from nexus.indexer import stamp_collection_version, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = None

    stamp_collection_version(col)

    col.modify.assert_called_once_with(
        metadata={"pipeline_version": PIPELINE_VERSION}
    )

# Test 4: get_collection_pipeline_version returns stored version
def test_get_pipeline_version_returns_value():
    from unittest.mock import MagicMock
    from nexus.indexer import get_collection_pipeline_version

    col = MagicMock()
    col.metadata = {"pipeline_version": "3"}
    assert get_collection_pipeline_version(col) == "3"

# Test 5: get_collection_pipeline_version returns None for new collections
def test_get_pipeline_version_returns_none_for_new():
    from unittest.mock import MagicMock
    from nexus.indexer import get_collection_pipeline_version

    col = MagicMock()
    col.metadata = None
    assert get_collection_pipeline_version(col) is None

    col.metadata = {}
    assert get_collection_pipeline_version(col) is None
```

Run: `uv run pytest tests/test_pipeline_version.py -v` (expect ImportError / failure)

### Step 2: Implement

In `src/nexus/indexer.py`, after the `_VOYAGE_EMBED_BATCH_SIZE` line (~line 30), add:

```python
# Pipeline version: bump when indexing changes invalidate existing embeddings.
# History:
#   v1-v3: pre-versioning (no version stamp in collection metadata)
#   v4:    RDR-028 language registry + RDR-014 CCE prefixes
PIPELINE_VERSION: str = "4"


def stamp_collection_version(col: object) -> None:
    """Write PIPELINE_VERSION to collection metadata, preserving existing keys.

    col.modify(metadata=...) REPLACES all metadata, so we must merge.
    """
    existing = getattr(col, "metadata", None) or {}
    col.modify(metadata={**existing, "pipeline_version": PIPELINE_VERSION})


def get_collection_pipeline_version(col: object) -> str | None:
    """Return the pipeline_version from collection metadata, or None."""
    meta = getattr(col, "metadata", None) or {}
    return meta.get("pipeline_version")
```

Run: `uv run pytest tests/test_pipeline_version.py -v` (expect green)

### Validation

- [ ] All 5 tests pass
- [ ] `uv run pytest` full suite still green (no regressions)
- [ ] Committable as standalone change

---

## Phase 2: Wire Version Stamp into _run_index and CLI Commands

**Bead**: nexus-0yr0 (depends: nexus-m4ek)
**Files**: `src/nexus/indexer.py`, `src/nexus/commands/index.py`, `tests/test_pipeline_version.py`
**LOC**: ~30 impl + ~25 tests

### Context

- `_run_index()` has `code_col` and `docs_col` as local variables (lines 1175-1176)
- RDR collection name: `_rdr_collection_name(repo)` (already imported in _run_index)
- Standalone CLI commands know their target collection names
- Stamp only when `force=True` AND indexing completed successfully

### Step 1: Write failing tests

Add to `tests/test_pipeline_version.py`:

```python
# Test 6: _run_index stamps collections on force=True
def test_run_index_stamps_on_force(tmp_path):
    """After force indexing, code/docs/rdr collections get pipeline_version stamped."""
    # This test patches stamp_collection_version and verifies it's called
    # when force=True in _run_index. Heavy mocking required.
    from unittest.mock import MagicMock, patch
    from nexus.indexer import PIPELINE_VERSION

    mock_col = MagicMock()
    mock_col.metadata = None
    mock_col.count.return_value = 0
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    with patch("nexus.indexer.stamp_collection_version") as mock_stamp:
        # Verify stamp was called (details depend on wiring)
        # Exact test shape depends on implementation
        pass  # Placeholder — flesh out after Phase 1

# Test 7: _run_index does NOT stamp without force
def test_run_index_no_stamp_without_force(tmp_path):
    """Non-force indexing must not stamp collections."""
    pass  # Placeholder

# Test 8: Standalone CLI stamps on force (Click runner test)
def test_cli_pdf_stamps_on_force():
    """nx index pdf --force stamps the target collection after indexing."""
    pass  # Placeholder
```

Note: These tests require significant mocking of T3, Voyage, and registry.
The implementer should use the patterns established in
`tests/test_doc_indexer_hash_sync.py` for mock construction.

### Step 2: Implement in _run_index

In `src/nexus/indexer.py`, inside `_run_index()`, add stamping after the
pruning step (before the `return` statement at ~line 1244):

```python
    # Stamp pipeline version on force indexing
    if force:
        stamp_collection_version(code_col)
        stamp_collection_version(docs_col)
        # Stamp RDR collection if it exists
        rdr_col_name = _rdr_collection_name(repo)
        try:
            rdr_col = db.get_or_create_collection(rdr_col_name)
            stamp_collection_version(rdr_col)
        except Exception:
            _log.debug("rdr_stamp_skipped", collection=rdr_col_name)
```

Note: `_rdr_collection_name` is already imported inside `_discover_and_index_rdrs`
(line 945). Move the import to the top of `_run_index` or use the existing
import at line 1142.

### Step 3: Implement in standalone CLI commands

In `src/nexus/commands/index.py`:

**index_pdf_cmd** (after line 230, before "Done"):
```python
    if force and n:
        from nexus.indexer import stamp_collection_version
        from nexus.db import make_t3
        col_name = collection if collection else f"docs__{corpus}"
        t3 = make_t3()
        stamp_collection_version(t3.get_or_create_collection(col_name))
```

**index_md_cmd** (after line 259, before "Done" equivalent):
```python
    if force and n:
        from nexus.indexer import stamp_collection_version
        from nexus.db import make_t3
        col_name = f"docs__{corpus}"
        t3 = make_t3()
        stamp_collection_version(t3.get_or_create_collection(col_name))
```

**index_rdr_cmd** (after line 323, before final echo):
```python
    if force and indexed:
        from nexus.indexer import stamp_collection_version
        from nexus.db import make_t3
        t3 = make_t3()
        stamp_collection_version(t3.get_or_create_collection(collection))
```

### Validation

- [ ] Force indexing stamps all affected collections
- [ ] Non-force indexing does NOT stamp
- [ ] Standalone commands (pdf, md, rdr) stamp on force
- [ ] `uv run pytest` full suite green
- [ ] Committable as standalone change

---

## Phase 3: Staleness Detection Warning in _run_index

**Bead**: nexus-nrqr (depends: nexus-m4ek)
**Files**: `src/nexus/indexer.py`, `tests/test_pipeline_version.py`
**LOC**: ~15 impl + ~25 tests

### Context

- Warning emitted at start of `_run_index()`, after collections are created (line ~1177)
- Uses `get_collection_pipeline_version()` from Phase 1
- Gate on `is not None` to skip new collections
- Warning is informational only (does not block indexing)

### Step 1: Write failing tests

Add to `tests/test_pipeline_version.py`:

```python
# Test 9: Staleness warning emitted for version mismatch
def test_staleness_warning_on_mismatch(caplog):
    """When stored pipeline_version != current, structlog warning is emitted."""
    from unittest.mock import MagicMock
    from nexus.indexer import check_pipeline_staleness, PIPELINE_VERSION
    import structlog

    col = MagicMock()
    col.metadata = {"pipeline_version": "2"}  # old version
    col_name = "code__test"

    result = check_pipeline_staleness(col, col_name)

    assert result is True  # is stale

# Test 10: No warning for new collections (None)
def test_no_warning_for_new_collection():
    """New collections (pipeline_version=None) should not trigger staleness."""
    from unittest.mock import MagicMock
    from nexus.indexer import check_pipeline_staleness

    col = MagicMock()
    col.metadata = None

    result = check_pipeline_staleness(col, "code__test")

    assert result is False

# Test 11: No warning for matching version
def test_no_warning_for_matching_version():
    """Current version should not trigger staleness."""
    from unittest.mock import MagicMock
    from nexus.indexer import check_pipeline_staleness, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = {"pipeline_version": PIPELINE_VERSION}

    result = check_pipeline_staleness(col, "code__test")

    assert result is False
```

Run: `uv run pytest tests/test_pipeline_version.py::test_staleness_warning_on_mismatch -v`
(expect ImportError for `check_pipeline_staleness`)

### Step 2: Implement check_pipeline_staleness helper

In `src/nexus/indexer.py`, after `get_collection_pipeline_version`:

```python
def check_pipeline_staleness(col: object, collection_name: str) -> bool:
    """Check if collection has a stale pipeline version.

    Returns True if the stored version differs from PIPELINE_VERSION.
    Returns False for new collections (stored version is None) or matching versions.
    Emits a structlog warning when stale.
    """
    stored = get_collection_pipeline_version(col)
    if stored is None:
        return False  # new collection, not stale
    if stored != PIPELINE_VERSION:
        _log.warning(
            "collection_pipeline_stale",
            collection=collection_name,
            stored_version=stored,
            current_version=PIPELINE_VERSION,
            hint=f"Collection {collection_name} indexed with pipeline v{stored}, "
                 f"current is v{PIPELINE_VERSION}. Run with --force to re-index.",
        )
        return True
    return False
```

### Step 3: Wire into _run_index

In `_run_index()`, after collections are created (after line 1177):

```python
    # Check pipeline version staleness
    check_pipeline_staleness(code_col, code_collection)
    check_pipeline_staleness(docs_col, docs_collection)
```

### Validation

- [ ] Warning emitted for version mismatch
- [ ] No warning for None (new collection)
- [ ] No warning for matching version
- [ ] Warning is informational only (indexing proceeds)
- [ ] `uv run pytest` full suite green
- [ ] Committable as standalone change

---

## Phase 4: --force-stale Flag on nx index repo

**Bead**: nexus-suhl (depends: nexus-m4ek, nexus-0yr0)
**Files**: `src/nexus/indexer.py`, `src/nexus/commands/index.py`, `tests/test_pipeline_version.py`
**LOC**: ~30 impl + ~20 tests

### Context

- `--force-stale` only on `nx index repo` (not pdf/md/rdr standalone)
- Mutual exclusion with `--force` and `--frecency-only`
- Coarse-grained: any stale collection -> force=True for entire run
- Implementation: check collection versions in `_run_index`, set `force=True` if stale

### Step 1: Write failing tests

Add to `tests/test_pipeline_version.py`:

```python
# Test 12: force_stale sets force=True when collections are stale
def test_force_stale_enables_force_when_stale():
    """When force_stale=True and a collection has old version, force should activate."""
    from unittest.mock import MagicMock
    from nexus.indexer import check_pipeline_staleness, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = {"pipeline_version": "2"}
    assert check_pipeline_staleness(col, "code__test") is True  # confirms staleness

# Test 13: force_stale does not force when current
def test_force_stale_noop_when_current():
    """When force_stale=True but all collections are current, force stays False."""
    from unittest.mock import MagicMock
    from nexus.indexer import check_pipeline_staleness, PIPELINE_VERSION

    col = MagicMock()
    col.metadata = {"pipeline_version": PIPELINE_VERSION}
    assert check_pipeline_staleness(col, "code__test") is False

# Test 14: CLI mutual exclusion --force-stale and --force
def test_force_stale_force_mutual_exclusion():
    """--force-stale and --force cannot be used together."""
    from click.testing import CliRunner
    from nexus.commands.index import index

    runner = CliRunner()
    result = runner.invoke(index, ["repo", "/tmp/fake", "--force", "--force-stale"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower() or result.exit_code != 0

# Test 15: CLI mutual exclusion --force-stale and --frecency-only
def test_force_stale_frecency_mutual_exclusion():
    """--force-stale and --frecency-only cannot be used together."""
    from click.testing import CliRunner
    from nexus.commands.index import index

    runner = CliRunner()
    result = runner.invoke(index, ["repo", "/tmp/fake", "--frecency-only", "--force-stale"])
    assert result.exit_code != 0
```

### Step 2: Add --force-stale to CLI

In `src/nexus/commands/index.py`, in `index_repo_cmd`:

Add the option decorator:
```python
@click.option(
    "--force-stale",
    is_flag=True,
    default=False,
    help="Re-index only if collection pipeline version is outdated (smart force).",
)
```

Add mutual exclusion checks in the function body:
```python
    if force_stale and force:
        raise click.UsageError("--force-stale and --force are mutually exclusive.")
    if force_stale and frecency_only:
        raise click.UsageError("--force-stale and --frecency-only are mutually exclusive.")
```

Pass to index_repository:
```python
    stats = index_repository(path, reg, frecency_only=frecency_only, force=force,
                             force_stale=force_stale,
                             on_locked=on_locked, on_start=on_start, on_file=on_file)
```

### Step 3: Thread force_stale through index_repository -> _run_index

In `src/nexus/indexer.py`:

**index_repository** — add `force_stale: bool = False` parameter, pass to `_run_index`:
```python
def index_repository(
    repo: Path,
    registry: "RepoRegistry",
    *,
    frecency_only: bool = False,
    chunk_lines: int | None = None,
    force: bool = False,
    force_stale: bool = False,
    ...
```

**_run_index** — add `force_stale: bool = False` parameter. After creating
collections (after line 1177), check staleness and escalate to force:

```python
    # --force-stale: check collection versions, escalate to force if any stale
    if force_stale:
        any_stale = (
            check_pipeline_staleness(code_col, code_collection)
            or check_pipeline_staleness(docs_col, docs_collection)
        )
        if any_stale:
            _log.info("force_stale_escalating", reason="stale collection detected")
            force = True  # escalate: rest of function uses force=True
        else:
            _log.info("force_stale_skipped", reason="all collections current")
```

### Validation

- [ ] `--force-stale` and `--force` are mutually exclusive
- [ ] `--force-stale` and `--frecency-only` are mutually exclusive
- [ ] Stale collection -> force=True for entire run
- [ ] Current collections -> no force
- [ ] Stamp written after force_stale escalation (inherited from Phase 2 logic)
- [ ] `uv run pytest` full suite green
- [ ] Committable as standalone change

---

## Phase 5: nx doctor Pipeline Version Check

**Bead**: nexus-nias (depends: nexus-m4ek)
**Files**: `src/nexus/commands/doctor.py`, `tests/test_pipeline_version.py`
**LOC**: ~50 impl + ~15 tests

### Context

- Add version check AFTER the database reachability checks (guarded by credentials)
- Use `T3Database.list_collections()` to get all collection names
- Use `T3Database.collection_info()` to get metadata with pipeline_version
- Compare against `PIPELINE_VERSION` from indexer
- Report: current, stale (with versions), no version stamp

### Step 1: Write failing tests

Add to `tests/test_pipeline_version.py`:

```python
# Test 16: Doctor reports stale collections
def test_doctor_reports_stale_collections():
    """nx doctor should flag collections with outdated pipeline_version."""
    from unittest.mock import MagicMock, patch
    from click.testing import CliRunner
    from nexus.commands.doctor import doctor_cmd

    # This test requires mocking the T3Database and credentials
    # The implementer should mock make_t3() to return a fake T3Database
    # whose list_collections returns collections with old pipeline_version
    pass  # Placeholder — shape depends on doctor wiring

# Test 17: Doctor handles collections with no version stamp
def test_doctor_handles_no_version_stamp():
    """Collections without pipeline_version should be reported as 'no version stamp'."""
    pass  # Placeholder
```

### Step 2: Implement in doctor_cmd

In `src/nexus/commands/doctor.py`, add after the database reachability checks
(after the `if not db_ok: _fix(...)` block, around line 121):

```python
    # -- Pipeline version check --
    if chroma_key and chroma_database and voyage_key:
        from nexus.db import make_t3
        from nexus.indexer import PIPELINE_VERSION

        try:
            t3 = make_t3()
            collections = t3.list_collections()
            if collections:
                lines.append("")  # blank line separator
                stale_count = 0
                for col_info in collections:
                    name = col_info["name"]
                    try:
                        info = t3.collection_info(name)
                        stored = info.get("metadata", {}).get("pipeline_version")
                        if stored is None:
                            lines.append(_check_line(
                                f"pipeline ({name})", True,
                                "no version stamp (index with --force to stamp)",
                            ))
                        elif stored != PIPELINE_VERSION:
                            stale_count += 1
                            lines.append(_check_line(
                                f"pipeline ({name})", False,
                                f"v{stored} (current: v{PIPELINE_VERSION})",
                            ))
                        else:
                            lines.append(_check_line(
                                f"pipeline ({name})", True, f"v{stored}",
                            ))
                    except Exception as exc:
                        _log.debug("doctor_version_check_failed",
                                   collection=name, error=str(exc))
                if stale_count:
                    _fix(lines,
                         "nx index repo <path> --force-stale  (re-index outdated collections)",
                         "nx index repo <path> --force        (re-index all collections)")
        except Exception as exc:
            _log.debug("doctor_pipeline_check_failed", error=str(exc))
```

### Validation

- [ ] Stale collections flagged with version mismatch
- [ ] Current collections shown as OK
- [ ] Collections without version stamp noted (not flagged as error)
- [ ] Fix suggestions shown when stale collections found
- [ ] Doctor check skipped gracefully when credentials unavailable
- [ ] `uv run pytest` full suite green
- [ ] Committable as standalone change

---

## Test Summary

All tests in `tests/test_pipeline_version.py`:

| # | Test | Phase | Type |
|---|------|-------|------|
| 1 | `test_pipeline_version_constant` | 1 | Unit |
| 2 | `test_stamp_merges_metadata` | 1 | Unit |
| 3 | `test_stamp_handles_none_metadata` | 1 | Unit |
| 4 | `test_get_pipeline_version_returns_value` | 1 | Unit |
| 5 | `test_get_pipeline_version_returns_none_for_new` | 1 | Unit |
| 6 | `test_run_index_stamps_on_force` | 2 | Mock |
| 7 | `test_run_index_no_stamp_without_force` | 2 | Mock |
| 8 | `test_cli_pdf_stamps_on_force` | 2 | CLI |
| 9 | `test_staleness_warning_on_mismatch` | 3 | Unit |
| 10 | `test_no_warning_for_new_collection` | 3 | Unit |
| 11 | `test_no_warning_for_matching_version` | 3 | Unit |
| 12 | `test_force_stale_enables_force_when_stale` | 4 | Unit |
| 13 | `test_force_stale_noop_when_current` | 4 | Unit |
| 14 | `test_force_stale_force_mutual_exclusion` | 4 | CLI |
| 15 | `test_force_stale_frecency_mutual_exclusion` | 4 | CLI |
| 16 | `test_doctor_reports_stale_collections` | 5 | Mock |
| 17 | `test_doctor_handles_no_version_stamp` | 5 | Mock |

Run all: `uv run pytest tests/test_pipeline_version.py -v`

## Risk Factors

1. **ChromaDB col.modify merge**: The biggest correctness risk. Every call to
   `col.modify(metadata=...)` must merge existing metadata. The
   `stamp_collection_version` helper centralizes this, but callers must use it
   (never call `col.modify` directly for version stamping).

2. **Standalone CLI command T3 connection**: The stamp step in standalone
   commands (`pdf`, `md`, `rdr`) creates a second `make_t3()` connection. This
   is acceptable because these commands already have a T3 connection via
   `doc_indexer`. Optimization: pass the existing T3 instance through if needed.

3. **Doctor performance**: `list_collections()` + `collection_info()` per
   collection makes N+1 API calls. Acceptable for a diagnostic command.

## References

- RDR: `docs/rdr/rdr-029-pipeline-versioning.md`
- Force reindex plan: `docs/plans/2026-03-03-force-reindex-impl-plan.md`
- Existing force tests: `tests/test_doc_indexer_hash_sync.py`
- ChromaDB collection API: `collection.modify(metadata={...})`
