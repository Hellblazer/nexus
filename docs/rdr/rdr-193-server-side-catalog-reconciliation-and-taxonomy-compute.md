---
title: "Server-Side Catalog Reconciliation and Taxonomy Compute: Move the Index-Time Catalog Diff/Housekeeping/Linking and the Taxonomy Discover Pipeline onto the Engine as Transactional SQL and Java Jobs"
id: RDR-193
type: Architecture
status: draft
resumed_date: 2026-11-03
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-08-15
accepted_date:
related_issues: [nexus-ap8l0, nexus-ejo6k, nexus-w5xgy, nexus-7lw6a, nexus-rqsh1, nexus-1vhfj]
related: [RDR-070, RDR-075, RDR-095, RDR-108, RDR-155, RDR-156, RDR-164, RDR-181, RDR-191, RDR-194, RDR-210, RDR-223, RDR-231, RDR-236, RDR-241, RDR-243]
---

# RDR-193: Server-Side Catalog Reconciliation and Taxonomy Compute

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md.

Terms used throughout. The **engine** is the Java nexus service that owns
every store in Postgres (PG) in both modes: a bundled PG17 with pgvector on
the laptop, or the managed service in the cloud. The **catalog** is the
engine's document registry (`nexus.catalog_documents` and its links). A
catalog document is addressed by a **tumbler**, a hierarchical address such
as `1.2.5` whose first two segments name its **owner** (for a git repository,
one owner per repository). A document's **manifest** is its rows in
`catalog_document_chunks`, one per chunk position, each naming a **chash**
(the SHA-256 of the chunk's text, which is the chunk's identity in T3, the
`nexus.chunks` table). An **index run** is one `nx index repo`; a **delta
run** is the `--since-head` form that every automatic post-commit run takes,
which indexes only the files changed since the last indexed commit. A
**round trip** is one HTTP request from the client to the engine and its
answer.

A few more terms. The **walk** is the client's pass over a repository's
files in one run: a **complete walk** visits every indexable file (a full
run), a **partial walk** only the changed ones (a delta run). **Frecency** is
a per-file score from git history (how often and how recently the file was
committed) that ranks search results. The **fence** is a document's index
state (`index_state`: `indexing`, `complete` or `failed`, with the
`index_content_hash` of the bytes it indexed), which a run sets when it
starts writing a document's chunks and settles when it finishes, so a run
that died midway is visible. The **edge** is the managed service's public
HTTPS front, whose origin timeout is about 30 s. A job's **envelope** is the
JSON answer a finished engine job returns. A **ghost** is a catalog document
that no longer stands for a live file of its own (a stale path, or a second
registration of the same file).

## Problem Statement

