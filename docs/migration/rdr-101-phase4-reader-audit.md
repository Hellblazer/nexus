# RDR-101 Phase 4 — Legacy-Key Reader Audit

**Bead:** `nexus-o6aa.10.2`
**Goal:** Catalogue every code path that reads any of the six T3 metadata keys
slated for prune in Phase 4, and decide each key's disposition (migrate /
keep / drop with no migration).

**Six candidate keys** (from `nexus-o6aa.10` description):
`source_path`, `title`, `git_branch`, `git_commit_hash`, `git_project_name`,
`git_remote_url`.

## Headline findings

1. **The four `git_*` flat keys have zero readers in `src/`.** Every reference
   is a writer that emits the key as input to `normalize()`, which then
   consolidates them into the `git_meta` JSON blob (`metadata_schema.py:196–
   203`). They persist on legacy chunks only because the consolidation post-
   dates the original writes. **Disposition: drop, no migration work.**

2. **`source_path` is widely read across ~20 source files** in five distinct
   roles (chunk identity, where-filter for chunk lookup, search-result
   display, catalog prefilter, synthesis/backfill). The schema docstring at
   `metadata_schema.py:118–119` already documents the replacement plan:
   *"Phase 5b plans to drop legacy `source_path` in favour of `source_uri`
   (RDR-096 P5.1/P5.2)"*. **Disposition: migrate readers to `doc_id`-keyed
   catalog lookup; the prune verb gates on those migrations completing.**

3. **`title` is load-bearing in two ways and should not be pruned.**
   - It is the **chunk identity field for `knowledge__knowledge` slug entries**
     (per `aspect_readers.py:151–156` `CHROMA_IDENTITY_FIELD`). Reading any
     MCP-promoted note in T3 routes through `where={"title": …}`.
     `t3.py:1140–1160` (`existing_ids_for_title`) is the canonical reader.
   - It is a **universal search-result display field** (~25 sites in
     `formatters.py`, `mcp/core.py`, `commands/search_cmd.py`,
     `search_clusterer.py`). The Phase 4 description already flags this:
     *"`title` may have a longer life — confirm during audit; some
     operator-facing UX uses it."*

   **Disposition: keep `title` permanently. Revise Phase 5c removal scope to
   exclude it.** Migrating `knowledge__knowledge` to `doc_id`-keyed identity
   is theoretically possible but is far more intrusive than keeping a single
   display key.

4. **The arithmetic still works.** The deferred over-cap surface
   (`docs/migration/rdr-101-live-migration-postmortem.md:69`) is at 35–36
   keys. Dropping just the four `git_*` keys leaves 31–32 — adding `doc_id`
   pushes the 32-key class to 33 (still over the cap). Dropping `git_*` +
   `source_path` (five keys) brings the surface to 30–31, and `+doc_id` =
   31–32 (fits with one slot of headroom in the worst case). **Recommended
   prune list: 5 keys, not 6** (drop `title` from the list; the canonical-
   schema slot it occupies is already at-cap-aware in `MAX_SAFE_TOP_LEVEL_KEYS`).

## Reader inventory

Categorized by disposition. Sites are listed once at their canonical
function/method scope. Display formatters that read multiple keys are listed
under each key they read.

### Category A — `git_*` flat keys (drop, zero migration)

| File | Site | Role | Notes |
|---|---|---|---|
| `src/nexus/indexer.py:109–112` | `_git_metadata` | **WRITE** | Emits flat `git_*` for `normalize()` to pack |
| `src/nexus/indexer_utils.py:136–139` | `_git_metadata` | **WRITE** | Sister writer to `indexer.py` (legacy duplicate; out-of-scope cleanup candidate) |
| `src/nexus/metadata_schema.py:127–131` | `_GIT_FIELD_MAP` | Schema | Long→short key mapping for `git_meta` JSON packing |
| `src/nexus/metadata_schema.py:186–204` | `normalize` | Schema | Unpacks/repacks `git_meta`; strips flat git_* from output |
| `src/nexus/metadata_schema.py:318` | `chunk_metadata` factory | Schema | Comment-only reference |

**Migration plan:** None. The prune verb removes the flat keys from legacy
chunks; new writes have never persisted them since `git_meta` consolidation
shipped. The `_GIT_FIELD_MAP` and `normalize()` references stay as-is —
they're the writer-side defense that prevents re-introduction.

