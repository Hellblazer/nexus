---
title: "RDR-101 Phase 0: RDR-086 chash_index doc_id naming-collision resolution"
rdr: RDR-101
phase: 0
bead: nexus-o6aa.3
created: 2026-04-30
author: Hal Hildebrand
status: deliverable
---

# RDR-101 Phase 0: RDR-086 `chash_index.doc_id` naming-collision resolution

## Decision

**Option A: rename.** The T2 `chash_index.doc_id` column is renamed to
**`chunk_chroma_id`** before RDR-101 Phase 3 ships.

Rationale in one paragraph: RDR-086's `chash_index.doc_id` stores the
**ChromaDB-scoped chunk natural ID** (the per-collection identifier
ChromaDB assigns to each chunk row). RDR-101's `Document.doc_id` is a
**UUID7 document identity** that owns a Document entity across its
lifecycle. Same column name, two unrelated namespaces. Phase 3 introduces
the new `Document` and `Chunk` projections and a reader-side join key of
`(coll_id, chash)` per RDR-101 §Phase 5. If both `doc_id` meanings ship
into Phase 3 unresolved, every reader has to disambiguate by table for the
remaining life of the system. Renaming the chash_index column to
`chunk_chroma_id` aligns with RDR-101's `chunk_id` terminology
(`Chunk.chunk_id PK` is the Chroma natural ID per the §Entities ER
diagram) and pays a one-time migration cost in exchange for permanent
unambiguous naming.

Option B (document-only) was rejected. The grep gate is brittle: any
future T2 migration or projection layer reintroduces the same trap. The
migration cost in A is an `ALTER TABLE` plus four read-path call-site
edits (see §Read-path call sites), bounded by Phase 0's window.

## Background

The colliding column lives in one DDL site and is consumed by a small
read-path. Confirmed today, 2026-04-30:

### Column declaration

- `src/nexus/db/t2/chash_index.py:50-56`. `_CHASH_INDEX_SCHEMA_SQL`
  declares `doc_id TEXT NOT NULL` as the third column of the `chash_index`
  table, alongside `chash`, `physical_collection`, `created_at`. PK is
  `(chash, physical_collection)`. Module docstring at lines 3-7 names the
  store "global chunk-hash to (collection, doc_id) lookup table" and
  identifies the value semantics as "which physical collection and doc_id
  hold the chunk".

- `src/nexus/db/migrations.py:774-822`. `migrate_chash_index` is the
  installed migration (ID 1654-1655) and emits an identical schema. The
  migration docstring at lines 784-791 declares the column type and
  position.

### Live host catalog (read-only inspection 2026-04-30)

T2 path: `~/.config/nexus/memory.db`. Read-only `mode=ro` URI connection,
no writes performed. Findings:

- Schema present and matches both DDL sites (single source of truth, no
  drift):

  ```sql
  CREATE TABLE chash_index (
      chash                TEXT NOT NULL,
      physical_collection  TEXT NOT NULL,
      doc_id               TEXT NOT NULL,
      created_at           TEXT NOT NULL,
      PRIMARY KEY (chash, physical_collection)
  )
  ```

- Row count: **111,461** rows across **578** distinct `physical_collection`
  values.

- Sample row shape: `doc_id` values are 32-hex-character ChromaDB-scoped
  identifiers (e.g. `006ac362d042161e4c71647f34a3fa12`,
  `f1ddd6cd524d9207ecd9c2f695fc1b9f`). They are **not** UUID7. They are
  per-collection Chroma natural IDs of the form ChromaDB returns from
  `col.get(...)["ids"]`.

This confirms that the production `chash_index.doc_id` column carries
exactly the semantics RDR-086 ascribed to it: the ChromaDB-scoped chunk
natural ID, equivalent to RDR-101's `Chunk.chunk_id PK` field.

### Why the collision matters in Phase 3

RDR-101 §Entities defines:

- `Document.doc_id PK (UUID7)` (per `rdr-101-catalog-t3-metadata-design.md`
  line 139). This is the Document entity's identity.