Indexing does catalog and taxonomy work on the client, one HTTP call at a
time, against state whose canonical copy lives on the engine host. The work
is set-shaped (diff a file list against stored rows, age missing rows,
resolve paths to tumblers, cluster a collection's embeddings) and the engine
holds every row it touches. Batching in July and August 2026 bought constant
factors: one owner read per run, batch routes for register, update, manifest
writes and taxonomy assignment, and a narrower hook lock. It left the shape
in place. The client owns the reconcile logic, the link resolution and the
clustering, and each further fix is another page size or another batch
route. This RDR moves the logic to where the rows are.

The cost today is real and small. The reopen measurement (the baseline in
Research Findings) puts the walk-shaped part at a few seconds per automatic
run; it grows with the catalog, and it is not yet a cost anyone has
complained of. The case for the change is the architecture (work that grows
with the tenant done one row per request, on every box) and the correctness
defect that rides with it (Gap 4), not a present slowdown.

### Enumerated gaps

This RDR closes Gaps 1, 2 and 4. Gap 3 is recorded here because the August
draft claimed to close it; this revision does not, and says why under Gap 3.

#### Gap 1: Catalog registration is a client-side reconcile loop over a downloaded copy of the catalog

Per `nx index repo` run (`src/nexus/indexer.py`):

- `_catalog_hook` (:1329) reads the owner's full document list once
  (`cat.by_owner`, :1520) and joins it in Python as `{file_path: entry}`.
- For each file it evaluates a change predicate with five arms
  (:1736-1760): `head_hash`, `physical_collection`, `source_mtime`, a
  content-hash change, and a stale register-time stamp. Part A1 states the
  arms exactly.
- It writes changed rows through paged `update_many` and new rows through
  paged `register_many` (`_CATALOG_REGISTER_PAGE = 1000`, :337). When a
  page fails it falls back to one `register` per file (:2053).
- `_run_housekeeping` (:2507) reads the owner list a second time. It resets
  or increments `miss_count` in each document's metadata, detects renames by
  content hash, moves links onto the renamed document and deletes orphans.
  Each write is one request (`w.update`, `w.link_if_absent`,
  `w.delete_document`), made serially. A `delete_many` client method exists
  (`http_catalog_client.py:2023`) and is unused here.
- Housekeeping runs only on a complete walk. The delta path sets
  `skip_housekeeping`, because a miss-count sweep over a partial walk would
  age every unvisited document toward deletion (`_maybe_run_housekeeping`,
  :2438, the nexus-fltb4 mass-delete class).
- Four other paths read the owner list again, each only on its own
  condition. Three run before the hook (:6458): `_stored_paths_among`
  (:1122, called at :5992) and `_warn_symlink_ghosts_with_dependents`
  (:1172, called at :5998) when a still-present file has become
  unindexable, and `_pdfs_needing_index` (:3267, called at :6033) when the
  run has PDFs. `_reconcile_needs_fence` (:2338) runs at run exit and
  re-reads the current fence state of every document the run registered or
  changed. A fifth, `_build_frecency_doc_id_map` (:2881), belongs to the
  separate `--frecency-only` mode (:2992, :3062); a normal run takes its
  frecency map from the hook's own result (:6487).
- `nx index md` (`doc_indexer.py` `_register_or_lookup_doc_id` :1015 and
  `_catalog_markdown_hook` :3845) and `nx store put`
  (`catalog/store_hook.py` `catalog_store_hook_tracked` :955) resolve
  identity with several round trips per document; the markdown hook repeats
  owner and path lookups the register step already did (counted at 3-6 per
  document on 2026-08-15, research-3; not re-counted for this revision).

The client work that needs the client is: walking the tree, `stat`, sha256,
the ephemeral-path guard, the pre-registration chunkability guard (Gap 3),
content classification, and the regex scans that find link targets (Gap 4).
The diff, the upsert, the miss-count state machine, rename detection and
path resolution are set operations over rows the engine already holds.

#### Gap 2: Taxonomy discover downloads the entire collection to cluster it, though embeddings are born on the engine

The flush-time assign step is already server-side
(`nexus.assign_from_chashes_<dim>`, Liquibase `taxonomy-006`, shipped
conexus 7.4.0 and engine v0.1.68; its cross-collection pass was rewritten
onto the HNSW index, pgvector's graph index for approximate nearest
neighbours, by `taxonomy-018` and pinned by `taxonomy-020`). What
remains on the client is four orchestrators (discover, rebuild, split,
`project_against`), the labeler's per-topic reads and writes, and per-pair
cross-link writes:

- `discover_for_collection` (`commands/taxonomy_cmd.py` :296, through
  `_discover_via_service` :180 and `_fetch_service_vectors` :100) pages
  every chunk's text to the client (250 per request, with
  `sensitivity="exclude"` so no sensitive chunk is read, RDR-243) and then
  every embedding (`t3.get_embeddings`), runs sklearn HDBSCAN for n ≤ 5000
  (`min_cluster_size=5`, `max_cluster_size` from `_max_cluster_size`, "eom"
  (excess of mass) selection of clusters from its tree, centroid centres) or
  MiniBatchKMeans above that
  (`k = max(10, √n/3)`, seed 42), plus c-TF-IDF labels (class-based term
  weighting, `CountVectorizer(stop_words="english")`, top 3 terms) in
  `db/t2/taxonomy_compute.py` (`_cluster` :204), and ships every assignment
  back (`persist_discovered_topics` carries each topic's full chash list).
- Cross-links: `compute_cross_links` (`db/t2/http_taxonomy_store.py`
  :1067) downloads all foreign centroid vectors and does the cosine matmul
  on the client; `persist_cross_links` (:1355) posts one
  `/links/upsert` per pair.
- `project_against` (:2091, through `_svc_fetch_all_embeddings` :1843)
  downloads every embedding of every source collection a second time under
  `--all`.
- The labeler `relabel_topics` (`taxonomy_cmd.py` :2542) makes one
  `get_topic_doc_ids` read per topic (:2624) and one `update_topic_label`
  write per topic (:2719).

Order of magnitude for a 100k-chunk, 1024-dimension collection (estimated
2026-08-15, not re-measured): about 740 bulk round trips and 1.2-1.6 GB of
egress as JSON float arrays (about 400 MB even as binary float32) for one
discover, roughly double under `--all`. Embeddings are computed on the
engine in every mode (`http_vector_client.py:4014`
`upsert_chunks_with_embeddings`: "forward chunk text, ignore caller's
embeddings"), so the download carries data that never needed to leave the
host. No clustering exists anywhere in `service/`; pgvector exposes no
k-means or HDBSCAN primitive (IVFFlat's build-time k-means is internal).

#### Gap 3: Registration precedes chunkability, so unchunkable files can become phantom catalog documents

Found 2026-08-15 in the nexus-3n7pr forensics. The reconcile loop of Gap 1
registers a catalog document for a file before the chunker decides whether
the file has any content it can chunk. A file that produces no chunks keeps
a tumbler, an owner, a `physical_collection` and `chunk_count 0`, with no
manifest, and nothing retracts it. Of the 910-document "zero-manifest"
population that drove weeks of forensics, about 600 were this class
(binary and data files that slipped the docs filter, zero-byte files); every
census reported them as gaps and prescribed a re-index that could not
change anything.

The interim fix shipped in 7.9.0 (nexus-rqsh1): a pre-registration guard
skips zero-byte, whitespace-only and binary-sniffed files before the hook
sees them, census surfaces report a `zero_content_by_design` bucket, and a
one-time sweep tombstoned the existing phantoms. A still-present
whitespace-only file that already has a document is retired in the same run
(nexus-5j8fp, indexer.py:5983-6001). The guard is a heuristic in front of
the ordering, not a change to it: a file the guard admits that still chunks
to nothing is registered first and left behind.

**This RDR does not close Gap 3.** The August draft had the engine refuse a
staged row marked unchunkable. That check could never fire: the guard
removes unchunkable files before the hook (indexer.py:5709-5964), and the
hook is handed only `indexed_for_catalog` (:6408-6424, passed at :6458), so
every row the engine would see says "chunkable". The real fix creates a new
document together with its first manifest write. That reorders the client
pipeline, because the hook hands the indexers each file's tumbler before
chunking starts (`_catalog_hook` returns the `{abs_path: doc_id}` map,
:6458). The reorder is its own decision. Step 6 counts the documents a full
run leaves with no manifest, so that decision starts from a number.

#### Gap 4: Index-time link generation lists whole content types across the tenant to link one repository's new files

Taken over from RDR-241 (no client walks of the managed catalog). RDR-241's
census sorts every whole-tenant read into classes, and its **HOT PATH**
class is a whole-tenant read on the index path itself. It has one member,
link generation (`link_generator.py:60`, where `_entries_of_type` calls
`all_documents(content_type=...)`), and RDR-241's scope table assigns it to
this RDR.

After registration, `_catalog_hook` (indexer.py:2165-2260) runs four
generators over the run's new tumblers:

- `generate_rdr_filepath_links` lists every `rdr` and every `code` document
  in the tenant (link_generator.py:277-278).
- `generate_prose_filepath_links` lists every `prose`, `markdown` and `docs`
  document, plus every `code` document (:758-761).
- Both scan the new files on disk for path strings. They resolve each path
  against a dict of every code document in the tenant, keyed by its
  repo-relative `file_path` (:282-286). Two repositories that share a
  relative path collide in that dict and the last listed wins, so an RDR
  can be linked to another repository's file.
- `generate_rdr_dependency_links` (:559) works through `rdr_resolution`
  (:533). That lists every `rdr` and every `prose` document in the tenant
  and keeps this repository's rows on the client (`is_in_repo`). `prose` is
  the default content type for all markdown, so the list grows with every
  markdown document of every repository. It then reads frontmatter from
  disk, and each `resolve_path` it calls costs one `resolve` round trip
  (`http_catalog_client.py:2660`).
- `generate_pdf_corpus_links` (:811) lists every `pdf` and `paper` document,
  groups them by file-content hash, and links duplicates `same-as`.

Every candidate edge then costs `link_if_absent`: a `link_query` existence
check plus a `POST /link`, two round trips per new edge
(`http_catalog_client.py:2962-2990`). The engine has no batch link-write
route. Its list route cannot combine an owner filter with a content-type
filter (the branches of `CatalogHandler.handleList` are exclusive,
`CatalogHandler.java:517-551`), so a client that wanted only its own
repository's RDRs cannot ask for them.

Seed gates keep most runs from listing at all. A generator returns without
reading when no new tumbler has its source type (`_no_qualifying_seed`,
link_generator.py:63). The dependency linker probes up to four new
tumblers one at a time before it decides to list (`_RDR_SEED_LOOKUP_CAP`,
:505-531). So the listings run on runs that add a qualifying document, not
on every run. The baseline does not say how the dependency linker's 4-6 s
splits between its two listings (about 530 rows on this repository, by the
comment at :499), the `resolve` round trips and the frontmatter reads.
Latency may matter more than list size.

The same walks reach the index path a second way. After the run,
`_relink_changed_pdfs` (indexer.py:2307, called at :2811) calls
`relink_changed_pdfs` (link_generator.py:998) whenever the run registered or
changed a document. Its `prune_stale_same_as_links` (:917) pages through
every `content_hash_dedup` same-as link, and when one touches the run it
lists every `pdf` and `paper` document (:972-976). When the run changed a
PDF, the PDF linker then lists them again.

Two callers outside `nx index` share these functions. `nx rdr set-status`
calls `rdr_resolution` (commands/rdr.py:689, :741). `nx catalog
generate-links` (commands/catalog_cmds/links.py:666) runs every generator as
a full pass and the same-as prune.

## Relationship to Prior RDRs

Searched `docs/rdr` for "reconcile", "housekeeping", "link_generator",
"taxonomy discover", "staging" and "jobs"; read the RDRs below.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-156 (closed) | Precedent | Charter "push work to the engine". This RDR applies it to index-time catalog and taxonomy work. |
| RDR-181, RDR-164 (closed) | Precedent | Server-side embed-skip and lifecycle cascades: the same diagnosis for other write paths, shipped. |
| RDR-191 (closed) | Precedent | Unified `nexus.chunks` and `nexus.taxonomy_centroids`, the manifest-chunk FK, and server-side GC that retired the client prune. Chunk cleanup after a trash is already the engine's job. |
| RDR-070, RDR-075 (closed) | Origin | Taxonomy design. "Not LLM-based topic naming at discover" is an explicit non-goal, kept: the LLM relabel stays on the client. |
| RDR-194 (closed) | Context | Its D2 added `catalog_links` tumbler FKs with ON DELETE CASCADE (changeset `catalog-032`, the id this RDR had reserved). A rename must move links before the old document is purged, or the cascade removes them. |
| RDR-223 (accepted) | Context | Retired the staging landing schema (`staging-003`, Sam 2026-09-30) and set the rule that chunk rows never land before owner rows. The reconcile landing tables in this RDR hold catalog rows only, never chunks. |
| RDR-236 (accepted) | Adjacent | Document fields live on the catalog document and position fields on the manifest; the chunk row keeps only what its text determines. Reconcile writes catalog rows only and must keep RDR-236's register-time meta rules (Technical Design, Part A1). The discover job reads `chunk_text` and embeddings, which RDR-236 leaves on the chunk row. |
| RDR-241 (draft) | Adjacent draft | Same diagnosis for tenant-wide aggregates; hands this RDR the index-time link walk (its scope table). Its ratchet lint is designed, not built: no walk table exists under `src/nexus/tables/`. Boundary below. |
| RDR-243 (accepted) | Context | Sensitive documents: discover and relabel must keep excluding them; `sensitive` moves on an existing document only through the audited route. |
| RDR-210 (closed) | Precedent | `ReembedJobRepository`: an engine job with PG-persisted rows, one of the two job precedents (Research Findings). |

Scope boundary with RDR-241:

| Concern | RDR-193 | RDR-241 |
| --- | --- | --- |
| Per-owner reconcile, housekeeping, rename detection in `nx index` | yes | no |
| Taxonomy discover, projection, cross-links, labeler batching | yes | no |
| Link generation on the index path (census HOT PATH) | yes (Gap 4) | names it, leaves it here |
| Tenant-wide aggregates in doctor, console, catalog verbs, MCP tools | no | yes |
| The ratchet lint over `all_documents` call sites (a test that lists every surviving call site and fails on a new one) | removes the HOT PATH row once the index path makes no tenant-wide listing (Step 7); the full-pass `generate-links` listings stay, as BULK JOB rows | owns the lint |

## Context

### Background

Designed and gated in August 2026 (two gate runs, PASSED at run 2), then
deferred on 2026-09-16 because every motivating bead had closed without a
user complaint. Sam reopened it on 2026-11-03 when RDR-241 left the
index-time link walk to it, with the instruction to measure first. The
measurement found no older baseline to call a regression against and
recorded the current per-run costs as the baseline (Research Findings).
This revision renumbers the reserved changesets, re-reads every code claim
against today's tree, adds Gap 4, and updates the design for what shipped
in between: the staging schema and `RekeyJobs` are gone, the ap8l0 plan
problem is fixed, the delta path skips housekeeping, and RDR-236 and
RDR-243 added register-time rules.

### Technical Environment

- **Every install is service mode.** The catalog, T2 and T3 are reached
  through the engine over HTTP in both modes (RDR-155, RDR-158, RDR-186).
  ChromaDB and SQLite are not live substrates. New persistent state goes to
  PG through Liquibase (NO-SQLITE rule).
- **No silent fallbacks.** A route the engine lacks fails loud; the client
  never recomputes locally (the `assign_from_chashes` / nexus-yu9w5
  precedent). The client's engine floor (`REQUIRED_ENGINE_VERSION`,
  `src/nexus/engine_version.py:747`) moves in the same client release as
  the engine cut that carries the routes.
- **SQL functions are the established shape** for set work on the engine:
  `catalog-004` manifest functions, `catalog-006/007/008/012` combined-query
  functions (one per vector dimension, `CREATE OR REPLACE`, explicit GRANT
  re-issue), `catalog-020` index-run fence, the `gc_*` procedures behind thin
  Java wrappers in `VectorHandler`, and `taxonomy-006` `assign_from_chashes`
  (VOLATILE plpgsql, SECURITY INVOKER, forced row-level security (RLS)
  scoping each tenant, a data-modifying common table expression, that is a
  `WITH` clause that writes).
- **Batch repository methods exist:**
  `CatalogRepository.registerDocumentManyWithOutcome` (:1331, N documents in
  one transaction, tumblers claimed in input order through `claimNextSeq`
  :1735 on the owner's `next_seq`, with the `highestChildSeq` self-heal
  :1685) and `updateDocumentsMany` (:2075).
- **Soft delete** is a `deleted_at` column on `catalog_documents` and
  `catalog_links`, set by the `nexus.document_trash` function
  (`catalog-003`); `nexus.purge_trash` removes rows physically later.
  `catalog-016` makes `source_uri` unique among live non-empty values
  (`ux_catalog_documents_live_source_uri`). `file_path` has no unique
  index, because it is repository-relative and two owners can share one
  (the `catalog-016` header). The document's jsonb column is `metadata`
  (`catalog-001-baseline.xml:75`); the client and the wire call it `meta`.
  `miss_count` is a key in `metadata`, not a column.
- **Request timeouts.** The edge's origin timeout is about 30 s
  (`ChunkKeysSweepJobs` javadoc). A request whose duration grows with the
  corpus can time out at the edge while its transaction commits anyway
  (GitHub issue 1390 was one), so such work runs as an engine job: start,
  answer with an id, poll.
- **The engine carries a numeric substrate already:** onnxruntime and DJL
  tokenizers (local bge-768 embedding), jOOQ, and a GraalVM native-image
  build. Pure-Java code with no reflection is native-image safe.

## Research Findings

### Investigation

Pass 1 and 2 (2026-08-15, research-1 to research-13): source reading of the
reconcile loop, housekeeping, the manifest and link repository methods, the
discover pipeline and sklearn surface, the job registry of the day, and two
spikes (c-TF-IDF in SQL against sklearn; the HDBSCAN parity fixture).

Pass 3 (2026-11-08, research-14 to research-24, for this revision): re-read
every file and line this RDR cites against the working tree at 8ed7bc865,
read the changelog directory for the next free ids and for what shipped
since August, read RDR-236, RDR-241 and RDR-243 for constraints, read the
link generators for Gap 4, and took the reopen measurement from T2. Line
numbers below are from that tree.

Pass 4 (2026-11-08, research-25 to research-38, round 2 for the pre-gate
critique, T2 `nexus/rdr193-pregate-critique-2026-11-08`): re-read the
change predicate, the register pass and its cross-owner branch, the
housekeeping and rename code, the fence and same-as maintenance after the
run, the engine's register and update methods line by line, the link write
path on both sides, the job registry's constants, the engine-release gates
and RDR-241's ratchet. The code is the same (the 8ed7bc865 tree;
the first revision changed only this file). Every claim Part A1 makes about today's behaviour
cites one of these findings.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Liquibase changelog (`service/src/main/resources/db/changelog`) | Yes | `catalog-032` and `taxonomy-008` taken; next free `catalog-048`, `taxonomy-022`; `staging-003` dropped the staging schema |
| PostgreSQL 17 + pgvector 0.8.2 | Yes (spike, pass 1) | `to_tsvector('english')` and `ts_lexize` reproduce sklearn's top c-TF-IDF term; no clustering primitive |
| sklearn HDBSCAN / MiniBatchKMeans | Yes (pass 1) | Parameters and the eom cap the Java port must reproduce (research-5) |
| Engine job registries | Yes (pass 3) | `RekeyJobs` deleted; `ChunkKeysSweepJobs` and `ReembedJobRepository` live |

### Key Discoveries

Pass 1 and 2 findings, with line references updated where the code moved:

- **Verified** (source search): Tumbler minting can move into plpgsql:
  `claimNextSeq` is an owner-row `SELECT ... FOR UPDATE`, a
  `max(next_seq, highest_child_seq) + 1` self-heal, and an `UPDATE`, in one
  transaction with no Java-only state. *Source:
  CatalogRepository.java:1459,1685-1748 (research-1)*
- **Verified** (source search): Rename precedence today is last-write-wins
  over `by_owner`'s `ORDER BY tumbler` (lexicographic), an accident.
  `catalog_links` conflict key is `(tenant_id, from_tumbler, to_tumbler,
  link_type)` with an existing merge that folds attribution into
  `metadata.co_discovered_by`. *Source: indexer.py:2525-2650;
  CatalogRepository.java:4380-4420 (research-2)* This finding's August
  conclusion, that the SQL rule would be a declared behaviour change, is
  withdrawn: research-26 restates the rule in full and the commit
  reproduces it.
- **Verified** (source search): `nx store put` registers a note under an
  `nx-note://<collection>/<title>` source URI (RDR-231; the engine
  canonicalises a legacy `chroma://` URI); `_find_ghost_by_title` is a
  ghost-only third tier; `_catalog_markdown_hook` also backfills title and
  year and applies the ephemeral-worktree guard. *Source:
  store_hook.py:784,955,1033; doc_indexer.py:1015,3845 (research-3)*
- **Verified** (source search): `miss_count` is meta-only; the meta merge
  is `||`. *Source: indexer.py:2570-2645 (research-4)*
- **Verified** (source search): The exact sklearn surface the Java port
  must reproduce (parameters, cap, eom, centroid store, MiniBatch branch,
  `_merge_labels` greedy algorithm at 0.8, `compute_split` KMeans
  n_init=10) and what is incidental. *Source: taxonomy_compute.py:186-260,314,400 (research-5)*
- **Verified** (source search): Nothing downstream depends on c-TF-IDF
  tokenisation; the terms feed only the LLM relabel prompt. *(research-6)*
- **Verified** (source search): `assign_batch` and `compute_assignments`
  have no production callers outside the store itself; the live projection
  callers are `project_against` (commands/index.py:2534;
  taxonomy_cmd.py:230,2939,3160) and the cross-links pair
  (http_taxonomy_store.py:1578-1579). *Source: grep 2026-11-08 (research-7)*
- **Verified** (source search): Discover CLI semantics to preserve:
  `--all` enumeration, `local_exclude_collections=["code__*"]`
  (config.py:1391), the existing-topics preflight, `--force` routes to
  rebuild, `record_discover_count`, and per-collection failure isolation in
  post-index auto-discover. *(research-9)*
- **Verified** (source search): Pure-Java clustering needs no native-image
  metadata, and no Java vector-math utility exists to reuse. *(research-10)*
- **Verified** (spike, PG 17.5 bundled): `to_tsvector('english')` class
  TF-IDF reproduces sklearn's top term in 8/8 proxy clusters (top-10 prefix
  overlap 4-8/10); stems display as their most common surface form via
  `ts_lexize`. *(research-12)*