**Verification on Hal's catalog post-prune:** `git_meta` JSON blob remains
unaffected; flat `git_branch` / `git_commit_hash` / `git_project_name` /
`git_remote_url` keys disappear from all chunks. Search result display does
not change (no readers).

### Category B — `source_path` readers (migrate to `doc_id`-keyed catalog lookup)

#### B.1 — Chunk identity (`where={"source_path": …}`)

| File | Site | Replacement |
|---|---|---|
| `src/nexus/aspect_readers.py:151–168, 255–296` | `CHROMA_IDENTITY_FIELD`, `_identity_fields_for`, `_read_chroma_uri` | Add `doc_id` to dispatch table; prefer `where={"doc_id": …}` when caller has it. Fall back to `source_path` then `title` only when `doc_id` is absent (legacy URI shape). This is the `.10.1` bead — already scoped. |
| `src/nexus/db/t3.py:128–130, 1011–1037` | `set_distance_metric_metadata`, `ids_for_source` | Add a `ids_for_doc_id(collection, doc_id)` companion. `ids_for_source` stays as a deprecated alias until `.10.3` prune lands. |
| `src/nexus/db/t3.py:1039–1052` | `delete_by_source` | Companion: `delete_by_doc_id`. Keep `delete_by_source` as alias; same shape. |
| `src/nexus/db/t3.py:1054–1095` | `update_source_path` | One-shot rewrite; keep as-is. Legacy migration helper, not a steady-state reader. |
| `src/nexus/db/t3.py:1100–1138` | `get_by_id` (already `doc_id`) | No change. |
| `src/nexus/aspect_extractor.py:760–795` | `_source_content_from_t3` | Switch `where={"source_path": source_path}` to `where={"doc_id": doc_id}`. Caller needs `doc_id` plumbed in (currently passes `source_path`). |
| `src/nexus/catalog/catalog.py:2885–2907` | `Catalog._extract_chunk_span_text` | Replace the `chunk_index + source_path` pair with `chunk_index + doc_id`. Caller is `Catalog._extract_span_text` which has the entry — `entry.doc_id` is available. |
| `src/nexus/doc_indexer.py:425, 484, 622, 904, 943, 1049` | Incremental-sync existence checks | `where={"source_path": sp}` → `where={"doc_id": doc_id}` once the indexer caller has registered the doc and obtained the doc_id. Sub-task per file. |
| `src/nexus/indexer.py:573, 989, 998` | Code/docs incremental-sync + classification-prune | Same shape: `where={"source_path": …}` for stale-chunk detection. Migrate together with `doc_indexer.py`. |
| `src/nexus/indexer_utils.py:202` | `_paginated_get` caller | Same. |
| `src/nexus/pipeline_stages.py:886` | `_prune_stale_pdf_chunks` | `where={"source_path": pdf_path}` → `where={"doc_id": doc_id}`. |

#### B.2 — Catalog pre-filter

| File | Site | Replacement |
|---|---|---|
| `src/nexus/search_engine.py:177–190, 285–297` | `_prefilter_from_catalog` | Catalog returns a list of `file_path`s today; switch to returning `doc_id`s and emit `where={"doc_id": {"$in": doc_ids}}`. Touches the catalog query helper too. |

#### B.3 — Display (search-result formatting)

| File | Site | Replacement |
|---|---|---|
| `src/nexus/formatters.py:161, 253, 284, 334, 370, 380, 390` | `format_grouped_lines`, `format_compact`, `format_vimgrep`, `format_plain`, `format_plain_with_context` | Resolve `r.metadata["doc_id"]` to a path via the catalog at format time. Or: have the search engine attach a derived `_display_path` field to results so formatters stay schema-agnostic. The latter is cleaner — fewer formatters touch the catalog. |
| `src/nexus/scoring.py:223, 257` | `_apply_link_boost` | Replace the `source_path → tumbler` lookup with a direct `doc_id → tumbler` lookup (the catalog already keys on tumbler). Drops one level of indirection. |
| `src/nexus/commands/search_cmd.py:328, 380, 390, 421` | search-result post-processing (rg-cache, files-only) | Same as `formatters.py` — use a derived display field. |
| `src/nexus/mcp/core.py:567–568, 789–807` | search response builders, group-by-document | Same. |
| `src/nexus/search_clusterer.py:91` | `_cluster_label` | Already prefers `title`; the `source_path` fallback is rarely hit and can become a `doc_id → catalog.title` lookup. |
| `src/nexus/commands/taxonomy_cmd.py:567` | taxonomy-listing | Reads `title` only (Category C); included here for context. |

