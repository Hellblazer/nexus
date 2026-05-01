# RDR-101 Phase 0: Direct T3 Metadata Access Survey

Bead: nexus-o6aa.6

## Summary

Direct ChromaDB `col.get(...)` / `col.query(...)` calls in production source: 27 distinct call sites across 12 modules. Of those, 17 sites read at least one Phase-5 deletion-list field (`source_path`, `title`, `git_*`, `corpus`, `store_type`, `category`, `tags`, `frecency_score`, `expires_at`, `ttl_days`, `chunk_count`, `section_type`, `section_title`, `embedded_at`) either as a `where=` predicate or via `meta.get(...)` on a returned page.

Breakdown by Phase-5-deletion-list field (production reads, in-repo only):

| Field | call sites | callers (file:line, abridged) |
|---|---|---|
| `source_path` (read or `where=`) | 16 | indexer.py:951,960,988; doc_indexer.py:425,484,622,904,943,1049; db/t3.py:953,977,1023; aspect_extractor.py:772; aspect_readers.py:268 (via dispatch); commands/catalog.py:2230,2535; commands/enrich.py:120,203; mcp/core.py:790,807; formatters.py:161,253,284,334,370; scoring.py:223,257; commands/search_cmd.py:328,380,390,421 |
| `title` (read or `where=`) | 8 | db/t3.py:1100; aspect_readers.py (`knowledge__` fallback); commands/catalog.py:2232,2331; commands/enrich.py:116; mcp/core.py:789,795; commands/index.py:712,674; search_clusterer.py:91 |
| `git_*` | 0 in production hot path; tests only (test_pdf_subsystem.py:292-293) | none in `col.get/query` predicates; only written, never read by `where=` |
| `corpus`, `store_type`, `category`, `tags` | 0 production reads; all writes only | none |
| `frecency_score` | 2 | scoring.py:107,119 |
| `chunk_count` | 2 | mcp/core.py:803; scoring.py:125 |
| `expires_at` / `ttl_days` | 1 (`ttl_days` in `where=`) | db/t3.py:785 (the TTL-expiry sweep); plus `metadata_schema.is_expired` Python-side fold |
| `section_type` / `section_title` | 1 | aspect_extractor.py:785; doc_indexer.py:736-737, 816-817 (write-only) |

In-repo sites split into three groups:
- **Hot path** (would silently return empty under Phase 5 without rewrite): `aspect_readers.py:_read_chroma_uri` + `aspect_extractor._source_content_from_t3` (the original ART-lhk1 failure surface), `doc_indexer.py` staleness-and-prune block (4 instances), `indexer.py:_prune_misclassified` and `_prune_deleted_files`, `db/t3.py:list_unique_source_paths` / `ids_for_source` / `delete_by_source` / `update_source_path`.
- **Backfill / recovery** (one-shot operator paths, not in steady-state RPS): `commands/catalog.py:_backfill_rdrs` / `_backfill_papers` / `_recover_files_for_collection`, `commands/enrich.py:enrich_aspects`.
- **Display / scoring** (reads chunk metadata returned by a search, not via direct `col.get`): `mcp/core.py:_dedup_by_doc_key`, `formatters.py`, `scoring.py`. These read fields off `r.metadata` after a search call, not via raw `col.get`; they still depend on Phase-5 fields surviving the schema flip.

Plugin / skill marketplace surface that documents these fields (in-repo `nx/` and external `~/.claude/plugins/marketplaces/nexus-plugins/`): 5 documentation references in `nx/CHANGELOG.md`, `nx/agents/_shared/CONTEXT_PROTOCOL.md`, `nx/hooks/scripts/session_start_hook.py`, plus the external `nexus-plugins/AGENTS.md` identity-field table. None of these are tools that bypass the catalog at runtime; they are doc strings that promise the fields exist.

Out-of-scope T1 reads (session scratch): `db/t1.py` and `plans/session_cache.py` carry their own per-session metadata schema. RDR-101 §"Out of Scope" excludes T1; these are listed for completeness only.

## In-repo direct reads

Every site below is in the `src/nexus/` tree. "Field accessed" lists what the call uses on the response. "On Phase 5 list?" maps to the deletion list in RDR-101 §Phase 5. "Phase 4 migration target" is the catalog projection method (or `doc_id`-keyed `get`) that should replace the direct read.

