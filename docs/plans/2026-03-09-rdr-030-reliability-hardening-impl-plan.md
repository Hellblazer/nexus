# RDR-030 Reliability Hardening Implementation Plan

**Epic**: nexus-56u1 (Implement RDR-030: Reliability Hardening)
**RDR**: docs/rdr/rdr-030-reliability-hardening.md (status: accepted)
**Branch**: feature/nexus-56u1-reliability-hardening
**Created**: 2026-03-09

## Executive Summary

Implement the four-phase reliability hardening programme from RDR-030:
1. Add structured logging to 14 silent catch-and-pass blocks across 8 modules
2. Expand `nx doctor` with orphan T1 detection, T2 FTS5 integrity, and ChromaDB pagination audit
3. Fix a confirmed correctness bug in `doc_indexer.py` stale-chunk pruning (unpaginated `col.get()`)
4. Sweep the codebase to annotate all intentional silent catches with rationale comments

Total scope: ~14 logging additions (1-2 lines each), 3 new `nx doctor` checks, 1 pagination bug fix, ~10-15 annotation comments. Low risk: all changes are additive (logging, diagnostics, comments) except the pagination fix which is a correctness improvement.

## Dependency Graph

```
nexus-56u1 (Epic — tracker)
  |
  +-- nexus-4our  Phase 1 impl   -----> nexus-op38  Phase 1 tests
  |
  +-- nexus-n1bv  Phase 2 impl   -----> nexus-btid  Phase 2 tests
  |
  +-- nexus-eld3  Phase 2.5 tests -----> nexus-klxc  Phase 2.5 impl
  |
  +-- nexus-1hv5  Phase 3 sweep   (no test task — comments only)
```

**Parallelization**: Phases 1, 2, 2.5, and 3 are fully independent. Each can be assigned to a parallel agent. Within each phase, implementation blocks testing.

**Critical path**: Phase 2.5 (pagination bug fix) is the highest priority (P1) and should be addressed first since it is a correctness defect with data-loss risk.

## Phase 1: Silent Error Audit (14 locations)

**Bead**: nexus-4our (impl) + nexus-op38 (tests)
**Files modified**: indexer.py, session.py, hooks.py, commands/hook.py, commands/index.py, commands/doctor.py, md_chunker.py, classifier.py

### Prerequisites

Three modules lack a module-level `_log` binding and need one added before logging calls:

| Module | Addition |
|--------|----------|
| `commands/hook.py` | `import structlog` + `_log = structlog.get_logger()` |
| `commands/index.py` | `import structlog` + `_log = structlog.get_logger()` |
| `classifier.py` | `import structlog` + `_log = structlog.get_logger()` |

### Site-by-Site Changes