#### B.4 — Catalog/event-log synthesis & doctor (Phase-3 backfill plumbing)

| File | Site | Replacement |
|---|---|---|
| `src/nexus/catalog/synthesizer.py:480–595` | `_synthesize_collection_chunks` | This is the **backfill** path that reads `source_path` and `title` to map orphan chunks back to a doc_id. It must keep reading both keys *during the prune window* — running the prune verb against a chunk would break this synthesizer if the chunks no longer carry source_path. **Order of operations:** synthesize-log + backfill-doc-id MUST run before the prune verb on each collection. The prune verb needs a guard: refuse if `--t3-doc-id-coverage` < 100% on the target collection. |
| `src/nexus/catalog/catalog.py:2896` | span-extraction | (covered in B.1) |

#### B.5 — Aspect extraction & promotion

| File | Site | Replacement |
|---|---|---|
| `src/nexus/aspect_extractor.py:212–227, 1078–1097` | LLM prompt template + response demux | The `source_path` here is **not** a T3-chunk-metadata read — it's the catalog entry's `file_path` value passed verbatim into the LLM prompt and echoed back. Out of scope for the prune. (The prompt could be retitled to `source_id` or `doc_id` later, but that is decoupled from the T3 prune.) |
| `src/nexus/aspect_extractor.py:772` | `_source_content_from_t3` | (covered in B.1) |
| `src/nexus/aspect_promotion.py:60–70` | `_RESERVED` allowlist | The set lists `source_path` as a reserved aspect-table column. Drop the entry once the columns table is migrated; or keep — it's a defensive guard, not a hot reader. **Decision: keep.** |
| `src/nexus/operators/aspect_sql.py:121–130, 445–460` | `_resolve_identity`, `_summarise_items` | Reads `source_path` from operator-runner item dicts (which originate from `query` results carrying chunk metadata). Add a `doc_id` resolution branch and prefer it over `source_path`. |

#### B.6 — Enrich command

| File | Site | Replacement |
|---|---|---|
| `src/nexus/commands/enrich.py:110–125, 195–205, 310–320` | `enrich-bib` chunk scan | Reads chunk metadata to map `title` → `source_path` for downstream filename inference. With `doc_id` available, the resolution is simpler: `doc_id → catalog.entry.file_path`. |

#### B.7 — Exporter (`.nxexp` round-trip)

| File | Site | Replacement |
|---|---|---|
| `src/nexus/exporter.py:188–190` | `export_collection` filter | `_apply_filter(source_path, includes, excludes)` — the include/exclude predicates are operator-facing path patterns. **Keep:** an exporter filter is a CLI ergonomic, not a chunk-identity read. After the prune the legacy `.nxexp` files still carry `source_path`; importers handle it via `--remap`. |
| `src/nexus/exporter.py:364–366` | `import_collection` remap | `meta["source_path"] = _apply_remap(...)` — operates on the input metadata dict before write. **Keep** as a transitional read; mark with a deprecation comment. The new write path strips it via `normalize()` once `source_path` leaves `ALLOWED_TOP_LEVEL`. |
| `src/nexus/exporter.py:391, 411` | `fire_store_chains(source_paths=…)` | Post-import hook signature. The chain consumers expect a `source_paths` list. Audit chain consumers separately — most use the path for catalog lookups, all migratable to `doc_id`s. Sub-task. |

#### B.8 — Catalog commands (operator surveys)

| File | Site | Replacement |
|---|---|---|
| `src/nexus/commands/catalog.py:2230, 2535` | `nx catalog import-from-t3`, `nx catalog auto-link` | Both walk a collection's chunks to discover unique `source_path`s for catalog backfill. Both run pre-Phase-4 (legacy migration verbs). After Phase 4, these become `doc_id` walks (already-registered). **Keep behind a legacy flag** in case operators run them on freshly-imported `.nxexp` files where `doc_id` isn't populated yet. |
| `src/nexus/commands/collection.py:255–280` | `nx collection delete --force` safety check | Reads chunks to decide whether sources are reindexable. With the catalog, this check can ask the catalog directly (fewer chunk reads). |

