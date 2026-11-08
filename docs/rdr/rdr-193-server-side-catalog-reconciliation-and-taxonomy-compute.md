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

## Problem Statement

Indexing does catalog and taxonomy work on the client, one HTTP call at a
time, against state whose canonical copy lives on the engine host. The work
is set-shaped (diff a file list against stored rows, age missing rows,
resolve paths to tumblers, cluster a collection's embeddings) and the engine
holds every row it touches. Batching between 2026-07 and 2026-08 (nexus-dst5h
one owner fetch per run, nexus-9dvqy `register_many`, nexus-xedhp
`update_many`, nexus-u2kwq and 67qsd `write_manifest_many`, nexus-lns3o and
yu9w5 `assign_from_chashes`, nexus-eslkl hook-lock narrowing) bought constant
factors and left the shape in place: the client owns the reconcile logic,
the link resolution and the clustering, and each further fix is another page
size or another batch route. This RDR moves the logic to where the rows are.

The cost today is real and small. The reopen measurement (the baseline in
Research Findings) puts the walk-shaped part at a few seconds per automatic
run; it grows with the catalog, and it is not yet a cost anyone has
complained of. The case for the change is the architecture (work that grows
with the tenant done one row per request, on every box) and the correctness
defects that ride with it (Gaps 3 and 4), not a present slowdown.

### Enumerated gaps to close

#### Gap 1: Catalog registration is a client-side reconcile loop over a downloaded copy of the catalog

Per `nx index repo` run (`src/nexus/indexer.py`):

- `_catalog_hook` (:1329) downloads the owner's full document list
  (`cat.by_owner`, :1520), builds `{file_path: entry}` in Python, and
  evaluates the change predicate in Python (:1736-1754: `head_hash`,
  `physical_collection`, `source_mtime`, `meta.content_hash`). It writes
  back through paged `update_many` and `register_many`
  (`_CATALOG_REGISTER_PAGE = 1000`, :337) with per-file `register`
  fallbacks when a page fails (:2053).
- `_run_housekeeping` (:2507) downloads the same owner list a second time,
  then resets or increments `meta.miss_count`, detects renames by
  `meta.content_hash`, moves links onto the renamed document, and deletes
  orphans, as one request per row (`w.update`, `w.link_if_absent`,
  `w.delete_document`), serially. A `delete_many` client method exists
  (`http_catalog_client.py:2023`) and is unused here. Housekeeping runs only
  on a complete walk: the delta path sets `skip_housekeeping`, because a
  miss-count sweep over a partial walk would age every unvisited document
  toward deletion (`_maybe_run_housekeeping`, :2438, the nexus-fltb4
  mass-delete class).
- `_build_frecency_doc_id_map` (:2881) downloads the owner list a third
  time to build `{file_path: tumbler}`. Four narrower paths
  (`_stored_paths_among` :1122, `_warn_symlink_ghosts_with_dependents`
  :1172, `_reconcile_needs_fence` :2338, `_pdfs_needing_index` :3267) each
  download it again when they run.
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
onto the HNSW index by `taxonomy-018` and pinned by `taxonomy-020`). What
remains on the client is four orchestrators (discover, rebuild, split,
`project_against`), the labeler's per-topic reads and writes, and per-pair
cross-link writes:

- `discover_for_collection` (`commands/taxonomy_cmd.py` :296, through
  `_discover_via_service` :180 and `_fetch_service_vectors` :100) pages
  every chunk's text to the client (250 per request, with
  `sensitivity="exclude"` so no sensitive chunk is read, RDR-243) and then
  every embedding (`t3.get_embeddings`), runs sklearn HDBSCAN for n ≤ 5000
  (`min_cluster_size=5`, `max_cluster_size` from `_max_cluster_size`, eom
  selection, centroid centres) or MiniBatchKMeans above that
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
one-time sweep tombstoned the existing phantoms. The guard is a heuristic
in front of the ordering, not a change to it: a file the guard admits that
still chunks to nothing is registered first and left behind. The engine is
the one place that can require a document and its manifest to arrive
together. The server-side reconcile therefore enforces the content rule
itself instead of trusting each client path to filter, and the ordering is
a named decision in the plan (Step 6).

#### Gap 4: Index-time link generation lists whole content types across the tenant to link one repository's new files

Taken over from RDR-241 (no client walks of the managed catalog), whose
scope table assigns this census HOT PATH to RDR-193: link generation on the
index path, `link_generator.py:60`, where `_entries_of_type` calls
`all_documents(content_type=...)`. After registration, `_catalog_hook`
(:2165-2260) runs four generators over the run's new tumblers:

- `generate_rdr_filepath_links` lists every `rdr` and every `code`
  document in the tenant (:277-278); `generate_prose_filepath_links` lists
  every `prose`, `markdown` and `docs` document plus every `code` document
  (:758-761). Both scan the new files on disk for path strings and resolve
  each against a dict of every code document keyed by its repo-relative
  `file_path` (:282-286). Two repositories that share a relative path
  collide in that dict, and the last listed wins, so an RDR can be linked to
  another repository's file.
