---
title: "RDR-101 Phase 0: Downstream Caller Survey for Catalog Duplicated Fields"
rdr: RDR-101
bead: nexus-o6aa.5
created: 2026-04-30
author: Hal Hildebrand
phase: 0
---

# RDR-101 Phase 0: Downstream Caller Survey

## Summary

Survey of every call site that reads or writes the four duplicated catalog fields targeted by RDR-101: `entry.source_uri`, `entry.head_hash`, `entry.chunk_count`, `entry.title`.

| Field | Reads | Mutating call sites | Phase 3 migration count |
|---|---:|---:|---:|
| `source_uri` | 12 | 4 (`cat.update(source_uri=)`, plus 2 internal projector inserts, 1 normalize) | 4 |
| `head_hash` | 8 | 1 (idempotency-guard SELECT at `register():778-786`) + 2 internal projector inserts | 1 read-write hot site (the dedup guard); writes are projector-internal |
| `chunk_count` | 7 | 2 (`cat.update(chunk_count=)` in `pipeline_stages.py:519`; one operator-facing write) | 2 |
| `title` | 16 | 1 (`cat.update(title=)` from CLI batch path) + 2 internal projector inserts | 1 user surface |
| **Total** | **43** | **10 mutating sites (catalog-level), 6 internal projector writes** | **8 sites needing event-emitter migration** |

Caveat on counts: "reads" includes display, comparison, predicate, dict-roundtrip, and projector-side rebuild reads. Some sites read multiple fields on the same line (`cat.update(...)` in `commands/catalog.py:543/558` accepts any subset of the four via `**fields`). The mutating site that matters operationally is **`Catalog.register()` line 778-786 (head_hash + title idempotency guard)**, which is read-only at the API level (a SELECT) but is the load-bearing dedup gate that Phase 5 plans to drop.

Plugin/skill markdown surface: zero call sites read these fields. Only `nx/CHANGELOG.md` mentions `source_uri` in release-note prose. No plugin or agent prompt reads the fields directly.

Test surface: 30 test assertions touch `entry.{source_uri,head_hash,chunk_count,title}` across 6 files; all are read-only assertions and migrate by adopting the projection-only contract.

## Read-only call sites

Read-only here means the site never mutates the underlying value. It reads through the `CatalogEntry` dataclass returned by `Catalog.resolve()` / `Catalog.find()` / `Catalog.by_owner()`. These sites are safe through Phase 4 because the write-through cache keeps the projection populated. Phase 5 retires `head_hash` entirely (so any reader is a Phase 5 break-point), retires `chunk_count` (replaced by `COUNT(*)` over chunks), and converts file-scheme `source_uri` to projection-only.