#### B.9 — Indexer write paths (no read of own writes)

| File | Site | Role |
|---|---|---|
| `src/nexus/indexer.py:1026` | indexer write | **WRITE** of `source_path`. After prune-deprecated-keys lands AND `ALLOWED_TOP_LEVEL` removes `source_path`, this write becomes a no-op via `normalize()`. Until then, dual-write is harmless. |
| `src/nexus/prose_indexer.py:72` | indexer write | Same. |
| `src/nexus/doc_indexer.py:779` | indexer write | Same. |
| `src/nexus/commands/search_cmd.py:134` | rg-cache write | Same. |

### Category C — `title` readers (KEEP — do not prune)

The Phase 4 description allows for a "reason to keep the metadata key
permanently" outcome. `title` qualifies on two distinct grounds:

#### C.1 — Identity field for `knowledge__knowledge`

| File | Site | Note |
|---|---|---|
| `src/nexus/aspect_readers.py:151–156, 255–296` | `CHROMA_IDENTITY_FIELD["knowledge__"] = ("source_path", "title")` | Slug-keyed MCP-promoted notes have NO `source_path`; their identity is `title`. Removing `title` from these chunks breaks `_read_chroma_uri` and every consumer of `read_source` for `knowledge__knowledge` URIs. |
| `src/nexus/db/t3.py:1140–1160` | `existing_ids_for_title` | The canonical title-keyed lookup. Used by `nx memory promote`, MCP `store_get_many`, and the synthesizer's title-fallback. |

#### C.2 — Universal display field

`title` is the human-readable label across every search-result surface:

| File | Site | Role |
|---|---|---|
| `src/nexus/formatters.py:336` | `format_plain` fallback when `source_path` is empty | Display |
| `src/nexus/mcp/core.py:567, 789–795, 1002, 1240, 1300, 1830` | search response builders, store-list, document group-by | Display + grouping |
| `src/nexus/commands/index.py:674, 712` | PDF index summary | Display |
| `src/nexus/commands/taxonomy_cmd.py:567` | taxonomy listing | Display |
| `src/nexus/commands/enrich.py:116–122` | enrich-bib title→chunk-id index | Identity within enrich (paper-grain, not chunk-grain) |
| `src/nexus/doc_indexer.py:791, 856–1070` | PDF indexer return-metadata + summary | Display |
| `src/nexus/pipeline_stages.py:712–784` | extraction-result → chunk metadata | **WRITE** (sources `title` from extraction result) |
| `src/nexus/catalog/synthesizer.py:562–579` | event-log title fallback for orphan resolution | Backfill |
| `src/nexus/search_clusterer.py:91` | cluster label | Display |
| `src/nexus/indexer.py:780, 811` | embed-text construction | **WRITE** |

Out-of-scope `title` references (catalog SQLite rows, T2 memory store, RDR
frontmatter, dialog events) — not T3 chunk metadata; not affected by the
prune.

#### C.3 — Recommendation

**Keep `title` in `ALLOWED_TOP_LEVEL` permanently. Revise `nexus-o6aa.13`
(Phase 5c schema removal) to exclude `title` from its scope.**
The slot cost is one out of the 32-key budget; the alternative is rewriting
~25 display sites and migrating `knowledge__knowledge` identity to `doc_id`,
which costs more than one metadata slot is worth.

## Recommended Phase 4 sequence (revised)

1. **`.10.2` (this audit)** — done.
2. **`.10.1` (`aspect_readers.py` doc_id dispatch)** — already scoped.
3. **`.10.X` (new bead) — search-engine catalog prefilter migration**
   (`search_engine.py:_prefilter_from_catalog`). One file, isolated change.
4. **`.10.Y` (new bead) — display-formatter migration**: have the search
   engine attach `_display_path` (catalog-resolved) so formatters/mcp/core
   stop reading `source_path`. Touches `formatters.py`, `commands/search_cmd.py`,
   `mcp/core.py`, `search_clusterer.py`, `scoring.py` together.