- `Chunk.chunk_id PK "Chroma natural ID, NOT chash"` (line 159). This is
  what RDR-086's `chash_index.doc_id` actually stores.

RDR-101 Phase 5 (line 591) explicitly rewrites the catalog
`register()` idempotency guard to query `(coll_id, chash, doc_id)`
against the new Chunk projection. In that statement, `doc_id` means
**`Document.doc_id` (UUID7)**. A reader who also reaches into
`chash_index.doc_id` to pull "the doc_id for this chash" gets a Chroma
natural ID instead. That is precisely the disambiguation tax we want to
eliminate before Phase 3.

## Migration plan (Option A)

Phase 0 does not run the migration; it specifies it. The actual SQL runs
in Phase 1 or pre-Phase-3 in lockstep with the Python read-path edits,
inside a single deploy window.

### Dry-run inspection (READ-ONLY, runnable today)

```python
# scripts/inspect-chash-index-collision.py (proposed)
# Phase 0 deliverable: read-only inspection of the live chash_index
# state. Does NOT execute any DDL; only prints proposed migration SQL.
import sqlite3
from pathlib import Path

DB = Path.home() / ".config" / "nexus" / "memory.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

# 1. Schema snapshot.
print("=== current schema ===")
for row in conn.execute(
    "SELECT sql FROM sqlite_master WHERE name='chash_index'"
):
    print(row[0])

# 2. Row count.
(rows,) = conn.execute("SELECT COUNT(*) FROM chash_index").fetchone()
print(f"\nrow count: {rows:,}")

# 3. Sample 5 rows so reviewers see the doc_id value shape.
print("\n=== sample rows ===")
for r in conn.execute(
    "SELECT substr(chash,1,16), physical_collection, doc_id, created_at "
    "FROM chash_index LIMIT 5"
):
    print(r)

# 4. Distinct collections (not strictly needed for the migration but
#    documents the surface area).
(colls,) = conn.execute(
    "SELECT COUNT(DISTINCT physical_collection) FROM chash_index"
).fetchone()
print(f"\ndistinct collections: {colls}")

# 5. Print the proposed migration SQL. NOT EXECUTED.
print("\n=== proposed migration SQL (NOT EXECUTED) ===")
print("ALTER TABLE chash_index RENAME COLUMN doc_id TO chunk_chroma_id;")

conn.close()
```

Inspection ran successfully on 2026-04-30 against the live host catalog
in strict read-only mode (sqlite URI `mode=ro`). Output is summarised in
§Background above; the database file's mtime did not change.

### Production migration (deferred)

Single statement, idempotent against a re-run via the migrations
framework's "skip if column already renamed" check:

```sql
-- migrate_chash_index_rename_doc_id (RDR-101 Phase 0/1)
ALTER TABLE chash_index RENAME COLUMN doc_id TO chunk_chroma_id;
```

Migration framework wrapper:

```python
def migrate_chash_index_rename_doc_id(conn: sqlite3.Connection) -> None:
    """Rename chash_index.doc_id to chunk_chroma_id (RDR-101 Phase 0).

    SQLite 3.25+ supports ALTER TABLE ... RENAME COLUMN natively. The
    PK (chash, physical_collection) is unaffected; the secondary index
    idx_chash_index_collection on physical_collection is unaffected.
    Idempotent: detect the new column and no-op if already renamed.
    """
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(chash_index)").fetchall()
    }
    if "chunk_chroma_id" in cols:
        return
    if "doc_id" not in cols:
        # Table absent or already migrated under a different name.
        # Let the create-if-not-exists path own this case.
        return
    conn.execute(
        "ALTER TABLE chash_index RENAME COLUMN doc_id TO chunk_chroma_id"
    )
    conn.commit()
```

Register the migration after `migrate_chash_index` in
`src/nexus/db/migrations.py` (the existing migration list at line 1654)
so it runs once per host on next `apply_pending`.

### Code changes that ship in the same commit as the SQL

