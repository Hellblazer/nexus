# RDR-004 Four-Store T3 Architecture — Implementation Plan

**Date:** 2026-02-28
**Design doc:** `docs/plans/2026-02-28-rdr-004-four-store-cloud-design.md`
**Branch:** `feature/rdr-004-four-store-t3`

## Executive Summary

Replace the single `chromadb.CloudClient` in `T3Database` with a dict of four
CloudClient instances (`code`, `docs`, `rdr`, `knowledge`), each pointed at a
suffixed database name. All routing is internal to `T3Database`; no callers
change. A new `nx migrate t3` command copies collections from the old single
store to the four new typed stores.

## Scope

| Area | Files changed | New files |
|------|--------------|-----------|
| T3 routing refactor | `src/nexus/db/t3.py` | -- |
| Migration command | -- | `src/nexus/commands/migrate.py` |
| CLI registration | `src/nexus/cli.py` | -- |
| Routing tests | `tests/test_t3.py` | -- |
| Migration tests | -- | `tests/test_migrate_cmd.py` |
| Doctor check | `src/nexus/commands/doctor.py` | -- |
| Documentation | `docs/storage-tiers.md`, `docs/getting-started.md`, `docs/configuration.md`, `docs/rdr/rdr-004-four-store-architecture.md`, `CHANGELOG.md` | -- |

**No other production source files change.** The public API of `T3Database` and
`make_t3()` remain identical. Every caller (`search_cmd.py`, `indexer.py`,
`pm.py`, `store.py`, etc.) is untouched.

---

## Phase 1 — T3Database Routing Refactor

**Goal:** Replace `self._client` (single CloudClient) with `self._clients`
(dict of 4 CloudClients) and route every operation to the correct client based
on collection name prefix.

### Task 1.1: Write Failing Routing Tests (TDD RED)

**File:** `tests/test_t3.py`
**Depends on:** nothing

Add a new fixture `mock_four_clients` that provides 4 distinct MagicMock
clients. Then add the following tests from the test plan:

| Test ID | Test name | What it verifies |
|---------|-----------|-----------------|
| P1 | `test_t3_routes_code_collection_to_code_client` | `get_or_create_collection("code__repo")` uses the code client |
| P2 | `test_t3_routes_docs_collection_to_docs_client` | `get_or_create_collection("docs__corpus")` uses the docs client |
| P3 | `test_t3_routes_rdr_collection_to_rdr_client` | `get_or_create_collection("rdr__decisions")` uses the rdr client |
| P4 | `test_t3_routes_knowledge_collection_to_knowledge_client` | `get_or_create_collection("knowledge__sec")` uses the knowledge client |
| P5 | `test_t3_unknown_prefix_falls_back_to_knowledge_client` | `get_or_create_collection("misc__other")` falls back to knowledge client |
| P6 | `test_t3_list_collections_fans_out_across_all_four_clients` | `list_collections()` combines results from all 4 clients |
| P7 | `test_t3_expire_only_touches_knowledge_client` | `expire()` only calls `list_collections` and `get_collection` on the knowledge client |
| P8 | `test_t3_single_mock_injection_still_works` | `T3Database(_client=mock)` maps all 4 types to the same mock; basic operations work |

**Fixture design for P1-P7:**

```python
@pytest.fixture
def four_clients():
    """Four distinct mock clients for routing tests."""
    clients = {t: MagicMock(name=f"client_{t}") for t in ("code", "docs", "rdr", "knowledge")}
    db = T3Database(_client=MagicMock())  # placeholder, overwritten below
    db._clients = clients
    db._ef_override = MagicMock()  # avoid VoyageAI calls
    return db, clients
```

**Acceptance criteria:**
- All 8 tests exist and FAIL (RED) before any production code changes
- Tests do not import or depend on implementation details beyond `_clients` dict
  (which is the documented internal structure from the design doc)

### Task 1.2: Implement Routing in T3Database (TDD GREEN)

**File:** `src/nexus/db/t3.py`
**Depends on:** Task 1.1

#### Step 1: Replace `self._client` with `self._clients` in `__init__`

**Location:** Lines 48-53

Replace:
```python
if _client is not None:
    self._client = _client
else:
    self._client = chromadb.CloudClient(
        tenant=tenant, database=database, api_key=api_key
    )
```

With:
```python
_STORE_TYPES = ("code", "docs", "rdr", "knowledge")

if _client is not None:
    self._clients = {t: _client for t in _STORE_TYPES}
else:
    self._clients = {
        t: chromadb.CloudClient(
            tenant=tenant, database=f"{database}_{t}", api_key=api_key
        )
        for t in _STORE_TYPES
    }
```

#### Step 2: Add `_client_for()` routing helper

**Insert after** the `_embedding_fn` method (after line 76):