5. **`.10.Z` (new bead) — incremental-sync where-filter migration**:
   `indexer.py`, `indexer_utils.py`, `doc_indexer.py`, `pipeline_stages.py`,
   `aspect_extractor.py`, `catalog/catalog.py`. Adds `doc_id` companions to
   `T3Database.{ids_for_source, delete_by_source}` and migrates callers.
6. **`.10.W` (new bead) — catalog-walk migration**: `commands/catalog.py`
   import-from-t3 + auto-link. Behind a legacy flag for operator
   `.nxexp` imports.
7. **`.10.V` (new bead) — exporter `fire_store_chains` consumer audit**:
   migrate `source_paths=` to `doc_ids=` after auditing every chain consumer.
   Smallest scope but highest fan-out; do last.
8. **`.10.3` (`nx catalog prune-deprecated-keys` verb)** — implement once
   `.10.1`, `.10.X`, `.10.Y`, `.10.Z`, `.10.W`, `.10.V` are all merged. The
   verb drops **5 keys** (4 git_* + source_path) and adds a guard refusing
   to run if `--t3-doc-id-coverage` < 100% on the target collection.
9. **Operator runs prune + `t3-backfill-doc-id --resume`** —
   `chunks_deferred` → 0.
10. **`.10.4` (post-prune backfill rerun docs)** — operator guide update.

## Out of scope

- **`source_uri` migration (RDR-096 P5.1/P5.2)** — different epic, different
  schema field. The Phase-4 prune of `source_path` does NOT depend on
  `source_uri` reaching steady state; the `doc_id`-keyed lookups are the
  immediate replacement, and `source_uri` is a separate denormalisation
  pattern that operates at the catalog/aspect-readers layer, not T3 chunks.
- **`source_title` rewrites** — already collapsed into `title` per
  `metadata_schema.py:63–66`. No further action.
- **`store_path`, `frecency_score`, `corpus`, `embedding_model`,
  `content_type`, `category`, etc.** — none flagged for prune; out of scope.
- **Aspect-runner / enrichment LLM prompt rename** — the `source_path`
  string in the prompt template (`aspect_extractor.py:212`) is a stable
  external contract with the model, not a T3 read. Decoupled.

## Schema-removal Phase 5c scope (revised)

Original Phase 5c (`nexus-o6aa.13`) was scoped to remove `source_path`,
`title`, and the four `git_*` flat keys from `ALLOWED_TOP_LEVEL`. **This
audit recommends:**

| Key | Phase 5c action |
|---|---|
| `git_branch`, `git_commit_hash`, `git_project_name`, `git_remote_url` | Remove from `_GIT_FIELD_MAP` only if all writers stop emitting them; otherwise keep `_GIT_FIELD_MAP` so `normalize()` continues to consolidate. (Since writers still need to consolidate, **keep `_GIT_FIELD_MAP`; remove the flat keys from any future `ALLOWED_TOP_LEVEL`-extension only.**) |
| `source_path` | Remove from `ALLOWED_TOP_LEVEL`. New writes will be silently dropped by `normalize()`. |
| `title` | **Keep.** See Category C. |

## Open questions

- **`exporter.py:391, 411` `fire_store_chains` consumers.** The exporter
  passes a `source_paths=` list to the post-import chain. Auditing every
  consumer is a sub-task (`.10.V`). If any consumer cannot be migrated to
  `doc_id` cheaply, the consumer audit may grow Phase 4 scope.
- **Legacy `.nxexp` import path.** Older `.nxexp` files predate `doc_id`. The
  importer's `--remap source_path` flag is operator-facing; should it gain a
  `--remap doc_id` companion, or should imported chunks pass through
  `synthesize-log` + `t3-backfill-doc-id` before the operator runs prune?
  (Recommend the latter — operator playbook update under `.10.4`.)
- **Doctor coverage guard.** `prune-deprecated-keys` should refuse if
  `--t3-doc-id-coverage` is < 100% on the target collection. Confirm the
  doctor exposes a programmatic coverage value (not just exit-code).

## Acceptance check

This audit's deliverable shape (per `nexus-o6aa.10.2` description):

- [x] Markdown report listing every reader.
- [x] For each reader: a planned migration approach.
- [x] Open follow-up beads filed for any non-trivial migration. *(filed in
  separate bead-creation step alongside this commit; see commit message.)*
- [x] At least one "keep permanently" decision (title — Category C).