- **Verified** (source search): Parity fixture
  `tests/fixtures/hdbscan_rootgrab_400x1024_f16.npz`: capped run gives 16
  clusters with the largest 40; uncapped gives 2 clusters [359, 23].
  *Source: tests/fixtures/PROVENANCE-hdbscan-rootgrab.md (research-13)*

Pass 3 findings (this revision):

- **Verified** (source search): The reserved ids are taken:
  `catalog-032-links-tumbler-fk.xml` and `taxonomy-008-link-types-jsonb.xml`.
  The changelogs top at `catalog-047-document-sensitivity.xml` and
  `taxonomy-021-cross-preview.xml`. *(research-14)*
- **Verified** (source search): The staging schema is dropped
  (`staging-003-drop-landing-schema.xml`, nexus-z0o2p.27, Sam's decision of
  2026-09-30), with `StagingHandler` deleted. *(research-15)*
- **Verified** (source search): `RekeyJobs` was deleted (nexus-lgdel.l1).
  The live precedents are `ChunkKeysSweepJobs` (memory-only, one running
  job per tenant, 503 when full, 409 for a conflicting request, 404 for an
  id the engine does not hold, answered by starting again, 30-minute
  retention, `evictTenant` on tenant reset) and `ReembedJobRepository` (job
  rows in PG, RDR-210). *(research-16)*
- **Verified** (source search): The nexus-ap8l0 plan problem is fixed:
  `taxonomy-018` rewrote the cross pass as a per-chunk LATERAL over HNSW
  (11.8-13.8 s exact against 0.26-0.35 s per 270-chunk batch) and
  `taxonomy-020` pins the plan. *(research-17)*
- **Verified** (source search): The link generators list whole content
  types tenant-wide and filter on the client; the list route cannot combine
  owner and content type. *(research-18)* Path resolution is tenant-wide by
  repo-relative path, so a cross-repository collision mis-links; each new
  edge costs two round trips, and no batch link-write route exists.
  *(research-19)*
- **Verified** (source search): Delta runs skip housekeeping; deletions
  there are explicit same-run tombstones (`_delete_docs_for_paths`,
  indexer.py:1027); held-back files count as present. *(research-20)*
- **Verified** (source search): Register-time rules added since August:
  the content hash stamped in metadata at register; `tags` and `category`
  fill-only-if-empty; `source_format` re-stamped every register; `sensitive`
  set on a new document only, moved on an existing one only through
  `/sensitivity`; the dicts the hook fills for its caller (fence
  bookkeeping) need each row's `index_state` and `index_content_hash`. *(research-21)*
- **Verified** (source search): RDR-243 on taxonomy: discover reads with
  `sensitivity="exclude"`, relabel skips topics holding a sensitive
  document, and the engine already has the exclusion predicate
  (`vectors-055`). `nexus.chunks.chunk_tsv` is a stored
  `to_tsvector('english', chunk_text)` column. *(research-22)*
- **Verified** (source search): Gap 3's interim guard is in production
  (nexus-rqsh1, 7.9.0); `catalog-016` removed duplicate live `source_uri`
  rows, so multi-collision renames come from identical content at several
  paths. *(research-23)*

Pass 4 findings (round 2):

- **Verified** (source search): The change predicate has five arms, not
  four. Besides `head_hash`, `physical_collection` and `source_mtime`
  (exact compare), `content_hash_changed` is true only when the file was
  read (an empty hash is a read failure, "inconclusive, not a change") and
  its hash differs from the stored one; `stamp_stale` is true when a key of
  the register-time stamp (RDR frontmatter id, `source_format`, and `tags`
  and `category` after the fill-only filter) is absent or different.
  `content_hash_changed` also feeds two lists the run needs later:
  `relink_rdr_tumblers` (an edited RDR is re-linked) and `needs_fence`
  (fence bookkeeping and the PDF same-as relink). A new document enters
  `needs_fence` too. *Source: indexer.py:1736-1760, 1300-1325, 1987-1993;
  indexed_meta.py:126-139 (research-25)*
- **Verified** (source search): Today's rename rule. The target map holds
  every owner row present this run (`keep` rows included) that carries a
  content hash, read after registration; when several share a hash, the one
  `documentsByOwner` lists last wins (it orders by `tumbler`,
  CatalogRepository.java:3444). Every missing row with a matching hash is
  renamed at once onto that target, several onto one if they match; no
  second miss is awaited. A rename carries the `non_evidentiary` stamp
  (nexus-4jj40), redirects both endpoints of a link through the run's
  rename map (nexus-12ml0), drops a link that becomes a self-link
  (nexus-x3bxj), skips a link touching a row the sweep deletes this run or
  whose other endpoint is not live, and copies `created_by` and spans but
  not the link's metadata. *Source: indexer.py:2507-2650 (research-26)*
- **Verified** (source search): `link_if_absent` checks existence with
  `link_query` on the pair it was given and skips when a row exists. The
  engine's `POST /link` is an upsert: on conflict it overwrites the spans
  and folds a second creator into `metadata.co_discovered_by`. It first
  resolves each endpoint through `alias_of` and locks both rows, and it
  accepts a self-link. *Source: http_catalog_client.py:2962-2990;
  CatalogRepository.java:3841-3880, 4336-4420 (research-27)*
- **Verified** (source search): Document identity. The client keys an
  existing document on `(owner, file_path)`. The engine's register derives
  `source_uri` from the owner's `repo_root` (an owner with no `repo_root`
  leaves it empty), then looks it up tenant-wide before it tries
  `file_path` inside the owner. So a register can land on another owner's
  live row (nexus-53cae: 198 RDRs under the curator owner). The client then
  counts the file as reconciled, uses that row's tumbler as the file's
  document, and changes nothing on it; it repoints a stale
  `physical_collection` only on a row of its own owner. *Source:
  indexer.py:1520, 1696-1700, 2006-2060; CatalogRepository.java:914-926,
  1331-1430; path_ambiguity.py:278-340 (research-28)*
- **Verified** (source search): Invariants of the register and update
  methods the commit replaces. Register: the owner row is ensured first
  (`ensureOwnerRow`); an explicit `source_uri` outside the owner's
  `repo_root` is refused (the containment guard); two new rows with one
  `source_uri` share one tumbler; a lost insert race re-selects the winner;
  a new row inherits `sensitive` from its most recent tombstoned predecessor
  and is audited; a flagged request that lands on a live row raises it.
  Update: an alias row's update lands on its canonical
  (`batchAliasTargets`); metadata is merged with `jsonb_concat`; a written
  `source_uri` is stored in its canonical spelling (`NoteUri`); a
  collection move rewrites a note URI and refuses collisions; `chunk_count`
  is re-derived from the manifest unless the update is metadata-only;
  `indexed_at` moves only when `head_hash` changes. *Source:
  CatalogRepository.java:967-975, 1052-1069, 1404-1606, 2075-2100,
  2226-2245, 2280-2372 (research-29)*
- **Verified** (source search): The owner row's `head_hash` is the base of
  the next `--since-head` delta. It is written at run end, and only when no
  file was deferred or failed. A commit at registration time that wrote it
  would move the base before any file is indexed. *Source:
  indexer.py:2800-2853, 264-300 (research-30)*
- **Verified** (source search): Of the narrower owner reads, three run
  before the hook and one at run exit, against the fence state as it is
  then; `_build_frecency_doc_id_map` belongs to `--frecency-only` only.
  Explicit deletions are applied in the prune phase after indexing.
  *Source: indexer.py:5992, 5998, 6033, 6458, 2338-2420, 3062, 7907-7936
  (research-31)*
- **Verified** (source search): Gap 3's guard runs before the hook, so the
  hook never sees an unchunkable file. *Source: indexer.py:5709-5964,
  6408-6458 (research-32)*
- **Verified** (source search): The rest of Gap 4. The PDF same-as anchor
  is `min(group, key=str(tumbler))`, a text order; a seeded anchor gets
  every member linked. `relink_changed_pdfs` prunes then relinks after the
  run. `rdr_resolution` also serves `nx rdr set-status`; `generate-links`
  runs full passes. *Source: link_generator.py:63, 505-531, 889-900,
  917-1027; commands/rdr.py:689, 741; commands/catalog_cmds/links.py:666-740
  (research-33)*
- **Verified** (source search): Projection reads ids and embeddings with
  `sensitivity="include"`; split reads texts with `"exclude"`. RDR-243 does
  not mention projection. *Source: http_taxonomy_store.py:1817, 1860, 2147
  (research-34)*
- **Verified** (source search): `ChunkKeysSweepJobs` holds one running job
  per tenant; a second start with the same request fingerprint is handed
  the running job, a different one is refused. It also carries a dry-run
  or apply kind, cancel for an apply, a check of who started the job,
  `MAX_RUNNING` 2, `QUEUE` 8, `RETENTION` 30 minutes, `MAX_RUNTIME` 4 hours
  and `MAX_RETAINED` 64, all across tenants. *Source:
  ChunkKeysSweepJobs.java:34-57, 355-371 (research-35)*
- **Verified** (source search): The migration rehearsal's
  `--candidate-migration` leg was deleted; `run.sh` takes `--package-upgrade`
  and `--acquire` only. The engine-release skill's gates are Step 2 (engine
  green on the exact commit), 3g (no held changeset), 4 (tag), 5
  (`--acquire` against the published bytes), 5b (the migration-release
  branch for a tag that carries a changeset), 6 (relay to conexus, whose
  pre-deploy gate is a Liquibase walk on a point-in-time fork of
  production) and 6.1 (the client-path gate through the edge). *Source:
  docs/agents/engine-service-release.md (version-is-tag-stamped and deploy
  bullets); tests/e2e/migration-rehearsal/run.sh:157-162;
  .claude/skills/engine-release/SKILL.md steps 2-6.1 (research-36)*
- **Verified** (source search): RDR-241 is a draft and its ratchet table
  does not exist yet. *Source: docs/rdr/rdr-241 frontmatter and lines
  186-218; `src/nexus/tables/` listing (research-37)*
- **Verified** (source search): A read-only path for aggregate counts
  against a live store exists: `run_diagnostic_sql` lints for aggregate-only
  SQL and runs it in a read-only transaction as the `nexus_diag` role.
  *Source: src/nexus/db/diag_connection.py:130-160;
  grants-nexus-diag.xml header (research-38)*

**The reopen baseline** (research-24; T2
`nexus/rdr193-reopen-measurement-2026-11-03`). Six automatic post-commit
delta runs of this repository on 2026-11-03, laptop, managed tenant,
catalog about 43,745 documents. Catalog operations per run:

| Files changed | Catalog ops total | Whole-catalog-shaped part |
| --- | --- | --- |
| 1 | 9.0 s | `all_documents` 1.1 s (2 calls) |
| 2 | 15.4 s | `all_documents` 5.1 s (4 calls); linking 10.1 s (rdr 4.0, rdr-dependency 6.0) |
| 3 | 12.0 s | `all_documents` 1.7 s; linking 4.8 s (rdr-dependency 4.7) |
| 5 | 31.4 s | linking 0.2 s |
| 10 | 48.8 s | `all_documents` 1.2 s; linking 4.5 s (rdr-dependency 4.4) |
| 22 | 102.3 s | not walk-shaped: `write_manifest_many` 67.2 s (7 calls), taxonomy assign 11.4 s |

Reading, as the memo states it: the walk-shaped index-time cost is about
4-6 s per run when an RDR is in the delta, and `all_documents` is 1-5 s per
run. Both grow with per-type document counts; neither is yet a felt cost.
The large runs are manifest writes, outside this RDR. Because these were
delta runs, housekeeping did not run in any of them, so the baseline says
nothing about Part A's housekeeping savings; no full-walk run was measured.
These figures are "the baseline" cited below.

### Critical Assumptions

- [ ] A set-based commit can reproduce the client's change predicate,
  miss-count machine, rename rule, register-time metadata rules and the
  engine invariants of the methods it replaces, row for row and link for
  link. **Status**: Unverified. The rules are read from source
  (research-25 to research-31), which says what parity means; it does not
  show that one plpgsql function achieves it. Gated at Step 2 by the parity
  fixtures (old path and commit on one seeded fixture, every column of
  every row and every link compared), with rename and stamp cases
  mandatory. **Method**: Source Search, then Spike (at implementation)