```python
def _client_for(self, collection_name: str) -> chromadb.CloudClient:
    """Return the CloudClient responsible for *collection_name*."""
    prefix = collection_name.split("__")[0] if "__" in collection_name else "knowledge"
    return self._clients.get(prefix, self._clients["knowledge"])
```

#### Step 3: Update all `self._client` callsites (14 occurrences)

Each `self._client.xxx(name, ...)` becomes `self._client_for(name).xxx(name, ...)`.
Method-by-method changes:

| Method | Line(s) | Change |
|--------|---------|--------|
| `get_or_create_collection` | 84 | `self._client_for(name).get_or_create_collection(...)` |
| `search` | 224 | `self._client_for(name).get_collection(...)` |
| `list_store` | 296 | `self._client_for(collection).get_collection(...)` |
| `collection_exists` | 335 | `self._client_for(name).get_collection(...)` |
| `delete_collection` | 342 | `self._client_for(name).delete_collection(...)` |
| `delete_by_source` | 347 | `self._client_for(collection_name).get_collection(...)` |
| `collection_info` | 362 | `self._client_for(name).get_collection(...)` |
| `collection_metadata` | 376 | `self._client_for(collection_name).get_collection(...)` |

#### Step 4: Special case — `expire()` (lines 270-284)

`expire()` only processes `knowledge__*` collections. Replace:
```python
for col_or_name in self._client.list_collections():
    ...
    col = self._client.get_collection(name)
```
With:
```python
kc = self._clients["knowledge"]
for col_or_name in kc.list_collections():
    ...
    col = kc.get_collection(name)
```

#### Step 5: Special case — `list_collections()` (lines 305-330)

Must fan out across all 4 clients and aggregate. Replace:
```python
for col_or_name in self._client.list_collections():
    names.append(...)
...
def _count(name: str) -> dict:
    col = self._client.get_collection(name)
    return {"name": name, "count": col.count()}
```
With:
```python
seen: set[str] = set()
for client in self._clients.values():
    for col_or_name in client.list_collections():
        n = col_or_name if isinstance(col_or_name, str) else col_or_name.name
        if n not in seen:
            names.append(n)
            seen.add(n)
...
def _count(name: str) -> dict:
    col = self._client_for(name).get_collection(name)
    return {"name": name, "count": col.count()}
```

Note: `seen` set deduplicates because `_clients.values()` may contain the same
client instance multiple times (e.g., single-mock-injection path where all 4
keys map to one mock).

**Acceptance criteria:**
- All 8 new routing tests (P1-P8) pass (GREEN)
- `_STORE_TYPES` defined as module-level constant for reuse

### Task 1.3: Update Existing Tests That Reference `_client`

**File:** `tests/test_t3.py`
**Depends on:** Task 1.2

Three existing tests directly inspect `T3Database._client` or assert
`CloudClient.assert_called_once_with()`. These must be updated:

| Test | Current assertion | New assertion |
|------|------------------|---------------|
| `test_cloudclient_init` (line 23) | `CloudClient.assert_called_once_with(tenant=..., database=..., api_key=...)` | Verify 4 calls with `f"{database}_{t}"` for each store type |
| `test_make_t3_uses_credentials` (line 392) | `CloudClient.assert_called_once_with(tenant=..., database="my-db", api_key=...)` | Verify 4 calls with suffixed database names; verify `_voyage_api_key` |
| `test_make_t3_client_injection` (line 411) | `assert db._client is fake_client` | `assert all(c is fake_client for c in db._clients.values())` |

**Acceptance criteria:**
- All 3 updated tests pass
- Full existing test suite passes (`pytest tests/test_t3.py` — all tests green)

### Task 1.4: Full Test Suite Verification

**Depends on:** Task 1.3

Run the complete test suite to verify no regressions:
```bash
pytest tests/ -x -q
```

**Acceptance criteria:**
- All tests pass with zero failures
- No test outside `tests/test_t3.py` was modified

---

## Phase 2 — Migration Command

**Goal:** Create `nx migrate t3` to copy collections from the old single-database
store to the new four-store routing.

**Depends on:** Phase 1 complete (make_t3() must route correctly)

### Task 2.1: Write Failing Migration Tests (TDD RED)

**File:** `tests/test_migrate_cmd.py` (new)
**Depends on:** Phase 1

| Test ID | Test name | What it verifies |
|---------|-----------|-----------------|
| P9 | `test_migrate_routes_code_collection_to_code_store` | `code__repo` from source ends up in the code-typed destination |
| P10 | `test_migrate_routes_docs_collection_to_docs_store` | `docs__corpus` from source ends up in the docs-typed destination |
| P11 | `test_migrate_is_idempotent_when_counts_match` | When dest count == source count, collection is skipped |
| P12 | `test_migrate_copies_embeddings_verbatim` | Embeddings in dest match source exactly (no re-embedding) |