| File:line | Field | Kind | Notes |
|---|---|---|---|
| `src/nexus/catalog/catalog.py:318` | `title` | dataclass `to_dict` | `CatalogEntry.to_dict()` self-read; consumed by JSON output paths |
| `src/nexus/catalog/catalog.py:325` | `chunk_count` | dataclass `to_dict` | same |
| `src/nexus/catalog/catalog.py:326` | `head_hash` | dataclass `to_dict` | same; Phase 5 drops the key |
| `src/nexus/catalog/catalog.py:331` | `source_uri` | dataclass `to_dict` | same |
| `src/nexus/catalog/catalog.py:1003` | `title` | alias-write rebuild | `set_alias()` rebuilds DocumentRecord from raw entry; projector-internal |
| `src/nexus/catalog/catalog.py:1010` | `chunk_count` | alias-write rebuild | same |
| `src/nexus/catalog/catalog.py:1011` | `head_hash` | alias-write rebuild | same |
| `src/nexus/catalog/catalog.py:1104` | `chunk_count` | bounds check | `resolve_chunk()` rejects chunk index >= chunk_count |
| `src/nexus/catalog/catalog.py:1110` | `title` | dict assembly | returned in `resolve_chunk()` payload |
| `src/nexus/catalog/catalog.py:1283` | `title` | update rebuild | `update()` carries existing fields through; projector-internal |
| `src/nexus/catalog/catalog.py:1290` | `chunk_count` | update rebuild | same |
| `src/nexus/catalog/catalog.py:1291` | `head_hash` | update rebuild | same |
| `src/nexus/catalog/catalog.py:1299` | `source_uri` | update rebuild | RDR-096 P3.1 carry-over comment notes the structural duplication |
| `src/nexus/catalog/catalog.py:1415` | `title` | tombstone build | `delete_document()` writes tombstone JSONL with full entry |
| `src/nexus/catalog/catalog.py:1422` | `chunk_count` | tombstone build | same |
| `src/nexus/catalog/catalog.py:1423` | `head_hash` | tombstone build | same |
| `src/nexus/catalog/catalog/__init__.py:39` | `title` | exact-match filter | `r.title == value` for catalog title resolution |
| `src/nexus/catalog/catalog_db.py:194-202,207` | `title,chunk_count,head_hash,source_uri` | rebuild INSERT | JSONL replay path; substantive critique Gap 2 item 2 (see §JSONL-replay coverage check) |
| `src/nexus/catalog/catalog_db.py:255-257` | `title,chunk_count,head_hash` | FTS5 search SELECT | Returns documents row tuple to Catalog.find()/search() |
| `src/nexus/catalog/consolidation.py:95,99` | `chunk_count` | drift check + log | Compares catalog count vs actual T3 count; logs mismatch |
| `src/nexus/collection_audit.py:267,566` | `title` | orphan list query/render | Uses raw `d.title` SELECT; OrphanChunk dataclass |
| `src/nexus/commands/catalog.py:339,424` | `title` | list/find display | Human-readable list output |
| `src/nexus/commands/catalog.py:365,370,371,374,375` | `title,source_uri,chunk_count,head_hash` | `show` display | The user-facing `nx catalog show` verb prints all four |
| `src/nexus/commands/catalog.py:581,584` | `title` | delete confirm + result | `nx catalog delete` confirmation prompt |
| `src/nexus/commands/catalog.py:655` | `title` | label fallback | `_link_label()` uses title when present |
| `src/nexus/commands/catalog.py:1686,1751` | `title` (raw `d.title`) | links display | SELECT in `links` and `session-summary` verbs |
| `src/nexus/commands/catalog.py:2046,2069` | `title` | RDR-module suggest | `suggest-implements` matches code module names against RDR titles |
| `src/nexus/commands/catalog.py:2374` | `chunk_count` | consolidate dry-run | `nx catalog consolidate --dry-run` prints projected chunk counts |
| `src/nexus/commands/enrich.py:680` | `title` | aspect file-path fallback | `e.file_path or e.title` for source_path |
| `src/nexus/commands/enrich.py:870` | `title` | aspect file-path fallback | `entry.file_path or entry.title` |
| `src/nexus/commands/enrich.py:698` (comment), `1454` | `source_uri,title` | doc/error string | Error text references entry title |
| `src/nexus/commands/enrich.py:1519` | `title` | gap-list JSON dump | `nx enrich aspects-list --missing --json` |

Total: **35 read-only call sites** across `src/nexus/`.

Disposition under RDR-101:

- All `to_dict()`/projector-rebuild sites (catalog.py 318, 325, 326, 331, 1003-1011, 1283-1299, 1415-1423; catalog_db.py 187-208) become projector-internal once Phase 3 introduces the event log; they do not need migration but their behaviour is governed by the projector contract.
- All `nx catalog show`, `list`, `delete`, `links`, `suggest-implements`, and `enrich` display sites continue to work through Phase 4; the projection still populates the columns. Phase 5 removes `head_hash` and `chunk_count` from the projection: any site that reads them must switch to `COUNT(*)` over chunks (chunk_count) or be deleted (head_hash). The `nx catalog show` Hash line is the most operator-visible site; Phase 5 must drop it or replace it with a doc_id display.
- `consolidation.py:95,99` chunk_count drift check is replaced by the doctor (deterministic event-log replay diff against actual T3 chunk-set; design §Read paths "Doctor" sequence).
- `enrich.py:680,870` use `entry.file_path or entry.title` as a fallback source-path key. After Phase 4, aspect extraction joins by `doc_id`; the title-fallback path becomes obsolete because the chroma reader no longer matches on string identity.

## Mutating call sites

These call sites either invoke `Catalog.update()` with one of the four fields as a kwarg, or perform a direct SQLite `UPDATE` / `INSERT` / `INSERT OR REPLACE`. Phase 3 migrates these to event-emitter helpers.