The migration is purely cosmetic at the SQL layer; the Python rename has
to land in the same deploy or readers crash on the missing column. Eight
edit points (see §Read-path call sites), all mechanical.

## Candidate RDR-086 amendment

Insert into `docs/rdr/rdr-086-chash-span-resolution.md` as a new top-level
section between §References and §Revision History (line 854). Suggested
heading: `## Amendment: chunk_chroma_id rename (RDR-101 Phase 0)`. Block
quoted markdown for paste:

> ## Amendment: `chunk_chroma_id` rename (RDR-101 Phase 0)
>
> Status: amendment, 2026-04-30. Owner: RDR-101 Phase 0 (bead
> nexus-o6aa.3).
>
> The T2 `chash_index.doc_id` column is renamed to **`chunk_chroma_id`**
> ahead of RDR-101 Phase 3.
>
> Reason: RDR-101 introduces `Document.doc_id` as a UUID7 document
> identity (RDR-101 §Entities, `Document.doc_id PK`). RDR-086's
> `chash_index.doc_id` stores the ChromaDB-scoped chunk natural ID
> (equivalent to RDR-101's `Chunk.chunk_id`). The shared name forces
> every Phase 3+ reader to disambiguate by table, which a one-time
> rename eliminates.
>
> Migration: single `ALTER TABLE chash_index RENAME COLUMN doc_id TO
> chunk_chroma_id` SQL plus eight Python read-path edits, applied in
> lockstep. PK `(chash, physical_collection)` and the
> `idx_chash_index_collection` secondary index are unaffected. The
> migration is idempotent (detects the renamed column and no-ops).
>
> Affected RDR-086 surfaces:
>
> - `ChashIndex.upsert(chash=..., collection=..., doc_id=...)`: keyword
>   renamed to `chunk_chroma_id`.
> - `ChashIndex.lookup(chash) -> [{"collection", "chunk_chroma_id",
>   "created_at"}]`: dict key renamed.
> - `ChashIndex.doc_ids_present_in_collection(...)`: method renamed
>   to `chunk_chroma_ids_present_in_collection(...)`.
> - `Catalog.resolve_chash(...)` `ChunkRef` return shape: the
>   `doc_id` key remains, **but its semantics now explicitly point at
>   `Document.doc_id` (UUID7) once RDR-101 Phase 3 lands.** Until
>   then, `Catalog.resolve_chash` continues to surface the Chroma
>   natural ID under the `doc_id` key for back-compat with the
>   `check-extensions` caller. Phase 3 of RDR-101 owns the cutover:
>   `ChunkRef.doc_id` becomes the UUID7 and a new `chunk_chroma_id`
>   key surfaces the Chroma natural ID.
>
> The RDR-086 `chunk_grounded_in(doc_id, ...)` taxonomy contract is
> unaffected: `topic_assignments.doc_id` is the ChromaDB-scoped chunk
> ID and is not part of `chash_index`. That column will be addressed
> in a separate Phase 3 amendment when the projection-layer naming
> aligns.

## Read-path call sites

Eight edit points on the read-path. All confirmed against the current
tree on 2026-04-30. Each `doc_id` reference in the table below refers
specifically to the colliding `chash_index.doc_id`, not to other
`doc_id` references in the same file.

| # | File | Line(s) | Surface | Change |
|---|------|---------|---------|--------|
| 1 | `src/nexus/db/t2/chash_index.py` | 51-57 | DDL `_CHASH_INDEX_SCHEMA_SQL` | rename column in CREATE TABLE IF NOT EXISTS |
| 2 | `src/nexus/db/t2/chash_index.py` | 95-122 | `ChashIndex.upsert` kwarg + INSERT | rename `doc_id=` kwarg to `chunk_chroma_id=`; rename column in INSERT OR REPLACE |
| 3 | `src/nexus/db/t2/chash_index.py` | 124-139 | `ChashIndex.lookup` SELECT + dict shape | rename column in SELECT; rename returned dict key `"doc_id"` to `"chunk_chroma_id"` |
| 4 | `src/nexus/db/t2/chash_index.py` | 240-261 | `ChashIndex.doc_ids_present_in_collection` | rename method to `chunk_chroma_ids_present_in_collection`; rename column in SELECT and parameter list |
| 5 | `src/nexus/db/t2/chash_index.py` | 267-311 | `dual_write_chash_index` helper | rename local variable `doc_id` to `chunk_chroma_id`; rename `chash_index.upsert(... doc_id=doc_id)` kwarg |
| 6 | `src/nexus/db/migrations.py` | 774-822 | `migrate_chash_index` (initial DDL) | rename column in the CREATE TABLE; add new migration `migrate_chash_index_rename_doc_id` after this for already-installed hosts |
| 7 | `src/nexus/catalog/catalog.py` | 1219, 1227-1262 | `resolve_chash` consumes `lookup()` rows | rename row-dict key reads from `row["doc_id"]` to `row["chunk_chroma_id"]`; ChunkRef `doc_id` field semantics carry through unchanged for back-compat (see Amendment §) |
| 8 | `src/nexus/collection_audit.py` | 388-432 | `compute_chash_coverage` calls `doc_ids_present_in_collection` | rename method invocation to `chunk_chroma_ids_present_in_collection`; rename local set `indexed_ids` is fine as-is, but variable name should be tightened to `indexed_chunk_chroma_ids` for clarity |

Indirect call sites that do **not** need column-name edits (they reach
through `Catalog.resolve_chash` which continues to surface a `doc_id`
key for back-compat per the Amendment):

- `src/nexus/commands/doc.py:334, 460, 699, 726`. Call
  `cat.resolve_chash(...)` and read `ref["doc_id"]` or `ref["chash"]`.
  These remain unchanged in Phase 0/1. RDR-101 Phase 3 may rename the
  `ref["doc_id"]` field semantics (Chroma natural ID → UUID7) under a
  separate amendment.
- `src/nexus/mcp_infra.py:702`. Calls `dual_write_chash_index(...)`
  with positional args; the helper's internal kwarg rename (#5) is
  invisible to this caller.
- `src/nexus/commands/collection.py:111, 202, 522`. Call methods on
  `db.chash_index` (`delete_collection`, `rename_collection`,
  `dual_write_chash_index`) that do not name `doc_id` directly.

Test fixtures that need follow-up edits in the same commit (not part of
the production read-path call-site count above; tracked here for
completeness):

- `tests/test_chash_index_store.py`: kwarg renames on every `upsert`
  call.
- `tests/test_resolve_chash.py`: fixture row builders use
  `doc_id="..."` kwarg; rename.
- `tests/test_collection_audit.py`: `doc_ids_present_in_collection`
  invocations.
- `tests/test_phase4_doc_commands.py`, `tests/test_phase5_doc_cite.py`,
  `tests/test_nexus_lub_collection_delete_cascade.py`,
  `tests/test_collection_rename.py`, `tests/test_collection_health.py`,
  `tests/test_backfill_hash.py`, `tests/test_migrations.py`,
  `tests/test_store_put_cli_parity.py`,
  `tests/test_abstract_themes_plan_integration.py`,
  `tests/test_document_aspects_store.py`: review for `doc_id` literal
  references that target `chash_index` specifically.

## Open question for Phase 1

The `Catalog.resolve_chash` `ChunkRef` return shape currently surfaces
both `chash` and `chunk_hash` keys (back-compat with `resolve_span`)
plus a `doc_id` key whose semantics today is the Chroma natural ID.
RDR-101 Phase 3 introduces `Document.doc_id` (UUID7). The Phase 1
implementer must decide whether `ChunkRef.doc_id` flips meaning at
Phase 3 cutover or whether a parallel `chunk_chroma_id` key is added
alongside the existing `doc_id` and the latter migrates over a
deprecation window. Recommend the parallel-key approach for the same
disambiguation reason that motivated this Phase 0 rename: namespace
collisions inside a single dict are even worse than across tables.
This is RDR-101's call to make in Phase 3, not RDR-086's, so the
Amendment block above defers it.