| # | File:Line | Exception caught | Current behavior | Change | Log level |
|---|-----------|-----------------|------------------|--------|-----------|
| 1 | `indexer.py:264-267` | `(UnicodeDecodeError, AttributeError)` | `pass` | Add `_log.debug("extract_name_decode_failed", error=str(exc), exc_info=True)` | debug |
| 2 | `indexer.py:270-273` | `(UnicodeDecodeError, AttributeError)` | `pass` | Add `_log.debug("extract_name_child_decode_failed", error=str(exc), exc_info=True)` | debug |
| 3 | `indexer.py:298-302` | `Exception` | `return ("", "")` | Add `_log.warning("get_parser_failed", language=language, error=str(exc), exc_info=True)` before return | warning |
| 4 | `indexer.py:304-307` | `Exception` | `return ("", "")` | Add `_log.debug("tree_parse_failed", language=language, error=str(exc))` before return | debug |
| 5 | `indexer.py:398` | `(OSError, subprocess.TimeoutExpired)` | `return ""` | Add `_log.debug("current_head_failed", repo=str(repo), error=str(exc))` before return | debug |
| 6 | `session.py:113-115` | `(OSError, ValueError)` | `pass` | Add `_log.debug("ppid_proc_read_failed", pid=pid, error=str(exc))` | debug |
| 7 | `session.py:199` | `(json.JSONDecodeError, OSError)` | `pass` | Add `_log.debug("sweep_corrupt_session_file", path=str(f), error=str(exc))` | debug |
| 8 | `hooks.py:55` | `Exception` | `return Path.cwd().name` | Add `_log.debug("infer_repo_git_failed", error=str(exc))` before return | debug |
| 9 | `hooks.py:155` | `(json.JSONDecodeError, OSError)` | `pass` | Add `_log.debug("session_end_own_record_corrupt", path=str(own_file), error=str(exc))` | debug |
| 10 | `commands/hook.py:24` | `Exception` | `pass` | Add `_log.debug("session_start_stdin_parse_failed", error=str(exc))` | debug |
| 11 | `commands/index.py:138` | `Exception` | `pass` | Add `_log.debug("hook_detection_failed", error=str(exc))` | debug |
| 12 | `commands/doctor.py:211` | `Exception` | `repos = []` | Add `_log.warning("doctor_registry_load_failed", error=str(exc))` | warning |
| 13 | `md_chunker.py:73` | `yaml.YAMLError` | `data = {}` | Add `_log.warning("frontmatter_parse_failed", error=str(exc))` before `data = {}` | warning |
| 14 | `classifier.py:46` | `OSError` | `return False` | Add `_log.debug("has_shebang_read_failed", path=str(path), error=str(exc))` before return | debug |

### Implementation Pattern

Each change follows the same pattern. Capture the exception into a variable and add one log line:

```python
# Before:
except (UnicodeDecodeError, AttributeError):
    pass

# After:
except (UnicodeDecodeError, AttributeError) as exc:
    _log.debug("extract_name_decode_failed", error=str(exc), exc_info=True)
```

For sites that already have `as exc`, only the log line is added.

### Success Criteria

- [ ] All 14 sites emit a log at the documented level when the exception path is triggered
- [ ] 3 modules have `_log = structlog.get_logger()` added
- [ ] `grep -r 'except.*:\s*pass$' src/nexus/{indexer,session,hooks,commands/hook,commands/index,commands/doctor,md_chunker,classifier}.py` returns 0 matches (all bare passes replaced with logging)
- [ ] All existing tests pass (`uv run pytest`)
- [ ] Code compiles cleanly

### Test Strategy (nexus-op38)

**Test file**: `tests/test_silent_error_logging.py`

Use `structlog.testing.capture_logs()` context manager for each site:

```python
import structlog
from structlog.testing import capture_logs

def test_indexer_get_parser_failure_logs_warning(monkeypatch):
    """Site 3: indexer.py get_parser() failure emits warning."""
    import nexus.indexer as mod
    monkeypatch.setattr("nexus.indexer.get_parser", lambda lang: (_ for _ in ()).throw(RuntimeError("no parser")))
    with capture_logs() as cap:
        result = mod._extract_context(b"x = 1", "python", 0, 0)
    assert result == ("", "")
    assert any(e["event"] == "get_parser_failed" and e["log_level"] == "warning" for e in cap)
```

Each of the 14 sites gets a test following this pattern:
1. Mock the failing dependency to trigger the exception path
2. Call the function under test
3. Assert the log event name and level appear in `capture_logs()` output
4. Assert the function returns the expected fallback value

## Phase 2: nx doctor Expansion

**Bead**: nexus-n1bv (impl) + nexus-btid (tests)
**File modified**: `src/nexus/commands/doctor.py`

### Step 5: Orphan T1 Process Detection

Scan `~/.config/nexus/sessions/` for `*.session` files whose `server_pid` does not correspond to a running process.