- `generate_rdr_dependency_links` (:559), through `rdr_resolution` (:533),
  lists every `rdr` and every `prose` document in the tenant and keeps this
  repository's rows on the client (`is_in_repo`). `prose` is the default
  content type for all markdown, so this list grows with every markdown
  document of every repository. It then reads each candidate's frontmatter
  from disk.
- `generate_pdf_corpus_links` (:811) lists every `pdf` and `paper` document
  to group them by file-content hash and link duplicates `same-as`.

Every candidate edge then costs `link_if_absent`, which is a `link_query`
existence check plus a `POST /link`: two round trips per new edge
(`http_catalog_client.py:2962`). The engine has no batch link-write route,
and its list route cannot combine an owner filter with a content-type
filter (the branches of `CatalogHandler.handleList` are exclusive,
`CatalogHandler.java:517-551`), so a client that wanted only its own
repository's RDRs cannot ask for them.

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
| RDR-241 (draft) | Adjacent draft | Same diagnosis for tenant-wide aggregates; hands this RDR the index-time link walk (its scope table). Boundary below. |
| RDR-243 (accepted) | Context | Sensitive documents: discover and relabel must keep excluding them; `sensitive` moves on an existing document only through the audited route. |
| RDR-210 (closed) | Precedent | `ReembedJobRepository`: an engine job with PG-persisted rows, one of the two job precedents (Research Findings). |

Scope boundary with RDR-241:

| Concern | RDR-193 | RDR-241 |
| --- | --- | --- |
| Per-owner reconcile, housekeeping, rename detection in `nx index` | yes | no |
| Taxonomy discover, projection, cross-links, labeler batching | yes | no |
| Link generation on the index path (census HOT PATH) | yes (Gap 4) | names it, leaves it here |
| Tenant-wide aggregates in doctor, console, catalog verbs, MCP tools | no | yes |
| The ratchet lint over `all_documents` call sites | removes the HOT PATH row when Gap 4 ships | owns the lint |

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
  scoping each tenant, data-modifying CTE).
- **Batch repository methods exist:**
  `CatalogRepository.registerDocumentManyWithOutcome` (:1331, N documents in
  one transaction, tumblers claimed in input order through `claimNextSeq`
  :1735 on the owner's `next_seq`, with the `highestChildSeq` self-heal
  :1685) and `updateDocumentsMany` (:2075).
- **Soft delete** is a `deleted_at` column on `catalog_documents` and
  `catalog_links`, set by the `nexus.document_trash` function
  (`catalog-003`); `nexus.purge_trash` removes rows physically later.
  `catalog-016` makes `source_uri` unique among live rows
  (`ux_catalog_documents_live_source_uri`). `miss_count` is a key in the
  `meta` jsonb column, not a column.