**Test approach:** Mock both source and destination. Source is a mock
`chromadb.CloudClient` with pre-populated collections. Destination is a mock
`T3Database` (or use `_client` injection with 4 distinct mocks to verify
routing).

Alternatively, use Click's `CliRunner` to invoke the command end-to-end with
`EphemeralClient` instances (preferred — tests the real code path).

**Design for testability:** The migration logic should be extracted into a
`migrate_t3(source_client, dest_db, ...)` function that can be called directly
from tests, separate from the Click command handler.

**Acceptance criteria:**
- All 4 tests exist and FAIL (RED) before implementation
- Tests use mock clients, no real ChromaDB Cloud calls

### Task 2.2: Implement Migration Command (TDD GREEN)

**File:** `src/nexus/commands/migrate.py` (new)
**Depends on:** Task 2.1

```python
@click.group()
def migrate():
    """Migration utilities."""
    pass

@migrate.command("t3")
def migrate_t3_cmd():
    """Migrate T3 collections from single-database to four-store layout."""
    ...
```

**Core logic (`migrate_t3_collections` function):**

```python
def migrate_t3_collections(
    source: chromadb.ClientAPI,
    dest: T3Database,
    *,
    verbose: bool = False,
) -> dict[str, int]:
    """Copy collections from source (old single DB) to dest (new 4-store).

    Returns dict mapping collection name -> documents copied.
    Idempotent: skips when dest count == source count.
    Non-destructive: source is never modified.
    """
```

**Algorithm:**
1. `source.list_collections()` to get all collection names
2. For each collection:
   a. `src_col = source.get_collection(name)`
   b. `src_count = src_col.count()`
   c. Check if dest already has it: `dest.collection_exists(name)` and count matches
   d. If counts match, skip (idempotent)
   e. Otherwise: `data = src_col.get(include=["documents", "metadatas", "embeddings"])`
   f. `dest_col = dest.get_or_create_collection(name)`
   g. `dest_col.upsert(ids=data["ids"], documents=data["documents"], embeddings=data["embeddings"], metadatas=data["metadatas"])`
3. Print progress per collection, final summary

**Source client construction in Click handler:**
```python
source = chromadb.CloudClient(
    tenant=get_credential("chroma_tenant"),
    database=get_credential("chroma_database"),  # old unsuffixed name
    api_key=get_credential("chroma_api_key"),
)
dest = make_t3()  # new four-store routing
```

**Acceptance criteria:**
- All 4 migration tests pass (GREEN)
- `migrate_t3_collections` is a standalone function (testable without Click)
- Idempotent: running twice produces no duplicate data
- Embeddings copied verbatim (no re-embedding)

### Task 2.3: Register in CLI

**File:** `src/nexus/cli.py`
**Depends on:** Task 2.2

Add:
```python
from nexus.commands.migrate import migrate
...
main.add_command(migrate)
```

**Acceptance criteria:**
- `nx migrate t3` is callable from the CLI
- `nx migrate --help` shows the subcommand

### Task 2.4: Migration Test Suite Verification

**Depends on:** Task 2.3

```bash
pytest tests/test_migrate_cmd.py tests/test_t3.py -x -q
```

**Acceptance criteria:**
- All migration and routing tests pass
- No regressions in existing T3 tests

---

## Phase 3 — Doctor Check + Documentation

**Depends on:** Phase 1 (doctor check needs to know about 4 databases)
**Can run in parallel with** Phase 2

### Task 3.1: Add Four-Database Doctor Check

**File:** `src/nexus/commands/doctor.py`
**Depends on:** Phase 1

Add a check that verifies the four derived databases exist in ChromaDB Cloud.
After the existing `CHROMA_DATABASE` check (line 86-93), add:

```python
# ── Four-store databases ─────────────────────────────────────────────────
if chroma_database:
    base = chroma_database
    for suffix in ("code", "docs", "rdr", "knowledge"):
        db_name = f"{base}_{suffix}"
        try:
            chromadb.CloudClient(
                tenant=chroma_tenant, database=db_name, api_key=chroma_key
            )
            lines.append(_check_line(f"ChromaDB  ({db_name})", True, "accessible"))
        except Exception as exc:
            lines.append(_check_line(f"ChromaDB  ({db_name})", False, str(exc)))
            failed = True
            _fix(lines,
                 f"Create database '{db_name}' in ChromaDB Cloud dashboard",
                 "https://trychroma.com  (Dashboard -> Databases)")
```

**Note:** This check is expensive (4 HTTP calls). Consider making it opt-in
(`nx doctor --full`) or caching the result. For v1, include it unconditionally
since doctor is an infrequent diagnostic command.