```python
# Pseudocode for doctor check
from nexus.session import SESSIONS_DIR

def _check_orphan_t1(lines: list[str]) -> bool:
    """Check for orphaned T1 session files. Returns True if all clean."""
    if not SESSIONS_DIR.exists():
        lines.append(_check_line("T1 sessions", True, "no sessions directory"))
        return True
    orphans = []
    for f in SESSIONS_DIR.glob("*.session"):
        try:
            record = json.loads(f.read_text())
            if not isinstance(record, dict):
                continue
            pid = record.get("server_pid")
            if pid:
                try:
                    os.kill(pid, 0)  # Check if alive
                except OSError:
                    orphans.append((f.name, pid))
        except (json.JSONDecodeError, OSError):
            orphans.append((f.name, None))
    if orphans:
        lines.append(_check_line("T1 sessions", False,
                     f"{len(orphans)} orphaned session file(s)"))
        return False
    lines.append(_check_line("T1 sessions", True, "no orphans"))
    return True
```

### Step 6: T2 Database Integrity Check

Run SQLite `PRAGMA integrity_check` and FTS5 `integrity-check` on the T2 memory database.

```python
def _check_t2_integrity(lines: list[str]) -> bool:
    """Verify T2 SQLite + FTS5 index integrity. Returns True if healthy."""
    db_path = Path.home() / ".config" / "nexus" / "memory.db"
    if not db_path.exists():
        lines.append(_check_line("T2 database", True, "not created yet"))
        return True
    try:
        conn = sqlite3.connect(str(db_path))
        # SQLite integrity
        result = conn.execute("PRAGMA integrity_check").fetchone()
        sqlite_ok = result and result[0] == "ok"
        # FTS5 integrity
        try:
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')")
            fts_ok = True
        except sqlite3.OperationalError:
            fts_ok = False
        conn.close()
        ok = sqlite_ok and fts_ok
        detail = "ok" if ok else []
        if not sqlite_ok:
            detail.append("SQLite integrity check failed")
        if not fts_ok:
            detail.append("FTS5 index corrupt")
        lines.append(_check_line("T2 database", ok,
                     detail if isinstance(detail, str) else "; ".join(detail)))
        return ok
    except (sqlite3.Error, OSError) as exc:
        lines.append(_check_line("T2 database", False, f"check failed: {exc}"))
        return False
```

### Step 7: ChromaDB Pagination Audit

Spot-check that `col.count()` matches the number of records retrievable via paginated `col.get()`. This catches any `col.get()` call site that silently truncates at 300.

```python
def _check_chroma_pagination(lines: list[str], client, db_name: str) -> bool:
    """Spot-check one collection's count vs paginated get. Returns True if consistent."""
    cols = client.list_collections()
    if not cols:
        return True
    # Spot-check the first non-empty collection
    for col in cols:
        count = col.count()
        if count == 0:
            continue
        # Paginated retrieval
        retrieved = 0
        offset = 0
        while True:
            batch = col.get(include=[], limit=300, offset=offset)
            batch_ids = batch.get("ids", [])
            retrieved += len(batch_ids)
            if len(batch_ids) < 300:
                break
            offset += 300
        if retrieved != count:
            lines.append(_check_line(
                f"pagination ({col.name})", False,
                f"count={count} but get returned {retrieved}"))
            return False
        lines.append(_check_line(
            f"pagination ({col.name})", True,
            f"{count} records consistent"))
        return True  # Spot-check one collection only
    return True
```

### Success Criteria

- [ ] `nx doctor` includes T1 orphan check, T2 integrity check, and ChromaDB pagination spot-check
- [ ] Each check reports pass/fail status in the existing output format
- [ ] Checks are gated behind credential/path availability (no crashes if unconfigured)
- [ ] Fix suggestions provided for failures

### Test Strategy (nexus-btid)

**Test file**: `tests/test_doctor_integrity.py`

- **Step 5**: Create tmp sessions dir with fake session files. Set `server_pid` to a non-existent PID. Verify doctor detects orphan.
- **Step 6**: Create a T2 database, verify integrity check passes. Then corrupt the database (truncate file), verify check fails.
- **Step 7**: Use `chromadb.EphemeralClient` with a test collection. Add records, verify pagination audit passes. (Cannot test truncation without a real Cloud client, but can verify the audit runs without error.)