- **Request timeouts.** The managed edge's origin timeout is about 30 s
  (`ChunkKeysSweepJobs` javadoc). A request whose duration grows with the
  corpus can time out at the edge while its transaction commits anyway (the
  GH #1390 class), so such work runs as an engine job: start, answer with an
  id, poll.
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
  transaction with no Java-only state. *Source: CatalogRepository.java:1459,1685-1748 (research-1)*
- **Verified** (source search): Rename precedence today is last-write-wins
  over `by_owner`'s `ORDER BY tumbler` (lexicographic), an accident; the SQL
  rule is a declared behaviour change. `catalog_links` conflict key is
  `(tenant_id, from_tumbler, to_tumbler, link_type)` with an existing merge
  that folds attribution into `meta['co_discovered_by']`. *Source:
  indexer.py:2525-2650; CatalogRepository.java:4380-4420 (research-2)*
- **Verified** (source search): `nx store put` registers a note under an
  `nx-note://<collection>/<title>` source URI (RDR-231; the engine
  canonicalises a legacy `chroma://` URI); `_find_ghost_by_title` is a
  ghost-only third tier; `_catalog_markdown_hook` also backfills title and
  year and applies the ephemeral-worktree guard. *Source: store_hook.py:784,955,1033; doc_indexer.py:1015,3845 (research-3)*
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
  `meta.content_hash` stamped at register; `tags` and `category`
  fill-only-if-empty; `source_format` re-stamped every register; `sensitive`
  set on a new document only, moved on an existing one only through
  `/sensitivity`; the hook's fence OUT-parameters need each row's
  `index_state` and `index_content_hash`. *(research-21)*
- **Verified** (source search): RDR-243 on taxonomy: discover reads with
  `sensitivity="exclude"`, relabel skips topics holding a sensitive
  document, and the engine already has the exclusion predicate
  (`vectors-055`). `nexus.chunks.chunk_tsv` is a stored
  `to_tsvector('english', chunk_text)` column. *(research-22)*
- **Verified** (source search): Gap 3's interim guard is in production
  (nexus-rqsh1, 7.9.0); `catalog-016` removed duplicate live `source_uri`
  rows, so multi-collision renames come from identical content at several
  paths. *(research-23)*

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

- [x] A set-based reconcile can reproduce the client's change predicate,
  miss-count machine, rename rule and register-time meta rules row for row.
  **Status**: Verified (the predicate and rules are read from source,
  research-1/2/4/21); parity is pinned by a test, Phase 1.
  **Method**: Source Search
- [x] Tumbler minting can run inside plpgsql. **Status**: Verified.
  **Method**: Source Search (research-1)
- [x] c-TF-IDF in SQL matches sklearn's top term. **Status**: Verified.
  **Method**: Spike (research-12)
- [ ] A Java HDBSCAN port reproduces sklearn on the parity fixture (ARI ≥
  0.90; 16/largest-40 capped; [359, 23] uncapped). **Status**: Unverified,
  gated at Step 10 (Phase 3) with that pass or fail bar.
  **Method**: Spike (at implementation)
- [ ] A 20k-row stage then commit finishes under 30 s on the test substrate.
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

- **Part A (Gaps 1 and 3):** a reconcile route pair (stage rows, commit as a
  job) replaces the owner download, the Python diff, the per-file writes,
  housekeeping and the frecency download. A document is registered only
  with evidence of content.
- **Part A4 (Gap 4):** one batch link-write route that resolves paths to
  tumblers inside the source document's owner; a list-route filter that
  returns one owner's documents of given types; a SQL function for the PDF
  `same-as` grouping. Each generator makes one write per run instead of two
  round trips per edge, and no generator lists a content type tenant-wide.
- **Part B1 (Gap 2, SQL):** whole-collection projection, centroid
  cross-links, and labeler batch routes as SQL functions and routes.
- **Part B2 (Gap 2, Java):** the discover, rebuild and split pipeline as an
  engine job with a Java HDBSCAN and k-means port.

### Technical Design

Interfaces, not implementations. Every route is tenant-scoped inside
`tenantScope.withTenant`, like every catalog route.

#### Shared: the engine job registry

Phase 1 extracts a shared `http/EngineJobs` registry from
`ChunkKeysSweepJobs`: memory-only, bounded running and queued counts (503
when full), one running job per scope key (a second identical start is
handed the running job; a different one is 409), retention of
finished jobs for their client to read, `evictTenant` wired to tenant
reset. The sharing is of the class, not the state: one instance per job
kind (`reconcileJobs`, `discoverJobs`, and `chunkKeysSweepJobs` moved onto
it), so a running catalog commit never blocks a discover for the same
tenant. The scope key is the tenant for discover and chunk-keys, and the
tenant plus owner for reconcile, so two repositories indexed at once (two
post-commit hooks) never wait on each other. A poll for an id the engine does not hold answers 404; that is the
whole "engine restarted" story, and it is safe because every job here is
idempotent to resubmit (reconcile by the run header's stored outcome,
discover by the existing-topics guard). Client contract, shared by every job client: 409
means poll the named running job and do not resubmit; 404 means log
`engine_job_lost` and resubmit once; `failed` means surface the error and
exit non-zero, never continue the run as if the job succeeded.

`ReembedJobRepository` (PG-persisted rows) is the other precedent and was
not chosen: these jobs are short and idempotent, and a persisted job table
is one more schema surface with nothing to recover that a resubmit does
not.

#### Part A1: reconcile on the engine (changeset `catalog-048-reconcile.xml`)

1. **Landing tables**, both with forced RLS and the tenant isolation
   policy. `nexus.catalog_reconcile_runs(tenant_id, run_id, owner_tumbler,
   walk, expected_rows, state, outcome jsonb, created_at)` holds one header
   per run: the first stage call of a run creates it with the walk mode
   and the number of rows the client will send. `nexus.catalog_reconcile_rows(tenant_id,
   run_id, kind, file_path, source_uri, physical_collection, content_hash,
   head_hash, source_mtime, content_type, title, meta jsonb, sensitive,
   chunkable)` holds the rows.
   `kind` is `present` (offered for register or update), `keep` (exists on
   disk but held back this run; counts as present, never written), or
   `deleted` (an explicit same-run deletion: a git deletion in a delta run,
   or a path the walk now excludes, nexus-d6qmz). It holds catalog rows only,
   never chunks, so the RDR-223 ordering rule is untouched. It lives in
   schema `nexus` because schema `staging` is retired. (Open question 1 asks
   whether Sam wants a landing table at all.)
2. **`nexus.catalog_reconcile_commit(p_owner text, p_run_id text) RETURNS
   jsonb`** (the outcome below): plpgsql, VOLATILE, SECURITY INVOKER (RLS
   scopes every row), one transaction. The walk mode, `complete` or
   `partial`, comes from the run header.
   - **Guards first.** A header in state `committed` returns its stored
     `outcome` and changes nothing, which is what makes a resubmit after a
     lost job safe. A header whose staged row count differs from
     `expected_rows` fails the commit (`incomplete_stage`) before any
     write, so a lost stage page can never read as a set of missing
     files. No header fails with `run_not_found` (reaped or never staged).
   - **Upsert.** `INSERT ... ON CONFLICT` on the live-row identity
     (`ux_catalog_documents_live_source_uri`, `catalog-016`) `DO UPDATE ...
     WHERE (head_hash, physical_collection, source_mtime,
     meta->>'content_hash') IS DISTINCT FROM (excluded...)`. The Python
     change predicate becomes the `WHERE`. A WHERE-guarded `DO UPDATE` that
     does not fire emits nothing through `RETURNING`, so the three-way
     action (`created`, `updated`, `unchanged`) is produced in two steps: a
     CTE captures `RETURNING file_path, tumbler, (xmax = 0) AS created`, and
     the final select left-joins the landing rows to it and to
     `catalog_documents`. Unchanged rows are not rewritten and `indexed_at`
     does not move; changed rows refresh `indexed_at` only when `head_hash`
     changes (the nexus-927mo rule, `CatalogRepository.java:2355-2372`).
   - **Register-time meta rules**, carried over from the client: every
     `meta` write is a merge (`meta || jsonb_build_object(...)` or
     `jsonb_set`), never a replacing `SET meta = ...` (nexus-ke45f);
     `content_hash` is always written; `source_format` is compared and
     re-stamped; `tags` and `category` are written only where the stored
     value is unset (absent, null, empty string or empty array: the
     `indexed_meta.is_unset` predicate, nexus-3ax49).
   - **Sensitivity.** `sensitive` from the landing row is written on a
     `created` row only. An existing document is never flagged by the
     upsert; the commit returns the tumblers whose landing row asked for
     `sensitive` and whose stored row lacks it, and the client raises them
     through the audited `/sensitivity` route as it does today (RDR-243).
   - **Content evidence (Gap 3).** Each `present` row carries
     `chunkable`, the client's pre-registration guard verdict (zero-byte,
     whitespace-only and binary-sniffed files are `false`). The commit
     creates no document for a row with `chunkable = false` and returns the
     count as `refused_unchunkable`, so the guard becomes a rule the engine
     enforces rather than a filter each client path must remember. This is
     still a verdict made before chunking: the hook must hand the run its
     tumblers before per-file indexing starts (`_catalog_hook` returns the
     `{abs_path: doc_id}` map the indexers use), so a file the guard admits
     that then chunks to nothing is still registered. The complete fix, a
     new document created only together with its first manifest write,
     reorders the client pipeline and is the decision point at Step 6.
   - **Housekeeping, only when the run's walk is `complete`**: owner documents with
     no `present` or `keep` row get `meta.miss_count + 1`; present ones get
     `0`; a document reaching `_ORPHAN_DELETE_MISSES` (2) is trashed with
     `nexus.document_trash`. A miss-count-only touch does not move
     `indexed_at`. **Rename**: a `created` row whose `content_hash` equals a
     missing row's moves that row's links onto the new tumbler (the
     `ON CONFLICT` merge `link_if_absent` uses today), then trashes the old
     row. Links move before the trash, so the `catalog-032` cascade, which
     fires only at purge, never sees them. The tie-break is symmetric:
     among `created` candidates the lowest tumbler in numeric segment order
     is the target, among missing candidates the lowest tumbler is the
     source, each missing row is consumed at most once, and leftovers fall
     through to the miss-count branch.
   - **Partial walk** (walk `partial`, every delta run): no miss-count
     branch at all. This is the nexus-fltb4 gate moved into SQL; a test pins
     that a partial commit never touches an unvisited row.
   - **Explicit deletions**, in both walk modes: `deleted` rows are trashed
     in the same transaction, which replaces `_delete_docs_for_paths`.
   - **Owner** `head_hash` update (`_set_owner_head_hash` folds in).
   - **Tumbler minting** for `created` rows inside the function: owner row
     `FOR UPDATE`, the highest-child self-heal, the `claimNextSeq` contract
     in plpgsql.
   - **Finish**: delete the run's rows, set the header to `committed` and
     store the outcome on it.
