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
related_issues: [nexus-6pfgb, nexus-x54wv, nexus-tko2w, nexus-u9dc5, nexus-3ajpd, nexus-j67ix, nexus-p89h4]
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
column on the chunk row. On `upsert-chunks` and `update-metadata`, and on
the combined manifest writes when the client sends `metadata_merge`
(every chunk-carrying combined write of the current client does, directly
or through its `MetadataMergingCatalog` wrapper, RF-22), the engine merges
it so the newest write wins
per key and no key is removed by a write that omits it:
**last-writer-wins**. A combined write without `metadata_merge` replaces
the row's metadata wholesale (RF-18). Two further terms: the **wire ledger**
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
hit carries is already text-determined (`spanFromMeta` emits
`chash:<hex>` whenever the row carries chunk_text_hash; the line form is
only a fallback), but the flattened line_start and line_end fields on the
search row, the file:line that `formatters.py` prints from them, and the
source_uri come from the row or from an arbitrary owner (census part 2,
row 21; schema analysis section 7). Notes and the two backfill verbs write
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
today they have no catalog home to answer from; and because the stage
filters after the search is cut at 4x the limit, the id-valued bib_*
filters (`bib_doi`, `bib_openalex_id`, `bib_semantic_scholar_id`), each
naming one document in practice (the catalog does not enforce uniqueness
on them, RF-22), now return short where they ran in-index before 0.1.165
(the stage's cut, RF-21; the pre-0.1.165 in-index evaluation is RF-3's
route (a) description). The metadata-scoped,
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
title and indexed_at from the row (T2
`nexus/chunk-row-normalization-external-readers-2026-10-26`, 2026-10-26;
RF-12, RF-13). One conexus tool also writes: the gate's copy tool
`etl_t3.py` inserts chunk rows with their source metadata verbatim, so it
would be refused in Phase 4 unless it strips the retired keys. Every
engine redeploy runs the parity gate, so those gates must read the catalog
before clients stop writing the keys, or the next redeploy, related or
not, undercounts.

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
One chunk table `nexus.chunks` (RDR-191 unified the per-dimension
tables; vectors-004), partitioned by embedding_model then tenant
(vectors-030), declared primary key (tenant_id, collection, chash,
embedding_model), with a foreign key to `catalog_collections` that makes
collection determine embedding_model; metadata merged with `jsonb_delete`
per delete key then `jsonb_concat` on upsert
(`PgVectorRepository.mergeMetadata`); the combined manifest writes merge
only under `metadata_merge` and otherwise replace the row's metadata
(RF-18). Catalog tables `catalog_documents`
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
`nexus_rdr/236-research-1` to `-24`, with its sources (RF-14, option B as
implemented, and RF-15, the schema analysis record, are T2-only; their
substance is in RF-8 and the Technical Design). The draft was
critiqued once before Sam's gate decision (T2
`nexus/critique-rdr236-draft-2026-10-26`); this revision answers it.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus-service `PgVectorRepository.mergeMetadata` | Yes (census, RF-4, RF-18) | `jsonb_delete` per delete key, then `jsonb_concat`; a key absent from the incoming row is kept on `upsert-chunks`, `update-metadata` and a combined write under `metadata_merge`; a combined write without it replaces the row (`batchUpdateMetadata`, `deleteKeys == null`); the engine adds no keys of its own on a normal upsert |
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
- **RF-2 (Verified, census part 2).** 23 client reader sites; none deletes
  or hides data on the strength of a row key (every destructive path is
  guarded by the engine's live-manifest check or the catalog owner
  resolve). The engine's own row-key readers are RF-19. Worst
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
  the engine reads doc_id and catalog_doc_id as the manifest-less census's
  forward attribution, and title and source_path for display in `/reapable`
  and the reaper's refusal audit (RF-19; moved by 6pfgb.39 before the
  sweep). Hygiene note: a gc restore INSERTs the quarantined row back into
  its origin collection with its metadata minus quarantined_at and
  origin_collection, leaving quarantined_by and reaper_quarantined_at on
  the live row (fixed by 6pfgb.40 before the quarantine CHECK lands) and
  carrying every other key the row had (RF-23), which is why the Step 3
  sweep strips retired document keys inside the quarantine collections
  too.
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
- **RF-6 (Documented, conexus 2026-10-26; confirmed by RF-12 and RF-13).**
  External readers listed in Gap 5. The raw-SQL recall leg breaks outright
  when category and tags leave the row; the engine leg depends on route
  (a) answering them from the catalog. Filed there as conexus-wd1q.
- **RF-7 (Decided, Sam 2026-10-26).** `title=` means the document's catalog
  title everywhere, the filename for code and prose; the per-position label
  stays display text (nexus-6pfgb.2, option A). `content_type=` maps by
  value: pdf and markdown answer from `source_format`, a GENERATED column
  on `catalog_documents` derived from content_type and the file extension
  (the ruling named a stamped meta key; RF-17 and the schema analysis
  superseded that with the generated column); code, prose, paper, rdr,
  knowledge from `catalog_documents.content_type` (nexus-6pfgb.3,
  option A). Both
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
  title and must invert. On the live tenant (2026-10-26) the .35 census counted 432 keyless
  notes: 427 with row values, of which the stamp-and-title rule recovered
  419 and option B the other 8; the 4 ghosts with no manifest and 1 note
  with an empty row hold nothing to recover and are residue for Sam's
  acceptance at the .10 dry run (no TTL at stake). The 432 was counted
  under the any-of-five definition; the dry run re-counts under the
  adopted one (no `put_at`), where pre-`.18` imports also appear. Sam
  chose option B on nexus-6pfgb.35: ttl_days gated on `row_written_by`; tags and category also
  accepted from a sole-live-owner piece row unless that row's stamp and
  title both name another note (`note_ttl.row_may_give_tags`, landed with
  .35 and .36).
- **RF-9 (Verified, source search 2026-10-26).** Manifest label writers
  split in two: every indexer path builds its manifest rows through
  `mcp_infra._manifest_chunk_rows`, which always sends section_title,
  section_type and page_number (empty string for "none here"), so those
  rows are labels_written. Notes (`note_write.py`,
  `store_hook.note_manifest_metadata`), orphan backfill and manifest
  backfill send no label keys and leave their rows legacy, the last two
  deliberately, because the row's labels are the last writer's.
- **RF-12 (Verified, source search of the conexus repository head on 2026-10-26; its sha is in research-12).**
  No executable caller of `upsert-chunks`, `update-metadata` or the
  combined manifest writes exists in conexus. Direct SQL on
  `nexus.chunks`: `deploy/gate/etl_t3.py` (a live gate and migration copy
  tool) inserts rows with the source's metadata verbatim, so document keys
  pass through; the gc purge fixture writes quarantine keys or nothing;
  the rehearsal scripts update the collection column only; the legacy ETL
  has no metadata column; the reseed script rides the nexus client. No
  `jsonb_set` or metadata concatenation outside tests. Readers of document
  keys: `livec_findings.sql` (title, indexed_at) and the recall leg of the
  parity gate.