## Phase 2.5: ChromaDB Pagination Fix

**Bead**: nexus-klxc (impl, P1 bug) + nexus-eld3 (tests)
**File modified**: `src/nexus/doc_indexer.py`

### Problem

Line 233 of `doc_indexer.py`:
```python
all_existing = _chroma_with_retry(col.get, where={"source_path": str(file_path)}, include=[])
```

This call has no `limit=` parameter. ChromaDB Cloud returns at most 300 records per `get()` call. For documents that produce >300 chunks, stale chunks beyond the 300-record limit survive re-indexing and pollute search results.

### Fix

Replace the single `col.get()` call with a pagination loop matching the pattern already used in `db/t3.py` (e.g., `delete_by_source()`, `find_ids_by_title()`):

```python
# Paginated retrieval of all existing IDs for this source_path
current_ids_set = set(ids)
stale_ids: list[str] = []
offset = 0
while True:
    batch = _chroma_with_retry(
        col.get,
        where={"source_path": str(file_path)},
        include=[],
        limit=300,
        offset=offset,
    )
    batch_ids = batch.get("ids", [])
    stale_ids.extend(eid for eid in batch_ids if eid not in current_ids_set)
    if len(batch_ids) < 300:
        break
    offset += 300
if stale_ids:
    _chroma_with_retry(col.delete, ids=stale_ids)
```

### Success Criteria

- [ ] `doc_indexer.py:_index_document()` stale-chunk pruning uses `limit=300` + offset pagination
- [ ] Documents with >300 chunks have all stale chunks correctly pruned on re-index
- [ ] Existing tests pass (no regression)

### Test Strategy (nexus-eld3)

**Test file**: `tests/test_doc_indexer_pagination.py`

Use `chromadb.EphemeralClient` to simulate a collection with >300 chunks for a single source_path:

1. Pre-populate collection with 350 fake chunks (IDs: `hash_0` through `hash_349`)
2. Call `_index_document()` with a file that produces only 10 new chunks
3. Verify that all 340 stale chunks (350 - 10) are deleted
4. Verify that the 10 new chunks are present

This requires mocking the embed function to avoid Voyage API calls.

## Phase 3: Codebase Sweep

**Bead**: nexus-1hv5 (chore, P3)
**Files modified**: Multiple (annotation-only changes)

### Process

1. Run `grep -rn 'except.*:\s*$' src/nexus/ --include='*.py' -A2` to find all except blocks
2. For each block without logging: add `# intentional: <reason>` comment
3. Skip blocks that already have logging, re-raise, or were addressed in Phase 1

### Known Intentional Silent Catches to Annotate

| File:Line | Exception | Reason |
|-----------|-----------|--------|
| `session.py:60` | `ValueError` in `_stable_pid()` | Invalid NX_SESSION_PID env var — fall through to os.getsid(0) |
| `session.py:302-303` | `ChildProcessError` in `stop_t1_server()` | Already commented: "not our child" |
| `session.py:304-305` | `OSError` in `stop_t1_server()` | Process already gone after SIGKILL — expected |
| `session.py:342-343` | `OSError` in `_try_remove_path()` | Best-effort file cleanup |
| `hooks.py:196-197` | `OSError` in `session_end()` | Best-effort session file deletion |
| `config.py:140-141` | `OSError` in atomic write cleanup | Cleanup after re-raise |
| `registry.py:179-180` | `OSError` in atomic write cleanup | Cleanup after re-raise |
| `scoring.py:187-188` | `StopIteration` in round-robin merge | Iterator exhaustion is normal |
| `frecency.py:96-97` | `ValueError` in timestamp parse | Corrupt git log line — skip it |

### Success Criteria

- [ ] Every `except` block without logging has either: (a) logging added (Phase 1), or (b) `# intentional: <reason>` comment
- [ ] No bare `pass` in exception handlers without explanation
- [ ] All existing tests pass