| File:line | Field(s) | Mutation API | Phase 3 migration target | Notes |
|---|---|---|---|---|
| `src/nexus/catalog/catalog.py:780-784` | `head_hash` (read), `title` (read) | `_db.execute(SELECT ... WHERE head_hash=? AND title=?)` | Replaced by `(coll_id, chash, doc_id)` lookup against the Chunk projection per Phase 5 plan. Phase 3 keeps the dedup logic but routes through the new event API. | **Substantive critique Gap 2 item 1, the load-bearing site.** This is technically a read, but Phase 5 retires `head_hash` so it is the single most important migration. See §Idempotency-guard analysis below. |
| `src/nexus/catalog/catalog.py:837-848` | `title,chunk_count,head_hash,source_uri` | `_db.execute("INSERT INTO documents ...")` inside `register()` | Replaced by emitting `DocumentRegistered(doc_id, owner_id, content_type, source_uri, coll_id, ts)` event; projector applies INSERT | Projector-internal after Phase 3; external callers go through the event API |
| `src/nexus/catalog/catalog.py:1329-1344` | `title,chunk_count,head_hash,source_uri` | `_db.execute("INSERT OR REPLACE INTO documents ...")` inside `update()` | Replaced by `DocumentRenamed` (for source_uri changes), no event for chunk_count (computed via COUNT), no event for head_hash (field dropped) | Projector-internal; external callers route through event-emitter helpers per Phase 3 prohibition on direct SQLite mutation |
| `src/nexus/catalog/catalog_db.py:185-209` | all four | `INSERT INTO documents (...) VALUES (...)` inside `rebuild()` | Replaced by event-log replay; rebuild becomes `replay events → project to SQLite` | **Substantive critique Gap 2 item 2.** See §JSONL-replay coverage check below |
| `src/nexus/indexer.py:322-328` | `head_hash` | `cat.update(existing.tumbler, head_hash=..., physical_collection=..., meta=..., source_mtime=...)` | `ChunkIndexed`/`DocumentRegistered` events emitted by the indexer; head_hash field is dropped by Phase 5 (idempotency moves to chash + doc_id) | The indexer's per-file hot-path; one of the two highest-volume mutation sites |
| `src/nexus/pipeline_stages.py:519-525` | `chunk_count` | `cat.update(existing.tumbler, physical_collection=..., chunk_count=..., indexed_at=..., source_mtime=...)` | Phase 5 drops chunk_count (computed via `COUNT(*)`); intermediate Phases keep it via write-through cache. Phase 3 routes through event helper. | PDF pipeline post-chunking; this is the second high-volume mutation site |
| `src/nexus/catalog/consolidation.py:86,114` | (none of the four; `physical_collection`) | `cat.update(entry.tumbler, physical_collection=...)` | `CollectionSuperseded` event in Phase 6 (per RDR §Collection naming) | Listed for completeness; this site does not mutate any of the four duplicated fields, but it is one of the operator-facing `cat.update()` callers and must be on the Phase 3 audit list |
| `src/nexus/commands/catalog.py:543,558` | `title,source_uri` (any subset) | `cat.update(t, **fields)` from `nx catalog update` CLI | `DocumentRenamed` for source_uri; `title` becomes immutable (or routed through a dedicated rename verb) | The user-facing CLI mutation surface. `--title`, `--source-uri` are accepted at lines 510-528 |
| `src/nexus/commands/catalog.py:3064,3067` | (none of the four; `file_path,meta`) | `cat.update(entry.tumbler, file_path=...)` / `cat.update(entry.tumbler, meta={"status":"missing"})` | `DocumentRenamed` (file_path is part of the same identity tuple as source_uri) | Listed for completeness; the file-path-fix sweep verb |
| `src/nexus/commands/dt.py:170-174` | `source_uri` | `cat.update(tumbler, source_uri=dt_uri, meta={"devonthink_uri": dt_uri})` | `DocumentRenamed(doc_id, new_source_uri="x-devonthink-item://...")` | Non-file-scheme source_uri stays per RDR §Phase 5; only the file-scheme persistence is retired. This site is preserved through Phase 5 |
| `src/nexus/commands/enrich.py:492-497` | (none of the four; `author,year,meta`) | `cat.update(tumbler, author=..., year=..., meta=meta_update)` | `DocumentEnriched(doc_id, model_version, payload, ts)` | Listed for completeness; bibliographic enrichment hook |
| `src/nexus/commands/doctor.py:1086` | (none of the four; `file_path`) | `cat.update(tumbler, file_path=new_rel)` | `DocumentRenamed` | Listed for completeness; doctor's rename-detection apply step |
| `src/nexus/mcp/catalog.py:301` | `title` (any subset) | `cat.update(Tumbler.parse(tumbler), **fields)` from MCP `update` tool | `DocumentRenamed` for source_uri; `title` semantics per CLI counterpart | The MCP tool counterpart of `nx catalog update`. Same fields accepted (lines 286-300) |
| `src/nexus/indexer.py:381,425` | (none of the four; `meta`) | `cat.update(entry.tumbler, meta=meta)` | `DocumentEnriched` (meta-only mutation) | Listed for completeness; housekeeping miss-count tracker |

