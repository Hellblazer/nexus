---
title: "Chunk-row metadata normalization: document fields on the catalog document, position fields on the manifest, the chunk row keeps only what its text determines"
id: RDR-236
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-26
accepted_date:
related_issues: [nexus-6pfgb, nexus-x54wv, nexus-tko2w, nexus-u9dc5, nexus-3ajpd, nexus-j67ix]
related_rdrs: [RDR-108, RDR-180, RDR-192, RDR-222, RDR-223, RDR-231, RDR-232]
---

# RDR-236: Chunk-row metadata normalization

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Asked for by Sam on 2026-10-26, at the end of a day in which the same defect
class had been patched reader by reader for the fifth time: "stop the
bleeding". The patches were correct and the defect kept reappearing, because
the schema invites it.

Terms used throughout. A **chunk** is one row of embedded text in the T3
table `nexus.chunks`, stored by the Java **engine** (the nexus service) in
Postgres with pgvector. Its identity is its **chash**, the SHA-256 of its
text (RDR-108, widths per RDR-180). Because the identity is the text, two
documents in the same collection that contain the same passage share one
chunk row; the row's key is (tenant, collection, chash, embedding model). A
**catalog document** is a row of `catalog_documents`, addressed by a
**tumbler** (a hierarchical address such as `1.2.5`; the catalog's name for
a document id), holding what is true of the document as a whole: title,
author, bibliographic columns, and a free-form JSON column called **meta**.
A document's **manifest** is its rows in `catalog_document_chunks`, one per
chunk position, each pointing at a chash; it is the only record of which
chunks compose a document and in what order. **Chunk metadata** is the JSON
column on the chunk row. The engine merges it on every write so the newest
write wins per key and no key is ever removed by a write that omits it:
**last-writer-wins**. Two further terms: the **wire ledger**
(`docs/wire-contract-pending.md`) records every change to what the engine
accepts or returns, each marked **additive** (an old client keeps working)
or **not-additive** (an old client breaks, so the deploy order is
constrained); and the **client floor** is the oldest engine version the
client accepts (`REQUIRED_ENGINE_VERSION`).

## Problem Statement

A chunk row is shared by every document in its collection whose text
contains that chunk, but the row carries fields that belong to one document:
its title, tags, category, bibliographic data, extraction provenance,
time-to-live, indexing time, frecency (a per-file ranking hint computed
from git history), and the section label and line span of one position in
one file. Under sharing, every one of those fields on the row is
whichever document wrote last. Twenty-three reader sites in the client and
three filter routes in the engine take those values from the row and present
them as that document's own (T2
`nexus/x54wv-shared-row-key-census-2026-10-26`, parts 1 and 2). Nothing is
deleted by this; what is wrong is what people and LLM prompts are told about
provenance, which results a filter returns, which note expires, and what a
backup records.

The epic nexus-6pfgb was opened on 2026-10-26 and by that evening held 50
children, 17 of them closed: reader moves, two rulings, three freshness
fixes and one identity bug. Each new reader, writer or route found since has
become a new child. The epic grows because the row still accepts and carries
the fields, so every new code path that touches chunk metadata reopens the
class. The remedy is a schema the engine enforces, not a longer list of
patched readers.

### Enumerated gaps to close

#### Gap 1: The chunk row carries document-level fields a shared row cannot hold

`make_chunk_metadata` (`src/nexus/metadata_schema.py`) and four hand-built
dicts in the indexers write title, source_author, tags, category,
content_type, the bib_* keys, extraction_method, quality_gate_overridden,
extraction_source, ttl_days, indexed_at, frecency_score, source_agent,
session_id and catalog_doc_id onto every chunk row. The census (part 1,
section 1) lists every writer. For a shared row each is the last writer's.
Some of these fields have a catalog home already (title, author, the bib_*
columns, indexed_at, source_uri); tags, category, extraction provenance,
ttl_days, frecency_score and source_agent had none until the note keys went
into catalog meta on 2026-10-26 (nexus-6pfgb.5), and indexed documents still
have none.

#### Gap 2: Position fields sit on the row though the manifest is the per-position record