### Test Strategy

No tests needed. This phase is purely annotation/documentation.

## Execution Order

### Recommended Sequence

1. **Phase 2.5** (nexus-eld3 tests first, then nexus-klxc impl) — P1 bug fix, TDD, highest priority
2. **Phase 1** (nexus-4our + nexus-op38) — Core reliability improvement
3. **Phase 2** (nexus-n1bv + nexus-btid) — Diagnostic expansion
4. **Phase 3** (nexus-1hv5) — Cleanup sweep

### Parallelization Opportunities

All four phases touch different code with minimal overlap:
- **Phase 1**: indexer.py, session.py, hooks.py, commands/hook.py, commands/index.py, commands/doctor.py, md_chunker.py, classifier.py (logging additions only)
- **Phase 2**: commands/doctor.py (new check functions)
- **Phase 2.5**: doc_indexer.py (pagination fix)
- **Phase 3**: Multiple files (comment annotations only)

Phase 1 and Phase 2 both touch `commands/doctor.py`, but Phase 1 modifies line 211 (one log line) while Phase 2 adds new functions. Merge conflict risk is minimal.

**Recommendation**: Spawn up to 3 parallel agents — one for Phases 1+3 (logging + annotations, same concern), one for Phase 2 (doctor expansion), one for Phase 2.5 (pagination fix).

## Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Adding logging to hot path degrades performance | Very low | Low | structlog short-circuits inactive levels; RDR confirms negligible overhead |
| nx doctor ChromaDB pagination audit slow for large collections | Low | Low | Spot-check only one collection; gate behind credential availability |
| Pagination fix changes stale-chunk pruning behavior | Low | Medium | Well-tested via EphemeralClient; fix is strictly more correct than current behavior |
| Phase 3 annotations create noisy diff | Very low | Very low | Comments are non-functional; can be merged independently |

## Bead Summary

| ID | Title | Type | Priority | Depends on |
|----|-------|------|----------|-----------|
| nexus-56u1 | Implement RDR-030: Reliability Hardening | feature | P2 | (tracker) |
| nexus-4our | Phase 1: Add structlog to 14 silent catch-and-pass blocks | task | P2 | none |
| nexus-op38 | Phase 1 Tests: capture_logs verification | task | P2 | nexus-4our |
| nexus-n1bv | Phase 2: nx doctor Steps 5-7 | task | P2 | none |
| nexus-btid | Phase 2 Tests: nx doctor expansion | task | P2 | nexus-n1bv |
| nexus-eld3 | Phase 2.5 Tests: pagination fix verification (TDD) | task | P1 | none |
| nexus-klxc | Phase 2.5: Fix doc_indexer.py pagination bug | bug | P1 | nexus-eld3 |
| nexus-1hv5 | Phase 3: Codebase sweep annotations | chore | P3 | none |

## Context for Executing Agents

### Knowledge Base Search Terms
- `structlog capture_logs testing` — for test patterns
- `chromadb pagination limit offset` — for pagination fix
- `nx doctor health check` — for existing doctor patterns
- `silent error logging policy` — for RDR-030 context

### Key References
- RDR: `docs/rdr/rdr-030-reliability-hardening.md`
- Existing pagination patterns: `src/nexus/db/t3.py` (search for `limit=300`)
- Existing doctor checks: `src/nexus/commands/doctor.py`
- Session management: `src/nexus/session.py`
- T2 schema: `src/nexus/db/t2.py` (FTS5 triggers and schema)

### Reminders for Executing Agents
- Use `mcp__sequential-thinking__sequentialthinking` for complex analysis
- Write tests FIRST (TDD) — Phase 2.5 and Phase 2 require tests before impl; Phase 1 logging additions are trivial enough for impl-first
- Ensure code compiles including all tests before marking complete
- Update continuation state after each significant milestone
- Sub-agents may spawn children for intensive work