- [x] Tumbler minting can run inside plpgsql. **Status**: Verified.
  **Method**: Source Search (research-1)
- [x] c-TF-IDF in SQL matches sklearn's top term. **Status**: Verified.
  **Method**: Spike (research-12)
- [ ] A Java HDBSCAN port reproduces sklearn on the parity fixture (adjusted
  Rand index, ARI, a 0-to-1 agreement score between two clusterings, at
  least 0.90; 16 clusters with the largest 40 capped; [359, 23] uncapped).
  **Status**: Unverified, gated at Step 10 (Phase 3) with that pass or fail
  bar. **Method**: Spike (at implementation)
- [ ] A 20,000-row commit finishes under 30 s on the test substrate.
  **Status**: Unverified, gated at Step 2 (Phase 1).
  **Method**: Spike (at implementation)

## Proposed Solution

### Approach

Move the set work onto the engine. State changes become one transaction;
SQL wherever SQL can express the work; a Java job only where an algorithm
(clustering) has no SQL form, and a job (start, poll) wherever duration
grows with the corpus. The client keeps the work that needs the local
filesystem or the operator's LLM: walking, hashing, guards, classification,
regex scans of file text, frontmatter reads, and the relabel prompt.

- **Part A (Gap 1):** a reconcile route pair (stage rows, commit as a job)
  replaces the owner read in the hook, the Python diff, the per-file
  writes and housekeeping.
- **Part A4 (Gap 4):** one batch link-write route that resolves paths to
  tumblers by the file's identity, never by a tenant-wide dict; a
  list-route filter that returns one owner's documents of given types;
  SQL functions for the PDF `same-as` grouping and its prune. The index
  path then makes no tenant-wide listing, and each generator makes one
  write per run instead of two round trips per edge.
- **Part B1 (Gap 2, SQL):** whole-collection projection, centroid
  cross-links, and labeler batch routes as SQL functions and routes.
- **Part B2 (Gap 2, Java):** the discover, rebuild and split pipeline as an
  engine job with a Java HDBSCAN and k-means port.

### Technical Design

Interfaces, not implementations. Every route is tenant-scoped inside
`tenantScope.withTenant`, like every catalog route.

#### Shared: the engine job registry

Phase 1 extracts a shared `http/EngineJobs` registry from
`ChunkKeysSweepJobs`. This is a refactor, not a move: the existing class
carries domain the new kinds do not want (research-35). `EngineJobs` takes
the scope key, the request fingerprint and the bounds (running, queued,
retained, retention, maximum runtime) as constructor parameters. The
chunk-keys wrapper keeps what is its own: the dry-run or apply kind,
cancel for an apply, and the check of who started a job. Its existing tests
run unchanged against the wrapper.

Each job kind gets its own instance (`reconcileJobs`, `discoverJobs`, and
`chunkKeysSweepJobs` on the refactored class), so a running catalog commit
never blocks a discover for the same tenant. The scope key is the tenant
for discover and chunk-keys, and the tenant plus owner for reconcile, so
two repositories indexed at once (two post-commit hooks) never wait on each
other. A start whose fingerprint matches the running job of its scope is
handed that job (`202` with its id); a start with a different fingerprint
gets `409` naming the running job. The bounds for the reconcile instance
are set at Step 1 for many tenants committing at once; chunk-keys keeps 2
running and 8 queued.

A poll for an id the engine does not hold answers 404; that is the whole
"engine restarted" story, and it is safe because every job here is
idempotent to resubmit (reconcile by the run header's stored outcome,
discover by the existing-topics guard). The client contract, shared by
every job client:

- `409`: poll the named running job; do not resubmit.
- `404`: log `engine_job_lost` and resubmit once.
- `503` (registry full): wait and resubmit, at most three times with
  backoff, then fail loud.
- `failed`: surface the error and exit non-zero; never continue the run as
  if the job succeeded.

`ReembedJobRepository` (PG-persisted rows) is the other precedent and was
not chosen: these jobs are short and idempotent, and a persisted job table
is one more schema surface with nothing to recover that a resubmit does
not.

#### Part A1: reconcile on the engine (changeset `catalog-048-reconcile.xml`)

The bar is parity: after a commit every catalog row and link is exactly
what today's client path leaves. The rules below are read from the code
(research-25 to research-31). Where the design departs from today, it says
so and the departure is listed under Consequences.

**1. Landing tables**, both with forced RLS and the tenant isolation
policy, in schema `nexus` because schema `staging` is retired. They hold
catalog rows only, never chunks, so the RDR-223 ordering rule is untouched.
(Open question 1 asks whether Sam wants landing tables at all.)

- `nexus.catalog_reconcile_runs(tenant_id, run_id, owner_tumbler, walk,
  expected_rows, state, outcome jsonb, created_at)`, primary key
  `(tenant_id, run_id)`. The first stage call of a run creates the header
  with the walk mode and the number of rows the client will send.
- `nexus.catalog_reconcile_rows(tenant_id, run_id, seq, kind, file_path,
  content_type, title, physical_collection, head_hash, source_mtime,
  content_hash, base_metadata jsonb, stamp jsonb, sensitive)`, primary key
  `(tenant_id, run_id, file_path)`, unique `(tenant_id, run_id, seq)`.
- `seq` is the file's position in the run (0-based, set by the client).
  New tumblers are minted in `seq` order, which is today's input order.
- There is no `source_uri` column. The engine derives it (below), because
  only the engine knows the owner's registered `repo_root`.
- `base_metadata` is what `_register_time_meta` builds before the stamp:
  the content hash when the file was read, and `non_evidentiary` for a
  fixture path (indexer.py:1300-1325). `stamp` is the register-time stamp
  read from the file and its path (RDR frontmatter id, `source_format`,
  `tags`, `category`), before the fill-only filter, which needs the stored
  value and so runs on the engine.
- `kind` is `present` (offered for register or update), `keep` (exists on
  disk but held back this run; counts as present, never written), or
  `deleted` (an explicit same-run deletion: a git deletion in a delta run,
  or a still-present path the walk now excludes, nexus-d6qmz).
- Deduplication: the primary key refuses a second row for one `file_path`
  in a run, at stage time, with a 400 naming the path. The walk yields each
  path once (symlinks are skipped, nexus-gnw42), so a duplicate is a client
  bug, not data.

**2. `nexus.catalog_reconcile_commit(p_owner text, p_run_id text) RETURNS
jsonb`**: plpgsql, VOLATILE, SECURITY INVOKER (RLS scopes every row), one
transaction. The walk mode, `complete` or `partial`, comes from the header.

*Transaction scope (declared change).* Today a failed register page falls
back to one register per file, and a failed file is recorded and skipped
while the run goes on (indexer.py:1801-1815, 2072-2110). The commit is all
or nothing, with one exception: refusals it can classify before writing. A
row whose derived `source_uri` fails the containment guard, or that carries
a value the catalog refuses, is returned as `refused` with its reason and
not written, and the rest commits. Any other error fails the whole commit;
the client exits non-zero before any chunk is written, so no chunk lands
without its document.

*Guards first.* A header in state `committed` returns its stored outcome and
changes nothing, which is what makes a resubmit after a lost job safe. A
header whose staged row count differs from `expected_rows` fails the commit
(`incomplete_stage`) before any write, so a lost stage page can never read
as a set of missing files. No header fails with `run_not_found` (reaped or
never staged).

*Match.* Each `present` and `keep` row is matched to a target in this order
(research-28):