Mutation sites that touch one of the four fields directly: **9 (catalog.py:780/837/1329; catalog_db.py:185; indexer.py:322; pipeline_stages.py:519; commands/catalog.py:543/558; commands/dt.py:170; mcp/catalog.py:301)**.

The catalog.py and catalog_db.py sites are projector-internal: Phase 3's "direct writes are prohibited" rule applies to external callers, not to the projector itself. The five external mutation sites (`indexer.py:322`, `pipeline_stages.py:519`, `commands/catalog.py:543/558`, `commands/dt.py:170`, `mcp/catalog.py:301`) are the Phase 3 migration set.

Phase 0 leaves `consolidation.py:86/114`, `commands/catalog.py:3064/3067`, `commands/enrich.py:492`, `commands/doctor.py:1086`, `indexer.py:381/425` listed but unscored on the four-field axis since they mutate orthogonal fields (`physical_collection`, `file_path`, `meta`, `author`, `year`). They still need event-emitter migration in Phase 3 because the Phase 3 prohibition is on `cat.update()` reaching SQLite directly, not on which field is mutated.

## Plugin / skill surface

Survey paths: `nx/agents/**/*.md`, `nx/commands/**/*.md`, `nx/skills/**/*.md`, `sn/hooks/**`, `~/.claude/plugins/marketplaces/nexus-plugins/**` (live cache).

| File:line | Field | Kind | Notes |
|---|---|---|---|
| `nx/CHANGELOG.md:31,35,56` | `source_uri` | release-note prose | References to the nexus-3e4s incident and the dt-stamp source_uri restoration. No code path |

**Zero plugin / skill / agent prompt files read any of the four fields directly.** The MCP `catalog.update` tool (`src/nexus/mcp/catalog.py:286-302`) is the only MCP surface that mutates them, and is captured in §Mutating call sites.

The `~/.claude/plugins/marketplaces/nexus-plugins/` mirror is a verbatim cache of the live repo (same `src/nexus/`, `tests/`, `docs/rdr/` paths). It carries the same call sites and is not a separate consumer surface; the survey treats it as out of scope for distinct counting.

This drives the Phase 4 deprecation announcement scope: **the public deprecation surface is the MCP `update` tool's `title` and (eventually) `source_uri` parameters, plus the `nx catalog update` CLI's `--title` / `--source-uri` flags.** No agent or skill prompt needs editing.

## Test surface

| File:line | Field | Kind | Notes |
|---|---|---|---|
| `tests/test_catalog.py:103` | `title` | assert | Register and resolve round-trip |
| `tests/test_catalog.py:129,142,158,177,207,258,290,305,830,992,1024,1025,1110,1154,1174` | `source_uri` | assert | RDR-096 P3.1 + nexus-3e4s register/update guard tests; Phase 5 makes file-scheme source_uri projection-only. These assertions still pass through Phase 4 (projection serves the read), but Phase 5 may force test updates if any of them mutate then re-read |
| `tests/test_catalog.py:256,676,993` | `title` | assert | Register/resolve title round-trip |
| `tests/test_catalog.py:422` | `chunk_count` | assert | Catalog chunk_count update assertion; Phase 5 replaces with COUNT(*), so the test must be updated |
| `tests/test_catalog.py:1109` | `head_hash` | assert | Idempotency guard test (the dedup gate at register():778-786). **Phase 5 break-point: this test must be replaced by a `(coll_id, chash, doc_id)` dedup assertion** |
| `tests/test_catalog_e2e.py:278` | `title` | assert | E2E register-resolve |
| `tests/test_catalog_knowledge_hook.py:38,50,114` | `title` | assert | Knowledge hook test |
| `tests/test_catalog_git.py:127` | `title` | assert | Git-context catalog hook |
| `tests/test_catalog_source_mtime.py:181` | `title` | assert | nexus-8luh source_mtime preservation test |
| `tests/test_catalog_indexer_hook.py:96,357,358` | `title,source_uri` | assert | nexus-3e4s register-time guard tests |