**Acceptance criteria:**
- `nx doctor` reports the status of all 4 derived databases
- Missing databases show clear fix instructions

### Task 3.2: Update Documentation

**Files:**
- `docs/storage-tiers.md` — update T3 table to show four separate cloud databases
- `docs/getting-started.md` — mention four databases to create in ChromaDB Cloud
- `docs/configuration.md` — explain base-name derivation (`{base}_code`, etc.)

**Acceptance criteria:**
- All three docs updated to reflect four-store architecture
- Migration instructions prominently documented

### Task 3.3: Restore RDR-004 Design Record

**File:** `docs/rdr/rdr-004-four-store-architecture.md`
**Depends on:** nothing

Restore the RDR with the correct cloud-based design (not the reverted
PersistentClient version).

**Acceptance criteria:**
- RDR-004 exists with correct CloudClient-based design
- Status: Approved

### Task 3.4: CHANGELOG Entry

**File:** `CHANGELOG.md`

Add entry under the next release section:

```markdown
### Added
- Four-store T3 architecture: collections route to typed CloudDB databases
  (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`)
- `nx migrate t3` command for migrating from single-database to four-store layout
- `nx doctor` now checks all four T3 database connections
```

**Acceptance criteria:**
- CHANGELOG entry describes the feature and migration path

---

## Dependency Graph

```
Phase 1
  Task 1.1 (write routing tests)
    └──> Task 1.2 (implement routing)
           └──> Task 1.3 (fix existing tests)
                  └──> Task 1.4 (verify all tests)
                         │
                         ├──> Phase 2
                         │      Task 2.1 (write migration tests)
                         │        └──> Task 2.2 (implement migration)
                         │               └──> Task 2.3 (register CLI)
                         │                      └──> Task 2.4 (verify)
                         │
                         └──> Phase 3 (parallelizable)
                                Task 3.1 (doctor check)
                                Task 3.2 (docs update)
                                Task 3.3 (RDR restore)
                                Task 3.4 (CHANGELOG)
```

**Critical path:** Task 1.1 -> 1.2 -> 1.3 -> 1.4 -> 2.1 -> 2.2 -> 2.3 -> 2.4

## Risks and Mitigations

### Risk 1: ChromaDB Cloud database creation (HIGH)

CloudClient errors immediately if the database does not exist. Users must
create all four databases in their ChromaDB Cloud dashboard before upgrading.

**Mitigation:** Task 3.1 adds a doctor check. Consider also wrapping the
4-client creation in `T3Database.__init__` with a try/except that produces a
clear error message listing which databases need to be created.

### Risk 2: Migration source uses raw CloudClient (MEDIUM)

After Phase 1, `T3Database(database="nexus")` creates clients for
`nexus_code`, `nexus_docs`, etc. — not the old `nexus` database. The migration
source must use `chromadb.CloudClient(database="nexus")` directly to access the
old single store.

**Mitigation:** Task 2.2 explicitly constructs a raw `CloudClient` for the
source, not a `T3Database`. This is documented in the implementation.

### Risk 3: Existing tests that inspect `_client` attribute (LOW)

Three existing tests in `test_t3.py` directly reference `db._client` or assert
`CloudClient.assert_called_once_with()`. These break after the refactor.

**Mitigation:** Task 1.3 explicitly updates these three tests. The quality
criterion "existing test suite passes without modification" applies to tests
OUTSIDE `test_t3.py` (callers like `test_indexer_e2e.py`). The T3Database
internal tests naturally need updating when internals change.

### Risk 4: `list_collections()` deduplication with single-mock injection (LOW)

When `_client=mock` maps all 4 types to the same mock, iterating
`self._clients.values()` calls `list_collections()` on the same mock 4 times,
potentially returning duplicates.

**Mitigation:** Use a `seen` set in the fan-out loop to deduplicate. Already
accounted for in Task 1.2 Step 5.

### Risk 5: `mock_chromadb` fixture returns same mock for all 4 CloudClient calls (LOW)

The existing `mock_chromadb` fixture sets `m.CloudClient.return_value = mock_client`.
After refactor, `T3Database.__init__` calls `CloudClient()` 4 times, each getting
the same mock. This is actually correct behavior — tests that don't care about
routing get a single shared mock, which is the desired backward-compat behavior.

**Mitigation:** No action needed. The fixture works correctly as-is for non-routing tests.

## Estimated Effort

| Phase | Tasks | Estimate |
|-------|-------|----------|
| Phase 1 | 4 tasks | 2-3 hours |
| Phase 2 | 4 tasks | 2-3 hours |
| Phase 3 | 4 tasks | 1-2 hours |
| **Total** | **12 tasks** | **5-8 hours** |

This is a 1-day project. No PM infrastructure needed.