- (a) The live row under the owner with the same `file_path`, which is the
  client's key today. When several match, the one `documentsByOwner` lists
  last (greatest `tumbler` in that query's order, CatalogRepository.java:3444)
  is the target, because that is the row the client's dict keeps today; the
  others are reported as `duplicate_path` and not touched.
- (b) Otherwise, the live row whose `source_uri` equals the derived one.
  The derivation is `deriveSourceUri`'s rule (CatalogRepository.java:914-926),
  reimplemented in plpgsql: a relative `file_path` anchored on the owner's
  `repo_root` and lexically normalised, or empty when the owner has none.
  If that row is under the owner, the action is `same_owner_by_uri`: its
  tumbler is returned and a stale `physical_collection` is repointed, as
  `reconcile_stale_physical_collection` does today, and nothing else is
  written. If it is under another owner, the action is `foreign_owner`: the
  tumbler is returned (the client uses it as the file's document, as it
  does today), nothing on that row is written, and the outcome names both
  owners. Whether such a file should get a document of its own is the
  nexus-53cae question, not this RDR's.
- (c) Otherwise the row is new.

Legacy rows with an empty `source_uri` are matched by (a), as today. The
commit does not backfill their `source_uri` (today's update never sends
one), so they are neither duplicated nor collided with. Step 0 counts them.

*Update* each arm-(a) target of a `present` row when the five-arm predicate
holds (research-25):

- `head_hash`, `physical_collection` or `source_mtime` differs, compared
  exactly, with no tolerance;
- or `content_hash_changed`: the staged `content_hash` is non-empty and
  differs from `metadata->>'content_hash'` (absent reads as empty). An
  empty staged hash is a read failure and is not a change;
- or `stamp_stale`: the effective stamp has a key whose stored value is
  absent or different (jsonb equality). The effective stamp is the staged
  stamp with `tags` and `category` dropped where the stored value is set,
  that is, not null, not an empty string and not an empty array (the
  `is_unset` rule, indexed_meta.py:126-139).

The update writes `head_hash`, `physical_collection`, `source_mtime`, and
`metadata = metadata || base_metadata || (effective stamp without the keys
of base_metadata)`. Every metadata write is a merge, never a replacing
`SET` (nexus-ke45f). A row the predicate does not select is not rewritten.
A `keep` row is never updated.

*Insert* each new row with a tumbler minted in `seq` order under the
`claimNextSeq` contract: owner row `FOR UPDATE`, cursor at the greater of
`next_seq` and the highest child sequence, one increment per row, one
`next_seq` write. Its metadata is `base_metadata || stamp` (stamp keys never
replace base keys, as `setdefault` does today). Its `sensitive` is the
staged flag or a tombstoned predecessor's flag. Two new rows that derive
one `source_uri` share one tumbler. The insert keeps today's race backstop:
`ON CONFLICT (tenant_id, source_uri) WHERE deleted_at IS NULL AND
source_uri <> '' DO NOTHING`, then the winner is selected again and
reported as `same_owner_by_uri` or `foreign_owner` by its owner.

*Invariants carried from the replaced repository methods* (research-29),
each with its own test at Step 2:

- The owner row exists before minting; when absent it is created behind
  the identity guard (`ensureOwnerRow`).
- The containment guard: a derived URI outside a repo owner's `repo_root`
  is a `refused` row.
- An update addressed at a row with `alias_of` lands on its canonical
  (`batchAliasTargets`).
- `chunk_count` is re-derived from the manifest on every update that is
  not metadata-only, and left alone on a miss-count touch.
- `indexed_at` moves only when `head_hash` changes (nexus-927mo).
- Any `source_uri` the commit writes is in `NoteUri` canonical spelling
  (a derived `file://` URI passes through unchanged).
- The RDR-231 collection-move rewrite of a note URI and its collision
  refusal. It is a no-op for `file://` URIs; the commit keeps it so that
  it matches `updateDocumentsMany` for every row shape.
- Sensitivity on created rows: inheritance from a tombstoned predecessor,
  and one audit row per row (`register_inherit` or `register_set`).

*Sensitivity on existing rows.* The upsert never flags an existing
document. The outcome lists, as `sensitivity_pending`, every target whose
landing row asks for `sensitive` and whose stored row lacks it, and the
client raises them through the audited `/sensitivity` route (RDR-243).
That list includes `same_owner_by_uri` and `foreign_owner` targets. Today
`register_many` raises those itself inside the engine, audited as
`register_raise` (CatalogRepository.java:1606); after this change the audit
reason is the client's. That is a declared change of audit reason only.

*Housekeeping, only when the walk is `complete`* (research-26). The present
set P is the `file_path` of every `present` and `keep` row.

- The target map is read after the upsert, so a row created in this run
  counts. For each content hash, the target is the owner's live row in P
  with that `metadata->>'content_hash'`; when several share it, the one
  `documentsByOwner` lists last.
- Every owner row not in P (a `deleted` row included) whose content hash is
  in the map is renamed. Otherwise, if its `miss_count + 1` reaches
  `_ORPHAN_DELETE_MISSES` (2), it is swept this run.
- Every row in P with `miss_count > 0` gets `miss_count = 0` (a
  metadata-only merge: no `chunk_count`, no `indexed_at`).
- A rename, for each missing row with a match: carry `non_evidentiary` to
  the target when the old row has it and the target lacks it; move each
  link from or to the old row, with both endpoints mapped through this
  run's rename map; skip a link that becomes a self-link, touches a swept
  row, or whose other endpoint is not live; insert only when `(from, to,
  link_type)` is absent, copying `created_by`, `from_span` and `to_span`
  but not the link's metadata; then trash the old row. Several missing rows
  may rename onto one target, and a rename does not wait for a second miss.
- Every other missing row gets `miss_count + 1`, or is trashed with
  `nexus.document_trash` at 2.
- Links move before the trash, so the `catalog-032` cascade, which fires
  only at purge, never sees them.

This replaces the August rule (lowest-tumbler symmetric tie-break, each
missing row consumed once). That rule changed today's outcome and lost
renames onto files registered in an earlier run.

*Partial walk* (every delta run): no miss-count branch at all. This is the
nexus-fltb4 gate moved into SQL; a test pins that a partial commit never
touches an unvisited row.

*Explicit deletions* (`deleted` rows). Each is matched by `(owner,
file_path)` only. When the owner holds no live row for the path and another
owner does, the row is reported `deleted_skipped_foreign_owner` and that
row is left live, as `_delete_docs_for_paths` does today (indexer.py:1075-1110).
In a complete walk a `deleted` row first goes through housekeeping as a
missing row, as today, where the hook's housekeeping runs before the
deletion; a row still live afterwards is trashed. In a partial walk it is
trashed with no link move, which is today's delta behaviour.

The timing moves (declared change): today deletions are applied in the
prune phase after indexing (indexer.py:7907-7936); the commit applies them
before indexing starts. A run that dies after the commit has already
retired them. Each is a file git reported gone or a path the walk now
excludes, so nothing that should stay is lost.

*Not in the commit: the owner row's `head_hash`.* It is the base of the
next `--since-head` delta and is written at run end only when no file was
deferred or failed (research-30). The August draft folded it into the
commit; that would have moved the base before indexing, so a failed run's
files would drop out of the next delta. It stays where it is.

*Finish*: delete the run's rows, set the header to `committed`, store the
outcome on it.

**3. `nexus.catalog_reconcile_reap(p_interval)`** drops headers and rows of
runs created longer ago than the interval (default 24 h), committed or not.
It runs at the start of every stage call for the tenant, so no scheduler
is needed.

Routes (`CatalogHandler` and `CatalogRepository`):

| Route | Body | Returns |
| --- | --- | --- |
| `POST /v1/catalog/reconcile/stage` | `{owner, run_id, walk, expected_rows, rows[≤1000]}` (`walk` and `expected_rows` must match the header on later pages) | `{staged}`; 400 naming a duplicate `file_path` |
| `POST /v1/catalog/reconcile/commit` | `{owner, run_id}` | `202 {job_id}`; the same `{owner, run_id}` while its commit runs gets the running job; `409 {job_id}` when another run of the owner is committing; `503` when the registry is full |
| `GET /v1/catalog/reconcile/{job_id}` | none | `{status: running}`, or `{status: succeeded, outcome}` (below), or `{status: failed, error}`; 404 for an unknown id |
| `POST /v1/catalog/doc/resolve_or_register` | `{owner_name or curator, source_uri or file_path, title, year?, title_fallback?}` | `{tumbler, created}` or `{tumbler, foreign_owner: true}`; title and year backfilled with `COALESCE` on empty; ghost-only title match when `title_fallback` |

The outcome carries:

- `rows`: one per staged row, `{file_path, tumbler, action,
  content_hash_changed, sensitive, index_state, index_content_hash}`, with
  `action` one of `created`, `updated`, `unchanged`, `keep`,
  `same_owner_by_uri`, `foreign_owner` (with the other owner), `refused`
  (with a reason) or `deleted`.
- `trashed`, `renamed: [{old, new}]`, `swept`, `duplicate_path`,
  `deleted_skipped_foreign_owner`, `sensitivity_pending`.

`index_state` and `index_content_hash` are read before the update, which
does not change them. The stage cap mirrors
`CatalogHandler.MAX_BATCH_DOC_IDS = 1000`; a 20,000-file repository is 20
stage calls, one commit, and polls.

#### Part A2: the client switch

- `catalog/http_catalog_client.py`: `reconcile_stage`, `reconcile_commit`
  (submit and poll under the shared job contract), `links_upsert_many`,
  `links_same_content`, `resolve_or_register`, added to
  `_SERVICE_ONLY_WRITE_OPS` (`catalog/factory.py:417`).
- `indexer.py` `_catalog_hook` keeps the walk, `stat`, sha256, the
  ephemeral-path guard, the nexus-rqsh1 guard and classification, and
  builds `base_metadata` and `stamp` for each file. It replaces the
  `by_owner` join, the Python diff, `register_many`, `update_many` and the
  per-file fallback with stage, commit and poll. `walk="partial"` is passed
  exactly where `skip_housekeeping` is set today.
- The hook rebuilds what its callers read from the outcome's `rows`, with
  no extra request:
  - `file_to_doc_id`: every row with a tumbler.
  - `new_tumblers` and `new_content_types`: `created` rows.
  - `relink_rdr_tumblers`: `rdr` rows with `content_hash_changed`.
  - `needs_fence`: `created` rows, and `updated` rows with
    `content_hash_changed`, each mapped to (content hash, collection).
  - `stale_fence_doc_ids`: `updated` and `unchanged` rows whose
    `index_state` is `indexing` or `failed`.
  - `complete_doc_hashes`: `updated` and `unchanged` rows whose
    `index_state` is `complete` with a non-empty `index_content_hash`.
  - `sensitive_confirmed`: rows with `sensitive` true; the client raises
    `sensitivity_pending` through `/sensitivity` and confirms those that
    succeed, as today.
  - `reconciled`: `same_owner_by_uri` and `foreign_owner` rows, logged as
    today.
  - `unregistered`: `refused` rows, with the reason.
- Deleted: `_run_housekeeping`, `_maybe_run_housekeeping` and the
  `_delete_docs_for_paths` call; `delta_deleted` (complete before the hook,
  indexer.py:5646-6001) is staged as `deleted` rows.
- Unchanged: the three owner reads that run before the hook
  (`_stored_paths_among`, `_warn_symlink_ghosts_with_dependents`,
  `_pdfs_needing_index`) and `_reconcile_needs_fence`, which needs the fence
  state at run exit, not at commit time. `_stored_paths_among` also keeps
  `delta_deleted` empty on quiet delta runs, which spares the prune phase
  (indexer.py:7907). `--frecency-only` keeps `_build_frecency_doc_id_map`.
  So a run makes one owner read fewer in the hook and none in housekeeping;
  it does not reach a single owner read, and this RDR does not claim it.
- `doc_indexer.py` `_register_or_lookup_doc_id` and `_catalog_markdown_hook`
  collapse to one `resolve_or_register` carrying title, year and
  `source_mtime` (`chunk_count` is already folded into `write_manifest_many`
  through `EX_DOC_CHUNKS`); `store_hook.py` the same.
- No fallback to the old path.

#### Part A4: index-time linking (Gap 4, changeset `catalog-048-reconcile.xml`)

1. **`nexus.catalog_links_upsert_many(p_edges jsonb) RETURNS TABLE(created
   int, skipped int, unresolved jsonb)`**, one transaction. Each edge is
   `{from, to, link_type, created_by, from_span?, to_span?}`, and each
   endpoint is `{tumbler}` or `{owner, file_path}`. There is no metadata
   and no `allow_dangling`, because no index-path caller passes either
   (research-27, research-33).
   - A path endpoint resolves first by identity: the live row whose
     `source_uri` equals the URI derived from the named owner's `repo_root`
     and the path, whichever owner registered it. That finds the same file
     under a stale owner of the same repository, and can never find another
     repository's file, whose URI has another root. Failing that, it
     resolves to the named owner's live row with that `file_path` (several:
     the one `documentsByOwner` lists last). For the file-path linkers the
     named owner is the source document's owner. No match is returned in
     `unresolved` as `no_path`.
   - A tumbler endpoint follows `alias_of` to its canonical, and both rows
     are locked in sorted order, the `resolveAndLockLinkEndpoints` contract.
     An endpoint that is not live after resolution is returned in
     `unresolved` as `dangling` and not written.
   - The insert is `ON CONFLICT DO NOTHING` on `(tenant_id, from_tumbler,
     to_tumbler, link_type)` of the resolved pair, so an existing link
     keeps its fields. Self-links are not refused; `upsertLink` accepts
     them too.
   - One difference from today (declared): `link_if_absent` checks the pair
     it was given, then `/link` upserts the alias-resolved pair, so an
     alias endpoint whose canonical edge exists gets that edge merged
     (spans overwritten, creator folded into `co_discovered_by`). The batch
     resolves first and skips it.
2. **List-route filter:**
   `GET /v1/catalog/list?owner=<prefix>&content_type=<t>[&content_type=<t2>]`
   returns one owner's documents of the named types, and
   `source_uri_prefix=` returns the documents whose URI starts with a
   prefix, so `handleList` gains one combined branch. `rdr_resolution`
   makes two reads: the current owner's `rdr` and `prose` rows, and the
   rows under its `rdr_source_prefix` (the second admission arm of
   `is_in_repo`, which covers the about 150 RDRs registered under a stale
   owner, rdr_canonical.py:170-180). The first read returns every markdown
   document of one repository, not only RDRs; it is bounded by one
   repository, not by the tenant. Because `rdr_resolution` itself changes,
   `nx rdr set-status` gets the same two reads.
3. **`nexus.catalog_link_same_content(p_tumblers text[], p_prune boolean)
   RETURNS jsonb`**, the rule `generate_pdf_corpus_links` and
   `relink_changed_pdfs` apply today (research-33), in one transaction:
   - Groups are live `pdf` and `paper` documents across the tenant, keyed
     by `index_content_hash`, else `metadata->>'content_hash'`, never
     `head_hash` (nexus-utkop). Cross-collection and cross-owner grouping is
     the point of this linker (nexus-sob9), so it stays tenant-wide, inside
     the engine.
   - The anchor is the member with the least `tumbler COLLATE "C"`, which is
     Python's `min(str(tumbler))` (link_generator.py:889). A numeric order
     would re-anchor groups such as `1.2.9` and `1.2.10` and orphan their
     existing links, so the anchor rule does not change.
   - Each given member links `same-as` to its anchor with `created_by
     content_hash_dedup`; when the anchor itself is given, every member of
     its group links to it (nexus-h0uy7). Insert only when absent.
   - With `p_prune`, first retract stale dedup links that touch the given
     tumblers, by `prune_stale_same_as_links`'s rule: a link is stale when
     an endpoint is no longer a live `pdf` or `paper` document, or both
     endpoints have a settled key and the keys differ (a key is unsettled
     when `index_content_hash` and the metadata hash are both set and
     disagree); a link with `co_discovered_by` is kept. The members of
     retracted links are then linked again. The retraction uses whatever
     `unlink` does today (inferred, not read: whether that is a soft or a
     hard delete is checked at Step 3). Today's refusal on an empty fetch
     (link_generator.py:972-980) becomes: refuse when candidate links exist
     and no live `pdf` or `paper` document does.
   - Routes: `POST /v1/catalog/links/upsert_many {edges}` (at most 1000
     edges per call) and `POST /v1/catalog/links/same_content {tumblers,
     prune}` returning `{created, retracted}`.
4. **Client:**
   - The file-path linkers keep their regex scans of new files and send
     path edges through one `links_upsert_many` call per generator (paged
     at 1000 edges).
   - The RDR-dependency linker reads its scoped candidates (item 2), keeps
     `rdr_canonical.resolve_all` and the frontmatter reads on the client
     (they need the file), and sends tumbler edges through one
     `links_upsert_many`.
   - The PDF linker on the index path becomes `same_content(prune=false)`,
     and `_relink_changed_pdfs` becomes `same_content(changed, prune=true)`.
   - `nx catalog generate-links` (a maintenance verb, not the index path)
     sends its path edges through `links_upsert_many` too, so the
     tenant-wide `path_to_code` dict is deleted everywhere. Its full pass
     still lists source documents across the tenant, and so does
     `generate_citation_links`; `_entries_of_type` keeps those callers and
     loses every index-path caller.
   - RDR-241's HOT PATH row goes when the index path has no tenant-wide
     listing left (Step 7). The full-pass listings stay listed, as BULK JOB.