Total: **30 test assertions across 6 files**.

Disposition:
- 29 of 30 are read-only assertions: they exercise `cat.register()` then `cat.resolve(); assert entry.<field> == ...`. They migrate transparently through Phase 4.
- `test_catalog.py:1109` (`assert entry.head_hash == "newhash"`) is the lone test directly tied to the head_hash semantics. **This is the Phase 5 break-point.** Replacement: assert dedup behaviour through the new `(coll_id, chash, doc_id)` lookup contract.
- `test_catalog.py:422` reads `entry.chunk_count`. Phase 5 replaces the column with `COUNT(*)`, so the test must be updated to read through the COUNT projection or through a new `chunk_count_for(doc_id)` helper.

No test fixture mutates any of the four fields directly. Test mutations all go through `Catalog.register()` / `Catalog.update()` and inherit the Phase 3 migration of those APIs.

## Idempotency-guard analysis (`Catalog.register()` line 778-786)

```python
# nexus/catalog/catalog.py:776-786
# Idempotency: check by head_hash + title within same owner
# (content-addressed dedup for re-indexing the same document)
if head_hash and title:
    prefix_clause, prefix_params = self._prefix_sql(str(owner))
    row = self._db.execute(
        f"SELECT tumbler FROM documents WHERE {prefix_clause} "
        f"AND head_hash = ? AND title = ? LIMIT 1",
        (*prefix_params, head_hash, title),
    ).fetchone()
    if row:
        return Tumbler.parse(row[0])
```

This SELECT is the only place in the code where `head_hash` is **read with semantic load**. Every other read of `head_hash` is one of:

1. dataclass round-trip / `to_dict()` (lines 326, 1011, 1291, 1423)
2. JSONL rebuild INSERT (`catalog_db.py:202`)
3. SELECT-and-display in `nx catalog show` (`commands/catalog.py:375`)

Removing the dedup gate without a replacement breaks the file-already-indexed short-circuit in `Catalog.register()`. The indexer's hot path at `indexer.py:309-328` calls `register()` for every file every run; without the gate, every re-index becomes a new tumbler allocation (the only other gate is the `by_file_path()` lookup on line 772, which catches same-file-path re-indexes but **misses content-identical documents at different paths**).

Phase 5 plan per RDR-101 §Phase 5: replace the `head_hash + title` SELECT with a `(coll_id, chash, doc_id)` lookup against the Chunk projection. The semantics: a re-indexed file produces the same chash for unchanged chunks; if a chunk with `chash=X AND doc_id=this_doc` already exists in the target collection, the document is the same.

**This survey confirms the substantive critique was correct: dropping `head_hash` requires designing a replacement, not just removing the field.** The replacement is specified in RDR-101 §Phase 5 and §"Re-index unchanged file (idempotency)". The Phase 0 deliverable here is to verify the survey captures every direct read of `head_hash` so Phase 3-5 has a complete migration target list.

Reads of `head_hash` captured by the survey:

| File:line | Read |
|---|---|
| `src/nexus/catalog/catalog.py:300` | dataclass field declaration |
| `src/nexus/catalog/catalog.py:326` | `to_dict()` |
| `src/nexus/catalog/catalog.py:780-784` | **idempotency-guard SELECT, the load-bearing site** |
| `src/nexus/catalog/catalog.py:1011` | alias-write rebuild |
| `src/nexus/catalog/catalog.py:1291` | update-rebuild carry-over |
| `src/nexus/catalog/catalog.py:1423` | tombstone build |
| `src/nexus/catalog/catalog_db.py:202` | rebuild INSERT |
| `src/nexus/catalog/catalog_db.py:257` | search SELECT (FTS5 result tuple) |
| `src/nexus/commands/catalog.py:375` | `nx catalog show` display line |
| `tests/test_catalog.py:1109` | idempotency-guard assertion |