| File:line | Field accessed | On Phase 5 list? | Phase 4 migration target |
|---|---|---|---|
| `aspect_readers.py:267-272` (the `_read_chroma_uri` `where=` loop) | `source_path` (rdr/docs/code) or `title` (knowledge fallback), via `_identity_fields_for(collection)` | Yes (both) | Rewrite `CHROMA_IDENTITY_FIELD` to `("doc_id",)`; `extract_aspects` accepts a `doc_id_lookup` callable that resolves the chroma URI's source-id segment to a `doc_id` via the catalog projection. **Substantive critique C2 highlighted this as the structural reproduction of the ART-lhk1 failure if not rewritten.** |
| `aspect_extractor.py:771-789` (`_source_content_from_t3`) | `where={"source_path": source_path}`; reads `chunk_index`, `section_type` from each chunk metadata | source_path: yes; chunk_index: stays on chunk; section_type: yes (Phase 5 list) | `where={"doc_id": doc_id}`; chunk_index stays intrinsic; section_type moves to a `Provenance` projection (or stays if Phase 0 audit reclassifies it as intrinsic). Function is already marked deprecated by RDR-096; preferred path is `aspect_readers.read_source` with chroma URI; that reader is the C2 site above. |
| `db/t3.py:918-959` (`list_unique_source_paths`) | iterates all metadatas, reads `source_path` | Yes | Replace with catalog projection: `SELECT DISTINCT source_uri FROM documents WHERE coll_id = ?`. Operator script `nx t3 prune-stale` (`commands/t3.py:9`) is the caller; it migrates to the catalog projection. |
| `db/t3.py:961-987` (`ids_for_source`) | `where={"source_path": source_path}` | Yes | Catalog: `SELECT chunk_id FROM chunks JOIN documents USING (doc_id) WHERE source_uri = ?`. Or with surrogate identity: `chunks WHERE doc_id = catalog.resolve_uri(source_uri)`. |
| `db/t3.py:989-1002` (`delete_by_source`) | calls `ids_for_source` then `delete(ids=...)` | Yes (transitively) | Same as above. RF-101-3 already calls this out as `delete_by_chunk_ids` in the new gc verb. |
| `db/t3.py:1004-1048` (`update_source_path`) | `where={"source_path": old_path}` then mutates `source_path` field on each chunk | Yes | Replaced by `DocumentRenamed` event; T3 chunks are unchanged after Phase 5 (no `source_path` to rewrite). The whole method goes away. |
| `db/t3.py:1084-1110` (`find_ids_by_title`) | `where={"title": title}` | Yes | Catalog: `SELECT chunk_id FROM chunks JOIN documents USING (doc_id) WHERE documents.title = ?`. Method has only one caller (`enrich.py:_resolve_bib_for_title`) which already has the title in hand and could pass `doc_id` instead. |
| `doc_indexer.py:423-428` (markdown staleness check) | `where={"source_path": sp}`, reads stored `content_hash` and `embedding_model` | source_path: yes; content_hash + embedding_model: stay on chunk (intrinsic) | `existing = catalog.resolve_uri(source_uri)` → if not None, fetch a single representative chunk by `doc_id` for the hash check. Or: compute idempotency from the catalog only (catalog stores `source_mtime` + `content_hash` per RDR-096) and only enter T3 to read the `embedding_model` field. |
| `doc_indexer.py:481-493` (markdown stale-prune) | `where={"source_path": sp}` | Yes | `where={"doc_id": doc_id}`. doc_id resolved once at entry to `_index_document`. |
| `doc_indexer.py:619-631` (PDF incremental stale-prune) | `where={"source_path": str(file_path)}` | Yes | Same as above. |
| `doc_indexer.py:902-907` (PDF staleness check) | `where={"source_path": str(pdf_path)}`, reads `content_hash` and `embedding_model` | source_path: yes; others stay | Same migration pattern as `doc_indexer.py:423-428`. |
| `doc_indexer.py:941-957` (streaming-PDF metadata fetch for return value) | `where={"source_path": str(pdf_path)}`, reads `page_number`, `title`, `source_author` | source_path: yes; title: yes; page_number: stays on chunk; source_author: stays on chunk (or moves to `Document` projection; Phase 0 audit decides) | `where={"doc_id": doc_id}` for the chunk fetch; title and source_author come from the catalog projection (`SELECT title, source_author FROM documents WHERE doc_id = ?`). |
| `doc_indexer.py:1047-1058` (PDF small-doc stale-prune) | `where={"source_path": str(pdf_path)}` | Yes | `where={"doc_id": doc_id}`. |
| `indexer.py:951-955` (`_prune_misclassified` code→docs) | `where={"source_path": source_path}` | Yes | `where={"doc_id": doc_id}` per file; doc_id resolved from catalog. |
| `indexer.py:960-964` (`_prune_misclassified` docs→code) | same | Yes | same |
| `indexer.py:981-995` (`_prune_deleted_files`) | iterates all metadatas, reads `source_path` | Yes | Replace with catalog projection: walk `documents WHERE owner_id = ?`, filter to those whose `source_uri` does not exist on disk, then `DocumentDeleted` event each. |
| `db/t3.py:80-152` (`_rewrite_collection_metadata`) | `where={"source_path": source_path}` (optional filter) | Yes | One-shot migration helper. After Phase 5 ships, this whole function becomes pre-Phase-5 dead code. |
| `db/t3.py:780-823` (`expire_ttl_entries`) | `where={"ttl_days": {"$gt": 0}}`, then `is_expired(meta)` Python-side | ttl_days: Phase 5 deletion list; expires_at: already removed | Move expiry to a `Frecency` projection in T2 (Phase 0 audit calls this out). The T3 sweep stays on `chunk_id` lookups by content; the expiry decision moves catalog-side. |
| `aspect_extractor.py:771-789` | `chunk_index`, `section_type` reads (in addition to `source_path` filter above) | section_type: yes | `chunk_index` is intrinsic; `section_type` moves to a `Provenance` or `ChunkAttributes` projection. |
| `commands/catalog.py:2227-2236` (`_backfill_rdrs`) | iterates pages, reads `source_path` and `title` per chunk metadata | Yes (both) | One-shot recovery operator. Phase 1 of RDR-101 (event-log synthesis) replaces this entirely; the synthesis walk reads catalog rows, not T3 chunks. Until Phase 1 lands, leaves it as-is. |
| `commands/catalog.py:2326-2333` (`_backfill_papers`) | reads first chunk's `title`, `bib_authors`, `bib_year` to seed paper metadata | title: yes; bib_*: stay on chunk (or move to Document projection per Phase 0 audit) | Same as above. |
| `commands/catalog.py:2521-2540` (`_recover_files_for_collection`) | iterates pages, reads `source_path` to deduplicate | Yes | Same as above. |
| `commands/enrich.py:103-125` (the title→chunks index pass) | iterates pages, reads `id_field` (`bib_semantic_scholar_id`), `title`, `source_path` | source_path: yes; title: yes; bib_semantic_scholar_id: Phase 0 audit must classify; the RDR explicitly calls this out as load-bearing for "this title was already enriched" | After Phase 4: index against the catalog (`SELECT doc_id, title, source_uri FROM documents`); the merge/update of bib fields stays per-chunk T3 writes. The `bib_semantic_scholar_id` skip-marker is the Phase 0 disposition decision; moves to `Document.bib_*` columns OR stays on chunk. |
| `commands/enrich.py:181-205` (per-batch chunk fetch + merge) | `col.get(ids=batch_ids)`, reads existing metadata to merge | reads `source_path` from each meta to populate `source_paths` set | After Phase 4: source_paths come from the catalog projection; chunk fetch stays by `chunk_id` (no field rename needed). |
| `commands/collection.py:58-65` (`info_cmd`) | iterates all metadatas, reads `indexed_at` | indexed_at: stays on chunk per RDR-101 entity table | No migration needed; `indexed_at` is intrinsic per-chunk; the column survives Phase 5. |
| `commands/collection.py:262-272` (`reindex_cmd` source_path scan) | iterates all metadatas, reads `source_path` | Yes | After Phase 4: read source paths from catalog `documents` table. |
| `commands/collection.py:472-528` (`backfill_chunk_text_hash`) | iterates pages, reads `chunk_text_hash` | chunk_text_hash: intrinsic (RDR-086) | No migration; chunk_text_hash stays per-chunk. |
| `commands/index.py:660-689` (`pdf` command's preview-only branch) | reads `documents` + `metadatas` from ephemeral collection, displays `title`, `source_author`, `page_number` | title: yes; source_author: see Phase 0 audit | After Phase 4: title from catalog; source_author from catalog (or Document projection). |
| `mcp/core.py:783-816` (`_dedup_by_doc_key` in search/query) | reads `content_hash`, `title`, `source_path`, `chunk_count`, `bib_*` from `r.metadata` | source_path: yes; title: yes; chunk_count: yes; bib_*: see Phase 0 | All these fields are read from `SearchResult.metadata`; populated by the chroma query. After Phase 5 the query returns chunks with only intrinsic per-chunk fields; the dedup pass migrates to read these from a catalog batch lookup keyed on `doc_id`. |
| `formatters.py:161,253,284,334,370` (search-result formatters) | `r.metadata.get("source_path"/"file_path"/"title")` | Yes (source_path, title) | Same migration: search response either auto-joins these via the catalog batch lookup, OR formatters take a `doc_lookup` argument. |
| `scoring.py:107,119,125,223,257` | `r.metadata.get("frecency_score", "chunk_count", "source_path")` | Yes (all three) | frecency_score moves to a T2 `Frecency` projection (Phase 0 audit). chunk_count moves to a `COUNT` over the Chunk projection. source_path moves to the catalog. |
| `commands/search_cmd.py:328,380,390,421` | `r.metadata.get("source_path")` for filter + display | Yes | Same as `formatters.py`. |
| `catalog/catalog.py:194-198` (chash fallback) | `where={"chunk_text_hash": hex_chash}` then reads `ids[0]` | chunk_text_hash: intrinsic | No migration. |
| `catalog/catalog.py:1133-1149` (`fetch_chunk_text_for_chash`) | `where={"chunk_text_hash": chunk_hash}`, returns metadata blob to caller | chunk_text_hash: intrinsic; metadata blob returned: depends on caller | No migration for the lookup; callers that consume the returned `metadata` must migrate per their field reads. |
| `catalog/catalog.py:2079-2087` (chash-positional-span resolver) | `where={"chunk_index": ..., "source_path": entry.file_path}` | source_path: yes | After Phase 4: `where={"chunk_index": ..., "doc_id": entry.doc_id}`. |
| `catalog/catalog.py:2180-2188` (`link_audit` chash verification) | `where={"chunk_text_hash": chunk_hash}` | chunk_text_hash: intrinsic | No migration. |
| `catalog/consolidation.py:67-80` | full-page iteration, no field-specific reads | None | No migration. Pure chunk re-upsert. |
| `health.py:680` (pagination audit) | `col.get(limit=page_size, offset=offset, include=[])` | None (just counts) | No migration. |
| `collection_audit.py:165` (live-distance probe) | `col.get(limit=n, include=["embeddings"])` | None | No migration. |
| `collection_audit.py:187-189` (per-embedding `query`) | `col.query(query_embeddings=..., n_results=2)` | None | No migration. |
| `collection_audit.py:421-431` (chash coverage sample) | `col.get(limit=300, include=["metadatas"])` then samples ids | reads `chunk_text_hash` indirectly via the chash_index | No migration. |
| `exporter.py:172-188` | `col.get(include=["documents", "metadatas", "embeddings"])`, reads `source_path` for filter | Yes | After Phase 4: take a `doc_lookup` callable; filter by `source_uri` looked up via catalog projection (or accept `--doc-id` filter directly). |
| `exporter.py:360-407` (import side) | reads/rewrites `source_path` in incoming metadata | Yes | After Phase 5 stops writing `source_path` to T3, this side becomes legacy-import-only: reads source_path from old export bundles, writes catalog `DocumentRegistered` events instead of T3 metadata. |

Notes:
1. T1 reads (`db/t1.py`, `plans/session_cache.py`) are excluded; RDR-101 §"Out of Scope" says T1 is not in scope.
2. `pdf_extractor.py:911` reads `doc_meta.get("title")` from PyMuPDF document properties at extraction time (before any T3 write). This is not a T3 read; it is a PDF metadata read. Excluded.
3. `search_clusterer.py:91` reads `meta.get("title")` from a passed-in `best_result` dict already populated upstream by a search call. Coverage flows through whichever upstream caller built the result; no new direct read.
4. Many sites listed under `mcp/core.py`, `formatters.py`, `scoring.py`, and `commands/search_cmd.py` are NOT raw `col.get` calls; they read off a `SearchResult` object whose metadata was populated by a `col.query` upstream. The migration target is the same: after Phase 4 the query path either auto-joins the catalog or returns only intrinsic chunk fields and the consumer joins. Listing them here keeps the inventory honest about the read surface.

## Plugin / skill marketplace surface

The marketplace surface is documentation that promises specific T3 metadata field names exist. None of these are runtime tools that bypass the catalog; they are doc references that downstream plugin authors (and operators reading the docs) will read literally.

| File:line | Field documented | Repo (in-repo / external) | Deprecation announcement scope |
|---|---|---|---|
| `nx/CHANGELOG.md:91, 270-271, 279, 935, 978, 1184, 1853, 1924, 1935-1936, 1951, 2035, 2039, 2109` | `source_path`, `chunk_text_hash`, `chunk_count`, `head_hash`, `git_project_name`/`git_branch`/`git_commit_hash`/`git_remote_url`, `section_type`, `section_title`, `expires_at`, `ttl_days`, `corpus`, `store_type` | in-repo (`nx/`) | Phase 4 PR updates the changelog forward; Phase 5 PR adds an entry naming each removed field. |
| `nx/agents/_shared/CONTEXT_PROTOCOL.md:104` | `chunk_text_hash` | in-repo | No change; `chunk_text_hash` is intrinsic and stays. |
| `nx/agents/_shared/CONTEXT_PROTOCOL.md:224` | `category`, `tags` (as `store_put` args) | in-repo | Phase 5 PR rewrites the example; `store_put` API stops accepting `category`/`tags` (RDR-101 §Phase 5). |
| `nx/hooks/scripts/session_start_hook.py:110` | `section_type` (in `where=` example) | in-repo | Phase 5 PR rewrites the line after the Phase 0 audit decides whether `section_type` is intrinsic-on-chunk or moves to a projection. |
| `nx/hooks/scripts/subagent-start.sh:122, 196` | `section_type`, `chunk_text_hash` | in-repo | Same. |
| `nx/skills/nexus/reference.md:36` | `section_type` (search example) | in-repo | Same. |
| `~/.claude/plugins/marketplaces/nexus-plugins/AGENTS.md:31-33` | `source_path`, `title` (identity-field table per collection prefix) | external (synced from this repo to the plugin marketplace) | **Highest blast radius.** This is the table plugin authors will copy verbatim into their own integrations. RF-101-5 calls out 6-month minimum wall-clock deprecation window for exactly this surface. The Phase 4 deprecation announcement re-publishes this table with `doc_id` as the only identity field. |
| `~/.claude/plugins/marketplaces/nexus-plugins/CHANGELOG.md` (multiple lines, ~17 mentions) | `source_path`, `chunk_text_hash`, `chunk_count`, `head_hash`, `git_*`, `section_type`, `section_title`, `expires_at`, `ttl_days`, `corpus`, `store_type` | external (mirrored) | Phase 5 release notes published to the marketplace registry. |
| `~/.claude/plugins/marketplaces/nexus-plugins/tests/test_*.py` (12+ files) | `source_path`, `chunk_text_hash`, `title`, `git_*` | external; these are tests in the marketplace plugin against the public T3 metadata schema. **Marketplace tests directly read fields the schema is about to drop.** | Phase 5 PR migrates the marketplace tests in lockstep. |
| `~/.claude/plugins/marketplaces/nexus-plugins/bench/queries/spike_5q.yaml` | `category` (eval bench taxonomy, not T3 metadata) | external | Likely unrelated; `category` here is a benchmark dimension, not the T3 metadata field. Confirm during Phase 5 audit. |

External doc references in `~/.claude/plugins/marketplaces/nexus-plugins/` are mirrored from this repo at marketplace publication time. The deprecation announcement is one PR + one changelog entry that propagates to both surfaces.

## Test direct reads

Tests bypass the catalog more aggressively than production code does; many setups manually populate T3 with `{"source_path": ...}` or `{"title": ...}` to drive specific code paths. Phase 5 default-on cannot land while these tests assume the field schema.

| File:line | Fields read | Fix-on-Phase-5 plan |
|---|---|---|
| `tests/test_exporter.py:125, 130, 134, 200, 205` | `m["source_path"]` per metadata | Migrate to `doc_id`-keyed export; tests assert `doc_id` survives round-trip. |
| `tests/test_p0_regressions.py:119, 219-237` | `meta.get("source_path")` (and a `mock_get` that simulates `where={source_path: X}`) | Migrate to mock that responds to `where={doc_id: X}`. |
| `tests/test_doc_indexer.py:134, 195, 278, 421, 460, 500, 706, 729-739, 929, 967, 1047, 1077, 1158, 1199, 1270` | many `mock_col.get.return_value = {"metadatas": [...]}` with `source_path`/`title`/`content_hash` | Update mocks to return `doc_id` + intrinsic chunk fields; staleness checks read catalog-side. |
| `tests/test_doc_indexer_pagination.py:73, 89, 145, 181` | tests `col.get(where={"source_path": ...})` pagination | Replace `source_path` with `doc_id` in the where filter. |
| `tests/test_doc_indexer_hash_sync.py:45-127, 154-234` | `mock_col.get.return_value = {"metadatas": [{"content_hash": ..., "embedding_model": ...}]}` | content_hash + embedding_model stay on chunk; tests still pass; just need the test's `where` to use `doc_id`. |
| `tests/test_indexer.py:54, 190, 205, 221, 340, 366, 495, 505, 514` | mock `col.get.return_value` with `source_path`-keyed data | Update mocks to `doc_id`-keyed. |
| `tests/test_indexer_e2e.py:153, 287, 290, 320, 350, 355` | `m.get("source_path")`, `m.get("embedding_model")`, etc | Assert `doc_id` membership; assert embedding_model survives. |
| `tests/test_catalog_backfill.py:66, 217, 394, 440` | `mock_col.get.return_value = {"metadatas": [{"title": "test doc"}]}` | Backfill is a one-shot operator; Phase 1 of RDR-101 replaces this entire flow. Tests delete after Phase 1 lands. |
| `tests/test_catalog_path.py:262, 276` | inspects `mock_col.get.call_args` to verify staleness check shape | Update assertion to expect `where={doc_id: X}`. |
| `tests/test_catalog_e2e.py:341, 348-362` | reads `chunk["metadatas"][0]["chunk_text_hash"]` and `content_hash` | Both intrinsic; no migration. |
| `tests/test_t3_prune_stale.py:178` | `surviving = col.get()` (no where filter) | Migration only if asserting on `source_path`-keyed iteration; check. |
| `tests/test_collection_cmd.py:71, 199, 211, 237` | mocks `col.get.return_value` with `source_path` in metadatas | Update to `doc_id`. |
| `tests/test_voyage_retry.py:293`, `tests/test_indexer_modules.py:57, 66, 155` | empty `col.get.return_value`; no field-specific assertions | No migration needed. |
| `tests/test_collection_audit.py:272` | `col.query` for distance probe; no field-specific reads | No migration. |
| `tests/test_chroma_retry.py:108, 131, 145, 154` | retry behavior; mock returns shape-only | No migration. |
| `tests/test_pdf_subsystem.py:165, 190, 233, 292-293` | `col.get.return_value = {...}` with `git_*` reads at 292-293 | Phase 5 PR drops the `git_*` assertion (or migrates to `Provenance` projection assertion). |
| `tests/test_enrich_command.py:17, 34, 72-` | mocks chunk pages with `title` + `bib_*` fields | Update mocks once `enrich.py` migrates per the production migration target. |
| `tests/test_md_chunker.py:65, 305` | asserts `c.metadata["source_path"] == "/my/doc.md"`, `c.metadata.get("section_type") == ""` | These are about the chunker's local-only metadata before any T3 write; not a T3 read, but the chunker's contract changes if `source_path` is no longer a chunk-write field. Phase 5 PR updates the chunker's metadata contract. |
| `tests/test_search_cmd.py:459` | `r.metadata.get("source_path", "?")` | Test asserts on search result formatting; migration aligns with `formatters.py` migration. |
| `tests/test_enrich_aspects.py:364, 433` | comments referring to `where={source_path: ...}` | Comment + assertion updates only. |
| `tests/test_scratch.py:391, 400, 409, 416, 423, 435, 455` | `t1._col.get(ids=[doc_id], include=["metadatas"])` | T1; out of scope. |

Test direct-read summary: ~22 test files touch deprecated metadata fields. The mocks bypass the catalog entirely; they fabricate T3 chunk metadata to exercise specific code paths. Each Phase 4/5 production migration needs a paired test mock update.

## `aspect_readers.py` `CHROMA_IDENTITY_FIELD` dispatch

This subsection is the most important single observation in the survey, and it is exactly what RDR-101's substantive critique C2 flagged.

The current dispatch at `src/nexus/aspect_readers.py:151-156` is:

```
CHROMA_IDENTITY_FIELD: dict[str, tuple[str, ...]] = {
    "rdr__":       ("source_path",),
    "docs__":      ("source_path",),
    "code__":      ("source_path",),
    "knowledge__": ("source_path", "title"),
}
```

`_identity_fields_for(collection)` (line 159-168) returns these tuples; `_read_chroma_uri` (line 207-310) iterates them and issues `coll.get(where={identity_field: source_id}, ...)` against ChromaDB. The dispatch is the only thing standing between an aspect-extraction call and a `(empty)` skip.

If Phase 5 lands without rewriting this dispatch, the result is structurally identical to the original ART-lhk1 failure: the `where=` predicate names a field that no longer exists in T3 metadata, ChromaDB returns zero rows for every document, and the aspect extractor reports `N/N skipped (empty)` for every paper in every collection.

The Phase 4 rewrite per RDR-101 §Phase 4:

```
# After Phase 4; uniform doc_id dispatch.
def _identity_fields_for(collection: str) -> tuple[str, ...]:
    return ("doc_id",)
```

Plus a plumbing change in `extract_aspects` to accept a `doc_id_lookup: Callable[[str, str], str]` (collection, source-id-segment) → `doc_id`. The chroma URI shape stays `chroma://<collection>/<source-id>`, but the `<source-id>` segment is resolved to a `doc_id` via the catalog at read time, and the `where=` predicate is `{"doc_id": doc_id}`.

This is one source-file change in `aspect_readers.py` plus one wiring change in `extract_aspects` plus mock updates in `tests/test_enrich_aspects.py`. The blast radius is small; the consequence of skipping it is the entire aspect-extraction pipeline returning zero results.

Phase 4 test gate per RDR §Phase 4: `nx enrich aspects <coll> --dry-run` reports zero `(empty)` skips on a collection where every document has at least one chunk. Adopt this as the Phase 4 PR's CI gate.

## Telemetry recommendation

**Recommendation: Option Y (per-call-site decoration), implemented as a thin context manager around each direct-read site, with the counter named `direct_t3_metadata_read_total{field, call_site}`.**

Reasoning, weighing both options:

Option X (chromadb-wrapper boundary) is appealing for its simplicity: every `_chroma_with_retry(col.get, ...)` would increment a coarse counter. The major drawback is that the wrapper sees the call but cannot tell which Phase-5 deletion-list field is being read. The `where=` predicate's keys are observable, but the response-side reads (`meta.get("source_path", "")` after the page returns) are not; and those are the more numerous and harder-to-find sites. A wrapper-only counter would over-count chash and chunk_id reads (which are not on the deletion list and never go to zero) and under-count the post-fetch reads. The Phase 5 flip-default decision (RF-101-5: "30+ contiguous days of zero direct-T3-metadata reads") needs field granularity to be meaningful; a coarse counter cannot answer it.

Option Y (per-call-site) requires adding ~30 decorations to the codebase, but each one is a single line and the PR is mechanical. The counter knows exactly which field is being read because the wrapper at the call site names it. The field tag `{field=source_path}` answers the Phase 5 gating question directly: when `direct_t3_metadata_read_total{field="source_path"}` stays at zero for 30 contiguous days while `catalog_projection_read_total{field="source_uri"}` rises, the Phase 5 default-flip is data-driven.

Concrete implementation:

1. Add `src/nexus/telemetry/t3_reads.py` with two helpers:
   - `record_direct_read(field: str, call_site: str)`; increments `direct_t3_metadata_read_total{field, call_site}`.
   - `record_projection_read(field: str)`; increments `catalog_projection_read_total{field}`.
2. At every site listed in §"In-repo direct reads" that touches a Phase-5-deletion-list field, decorate with `record_direct_read(field=..., call_site=__file__ + ":" + line)` immediately after the read. The site list above is the work breakdown for that PR.
3. The catalog's projection-read methods (the new methods that replace direct reads in Phase 4) call `record_projection_read(field=...)` once per call.
4. Counter sink: T2 SQLite table `telemetry_counters(name, labels_json, value, ts)` with periodic flush from in-memory aggregator. Operator dashboard: `nx telemetry t3-reads --window=30d` reports the per-field counts.
5. Phase 5 gate: a dedicated CI / pre-flip command `nx catalog phase5-readiness` checks `MAX_OVER_30_DAYS(direct_t3_metadata_read_total{field IN (...)})  == 0` for the target field set (`source_path`, `title`, `git_project_name`, `git_branch`, `git_commit_hash`, `git_remote_url`, `git_meta`, `corpus`, `store_type`, `category`, `tags`, `frecency_score`, `expires_at`, `ttl_days`, `chunk_count`, `section_type`, `section_title`, `embedded_at`).

The telemetry counter should be added in the **same PR as Phase 4's reader migration**. Not earlier (the rate before Phase 4 is whatever the production rate is, which is the wrong baseline; the rate after Phase 4 is what the gate measures). Not later (Phase 5 cannot ship without 30 days of post-Phase-4 telemetry data).

Field set tracked by the counter: every key on the Phase 5 deletion list. The Phase 0 audit may add or remove fields; the counter set follows the audit.

## Risks and gaps

1. **Not all reads come from `col.get` / `col.query`.** A large fraction of the deprecated-field reads go through the search pipeline: `mcp/core.py`, `formatters.py`, `scoring.py`, `commands/search_cmd.py` all read `r.metadata.get("source_path"/...)` from `SearchResult` objects produced by an upstream `col.query`. These are not direct `chromadb.get/query` calls but they still depend on Phase-5 fields. Decoration must extend to every `r.metadata.get(<field>, ...)` site that touches the deletion list. Expanding the regex from "calls to col.get/query" to "reads of metadata-dict keys whose name is in the Phase-5 list" produces the larger set listed above.
2. **`bib_*` fields are not on the Phase 5 deletion list but the survey found them in identical call patterns.** RDR-101 Phase 0 explicitly calls out `bib_semantic_scholar_id` as a Phase 0 disposition decision. Until that decision is made, telemetry should track it too, since it controls `commands/enrich.py`'s skip-already-enriched logic.
3. **External marketplace tests bypass the migration plan.** `~/.claude/plugins/marketplaces/nexus-plugins/tests/` carries 12+ test files that assert directly on `source_path` and `title` in chunk metadata. The marketplace plugin sync from this repo includes those tests. Phase 5 ships a marketplace-plugin sync PR that updates the tests in lockstep; otherwise the marketplace's own CI breaks the day Phase 5 lands. List for that sync PR is in the "external" rows of §"Plugin / skill marketplace surface".
4. **T1 was scoped out, but `plans/session_cache.py` calls `col.get(where={"session_id": ...})`.** `session_id` is on the Phase 5 deletion list. RDR-101 §"Out of Scope" excludes T1, but `session_id` is a per-T1-row field, and the session-cache layer is T1, so the exclusion holds. Confirm explicitly in the Phase 0 audit.
5. **`exporter.py` round-trips the deprecated fields via msgpack export bundles.** Old export bundles (`.nxa` files in the wild) carry `source_path` + `title` + `git_*` per record. After Phase 5 the import side has to translate: detect old-shape bundles, re-derive `doc_id` (or assign new ones), emit `DocumentRegistered` events. The exporter doesn't read the catalog today; it iterates `col.get`. Phase 4 PR updates the exporter to take a `doc_lookup` callable; Phase 5 PR updates the importer to translate.
6. **The doctor verb uses `col.get(include=[])` for chunk-id enumeration.** This is field-free and survives Phase 5. But the doctor today computes drift over `source_path` strings; greenfield doctor computes drift over `doc_id` chunk-id sets (RDR §Sequence: Doctor). The doctor rewrite is in Phase 6 of the RDR plan, after Phase 5 lands.
7. **`db/t3.py:expire_ttl_entries` reads `where={"ttl_days": {"$gt": 0}}` and then folds to `is_expired(meta)` Python-side.** Phase 5 removes `ttl_days` from T3 metadata; the expiry decision migrates to a T2 `Frecency` projection per the Phase 0 audit hint in the RDR. This is a non-trivial migration because the T3 sweep currently scans every collection for expired chunks. Migration: the T2 frecency projection holds `(chunk_id, expires_at, ttl_days)` and the sweep becomes a `SELECT chunk_id FROM frecency WHERE expires_at < ?`, then `T3.delete_by_chunk_ids(...)`. Phase 0 audit must confirm this is in scope; if it is not, `ttl_days` stays on chunk and only the migration table changes.
8. **No `direct_t3_metadata_read_total` counter exists today.** Bootstrapping it is part of Phase 4 work; the survey is written assuming the counter ships in Phase 4. If Phase 4 is split into sub-phases, the counter goes in the same sub-phase as the first reader migration so the migrated and unmigrated reads can be compared.