3. **`nexus.catalog_reconcile_reap(p_interval)`** drops headers and rows
   of runs created longer ago than the interval (default 24 h), committed
   or not. It runs at the start of every stage call for the tenant, so no
   scheduler is needed.

Routes (`CatalogHandler` and `CatalogRepository`):

| Route | Body | Returns |
| --- | --- | --- |
| `POST /v1/catalog/reconcile/stage` | `{owner, run_id, walk, expected_rows, rows[≤1000]}` (`walk` and `expected_rows` must match the header on later pages) | `{staged}` |
| `POST /v1/catalog/reconcile/commit` | `{owner, run_id}` | `202 {job_id}`; `409 {job_id}` when a commit for the same owner is running |
| `GET /v1/catalog/reconcile/{job_id}` | none | `{status: running}`, or `{status: succeeded, rows: [{file_path, tumbler, action, index_state, index_content_hash}], trashed: [...], renamed: [{old, new}], sensitivity_pending: [...], refused_unchunkable: n}`, or `{status: failed, error}`; 404 for an unknown id |
| `POST /v1/catalog/doc/resolve_or_register` | `{owner_name or curator, source_uri or file_path, title, year?, title_fallback?}` | `{tumbler, created}`; title and year backfilled with `COALESCE` on empty; ghost-only title match when `title_fallback` |

`rows` in the commit envelope carries every file of the run with its
tumbler, so it replaces the frecency download, and it carries
`index_state` and `index_content_hash` so the
hook can keep filling `stale_fence_doc_ids`, `complete_doc_hashes` and
`needs_fence` with no extra request. The stage cap mirrors
`CatalogHandler.MAX_BATCH_DOC_IDS = 1000`; a 20,000-file repository is 20
stage calls, one commit, and polls.

#### Part A2: the client switch

- `catalog/http_catalog_client.py`: `reconcile_stage`, `reconcile_commit`
  (submit and poll with the shared job contract), `links_upsert_many`,
  `resolve_or_register`, added to `_SERVICE_ONLY_WRITE_OPS`
  (`catalog/factory.py:417`).