A section title, section type, page number and the line and character span
describe one position in one document, not the text. nexus-tko2w (found by
RDR-232's research) proved the section labels wrong under sharing and gave
the manifest its own label columns (changeset catalog-046: section_title,
section_type, page_number, and `labels_written`, a flag that says the row's
labels are its document's own answer) with an engine stage
(`ManifestLabelStage`) that answers label filters per owning document. The
span columns the manifest has carried since RDR-108 (char_start, char_end,
line_start, line_end) are not what search returns: the citation address a
hit carries (chash, span, source_uri) still takes its span from the row
(`PgVectorRepository.spanFromMeta`) and its source_uri from an arbitrary
owner (census part 2, row 21). Notes and the two backfill verbs write
manifest rows without label keys (nexus-jz86a; RF-9), so a note that shares
text with an indexed file reads that file's label.

#### Gap 3: The engine accepts any key on chunk metadata, so any path can reopen the class

The chunk write routes accept whatever JSON the client sends and merge it:
`/v1/vectors/upsert-chunks`, `/v1/vectors/update-metadata` (the partial
overlay route), and the combined manifest writes
(`/v1/catalog/manifest/append`, `append_many` and `write_many`, the last
being the main one: one document in N batches and every note go through
it). The client's
`ALLOWED_TOP_LEVEL` list is advisory: `commands/enrich.py` writes
bib_openalex_id and bib_doi outside it, and the extraction post-pass writes
title and provenance through `update-metadata`, both paths bypassing
`validate()` (census part 2, closing note). RDR-223 made the same
observation about ownerless chunk writes and closed it by making the engine
refuse them. The same is needed here, and as an allow-list: a chunk write
may carry chunk-intrinsic keys only; every other key is refused, not merged.

#### Gap 4: Filters on document keys evaluate on the row on most routes

A `where=` filter on tags, category, title, extraction_method or
content_type reaches the engine untouched and is evaluated as JSON
containment on `chunks.metadata` (census part 2, section 4). A shared chunk
is judged on the last writer's value, so a filter can return a hit for a
document that does not satisfy it and drop one that does. Engine 0.1.165
(deployed 2026-10-26) carries nexus-6pfgb.24: `ManifestLabelStage` answers
source_author, indexed_at and the bib_* keys per owning document on the
plain search routes. Its own documentation says the catalog meta keys
(tags, category, ttl_days and the rest) are not answered there, because
today they have no catalog home to answer from. The metadata-scoped,
aspect, graph-hop and get routes (nexus-u9dc5), `title=` and
`content_type=` (nexus-6pfgb.25) and the meta keys (nexus-6pfgb.29) still
evaluate on the row.

#### Gap 5: Readers outside the nexus repository depend on the row keys

The conexus repository (the deployment and operations repository for the
cloud engine, distinct from the `conexus` PyPI package) reads chunk
metadata directly in its deploy gates: the parity gate (deploy step 6,
which replays a fixed query set against the new engine and compares the
results with the old one's) sends 12 of 113 queries with category or tags
filters through the engine's search routes (its engine leg) and also
applies them as raw SQL `metadata->>key = value` on `nexus.chunks` to
count what the engine should have found (its recall leg); a findings
query reads
title and indexed_at from the row; a rehearsal census assumes indexed_at
survives in place (T2
`nexus/chunk-row-normalization-external-readers-2026-10-26`, 2026-10-26).
Every engine redeploy runs the parity gate, so those gates must read the
catalog before clients stop writing the keys, or the next redeploy, related
or not, undercounts.

## Relationship to Prior RDRs

Searched the RDR index for: chunk metadata, content address, manifest,
catalog split, last writer, section labels, atomic write, source_uri.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-108 graph identity normalization | Origin | Made the chash the chunk's identity and the manifest the record of document structure; its Phase 3 dropped `doc_id`, `chunk_index` and `chunk_count` from chunk metadata. `docs/agents/catalog-t3-split.md`, which summarises it, states the rule this RDR extends: "Document structure lives in the catalog manifest, never in chunk metadata; identical chunk text in the same collection collapses to one T3 row by design." RDR-108 said nothing about the other document fields, which stayed by omission. Its rationale holds in full. |
| RDR-180 chash as 32 raw bytes | Precedent | Closed a width ambiguity across every table with one definition, a check constraint, and a one-time rewrite that reused embeddings. Same shape here: one definition of what a chunk row holds, enforced at the engine, with a one-time fill of the catalog from the rows. |
| RDR-223 atomic chunk-plus-owner write | Precedent | Its Phase 3 made the engine refuse ownerless chunk writes. Its mechanics, which Phase 4 below copies: the check ships log-only first, enforcement waits until the engine's log of would-be-refused writers is empty or every entry is dispositioned (a client-side search cannot see subprocess, shell or Java writers; the engine is the only oracle), one paired client-and-engine cut rather than a floor set first, and a not-additive ledger entry. |
| RDR-222 request identity for mutating routes | Adjacent, accepted | Its run fence fields (index_run_id, index_content_hash, index_state, index_started_at) are catalog_documents columns, never chunk keys (RF-4). Unchanged. |
| RDR-192 superseded note chunks | Adjacent, shipped | Defines `live(c)` (a chunk some live manifest owns) and the reaper. Deletion and reaping never trust row metadata, which is why no reader here destroys data. Unchanged. |
| RDR-231 note source URI scheme | Adjacent, accepted (in flight) | Note identity moves to `nx-note://` in the catalog document's source_uri. It does not replace any per-row signal this RDR retires (see RF-8 and Phase 4 Step 1 for the note TTL lifecycle). Sequencing: independent. |
| RDR-232 nx_answer evidence sufficiency | Adjacent, accepted | Its research found the section-label defect (nexus-tko2w). It consumes labels through the manifest stage and is otherwise unaffected. Excluded from this effort by Sam. |

## Context

### Background

The defect class surfaced on 2026-10-24 when RDR-232 research found RDR-084
position 24 carrying the title of RDR-002, because the two share a chunk of
text. nexus-tko2w gave section labels a manifest home (2026-10-25);
nexus-3ajpd did the same for the staleness file hash; the x54wv census
(2026-10-26) enumerated every document key, its writers, 23 reader sites and
three filter routes, and the epic's owning session filed the remedy beads
from it with a three-wave plan (T2
`nexus/plan-6pfgb-shared-row-keys-2026-10-26`). Seventeen children closed
in one day. Sam's reading that evening: the plan is right and the per-reader
cadence is the wrong unit of work, because the row still invites the next
writer. This RDR turns the plan into a schema with an enforcement point and
maps every open child to a phase.

One consequence is live today. Since engine 0.1.165 deployed, a document
indexed with `nx index pdf --enrich` reads as unenriched to any bib_* filter,
because that path writes bib values to chunk rows only and the stage now
answers bib_* from the catalog columns (nexus-6pfgb.28). It is the most
urgent Phase 1 item.

### Technical Environment

Engine: nexus-service, Java, Postgres 17 with pgvector, Liquibase
changesets under `service/src/main/resources/db/changelog/`. Client pin
`REQUIRED_ENGINE_VERSION = (0, 1, 165)` (`src/nexus/engine_version.py`);
the next cut is engine-service-v0.1.166 (nexus-j67ix, tag on Sam's go).
Chunk tables `chunks_{384,768,1024}` by dimension, metadata merged with
`jsonb_delete` per delete key then `jsonb_concat` on upsert
(`PgVectorRepository.mergeMetadata`). Catalog tables `catalog_documents`
(meta merged with `jsonb_concat`, so an explicit empty value overwrites:
nexus-ke45f) and `catalog_document_chunks` (per position: chash,
chunk_index, char_start, char_end, line_start, line_end; since catalog-046
section_title, section_type, page_number, labels_written). Client resolves
chash to owners through `docs_for_chashes`, `chash_positions`,
`resolve_many` and `get_manifest`, all in engine 0.1.164 and later.

## Research Findings

### Investigation

Record of record is the census, two T2 entries read in full for this
draft: `nexus/x54wv-shared-row-key-census-2026-10-26 (1/2)` (writers and
catalog homes, develop d683a9739) and `(2/2)` (23 reader rows, three where
routes, remedy classes). Also read: the plan
`nexus/plan-6pfgb-shared-row-keys-2026-10-26` and its audit; the
content_hash census `nexus/3ajpd-content-hash-census-2026-10-25`; the
tko2w key split `nexus/tko2w-key-split-2026-10-25`; Sam's two rulings
(nexus-6pfgb.2, nexus-6pfgb.3) and the note-recovery ruling
(nexus-6pfgb.35 option B), all 2026-10-26; the critique records of the
closed children; the conexus answer on external readers. Engine-stamped
keys and position keys were censused for this RDR on 2026-10-26 against
develop f14fe0148 (RF-4, RF-5); owner-resolution cost was traced and timed
(RF-10). Each finding below is also a T2 record,
`nexus_rdr/236-research-1` to `-10`, with its sources. The draft was
critiqued once before Sam's gate decision (T2
`nexus/critique-rdr236-draft-2026-10-26`); this revision answers it.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus-service `PgVectorRepository.mergeMetadata` | Yes (census, RF-4) | `jsonb_delete` per delete key, then `jsonb_concat`; a key absent from the incoming row is kept; the engine adds no keys of its own on a normal upsert |
| nexus-service `CatalogRepository` meta update | Yes (nexus-ke45f, plan audit) | `jsonb_concat`; an explicit empty value overwrites, so homes send a key only with a real value |
| `ManifestLabelStage.java` | Yes (tko2w, 6pfgb.24, critique) | per-(document, position) expansion with fallback to the row when `labels_written` is false; DOC_KEYS = source_author, indexed_at, bib_* (authoritative); meta keys declared not answered; the search is cut at 4x the limit, ceiling 1200, before the stage (`OVERFETCH`), so a selective positive filter returns fewer rows than the limit |
| `VectorHandler` and `CatalogHandler` route tables | Yes (critique rounds 1 and 2) | `store-put` and `upsert-reference-only` answer 410 Gone (retired); live chunk-metadata writers are `upsert-chunks`, `update-metadata` and the combined manifest writes `append`, `append_many`, `write_many` (`CatalogHandler` ~178-180), the last used by `multi_batch_write` and `note_write` |
| hybrid search dispatch (`PgVectorRepository` ~3055-3200, vectors-031) | Yes (critique round 2) | a bounded probe (`SELECTIVE_GATE_MAX` = 5000) fetches the matching chash set; a small set is ranked exactly by `text_gated_search_by_chash_<dim>` (`chash = ANY`, a primary-key filter); a large set falls back to the HNSW-first plan. Route (b)'s `search_metadata_scoped_<dim>` is one HNSW-ordered join that can return short under a selective filter (`PgSession` ~608) |
| Liquibase catalog-046 | Yes | adds section_title, section_type, page_number, labels_written to `catalog_document_chunks`; additive |

### Key Discoveries

- **RF-1 (Verified, census part 1).** Every full-row writer goes through
  `make_chunk_metadata` except four hand-built dicts (`indexer.py` repo PDF
  path, `code_indexer`, `prose_indexer`, `db/t3.py put`) and two overlay
  writers (`commands/enrich.py` bib, `exporter.py` import). The `.nxexp`
  import writes the export file's chunk metadata verbatim onto the target
  row, so importing document X restamps rows document Y owns. The repo PDF
  dict still sets `doc_id` and the flat git keys (`indexer.py` ~3375).
- **RF-2 (Verified, census part 2).** 23 reader sites; none deletes or hides
  data on the strength of a row key (every destructive path is guarded by
  the engine's live-manifest check or the catalog owner resolve). Worst
  severity MEDIUM: provenance line in LLM prompts (row 1), backup export
  (row 7), note expiry (row 8), enrich copying one owner's bib onto another
  (row 10), orphan backfill by title (row 13), citation span and source_uri
  (row 21), extraction audit (row 22). Rows 1, 7, 8, 10, 13 closed by
  2026-10-26 evening (6pfgb.11, .14, .15, .21, .22); 21 and 22 open.
- **RF-3 (Verified, census part 2 section 4).** Three filter routes: (a)
  plain search, per-collection and hybrid, where the manifest stage exists;
  (b) the SQL functions `search_metadata_scoped_{384,768,1024}`, graph-hop
  and aspect functions, where document predicates join the catalog but the
  `where` part is `c.metadata @> p_where`, so a document passes if any of its
  rows matches; (c) `/v1/vectors/get` with where, tool-internal, fixed by
  selecting through the manifest instead.
- **RF-4 (Verified, sweep 2026-10-26 at f14fe0148; T2
  `nexus/rdr236-engine-and-position-key-census-2026-10-26`).** The engine
  stamps exactly four keys onto chunk metadata, all quarantine state:
  quarantined_at, origin_collection (the three quarantine functions in
  vectors-030), quarantined_by and reaper_quarantined_at (the reaper only).
  On a normal upsert it merges the client's JSON as sent and adds nothing.
  The RDR-222 fence fields, the tumbler work of catalog-025 and vectors-025,
  and lifecycle_state are catalog columns and never touch a row. The
  engine's own row columns (last_written_at, retention, collection,
  embedding_model) and the sibling table `chunk_orphaned_at` already keep
  row state outside the metadata. Legacy rows carry doc_id and chunk_index;
  the engine reads doc_id only as a manifest-less fallback in the census and
  reaper queries. Hygiene note: a gc restore removes quarantined_at and
  origin_collection but leaves quarantined_by and reaper_quarantined_at on
  the live row.
- **RF-5 (Verified, same sweep).** `catalog_document_chunks` already has a
  column for every position field; the engine writes all of them on the
  manifest write, and the client builds the manifest row from the chunk
  metadata dict (`mcp_infra._manifest_chunk_rows`, chunk_start_char mapped
  to char_start). Row readers that remain: the engine's `spanFromMeta`
  builds the search row's span from the row's line_start and line_end and
  reads chunk_text_hash from the row; `formatters.py` prints file:line from
  the row at five sites; `filters.py` lists line and char keys as row-side
  numeric where keys; the label fallbacks in `ManifestLabelStage`,
  `mcp/core.py` and `plans/runner.py` read the row when labels_written is
  false. chunk_type (the table_page marker) is not in `ALLOWED_TOP_LEVEL`,
  so `_normalize_partial` drops it before every write outside the taxonomy
  and quarantine collections; no reader exists; it is dead as written
  (inference from source, not run).
- **RF-6 (Documented, conexus 2026-10-26).** External readers listed in
  Gap 5. The raw-SQL recall leg breaks outright when category and tags leave
  the row; the engine leg depends on route (a) answering them from the
  catalog. Filed there as conexus-wd1q.
- **RF-7 (Decided, Sam 2026-10-26).** `title=` means the document's catalog
  title everywhere, the filename for code and prose; the per-position label
  stays display text (nexus-6pfgb.2, option A). `content_type=` maps by
  value: pdf and markdown answer from a catalog meta key `source_format`
  stamped at register and backfilled; code, prose, paper, rdr, knowledge
  from `catalog_documents.content_type` (nexus-6pfgb.3, option A). Both
  change what an existing request shape returns: `title=` on code and prose
  today matches the per-position label; `content_type=prose` from the
  catalog includes markdown files the row form excluded. The ledger entry
  for nexus-6pfgb.25 names both.
- **RF-8 (Verified, 6pfgb.27 comments and `note_ttl.py`).** Three readers
  depend on row keys Phase 4 retires: `note_ttl.row_written_by` (stamp AND
  title: is this row the owner's own write) is how a keyless legacy note's
  TTL, tags and category are read by the expiry sweep and the
  recovery-bundle export (6pfgb.15, .34); `note_ttl.catalog_ttl_stale` and
  `row_names_note` (6pfgb.30) refuse to retire a note whose rows record a
  put its catalog keys do not; and
  `tests/integration/test_rdr223_pdf_journey.py` asserts the shared-row
  title and must invert. On the live tenant (2026-10-26) the stamp-and-title
  rule recovered 419 of 427 keyless notes, and Sam chose option B on
  nexus-6pfgb.35: ttl_days gated on `row_written_by`, tags and category also
  accepted from a sole-live-owner row.
- **RF-9 (Verified, source search 2026-10-26).** Manifest label writers
  split in two: every indexer path builds its manifest rows through
  `mcp_infra._manifest_chunk_rows`, which always sends section_title,
  section_type and page_number (empty string for "none here"), so those
  rows are labels_written. Notes (`note_write.py`,
  `store_hook.note_manifest_metadata`), orphan backfill and manifest
  backfill send no label keys and leave their rows legacy, the last two
  deliberately, because the row's labels are the last writer's.
- **RF-10 (Verified, source search and spike 2026-10-26; T2
  `nexus/rdr236-a3-owner-resolution-batching-2026-10-26`).** Owner
  resolution is one SQL statement per chash list on the engine
  (`docsForChashes`, `chashPositions`, `resolveMany`, cap 1000). On the
  client, `search` resolves once per candidate window and
  `store_get_many` once per call; `query` repeats `docs_for_chashes` after
  `search_cross_corpus` already stamped the owners, the plan runner's chash
  path makes five calls with nothing shared, and `query`'s
  catalog-parameter text branch calls `resolve` and `get_manifest` inside
  its per-row loop. Timed laptop to the cloud engine: `chash_positions`
  0.09 to 0.23 s for 10 to 200 hashes; `docs_for_chashes` 0.2 to 1.05 s,
  because its flat document list forces a second `get_manifests` call. No
  local service numbers.

### Critical Assumptions

- [x] A1: the engine merge never retracts a key without `delete_keys` —
  **Status**: Verified — **Method**: Source Search (3ajpd, x54wv, RF-4).
- [x] A2: catalog meta merge is `jsonb_concat`; an explicit empty value
  overwrites — **Status**: Verified — **Method**: Source Search (nexus-ke45f).
- [ ] A3: every live reader can resolve chash to owners at page cost, not
  per hit — **Status**: Verified for `search` and `store_get_many`; not yet
  true for `query` (repeats the lookup), the plan runner's chash path (five
  calls) and `query`'s catalog-parameter branch (per row); Phase 2 makes it
  true by consuming rows already fetched (RF-10), and the box is ticked
  when those land — **Method**: Source Search plus a timed spike.
- [x] A4: `catalog_document_chunks` carries a column for every position
  field and the writers fill them on every path — **Status**: Verified
  (RF-5, RF-9): the columns exist and every indexer path sends the labels;
  notes, orphan backfill and manifest backfill send none and leave their
  rows legacy, which is what nexus-jz86a fixes for notes in Phase 1 —
  **Method**: Source Search.
- [ ] A5: no writer outside the nexus client sends document keys to chunk
  rows — **Status**: Unverified — **Method**: the engine's would-refuse log
  from Phase 4 Step 2a, read over a stated window, empty or every entry
  dispositioned (RDR-223's method; a search of the conexus repository is
  only a first look).
- [ ] A6: the parity gate's twelve filtered queries and the findings SQL can
  be rewritten against the catalog without changing what they measure —
  **Status**: Unverified — **Method**: Spike in conexus (conexus-wd1q)
  before Phase 4 Step 1.
- [ ] A7: the backfill can give every existing document its own values.
  Notes: by option B (RF-8): TTL from a stamp-and-title row whatever the
  owner count; tags and category from that row or from a piece row the
  note solely owns; residue is a note with neither (0 of 427 on the live
  tenant for tags and category; TTL residue is the 8 unstamped notes, none
  of which had a TTL).
  Indexed documents: tags, category and source_format are constants per
  content type and extension, so they need no row at all; extraction_* and
  frecency_score are copied from a row with exactly one live owner, and
  frecency is recomputable from git. Residue: an all-shared PDF's
  extraction provenance, which only a forced re-extraction restores (an
  unchanged document is skipped by the freshness check) — **Status**:
  Assumed; measured by the backfill's dry run (counts filled, derived,
  skipped, residue per kind) and its live run, both before Phase 4 Step 1,
  which needs the keyless-note count at zero.

## Proposed Solution

### Approach

One rule: a chunk row holds only what its text determines. Everything else
has exactly one other home, and the engine enforces the rule on write.

1. **A checked key table.** A table in `src/nexus/tables/` (the repo's
   convention for closed vocabularies) names every metadata key the system
   has ever written to a chunk row and gives it one home: CHUNK (determined
   by the text and the embedding), DOCUMENT (a catalog column or a named
   meta key), POSITION (a manifest column), ROW (engine state about the
   physical row), or LEGACY (dropped by an earlier RDR, still on old rows).
   `make_chunk_metadata`, `ALLOWED_TOP_LEVEL` and the engine's accepted-key
   set are derived from it, and a lint fails when any of the three
   differs. The table is the census of record; the T2 census entries become
   its history.
2. **Homes first, then backfill.** Every DOCUMENT key without a home gets
   one in catalog meta (tags, category, ttl_days, put_at, source_agent,
   extraction_method, quality_gate_overridden, extraction_source,
   frecency_score, source_format, the bib writer) or an existing column
   (markdown author), written at the seams that already update the
   catalog. A
   one-time, dry-run-first, fill-only verb stamps them for existing
   documents under the per-kind rules of A7 and reports counts. Position
   keys already have their manifest columns (RF-5); what moves is the
   readers.
3. **Readers move.** Every reader in the census takes DOCUMENT keys from
   the owning catalog document(s) and POSITION keys from the manifest row,
   resolving chash to owners once per page and reusing rows the same
   request already fetched (RF-10). When owners disagree or none resolves,
   the reader says nothing rather than a row value. Until a home is filled,
   a reader may fall back to the row value when the home is absent
   (**presence fallback**, the rule `labels_written` already follows); the
   one module allowed to do so is named in the key table's lint.
4. **The engine answers filters from the homes on every route.** Labels,
   author, indexed_at and bib_* already go through the manifest stage on
   route (a). The remaining DOCUMENT keys are selective (title names one
   document; content_type, tags and category can exclude most of a
   collection), and a stage that filters after the search is cut at 4x the
   limit cannot answer them without losing recall. So those keys are
   answered by a **pre-filter** built on the dispatch hybrid search already
   uses: resolve the qualifying documents from the catalog, collect their
   manifest chashes under a bound, rank a small set exactly by primary key
   (`chash = ANY`), and fall back to the index-first plan with the stage
   when the set is large (a dense filter). A hit is reported under the
   qualifying owner. The post-search stage keeps the labels and the span
   and source_uri re-pointing. Route (b)'s SQL functions get the same
   dispatch. Route (c) callers select by manifest.
5. **The engine refuses non-chunk keys on chunk writes.** A chunk write
   whose metadata carries any key outside the table's CHUNK rows is refused
   with 422 naming the key and its home, on `upsert-chunks`,
   `update-metadata` and the three combined manifest writes (`append`,
   `append_many`, `write_many`), checked in the one service they share
   (`CombinedWriteService`) and in the upsert path. `delete_keys` is
   never refused (it is how a client retracts a key it used to write, and
   how the final sweep works). Position fields travel in the manifest rows,
   which already have their columns; document fields travel on the catalog
   document. The check ships log-only first and enforces in a later paired
   cut, after the conexus gates read the catalog and the would-refuse log
   is empty (RDR-223's mechanics, Phase 4).
6. **Existing rows are swept when nothing reads them.** A stale value on an
   existing row is served wherever a reader's presence fallback finds no
   home (a legacy manifest label, an empty catalog author, a document the
   backfill could not fill). The sweep that removes the retired keys with
   `delete_keys` therefore has a measured precondition: `nx doctor` (the
   client's health report) counts zero legacy-label manifest rows, zero
   **keyless notes** (notes whose catalog meta has no tags, category or
   ttl_days because they were written before 6pfgb.5) and zero home-less
   documents (documents the backfill could not fill), or each residue is
   named and accepted by Sam. The orphan audit,
   which today labels an orphan by its row title, labels it by a chunk text
   prefix instead.

### Technical Design

#### The key table

Columns: key, home, location after normalization, writers (symbols),
readers after normalization, phase that moves it, notes. Chunk-intrinsic
keys stay: chunk_text_hash (the chunk's hex identity on the wire, RDR-180;
the engine reads it only to build the citation id today, and Phase 4 adds
the check that it equals the row's chash) and embedding_model (repeats a
row column; kept for the wire shape).

| Key | Home | Location after normalization | Moved by |
| --- | --- | --- | --- |
| chunk_text_hash, embedding_model | CHUNK | the row | stays |
| content_type (chunk vocabulary) | DOCUMENT | `catalog_documents.content_type` for code/prose/paper/rdr/knowledge; meta `source_format` for pdf/markdown (RF-7); falls back to the row while `source_format` is absent | 6pfgb.23, .25 |
| title | DOCUMENT | `catalog_documents.title` (RF-7); always present, so no fallback; the per-position label is display only | 6pfgb.25, .27 |
| source_author | DOCUMENT | `catalog_documents.author`; falls back to the row while empty (markdown: 6pfgb.7) | 6pfgb.7 |
| bib_year, bib_authors, bib_venue, bib_citation_count, bib_semantic_scholar_id, bib_openalex_id, bib_doi | DOCUMENT | the seven bib columns plus `bib_enriched_at` (authoritative; no fallback) | 6pfgb.28, .12 |
| bib_enriched_by (proposed by 6pfgb.32) | DOCUMENT | a catalog meta key beside the `bib_enriched_at` column (a meta key keeps Phase 1 client-only), never a row key; .32 is re-scoped | 6pfgb.32 |
| indexed_at | DOCUMENT | display and `where=` read `catalog_documents.indexed_at` (falls back to the row while NULL); the note TTL clock is meta `put_at`, the time of the note's own put (6pfgb.15), because the engine refreshes indexed_at on every manifest write | 6pfgb.13 |
| tags, category | DOCUMENT | meta (notes: 6pfgb.5 done; indexed documents: constants per content type and extension, 6pfgb.8) | 6pfgb.8, .10 |
| ttl_days, put_at | DOCUMENT | meta (6pfgb.5, .15 done; expiry per owner) | done |
| extraction_method, quality_gate_overridden, extraction_source | DOCUMENT | meta | 6pfgb.6, .16 |
| frecency_score | DOCUMENT | meta; scorer reads the attributed owner | 6pfgb.9 |
| source_agent | DOCUMENT | meta (notes done in .5) | 6pfgb.17 |
| session_id | none | retired; zero readers | 6pfgb.4 |
| catalog_doc_id (notes) | DOCUMENT | the manifest itself; the row stamp is read only by the note TTL rules until the backfill empties the keyless-note count (RF-8, Phase 4 Step 1) | 6pfgb.27 |
| content_hash (file hash) | DOCUMENT | `catalog_documents.index_content_hash` (3ajpd, done) | done |
| section_title, section_type, page_number | POSITION | `catalog_document_chunks` (catalog-046); notes write them too after jz86a | tko2w, jz86a, 1nrcu |
| line_start, line_end, chunk_start_char, chunk_end_char | POSITION | `catalog_document_chunks.line_start/line_end/char_start/char_end`; search re-points the citation span | 6pfgb.26, .19 |
| chunk_type (table_page) | none | dead: dropped by normalize before every write, no reader (RF-5); the writer is removed | 6pfgb.27 |
| quarantined_at, origin_collection, quarantined_by, reaper_quarantined_at | ROW | stay on the row; written only by the engine (RF-4); refused from a client write | stays |
| doc_id, chunk_index, chunk_count | LEGACY | dropped by RDR-108 Phase 3; `indexer.py` ~3375 still sets doc_id in the repo PDF dict (Phase 0 settles whether normalize strips it on that path); the engine reads doc_id only as a manifest-less fallback; `chunk_index` lives on as the manifest column | sweep |
| source_path | LEGACY | dropped by RDR-102 D2; still read as an absent key by `enrich.py`, `commands/catalog.py`, `aspect_readers`, `exporter` and the engine's ownership log; those reads are removed in Phase 2 | sweep |
| store_type, corpus, git_meta, git_project_name, git_branch, git_commit_hash, git_remote_url | LEGACY | dropped by RDR-101 Phase 5c; the repo PDF dict still sets the flat git keys (`indexer.py` ~3376), which Phase 0 removes | sweep |
| RDR-222 fence fields, lifecycle_state, tumbler | not row keys | catalog_documents and catalog_collections columns (RF-4); listed so no one looks for them on the row | n/a |

The site-level detail behind the table (every writer and reader by file
and line, the `nexus.chunks` and manifest DDL history) is in the two census
records; the Phase 0 module carries symbols, not line numbers.

Two inconsistencies in today's key set, for Phase 0: `ManifestLabelStage`
answers bib_openalex_id and bib_doi, which `ALLOWED_TOP_LEVEL` does not
contain (the enrich writer sends them through `update-metadata`,
unvalidated); and the table's bypass collections (`taxonomy__*`,
`quarantine-*`, which `db/t3._BYPASS_SCHEMA_PREFIXES` exempts from
normalize today) stay exempt from the allow-list, named as such in the
table and the engine check.

#### Enforcement at the engine (Phase 4)

`upsert-chunks`, `update-metadata` and the combined manifest writes
(`append`, `append_many`, `write_many`, all through `CombinedWriteService`)
validate each incoming metadata object against the engine's copy of the key
table:
any key not in the CHUNK set, for a collection outside the bypass prefixes,
returns 422 `chunk_metadata_key_refused` with the key and its home named.
`delete_keys` is not inspected. Position fields in the manifest rows and
document fields on the catalog document are accepted as today. The key set
lives in Java beside `ManifestLabelStage.LABEL_KEYS` and is generated from
the Python table (one source, never two hand-kept copies). The check has a
log mode: it records the would-be-refused key, route and client version
without refusing, so the window before enforcement shows every writer the
client census could not see.

#### The pre-filter for selective document keys (Phase 3)

Route (b)'s `search_metadata_scoped_<dim>` is not the model: it is one
join ordered by the HNSW index, and under a selective filter the index
scan can exhaust its tuple budget and return short or empty (`PgSession`,
the nexus-bq06h record). Hybrid search already solved this shape
(`PgVectorRepository` hybrid dispatch, `SELECTIVE_GATE_MAX` = 5000): a
bounded probe fetches the chash set that satisfies the gate; a set under
the bound is ranked exactly by `text_gated_search_by_chash_<dim>`, a
primary-key `chash = ANY` filter; a set over the bound (a dense filter
that most of the collection satisfies) falls back to the HNSW-first plan.
The pre-filter reuses that dispatch with the catalog as the gate: resolve
the qualifying documents from `catalog_documents` and its meta, collect
their manifest chashes up to the bound, and dispatch. A hit from a shared
chash is reported under the qualifying owner (the manifest row that
admitted it), never under an owner the filter excluded. Fallback rules
per key, applied in the resolution: title has none (the catalog title
always exists); content_type=pdf or markdown admits a document whose
`source_format` is absent when any of its rows carries the row value (the
same presence fallback as the stage, paid once per candidate document);
meta keys fall back to the row the same way while the key is absent.
Labels, author, indexed_at and bib_* stay with the post-search stage,
where they already are. Pins: `title=` in a collection larger than 1200
chunks returns the document's hits; a dense filter (category=prose on a
prose collection) returns the same page as no filter; a shared chunk is
attributed to the qualifying owner. Cost: one catalog query and one
manifest chash fetch before the search, bounded by `SELECTIVE_GATE_MAX`,
measured on the parity gate's twelve filtered queries.

#### Splitting the chunker output (Phase 4, before Step 1)

`_manifest_chunk_rows` builds the manifest row from the same dict that
becomes the chunk row. Before any writer stops emitting document and
position keys, the chunkers return two dicts, chunk fields and position
fields; the manifest builder reads the position fields and
`make_chunk_metadata` takes only the chunk fields. Without this, Step 1
would write manifest rows with empty labels and NULL spans marked
labels_written, an authoritative loss that never falls back.

#### The backfill verb (Phase 1)

nexus-6pfgb.10, with A7's per-kind rules, which are Sam's option B
(nexus-6pfgb.35) per key. Dry run by default. Notes: ttl_days and `put_at`
(the note's own put time, the TTL clock since 6pfgb.15) are taken only
from a row that `row_written_by` accepts (stamp AND title), `put_at` from
that row's indexed_at; tags, category and source_agent are taken from
that row, or, when there is none, from a piece row whose chunk has the
note as its sole live owner; a note with neither is counted residue per
key (on the live tenant, 2026-10-26, the 8 notes with no stamped row were
all sole owners with no ttl_days, so option B recovers every one).
Indexed documents: tags, category and source_format are derived from
content_type and extension; extraction_* and frecency_score are copied
from a chunk with exactly one live owner; a document with none is counted
residue. Fill-only: never overwrite an existing meta key. The run asserts
it examined at least one document; the first live run's counts go on the
bead. The backfill, the expiry sweep and the export share one helper that
implements option B (`row_written_by` for the TTL, the sole-live-owner
rule for tags and category), so the three agree.

### Existing Infrastructure Audit

Already built and reused: `ManifestLabelStage` and its presence fallback;
`docs_for_chashes`, `chash_positions`, `resolve_many`, `get_manifest`;
`_resolve_display_maps` (batched owner resolve per result page);
`catalog_store_hook_tracked` (the seam that writes note meta);
`_catalog_pdf_hook` and the markdown register call (the seams for
extraction meta and author); `note_ttl.row_written_by`; the catalog
backfill verbs beside which .10 sits; `rewrite_delete_keys`; route (b)'s
catalog-joined SQL functions as the pre-filter's model; the wire ledger and
RDR-223's log-then-enforce mechanics; `src/nexus/tables/` checked tables.

### Decision Rationale

Content addressing is the right identity for a chunk: it is what makes
re-indexing cheap, citations stable and deduplication automatic (RDR-108,
RDR-180). The cost of that identity is that a row cannot say anything about
a document, because it may belong to several. The system has two other
records that can: the catalog document, which is per document by
construction, and the manifest, which is per (document, position) by
construction. Every document key already has or can cheaply get a home in
one of them, so what this RDR moves is the fields, not the identity.
Enforcing the move at the engine is what makes it stick: RDR-223 showed
that a client-side rule about what may be written is reopened by the next
path, and that an engine refusal, shipped log-only first and enforced when
the log is clean, closes it.

## Alternatives Considered

### Alternative 1: Keep moving readers one bead at a time

**Pros**: no engine change; every step is small and independently shippable;
it is what the epic has been doing successfully.
**Cons**: the row still accepts the keys, so each new writer, route or
importer reopens the class; no end state is enforced.
**Reason for rejection**: this is the bleeding Sam asked to stop. The
reader moves are kept as Phase 2; what is rejected is stopping there.

### Alternative 2: Per-document chunk rows

Give up sharing: key the row by (document, position) so each document owns
its metadata.
**Pros**: every row field is unambiguous.
**Cons**: undoes RDR-108 and RDR-180; storage and embedding cost multiply by
the number of owners; `chash:` citations stop naming one row; every reaper,
live-manifest check and manifest function changes.
**Reason for rejection**: the identity is right; the metadata placement is
wrong. Fix the placement.

### Alternative 3: Per-owner maps on the row

Keep the keys on the row but nest them under the owning document's id.
**Pros**: no new homes.
**Cons**: unbounded row growth with owners; the manifest already is that
map, kept by the engine with referential integrity; filters would need
JSON-path queries over nested maps on every route; the catalog document
would hold a second copy of the same facts.
**Reason for rejection**: it rebuilds the manifest inside a JSON column.

### Alternative 4: Answer selective document filters in the post-search stage

Extend `ManifestLabelStage` to title, content_type, tags and category.
**Pros**: one mechanism; already shipped for labels and bib_*.
**Cons**: the stage filters after the search is cut at 4x the limit
(ceiling 1200), and its own documentation names the cost: a positive
filter that rejects most of the neighbourhood returns fewer rows than the
limit. `title=` names one document; in a large collection its chunks are
rarely among the 1200 nearest, so the filter returns nothing where it
returns hits today.
**Reason for rejection**: recall loss on the most selective filters, and
the parity gate's category queries would show it at the first redeploy.
The pre-filter is the remedy.

### Briefly Rejected

- **Client-only discipline without an engine refusal**: RDR-223 Gap 3 is
  the recorded case against it.
- **A deny-list of document keys**: the next new key passes it (Phase 1's
  own bib_enriched_by proposal would have). An allow-list of CHUNK keys is
  what closes Gap 3.
- **Dropping the keys from existing rows before readers and homes are
  complete**: breaks every presence fallback; the sweep is last and has a
  measured precondition.

## Trade-offs

### Consequences

- Positive: one enforced schema; every reader and filter is per document
  by construction; the epic's remaining children become a finite, ordered
  list; backups, expiry and provenance lines become true.
- Positive: `make_chunk_metadata` shrinks to identity and embedding
  provenance; four hand-built dicts disappear.
- Negative: Phase 4's enforcement is not-additive: a client older than the
  paired cut gets 422 on chunk writes after the engine deploys. Mitigated
  as RDR-223 did: log-only first, enforce when the log is clean, one paired
  client-and-engine cut, ledger entry written not-additive.
- Negative: an all-shared PDF's extraction provenance is not recoverable
  without a forced re-extraction (A7); the dry run counts them and Sam
  decides whether to re-extract or accept the residue.
- Negative: external gates in conexus must change before clients stop
  writing the keys (Phase 5 before Phase 4 Step 1).

### Risks and Mitigations

- **Risk**: owner resolution on the search hot path costs a round trip per
  hit. **Mitigation**: `search` already resolves once per window (RF-10);
  Phase 2's `query` and runner beads consume those rows instead of
  re-asking, and the per-row `resolve` in `query`'s catalog-parameter
  branch is batched before any new reader lands there.
- **Risk**: a meta write with an empty value erases a sibling key
  (jsonb_concat). **Mitigation**: homes send a key only with a real value;
  a test writes an empty value and asserts the sibling survives.
- **Risk**: the Java and Python copies of the key table drift.
  **Mitigation**: one is generated from the other; a pin fails when they
  differ.
- **Risk**: a selective filter loses recall when answered after the
  overfetch cut. **Mitigation**: the pre-filter (Alternative 4 rejected);
  the recall pin on `title=` in a large collection; the parity gate's
  category queries compared across the deploy.
- **Risk**: a writer the client census cannot see (a subprocess, a shell
  script, a Java client) is refused at enforcement. **Mitigation**: the log
  mode window before enforcement (A5).
- **Risk**: a note re-put by a new client that writes no row keys leaves
  the old put's TTL on the row, and `catalog_ttl_stale` then holds the note
  forever. **Mitigation**: the lifecycle in Phase 4 Step 1 and its pin.
- **Risk**: a reader that still trusts the row survives the sweep
  undetected. **Mitigation**: the lint that forbids reading a non-CHUNK key
  from `metadata` outside the legacy-fallback module; the doctor row for
  rows still carrying retired keys.

### Failure Modes

A refused write is visible: 422 with the key named; the client logs it and
the indexer marks the document failed, as it does for other 4xx. In log
mode nothing is refused and the log row names the writer. A reader that
cannot resolve an owner says nothing (no provenance line, no filter match)
rather than a wrong value; `nx doctor` counts unresolved chashes. A
backfill that finds nothing to fill fails its non-vacuity assert. A
diverged key table fails CI.

## Implementation Plan

### Prerequisites

- [ ] The epic owner's in-flight children (.35, .36, .37, .33) pushed; they
      are adjacent and keep their beads.
- [ ] The dry run and the live run of the backfill (A7) read by Sam before
      Phase 4 Step 1.

### Minimum Viable Validation

One integration module on the live-engine fixture, two fixtures, because
notes and indexed files carry different keys.

Pair 1, two indexed markdown files A and B sharing one chunk, with different
titles, tags, section labels and line spans, B written last. Under each
document: `where=` on tags and title (red today: evaluated on the row), on
section_type (green today on labels_written rows, kept as a regression
pin), returns the hit for the satisfying owner only; the citation span and
source_uri are the searched document's (red today); the provenance line and
store_get name that document's own values (green since 6pfgb.11; kept).

Pair 2, two notes with the same text, different ttl_days, tags and
category, B written last. The recovery-bundle export carries each note's
own values (green since .14; kept); `nx store expire` retires only the
lapsed owner and keeps the chunk (green since .15; kept); a re-put of A by
a client that writes no row keys, changing its TTL, still expires on time
(red after Phase 4 Step 1 without the lifecycle fix).

Refusal legs: an `upsert-chunks` carrying `title` is refused with 422
naming the key and its home; the same content with `title` on the catalog
document and `line_start` in the manifest row of the combined write
succeeds; an `update-metadata` carrying `bib_doi` is refused; a
`delete_keys` naming `title` is accepted; an `.nxexp` import restamps no
document key on a row another document owns; the backfill dry run reports
counts and stamps nothing. All red on develop today except the delete_keys
acceptance.

### Phase 0: The key table

The checked table (`src/nexus/tables/chunk_metadata_keys.py`) with every
row of the table above, a test that `ALLOWED_TOP_LEVEL` and
`make_chunk_metadata`'s output equal its CHUNK, DOCUMENT and POSITION rows
today (so the table is true before anything moves), a generated Java key
set with a pin, and a lint that a `metadata[...]` or `metadata.get(...)`
read of a non-CHUNK key outside the legacy-fallback module is a finding
(ratchet: current sites listed, count may only fall). Settles the two
inconsistencies above and whether normalize strips `doc_id` and the git
keys on the repo PDF path. nexus-x54wv, the census of record, closes when
this lands.

### Phase 1: Homes and backfill (client-only, safe on 0.1.165)

- The bib fix first: `nx index pdf --enrich` writes the catalog bib columns
  (nexus-6pfgb.28), the live regression named in Background.
- Extraction provenance in meta (6pfgb.6); markdown author into the
  catalog author column (.7); indexed documents' tags and category in meta
  (.8); frecency_score in meta (.9); source_format at register (.23); the
  bib writer recorded on the catalog document, not the row (.32,
  re-scoped); notes write label keys on their manifest rows (jz86a);
  labels backfilled for single-owner chashes written before catalog-046
  (1nrcu).
- The backfill verb (.10), after the homes above, with the first live
  run's counts on the bead.

### Phase 2: Readers to the catalog and the manifest (client-only)

- Doc card and quality boost read the owners' bib (.12), consuming the
  entries `search_cross_corpus` already resolved rather than the duplicate
  `docs_for_chashes`, and batching the per-row `resolve` in the
  catalog-parameter branch (RF-10); store_get and store_list display from
  the catalog entry (.13); extraction readers and the degraded-document
  audit (.16); the contradiction flag's source_agent (.17); citation span
  resolve (.19); link boost over every owner (.20); ambiguous owners
  distinguishable from unstamped (sx1t0); the export race (6pfgb.1); the
  plan runner's chash path reuses its `store_get_many` rows (RF-10).
- The dead `source_path` reads are removed (key table, LEGACY row).

### Phase 3: The engine answers from the homes on every route (the engine cut that carries each bead, 0.1.166 or later)

- `title=` and `content_type=` by the pre-filter under Sam's rulings (.25,
  re-scoped from the post-search stage its text names to the pre-filter;
  ledger entry naming both behaviour changes); span and source_uri from one
  owner, deterministic and collection-scoped, `spanFromMeta` reading the
  manifest row (.26); the meta keys by the pre-filter once Phase 1 writes
  them and the backfill has run (.29, re-scoped the same way); route (b)
  SQL functions and route
  (c) get (u9dc5); a cap on per-hit expansion (6cmam); an old-client
  manifest rewrite must not reset labels_written (3xjlq); quarantine
  reattach reads the owner's file hash (j67ix.1).
- Each entry additive in the wire ledger. Client halves gate on a
  capability check (the client asks the deployed engine whether it has
  the route or column, rather than trusting the version pin).
- The tko2w client half lands here, after .26 is deployed: `formatters.py`
  and the label fallbacks in `mcp/core.py` and `plans/runner.py` read the
  manifest span and labels; `filters.py` stops listing line and char keys
  as row-side keys.

### Phase 4: Refuse, retire, sweep (not-additive, one paired cut)

#### Step 0: Split the chunker output

As in Technical Design: chunk fields and position fields are separate
dicts before any writer changes. Pin: after a re-index, the manifest
labels and spans equal their pre-change values.

#### Step 1: Clients stop sending, with the note TTL lifecycle

Lands only after Phase 5 (the conexus gates read the catalog) and after the
backfill's keyless-note count is zero or its residue accepted. session_id
stops now (6pfgb.4); import stops merging document keys (.18);
`make_chunk_metadata` and the hand-built dicts emit CHUNK keys only,
`ALLOWED_TOP_LEVEL` shrinks to the table's CHUNK rows, the chunk_type
writer and the repo PDF dict's legacy keys go, the overlay writers through
`update-metadata` stop sending document keys (the extraction post-pass's
title and provenance, the frecency-only reindex's score; both now write
the catalog), and `find_ids_by_title` reads the catalog first (.27, whose
floor text is updated). Note TTL: `row_written_by` and the
export's row path are deleted, not replaced, because every keyless note is
now filled; `catalog_ttl_stale` stays until enforcement (old clients exist
until then). Its rule for a frozen row: a row is ignored when it is older
than the catalog's `put_at` AND it was written by the engine's own clock
on this tenant (row `last_written_at` before the catalog write), which
excludes the two cases round 2 named: an `.nxexp` import copies the
source box's indexed_at verbatim, so its row can look old while its
`last_written_at` is fresh and it is still caught; an old-client re-put
stamps indexed_at now (`note_write`) and is caught as before. An entry
with no `put_at` is never treated as newer than its rows. Pins: new-client
TTL change expires; import that makes a note permanent is not retired;
old-client re-put is still caught. The RDR-223 journey test's title
assertion inverts. The client pin moves to the engine tag that carries
.26 and .29 (several Phase 3 cuts may precede it).

#### Step 2: The engine refuses

2a: the allow-list check ships in log mode in an engine cut. 2b: the
would-refuse log is read over a stated window (two weeks of normal use,
including an index run on every client kind); every entry is a client to
upgrade or a key to classify. 2c: enforcement ships in one paired cut with
the client release that raises the pin to it (Step 1's client already
stopped writing), the ledger entry written not-additive, conexus armed,
in the deploy order of RDR-223 Phase 3.
`catalog_ttl_stale` is deleted here.

#### Step 3: Sweep (optional, per tenant, measured precondition)

A `delete_keys` sweep over existing rows removing the retired keys, dry
run first, counts on the bead, only when the doctor counts of legacy-label
manifest rows, keyless notes and home-less documents are zero or each
residue is accepted. The orphan audit's human label becomes a chunk text
prefix before the sweep removes row titles. A doctor row counts rows still
carrying retired keys, not-applicable on a virgin box.

### Phase 5: External readers (conexus repository, after the Phase 1 backfill and the .29 deploy, before Phase 4 Step 1)

conexus-wd1q (filed 2026-10-26, gated ahead of the engine redeploy that
follows Step 1). Lower bound: the catalog must hold tags and category for
every document the gate's queries can hit, so Phase 5 lands after the
backfill has run on the gated tenant and the .29 engine is deployed; its
recall SQL applies the same presence fallback as the engine (row value
while the meta key is absent). Parity gate: the engine leg keeps its where strings (route
(a) answers them from the catalog after Phase 3); the recall leg's raw SQL
joins `catalog_document_chunks` and `catalog_documents` for category and
tags. `livec_findings.sql` and `owner_1_1_census.py` read title and
indexed_at from the catalog. Rehearsal census predicates on quarantine keys
are unchanged (ROW keys). A6's spike runs here.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| catalog meta keys (tags, category, ttl_days, put_at, source_agent, extraction_*, frecency_score, source_format) | In scope (`nx catalog show`) | In scope | N/A (document lifecycle) | In scope (doctor: documents missing a home key after backfill, keyless notes) | In scope (recovery bundle exports them, 6pfgb.14) |
| the key table and its generated Java set | In scope (the module) | In scope | N/A | In scope (CI pin) | git |
| the would-refuse log | In scope (engine route or SQL view) | In scope | Ages out | In scope (empty before 2c) | N/A |
| retired keys on existing rows | In scope (doctor count) | N/A | In scope (Step 3 sweep, dry run first) | In scope (doctor row) | N/A |

### New Dependencies

None.

## Test Plan

- **Scenario**: pair 1, filter on tags and title under each owner —
  **Verify**: only the satisfying owner's hit, on routes (a) and (b); red
  today.
- **Scenario**: `title=` in a collection of more than 1200 chunks —
  **Verify**: the document's hits are returned (the pre-filter); red under
  Alternative 4.
- **Scenario**: pair 1, citation address — **Verify**: span and source_uri
  are the searched document's, identical across runs; red today.
- **Scenario**: backfill dry run on a tenant with stamped notes, keyless
  notes and indexed documents — **Verify**: counts filled, derived, skipped,
  residue per kind; a second run stamps nothing; non-vacuity assert fires
  on an empty tenant.
- **Scenario**: meta write with an empty value — **Verify**: sibling keys
  survive.
- **Scenario**: Step 0 re-index — **Verify**: manifest labels and spans
  unchanged.
- **Scenario**: pair 2, re-put by a new client changing the TTL —
  **Verify**: expires on time; `catalog_ttl_stale` ignores the frozen row.
- **Scenario**: `upsert-chunks` and `update-metadata` with a non-CHUNK key,
  in log mode and enforced — **Verify**: log row, then 422 naming key and
  home; `delete_keys` accepted in both; a bypass-prefix collection
  unaffected.
- **Scenario**: Java key set versus Python table — **Verify**: pin fails when
  one changes alone.
- **Scenario**: lint ratchet — **Verify**: a new `metadata["title"]` read
  outside the fallback module is a finding; the listed sites count may only
  fall.
- **Scenario**: conexus parity gate across the Phase 3 and Phase 4 deploys
  — **Verify**: the twelve filtered queries return the same documents as
  before (conexus-wd1q).

## Validation

### Testing Strategy

Tests-first per bead (red on develop, named in each bead; a pin that
passes with the fix deleted proves nothing). The MVV above is the
end-to-end proof and is in scope for Phase 4. Each engine entry has a Java
test and an integration-group test; the refusal has a positive control (an
accepted CHUNK-only write) beside each refused leg.

### Performance Expectations

Owner resolution is already paid on `search` (two batched calls per
candidate window, 0.1 to 0.3 s laptop to cloud, RF-10); Phase 2 adds no
call there and removes the duplicates on `query` and the runner. The
pre-filter adds one catalog query before the vector search for a selective
`where=`; its cost is measured on the parity gate's twelve filtered queries
across the Phase 3 deploy, not estimated here.

## Finalization Gate

### Contradiction Check

To be written at gate.

### Assumption Verification

A1 to A4 verified; A5 by the would-refuse log at Phase 4 Step 2b; A6 by
conexus-wd1q's spike; A7 by the backfill dry run.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/vectors/upsert-chunks`, `update-metadata` metadata merge | nexus-service | Source Search |
| `catalog_documents.metadata` update | nexus-service `CatalogRepository` | Source Search |
| `docs_for_chashes`, `chash_positions`, `resolve_many` | nexus-service (0.1.164) | Source Search, Spike |

### Scope Verification

The MVV is the Phase 4 integration module and is not deferred.

### Cross-Cutting Concerns

- **Deployment model**: Phase 4 is not-additive, deployed in the RDR-223
  Phase 3 order: log mode, clean log, one paired cut with conexus armed.
- **Incremental adoption**: Phases 0 to 3 are additive and ship with the
  epic's existing cadence.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: the backfill verb pages by document.

### Proportionality

Right-sized for a change that touches every writer, 23 readers, three
routes and one external repository. The key table is the section future
readers need; the phase lists exist to map 33 open beads and may be trimmed
to bead ids at lock.

## References

- T2 `nexus/x54wv-shared-row-key-census-2026-10-26 (1/2)`, `(2/2)`;
  `nexus/plan-6pfgb-shared-row-keys-2026-10-26`;
  `nexus/3ajpd-content-hash-census-2026-10-25`;
  `nexus/tko2w-key-split-2026-10-25`;
  `nexus/rdr236-engine-and-position-key-census-2026-10-26`;
  `nexus/rdr236-a3-owner-resolution-batching-2026-10-26`;
  `nexus/chunk-row-normalization-external-readers-2026-10-26`;
  `nexus/critique-rdr236-draft-2026-10-26`; `nexus_rdr/236-research-1`
  to `-10`.
- `docs/agents/catalog-t3-split.md`; `docs/architecture.md`;
  `docs/wire-contract-pending.md`.
- `service/src/main/resources/db/changelog/catalog-046-manifest-section-labels.xml`;
  `ManifestLabelStage.java`; `VectorHandler.java`.
- RDR-108, RDR-180, RDR-192, RDR-222, RDR-223, RDR-231, RDR-232.
- Beads: nexus-6pfgb and children; nexus-tko2w; nexus-u9dc5; nexus-j67ix;
  conexus-wd1q.

## Revision History

- 2026-10-26: created from Sam's direction and the x54wv census.
- 2026-10-26: confirmation critique (T2
  `nexus/confirm-critique-rdr236-draft-2026-10-26`) found three leftovers,
  fixed here: the `write_many` combined route is in the refusal; the
  pre-filter is modelled on hybrid search's bounded chash-set dispatch, not
  route (b); the backfill applies option B per key; plus the import case of
  the frozen-row rule, Phase 5's lower bound, the `update-metadata` overlay
  writers in Step 1, and register terms.
- 2026-10-26: revised after the pre-gate critique (T2
  `nexus/critique-rdr236-draft-2026-10-26`): refusal becomes an allow-list
  on the live routes with log-then-enforce; key table gains LEGACY rows and
  one home per key; Phase 3 selective filters move to a pre-filter and the
  6pfgb.24 facts are corrected; A7 restated per kind under Sam's .35
  ruling; note TTL lifecycle, chunker split, phase order, MVV fixtures and
  register terms fixed.