Resolving a path by the file's identity is a declared behaviour change. An
RDR in one repository that names a path existing only in another
repository no longer gets an `implements` link to it. Today that link is
made only when the last listed document for the path happens to be the
other repository's, which is the defect. A link to the same repository's
file under a stale owner is kept by the identity arm; one whose target row
has an empty `source_uri` under a stale owner is dropped. Step 0 counts
both kinds today. (Open question 2 asks whether cross-repository path links
should come back behind an opt-in.)

#### Part B1: taxonomy SQL (changeset `taxonomy-022-serverside-projection-links.xml`)

- `nexus.taxonomy_project_collection_<dim>(p_src_collection, p_threshold)`:
  the whole-collection form of the `assign_from_chashes` cross pass, built
  on the `taxonomy-018` per-chunk LATERAL over HNSW with the `taxonomy-020`
  plan pin; argmax over foreign centroids at or above the projection
  threshold, upsert `topic_assignments` (`assigned_by='projection'`,
  `GREATEST` semantics verbatim), no egress. Replaces `project_against` and
  `_svc_fetch_all_embeddings`.
  - Sensitivity posture: the function reads sensitive chunks, as
    `project_against` does today (`sensitivity="include"`,
    http_taxonomy_store.py:1860, 2147). A projection writes an assignment
    row and no text, and no label is computed from it; RDR-243 does not
    speak to projection (research-34). Parity is the choice here, stated
    so the gate can disagree.
- `nexus.taxonomy_cross_links(p_collection, p_threshold)`: centroid against
  foreign centroid cosine through `<=>`, `INSERT topic_links ON CONFLICT`,
  returns a count. Replaces `compute_cross_links` and the per-pair
  `persist_cross_links`. Its inputs are centroids, which discover computes
  from chunks read with sensitive owners excluded.
- Labeler routes: `POST /v1/taxonomy/topics/label_context_many {topic_ids,
  samples_per_topic}` (the engine picks sample chunk texts through the
  manifest, excluding chunks with a live sensitive owner by the
  `vectors-055` predicate, and reports which topics hold a sensitive
  document so the client keeps skipping them) and `POST
  /v1/taxonomy/topics/update_label_many` (one transaction; resolves the
  nexus-ejo6k duplicate-label 409 inside the batch). The `claude -p`
  relabel stays on the client.

#### Part B2: the discover job (Java)

- `POST /v1/taxonomy/discover {collections: [...], force}` answers `202
  {job_id}`; the same request while it runs gets the running job; a
  different request while a discover for the tenant runs gets `409`;
  `GET /v1/taxonomy/discover/{job_id}` polls; 404 for an unknown id. Shared
  job contract above.
- One job per submit; collections processed in order, each in its own
  transaction; the poll envelope carries `[{collection, status: pending |
  running | succeeded | skipped_existing | failed, topics, error}]`, so an
  engine death mid-job leaves a truthful record of the collections that
  committed. The existing-topics guard runs inside each collection's
  transaction: a collection that already has topics is `skipped_existing`
  unless `force` is set, which routes to the rebuild path. This guard is
  also what makes a resubmit after a 404 safe.
- Job body (`taxonomy/TaxonomyDiscoverService`): per collection, stream
  `chash` and the dimension's embedding from `nexus.chunks` over JDBC
  (host-local, never materialised as JSON), excluding chunks with a live
  sensitive owner (the `vectors-055` predicate), cluster in Java, then in
  one transaction insert `topics` and `topic_assignments`
  (`assigned_by='hdbscan' | 'kmeans'`), upsert `taxonomy_centroids`, record
  `taxonomy_meta` and the discover count, and run `taxonomy_cross_links`
  (B1). Rebuild's `_merge_labels` (old-to-new centroid cosine, greedy claim
  at 0.8, carrying operator labels and `accepted`) becomes a SQL step over
  the two centroid sets. Split is k=2 over one topic's chunks through the
  same code.
- Memory: the job holds one collection's vectors as `float[]`, n × d × 4
  bytes: about 410 MB for 100,000 chunks of 1024 dimensions, plus k-means
  centroids. HDBSCAN at n ≤ 5000 holds about 20 MB of vectors and computes
  distances in blocks. A collection whose vector bytes exceed a stated
  share of the engine's maximum heap fails as `too_large` and the job goes
  on to the next. The native image's heap ceiling is set at launch and is
  not read here (inferred). Step 11 measures peak memory and wall time for
  a 100,000 × 1024 discover.
- Clustering: a Java port of HDBSCAN for n ≤ 5000 with today's parameters
  (mutual reachability, Prim MST in O(n²), single linkage, condensed tree,
  eom selection with the nexus-9b9oi `max_cluster_size` cap, centroid
  centres) and k-means++ / MiniBatch above 5000 (`k = max(10, √n/3)`, seed
  42), with a small `taxonomy/VectorMath`. About 1.3-1.5k lines of Java
  before tests (estimated in pass 1; revisited at Phase 3 planning).
- c-TF-IDF in SQL over each cluster's `chunk_tsv`: class-based term
  frequency as one `GROUP BY (cluster, lexeme)` with cross-cluster document
  frequency, top 3 per cluster, ranked by stem and displayed as the most
  common surface form (`ts_lexize('english_stem', word)`). No Java
  tokenizer.
- Client: `discover_for_collection` becomes submit, poll, summary.
  Post-index auto-discover submits and reports, logging a `failed`
  collection and continuing; `nx taxonomy discover` waits. Deleted:
  `_fetch_service_vectors`, `_discover_via_service`,
  `compute_discovered_topics`, `compute_rebuild_plan`, `compute_split`,
  `_cluster`, `_merge_labels` (the projection and cross-link methods went
  with Part B1). No fallback.

#### Resolved design questions (pinned 2026-08-15, updated 2026-11-08)

1. Tumbler minting moves into plpgsql (research-1). Minting then exists
   twice, in Java `claimNextSeq` for the routes kept for other callers and
   in the commit. A drift test seeds owners with a drifted `next_seq` and
   a tombstoned highest child and asserts both mint the same next tumbler.
2. Rename precedence reproduces today's outcome exactly (research-26). The
   August symmetric rule is withdrawn: parity is the bar, and changing
   precedence would be a separate decision with its own migration story.
3. `resolve_or_register` is keyed on `(owner, source_uri)` with a
   ghost-only title fallback; a match under another owner is returned as
   `foreign_owner` and not adopted; the ephemeral-worktree guard stays on
   the client (research-3, research-28).
4. c-TF-IDF in SQL, spike-verified (research-6, research-12).
5. HDBSCAN parity gate: the fixture above, ARI ≥ 0.90 against sklearn
   labels; `_merge_labels` and `compute_split` reproduced exactly on seeded
   fixtures; noise chunks get no assignment row (research-5, research-13).
6. One discover job per submit, each collection its own transaction,
   per-collection result array (research-8, updated for the 404 contract).
7. The client retires `assign_batch`, `compute_assignments`,
   `project_against`, `_svc_fetch_all_embeddings`, `compute_cross_links`,
   `persist_cross_links` and its use of the `centroids/foreign` route; the
   engine route stays until a later retirement RDR (research-7).
8. `taxonomy/VectorMath` is new; nothing exists to reuse (research-10).
9. The landing tables are self-reaping at every stage call (research-4,
   moved to schema `nexus` by research-15).
10. The ap8l0 plan problem: closed by `taxonomy-018` and `taxonomy-020`
    (research-17). B1 builds on that plan rather than measuring it again.
11. The PDF same-as anchor stays the text-least tumbler (research-33).

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `EngineJobs` registry | `http/ChunkKeysSweepJobs.java` | Refactor: a parameterised registry; chunk-keys keeps its kind, cancel and starter check in a wrapper |
| Reconcile commit | `CatalogRepository.registerDocumentManyWithOutcome`, `updateDocumentsMany`, `claimNextSeq` | Replace for the index path, carrying each invariant in Part A1 with a test; keep the routes for other callers |
| Landing tables | `staging` schema (dropped) | Two new tables in schema `nexus` (a run header and catalog rows; never chunks) |
| `links_upsert_many` | `CatalogRepository.upsertLink`, `resolveAndLockLinkEndpoints`, `/link` | Reuse the conflict key and the endpoint resolution in a batch function that inserts only when absent (the route itself upserts) |
| Owner plus type list filter | `CatalogHandler.handleList` | Extend with a combined branch |
| Same-as grouping and prune | `link_generator.generate_pdf_corpus_links`, `prune_stale_same_as_links` | Move the rules into one SQL function |
| Projection, cross-links | `assign_from_chashes_<dim>` (taxonomy-018/020 body) | Extend to whole-collection form |
| Sensitivity exclusion in the job | `vectors-055` predicate, `ChunkSensitivity` | Reuse |
| Discover clustering | `db/t2/taxonomy_compute.py` (sklearn) | Replace with Java port |

### Decision Rationale

The residual cost is round-trip count and download size, and both go to a
constant only when the logic moves to where the rows are; another batch
route keeps the client walking. The same move fixes a correctness defect
the client shape carries: tenant-wide path resolution (Gap 4). SQL keeps
each state change in one transaction under the RLS the engine already
enforces; the Java job exists only because clustering has no SQL form.

## Alternatives Considered

### Alternative 1: Keep batching the client path

**Description**: Another `_many` route, larger pages, a memoised owner
list, a batch link route with client-side resolution.

**Pros**:

- No engine cut for the catalog half; smaller changes.

**Cons**:

- Leaves the owner reads, the tenant-wide type lists and the client
  diff; each step buys a constant factor, as July and August 2026 did.
- Leaves the tenant-wide path resolution defect.

**Reason for rejection**: the cost grows with the tenant, and the
correctness defect is a property of where the logic runs.

### Alternative 2: Synchronous commit and discover routes

**Description**: Plain request and response for commit and discover.

**Pros**:

- Simpler client.

**Cons**:

- The edge's origin timeout (about 30 s) would cut long requests while the
  transaction commits anyway.

**Reason for rejection**: rejected at gate run 1; jobs are the established
answer (`ChunkKeysSweepJobs`).

### Briefly Rejected

- **k-means-only server clustering**: changes small-collection topic shape
  and forces a re-discover on upgrade (Hal).
- **An engine-hosted Python worker for clustering**: puts Python on a host
  whose engine is a native binary by design (RDR-157, RDR-161) (Hal).
- **Client clustering over binary embeddings**: cuts egress about three
  times and keeps the round trips and the `--all` double download.
- **Persisted job rows (`ReembedJobRepository` shape)**: nothing to recover
  that an idempotent resubmit does not.

## Trade-offs

### Consequences

- Positive: a warm full run's catalog phase goes from two owner reads in
  the hook and housekeeping, O(changed) pages and O(missing) serial writes
  to stage calls, one commit and polls. A delta run loses its owner read in
  the hook and its per-file writes. Link generation goes from tenant-wide
  type lists and two round trips per edge to one write per generator and
  one scoped read. Whether that removes the 4-6 s and 1-5 s parts of the
  baseline is measured at Step 7, not assumed, because the baseline does
  not split list time from probes and frontmatter reads. `nx index md` and
  `store put` identity resolution becomes one request. Discover's egress
  goes to near zero and its round trips to a submit and polls (the relabel
  still pulls a few sample texts per topic, by design). The client sheds
  the reconcile machine, sklearn from the discover path, and the
  cross-process centroid-cache staleness noted at
  `http_centroid_store.py:97-133`.
- Negative: two engine cuts; a Java HDBSCAN port with a parity obligation;
  two landing tables and a job registry as operational surfaces; tumbler
  minting in two places, held together by a drift test.
- Behaviour changes, declared:
  - Path links resolve by the file's identity, so cross-repository links
    and links to stale-owner rows with no `source_uri` are dropped.
  - The commit is all or nothing apart from classified refusals; a run
    whose commit fails exits non-zero instead of indexing unregistered.
  - Deletions are applied at commit time, before indexing, not in the
    prune phase.
  - Sensitivity raised on a reconciled row is audited with the client's
    reason, not `register_raise`.
  - An alias endpoint whose canonical edge exists is skipped, not merged.
  - Order of link writes: today the generators run before housekeeping
    (indexer.py:2165-2275), so when a generator and a rename both produce
    one edge, the generator's row survives; after this change the rename's
    move runs first, and the surviving row carries the old link's
    `created_by` and spans.