- **RF-13 (Verified, source search in both repositories, 2026-10-26).** The
  twelve filtered parity queries each carry one equality filter on an
  indexer constant: category=paper (four, the DEVONthink papers
  collection), category=code (two), tags=js (one), category=prose (four),
  tags=pdf (one). None uses a user note value. Ten rewrite to catalog
  content_type or the collection scope; the two tag filters read the
  GENERATED tags column of indexed files (.8, decided after RF-17). The
  engine leg forwards the
  clause unchanged (`run_gate.py` ~2029, `warmup.py` ~70) and the oracle
  is recaptured; the recall leg (`_where_clause_parts` ~3848, pool count
  ~4101) becomes a join through `catalog_document_chunks` on tenant,
  collection and chash, counting chunks, since chashes recur across
  collections. `livec_findings.sql` line 100 can read the catalog title or
  file_path directly, which also fixes the page-suffix PDF titles its
  regexp misses today; its lines 104-105 (chunk indexed_at against the
  document's, for superseded residue) have no direct rewrite and need the
  manifest join and a decision on which side supplies the older value.
  Unconfirmed: the exact-recall code after `run_gate.py` ~4112 and the
  catalog content_type values held for the papers and rdr collections.
- **RF-11 (Verified, source search 2026-10-26).** Every indexer writes
  tags and category as constants (code: extension and "code"; prose:
  extension and "prose"; markdown: "markdown" and "prose"; `nx index pdf`
  and the streaming pipeline: "pdf" and "paper"; the repo PDF path: "pdf"
  and "prose"), a census of all five writers, so the backfill derives them
  for indexed documents without reading a chunk row. For notes, the live
  census on nexus-6pfgb.35 recovers all 427 keyless notes that have row
  values (of 432 keyless) under option B and finds no TTL at stake; the
  census names its tenant `default` where RF-16 names tenants by id
  (nexus, gate-xr789, three with 0 manifest rows), so which RF-16 tenant
  it saw is inferred, not read, and the .10 dry run re-counts per tenant.
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
- **RF-16 (Verified, live census 2026-10-27T00:45Z; T2 research-16).** The
  six schema census queries, run read-only on the production database as
  nexus_admin per tenant with every transaction rolled back, counts only.
  Manifest rows 635140 (tenant nexus 518648, gate-xr789 116492, the other
  three tenants 0). chunk_text_hash differs from the chash on 0 rows and 0
  rows lack the key; 0 rows have a chash other than 32 bytes (no legacy
  16-byte row is left); manifest collection differs from the document's
  physical_collection on 0 rows and 0 manifest rows lack a document or a
  physical_collection; chunk_index differs from position on 0 rows and is
  null on 0. A chash repeated inside one document: 4976 (tenant, doc_id,
  chash) manifest groups across 1752 documents, all in tenant nexus.
  Quarantine keys: live collections 0 half-present either way; quarantine
  collections 0 quarantined_by without quarantined_at and 158
  quarantined_at without quarantined_by, which the client move explains
  (`chunk_quarantine.py` writes no quarantined_by). Also observed: the
  chunk-row counts (599799 rows) were identical under every tenant GUC
  while the manifest counts were tenant-scoped, so `nexus.chunks` is not
  row-filtered for the owner role under `SET LOCAL ROLE nexus_admin`
  (cause unconfirmed); a direct-SQL sweep run as the owner sees every
  tenant and must carry tenant_id in its batches.
- **RF-17 (Decided, Sam 2026-10-26).** tags is a set: a JSON array on the
  document filtered by containment, never a comma string matched by
  equality; indexed files get a GENERATED one-element array. The design
  changes the schema analysis forced (database CHECKs behind the Java
  allow-list after a mandatory allow-list-shaped sweep; GENERATED columns
  for source_format and indexed-file tags and category, moving .8 and .23
  to Phase 3; frecency with computed_at; backfill authorship by the full
  row key with tombstoned owners and the last_written_at guard; one
  manifest row for nexus-1nrcu; the .26 re-scope) are accepted as written.
  No code stores the array today: every note since 6pfgb.5 holds tags as
  the comma string the CLI and MCP pass through unsplit (RF-20), so
  6pfgb.38 is the implementing bead (writer, sentinel, recovery bundle,
  display, one-time conversion, the containment predicate over both
  homes).
- **RF-18 (Verified, source search 2026-10-27).** Engine write modes:
  `upsert-chunks` and `update-metadata` merge; the combined manifest
  writes merge only when the client sends `metadata_merge` (the current
  client's `note_write`, `indexer.py` and the exporter send it directly;
  `multi_batch_write` defaults it off and the pipeline, doc_indexer and
  oversize paths supply it through `MetadataMergingCatalog`, RF-22) and
  otherwise replace
  the row's metadata wholesale; their delete field is
  `metadata_delete_keys`. The identical-text re-write of a re-put bumps
  `last_written_at`.
- **RF-19 (Verified, source search 2026-10-27).** Engine readers of row
  keys the sweep would blind: the manifest-less census attributes a chunk
  by `catalog_doc_id` then `doc_id` (forward join) or by a note's catalog
  meta `doc_id` equal to the chash (reverse); `/reapable` and the reaper's
  refusal audit read `title` and `source_path` for display; the gc
  quarantine audit returns the row title (its live definition is in
  vectors-030, RF-22; `spanFromMeta`'s line fields are RF-5's and .26's).
  The reaper, the rdr192
  upgrade-ladder rung and `nx t3 census-manifest-less --require-zero`
  depend on the census's `legacy-unmanifested` bucket reading zero. The
  gc restore functions (vectors-030; catalog-043's bodies are superseded)
  strip only quarantined_at and origin_collection; `quarantine_restore_chunks`
  strips all four; all three INSERT the row back into its origin
  collection with the rest of its metadata intact (RF-23). A fifth engine
  reader: `quarantine_reattach_plan` (vectors-025), called by
  `quarantine_restore_chunks`, resolves a quarantined chunk's owner forward
  by its metadata `catalog_doc_id` or `doc_id` and its position by
  `chunk_index` (RF-24), so the quarantine sweep keeps those three LEGACY
  keys on `quarantine-*` rows until j67ix.1 moves the plan's forward path to
  the manifest and the census record.
- **RF-20 (Verified, source search 2026-10-27).** The note tag path
  stores a comma string end to end (CLI, MCP, `put_note`,
  `note_catalog_meta`, the recovery bundle); two keyless-note definitions
  exist (`recovery_bundle._own_note_keys`: none of the five note keys;
  `note_ttl.has_own_keys`: put_at or ttl_days); `put_note` writes the
  catalog entry with `put_at` first and the chunk row second; a repo PDF
  registers with catalog content_type `pdf` and `nx index pdf` with
  `paper`; a DEVONthink record keeps its on-disk file_path and gets the
  `x-devonthink-item://` source_uri after indexing.
- **RF-21 (Verified, source search 2026-10-27; the band arithmetic is
  inferred from the constants, not measured).** `OVERFETCH` 4 with ceiling
  1200 in `ManifestLabelStage`; `SELECTIVE_GATE_MAX` 5000 and the in-SQL
  gate under `hnsw.iterative_scan` in hybrid dispatch;
  `exactOnUnderReturn` (nexus-bq06h) re-runs an empty page exactly and
  records short pages as its residual. A filter passing more than 5000
  chashes but fewer than one in four of the nearest rows under-fills
  through the stage (5000/N for a collection of N chunks; the 599799 of
  RF-16 is the whole database). Bead census 2026-10-27: 29 open children,
  .33, .35, .36 and .37 closed; nexus-p89h4 absorbed.
- **RF-22 (Verified where this session re-read; the rest relayed from the
  fix-check critics' worktree reads, marked in the record).**
  `exactOnUnderReturn` fires on an empty page by design and records short
  pages as its residual; hybrid's dispatch exposes its bound for tests;
  `metadata_delete_keys` requires `metadata_merge`; `prose_indexer` routes
  `.md` and `.markdown` alike; `catalog_ttl_stale` already compares the
  row's `indexed_at` with the catalog clock; the ladder runs on every
  install; `ChunkReaper` documents the no-owner blind spot; the stranded
  note the rdr192 rung cannot heal; `hnsw.max_scan_tuples` is a limit, not
  a signal; the four closed beads closed 2026-10-27 UTC.
- **RF-23 (Verified, source search 2026-10-27).** rdr180-001 and
  vectors-004 record that a boot-time VALIDATE of a chunk CHECK crash-loops
  an un-rekeyed store, so `chunks_chash_octet_check` exists NOT VALID
  (vectors-004, carried by vectors-030) and nothing has validated it since
  the chash-rekey rung retired; the three
  gc restore functions INSERT a quarantined row back into its origin
  collection with its metadata minus the quarantine keys, so a quarantined
  row keeping legacy document keys would fail a validated allow-list CHECK
  on restore; the `.nxexp` import writes no note keys to the catalog by
  design; `orphan_backfill` derives a catalog content_type from the
  collection prefix (`docs` among them); the indexers write tags as the
  lower-cased, dot-stripped extension; the reaper refuses a collection
  while `legacy-unmanifested` or `unclassified` is above zero; the scan
  stops at `hnsw.max_scan_tuples` or the `work_mem` budget; catalog-001
  indexes only two of the three bib ids.
- **RF-24 (Verified, source search 2026-10-27).** catalog-045 indexes
  (tenant_id, md5(title)) for live rows and records why not title: a btree
  on free text can exceed the row-size limit, so the probe states
  `md5(title) = md5(?)` beside `title = ?`; author is unbounded TEXT with
  the same hazard. The stage answers `source_author` from the catalog and
  falls back to the row when the author is empty, and its operator set
  includes the four comparisons. `quarantine_reattach_plan` (vectors-025)
  resolves a quarantined chunk's owner forward by `catalog_doc_id` or
  `doc_id` and its position by `chunk_index`. `nexus.ladder_completions` is
  keyed (tenant_id, rung_name). vectors-036's width CHECK sits in a
  rollback block on a retired table. `plain_search_<dim>` takes `p_where`
  and `p_where_path`. The indexers write "" for a file with no extension.
  The epic has 36 open children after .44.

### Critical Assumptions

- [x] A1: the engine merge never retracts a key without `delete_keys` —
  **Status**: Verified with one exception. True on `upsert-chunks`,
  `update-metadata` and the current client's combined writes, which send
  `metadata_merge`; false for a combined write without `metadata_merge`,
  which replaces the row's metadata and so retracts every omitted key
  (RF-18). Consequences: the Java check validates a replace-mode write on
  its incoming keys like any other and inspects neither `delete_keys` nor
  the combined routes' `metadata_delete_keys`; Step 1's premise that legacy
  keys stay on a row until the sweep holds for merging writers only, and
  the sweep does not depend on it — **Method**: Source Search (3ajpd,
  x54wv, RF-4, RF-18).
- [x] A2: catalog meta merge is `jsonb_concat`; an explicit empty value
  overwrites — **Status**: Verified — **Method**: Source Search (nexus-ke45f).
- [x] A3: owner resolution can be done at page cost, not per hit, on every
  live reader — **Status**: Verified with exceptions (three sites are
  Phase 2 edits): the engine answers a whole chash list
  in one statement (cap 1000), `search` and `store_get_many` already pay
  one batched pair per page, and the three sites that do not (`query`'s
  duplicate lookup, the plan runner's chash path, `query`'s per-row
  catalog-parameter branch) have the batched rows available in the same
  request, so making them page-cost is a Phase 2 edit, not an open
  question (RF-10) — **Method**: Source Search plus a timed spike.
- [x] A4: `catalog_document_chunks` carries a column for every position
  field and the writers fill them on every path — **Status**: Verified
  with exceptions (RF-5, RF-9): the columns exist and every indexer path
  sends the labels; notes, orphan backfill and manifest backfill send none
  and leave their rows legacy, which is what nexus-jz86a fixes for notes
  in Phase 1, and orphan backfill writes NULL spans, so the Step 3
  precondition counts NULL-span manifest rows before the row's line fields
  are swept — **Method**: Source Search.
- [x] A5: no writer outside the nexus client sends document keys to chunk
  rows on purpose — **Status**: Verified for the path census of the
  conexus repository (RF-12): no caller of a chunk write route exists
  there, and the only direct writer that carries document keys is the
  gate's copy tool `deploy/gate/etl_t3.py`, which inserts source rows'
  metadata verbatim and must strip non-chunk keys in Phase 5; everything
  else writes quarantine keys, collection only, or no metadata (the
  rehearsal `hnsw_probe.py` is in that census by file name; its content was
  not traced: inferred, not read). The engine's would-refuse log from Phase 4 Step 2a, read over a stated
  window and empty or dispositioned, remains the final check for writers
  no repository shows (RDR-223's method) — **Method**: Source Search, then
  the log.
- [x] A6: the parity gate's twelve filtered queries and the findings SQL can
  be rewritten against the catalog without changing what they measure —
  **Status**: Verified for the queries (RF-13): all twelve filter on
  indexer constants, ten rewrite to catalog content_type or the
  collection scope, two (tags=js, tags=pdf) read the GENERATED tags
  column Phase 3 adds (.8; `source_format` cannot express tags=js); the
  engine leg
  forwards the where clause unchanged and needs nothing; the recall leg
  becomes a manifest join. Not fully true for the findings SQL: its
  superseded-residue predicate compares a chunk row's indexed_at with the
  document's, and a shared row has no single indexed_at, so that predicate
  needs a manifest join and a design decision in conexus-wd1q — **Method**:
  Source Search in both repositories; the rewritten gate's first run is
  the spike.
- [x] A7: the backfill can give every existing document its own values.
  **Status**: Verified with exceptions: verified for notes with row values
  and for the derived keys; Assumed for the extraction and frecency
  residue; false for the five notes with nothing to recover and for
  pre-`.18` imports, named above. Notes: by option B (RF-8), the live
  census of 2026-10-26 recovers all 427 keyless notes that have row values
  for tags and category (419 by stamp and title, 8 as sole live owners) and
  loses no TTL (RF-11); the other 5 of the 432 (4 ghosts, 1 empty row)
  and any pre-`.18` import are residue named for Sam's acceptance at the
  .10 dry run. Indexed documents: tags, category and
  source_format are constants per indexer (RF-11, a census of all five
  writers), so they are derived, never copied. What stays assumed:
  extraction_method, quality_gate_overridden, extraction_source and
  frecency_score are copied only from a chunk with exactly one live
  owner; an all-shared PDF's extraction provenance needs a forced
  re-extraction (an unchanged document is skipped by the freshness check),
  and frecency is recomputable from git — **Method**: Source Search and
  the .35 live census; the residue count comes from the backfill's dry run
  and live run, both before Phase 4 Step 1, which needs the keyless-note
  count at zero or its residue accepted.

- [ ] A8: an in-SQL EXISTS under `hnsw.iterative_scan` fills a page for a
  filter the dense branch takes (about 1 percent of a collection) with a
  query far from the filtered documents — **Status**: Assumed; RF-21
  verifies the shape for hybrid's text gate only, and hybrid's own javadoc
  marks that dispatch a heuristic (RF-22); the exact re-run on a short page
  is the guard if it fails — **Method**: Spike on the live-engine fixture
  before Phase 3's .25 lands, with the dispatch bound pinned so a
  2000-chash filter on a 20000-chunk fixture takes the dense branch (the
  band is 5000/N, so an unpinned fixture never reaches it) and a far query;
  full page, or the re-run fires once; recorded as research.

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
   one. Notes: catalog meta (tags as a JSON array, 6pfgb.38; category,
   ttl_days, put_at, source_agent). Every document: meta for
   extraction_method, quality_gate_overridden, extraction_source,
   frecency_score with its computed_at and the bib writer, or an existing
   column (markdown author), written at the seams that already update the
   catalog. Indexed files: source_format, tags and category are GENERATED
   columns on `catalog_documents` (Phase 3, engine changeset), stored
   nowhere. A one-time, dry-run-first, fill-only verb stamps the meta keys
   for existing documents under the per-kind rules of A7 and reports
   counts; its one exception to fill-only is the tags conversion
   (6pfgb.38). Position
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
   route (a) (author and the id-valued bib keys leave it for the pre-filter
   below). The remaining DOCUMENT keys are selective (title names one
   document; content_type, tags and category can exclude most of a
   collection), and a stage that filters after the search is cut at 4x the
   limit cannot answer them without losing recall. So those keys are
   answered by a **pre-filter** built on the dispatch hybrid search already
   uses: resolve the qualifying documents from the catalog, collect their
   manifest chashes under a bound, rank a small set exactly by primary key
   (`chash = ANY`), and, when the set is large, run the index-first plan
   with the document predicate evaluated inside the search SQL (an EXISTS
   on the manifest joined to the catalog, under `hnsw.iterative_scan`, the
   shape hybrid's text gate already takes), never by the post-search
   stage's cut; on that branch only, a page shorter than the limit is
   re-run exactly (nexus-bq06h's `exactOnUnderReturn`, whose trigger today
   is an empty page, extended to a short one there: more than the bound's
   worth of chashes qualify on that branch, so a short page is starved by
   construction). A hit is reported under the qualifying owner. The
   post-search stage keeps the span and source_uri re-pointing, indexed_at
   and the non-id bib_* keys, and the per-owner re-evaluation of every
   label predicate; label filters, which under-fill through the same cut
   today (nexus-p89h4), take the same EXISTS on the manifest without the
   catalog join, with the `labels_written` row fallback inside it. Route (b)'s SQL functions get the dispatch in their own bead after
   route (a) is deployed and measured (6pfgb.41). Route (c) callers select
   by manifest.
5. **The engine refuses non-chunk keys on chunk writes.** A chunk write
   whose metadata carries any key outside the table's CHUNK rows is refused
   with 422 naming the key and its home, on `upsert-chunks`,
   `update-metadata` and the three combined manifest writes (`append`,
   `append_many`, `write_many`), checked in the one service they share
   (`CombinedWriteService`) and in the upsert path. `delete_keys` is
   never refused (it is how a client retracts a key it used to write; the
   final sweep is a direct UPDATE, Step 3). Position fields travel in the manifest rows,
   which already have their columns; document fields travel on the catalog
   document. The check ships log-only first and enforces in a later paired
   cut, after the conexus gates read the catalog and the would-refuse log
   is empty (RDR-223's mechanics, Phase 4).
6. **Existing rows are swept when nothing reads them.** A stale value on an
   existing row is served wherever a reader's presence fallback finds no
   home (a legacy manifest label, an empty catalog author, a document the
   backfill could not fill). The sweep that removes the retired keys (a
   direct UPDATE in Step 3; `delete_keys` is the per-write form) therefore
   has a measured precondition: `nx doctor` (the
   client's health report) counts zero legacy-label manifest rows, zero
   **keyless notes** (one definition, adopted here: a note whose catalog
   meta has no `put_at`, which every put since 6pfgb.15 stamps and which
   the `.nxexp` import stamps from `.18` on (`put_at` now and ttl_days 0 on
   a note's entry that has no note keys; an existing entry keeps its keys;
   indexed documents get none; the export carries no TTL the catalog could
   keep true, RF-23); the recovery
   bundle's
   any-of-five test and `note_ttl.has_own_keys` are re-pointed at `put_at`
   as part of 6pfgb.10, RF-20, which also stamps `put_at` and `ttl_days` 0
   for a note recovered as sole owner with no TTL anywhere, so the count
   can reach zero), zero home-less documents (documents the
   backfill could not fill), zero `legacy-unmanifested` chunks in the
   engine's manifest-less census (the rdr192 rung has converged on the
   install, RF-19), zero manifest rows whose span is NULL while their
   chunk row's span is not (A4) and zero indexed documents whose GENERATED
   columns come out NULL, or each residue is named and accepted by Sam at
   the dry run; an accepted `legacy-unmanifested` residue blocks the sweep
   for that collection (the reaper refuses a collection while the bucket is
   above zero, RF-23, and a swept stranded note's chunk would read
   `no-owner` and become reapable), so a stranded note the rung cannot heal
   (RF-22) is healed by hand or its collection stays unswept: the sweep
   route skips a blocked collection and the per-tenant sweep count the
   table-wide routes read excludes it, so the install's CHECKs still land;
   the blocked collection's legacy-keyed rows then fail any UPDATE under
   the NOT VALID allow-list CHECK until healed (reads, and the reaper's
   move into a quarantine collection, are unaffected), which the acceptance
   text states. The engine's own row-key readers (6pfgb.39) change first:
   the census drops its forward join once the tenant's rdr192 ladder
   completion record exists (`nexus.ladder_completions` is keyed by tenant
   and rung, RF-24; the bucket itself is not the condition, since a dropped
   join would read it as zero vacuously), keeps
   the reverse path, and updates the census SQL identity pin and the CLI
   buckets, so a later legacy-shaped chunk reads `no-owner` (accepted: only
   `legacy-unmanifested` and `unclassified` gate the reaper, RF-19);
   `/reapable`, the reaper's refusal audit and the gc quarantine audit
   label a row by a chunk text prefix and the manifest owner's title where
   one exists.

### Technical Design

#### Schema analysis

Sam asked (2026-10-26) whether the normalization had been given the
standard tests. It had not: the draft argued "one home per key" in prose.
This subsection records the analysis (T3
`nexus-architecture/analysis-rdr236-chunk-schema-normalization-2026-10-26`,
parts 1 to 3; T2 `nexus/analysis-rdr236-schema-normalization-2026-10-26`),
run against the changesets at develop 9681c75aa. Notation: **K** is the
chunk row key (tenant_id, collection, chash, embedding_model); **D** is a
catalog document (tenant_id, tumbler); **Pn** is one manifest position
(D, position). "A determines B" means one A value fixes one B value;
"A determines a set of B" means one A fixes several.

**Relations and keys, from the DDL.** `nexus.chunks`: declared key K
(vectors-030); collection determines embedding_model through the foreign
key to `catalog_collections`, so the minimal key is (tenant_id,
collection, chash) and embedding_model is in the declared key only because
the partition column must be. `catalog_documents`: key D; a second key
(tenant_id, source_uri) where source_uri is non-empty (catalog-016).
`catalog_document_chunks`: key Pn; foreign keys to the document (fk-001)
and to the chunk on all four columns of K (vectors-030, deferrable,
validated).

**The dependency set.** chash determines chunk_text and (generated)
chunk_tsv; (chash, embedding_model) determines the embedding (the
contextual embedder sends each text as its own one-chunk document, so the
vector does not depend on the owning document; nexus-u2mlh.1). Pn
determines chash, collection, embedding_model, chunk_index, the spans,
the three labels and labels_written. K determines a set of D and a set of
Pn: one chunk has many owners and may sit at several positions in one
owner. D determines title, author, content_type, the bib columns,
indexed_at, tags, category, ttl_days, put_at, source_agent, extraction
provenance and frecency (with a computation time).

**The root cause, formally.** Storing a document attribute A on the chunk
row claims K determines A. The true dependencies are K determines a set
of D, and D determines A; from those only "K determines a set of A"
follows. D is not a column of `chunks`, so the dependency D determines A
can neither be stored nor checked there, and the merge turns the second
owner's value into an update. No normal form admits A on `chunks`:
storing the set would need a key (K, A) and the multivalued dependency
then breaks 4NF. A belongs with D. The same holds for position attributes
and Pn.

**Normal form today.** 1NF holds except tags, a comma string used as a
set and filtered by equality (the engine has $eq, $ne, $in, $nin on the
key; `tags=arch` misses a note tagged "arch,decision"). 2NF fails in a
harmless way (chash determines chunk_text and chunk_text_hash; collection
determines embedding_model: the determinants are inside the key, so each
copy is a fixed function of it) and in the way that matters: document and
position attributes whose determinant is not in the relation. Anomalies,
with the census's 23 reader rows classified: update (last writer wins)
rows 1 to 9, 12, 18; update and deletion (a co-owner's `delete_keys`
erases another owner's value through `REWRITE_OWNED_KEYS`) row 22;
insertion (a document attribute cannot exist without a chunk: the
zero-chunk file hash, nexus-5j8fp; keyless notes) is the residue the
backfill fills; lossy join (joining on a non-key such as title or
content_hash) rows 10, 11, 13, 14, 15, 17, 19; arbitrary pick (docs[0], or
the last row) rows 21, 23; a third writer restamping then update, row 16;
row 20 has no anomaly (orphans have no owner). The shipped fixes map onto
these: 3ajpd removed the staleness update anomaly and the content_hash
lossy join; 5j8fp the zero-chunk insertion anomaly; 6pfgb.5 the note
insertion and update anomalies for new notes; .11, .14, .15 moved rows 1,
7, 8 to the document; .21 and .22 replaced the lossy joins of rows 10, 11,
13 by manifest selection.

**Normal form after.** With every document attribute on `catalog_documents`
(key D) and every position attribute on the manifest (key Pn), both are in
BCNF for those dependencies; moving keys into the catalog's meta JSON does
not bring the violation back, because the tumbler is the key. What meta
costs instead: no declared type, NOT NULL or CHECK per key; no retraction
(catalog update has no delete-keys, so an empty value overwrites,
nexus-ke45f); and it invites storing values that are computable. That last
point changes three homes in this RDR: source_format, and tags and
category for indexed files, are determined by content_type and the file
extension (RF-11), so storing them would add a 3NF transitive dependency;
they become GENERATED columns on `catalog_documents` (engine changeset),
not meta keys. frecency_score depends on the document and the time it was
computed (`frecency.py` uses now()), so its home carries a computed_at.
tags is a set (Sam, 2026-10-26, RF-17): in 4NF a child relation
(document, tag), in practice a JSON array on the document meta filtered
with containment (`tags=arch` matches a note tagged arch and decision),
which needs a containment filter in the catalog meta predicates and a GIN
index (`jsonb_path_ops`) on `catalog_documents.metadata`, both engine
changesets in Phase 3; the comma string every note holds today (RF-20) is
legacy, and 6pfgb.38 converts it to the array once, the one write exempt
from the backfill's fill-only rule. Two attributes
travel under one name and
are split in the key table: title (the document title for notes and PDFs;
a per-position label for code, prose and repo PDFs) and indexed_at (the
row's last client write; the catalog's last engine manifest write, heals
included; the note's put_at). Also found: `nexus.frecency`
(telemetry-001) is a third, chunk-keyed home for frecency_score and
ttl_days with no client writer found; it is listed in the key table and
6pfgb.42 retires it or records its writer and reader.

**Lossless-join test.** Decompose the joined data into chunks (K and the
chunk columns), manifest (Pn, K, the position columns) and documents (D and
the document columns). manifest joined to chunks on K is lossless because
K is the key of chunks (Heath's theorem) and the four-column foreign key
guarantees every manifest row finds its chunk; that result joined to
documents on D is lossless because D is the key of documents and fk-001
guarantees the document exists. For a single-owner chunk the join returns
today's row exactly, except that a chash repeated at several positions in
one document returns one row per position where today's row holds one
label: so nexus-1nrcu's backfill rule must require exactly one manifest
row for the chash, not one live owner. For a shared chunk the join returns
one row per (owner, position) with that owner's values, which today's
schema cannot represent. The new design also holds a document with no
chunks and a chunk with no manifest row. Values the join cannot rebuild,
lost at write time and not by this decomposition: extraction fields of
PDFs whose chunks are all shared (forced re-extraction), the TTL of the 8
unstamped notes (none had one), labels and spans on legacy manifest rows
of multi-owner or repeated chashes (forced re-index), bib values that
`--enrich` wrote only to shared rows (re-enrichment). frecency is
recomputable.

**Dependency preservation.** Enforced by the database today: the three
primary keys, the collection to model foreign key, the generated
chunk_tsv, the manifest's two foreign keys. Not enforced anywhere, with
the enforcement this RDR adds:

| Dependency | Enforcement | Write cost |
| --- | --- | --- |
| chunk metadata holds only CHUNK and ROW keys | CHECK `(metadata - allowed) = '{}'` with the bypass prefixes (`taxonomy__*`, `quarantine-*`) exempt from the CHECK, while the sweep strips retired document keys inside `quarantine-*` rows too (keeping the four ROW keys), because every gc restore copies a quarantined row's metadata back into a live collection where the CHECK applies (RF-23); added NOT VALID after every tenant's sweep and then VALIDATEd through the table-wide routes (6pfgb.43), never in a startup changeset; the Java allow-list in front of it; the allowed-key array is rendered from the Python table into the route's DDL, and a key-table change re-adds the constraint through the same route (a Liquibase changeset is immutable, so none carries this array) | microseconds a row |
| chunk_text_hash = encode(chash, 'hex') | CHECK, added NOT VALID by the add-constraint route after every tenant's sweep and VALIDATEd by the validate route (6pfgb.43); never a startup changeset | negligible |
| octet_length(chash) = 32 | the existing NOT VALID CHECK `chunks_chash_octet_check` (vectors-004, carried onto the partitioned table by vectors-030; RF-24), left NOT VALID since the chash-rekey rung retired; VALIDATE only, by the validate route, after reading `pg_constraint.convalidated` | negligible |
| metadata embedding_model = the column | CHECK; not censused by RF-16, counted by the Step 3 dry run first (6pfgb.42) | negligible |
| quarantined_by implies quarantined_at (RF-16: 158 rows in the quarantine collections, moved by the engine's `gc_quarantine_orphans` on the client's call, carry quarantined_at alone, 0 rows the other way, 0 half-present in live collections; a gc restore drops quarantined_at and leaves quarantined_by, so the restore drops all four before the CHECK is added: 6pfgb.40) | CHECK | negligible |
| manifest collection = the document's physical_collection | composite foreign key or trigger; documents with no physical_collection (RDR-191) need a rule (6pfgb.42) | one index lookup a manifest row |
| chunk_index = position | CHECK (6pfgb.42) | negligible |
| labels_written false means the labels are NULL | CHECK on `catalog_document_chunks`; not censused by RF-16, counted by the Step 3 dry run first, added in 6pfgb.42's own step (not the chunk routes) | negligible |
| ttl_days > 0 means put_at present | CHECK on `catalog_documents` meta, counted at the .10 dry run, added NOT VALID only after the Step 2c paired cut (a pre-6pfgb.15 client writes ttl_days without put_at and would be refused by a NOT VALID CHECK; after 2c such a client is already refused, so the ledger entry is the same not-additive one) and VALIDATEd in its own step (6pfgb.44) | negligible |
| derived document keys (source_format; indexed files' tags and category) | GENERATED columns | one regex a write |

Why both the Java check and the CHECK: the Java check validates what a
client sends on the write routes, can name the key and its home in a 422,
can run in log mode, and alone can enforce "ROW keys are written only by
the engine" (a CHECK sees the stored row, not the writer). The RDR's goal
is a property of the stored rows, and only a CHECK covers every writer:
direct SQL such as the conexus copy tool (RF-12), the SQL functions,
future data changesets; unlike a trigger it cannot be switched off by
session_replication_role. Two cautions: a CHECK failure prints the failing
row, chunk text and vector included, so the Java check stays in front; and
`mergeMetadata` never removes keys, so a CHECK enforced before the sweep
would fail the next upsert of every legacy shared row. Hence the order in
Phase 4: Java check in log mode, then enforce mode (Step 2c), sweep, CHECK
NOT VALID, VALIDATE, the last two through the engine-admin routes and
never a startup changeset (RF-23: rdr180-001 records a boot-time VALIDATE
as the crash-loop shape, and vectors-004 left the width CHECK NOT VALID for
that reason).

**Denormalization decisions.**

| Attribute | Kept or removed | Why |
| --- | --- | --- |
| chunk_text_hash in metadata | kept, CHECKed | zero read cost either way (the response already holds the row id); kept for the wire shape, verified equal to the column |
| embedding_model in metadata | kept, CHECKed | same |
| embedding_model in the chunk key | kept | partitioning requires it; the foreign key enforces it |
| manifest collection and embedding_model | kept | foreign-key join columns; the doc_id to collection check is added |
| presence fallback (catalog value, else the row's) | kept while bounded | the row is already in hand; it reintroduces the update anomaly for any document whose home is still empty, so it ends when the doctor counts reach zero, and has one SQL definition shared by the stage, the pre-filter and the conexus recall SQL |
| frecency_score on the row | removed | `resolve_many` already runs once per window (RF-10); refresh writes drop from one per chunk to one per file; a computed_at is stored |
| span on the search row | already text-determined | `spanFromMeta` emits the chash form; what moves is the flattened line fields and file:line, one `chash_positions` call per window |
| pre-filter | added | one catalog query plus a manifest fetch capped at 5000; no index exists today on `catalog_documents.metadata`, so the GENERATED columns carry btree indexes (source_format, category) and a GIN index on the generated tags array, the meta gets a GIN index (`jsonb_path_ops`) for the note tags containment test (6pfgb.38), and the selective probes use one index list: `title=` uses the existing partial index on (tenant_id, md5(title)) for live rows (catalog-045; the probe states `md5(title) = md5(?)` beside `title = ?`, because a btree on free text can exceed the row-size limit, RF-24), `source_author=` gets the same form, a partial index on (tenant_id, md5(author)) WHERE deleted_at IS NULL, and `bib_doi=` a partial index on (tenant_id, bib_doi) WHERE bib_doi <> '' beside catalog-001's two bib-id indexes; never a plain btree on title or author (RF-24), measured before Phase 3 |
| stored source_format, tags, category for indexed files | removed | GENERATED columns cost nothing to read |

**Transforms.** The analysis writes the backfill, the sweep and the
constraints as SQL (T3 record, section 8). Rules they share and this RDR
adopts: group by the full row key K, never chash alone; count tombstoned
owners when judging authorship (being the only live owner does not mean
having written last); guard the copy with `c.last_written_at <=
d.indexed_at + 5 minutes` so the owner's latest manifest write is no older
than the row's latest write; count a document whose rows disagree as
residue (HAVING one distinct value); the sweep is allow-list shaped
(keep only the allowed keys) in batches with the chash-length guard, and
a direct UPDATE does not bump last_written_at; the CHECKs are added NOT
VALID after the sweep and then VALIDATEd. The six census queries ran
read-only on the production database on 2026-10-27 (RF-16): every
equality the census covered already holds on live data (chunk_text_hash
equals the chash, chash is 32 bytes everywhere, manifest collection
equals the document's physical_collection, chunk_index equals position),
so the CHECKs on those equalities (chunk_text_hash, chash width,
chunk_index; the manifest collection rule is 6pfgb.42's) hold there
without a data fix; two CHECKs the table lists were not censused (metadata
embedding_model equal to the column; labels_written false with NULL
labels) and are counted by the Step 3 dry run before they are added
(6pfgb.42). The census covers one production database; the CHECKs ship to
every install, so each install's sweep rung re-counts the four censused
equalities and the two uncensused ones before its add-constraint and
validate steps (6pfgb.43), and no chunk CHECK is added or validated in a
startup changeset: chunk_text_hash is added NOT VALID by the add-constraint
route after the sweep, and the existing width CHECK
`chunks_chash_octet_check` is VALIDATEd by the validate route (RF-23); a
chash recurs inside one
document in 4976 manifest groups across 1752 documents, so the one
manifest row rule (nexus-1nrcu) is load-bearing; and 158 quarantine rows
carry quarantined_at without quarantined_by (the client move writes no
quarantined_by), so the quarantine key CHECK is one-directional:
quarantined_by implies quarantined_at, never all four or none.

#### The key table

Columns: key, determinant (what fixes its value), domain (what absent and
the sentinel mean), home after normalization, phase that moves it. The
determinant is the test: a key stays on the chunk row only when K, or a
part of K, determines it.

| Key | Determinant | Domain | Home after normalization | Moved by |
| --- | --- | --- | --- | --- |
| chunk_text_hash | chash (equal to its hex) | always present on new rows | CHUNK, kept for the wire shape, CHECKed equal to the column | Phase 4 CHECK through the add-constraint and validate routes (6pfgb.43) |
| embedding_model (metadata) | the column of the same name | always present | CHUNK, CHECKed equal to the column | Phase 4 CHECK through the routes (6pfgb.43) |
| content_type (chunk vocabulary) | notes: the collection prefix; indexed files: the indexer path and extension | one of code, pdf, markdown, prose | DOCUMENT: `catalog_documents.content_type` for code/prose/paper/rdr/knowledge; the GENERATED `source_format` column for pdf/markdown (RF-7); row fallback while the column is absent | 6pfgb.23 (engine, generated column), .25 |
| title | two attributes: D for notes and `nx index pdf`; Pn (the per-position label "rel:ls-le") for code, prose and repo PDFs | always present | DOCUMENT `catalog_documents.title` (RF-7); the per-position label is display text derived from the manifest spans and file_path, stored nowhere; the engine's `/reapable` listing and reaper audit display read the row title until 6pfgb.39 (RF-19) | 6pfgb.25, .27, .39 |
| source_author | D | absent = unknown | DOCUMENT `catalog_documents.author`; row fallback while empty (markdown: 6pfgb.7) | 6pfgb.7 |
| bib_year, bib_authors, bib_venue, bib_citation_count, bib_semantic_scholar_id, bib_openalex_id, bib_doi | D | columns are NOT NULL with defaults; `bib_enriched_at` set means enriched | DOCUMENT, the bib columns, authoritative, no fallback | 6pfgb.28, .12 |
| bib_enriched_by (proposed by .32) | D | absent = not enriched | DOCUMENT meta key beside `bib_enriched_at`; never a row key | 6pfgb.32 |
| indexed_at | three attributes: the row's last client write; the catalog's last engine manifest write (heals included); the note's put_at | timestamp | DOCUMENT: display and `where=` read `catalog_documents.indexed_at` (row fallback while NULL); the note TTL clock is meta `put_at` (6pfgb.15); the row's own write time is the ROW column `last_written_at` | 6pfgb.13 |
| tags | D; for indexed files computable from extension | a set (Sam, 2026-10-26, RF-17): a JSON array in the document meta, filtered by containment; a comma string is legacy, converted once by 6pfgb.38; absent or `[]` = no tags | DOCUMENT: notes in meta (6pfgb.5 stores the comma string today, RF-20; 6pfgb.38 stores the array and converts existing strings); indexed files as a GENERATED one-element array from file_path, not stored | 6pfgb.38 (notes), 6pfgb.8 (re-scoped, indexed files) |
| category | D; for indexed files computable from content_type | one of code, prose, paper, or a note's own | DOCUMENT: notes in meta (done); indexed files GENERATED from content_type | 6pfgb.8 (re-scoped) |
| ttl_days, put_at | D | ttl_days 0 or absent = permanent in meta (one encoding, 0 as the retraction sentinel); put_at present whenever ttl_days > 0 (CHECK, 6pfgb.44) | DOCUMENT meta (6pfgb.5, .15 done) | done; CHECK 6pfgb.44 |
| extraction_method, quality_gate_overridden, extraction_source | D | absent = unknown; explicit false for the override | DOCUMENT meta | 6pfgb.6, .16 |
| frecency_score | D and computation time (`frecency.py` uses now()) | float with a computed_at | DOCUMENT meta with computed_at; the scorer reads the attributed owner | 6pfgb.9 (re-scoped) |
| source_agent | D | absent = unknown | DOCUMENT meta (notes done in .5) | 6pfgb.17 |
| session_id | none that matters; zero readers | retired | none | 6pfgb.4 |
| catalog_doc_id (notes) | the last writer's D | legacy stamp | DOCUMENT: the manifest is the owner record; read by the note TTL rules until the keyless-note count is zero (RF-8) and by the engine's manifest-less census forward join until the rdr192 rung has converged and 6pfgb.39 lands (RF-19) | 6pfgb.27, .39 |
| content_hash (file hash) | D | sha256 of the file | DOCUMENT `catalog_documents.index_content_hash` (3ajpd); the row writer at `metadata_schema.py` ~452 still stamps it and goes in Phase 4 Step 1 | 3ajpd done; writer .27 |
| section_title, section_type, page_number | Pn | absent = unknown; "" = none here, marked by labels_written | POSITION `catalog_document_chunks` (catalog-046) | tko2w, jz86a, 1nrcu (one manifest row, not one owner) |
| line_start, line_end, chunk_start_char, chunk_end_char | Pn | integers | POSITION `catalog_document_chunks` line_start/line_end/char_start/char_end; the flattened search-row fields and file:line are re-pointed | 6pfgb.26 (re-scoped), .19 |
| chunk_type (table_page) | none; dead | dropped by normalize before every write | none; writer removed | 6pfgb.27 |
| quarantined_at, origin_collection, quarantined_by, reaper_quarantined_at | the physical row; written only by the engine | quarantined_by implies quarantined_at (CHECK); quarantined_at alone is a gc orphan move (158 rows in the quarantine collections, RF-16); gc restores today drop two | ROW; refused from a client write; the bypass prefixes (`quarantine-*`) stay exempt from the allow-list; the 158 rows with quarantined_at alone were stamped by the engine's `gc_quarantine_orphans`, which `chunk_quarantine.py` calls and which writes two of the four keys (RF-22); the restore fix for the four keys is 6pfgb.40, and the Step 3 sweep strips retired document keys from quarantined rows too so a restore passes the validated CHECK (RF-23) | stays; CHECK in Phase 4 |
| last_written_at, retention, collection, embedding_model (columns) | the row | columns | ROW | stays |
| `nexus.frecency` table (telemetry-001: frecency_score, ttl_days keyed by chunk) | a third home with no client writer found | engine-internal writers UNCONFIRMED | retire, or list its writer and reader | 6pfgb.42 settles |
| doc_id, chunk_index, chunk_count | legacy (RDR-108 Phase 3) | on old rows; `indexer.py` ~3375 still sets doc_id | LEGACY; read by the engine's manifest-less census forward join, the rdr192 rung and `manifest_backfill.py` to attribute a manifest-less chunk (RF-19) until the tenant's rdr192 completion record exists and 6pfgb.39 lands; then swept; kept on `quarantine-*` rows until j67ix.1 moves the reattach plan's forward path off them (RF-24); chunk_index lives on as the manifest column (CHECK equal to position) | 6pfgb.39, sweep |
| source_path | legacy (RDR-102 D2) | absent | LEGACY; its dead client reads are removed in Phase 2; the engine's `/reapable` and reaper audit display read it until 6pfgb.39 (RF-19) | 6pfgb.39, sweep |
| store_type, corpus, git_meta, git_project_name, git_branch, git_commit_hash, git_remote_url | legacy (RDR-101 Phase 5c) | the repo PDF dict still sets the flat git keys | LEGACY; writer removed in Phase 0 | sweep |
| RDR-222 fence fields, lifecycle_state, tumbler | D or the collection | catalog columns | not row keys; listed so no one looks for them on the row | n/a |

Manifest redundancies the analysis found, carried by 6pfgb.42 in the
Phase 4 cut or after it (none is on the five Gaps' path): (tenant_id,
doc_id) determines collection but nothing checks it against the
document's physical_collection; chunk_index always equals position
(CHECK); labels_written false must mean NULL labels (CHECK).

The site-level detail behind the table (every writer and reader by file
and line, the `nexus.chunks` and manifest DDL history) is in the two census
records; the Phase 0 module carries symbols, not line numbers.

Two inconsistencies in today's key set, for Phase 0: `ManifestLabelStage`
answers bib_openalex_id and bib_doi, which `ALLOWED_TOP_LEVEL` does not
contain (the enrich writer sends them through `update-metadata`,
unvalidated); and the table's bypass collections (`taxonomy__*`,
`quarantine-*`, which `db/t3._BYPASS_SCHEMA_PREFIXES` exempts from
normalize today) stay exempt from the Java check and the CHECK, named as
such in the table and the engine check; the Step 3 sweep still strips
retired document keys from `quarantine-*` rows (not from `taxonomy__*`,
which no restore function targets: `quarantine_restore_chunks` accepts only
a registered quarantine source), because the three
gc restore functions copy a quarantined row's metadata into its origin
collection, where the CHECK applies (RF-23).

#### Enforcement at the engine (Phase 4)

`upsert-chunks`, `update-metadata` and the combined manifest writes
(`append`, `append_many`, `write_many`, all through `CombinedWriteService`)
validate each incoming metadata object against the engine's copy of the key
table:
any key not in the CHUNK set, for a collection outside the bypass prefixes,
returns 422 `chunk_metadata_key_refused` with the key and its home named.
`delete_keys` on `upsert-chunks` and `update-metadata`, and
`metadata_delete_keys` on the combined routes (RF-18), are not inspected; a
combined write without `metadata_merge` replaces the row and is validated
on its incoming keys like any other. Position fields in the manifest rows
and document fields on the catalog document are accepted as today. The key set
lives in Java beside `ManifestLabelStage.LABEL_KEYS` and is generated from
the Python table (one source, never two hand-kept copies). The check has a
log mode: it records the would-be-refused key, route and client version
without refusing, so the window before enforcement shows every writer the
client census could not see. Behind it, after the sweep, the database
CHECKs of the dependency-preservation table (allow-list on the metadata
keys with the bypass prefixes exempt, chunk_text_hash equal to the chash,
metadata embedding_model equal to the column, quarantined_by implies
quarantined_at) are added NOT VALID and then VALIDATEd through the
add-constraint and validate routes of 6pfgb.43, never in a startup
changeset (rdr180-001: a boot-time VALIDATE crash-loops an install whose
rows differ from the measured one; RF-23), so the property holds for every
writer, direct SQL included. Precedent for a NOT VALID CHECK on this
partitioned table: vectors-030 (vectors-036's copy sits in a rollback
block on a retired table, RF-24).

#### The pre-filter for selective document keys (Phase 3)

Route (b)'s `search_metadata_scoped_<dim>` is not the model: it is one
join ordered by the HNSW index, and under a selective filter the index
scan can exhaust its tuple budget and return short or empty (`PgSession`,
the nexus-bq06h record). Hybrid search already solved this shape
(`PgVectorRepository` hybrid dispatch, `SELECTIVE_GATE_MAX` = 5000): a
bounded probe fetches the chash set that satisfies the gate; a set under
the bound is ranked exactly by `text_gated_search_by_chash_<dim>`, a
primary-key `chash = ANY` filter; a set over the bound falls back to the
HNSW-first plan. That fallback is safe for hybrid because its gate is
evaluated inside the SQL under `hnsw.iterative_scan`, so the scan keeps
walking within its budget until the page fills, and a dense gate is found
within that budget (RF-21). It is not safe for a predicate the
post-search stage evaluates: the stage sees only 4x the limit (ceiling
1200), so a filter that more than 5000 chashes satisfy but fewer than one
in four of the nearest rows pass returns short. For a collection of N
chunks the band runs from 5000/N to about 25 percent of the collection;
the production database holds 599799 chunk rows across its collections
(RF-16), so on its larger collections the band is roughly 1 to 25 percent
of the chunks, and the parity gate's `tags=js` on the code collection is a
plausible member (RF-21: arithmetic from the two constants, not
measured). So the
pre-filter has two branches and neither uses the stage's cut: a set under
the bound is ranked exactly by `chash = ANY`; a set over the bound runs the
HNSW-first plan with the document predicate inside the SQL (an EXISTS on
`catalog_document_chunks` joined to `catalog_documents` on tenant,
collection and document, live owners only), and a page shorter than the
limit is re-run exactly: the probe is collection-scoped and applies the
search's own row predicates (live owner, retention, embedding_model), so on
this branch more than the bound's worth of chashes qualify and a short
page means the
iterative scan stopped at `hnsw.max_scan_tuples` or the `work_mem` budget
(RF-23; nothing signals the stop, the page size is the trigger). This
extends `exactOnUnderReturn` (nexus-bq06h) from an empty page to a short
one on this branch only, its recorded residual; the exact branch is
already exact and never re-runs; a dense-branch page is legitimately short
only when `n_results` exceeds the qualifying live rows, which the probe's
count rules out before the search (RF-22). The re-run orders the collection without the index (a
bitmap or sequential scan, then a sort), O(collection) per starved call,
within the two statement_timeout bounds bq06h records, and is counted. Positive label filters take the same EXISTS on the
manifest alone (nexus-p89h4, absorbed here), carrying the `labels_written`
row fallback the stage applies today (a legacy manifest row matches on the
row's label until re-index). The EXISTS only prunes: it admits a chunk,
not an owner, so the stage then re-evaluates the document and label
predicates per owning document over the admitted chunks (at the document's
lowest passing position, as today) to name the qualifying owner and its
labels, and the EXISTS carries the stage's full operator set (`$eq`,
`$ne`, `$in`, `$nin`, `$gt`, `$gte`, `$lt`, `$lte` on page_number, RF-24),
negative operators included, so no label predicate is left to the cut; a
label-only filter has no catalog probe, so its page is never re-run (it
can be legitimately short) and nexus-p89h4's pin is recall on a rare label
in a large collection. `category=` is
one predicate over its two homes like `tags=` (the GENERATED column for
indexed files, the meta key for notes). Whether a correlated EXISTS under
`hnsw.iterative_scan` fills a page for a far query is A8. The
pre-filter reuses hybrid's dispatch with the catalog as the gate: resolve
the qualifying documents from `catalog_documents` and its meta, collect
their manifest chashes up to the bound, and dispatch. The catalog resolve
and the manifest fetch carry tenant_id explicitly, as the read-path SQL
functions do. A hit from a shared chash is reported under the qualifying
owner the stage names, never under an owner the filter excluded. Fallback rules per key, applied in the resolution: title has
none (the catalog title always exists); `source_author=` admits a document
whose catalog author is empty when any of its rows carries the row value
(the stage's own rule, because markdown authors predate their catalog home
until .7's backfill, RF-24); content_type=pdf or markdown
admits a document whose `source_format` is absent when any of its rows
carries the row value (the same presence fallback as the stage, paid once
per candidate document); meta keys fall back to the row the same way while
the key is absent; `tags=` is one containment test over both tag homes
(the meta array for notes, the GENERATED one-element array for indexed
files; 6pfgb.38), so a note tagged arch and decision matches `tags=arch`;
while a note's meta still holds a legacy comma string, or the row fallback
applies, the string is split before the test. indexed_at and the non-id
bib_* keys (year, venue, citation count: range or dense filters) stay with
the post-search stage; `source_author=` and the id-valued keys (`bib_doi`,
`bib_openalex_id`, `bib_semantic_scholar_id`) name few documents like
`title=` and go through the pre-filter as well, because they under-fill
through the stage's cut today where they ran in-index before 0.1.165 (the
same class as .28's regression; pinned under .25).
Pins: `title=`, `source_author=` and `bib_doi=` in a collection larger
than 1200 chunks return the document's hits (the exact branch at the
default bound); a
filter at about one chunk in ten of a 20000-chunk fixture collection
returns a full page on the dense branch with the bound pinned to 500 (the
dispatch exposes its bound for tests, RF-22; at the default 5000 that
filter is 2000 chashes and takes the exact branch, so the dense branch is
unreachable on a fixture without the pinned bound); a dense filter
(category=prose on a prose collection) returns the same page as no filter;
a starved page on the dense branch (scan budget forced small) re-runs
exactly once (the global `exactFallbackCount` delta is one against a
baseline, in a serial test) and an exact-branch page leaves it unchanged;
a shared chunk is attributed to the qualifying owner. Cost: one catalog query and one
manifest chash fetch before the search, bounded by `SELECTIVE_GATE_MAX`,
and the in-SQL EXISTS against the plain HNSW plan, measured on the parity
gate's twelve filtered queries, with the exact re-run counter read across
the deploy.

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
note as its sole live owner in its collection and which
`note_ttl.row_may_give_tags` accepts (it refuses only a row whose stamp
AND title both name another note: Sam's reading of option B on
2026-10-26, which recovers renamed notes and `.nxexp` imports while a
departed co-owner's write gives nothing); a note recovered as sole owner
with no ttl_days anywhere gets `put_at` from that row's indexed_at and
ttl_days 0, the stamp Approach 6 names, so it leaves the keyless count; a
note with neither is counted residue per key (on the live tenant,
2026-10-26, the 8 notes with no stamped row were all sole owners with no
ttl_days, so option B recovers every one and the stamp fills their
`put_at`).
Indexed documents: extraction_* and frecency_score are copied from a
chunk with exactly one live owner; a document with none is counted
residue. Fill-only: never overwrite an existing meta key, with one named
exception: a legacy comma string under `tags` is converted to the array
once, idempotently (6pfgb.38). Authorship
rules from the schema analysis: group by the full row key K, never chash
alone; "single owner" counts tombstoned owners too (a deleted co-owner may
have written last); copy a value only when the row's `last_written_at` is
no later than the document's indexed_at plus five minutes of clock skew; a
document whose single-owner rows disagree on a value is residue, not
filled. Position labels (nexus-1nrcu) copy only when the chash has exactly
one manifest row in the tenant, not one live owner (RF-16: 4976 groups in
1752 documents recur inside their own document). tags, category and
source_format for indexed documents are not backfilled at all: they are
GENERATED columns (expressions under Phase 1's "Moved to Phase 3" item).
The tags the verb does fill for notes, from a row's comma string, are
written as the array (6pfgb.38's form), never as the string. The run asserts it examined at least one document; the
first live run's counts go on the bead. The backfill, the expiry sweep and the export share the two
helpers in `note_ttl.py` that implement option B (`row_written_by` for
the TTL, `row_may_give_tags` plus the caller's sole-ownership check for
tags and category), so the three agree. A failed owner lookup withholds
the keys and counts the note; it never fails the run.

### Existing Infrastructure Audit

Already built and reused: `ManifestLabelStage` and its presence fallback;
`docs_for_chashes`, `chash_positions`, `resolve_many`, `get_manifest`;
`_resolve_display_maps` (batched owner resolve per result page);
`catalog_store_hook_tracked` (the seam that writes note meta);
`_catalog_pdf_hook` and the markdown register call (the seams for
extraction meta and author); `note_ttl.row_written_by`; the catalog
backfill verbs beside which .10 sits; `rewrite_delete_keys`; hybrid
search's bounded chash-set dispatch and `exactOnUnderReturn` (nexus-bq06h)
as the pre-filter's model; the rdr192 upgrade-ladder rung as the sweep
vehicle's model (6pfgb.43); the wire ledger and RDR-223's log-then-enforce
mechanics; `src/nexus/tables/` checked tables.

### Decision Rationale

Content addressing is the right identity for a chunk: it is what makes
re-indexing cheap, citations stable and deduplication automatic (RDR-108,
RDR-180). The cost of that identity is that a row cannot say anything about
a document, because it may belong to several. The system has two other
records that can: the catalog document, which is per document by
construction, and the manifest, which is per (document, position) by
construction. Every document key already has or can cheaply get a home in
one of them, so what this RDR moves is the fields, not the identity, and
the three-relation decomposition is lossless because each join is on the
target relation's key with a foreign key guaranteeing the match (schema
analysis).
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
The same reasoning applies to the id-valued bib_* keys the stage already
answers, so they move to the pre-filter too. The pre-filter is the remedy.

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
- **Risk**: the Java set, the Python table and the CHECK's allowed-key
  array drift. **Mitigation**: the Java set and the add-constraint route's
  DDL are rendered from the Python table; a pin compares the live
  constraint's definition (`pg_get_constraintdef`) with the table on the
  live-engine fixture and fails when any of the three differs.
- **Risk**: a filter loses recall when answered after the overfetch cut,
  whether selective (title, the id-valued bib keys) or in the mid-density
  band (RF-21). **Mitigation**: the pre-filter's two branches, neither
  using the cut (Alternative 4 rejected); the recall pins on `title=` in a
  large collection and on a one-in-ten filter; the exact re-run on a
  starved dense-branch page; the parity gate's filtered queries compared
  across the deploy.
- **Risk**: a writer the client census cannot see (a subprocess, a shell
  script, a Java client) is refused at enforcement. **Mitigation**: the log
  mode window before enforcement (A5).
- **Risk**: a note re-put by a new client that writes no row keys leaves
  the old put's TTL on the row, and `catalog_ttl_stale` then holds the note
  forever. **Mitigation**: the frozen-row rule in Phase 4 Step 1 ignores a
  row whose `indexed_at` is not later than the catalog's `put_at` plus
  `PUT_SKEW`, and its pin uses a single-owner note with a legacy-keyed
  row.
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

- [x] The epic owner's in-flight children (.35, .36, .37, .33) pushed; they
      are adjacent and keep their beads (all four closed 2026-10-27 UTC,
      RF-22).
- [ ] Bead descriptions updated to this RDR's decisions at acceptance: .8
      and .23 (GENERATED columns, not stamped meta keys), .25 and .29 (the
      pre-filter, not the post-search stage), .26 (the flattened line
      fields from the manifest; it stays on the stage), .9 (computed_at),
      .27, .10 (fill-only with the tags conversion exempt; writes the tags
      it fills from row strings as arrays; stamps `put_at` for sole-owner
      notes with no TTL; re-points the two keyless tests at `put_at`; lists
      the notes held today by `catalog_ttl_stale`'s first sign alone for
      Sam before Step 1), .18
      (the import writes no row keys and stamps `put_at` now with ttl_days
      0 on a keyless entry; an entry's existing keys win),
      .32, .43 (the three routes and the one order), nexus-p89h4 (absorbed
      into .25's manifest EXISTS).
- [ ] The dry run and the live run of the backfill (A7) read by Sam before
      Phase 4 Step 1.
- [x] The six census queries of the schema analysis run read-only on the
      live tenant before this RDR locks (chunk_text_hash versus chash;
      repeated chash within a document; manifest collection versus
      physical_collection; chunk_index versus position; chash width;
      quarantine keys half present), counts recorded as research (RF-16,
      2026-10-27).
- [x] Sam's ruling on the tags domain (string matched by equality, or a
      set) before 6pfgb.8 and the pre-filter land: a set (2026-10-26,
      RF-17). The design changes the schema analysis forced (CHECKs behind
      the Java allow-list, GENERATED derived columns, frecency computed_at,
      authorship by full row key, one manifest row for 1nrcu, the .26
      re-scope) are accepted (Sam, 2026-10-26, RF-17).

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
(red after Phase 4 Step 1 without the lifecycle fix). Pair 2b, one note
as the sole owner of its row, which carries the legacy stamp, title and
ttl_days 7 and an `indexed_at` from the first put: a new-client re-put to
30 days expires at 30, because the row's `indexed_at` is not later than
the new `put_at` plus `PUT_SKEW`, so the rule ignores it.

Refusal legs: an `upsert-chunks` carrying `title` is refused with 422
naming the key and its home; the same content with `title` on the catalog
document and `line_start` in the manifest row of the combined write
succeeds; a `write_many`, an `append` and an `append_many` carrying `title`
in a chunk's metadata are each refused, with and without `metadata_merge`
(one leg per combined route); an `update-metadata` carrying `bib_doi` is
refused; a `delete_keys` naming `title` is accepted on `upsert-chunks`, and
a `metadata_delete_keys` naming `title` under `metadata_merge` on
`write_many`; an `.nxexp` import restamps no document key on a row another
document owns; the backfill dry run reports counts and stamps nothing. A
pin fails when `VectorHandler` or `CatalogHandler` gains a route that
accepts `chunks` or `metadata` (a body field the handler reads into chunk
metadata) outside the check's route list. All red on develop today except
the two delete-key acceptances, the accepted CHUNK-only positive control
and the route-list pin.

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
  catalog author column (.7); frecency_score in meta with a computed_at
  (.9, re-scoped); the bib writer recorded on the catalog document, not
  the row (.32, re-scoped); notes store tags as a JSON array with the
  one-time conversion of existing comma strings, the recovery bundle and
  the display following (.38); notes write label keys on their manifest
  rows (jz86a); labels backfilled for chashes with exactly one manifest row
  written before catalog-046 (1nrcu, re-scoped from one owner).
- Moved to Phase 3 because they are engine changesets: indexed documents'
  tags and category (.8) and source_format (.23) become GENERATED columns
  on `catalog_documents` from content_type and file_path, never stored
  meta keys (schema analysis, 3NF); the client reads them like any column.
  Expressions, written from the indexers' constants (RF-11) and the
  register values (RF-20), pinned by a test comparing the generated values
  with what each indexer writes on a fixture that holds every case below,
  the extension deciding before content_type (the register sites write
  the catalog content_types code, prose, rdr, pdf, paper and knowledge,
  RF-20; `orphan_backfill` derives one from the collection prefix for a
  synthesized document, `docs` among them, RF-23; a value outside the
  rules below yields NULL and is counted): `source_format`
  is `pdf` for the pdf extension or content_type pdf or paper, `markdown`
  for the md or markdown extension (`prose_indexer` routes both to the
  markdown chunker, RF-22) whatever the content_type, so an rdr or
  prose-registered `.md` file is markdown, `code` for content_type code,
  `prose` for any other prose or rdr file, NULL for knowledge (notes have
  no file); `category` is `code` for code, `paper` for paper, `prose` for
  pdf (the repo PDF path's constant), prose and rdr, NULL for knowledge;
  `tags` is `["markdown"]` for the md or markdown extension, `["pdf"]` for
  the pdf extension or content_type pdf or paper, otherwise the one-element
  array of the lower-cased, dot-stripped extension for code and prose (what
  `code_indexer` and `prose_indexer` write, RF-23; rdr files are `.md`, so
  the markdown rule covers them), NULL for knowledge and for a file with no
  extension (the indexers write the empty string there, `ext.lstrip(".")`
  of an empty suffix, so the fixture pin reads "" and NULL as equal,
  RF-24). A document with no
  extension to read (DEVONthink records keep their on-disk file_path,
  RF-20, so they have one) comes out NULL and is counted by the Step 3
  precondition before the row fallback goes.
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
  re-scoped from the post-search stage its text names to the pre-filter,
  both dispatch branches with the in-SQL predicate and the exact re-run on
  a starved dense-branch page, `source_author=` and the id-valued bib keys
  included, the new SQL function variant per dimension (the
  EXISTS-carrying HNSW-first form; `plain_search_<dim>` takes `p_where` and
  `p_where_path` today, RF-24; the exact branch reuses
  `text_gated_search_by_chash_<dim>` with the catalog's chash set and needs
  no new function) and the one index list of the pre-filter row (the
  existing md5(title) index, a partial md5(author) index, a partial bib_doi
  index; no plain btree on free text, RF-24); ledger entry naming both
  behaviour changes); positive label filters by the same manifest EXISTS
  (nexus-p89h4, absorbed); the tags containment predicate over both tag
  homes and the GIN index (.38, engine half); the flattened line fields
  and source_uri on a search row from one owner, deterministic and
  collection-scoped (.26, re-scoped: the citation span is already the
  chash form and text-determined; what moves is line_start, line_end and
  file:line, read from the manifest once per window); the GENERATED
  columns source_format, and tags and category for indexed files (.8,
  .23); the meta keys by the pre-filter once Phase 1 writes them and the
  backfill has run (.29, re-scoped the same way); route (b) SQL functions
  get the dispatch as their own designed and sized bead after route (a)
  (.41); route (b) position labels and route (c) get (u9dc5); a cap on
  per-hit expansion (6cmam); an old-client manifest rewrite must not reset
  labels_written (3xjlq); quarantine reattach reads the owner's file hash
  (j67ix.1); the engine's row-key readers move to the manifest and a chunk
  text prefix (.39); the gc restore functions strip all four quarantine
  keys (.40).
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
export's row path are deleted, not replaced, because every keyless note
with row values is now filled (`put_at` included, from the stamped row or
as the sole-owner stamp) and the residue has been accepted at the dry run;
`catalog_ttl_stale` stays until enforcement (old clients exist until
then). Its rule for a frozen row reads the row's `indexed_at`, not
`last_written_at`: the engine bumps `last_written_at` on every chunk
write, the identical-text re-write of a TTL-only re-put included (RF-18),
so after Step 1 a stale row and a fresh one carry the same engine clock
and it cannot tell them apart. After Step 1 no client writes `indexed_at`
or `ttl_days` to a row, so a row that still carries them was written by an
old client or before Step 1, and its `indexed_at` is that write's client
clock; the catalog's `put_at` is the client clock `note_catalog_meta`
stamps at the catalog write (RF-20). The rule: a row's TTL sign counts
only when the row's `indexed_at` is later than `put_at` plus `PUT_SKEW`
(five minutes; `catalog_ttl_stale` already compares `indexed_at` with the
catalog clock as its second sign, RF-22); a row whose `indexed_at` is not
later than `put_at` plus `PUT_SKEW` is ignored whatever its TTL or
`last_written_at`; that one threshold holds at every site. So a new-client
TTL-only re-put, whose merge leaves the single-owner row's old `ttl_days`
and old `indexed_at` in place, is not held: the row's `indexed_at` is not
later than `put_at` plus `PUT_SKEW`. An old-client re-put stamps `indexed_at` now, later
than `put_at` plus `PUT_SKEW`, and is caught as before. The `.nxexp`
import stops being a case: from `.18` the import writes no row keys and stamps `put_at` now
with ttl_days 0 on an entry that has no note keys (the export carries no
TTL the catalog could keep true, RF-23; an entry with keys keeps them), so
an imported note is permanent in the catalog unless its entry already said
otherwise. The import stamps only entries of notes (`store_put`-origin
documents; an indexed document gets no note keys). Notes held today by the
first sign of `catalog_ttl_stale` alone (the row TTL differs from the
catalog's while the row `indexed_at` is not later than `put_at` plus
`PUT_SKEW`: a pre-`.18` import's permanent row, or a note whose owner
changed its TTL by a new-client re-put, two shapes the rows cannot tell
apart) are counted and listed by the .10 dry run, not stamped: the catalog
TTL the owner set is the authority once Step 1 lands, and the ledger
records that an import's row no longer holds a note. Sam reads the list
before Step 1. An entry with no `put_at` is never treated as newer than its
rows. Pins: new-client TTL change on a single-owner note with a
legacy-keyed row expires on time; import that makes a note permanent is
not retired (through the catalog); old-client re-put is still caught; a
re-put of unchanged text bumps `last_written_at` on the engine (RF-18),
which the rule does not read. The RDR-223 journey test's title
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

#### Step 3: Sweep and constraints (mandatory, per tenant, measured precondition; vehicle 6pfgb.43; the manifest constraints and `nexus.frecency` by 6pfgb.42 in this cut or after it)

An allow-list-shaped sweep (keep only the CHUNK and ROW keys; a deny-list
would leave any key nobody listed and the CHECK would then reject the
row) over existing rows in batches, as a direct UPDATE that does not bump
`last_written_at`, with the bypass prefixes exempt from the CHECK but
`quarantine-*` rows included in the sweep (their retired document keys go,
the four ROW keys stay, and `catalog_doc_id`, `doc_id` and `chunk_index`
stay on them until j67ix.1 moves `quarantine_reattach_plan`'s forward path
off them, RF-24, so a later gc restore passes the validated CHECK, RF-23)
and the chash-length guard (a leftover 16-byte row would fail the existing NOT VALID
`chunks_chash_octet_check` on any update, RF-23; RF-16 found none on
2026-10-27), batched by tenant_id explicitly,
because the owner role sees every tenant's chunk rows (RF-16). Vehicle
(6pfgb.43), in one order: sweep every tenant, then add the allow-list
CHECK NOT VALID, then VALIDATE. Three engine-admin routes carry it: a
per-tenant sweep route with a dry-run flag that returns counts, driven on
every install by an upgrade-ladder rung (the rdr192 rung's shape; the
ladder runs on local-mode installs too, RF-22), so the sweep reaches the
installs RF-16 did not measure; a table-wide add-constraint route and a
table-wide validate route, each refusing while any tenant of the install
has a non-zero sweep count (the engine keeps the per-tenant counts, so
one tenant's rung cannot validate while another is unswept) and naming
the tenant. On a single-tenant local install the rung runs all three; on
the cloud the two table-wide steps are an operator verb (`nx t3
retire-keys --add-check`, `--validate`) run once every tenant's rung has
completed, or by an operator per tenant (`nx t3 retire-keys --tenant
<id>`) for a tenant with no ladder-running client, such as a gate tenant.
No chunk CHECK is added or validated in a startup changeset (RF-23:
rdr180-001 and vectors-004 record why, a boot-time VALIDATE crash-loops an
install whose rows differ from the measured one, and RF-16 measured one
database): the Step 2c cut carries no CHECK changeset; after the sweep the
add-constraint route adds the four `nexus.chunks` CHECKs NOT VALID (the
allow-list; chunk_text_hash equal to the chash; metadata embedding_model
equal to the column, the one uncensused chunk-table CHECK; quarantined_by
implies quarantined_at, after 6pfgb.40), and the validate route VALIDATEs
those four and the existing width CHECK `chunks_chash_octet_check`
(reading `pg_constraint.convalidated` first; nothing has validated it since
the chash-rekey rung retired, RF-23). The two routes carry `nexus.chunks`
constraints only; the manifest-table CHECKs (labels_written false means
NULL labels, chunk_index equals position, the collection rule: 6pfgb.42)
and the catalog-table CHECK (ttl_days implies put_at: 6pfgb.44) are counted
and added in their own beads' steps, after their own backfills. A tenant whose
client never upgrades: its reads keep working and its writes are refused
with 422 after Step 2c (not-additive, Consequences); its legacy rows stay
until the operator sweep runs for it, and only the table-wide steps wait on
it. The sweep batches 1000 rows; a direct UPDATE of metadata on a row that
carries a 3 KB or larger vector rewrites the row and, when the update is
not HOT, its HNSW index entry (inferred from the schema, not measured), so
the dry run reports rows per tenant and the live run reports the measured
cost of its first batch before continuing. Pin: a legacy-keyed row is still upserted after
the Step 2c cut and before the sweep. Dry run first, counts on the bead. Precondition, counted by the engine per tenant on each install and enforced by the sweep route for the rung and the operator verb alike: the
doctor counts of legacy-label manifest rows, keyless notes (no `put_at`),
home-less documents, NULL-span manifest rows and indexed documents whose
GENERATED columns are NULL are zero or each residue is accepted; the
engine's manifest-less census reads zero `legacy-unmanifested` (`nx t3
census-manifest-less --all --require-zero legacy-unmanifested`), because
the census's forward join, the rdr192 rung and `manifest_backfill.py`
attribute a manifest-less chunk by the row's `catalog_doc_id` or `doc_id`
(RF-19) and lose that after the sweep, and an accepted residue in that
bucket blocks the sweep for its collection, which the sweep route skips
and the count excludes; the two uncensused equalities of the dependency
table count zero violations; the engine reports the Phase 3 capabilities
the sweep relies on (the pre-filter route, the route (b) dispatch of .41
and u9dc5, the GENERATED columns), the countable form of "every Phase 3
bead is deployed", because route (b) evaluates `c.metadata @> p_where` on
the row until then (RF-3). The preconditions are the sweep route's own
checks, counted by the engine per tenant, so the rung and the operator
per-tenant verb go through the same gate. The rdr192 rung refuses to run
once the sweep has completed on the install (pin), and the engine's own
row-key readers (6pfgb.39), the gc quarantine audit's row-title label
included, have moved to the manifest and a chunk text prefix before the
sweep removes row titles and ids. Then the four `nexus.chunks` CHECKs are
added NOT VALID and VALIDATEd through the two table-wide routes
(concurrent writes allowed); the manifest and catalog CHECKs follow in
6pfgb.42's and 6pfgb.44's own steps. A doctor row counts rows still
carrying retired keys, not-applicable on a virgin box.

### Phase 5: External readers (conexus repository, after the engine cut carrying .8, .23, .25, .29 and .38's engine half is deployed, before Phase 4 Step 1)

conexus-wd1q (filed 2026-10-26, gated ahead of the engine redeploy that
follows Step 1). Lower bound: the gate's twelve queries filter on indexer
constants of indexed files (RF-13), whose tags and category are the
GENERATED columns of .8 and .23, never backfilled and absent only where
the Step 3 NULL count reads non-zero (a file with no extension, a value
outside the rules), so that count runs on the gate tenants before Phase 5
and confirms the papers and rdr content_type values RF-13 left
unconfirmed; `tags=` and `category=` are answered on route (a) by the
containment predicate of .38's engine half and by .29, so Phase 5 lands
after the engine cut carrying .8, .23, .25, .29 and .38's engine half is
deployed on the gated tenant; its recall SQL reads the generated columns
and applies the row fallback only to the NULL residue that count names.
Parity gate: the engine leg keeps its where strings (route
(a) answers them from the catalog after Phase 3); the recall leg's raw SQL
joins `catalog_document_chunks` and `catalog_documents` for category and
tags, counting chunks through the manifest since chashes recur across
collections; the two tag filters read the generated tags column.
`livec_findings.sql` reads the catalog title or file_path
in place of the regexp-stripped row title; its superseded-residue
predicate on indexed_at is redesigned over the manifest join (which side
supplies the older value is conexus-wd1q's decision). `etl_t3.py` strips
non-chunk keys from the rows it copies. `owner_1_1_census.py` reads
neither key in code and needs nothing. Rehearsal census predicates on
quarantine keys are unchanged (ROW keys). The rewritten gate's first run
against a Phase 3 engine is A6's spike.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| catalog meta keys (tags as an array, category, ttl_days, put_at, source_agent, extraction_*, frecency_score with computed_at) | In scope (`nx catalog show`) | In scope | N/A (document lifecycle) | In scope (doctor: documents missing a home key after backfill, keyless notes) | In scope (recovery bundle exports them, 6pfgb.14, the array by 6pfgb.38) |
| GENERATED columns (source_format; indexed files' tags and category) | In scope (`nx catalog show`, like any column) | In scope | N/A (computed) | In scope (doctor: indexed documents whose generated values are NULL) | N/A (recomputed) |
| the key table and its generated Java set | In scope (the module) | In scope | N/A | In scope (CI pin) | git |
| the would-refuse log | In scope (engine route or SQL view) | In scope | Ages out | In scope (empty before 2c) | N/A |
| retired keys on existing rows | In scope (doctor count) | N/A | In scope (Step 3 sweep, dry run first) | In scope (doctor row) | N/A |

### New Dependencies

None.

## Test Plan

- **Scenario**: pair 1, filter on tags and title under each owner —
  **Verify**: only the satisfying owner's hit, on route (a) (.25) and, once
  .41 lands, on route (b); red today.
- **Scenario**: `title=` in a collection of more than 1200 chunks —
  **Verify**: the document's hits are returned (the pre-filter); red under
  Alternative 4.
- **Scenario**: pair 1, citation address — **Verify**: span and source_uri
  are the searched document's, identical across runs; red today.
- **Scenario**: backfill dry run on a tenant with stamped notes, keyless
  notes and indexed documents — **Verify**: counts filled, skipped,
  converted (legacy tag strings, 6pfgb.38), residue per kind, with the
  five unrecoverable notes of the .35 census shape (ghosts and an empty
  row) in residue; a second run stamps nothing; non-vacuity assert fires on
  an empty tenant.
- **Scenario**: meta write with an empty value — **Verify**: sibling keys
  survive.
- **Scenario**: Step 0 re-index — **Verify**: manifest labels and spans
  unchanged.
- **Scenario**: pair 2 and pair 2b, re-put by a new client changing the
  TTL — **Verify**: expires on time; `catalog_ttl_stale` ignores the row
  whose `indexed_at` is not later than `put_at` plus `PUT_SKEW` even though
  its `last_written_at` is later than `put_at` (the engine bump of RF-18,
  asserted against a live engine); an old-client re-put (row `indexed_at`
  later than `put_at` plus `PUT_SKEW`) is still held.
- **Scenario**: `upsert-chunks`, `update-metadata` and each combined route
  (`append`, `append_many`, `write_many`) with a non-CHUNK key, in log mode
  and enforced — **Verify**: log row, then 422 naming key and home;
  `delete_keys` and `metadata_delete_keys` accepted in both modes; a
  `taxonomy__*` collection and a `quarantine-*` collection unaffected by
  the Java check; a pin fails when a handler route accepting `chunks` or
  `metadata` is outside the check's route list.
- **Scenario**: a legacy-keyed `quarantine-*` row before and after the
  sweep — **Verify**: the sweep removes its document keys and keeps its
  four quarantine keys and its `catalog_doc_id`, `doc_id` and `chunk_index`
  (RF-24); `gc_restore_rereferenced` and `quarantine_restore_chunks` each
  restore it into the live collection after the allow-list CHECK is
  VALIDATEd, and the reattach plan still names its forward owner (red
  without the quarantine rows in the sweep).
- **Scenario**: two tenants hold the same chunk text with retired keys; a
  pre-filtered search and a Step 3 sweep run under one tenant — **Verify**:
  the pre-filter's catalog resolve and manifest fetch read that tenant's
  rows only; the sweep's dry-run count equals that tenant's row count and
  the live run leaves the other tenant's keys in place.
- **Scenario**: a filter satisfied by about one chunk in ten of a
  20000-chunk fixture collection with the dispatch bound pinned to 500
  (the mid-density band, RF-21) — **Verify**: a full page on the dense
  branch; red under the post-search stage; the same filter at the default
  bound takes the exact branch and also fills.
- **Scenario**: `bib_doi=` naming one document in a collection larger than
  1200 chunks — **Verify**: the document's hits are returned; red through
  the stage since 0.1.165.
- **Scenario**: a starved page on the dense branch (scan budget forced
  small) — **Verify**: the exact re-run fires once (the global
  `exactFallbackCount` delta is one against a baseline, serial test) and
  fills the page; an exact-branch page leaves the counter unchanged.
- **Scenario**: a positive label filter against a legacy manifest row
  (`labels_written` false) and a `category=` filter against a note —
  **Verify**: the legacy row matches on its row label; the note matches on
  its meta category; the hit names the owning document.
- **Scenario**: a note put with tags `arch,decision` by the new client, and
  a legacy note whose meta holds that comma string — **Verify**: the new
  note's meta tags is the array; `tags=arch` matches both after the one-time
  conversion; the recovery bundle round-trips the array (6pfgb.38).
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
`where=`, and its dense branch replaces the stage's cut with an EXISTS
inside the HNSW scan; both costs are measured on the parity gate's twelve
filtered queries across the Phase 3 deploy, with the exact re-run counter
read before and after, not estimated here.

## Finalization Gate

### Contradiction Check

Round 1 (2026-10-27, T2 `nexus_rdr/236-gate-critique-2026-10-27-r1`)
found statements left behind by earlier revisions: stamped meta keys
beside the GENERATED columns, tags as an array beside the string the
writer stores, a CHECK added before the sweep beside one added after, A1
stated for one write mode, and stale counts. This revision swept every
site the critique listed. The facts that live in more than one section and
must agree: the write modes (Terms, Technical Environment, A1, Enforcement,
MVV, RF-18); the homes of source_format, tags and category (Approach 2,
RF-7, RF-17, the key table, the backfill verb, Phase 1, Phase 3, Day 2);
the pre-filter's two branches and what the stage keeps (Approach 4, the
pre-filter section, Gap 4, A6, Alternative 4, Phase 3, the Test Plan); the
sweep's order, vehicle and preconditions (the dependency table,
Enforcement, Transforms, Approach 6, Step 3, Cross-Cutting Concerns,
RF-19); the keyless-note definition and the 432 count (Approach 6, RF-8,
RF-11, A7, Step 1, Step 3); the frozen-row rule's clock and its one
threshold (Risks, MVV pair 2b, Step 1, the Test Plan); CHECK placement (no
chunk CHECK in a startup changeset: the dependency table, the order
sentence, Transforms, Enforcement, Step 3, Cross-Cutting Concerns); the
quarantine rows in the sweep (RF-4, RF-19, the dependency table, the key
table, the bypass paragraph, Step 3, the Test Plan, Scope); the index list
of the selective probes (the pre-filter row, Phase 3, Revision History);
which table each CHECK route carries (the dependency table, Step 3 twice);
the operator set and owner attribution (Approach 4, the pre-filter
section). Round 2 (`nexus_rdr/236-gate-critique-2026-10-27-r2`) found the
sweep order and the quarantine rows disagreeing across sites after the
fix checks had closed, and its fix check
(`nexus_rdr/236-fix-check-2874ec21d`) found the index list and the route
scope disagreeing; this revision swept them.

### Assumption Verification

A1 verified with the combined-write replace mode as its exception
(RF-18); A2 and A6 verified as stated, A6 with the findings predicate
left to conexus-wd1q's design; A3 and A4 verified with the exceptions
their status lines name (three Phase 2 edits; the legacy and NULL-span
manifest rows the Step 3 precondition counts); A5 verified for the conexus
path census with the would-refuse log at Phase 4 Step 2b as its final
check; A7 verified for notes with row values and for the derived keys,
false for the five notes with nothing to recover, its extraction and
frecency residue measured by the backfill runs; A8 assumed, with its spike
(pinned dispatch bound, far query) due before .25 lands and the exact
re-run as the guard if it fails.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/vectors/upsert-chunks`, `update-metadata` metadata merge | nexus-service | Source Search |
| `catalog_documents.metadata` update | nexus-service `CatalogRepository` | Source Search |
| `docs_for_chashes`, `chash_positions`, `resolve_many` | nexus-service (0.1.164) | Source Search, Spike |

### Scope Verification

The MVV is the Phase 4 integration module and is not deferred. Work the
analysis surfaced that is not on the five Gaps' path has a bead and a
phase: the manifest redundancy constraints and `nexus.frecency` (6pfgb.42,
the Phase 4 cut or after), the gc restore four-key fix (6pfgb.40; the
restore of legacy-keyed quarantined rows is covered by the sweep including
`quarantine-*`), route (b)'s dispatch (6pfgb.41), the ttl_days CHECK
(6pfgb.44). nexus-p89h4 is absorbed into Phase 3.

### Cross-Cutting Concerns

- **Deployment model**: Phase 4 is not-additive, deployed in the RDR-223
  Phase 3 order: log mode, clean log, one paired cut with conexus armed.
- **Incremental adoption**: Phases 0 to 3 are additive and ship with the
  epic's existing cadence.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: the backfill verb pages by document.
- **Tenant isolation**: the pre-filter's catalog resolve and manifest
  fetch, the sweep's batches and the census it depends on all carry
  tenant_id explicitly, because RF-16 found the owner role sees every
  tenant's chunk rows under `SET LOCAL ROLE` (cause unconfirmed); a
  two-tenant test in the Test Plan pins the pre-filter and the sweep.
- **Local-mode installs**: the engine routes and the sweep reach every
  install through the Phase 4 cut and its upgrade-ladder rung (6pfgb.43);
  RF-16 measured one production database, so each install counts its own
  preconditions before its add-constraint and validate steps, and no chunk
  CHECK is added or validated in a startup changeset (RF-23).

### Proportionality

Right-sized for a change that touches every writer, 23 client readers,
four engine readers, three routes and one external repository. The key
table is the section future readers need; the phase lists exist to map 36
open beads (29 at the round 1 gate, six added by its fix round, one by
round 2's) and may be trimmed to bead ids at lock. The CHUNK set ends at two keys that both
equal a column, guarded by the Java allow-list and the CHECK: the
allow-list is what names a key and its home in a 422 and what can run in
log mode, the CHECK is what covers direct SQL; neither alone does both.

## References

- T2 `nexus/x54wv-shared-row-key-census-2026-10-26 (1/2)`, `(2/2)`;
  `nexus/plan-6pfgb-shared-row-keys-2026-10-26`;
  `nexus/3ajpd-content-hash-census-2026-10-25`;
  `nexus/tko2w-key-split-2026-10-25`;
  `nexus/rdr236-engine-and-position-key-census-2026-10-26`;
  `nexus/rdr236-a3-owner-resolution-batching-2026-10-26`;
  `nexus/chunk-row-normalization-external-readers-2026-10-26`;
  `nexus/critique-rdr236-draft-2026-10-26`; `nexus_rdr/236-research-1`
  to `-24`; `nexus_rdr/236-gate-critique-2026-10-27-r1` and `-r2`;
  `nexus_rdr/236-fix-check-fe55f7efc`, `-9628991be` and `-2874ec21d`.
- `docs/agents/catalog-t3-split.md`; `docs/architecture.md`;
  `docs/wire-contract-pending.md`.
- `service/src/main/resources/db/changelog/catalog-046-manifest-section-labels.xml`;
  `ManifestLabelStage.java`; `VectorHandler.java`.
- RDR-108, RDR-180, RDR-192, RDR-222, RDR-223, RDR-231, RDR-232.
- Beads: nexus-6pfgb and children; nexus-tko2w; nexus-u9dc5; nexus-j67ix;
  conexus-wd1q.

## Revision History

- 2026-10-26: created from Sam's direction and the x54wv census.
- 2026-10-26: schema analysis added at Sam's request (T3
  `nexus-architecture/analysis-rdr236-chunk-schema-normalization-2026-10-26`):
  dependency set, normal form before and after, anomaly map of the 23
  reader rows, lossless-join and dependency-preservation tests, a
  denormalization table and SQL transforms. Design changes it forced:
  database CHECKs behind the Java allow-list after a mandatory
  allow-list-shaped sweep; source_format and indexed files' tags and
  category become GENERATED columns (3NF), moving .8 and .23 to Phase 3;
  frecency carries a computed_at; backfill authorship by full row key with
  tombstoned owners and a last_written_at guard; 1nrcu requires one
  manifest row; .26 re-scoped (the span is already chash-form); tags needs
  a domain ruling; `nexus.frecency` and the still-written content_hash
  listed; the chunk-table description corrected.
- 2026-10-26: research verification pass: A3 restated as the verified
  fact with Phase 2 edits named; A5, A6 and A7 verified from source and
  the live note census (RF-11 to RF-13); the conexus copy tool `etl_t3.py`
  and the findings SQL's indexed_at predicate added to Gap 5 and Phase 5.
- 2026-10-26: confirmation critique (T2
  `nexus/confirm-critique-rdr236-draft-2026-10-26`) found three leftovers,
  fixed here: the `write_many` combined route is in the refusal; the
  pre-filter is modelled on hybrid search's bounded chash-set dispatch, not
  route (b); the backfill applies option B per key; plus the import case of
  the frozen-row rule, Phase 5's lower bound, the `update-metadata` overlay
  writers in Step 1, and register terms.
- 2026-10-27: fix-check round on the round 2 fix (T2
  `nexus_rdr/236-fix-check-2874ec21d`, RF-24): no plain btree on title or
  author (the md5 index forms, a partial bib_doi index); the chunk routes
  carry the four `nexus.chunks` CHECKs only, the manifest and catalog
  CHECKs in .42 and .44; the author row fallback; a blocked collection is
  skipped and excluded from the count; the engine counts the preconditions
  per tenant for the rung and the operator verb alike; `.39` reads the
  tenant's ladder record; the quarantine sweep keeps the reattach plan's
  three keys until j67ix.1; the pre-`.18` held notes are listed for Sam,
  not stamped; Phase 5 gated on .29 and .38's engine half too, with the
  NULL count first; the EXISTS carries every label operator and the stage
  attributes; A8's spike pins the bound; citations and the 36 count.
- 2026-10-27: Gate round 2 — BLOCKED (2 Critical, 14 Significant, 2 ship-blocker(s)); commit `9628991be`; critique `nexus_rdr/236-gate-critique-2026-10-27-r2`.
- 2026-10-27: fix round after gate round 2 (RF-23): no chunk CHECK is
  added or validated in a startup changeset (the existing width CHECK is
  VALIDATE-only through the route); the sweep strips retired document
  keys inside `quarantine-*` rows so the gc restore passes the validated
  CHECK; Phase 5 gated on the cut carrying .8, .23 and .25 with the
  generated tags column; an accepted `legacy-unmanifested` residue blocks
  its collection's sweep and .39's condition is the ladder record; the
  starved-page trigger defined and one wording; the EXISTS prunes and the
  stage attributes, with A8 added; the verb stamps `put_at` for sole-owner
  notes; the author and bib_doi indexes (corrected to the md5 and partial
  forms by the fix check); GENERATED set and extension
  form; `.18` stamps a keyless entry and pre-`.18` imports are counted;
  one frozen-row threshold; 6pfgb.44 for the ttl_days CHECK; route (b)
  ordered before the sweep; `source_author=` to the pre-filter; the
  operator per-tenant sweep and batch cost; wording corrections.
- 2026-10-27: Gate round 1 — BLOCKED (3 Critical, 13 Significant, 3 ship-blocker(s)); commit `b67783a25`; critique `nexus_rdr/236-gate-critique-2026-10-27-r1`.
- 2026-10-27: fix-check round (T2 `nexus_rdr/236-fix-check-fe55f7efc`,
  RF-22): Step 3 gets one order (sweep every tenant, add the allow-list
  CHECK NOT VALID, VALIDATE) through three engine-admin routes with no
  legacy-failable CHECK in a startup changeset; the frozen-row rule reads
  `indexed_at` against `put_at`, and `.18` moves the import's TTL to the
  catalog; the exact re-run is scoped to a starved dense-branch page; the
  mid-density pin pins the dispatch bound; keyless means no `put_at` with
  `.10` stamping sole-owner notes and re-pointing both tests; GENERATED
  expression precedence; label and category two-home predicates with the
  `labels_written` fallback; `.39` drops the census forward join; wording
  and count corrections the three checks listed.
- 2026-10-27: fix round after gate round 1 (RF-18 to RF-21): the
  pre-filter's dense branch evaluates the predicate inside the search SQL
  with an exact re-run on a short page, the id-valued bib keys and
  nexus-p89h4 join it; tags gets its implementing bead (6pfgb.38) and the
  array claim is corrected; Step 3 gains the manifest-less census
  precondition, a vehicle (6pfgb.43) and the engine readers (6pfgb.39); A1
  restated per write mode; one keyless-note definition and the 432 count;
  frozen-row clocks named; combined-route refusal legs and the route-list
  pin; beads 6pfgb.40 to .42 for the gc restore, route (b) and the manifest
  constraints; tenant isolation and local installs in Cross-Cutting
  Concerns; stale counts corrected.
- 2026-10-26: revised after the pre-gate critique (T2
  `nexus/critique-rdr236-draft-2026-10-26`): refusal becomes an allow-list
  on the live routes with log-then-enforce; key table gains LEGACY rows and
  one home per key; Phase 3 selective filters move to a pre-filter and the
  6pfgb.24 facts are corrected; A7 restated per kind under Sam's .35
  ruling; note TTL lifecycle, chunker split, phase order, MVV fixtures and
  register terms fixed.