- `indexer.py` `_catalog_hook`: keep the walk, stat, sha256, ephemeral and
  chunkability guards, and classification; replace the `by_owner` join, the
  Python diff, `register_many` and `update_many` with stage, commit, poll;
  feed the envelope's `rows` to the frecency map; re-point each of the four
  narrower owner downloads at the envelope where it holds what that path
  reads, and keep the owner list for the rest (each runs only on its own
  condition);
  delete `_run_housekeeping` and `_delete_docs_for_paths` (their work is in
  the commit); pass `walk="partial"` exactly where `skip_housekeeping` is set
  today.
- `doc_indexer.py` `_register_or_lookup_doc_id` and `_catalog_markdown_hook`
  collapse to one `resolve_or_register` carrying title, year and
  `source_mtime` (`chunk_count` is already folded into `write_manifest_many`
  through `EX_DOC_CHUNKS`); `store_hook.py` the same.
- No fallback to the old path.

#### Part A4: index-time linking (Gap 4, changeset `catalog-048-reconcile.xml`)

1. **`nexus.catalog_links_upsert_many(p_edges jsonb) RETURNS TABLE(created
   int, skipped int, unresolved jsonb)`**: each edge names its source and
   target either by tumbler or by `(owner, file_path)`. A path endpoint
   resolves to the live document with that `file_path` under the named
   owner, which is the source document's owner for the file-path
   linkers; a path with no match there is returned in `unresolved`, never
   matched in another owner. Insert `ON CONFLICT DO NOTHING` on the
   `(tenant_id, from_tumbler, to_tumbler, link_type)` key, so an existing
   link keeps its fields (today's "if absent" contract, which
   `link_if_absent` gets from its pre-check). One transaction.
2. **List-route filter:** `GET /v1/catalog/list?owner=<prefix>&content_type=<t>[&content_type=<t2>]`
   returns one owner's documents of the named types, so `handleList` gains
   one combined branch. The RDR-dependency linker asks for the current
   owner's `rdr` and `prose` rows, and separately for rows under its
   `rdr_source_prefix` (the second admission arm of `is_in_repo`, which
   covers stale owners of the same repository), with a
   `source_uri_prefix=` filter. Its rows are this repository's RDR-shaped
   documents, not every markdown document in the tenant.
3. **`nexus.catalog_link_same_content(p_tumblers text[]) RETURNS int`**:
   for each given `pdf` or `paper` document, find the live `pdf` or
   `paper` documents with the same file-content hash (`index_content_hash`,
   else `meta.content_hash`; never `head_hash`, nexus-utkop), and link each
   new member `same-as` to the group's anchor (lowest tumbler), the rule
   `generate_pdf_corpus_links` applies today. One statement.
   Routes: `POST /v1/catalog/links/upsert_many {edges: [...]}` (at most 1000
   edges per call) and `POST /v1/catalog/links/same_content {tumblers}`.
4. **Client:** the file-path linkers keep their regex scans of new files
   and send path edges through one `links_upsert_many` call per generator
   (paged at 1000 edges); the
   RDR-dependency linker reads its scoped candidates (item 2), keeps
   `rdr_canonical.resolve_all` and the frontmatter reads on the client
   (they need the file), and sends tumbler edges through one
   `links_upsert_many`; the PDF linker becomes one call to item 3's route.
   `_entries_of_type` and the tenant-wide `path_to_code` dict are deleted.
   RDR-241's ratchet table loses its HOT PATH row in the same change.

Path resolution inside the owner is a declared behaviour change: an RDR in
one repository that names a path existing only in another repository no
longer gets an `implements` link to it. Today that link is made only when
the last listed document for the path happens to be the other repository's,
which is the defect. (Open question 2 asks Sam whether cross-repository
path links should be kept on an explicit opt-in.)

#### Part B1: taxonomy SQL (changeset `taxonomy-022-serverside-projection-links.xml`)

- `nexus.taxonomy_project_collection_<dim>(p_src_collection, p_threshold)`:
  the whole-collection form of the `assign_from_chashes` cross pass, built
  on the `taxonomy-018` per-chunk LATERAL over HNSW with the `taxonomy-020`
  plan pin; argmax over foreign centroids at or above the projection
  threshold, upsert `topic_assignments` (`assigned_by='projection'`,
  `GREATEST` semantics verbatim), no egress. Replaces `project_against` and
  `_svc_fetch_all_embeddings`.