- Migration: none for data; the functions work on existing tables. The old
  engine routes stay served for other callers until a later retirement RDR.
  A new client against an old engine fails loud on the new routes; an old
  client against the new engine keeps working on the retained routes.

### Risks and Mitigations

- **Risk**: the SQL change predicate differs from Python and rewrites
  unchanged rows (the "always-true drift predicate restores the write
  storm" finding from nexus-dst5h).
  **Mitigation**: the Step 2 parity fixtures, old path against commit on
  one fixture, identical row state and unchanged `indexed_at`.
- **Risk**: a partial walk reaches the miss-count branch (mass delete).
  **Mitigation**: the walk mode is a required field of the run header; a
  test commits a partial run over an owner with unvisited rows and asserts
  none changed.
- **Risk**: a metadata rule from RDR-236 or RDR-243, or an engine
  invariant of the replaced methods, is lost in the move.
  **Mitigation**: one test per rule and per invariant listed in Part A1.
- **Risk**: one bad row now fails a whole run's registration.
  **Mitigation**: the predictable row errors are classified refusals; Step
  2 tests that a refused row leaves the rest committed.
- **Risk**: the Java port diverges from sklearn.
  **Mitigation**: the parity fixture gate at Step 10 blocks Phase 3.

### Failure Modes

- The engine restarts mid-job: the poll gets 404, the client logs
  `engine_job_lost` and resubmits once; reconcile re-commits the same
  `run_id` (a committed run returns its stored outcome), discover skips
  committed collections.
- A commit fails: the envelope says `failed` with the error; the index run
  exits non-zero and says so, before any chunk is written, and the owner's
  `head_hash` does not move, so the next delta still holds the files.
  Landing rows remain and are reaped after 24 h.
- A row is refused: it is named in the outcome with its reason and recorded
  as unregistered; the rest of the run proceeds.
- A stage page is lost: the commit fails `incomplete_stage` before any
  write; the run is re-staged under a new `run_id`.
- The registry is full: `503`, three bounded resubmits, then a loud
  failure.
- An unresolved link path: returned in `unresolved` and logged with the
  source document; no edge.
- Diagnosis: `catalog_hook_stage_timing` keeps its stage names (stage,
  commit, linking per generator), so a slow phase is still attributable.

## Implementation Plan

### Prerequisites

- [x] Sam's answers to the open questions (Finalization Gate), 2026-11-08:
  landing tables; cross-repository path links dropped unless Step 0 shows
  reliance; Parts A and A4 accepted, Part B held.
- Part B is held at accept (open question 3). Steps 4, 8 and 10-12 are
  outside this acceptance and wait for a later trigger.
- [x] Changeset ids checked free: `catalog-048`, `taxonomy-022`.

### Minimum Viable Validation

A warm full `nx index repo` of this repository against a local engine built
from the Phase 1 tree makes exactly one commit request, `ceil(N/1000)` stage
requests, zero single `update`, `register`, `delete` or `link` requests, and
zero `GET /list?content_type=` requests without an owner filter (counted at
the HTTP client), and leaves catalog row and link state identical to the old
path on the same tree. Then a delta run with one changed RDR does the same
with `walk=partial` and the linking stage costs at most one scoped read and
one write per generator.

### Phase 1: Engine half of Parts A, A4 and B1 (engine cut 1)

#### Step 0: Measure the live catalog (read-only, before Step 2's tests)

Three counts decide details of the identity and path-resolution rules, and
none is in hand. They are taken once against the managed tenant, read-only,
after the run is announced to conexus on the bus with its expected load.

1. Live documents with an empty `source_uri`, by owner, and how many of
   them share a `file_path` with another live row of the same owner.
2. Live documents under one owner whose `source_uri` lies under another
   owner's `repo_root` (the nexus-53cae class, the rows a commit would
   report as `foreign_owner`), by owner pair.
3. Live `implements` links made by the file-path linkers whose endpoints
   sit under different owners, split into same-repository (both URIs under
   one `repo_root`) and cross-repository, and the number whose target has
   an empty `source_uri`.

Bounds: aggregate counts only, no document text and no row export, through
`run_diagnostic_sql` (`src/nexus/db/diag_connection.py:130`: an
aggregate-only lint and a read-only session, as the `nexus_diag` role),
each statement under a 30 s statement timeout, one pass. The counts go to
T2 `nexus/rdr193-identity-measurement` and to a research finding. Use:
count 1 says whether `duplicate_path` fires on real data (zero keeps it as
a tested refusal that does not fire); count 2 sizes the `foreign_owner`
report; count 3 sets open question 2's default and says how many
same-repository links the identity arm keeps.

#### Step 1: Job registry

Refactor `ChunkKeysSweepJobs` into `EngineJobs` plus a chunk-keys wrapper,
add the reconcile instance with its bounds. Tests: the existing
`ChunkKeysSweepJobs` tests unchanged against the wrapper; running,
succeeded, failed; identical start handed the running job; different start
409; 404; 503; retention; `evictTenant`; two owners of one tenant running
at once.

#### Step 2: Reconcile changeset and routes

`catalog-048-reconcile.xml` (landing tables, commit, reap), the three
reconcile routes and `resolve_or_register`. Java tests on testcontainers PG.

Parity fixtures (mandatory): the old path (`register_many`, `update_many`,
`_run_housekeeping`, `_delete_docs_for_paths`) and the commit run on one
seeded fixture, and every column of every catalog row and every link must
match. The fixture holds at least:

- Rename: a single rename; a rename onto a file registered in an earlier
  run; two missing rows onto one target; present rows sharing a hash with
  tumblers `1.2.9` and `1.2.10`; a link between two renamed rows; a link
  that becomes a self-link; a link to a swept orphan; a link to a
  tombstoned partner; a `non_evidentiary` carry; a rename that is also a
  `deleted` row on a complete walk.
- Stamp: an RDR id change and a null id; a `source_format` change; `tags`
  unset (absent, null, `""`, `[]`) against set; an empty file hash.
- Content hash: a HEAD bump alone (`updated`, `content_hash_changed`
  false); a content change (true); an edited RDR in the relink list; a new
  file and a changed file in `needs_fence`.
- Identity: a `foreign_owner` path; a `same_owner_by_uri` path with a stale
  collection; a legacy row with an empty `source_uri`; a `duplicate_path`;
  a `deleted` path held only by another owner.

Invariant tests, one each: alias target, `chunk_count` re-derive and its
metadata-only exception, `indexed_at` on a `head_hash` change only,
`NoteUri` spelling, `ensureOwnerRow`, containment refusal, sensitivity
inheritance and audit, two new rows sharing one tumbler, a lost insert
race, the minting drift test.

Behaviour tests: miss, second miss, trash; a partial walk touches no
unvisited row and trashes `deleted` rows; `keep` rows count as present and
are never written; `sensitive` only on created rows, with
`sensitivity_pending` returned; a refused row with the rest committed; RLS
isolation; landing rows cleared; a re-commit returns the stored outcome and
writes nothing; a short stage fails `incomplete_stage` with no write; a
duplicate `file_path` refused at stage; the owner `head_hash` untouched by
the commit; the 20,000-row commit under 30 s (accept bar).

#### Step 3: Linking functions and filter

`catalog_links_upsert_many`, `catalog_link_same_content`, the combined
owner and content-type list branch with `source_uri_prefix`. First read
what `unlink` does today (soft or hard delete) and match it. Tests: identity
resolution finds a stale owner's row of the same file; a path present only
in another repository returns `unresolved`; an alias endpoint resolves to
its canonical; a dangling endpoint is `unresolved`; an existing link keeps
its fields; `same-as` grouping and the anchor match today on a fixture with
`1.2.9` and `1.2.10`; a seeded anchor gets every member; prune keeps a
`co_discovered_by` link and an unsettled key; RLS.

#### Step 4: Taxonomy SQL and labeler routes

Held at accept (Part B, open question 3).

`taxonomy-022-serverside-projection-links.xml` and the two labeler routes.
Tests: projection equals today's `project_against` result on a seeded
fixture, sensitive chunks included as today; cross-links count and rows;
`label_context_many` returns no sensitive chunk and names topics holding a
sensitive document; a duplicate label inside one batch resolves without a
409.

#### Step 5: Wire ledger and engine cut 1

Record the routes in `docs/wire-contract-pending.md` as `[additive]`. Cut
the engine through the `engine-release` skill and its current gates: Step 2
(engine suite and native build green on the exact commit), 3g (no held
changeset), 4 (the tag, Sam's), 5 (`--acquire` against the published
bytes), 5b (the migration-release branch, since the cut carries
`catalog-048`: a `--package-upgrade` rehearsal at a stated corpus floor, a
rollback decision, and post-deploy row-count checks), 6 (the relay to
conexus, whose Liquibase walk on a point-in-time fork of production is the
pre-deploy gate) and 6.1 (the client-path gate through the edge). The
`--candidate-migration` leg the August draft named no longer exists.

### Phase 2: Client half of Parts A, A4 and B1

#### Step 6: Reconcile client

The A2 switch and deletions. Contract tests in the nexus-dst5h style:
commit exactly once per run, stage `ceil(N/1000)`, zero single writes in a
warm run; the job contract (409 waits, 404 resubmits once, 503 bounded,
failed aborts with no chunk written); each dict the hook fills for its
caller rebuilt from the outcome; a service-mode test through
`ensure_engine`. Measurement for Gap 3 (no code change in this RDR): after
a full run of this repository, count the documents created in that run
that have `chunk_count 0` and no manifest, and record the count in a bead
for the reorder decision (filed then, if none exists).

#### Step 7: Linking client

The A4 client changes and deletions. Probe: re-run the baseline's delta
shape (a change to one RDR, one to a few code files) and compare linking
and `all_documents` time with the baseline, split into list, probe and
frontmatter time. If RDR-241's ratchet table exists by then, remove its
HOT PATH row and list the full-pass `generate-links` sites as BULK JOB; if
it does not, record the same change in RDR-241's census entry (T2
`nexus/rdr-241-walk-census`).

#### Step 8: Taxonomy client for B1

Held at accept (Part B, open question 3).

Projection, cross-links and relabel switched to the Step 4 routes, with
the client deletions listed under Part B1. Test: relabel still skips topics
with a sensitive document.

#### Step 9: Floor bump and release

`REQUIRED_ENGINE_VERSION` raised to engine cut 1 in the client release that
ships Steps 6-8 (paired release).

### Phase 3: Part B2 engine (engine cut 2)

#### Step 10: Clustering port

Held at accept (Part B, open question 3).

Java HDBSCAN, k-means and `VectorMath`. Gate: the parity fixture (ARI ≥
0.90, capped 16 clusters with the largest 40, uncapped [359, 23]);
`_merge_labels` and `compute_split` reproduced on seeded fixtures. The
phase stops here if the gate fails.

#### Step 11: Discover service, routes and engine cut 2

Held at accept (Part B, open question 3).

`TaxonomyDiscoverService`, the `discoverJobs` instance and routes, then
engine cut 2 with the same gates as Step 5. Tests: seeded chunks give
topics, assignments, centroids and `taxonomy_meta` in one transaction per
collection;
sensitive chunks never reach clustering or labels; `skipped_existing`
without `force`; resubmit after 404 skips committed collections; a
`too_large` collection fails alone; RLS. Probe: peak memory and wall time
of a 100,000 × 1024 discover on the engine, recorded.

### Phase 4: Part B2 client

#### Step 12: Discover client

Held at accept (Part B, open question 3).

Submit, poll, summary, deletions. Contract test: `nx taxonomy discover`
against `ensure_engine` makes zero `/vectors/get`, `/get-embeddings` and
`/centroids/foreign` calls. Probe: discover wall time on one collection,
engine job against today's client path, recorded. Floor bump to engine cut
2 in the release that ships it.

Each phase: engine and client halves reviewed by `code-review-expert` and
`substantive-critic`, and `/conexus:phase-review-gate` at the boundary.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `nexus.catalog_reconcile_runs`, `nexus.catalog_reconcile_rows` | N/A (transient) | N/A | Self-reaping at every stage call | Reaper test | N/A (transient) |
| `EngineJobs` instances | Poll by id | Poll by id | Retention expiry; `evictTenant` | Registry tests | N/A (memory only) |

### New Dependencies

None. The clustering port is new Java code with no new library.

## Test Plan

- **Scenario**: warm full run, nothing changed. **Verify**: one commit, no
  row rewritten, `indexed_at` unchanged.
- **Scenario**: warm full run after a commit that touched no file (HEAD
  bump only). **Verify**: every row `updated` with `content_hash_changed`
  false, `needs_fence` empty, no RDR relinked.