Read-of-`title` co-located with `head_hash` (the second half of the dedup tuple): `entry.title` reads at `commands/catalog.py:339, 365, 424, 581, 584, 655, 1454, 1519, 2046, 2069`, plus the catalog-internal sites listed in §Read-only. None of these depend on the dedup contract; they just display or compare the title for unrelated purposes. **The dedup contract is exclusively at `register():778-786`.** Phase 5 can drop the SELECT without re-plumbing the title-display sites.

**Risk**: the indexer (`indexer.py:309-328`) calls `register()` with `head_hash` and `meta={"content_hash": file_hash}`. The Phase 5 replacement needs the chash of at least one chunk to be known at registration time, but registration today happens before chunking in some pipelines (the markdown hook at `pipeline_stages.py:474-525` calls register after chunking; the code-indexer at `indexer.py:309-320` calls register without chunking yet). The Phase 5 dedup must either (a) defer dedup to the first `ChunkIndexed` event (pay the cost of one tumbler allocation for a re-indexed file, then merge-on-conflict in the projector) or (b) plumb a content_hash to doc_id lookup table that survives the head_hash drop. Phase 0 surface only; flagged for Phase 5 design.

## JSONL-replay coverage check (`CatalogDB.rebuild()` line 165-208)

Substantive critique Gap 2 item 2 stated: "dropped columns reappear during catalog reconstruction because `_filter_fields(DocumentRecord, obj)` re-populates every declared field. The migration breaks disaster recovery."

Verified at `src/nexus/catalog/catalog_db.py:165-209`:

```python
for tumbler, d in documents.items():
    self._conn.execute(
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler,
            d.title,
            d.author,
            d.year,
            d.content_type,
            d.file_path,
            d.corpus,
            d.physical_collection,
            d.chunk_count,    # field 9
            d.head_hash,      # field 10
            d.indexed_at,
            json.dumps(d.meta),
            d.source_mtime,
            d.alias_of,
            d.source_uri,     # field 15
        ),
    )
```

`documents: dict[str, DocumentRecord]` is constructed by the JSONL replay loop in `Catalog._load_documents()` (not in scope for this survey but called by the rebuild path). Each DocumentRecord carries every column; the rebuild's INSERT writes them all back into SQLite verbatim. Confirmed: **all four fields resurrect on rebuild from the existing JSONL log.**

RDR-101's resolution per RF-101-2 (Verified, updated post-remediation): the v: 0 projector path requires explicit sub-case handling. Per RDR §Phase 1:

> Tombstoned rows (`_deleted: True`) project as `DocumentRegistered + DocumentDeleted`; aliased rows (`alias_of != ""`) project as `DocumentRegistered + DocumentAliased`; empty-`source_uri` rows fall back to title-based matching for the `ChunkIndexed` doc_id assignment, with `_synthesized_orphan` tagging for unmatched chunks. Without these sub-cases the projector silently resurrects deleted documents and strips alias graphs.

The Phase 1 sub-cases address tombstones, aliases, and empty-`source_uri` rows. They do **not** address the question Gap 2 item 2 actually raised: **after Phase 5 drops `head_hash` from the schema, what happens when `rebuild()` reads a JSONL line written before Phase 5 that still carries `head_hash`?**

Two paths the projector could take:

1. **Strict-projection path**: the Phase 5 projector ignores fields that no longer exist in the schema. JSONL replay reads them, the projector drops them on the floor. The schema change is forward-only; downgrading reverts the field but not the data. This is consistent with RF-101-2's "v: 0 projector path": old events project under old rules, new events under new rules. Acceptable.
2. **Loose-projection path**: the Phase 5 projector still INSERTs them into a back-compat column or `meta` blob. Implicit duplication survives. **This is exactly the resurrection bug the substantive critique flagged.**

Verification status: **the RDR specifies path 1 implicitly via RF-101-2's `v: 0` dispatch but does not call out the schema-removal interaction explicitly.** Phase 1's sub-case handling addresses the `_deleted`/`alias_of`/empty-URI cases but not the dropped-column case. **This is a residual gap.** Phase 5 PR should:

