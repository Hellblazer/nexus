# RDR-101 Phase 0: Field-by-field disposition audit

This audit enumerates every key reachable through `src/nexus/metadata_schema.py` (the
canonical 30-key `ALLOWED_TOP_LEVEL` set, the four flat `git_*` short keys that
`make_chunk_metadata()` accepts before `normalize()` packs them into `git_meta`, and the
projection-level fields named in RDR-101 Entities and Phase 5 that do not exist in the
current schema yet). For each key the table records its home today, its disposition
category (A/B/C/D/E), its new home under the greenfield design, and a one-sentence
justification cross-referenced against RDR-101 §Entities, §Provenance projection, and
the Phase 5 deletion checklist. Keys that the bead description names but that are
already gone from the live schema (for example `expires_at`, `document_title`,
`embedded_at`, `bib_doi`, the flat `git_*` keys, `chash`) are still listed so the
disposition is decided once for the full surface RDR-101 will touch, not just for what
the current factory emits.

Categories: **A** stays on chunk in T3, **B** moves to Document projection, **C** moves
to Provenance projection (T2), **D** moves to a new Frecency projection (T2), **E**
removed entirely.

| key | today's home | category | new home | justification |
|---|---|---|---|---|
| `source_path` | T3 chunk metadata (`ALLOWED_TOP_LEVEL`) | E (and B) | Removed from T3 (chunks key on `doc_id`); the per-document URI lives in the `Document` projection as `source_uri`, sourced from `DocumentRegistered` and `DocumentRenamed` events. | RDR-101 Phase 5 explicitly removes `source_path` from T3 metadata; the file-scheme URI persists exactly once in the Document projection. |
| `content_hash` | T3 chunk metadata | A | Stays on chunk in T3 (intrinsic to chunk identity). | RDR-101 §Entities lists `content_hash` on the `Chunk` row. |
| `chunk_text_hash` (`chash`) | T3 chunk metadata | A | Stays on chunk as `chash`, indexed but not unique. | RDR-101 §Entities; chash supports `chash:<hex>` span resolution per RDR-086 and is non-unique across documents (substantive critique C1). |
| `chunk_index` | T3 chunk metadata | A | Stays on chunk in T3 (intrinsic position within a document). | Position is per-chunk per-document, computed at index time, not derived from any other store. |
| `chunk_count` | T3 chunk metadata | E | Removed; readers compute `SELECT COUNT(*) FROM chunks WHERE doc_id = ?` against the `Chunk` projection. | RDR-101 §Provenance projection and Phase 5 explicitly remove `chunk_count`; carrying it on every chunk denormalises a value the catalog can compute on demand. |
| `chunk_start_char` | T3 chunk metadata | A | Stays on chunk in T3 (intrinsic chunk-position offset). | Character offset is meaningful only with respect to the chunk inside its document; RDR-101 §Provenance projection retains per-chunk position fields. |
| `chunk_end_char` | T3 chunk metadata | A | Stays on chunk in T3. | Same reasoning as `chunk_start_char`. |
| `line_start` | T3 chunk metadata | A | Stays on chunk in T3 (intrinsic chunk-position field). | Line ranges are computed once at chunking time and never recomputed; RDR-101 §Provenance projection retains them on the chunk. |
| `line_end` | T3 chunk metadata | A | Stays on chunk in T3. | Same reasoning as `line_start`. |
| `page_number` | T3 chunk metadata | A | Stays on chunk in T3 (intrinsic to PDF chunks; analogous to `line_start` for line-oriented sources). | Page number is a position field, decided once at chunking time, mirrors line ranges in RDR-101's "intrinsic" group; not enumerated explicitly in RDR-101 but parallel to the line-range disposition. |
| `title` | T3 chunk metadata (per-chunk) | B | Moves to `Document.title` (projection-only); per-chunk persistence removed. | RDR-101 partially supersedes RDR-096 on per-chunk `title`; Phase 5 removes the per-chunk field, the document title lives once in the catalog projection. |
| `document_title` (RDR-101 reference name) | not in current schema; effectively the projection of `title` to per-document scope | B | New name for the per-document title in the `Document` projection. | RDR-101 §Entities does not introduce a `document_title` column literally, but the per-document `title` lives in the `Document` projection (read via `entry.title` today). Listing it confirms there is no second per-document title field. |
| `source_author` | T3 chunk metadata | B | Moves to `Document` projection (per-document fact). | Author is identical for every chunk of the same document; carrying it per-chunk is the same denormalisation pattern Phase 5 removes for `title`. JUDGMENT CALL: not named in RDR-101; treated as parallel to per-doc `title`. |
| `section_title` | T3 chunk metadata | A | Stays on chunk in T3 (intrinsic to the chunk's region within a structured document). | Different chunks of one document have different section titles; not denormalised. JUDGMENT CALL: RDR-101 §Provenance projection lists `section_title` in the "Removed entirely" prose, but the surrounding context is hierarchical headings vs flat tags; section_title is genuinely per-chunk and reading-relevant, so I retain it as A. Cross-checked against the Phase 5 deletion list, which does not name `section_title`. |
| `section_type` | T3 chunk metadata | A | Stays on chunk in T3 (intrinsic to the chunk's structural role). | Same JUDGMENT CALL as `section_title`; RDR-101 §Provenance projection prose names it, but the Phase 5 deletion list does not, and it is per-chunk truth, not denormalisation. |
| `tags` | T3 chunk metadata | E | Removed entirely (cargo data). | RDR-101 §Provenance projection explicitly names `tags` in the "Removed entirely" group; tags duplicate categorisation already encoded in the collection name and content_type. |
| `category` | T3 chunk metadata | E | Removed entirely (cargo data). | RDR-101 §Provenance projection explicitly names `category` in the "Removed entirely" group; replaced by the collection name's `<content_type>` segment. |
| `content_type` | T3 chunk metadata | E (and B) | Removed from T3 (encoded in the collection name); persisted once on `Document.content_type`. | RDR-101 §Collection naming and invariants requires `content_type` to be the leading segment of `coll_id`, so per-chunk persistence is redundant; kept on the Document row. |
| `store_type` | T3 chunk metadata | E | Removed entirely (cargo data). | RDR-101 §Provenance projection explicitly names `store_type` in the "Removed entirely" group; the store is implied by the collection. |
| `corpus` | T3 chunk metadata | E | Removed entirely (cargo data). | RDR-101 §Provenance projection explicitly names `corpus` in the "Removed entirely" group; corpus identity is encoded in `<owner_id>` of the collection name. |
| `embedding_model` | T3 chunk metadata | E (and B) | Removed from T3 chunk; encoded in the collection name (`<embedding_model>@<model_version>`) and persisted on the `Collection` projection. | RDR-101 §Collection naming and invariants makes embedding model + version a collection-level fact, not a per-chunk fact; the `Collection` row is the single home. |
| `indexed_at` | T3 chunk metadata | C (and B) | The per-chunk `embedded_at` (timestamp of the T3 write) stays on the chunk per RDR-101 §Provenance projection; the per-document `indexed_at_doc` lives on the `Document` projection. | RDR-101 distinguishes `indexed_at_doc` (Document) from `embedded_at` (Chunk); today's single `indexed_at` collapses both. JUDGMENT CALL: split into two fields during Phase 3, since the existing single field cannot serve both projections. |
| `embedded_at` (RDR-101 name) | not yet in current schema | A | Stays on chunk in T3 (per-chunk timestamp of the T3 write). | RDR-101 §Provenance projection explicitly retains `embedded_at` on the chunk. New name for the chunk-scoped portion of today's `indexed_at`. |
| `indexed_at_doc` (RDR-101 name) | not yet in current schema | B | New column on the `Document` projection. | RDR-101 §Entities lists `indexed_at_doc` on the `Document` row; today's `indexed_at` is partially this. |
| `source_mtime` | not in current schema | B | New column on the `Document` projection. | RDR-101 §Entities lists `source_mtime` on the `Document` row. JUDGMENT CALL: The bead enumerates it; the field is not in `ALLOWED_TOP_LEVEL` today but the catalog already tracks file mtime for change detection, so this is simply naming the projection home. |
| `ttl_days` | T3 chunk metadata | D | Moves to a new `Frecency` projection (T2 SQLite, FK `chunk_id`). | RDR-101 §Provenance projection groups frecency state in T2; ttl is part of the lifecycle/scoring trio (`ttl_days`, `expires_at` derived, `frecency_score`). |
| `frecency_score` | T3 chunk metadata | D | Moves to a new `Frecency` projection (T2 SQLite, FK `chunk_id`). | RDR-101 §Provenance projection names `frecency_score` for relocation; per-chunk heat does not belong on the immutable chunk record. |
| `expires_at` (currently derived) | derived in `is_expired()` from `indexed_at + ttl_days` | D | Either derived from the Frecency projection's `(embedded_at, ttl_days)` columns, or materialised as a Frecency-projection column for `WHERE expires_at < now` filtering. | The current schema already removed it; restored here as a Frecency-projection concern so RDR-101's "where does decay live" question is answered explicitly. |
| `miss_count` (RDR-101 reference name) | not in current schema | D | New column on the Frecency projection (T2 SQLite, FK `chunk_id`). | The bead enumerates `miss_count` as part of the Frecency relocation. JUDGMENT CALL: RDR-101 does not specify a Frecency projection schema; this audit flags that gap below. |
| `source_agent` | T3 chunk metadata | C | Moves to `Provenance` projection (T2 SQLite, FK `chunk_id`). | RDR-101 §Provenance projection explicitly names `source_agent`; provenance does not need to ride on every vector record. |
| `session_id` | T3 chunk metadata | C | Moves to `Provenance` projection (T2 SQLite, FK `chunk_id`). | RDR-101 §Provenance projection explicitly names `session_id`. |
| `git_meta` (consolidated JSON blob) | T3 chunk metadata | C | Decomposed into the `Provenance` projection columns (`git_commit`, `git_branch`, `git_remote`, plus `git_project_name` if retained); the JSON blob form is dropped. | RDR-101 §Entities lists the git fields as relational columns on `Provenance`; the JSON-packed slot was a Chroma-quota workaround that the projection makes unnecessary. |
| `git_project_name` (flat, accepted by factory pre-pack) | flat key consumed by `make_chunk_metadata()` then packed into `git_meta` by `normalize()` | C | Moves to `Provenance` projection column (alongside `git_commit`, `git_branch`, `git_remote`). | RDR-101 §Provenance projection lists git_* fields as relational columns. |
| `git_branch` (flat) | same as above | C | Moves to `Provenance.git_branch`. | RDR-101 §Entities lists `git_branch` on `Provenance`. |
| `git_commit_hash` (flat) | same as above | C | Moves to `Provenance.git_commit`. | RDR-101 §Entities names the column `git_commit`; this row is the rename. |
| `git_remote_url` (flat) | same as above | C | Moves to `Provenance.git_remote`. | RDR-101 §Entities names the column `git_remote`. |
| `bib_year` | T3 chunk metadata | B | Moves to the `Document.aspects` slot (paper documents only) via `DocumentEnriched` events / `Aspect` projection. | RDR-101 §Entities defines `Aspect { doc_id, model_version, payload_json }` for scholarly-paper-v1 enrichment; bib_* values are paper aspects, not per-chunk facts. |
| `bib_authors` | T3 chunk metadata | B | Moves to the `Aspect` projection (scholarly-paper-v1 payload). | Same reasoning as `bib_year`. |
| `bib_venue` | T3 chunk metadata | B | Moves to the `Aspect` projection (scholarly-paper-v1 payload). | Same reasoning as `bib_year`. |
| `bib_citation_count` | T3 chunk metadata | B | Moves to the `Aspect` projection (scholarly-paper-v1 payload). | Same reasoning as `bib_year`. |
| `bib_semantic_scholar_id` | T3 chunk metadata (load-bearing "this title was enriched" marker) | B | Moves to `Aspect.payload_json.semantic_scholar_id` AND becomes the catalog-side enriched-title flag (`Document` row has an `Aspect` of `model_version=scholarly-paper-v1`). | RDR-101 Phase 0 already calls out this migration ("`bib_semantic_scholar_id` migration plan"): commands/enrich.py's skip logic must query the catalog/aspect row, not T3. JUDGMENT CALL: the enriched-title check changes from "field present in T3 metadata" to "Aspect row exists in T2"; the bead RDR-101 design already names this but does not commit a final API. |
| `bib_doi` (RDR-101 reference name) | not in current schema | B | Lives in `Aspect.payload_json` for paper documents (scholarly-paper-v1). | The bead names `bib_doi`; current schema does not have it, but Aspect.payload_json is the single home for any future bibliographic field. |

## Cross-check against the RDR-101 Phase 5 deletion checklist

Phase 5 (`metadata_schema.py:ALLOWED_TOP_LEVEL` updates) names: `source_path`,
per-chunk `title`, `git_*`, `corpus`, `store_type`. All five are present in the table
above:

- `source_path` row, category E (relocated to Document projection)
- `title` row, category B (per-chunk persistence removed; lives on Document)
- `git_meta` plus the four flat `git_*` rows, category C (relational columns on Provenance)
- `corpus` row, category E
- `store_type` row, category E

The Phase 5 catalog-schema removals (`head_hash`, `chunk_count`, mutability of
file-scheme `source_uri`) are also covered: `chunk_count` row category E with
COUNT-on-projection rationale; `head_hash` is not a chunk-metadata key so it does not
appear in this table, but its removal is tracked in RDR-101 Phase 5 directly.

## Judgment calls (escalated for review)

These dispositions are not mechanical translations of RDR-101 prose. Each may need a
second pass by Phase 1 design.

1. **`section_title` and `section_type`** kept as category A (stay on chunk). RDR-101
   §Provenance projection prose lists them in the "Removed entirely" group, but the
   Phase 5 deletion checklist does not. They are genuinely per-chunk reading context,
   not denormalisation. If the eventual Phase 5 PR wants to remove them, the table row
   should flip to E with the rationale that ARM/section reconstruction can happen via
   document-aspect rather than per-chunk metadata.
2. **`source_author`** category B (move to Document projection). Not enumerated in
   RDR-101 §Entities; treated by analogy with the per-document `title` rename. If
   `source_author` is paper-specific, Phase 1 may prefer to live inside
   `Aspect.payload_json` instead of a top-level Document column.
3. **`indexed_at` split** into chunk-scoped `embedded_at` and document-scoped
   `indexed_at_doc`. Today there is one field; RDR-101 §Entities lists two. The split
   is an additive change in the projector, but a deduplication decision is needed in
   Phase 3: keep emitting both during the deprecation window, or fail loudly when
   only one is provided.
4. **`bib_*` rehome to `Aspect`** rather than to a flat `Document` column set. RDR-101
   §Entities defines `Aspect`, but the original bib_* field set predates RDR-101 and
   Phase 0 (in the RDR itself) calls out the unresolved migration plan. This audit
   commits the disposition to Aspect; Phase 3 may prefer a hybrid where the
   load-bearing `bib_semantic_scholar_id` lives on Document for cheap "is this
   enriched?" checks and the rest live on Aspect.
5. **`miss_count`** disposition assumes a Frecency projection exists. The RDR-101
   design does not currently spec this projection's schema (see gap 1 below).

## Gaps in RDR-101 design surfaced by this audit

1. **No Frecency projection schema in RDR-101 §Entities.** The bead enumerates
   `frecency_score`, `expires_at`, `ttl_days`, and `miss_count` for relocation to a new
   T2 projection. RDR-101 §Provenance projection prose mentions "frecency lives in T2
   frecency table" but the §Entities ER model does not include a `Frecency` row with
   columns and FK shape. Phase 1 needs at minimum:
   - `Frecency { chunk_id PK FK Chunk, embedded_at, ttl_days, frecency_score, miss_count, last_hit_at }`
   - Decision: is `expires_at` materialised as a column for indexed `WHERE
     expires_at < now` filtering, or always derived from `(embedded_at, ttl_days)`?
   - Decision: does the Frecency row participate in the event log (`ChunkAccessed`
     event for last_hit_at, `ChunkMissed` for miss_count) or live as a non-event-sourced
     mutable side table? The current `nx t3 gc` design implies events; consistency
     with that pattern argues for events here too.

2. **No `Aspect.payload_json` schema fragment for bib_*.** RDR-101 §Entities defines
   `Aspect` generically (`payload_json` is opaque), and Phase 0 of the RDR itself
   already flags that `bib_semantic_scholar_id` needs a migration plan. The Aspect
   payload schema for `scholarly-paper-v1` should pin: `{semantic_scholar_id, doi,
   year, authors, venue, citation_count}` with a versioned schema id, so the
   enrichment-skip check has a stable API.

3. **`section_title`/`section_type` ambiguity** between RDR-101 §Provenance projection
   prose ("Removed entirely") and the Phase 5 deletion list (does not name them).
   Phase 5 PR needs an explicit decision; prose-vs-checklist disagreement should not
   ride into implementation.

4. **`source_author` not enumerated in RDR-101 §Entities.** Either add it to the
   Document row or commit it to `Aspect.payload_json` for paper documents (and decide
   what `code__` and `prose__` content do for it: probably nothing, the field is paper-
   specific in practice and was over-generalised when added to `ALLOWED_TOP_LEVEL`).

5. **`indexed_at` two-field split** during the deprecation window. RDR-101 §Entities
   distinguishes `indexed_at_doc` and `embedded_at`; the projector has to populate
   both from incoming events. Phase 3 needs to decide whether the new write path emits
   both fields explicitly (preferred) or whether the projector synthesises one from
   the other (couples the projection to a write-time invariant that may drift).