- **Scenario**: delta run, one file deleted, others unvisited. **Verify**:
  the deleted file's document trashed, no unvisited row touched.
- **Scenario**: a delta run whose files are deferred. **Verify**: the owner
  `head_hash` does not move, and the next delta holds the files.
- **Scenario**: the rename and stamp cases of the Step 2 parity fixture,
  end to end through `nx index repo`. **Verify**: row and link state equal
  to the old path.
- **Scenario**: a path whose `source_uri` is another owner's live row.
  **Verify**: `foreign_owner` reported, that row unchanged, the file's
  chunks carry its tumbler as today.
- **Scenario**: one staged row fails the containment guard. **Verify**: it
  is `refused` and unregistered; every other row committed.
- **Scenario**: re-index of a document whose `tags` were set by hand.
  **Verify**: `tags` kept, `source_format` re-stamped.
- **Scenario**: `--sensitive-glob` matches an existing unflagged document.
  **Verify**: commit does not flag it; the client raises it through
  `/sensitivity`.
- **Scenario**: an RDR names a path that exists in this repository and in
  another. **Verify**: the link targets this repository's document.
- **Scenario**: two PDFs with the same content in different collections.
  **Verify**: one `same-as` link to the text-least tumbler.
- **Scenario**: the engine restarts during a commit and during a discover.
  **Verify**: 404, one resubmit, correct final state.
- **Scenario**: discover over a collection with a sensitive document.
  **Verify**: none of its chunks in any topic, none of its terms in a
  label.

## Validation

### Testing Strategy

Java tests on testcontainers PG for every function and route (Phases 1
and 3); Python contract tests that count requests at the HTTP client
(Phases 2 and 4); the parity fixture for clustering; the parity fixtures of
old and new reconcile paths. Done means every listed test passes and the
Minimum Viable Validation's counts hold on this repository.

### Performance Expectations

Measured against the baseline at Step 7: the delta-run linking and
`all_documents` parts (4-6 s and 1-5 s) should stop growing with tenant
size, since the scoped reads return one repository's rows. Whether they
shrink depends on how much of today's time is list size, which the baseline
does not say. The 20,000-row commit has an accept bar of 30 s in Phase 1.
No other target is claimed; full-run housekeeping savings have no baseline
and will be measured at Step 6, and discover's at Steps 11 and 12.

## Finalization Gate

### Contradiction Check

None found between research findings, design principles and the proposed
solution. The places where the design changes today's behaviour are listed
under Consequences, each with its reason. Gap 3 is recorded and not closed,
and the document says so where the gap is stated.

### Assumption Verification

Three load-bearing claims are unverified and gated at named steps with
pass or fail bars: commit parity (Step 2), the 20,000-row commit time
(Step 2) and HDBSCAN parity (Step 10). Three counts are measured at Step 0
before the identity tests are written. One mechanism is inferred, not read:
whether `unlink` deletes softly or hard (checked at Step 3). Every other
claim is verified by source reading or spike (research-1 to research-38).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `UPDATE ... FROM` a landing table, `INSERT ... ON CONFLICT (cols) WHERE ... DO NOTHING` | PostgreSQL 17 | Source Search (the register path uses the same partial-index conflict target, CatalogRepository.java:1527-1531) |
| `COLLATE "C"` ordering of text | PostgreSQL 17 | Documented (PostgreSQL collation support); fixture at Step 3 |
| `to_tsvector('english')`, `ts_lexize('english_stem', ...)` | PostgreSQL 17 | Spike (research-12) |
| `<=>` cosine distance, HNSW LATERAL with plan pin | pgvector 0.8.2 | Source Search (taxonomy-018/020) |
| `ChunkKeysSweepJobs` start, poll, `evictTenant` | engine | Source Search (research-16, research-35) |

### Scope Verification

The Minimum Viable Validation is in scope and runs at the end of Phase 2
(Steps 6 and 7) on this repository against a local engine.

### Cross-Cutting Concerns

- **Versioning**: paired releases; floor bumps with each engine cut;
  routes recorded as `[additive]` in the wire ledger.
- **Build tool compatibility**: pure Java, native-image safe; Liquibase
  changesets follow the per-dimension and GRANT re-issue shape.
- **Licensing**: N/A (no new dependency).
- **Deployment model**: engine cuts through the `engine-release` skill and
  the gates named at Step 5.
- **IDE compatibility**: N/A.
- **Incremental adoption**: old routes stay served; the client switches per
  phase with no fallback.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: the discover job holds one collection's vectors
  (n × d × 4 bytes) and refuses a collection over a stated heap share;
  HDBSCAN is O(n²) at n ≤ 5000 (about 25 million distances, computed in
  blocks); k-means above.

### Proportionality

The document is long because it carries two parts and three gaps that share
one mechanism, and because parity needs its rules written out. Open
question 3 asked whether Sam wants Part B held back; he did (2026-11-08),
which removes Steps 4, 8 and 10-12 from this RDR's acceptance.

### Open questions for Sam

1. **Landing tables or one request.** Sam retired the staging schema on
   2026-09-30.
   - (a) Landing tables, as designed: two tables in schema `nexus`, a run
     header, an expected-row check, a reaper. Each stage page is small and
     far inside the edge's timeout; a lost job is re-committed by `run_id`
     with no re-upload. Cost: two schema objects, the reaper, and the
     `incomplete_stage` failure mode.
   - (b) One commit request carrying every row, held in memory by the job.
     About 6 MB of JSON for 20,000 files at about 300 bytes a row
     (estimate, not measured). It removes the tables, the header, the
     expected-row check and the reaper. Cost: one upload must finish inside
     the edge's 30 s on a slow link, the edge's request-body limit (not
     read) must admit it, a lost job means a re-upload, and the engine
     holds each running commit's rows in memory.
   - Recommendation: (b), because it matches the staging retirement and
     removes the most machinery, on one condition: a Step 2 spike pushes a
     20,000-row body through the edge from a slow link. If that fails,
     (a).
   - **Answer (Sam, 2026-11-08): (a), landing tables, as designed.** Gate
     round 3 found that (b) loses the stored outcome a resubmit after a lost
     job reads, so the Step 2 spike is not run.
2. **Cross-repository path links.**
   - (a) Drop them: paths resolve by the file's identity, then inside the
     source's owner (as designed). Cost: links today made by accident of
     listing order disappear.
   - (b) Keep them behind an explicit opt-in per run, resolved by a stated
     rule (for example, only when exactly one repository has the path).
     Cost: a flag, a second resolution arm and its tests.
   - Recommendation: (a), unless Step 0's count 3 shows cross-repository
     links in numbers someone relies on; then (b) with the
     exactly-one-repository rule.
   - **Answer (Sam, 2026-11-08): the recommendation.** Drop them unless
     Step 0's count 3 shows reliance; then (b) with the
     exactly-one-repository rule.
3. **Scope at accept.**
   - (a) Accept Parts A, A4 and B together.
   - (b) Accept Parts A and A4 now, and hold Part B (taxonomy) for a later
     trigger. Cost: discover keeps downloading the collection until then.
   - Recommendation: (b). The baseline says the walk cost is not felt yet;
     Part A4 fixes a live correctness defect, and Part A is its carrier.
     Part B carries the Java port and its parity risk, and its egress
     figures are an August estimate with no felt cost behind them.
   - **Answer (Sam, 2026-11-08): (b).** Parts A and A4 are accepted, each on
     its own case; Part A4 does not depend on Part A1 (gate round 3), so the
     two can land in either order. Part B is held.

## References

- T2 `nexus/rdr193-reopen-measurement-2026-11-03` (the baseline)
- T2 `nexus_rdr/193-research-1` to `193-research-38`
- T2 `nexus/rdr193-pregate-critique-2026-11-08` (the pre-gate critique this
  revision answers)
- T2 `nexus/rdr-241-walk-census` (RDR-241's census, HOT PATH row)
- T2 `nexus/critique-rdr-193-gate-2026-08-15` [22617],
  `nexus/critique-rdr-193-gate-run2-2026-08-15` [22620]
- T2 `nexus/plan-serverside-catalog-reconcile-and-taxonomy-compute` [22599]
  and `-decisions` [22600] (design record of origin)
- T2 `flush-tail-investigation-2026-08-08` [21723],
  `indexing-perf-assessment-7.4.0` [21715] (August measurements)
- `docs/rdr/rdr-236-chunk-row-metadata-normalization.md`,
  `docs/rdr/rdr-241-no-client-walks-of-the-managed-catalog.md`
- Sources: `src/nexus/indexer.py`, `src/nexus/catalog/link_generator.py`,
  `src/nexus/catalog/http_catalog_client.py`,
  `src/nexus/catalog/indexed_meta.py`,
  `src/nexus/catalog/path_ambiguity.py`,
  `src/nexus/catalog/rdr_canonical.py`, `src/nexus/commands/rdr.py`,
  `src/nexus/commands/catalog_cmds/links.py`,
  `src/nexus/commands/taxonomy_cmd.py`,
  `src/nexus/db/t2/taxonomy_compute.py`,
  `src/nexus/db/t2/http_taxonomy_store.py`,
  `src/nexus/db/diag_connection.py`,
  `service/src/main/java/dev/nexus/service/db/CatalogRepository.java`,
  `service/src/main/java/dev/nexus/service/http/CatalogHandler.java`,
  `service/src/main/java/dev/nexus/service/http/ChunkKeysSweepJobs.java`,
  the changesets named above

## Revision History

- 2026-08-15: created (draft). Gate run 1 BLOCKED: 1 Critical (synchronous
  unbounded commit, made a job through a shared registry), 5 Significant
  (jsonb `||` merge, discover existing-topics guard, symmetric rename
  tie-break, `updated_at` to `indexed_at` per nexus-927mo, two-step
  `action` CTE), all folded in; critique T2
  `nexus/critique-rdr-193-gate-2026-08-15` [22617]. Gate run 2 PASSED; its
  2 Significant (one registry instance per job kind; client 409, lost-job
  and failed handling with a contract test) folded in; critique T2
  `nexus/critique-rdr-193-gate-run2-2026-08-15` [22620].
- 2026-09-16: deferred. Every motivating bead (nexus-ap8l0, nexus-ejo6k,
  nexus-w5xgy, nexus-7lw6a) closed without a user complaint. The reserved
  changeset ids had been taken; renumber at reopen. Reopen trigger: an
  index wall-time or catalog-phase cost regression.
- 2026-11-03: reopened (Sam), status deferred to draft. RDR-241 left the
  index-time link walk to this RDR. No older baseline existed; today's
  costs are recorded as the baseline in T2
  `nexus/rdr193-reopen-measurement-2026-11-03`.
- 2026-11-08: revised for re-gate (nexus-1vhfj). Restructured into the
  template sections. Changesets renumbered to `catalog-048` and
  `taxonomy-022`. Added Gap 4 (index-time link generation, from RDR-241)
  with Part A4. Re-read every code claim against 8ed7bc865: landing tables
  moved to schema `nexus` (staging dropped), job registry rebased on
  `ChunkKeysSweepJobs` with a 404 resubmit contract (RekeyJobs deleted),
  commit gained a run header (walk mode, expected row count, stored outcome
  for a safe resubmit), `deleted` and `keep` rows, and a per-owner job scope
  (delta runs skip housekeeping), RDR-236 and RDR-243 register-time rules
  carried into the commit and the discover job, Gap 3 restated after the
  nexus-rqsh1 interim fix, the ap8l0 obligation closed by
  taxonomy-018/020, line references updated. Baseline numbers stated once
  in Research Findings. Three open questions for Sam.
- 2026-11-08: revised again for the pre-gate critique (T2
  `nexus/rdr193-pregate-critique-2026-11-08`; research-25 to research-38).
  Part A1 re-derived from the code: five-arm predicate, outcome rows carry
  `content_hash_changed`, today's rename rule restated and the symmetric
  rule withdrawn, match on `(owner, file_path)` with `source_uri` derived
  engine-side and `foreign_owner` as its own class, the replaced methods'
  invariants listed with tests, declared all-or-nothing commit with
  classified refusals, landing key, dedup rule and `seq`, owner `head_hash`
  kept out of the commit. Gap 3 left open (the flag could not fire). Gap 4
  gained the same-as prune and relink, `set-status`, `generate-links`, the
  seed gates and an identity-first path resolution. Release gates renamed,
  Step 0 measurement added, open questions costed with recommendations,
  terms defined.
- 2026-11-08: Gate round 3 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); commit `962a93b3c`; critique `nexus_rdr/193-gate-critique-2026-11-08-r3`.