- Verify the projector dispatches strictly on `(type, v)` and ignores unknown payload keys (RF-101-2 says it does, but the test gate is not specified).
- Add a test: synthesize a v: 0 JSONL line with `head_hash` populated, run the projector, assert SQLite has no `head_hash` column write.
- Document explicitly in RDR-101 Phase 5 that JSONL log entries written before Phase 5 retain `head_hash` in the on-disk format but project to nothing.

The substantive critique concern is partially resolved (Phase 1 sub-case handling) and partially live (schema-drop interaction). Flagged as residual gap below.

## Risks and gaps

1. **Phase 5 projector strict-vs-loose semantics not pinned down.** RF-101-2 says unknown `(type, v)` pairs log a warning and skip. It does not say what happens when a known `(type, v)` payload contains an extra key the new projector doesn't recognize. The substantive critique Gap 2 item 2 still has a residual edge: post-Phase-5 JSONL replay of pre-Phase-5 events. Recommend an explicit test gate in Phase 5 PR. (See §JSONL-replay coverage check.)

2. **`register()` dedup replacement timing.** The Phase 5 replacement of `head_hash + title` with `(coll_id, chash, doc_id)` requires at least one chunk's chash to be known at register time. The code-indexer (`indexer.py:309-320`) calls `register()` before chunking. The replacement either accepts a "dedup deferred until first ChunkIndexed event" semantic (with merge-on-conflict in the projector) or threads `content_hash` from `meta["content_hash"]` (which the indexer already passes through) into a new lookup table. RDR-101 §Phase 5 specifies the target shape but not the registration-ordering interaction. Flagged for Phase 5 design. (See §Idempotency-guard analysis.)

3. **`nx catalog show` displays head_hash and chunk_count.** Phase 5 retires both. The CLI must either drop the lines or replace with doc_id (head_hash) and `COUNT(*)` (chunk_count). Trivial fix; flagged for Phase 5 PR scope so it isn't forgotten.

4. **Test `test_catalog.py:1109` directly asserts `entry.head_hash`.** Phase 5 break-point. Replacement semantics: assert dedup contract through chash lookup. Phase 5 PR must include an updated dedup test.

5. **Mirror in plugins marketplace cache.** `~/.claude/plugins/marketplaces/nexus-plugins/` is a verbatim copy of the live repo. Plugin marketplace publishing (per RDR §Phase 4 telemetry) must invalidate this mirror or the deprecation telemetry will show stale reads. Out of survey scope but flagged for Phase 4 ops planning.

6. **`consolidation.py:95,99` chunk_count drift check.** Today this site logs a warning when catalog `chunk_count` and actual T3 chunk count disagree by >10%. Phase 5 makes catalog `chunk_count` a `COUNT(*)` over chunks, so the disagreement becomes structurally impossible; the warning loses semantic meaning. Phase 5 should delete the check (or replace with the doctor's deterministic diff).

7. **No evidence of plugin marketplace consumers reading the four fields directly.** The plugin/skill md surface is empty. Phase 4 deprecation telemetry can scope to the MCP `update` tool boundary plus the CLI, which significantly narrows the deprecation announcement audience. This is a green-light finding, not a gap.

8. **`CHROMA_IDENTITY_FIELD` dispatch in `aspect_readers.py` is referenced but not surveyed here.** RDR-101 §Phase 4 mentions it ("rewritten to `doc_id`-keyed dispatch"), but this survey targets catalog-side reads, not T3-side aspect-extraction joins. The aspect reader migration is its own Phase 0 deliverable (separate bead). Out of scope for this bead.

9. **The four-field axis is not exhaustive.** RDR-101 §Phase 0 also calls for a field-by-field disposition audit of `metadata_schema.py:ALLOWED_TOP_LEVEL` (~30 keys, including `frecency_score`, `tags`, `category`, `chunk_index`, `chunk_count`, `corpus`, `store_type`, `ttl_days`, `expires_at`, `section_type`, `section_title`, `embedded_at`, `git_*`, `bib_semantic_scholar_id`). That audit is a separate Phase 0 deliverable and is not covered by this survey. Out of scope.