- `nexus.taxonomy_cross_links(p_collection, p_threshold)`: centroid against
  foreign centroid cosine through `<=>`, `INSERT topic_links ON CONFLICT`,
  returns a count. Replaces `compute_cross_links` and the per-pair
  `persist_cross_links`.
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
  {job_id}`; `GET /v1/taxonomy/discover/{job_id}` polls; 409 when a
  discover for the tenant is running; 404 for an unknown id. Shared job
  contract above.
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

1. Tumbler minting moves into plpgsql (research-1).
2. Rename precedence is the declared symmetric lowest-tumbler rule
   (research-2), tested with identical-content fixtures (research-23).
3. `resolve_or_register` is keyed on `(owner, source_uri)` with a
   ghost-only title fallback; the ephemeral-worktree guard stays on the
   client (research-3).
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

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `EngineJobs` registry | `http/ChunkKeysSweepJobs.java` | Extend: extract the class, move chunk-keys onto it |
| Reconcile commit | `CatalogRepository.registerDocumentManyWithOutcome`, `updateDocumentsMany`, `claimNextSeq` | Replace for the index path; keep the routes for other callers |
| Landing tables | `staging` schema (dropped) | Two new tables in schema `nexus` (a run header and catalog rows; never chunks) |
| `links_upsert_many` | `CatalogRepository.upsertLink`, `/link` | Reuse its conflict key in a batch function that inserts only when absent (the route itself upserts) |
| Owner plus type list filter | `CatalogHandler.handleList` | Extend with a combined branch |
| Projection, cross-links | `assign_from_chashes_<dim>` (taxonomy-018/020 body) | Extend to whole-collection form |
| Sensitivity exclusion in the job | `vectors-055` predicate, `ChunkSensitivity` | Reuse |
| Discover clustering | `db/t2/taxonomy_compute.py` (sklearn) | Replace with Java port |

### Decision Rationale

The residual cost is round-trip count and download size, and both go to a
constant only when the logic moves to where the rows are; another batch
route keeps the client walking. The same move fixes two correctness defects
the client shape carries: registration before content (Gap 3) and
tenant-wide path resolution (Gap 4). SQL keeps each state change in one
transaction under the RLS the engine already enforces; the Java job exists
only because clustering has no SQL form.

## Alternatives Considered

### Alternative 1: Keep batching the client path

**Description**: Another `_many` route, larger pages, a memoised owner
list, a batch link route with client-side resolution.

**Pros**:

- No engine cut for the catalog half; smaller changes.

**Cons**:

- Leaves the owner downloads, the tenant-wide type lists and the client
  diff; each step buys a constant factor, as 2026-07/08 did.
- Leaves the tenant-wide path resolution defect.

**Reason for rejection**: the cost grows with the tenant, and the
correctness defects are properties of where the logic runs.

### Alternative 2: Synchronous commit and discover routes

**Description**: Plain request and response for commit and discover.

**Pros**:

- Simpler client.

**Cons**:

- The edge's origin timeout (about 30 s) would cut long requests while the
  transaction commits anyway (the GH #1390 class).

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

- Positive: a warm full run's catalog phase goes from several owner
  downloads, O(changed) pages and O(missing) serial writes to stage calls,
  one commit and polls; a delta run loses its owner download and per-file
  writes. Link generation goes from tenant-wide type lists and two round
  trips per edge to one write per generator and one scoped read, which
  replaces the 4-6 s and 1-5 s parts of the baseline and stops their growth
  with the tenant (measured at Step 7, not assumed). `nx index md`
  and `store put` identity resolution becomes one request. Discover's
  egress goes to near zero and its round trips to a submit and polls (the
  relabel still pulls a few sample texts per topic, by design). The client
  sheds the reconcile machine, sklearn from the discover path, and the
  cross-process centroid-cache staleness noted at
  `http_centroid_store.py:97-133`.
- Negative: two engine cuts; a Java HDBSCAN port with a parity obligation;
  two landing tables and a job registry as operational surfaces.
- Behaviour changes, declared: the rename tie-break; owner-scoped path
  links; a `chunkable = false` row is refused instead of registered.
- Migration: none for data; the functions work on existing tables. The old
  engine routes stay served for other callers until a later retirement RDR.
  A new client against an old engine fails loud on the new routes; an old
  client against the new engine keeps working on the retained routes.

### Risks and Mitigations

- **Risk**: the SQL change predicate differs from Python and rewrites
  unchanged rows (the "always-true drift predicate restores the write
  storm" finding from nexus-dst5h).
  **Mitigation**: parity test, old path against commit on one fixture,
  identical row state and unchanged `indexed_at`.
- **Risk**: a partial walk reaches the miss-count branch (mass delete).
  **Mitigation**: the walk mode is a required field of the run header; a
  test commits a partial run over
  an owner with unvisited rows and asserts none changed.
- **Risk**: a meta rule from RDR-236 or RDR-243 is lost in the move.
  **Mitigation**: tests for fill-only `tags`/`category`, re-stamped
  `source_format`, and `sensitive` never set on an existing row by the
  commit.
- **Risk**: the Java port diverges from sklearn.
  **Mitigation**: the parity fixture gate at Step 10 blocks Phase 3.

### Failure Modes

- The engine restarts mid-job: the poll gets 404, the client logs
  `engine_job_lost` and resubmits once; reconcile re-commits the same
  `run_id` (a committed run returns its stored outcome), discover skips
  committed collections.
- A commit fails: the envelope says `failed` with the error; the index run
  exits non-zero and says so. Landing rows remain and are reaped after 24 h.
- A stage page is lost: the commit fails `incomplete_stage` before any
  write; the run is re-staged under a new `run_id`.
- An unresolved link path: returned in `unresolved` and logged with the
  source document; no edge.
- Diagnosis: `catalog_hook_stage_timing` keeps its stage names (stage,
  commit, linking per generator), so a slow phase is still attributable.

## Implementation Plan

### Prerequisites

- [ ] Sam's answers to the open questions (Finalization Gate).
- [x] Changeset ids checked free: `catalog-048`, `taxonomy-022`.

### Minimum Viable Validation

A warm full `nx index repo` of this repository against a local engine built
from the Phase 1 tree makes exactly one commit request, `ceil(N/1000)` stage
requests, zero single `update`, `register`, `delete` or `link` requests, and
zero `GET /list?content_type=` requests without an owner filter (counted at
the HTTP client), and leaves catalog row state identical to the old path on
the same tree. Then a delta run with one changed RDR does the same with
`walk=partial` and the linking stage costs at most one scoped read and one
write per generator.

### Phase 1: Engine half of Parts A, A4 and B1 (engine cut 1)

#### Step 1: Job registry

Extract `EngineJobs` from `ChunkKeysSweepJobs`, move chunk-keys onto it,
add the reconcile instance. Tests: running, succeeded, failed, 409, 404,
503, retention, `evictTenant`.

#### Step 2: Reconcile changeset and routes

`catalog-048-reconcile.xml` (landing tables, commit, reap), the three
reconcile routes and `resolve_or_register`. Java tests on testcontainers
PG: unchanged rows not rewritten; changed field `updated` with `indexed_at`
moved only on a `head_hash` change; miss, second miss, trash; miss-count
touch keeps every other meta key; rename moves links before the trash;
two-by-two identical-content rename per the symmetric rule; partial walk
touches no unvisited row and trashes `deleted` rows; `keep` rows count as
present; meta rules (fill-only, re-stamp, merge); `sensitive` only on
created rows, with `sensitivity_pending` returned; `refused_unchunkable`;
RLS isolation; landing rows cleared; a re-commit returns the stored
outcome and writes nothing; a short stage fails `incomplete_stage` with no
write; two owners commit concurrently without a 409; the 20k-row
commit under 30 s (accept bar); parity against the old `update_many` path.

#### Step 3: Linking functions and filter

`catalog_links_upsert_many`, `catalog_link_same_content`, the combined
owner and content-type list branch with `source_uri_prefix`. Tests: owner
resolution, a path present only in another owner returns `unresolved`, an
existing link keeps its fields, `same-as` grouping matches today's anchor
rule, RLS.

#### Step 4: Taxonomy SQL and labeler routes

`taxonomy-022-serverside-projection-links.xml` and the two labeler routes.
Tests: projection equals today's `project_against` result on a seeded
fixture; cross-links count and rows; `label_context_many` returns no
sensitive chunk and names topics holding a sensitive document; a duplicate
label inside one batch resolves without a 409.

#### Step 5: Wire ledger and engine cut 1

Record the routes in `docs/wire-contract-pending.md` as additive; cut the
engine through the `engine-release` skill with the migration rehearsal
`--candidate-migration`.

### Phase 2: Client half of Parts A, A4 and B1

#### Step 6: Reconcile client

The A2 switch and deletions. Contract tests in the nexus-dst5h style:
commit exactly once per run, stage `ceil(N/1000)`, zero single writes in a
warm run; the job contract (409 waits, 404 resubmits once, failed aborts);
the hook's OUT-parameters filled from the envelope; a service-mode test
through `ensure_engine`. Decision point (Gap 3): after a full run of this
repository, count new documents with `chunk_count 0` and no manifest. If
any remain, move document creation for new files into their first
manifest write, and record the change here before Step 7.

#### Step 7: Linking client

The A4 client changes and deletions, and the HOT PATH row removed from
RDR-241's ratchet table. Probe: re-run the baseline's delta shape (a
change to one RDR, one to a few code files) and compare linking and
`all_documents` time with the baseline.

#### Step 8: Taxonomy client for B1

Projection, cross-links and relabel switched to the Step 4 routes, with
the client deletions listed under Part B1. Test: relabel still skips topics
with a sensitive document.

#### Step 9: Floor bump and release

`REQUIRED_ENGINE_VERSION` raised to engine cut 1 in the client release that
ships Steps 6-8 (paired release).

### Phase 3: Part B2 engine (engine cut 2)

#### Step 10: Clustering port

Java HDBSCAN, k-means and `VectorMath`. Gate: the parity fixture (ARI ≥
0.90, capped 16 clusters with the largest 40, uncapped [359, 23]);
`_merge_labels` and `compute_split` reproduced on seeded fixtures. The
phase stops here if the gate fails.

#### Step 11: Discover service, routes and engine cut 2

`TaxonomyDiscoverService`, the `discoverJobs` instance and routes, then
engine cut 2 with the same gates as Step 5. Tests: seeded chunks give
topics, assignments, centroids and meta in one transaction per collection;
sensitive chunks never reach clustering or labels; `skipped_existing`
without `force`; resubmit after 404 skips committed collections; RLS.

### Phase 4: Part B2 client

#### Step 12: Discover client

Submit, poll, summary, deletions. Contract test: `nx taxonomy discover`
against `ensure_engine` makes zero `/vectors/get`, `/get-embeddings` and
`/centroids/foreign` calls. Floor bump to engine cut 2 in the release that
ships it.

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
- **Scenario**: delta run, one file deleted, others unvisited. **Verify**:
  the deleted file's document trashed, no unvisited row touched.
- **Scenario**: a file renamed with identical content, and a two-by-two
  identical-content collision. **Verify**: links moved to the new
  tumbler before the old is trashed; symmetric rule applied.
- **Scenario**: re-index of a document whose `tags` were set by hand.
  **Verify**: `tags` kept, `source_format` re-stamped.
- **Scenario**: `--sensitive-glob` matches an existing unflagged document.
  **Verify**: commit does not flag it; the client raises it through
  `/sensitivity`.
- **Scenario**: an RDR names a path that exists in this repository and
  in another. **Verify**: the link targets this repository's document.
- **Scenario**: two PDFs with the same content in different collections.
  **Verify**: one `same-as` link to the lowest tumbler.
- **Scenario**: the engine restarts during a commit and during a discover.
  **Verify**: 404, one resubmit, correct final state.
- **Scenario**: discover over a collection with a sensitive document.
  **Verify**: none of its chunks in any topic, none of its terms in a
  label.

## Validation

### Testing Strategy

Java tests on testcontainers PG for every function and route (Phases 1
and 3); Python contract tests that count requests at the HTTP client
(Phases 2 and 4); the parity fixture for clustering; the parity test of
old and new reconcile paths on one fixture. Done means every listed test
passes and the Minimum Viable Validation's counts hold on this repository.

### Performance Expectations

Measured against the baseline: the delta-run linking and `all_documents`
parts (4-6 s and 1-5 s) should stop growing with tenant size, since the
scoped read returns one repository's RDR-shaped rows. The 20k-row commit
has an accept bar of 30 s in Phase 1. No other target is claimed; full-run
housekeeping savings have no baseline and will be measured at Step 6.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. Two places where the design changes today's behaviour
are declared as such (rename tie-break, owner-scoped path links), and the
Gap 3 fix is partial by design with a named decision point (Step 6).

### Assumption Verification

The HDBSCAN parity and the 20k-row commit time are unverified and gated at
named steps with pass or fail bars (Step 10, Step 2). Every other
load-bearing claim is verified by source reading or spike (research-1 to
research-24).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE`, `RETURNING (xmax = 0)` | PostgreSQL 17 | Source Search (pass 1) |
| `to_tsvector('english')`, `ts_lexize('english_stem', ...)` | PostgreSQL 17 | Spike (research-12) |
| `<=>` cosine distance, HNSW LATERAL with plan pin | pgvector 0.8.2 | Source Search (taxonomy-018/020) |
| `ChunkKeysSweepJobs` start, poll, `evictTenant` | engine | Source Search (research-16) |

### Scope Verification

The Minimum Viable Validation is in scope and runs at the end of Phase 2
(Steps 6 and 7) on this repository against a local engine.

### Cross-Cutting Concerns

- **Versioning**: paired releases; floor bumps with each engine cut;
  routes recorded as additive in the wire ledger.
- **Build tool compatibility**: pure Java, native-image safe; Liquibase
  changesets follow the per-dimension and GRANT re-issue shape.
- **Licensing**: N/A (no new dependency).
- **Deployment model**: engine cuts through `engine-release`, migration
  rehearsal mandatory.
- **IDE compatibility**: N/A.
- **Incremental adoption**: old routes stay served; the client switches per
  phase with no fallback.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: the discover job streams embeddings over JDBC;
  HDBSCAN is O(n²) at n ≤ 5000 (about 25 million distances, computed in
  blocks); k-means above.

### Proportionality

The document is long because it carries two parts and four gaps that share
one mechanism. Open question 3 asks whether Sam wants Part B held back,
which would remove Steps 4 and 8 and Phases 3-4 from this RDR's acceptance.

### Open questions for Sam

1. **Landing table.** Sam retired the staging schema on 2026-09-30. This
   design adds two catalog-row landing tables (`nexus.catalog_reconcile_runs`
   and `nexus.catalog_reconcile_rows`)
   in schema `nexus`, so a 20,000-file run can stage in 1000-row pages and
   commit once. The alternative is no landing table: one commit request
   carrying every row (a few MB for 20,000 files), held by the job. Which?
2. **Cross-repository path links.** Owner-scoped resolution drops links
   from an RDR to a path that exists only in another repository. Keep them
   dropped, or keep them behind an explicit opt-in?
3. **Scope at accept.** The baseline says the walk cost is not yet felt.
   Accept Parts A, A4 and B together, or Part A and A4 now with Part B
   (taxonomy) held for a later trigger?

## References

- T2 `nexus/rdr193-reopen-measurement-2026-11-03` (the baseline)
- T2 `nexus_rdr/193-research-1` to `193-research-24`
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
  `src/nexus/catalog/http_catalog_client.py`, `src/nexus/catalog/indexed_meta.py`,
  `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/taxonomy_compute.py`,
  `src/nexus/db/t2/http_taxonomy_store.py`,
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
  (delta runs skip housekeeping), RDR-236 and RDR-243 register-time rules carried into the
  commit and the discover job, Gap 3 restated after the nexus-rqsh1 interim
  fix, the ap8l0 obligation closed by taxonomy-018/020, line references
  updated. Baseline numbers stated once in Research Findings. Three open
  questions for Sam.
