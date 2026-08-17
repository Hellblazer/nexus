---
title: "Server-Side Catalog Reconciliation and Taxonomy Compute: Move the Index-Time Catalog Diff/Housekeeping/Linking and the Taxonomy Discover Pipeline onto the Engine as Transactional SQL and Java Jobs"
id: RDR-193
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-08-15
accepted_date:
related_issues: [nexus-ap8l0, nexus-ejo6k, nexus-w5xgy, nexus-7lw6a]
related: [RDR-070, RDR-075, RDR-095, RDR-108, RDR-155, RDR-156, RDR-164, RDR-181, RDR-191]
---

# RDR-193: Server-Side Catalog Reconciliation and Taxonomy Compute

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

Gate log: run 1 (2026-08-15) BLOCKED — 1 Critical (sync unbounded commit →
made async via shared `EngineJobs`), 5 Significant (jsonb `||` merge,
discover existing-topics guard, symmetric rename tie-break,
`updated_at`→`indexed_at`/nexus-927mo, two-step `action` CTE) — all folded
in below; critique T2 `nexus/critique-rdr-193-gate-2026-08-15` [22617].
Run 2 (2026-08-15) PASS — all 10 run-1 findings verified resolved; 2 new
Significant (one `EngineJobs` instance per job kind; client 409/410/failed
envelope handling + contract test) folded in; critique T2
`nexus/critique-rdr-193-gate-run2-2026-08-15` [22620].

Design record of origin: T2 `nexus/plan-serverside-catalog-reconcile-and-taxonomy-compute`
[22599] + `-decisions` [22600]; T3 knowledge doc
`d4d6e32e7708ac52c1eb08bb578aa66a4ccad3b7edbd9980435aa405a583cf00`.

## Problem Statement

Indexing has two remaining time sinks that are round-trip and egress shaped.
Both manipulate state whose canonical copy already lives on the engine host,
and both do that manipulation on the client, one HTTP call at a time. Every
catalog op costs the ~0.1s cloud round-trip floor regardless of payload
(measured: T2 `flush-tail-investigation-2026-08-08` [21723] — `begin_index_run_many`
0.114 s/call as the batched control against 0.10-0.13 s for every per-row op).

Both have been *batched* incrementally over 2026-07/08 (nexus-dst5h one-fetch
join, nexus-9dvqy `register_many`, nexus-xedhp `update_many`, nexus-u2kwq /
67qsd `write_manifest_many`, nexus-lns3o / yu9w5 `assign_from_chashes`,
nexus-eslkl hook-lock narrowing). Batching bought a constant factor and left
the architecture in place: the client still owns the reconcile logic and the
clustering, and every additional bead is another page-size or another
whitelist entry. This RDR replaces the architecture, not the batch size.

### Gap 1: Catalog registration is a client-side reconcile loop over a downloaded copy of the catalog

Per `nx index repo` run (`src/nexus/indexer.py`):

- `_catalog_hook` (~:877) downloads the owner's FULL document list
  (`cat.by_owner`, ~:986), builds `{file_path: entry}` in Python, stats and
  hashes every file, and evaluates the change predicate in Python
  (~:1176-1191: `head_hash` / `physical_collection` / `source_mtime` /
  `meta.content_hash`). It then writes back through paged `update_many` /
  `register_many` (1000/page, `update_many` at :1250, `_CATALOG_REGISTER_PAGE` :345) with per-file
  `update` / `register` fallbacks.
- `_run_housekeeping` (~:1531-1610) downloads the SAME owner list a SECOND
  time, then does miss_count reset / increment, rename detection by
  `meta.content_hash`, rename link transfer, and orphan delete — as ONE POST
  PER ROW (`w.update`, `w.link_if_absent`, `w.delete_document`), serial. A
  `/delete_many` route exists and is unused here.
- `_build_frecency_doc_id_map` (~:1739) downloads the owner list a THIRD
  time to build `{file_path: tumbler}`.
- Link generation (`catalog/link_generator.py`) issues ~8 UNBOUNDED
  `GET /list?content_type=` full-corpus downloads per run and one
  `POST /link` per edge — there is NO batch link route.
- `nx index md` (`doc_indexer.py` `_register_or_lookup_doc_id` :565 +
  `_catalog_markdown_hook` :2583) and `nx store put`
  (`catalog/store_hook.py::catalog_store_hook_tracked` :180) spend 3-6
  identity-resolution round trips PER DOCUMENT, and the markdown hook repeats
  the owner+path lookups the register step just did.

The client work that genuinely needs the client is: walking the tree,
`stat`, sha256, ephemeral-path guard, content classification, and the regex
scan that finds link targets. Everything else — the diff, the upsert, the
miss_count state machine, rename detection, link resolution — is a
set-operation over rows the engine already holds.

### Gap 2: Taxonomy discover downloads the entire collection to cluster it, though embeddings are born on the engine

The flush-time ASSIGN leg is already server-side (`nexus.assign_from_chashes_<dim>`,
Liquibase `taxonomy-006`, shipped conexus 7.4.0 / engine v0.1.68; measured
20-30x lower, T2 `indexing-perf-assessment-7.4.0` [21715]). What remains
client-side is exactly four orchestrators — `discover`, `rebuild`, `split`,
`project_against` — plus the labeler's two N+1 loops and the per-pair
`persist_cross_links`:

- `discover_for_collection` (`commands/taxonomy_cmd.py` :242 → :143 → :68)
  pages EVERY chunk's text (250/req, `POST /v1/vectors/get` — the server
  ignores `include=` and returns full documents) AND EVERY embedding
  (300/req, `/get-embeddings`) to the client, runs sklearn HDBSCAN
  (n ≤ 5000; `min_cluster_size=5`, `max_cluster_size=max(n//4, 50)`, eom,
  centroid centres) or MiniBatchKMeans (n > 5000; `k=max(10, √n/3)`,
  seed 42) plus c-TF-IDF (`CountVectorizer(stop_words="english")`, top-3)
  in `db/t2/taxonomy_compute.py` (:159 `_cluster`, :374), then ships every
  assignment back (`persist_discovered` carries each spec's full doc_id list).
- Cross-links: `compute_cross_links` (`db/t2/http_taxonomy_store.py` :759)
  downloads ALL foreign centroid vectors and does the cosine matmul
  client-side; `persist_cross_links` (:908) is ONE POST PER PAIR.
- `project_against` (:1414 → `_svc_fetch_all_embeddings` :1234) re-downloads
  every embedding of every source collection a SECOND time under `--all`.
- LLM labeler `relabel_topics` (taxonomy_cmd.py :1746): one
  `GET /assignments/docs` PER TOPIC (:1787) and one `POST /topics/update_label`
  PER TOPIC (:1860).

Order of magnitude for a 100k-chunk / 1024-dim collection: ~740 bulk round
trips and ~1.2-1.6 GB egress (JSON float arrays; ~400 MB even as binary
float32) for one discover, roughly doubled under `--all`. Embeddings are
computed ON THE ENGINE in every mode (the client uploads chunk text;
`http_vector_client.py:1582 upsert_chunks_with_embeddings` "forwards chunk
TEXT and ignores caller's embeddings"), so the download is pure waste: the
data never needed to leave the host.

At production corpus scale the taxonomy leg is again ~100% of flush-grain
hook cost (90.1 s of 90.2 s over 3 flushes, nexus-ap8l0, 2026-08-10) even on
the server-side assign path — an unexplained per-POST cost this RDR folds in
as a measurement obligation (an `EXPLAIN` of `assign_from_chashes_1024` at
~600 centroids / 1000 chashes) rather than a fresh guess.

No server-side clustering exists anywhere in `service/`; pgvector exposes no
k-means / HDBSCAN primitive (IVFFlat's build-time k-means is internal).

### Gap 3: Registration precedes chunkability, so unchunkable files become phantom catalog documents

Added 2026-08-15 (Hal; evidence from the nexus-3n7pr forensics, bead
nexus-rqsh1 P1). The client-side reconcile loop of Gap 1 registers a catalog
document for every file the extension filter admits, BEFORE the chunker
decides whether the file has any content it can chunk. Binary and data files
that slip the docs filter (XML-shaped VTK `.vtp`/`.vtu`, Gmsh `.msh`, voxel
`.vol`, `.jks`, `.stl`, `.bundle`, `.npz`) and zero-byte files (empty
`__init__.py`, empty `.md`) get a tumbler, an owner, a `physical_collection`
and `chunk_count 0`, then produce no chunks and no manifest, and nothing ever
retracts the row. Measured on the live store 2026-08-15: of the 910-document
"zero-manifest" population that drove weeks of forensics, roughly 600 were
this class (WeakAuras2 371 `docs__` rows, simc, searxng, t8code's 8 VTK
fixtures, Luciferase's 7 voxel/keystore files, dozens of empty `__init__.py`);
only the ~245 store_put-origin documents were real damage. Every census
(`nx catalog verify` never_chunked, `reconcile-stale` reindex_candidate, the
backfill dry-run) reported the phantoms as gaps, prescribed a re-index that
cannot change anything (`--force` re-chunks the repo and skips exactly the
phantom count, observed 8/7/2/1/2 across five repos), and every plan cycle
inherited them as data loss. "Register, then skip" is the classic
identity-before-content ordering bug.

What Part A must therefore also decide: the server-side reconcile registers a
document ONLY when the client presents at least one chunk for it (or the
engine's own chunkability decision admits it), never on file enumeration
alone; existing phantoms are swept (a counted, dry-run-first tombstone pass,
`zero_content_by_design` in the census output rather than a re-index
prescription); and the census surfaces (verify, reconcile-stale) stop
labelling zero-content sources as repairable. This is a correctness gap in
Gap 1's design, not a performance one, and it is the reason the migration to
server-side reconcile is the right place to fix it: the client loop cannot
know chunkability at registration time without doing the chunking, and the
engine can require the manifest and the document to arrive together.

## Constraints and Verified Facts

- **Everything is service mode.** Every install (local = bundled PG17+pgvector
  behind the native engine; cloud = managed service) reaches the catalog,
  T2 and T3 through the engine over HTTP (RDR-155/158/186). There is no
  in-process backend to keep a client-side path alive for. NO-SQLite
  directive: new persistent state goes to PG via Liquibase.
- **No silent fallbacks** (Hal directive; `assign_from_chashes` / nexus-yu9w5
  precedent): a route the engine lacks 404s and propagates; the client never
  recomputes locally. The engine floor (`REQUIRED_ENGINE_VERSION`) bumps in
  the same client release under the paired-release choreography.
- **All DDL through Liquibase; SQL functions are the established shape** —
  `catalog-004` manifest fns, `catalog-006/007/008/012` combined-query fns
  (per-dim triplicate, `CREATE OR REPLACE`, explicit GRANT re-issue),
  `catalog-020` fence, `catalog-023/024/027/028` `gc_*` procedures (thin
  Java wrappers in `VectorHandler`), `taxonomy-006` `assign_from_chashes`
  (VOLATILE plpgsql, SECURITY INVOKER, FORCE RLS scoping, data-modifying CTE).
- **Single-transaction batch repo methods exist:**
  `CatalogRepository.registerDocumentManyWithOutcome` (~:1220, N docs one txn,
  tumblers in input order via `claimNextSeq` :1565 on the owner's `next_seq`),
  `updateDocumentsMany` (~:1889, batched alias resolution + `ctx.batch`).
- **A `staging` schema exists** (`staging-001-landing-tables.xml`:
  `staging.chunks`, FORCE RLS, tenant_isolation policy) — the landing-table
  idiom for "upload rows cheaply, commit once".
- **Async job registry with 202+poll exists** (`http/RekeyJobs.java`,
  nexus-b878d): needed because the TLS proxy (`conexus-engine-tls` nginx
  `proxy_read_timeout` ~120 s) 504s any long synchronous request while the
  txn commits anyway — the GH #1390 hazard class. A discover over a large
  collection is a minutes-scale job and MUST NOT be a synchronous route.
- **The engine already carries the numeric/text substrate:** onnxruntime +
  DJL tokenizers (Seam B, local bge-768 embed), jOOQ, GraalVM native-image
  build with reachability metadata. Pure-Java clustering code (no reflection)
  is native-image safe by construction.
- **`miss_count` is stored in `catalog_documents.meta` jsonb**, not a column;
  rename detection keys on `meta->>'content_hash'`. Soft delete is
  `nexus.document_trash` (catalog-003).
- **Tumbler minting is Java-side** (`claimNextSeq` claims from
  `catalog_owners.next_seq` under the prevent-rather-than-catch stance for
  the address PK). A SQL reconcile either moves the claim into plpgsql
  (`SELECT … FOR UPDATE` on the owner row + increment) or receives
  pre-minted tumblers from Java — an implementation decision recorded in
  Phase 1, not a design fork.
- **Search-time taxonomy use is NOT the problem:** `cluster_by="semantic"`
  tries topic grouping first (batched reads) and falls back to Ward on the
  ≤`limit` result rows (`search_clusterer.py`); `apply_topic_boost` is
  batched. Out of scope. One adjacent defect to bead separately: the
  `topic="label"` prefilter (`search_engine.py` ~:421) downloads the label's
  full doc-id list unbounded although `search_topic_scoped_<dim>` exists.
- **Prior art in-repo:** RDR-156 (charter: "push work to the engine"),
  RDR-181 (server-side embed-skip), RDR-164 (server-side lifecycle cascades),
  RDR-191 (unified `nexus.chunks` + `nexus.taxonomy_centroids`, manifest FK,
  server-side GC that already retired the client-side prune fallback),
  RDR-070/075/077 (taxonomy origin; "not LLM-based topic naming at discover"
  is an explicit non-goal — the LLM relabel stays client-side).

## Decision

Move both pipelines onto the engine. State manipulation becomes one
transaction; SQL wherever SQL can express it; a Java job only where an
algorithm (clustering) has no SQL form. The client keeps exactly the work
that needs the local filesystem or the operator's LLM.

### Part A — Catalog: `reconcile` (stage → commit) replaces hook + housekeeping + link fan-out

**A1 Engine.** New changeset `catalog-032-reconcile.xml`:

1. `staging.catalog_reconcile(tenant_id, run_id, owner_tumbler, source_uri,
   file_path, physical_collection, content_hash, head_hash, source_mtime,
   meta jsonb, content_type, title, …)` — FORCE RLS, tenant_isolation
   policy, same shape as `staging.chunks`.
2. `nexus.catalog_reconcile_commit(p_owner text, p_run_id text)
   RETURNS TABLE(source_uri text, tumbler text, action text)` — plpgsql,
   VOLATILE, SECURITY INVOKER (RLS scopes every row), ONE transaction:
   - UPSERT `catalog_documents` from staging `ON CONFLICT (tenant_id, source_uri)
     [live-only identity `ux_catalog_documents_live_source_uri`, catalog-016]
     DO UPDATE … WHERE (head_hash, physical_collection, source_mtime,
     meta->>'content_hash') IS DISTINCT FROM (excluded.…)`. The Python
     change predicate becomes the `WHERE`. Because a WHERE-guarded
     `DO UPDATE` that does not fire emits nothing through `RETURNING`, the
     3-way `action` is produced in TWO steps: `WITH upserted AS (INSERT …
     RETURNING source_uri, tumbler, (xmax = 0) AS created) SELECT s.source_uri,
     COALESCE(u.tumbler, d.tumbler), CASE WHEN u.source_uri IS NULL THEN
     'unchanged' WHEN u.created THEN 'created' ELSE 'updated' END FROM staging
     s LEFT JOIN upserted u USING (source_uri) LEFT JOIN catalog_documents d …`.
     Unchanged rows are NOT rewritten and `indexed_at` does not move; changed
     rows refresh `indexed_at` under the existing head_hash-gated rule
     (nexus-927mo, `CatalogRepository.java:2082-2108`) which the function
     reproduces. The dst5h critique's "an always-true drift predicate
     silently restores the write storm" is the parity test.
   - Every `meta` write in the function (miss_count reset/increment, rename
     bookkeeping) uses `meta || jsonb_build_object(...)` / `jsonb_set`, NEVER
     a bare `SET meta = …` — the merge-not-replace contract of
     `updateDocument` (nexus-ke45f). A miss_count-only touch does NOT move
     `indexed_at`.
   - Housekeeping, set-based: owner docs absent from staging →
     `meta.miss_count + 1`; present → `0`; `≥ 2` → `nexus.document_trash`;
     rename = a `created` row whose `content_hash` equals a missing row's →
     `UPDATE catalog_links SET from/to_tumbler` onto the new tumbler
     (`ON CONFLICT` merge as `link_if_absent` does today) then trash the old.
     Tie-break is SYMMETRIC: among `created` candidates the lowest tumbler
     (numeric segment order) is the target, among `missing` candidates the
     lowest tumbler is the source, each missing row is consumed at most
     once, and any leftover missing rows fall through to the miss_count
     branch — a declared rule (Resolved Q2), tested with a multi-collision
     fixture (the live catalog carries 201 duplicated `source_uri`s per
     catalog-002, so many-to-one is not hypothetical).
     The two-run grace window semantics are unchanged (see
     `_prune_deleted_files`'s docstring — the manifest FK cascade + server-side
     GC already handle chunk cleanup).
   - Owner `head_hash` update (`_set_owner_head_hash` folds in).
   - `DELETE FROM staging.catalog_reconcile WHERE run_id = p_run_id`.
   - Tumbler minting for `created` rows happens INSIDE the function (owner-row
     `FOR UPDATE` + highest-child self-heal, the `claimNextSeq` contract in
     plpgsql — Resolved Q1). Rename tie-break: lowest tumbler by numeric
     segment order (declared rule — Resolved Q2).
   - `nexus.catalog_reconcile_reap(p_interval)` drops stale runs (newest
     stage row older than the interval, default 24 h); invoked at the start
     of every `stage` call for the tenant (Resolved Q9).
3. `nexus.catalog_links_upsert_by_path(p_owner text, p_edges jsonb)
   RETURNS TABLE(created int, skipped int, unresolved jsonb)` — resolves
   `file_path → tumbler` in SQL, `INSERT … ON CONFLICT DO NOTHING` (with the
   `co_discovered_by` merge), one txn. Kills link_generator's ~8 full-corpus
   lists and per-edge POSTs.

Routes (`CatalogHandler` switch + `CatalogRepository`, inside
`tenantScope.withTenant`). **`commit` is ASYNC** — the same TLS-proxy
reasoning that makes B2's discover a job applies to any route whose
duration scales with corpus size (GH #1390 class: a 504 while the txn
commits anyway). Phase 1 therefore generalises `RekeyJobs` into a shared
`http/EngineJobs<Envelope>` registry (per-tenant single-flight, epoch-fenced
ids, 202 + poll, 410 foreign-epoch, terminal retention) that `commit` uses
in Phase 1 and the discover job reuses in Phase 4. Sharing is of the CLASS,
not the state: ONE `EngineJobs` INSTANCE per job kind (`reconcileJobs`,
`discoverJobs`, the existing `rekeyJobs`), each with its own per-tenant
in-flight map, so a running catalog commit never 409-blocks a discover for
the same tenant (the RekeyJobs one-instance-per-kind precedent). `stage`
stays synchronous (bounded at 1000 rows). The client polls at ~1 s; one
extra round trip.

| Route | Body | Returns |
|---|---|---|
| `POST /v1/catalog/reconcile/stage` | `{owner, run_id, rows[≤1000]}` | `{staged}` |
| `POST /v1/catalog/reconcile/commit` | `{owner, run_id}` | `202 {job_id, poll}`; `409 {job_id}` if a commit for the tenant is running |
| `GET /v1/catalog/reconcile/{job_id}` | — | `200 {status: running}` \| `200 {status: succeeded, created:[{source_uri,tumbler}], updated:[…], unchanged_count, trashed:[…], renamed:[{old,new}], path_to_tumbler:{…}}` \| `200 {status: failed, error}` \| `410 {status: lost}` (foreign epoch — re-commit is idempotent and self-answering) |
| `POST /v1/catalog/links/upsert_many` | `{owner, edges:[{from_path,to_path,type,created_by,spans?}]}` | `{created, skipped, unresolved}` |
| `POST /v1/catalog/doc/resolve_or_register` | `{owner_name/curator, source_uri \| file_path, title, year?, title_fallback?}` | `{tumbler, created}` — `title`/`year` backfill via `COALESCE` on empty; GHOST-only title match when `title_fallback` (Resolved Q3) |

`path_to_tumbler` in the commit envelope replaces the frecency third
download. `MAX_BATCH_DOC_IDS = 1000` mirrored on stage; commit is one txn
regardless of staged count (a 20k-file repo = 20 stage calls + 1 commit +
polls). Perf obligation (Phase 1 gate, not assumed): a Java test drives a
synthetic 20k-row owner through stage → commit on the test substrate and
records commit wall time; the accept bar is < 30 s at 20k rows (set-based
SQL over indexed keys — expected single-digit seconds), and the async
envelope makes the TLS ceiling a latency fact rather than a correctness
hazard either way.

Java tests (testcontainers PG): unchanged → no row rewrite (`indexed_at`
unchanged); changed field → `updated` (+ `indexed_at` refreshed only under
the head_hash-gated rule); miss / second miss / trash; miss_count touch
preserves every other `meta` key (content_hash, bib fields, custom keys) and
leaves `indexed_at` alone; rename → links transferred; MULTI-collision
rename (2 missing × 2 created sharing one content_hash) resolves per the
symmetric lowest-tumbler rule; RLS cross-tenant isolation; staging cleared;
idempotent re-commit is a no-op; 20k-row perf gate; job envelope states
(running/succeeded/failed/409/410); parity: same fixture through the old
`update_many` path vs `commit` → identical row state.

**A2 Client.**

- `catalog/http_catalog_client.py`: `reconcile_stage`, `reconcile_commit`,
  `links_upsert_many`, `resolve_or_register` — added to
  `_SERVICE_ONLY_WRITE_OPS` (`catalog/factory.py` idiom; the capability check
  is honest and, since every mode is service mode, never false).
- `indexer.py` `_catalog_hook`: keep walk/stat/sha256/ephemeral/classify;
  replace the `by_owner` join + Python diff + `register_many`/`update_many`
  + per-file fallbacks with `stage(pages) → commit → poll`; feed `path_to_tumbler`
  to `_build_frecency_doc_id_map`; DELETE `_run_housekeeping` (its work is
  inside commit); `link_generator.py` keeps the regex scans, emits path
  edges, one `links_upsert_many` per generator.
- `doc_indexer.py` `_register_or_lookup_doc_id` + `_catalog_markdown_hook`
  → one `resolve_or_register` carrying `title`/`year`/`source_mtime`
  (`chunk_count` is already folded into `write_manifest_many` — nexus-u2kwq
  "chunk_count fold-in", `CatalogRepository.EX_DOC_CHUNKS`; the markdown
  hook's remaining writes are exactly title/year/indexed_at/source_mtime,
  doc_indexer.py ~2613-2660); the ephemeral-worktree guard stays
  client-side;
  `store_hook.py` same collapse (title-ghost matching moves into the route
  if it cannot be expressed otherwise).
- NO fallback to the old path.
- Job-envelope handling (client contract, shared with the B2 discover
  client): `409` → wait on the named running `job_id` (poll it, do NOT
  resubmit); `410 lost` → re-submit `commit` for the same `run_id`
  (idempotent: staged rows are still present if the first commit never
  ran, or the run is already committed and re-commit is a no-op) after
  logging `catalog_reconcile_job_lost`; `failed` → surface the error and
  exit non-zero, never continue the index run as if reconciled.
- Tests: `_catalog_hook` contract re-pinned in the dst5h style — commit
  exactly 1/run, stage = ceil(N/1000), ZERO single `update` / `register` /
  `delete` / `link` calls in a warm run; job-envelope contract test
  (409 waits, 410 re-submits once, failed aborts); service-mode contract
  test through `ensure_engine`.

Expected effect (warm 1930-file run, 0.1 s RTT floor): catalog phase from
3 full-list downloads + O(changed) pages + O(missing) serial writes → ~3
round trips; link phase from ~8 lists + O(edges) POSTs → O(generators)
POSTs; `nx index md` identity trips per file 6 → 1.

### Part B — Taxonomy on the engine

**B1 SQL wins (ship with Part A; no clustering involved).** New changeset
`taxonomy-008-serverside-projection-links.xml`:

- `nexus.taxonomy_project_collection_<dim>(p_src_collection, p_threshold)` —
  the whole-collection generalisation of the `assign_from_chashes` cross
  pass: `chunks ⋈ foreign centroids` argmax `≥ PROJECTION_THRESHOLD`, upsert
  `topic_assignments` (`assigned_by='projection'`, GREATEST semantics
  verbatim), one statement, zero egress. Replaces `project_against` and
  `_svc_fetch_all_embeddings`.
- `nexus.taxonomy_cross_links(p_collection, p_threshold)` — centroid ×
  foreign-centroid cosine via `<=>`, `INSERT topic_links ON CONFLICT`,
  returns count. Replaces `compute_cross_links` + the N+1
  `persist_cross_links`.
- Labeler batch routes: `POST /v1/taxonomy/topics/label_context_many
  {topic_ids, samples_per_topic}` (server picks sample texts via the
  manifest / `document_text` join) and `POST /v1/taxonomy/topics/update_label_many`
  (one txn; resolves nexus-ejo6k's duplicate-label 409 in-batch). The
  `claude -p` relabel stays client-side by design.
- Measurement obligation (nexus-ap8l0): `EXPLAIN (ANALYZE, BUFFERS)`
  `assign_from_chashes_1024` at ~600 centroids / 1000 chashes; verify the
  per-dim partial HNSW on the unified `nexus.taxonomy_centroids` is used by
  the cross pass; fix the plan before B2 if it is the 30 s/flush.

**B2 Engine discover job (Java, async).**

- `POST /v1/taxonomy/discover {collections:[…], force}` → `202 {job_id, poll}`;
  `GET /v1/taxonomy/discover/{job_id}`; `409` if a discover for the tenant is
  already running (RekeyJobs pattern verbatim, including the epoch-fenced
  "outcome unknown to this instance" answer).
- Job shape (Resolved Q6): ONE job per submit; collections processed
  sequentially, each in ITS OWN txn; poll envelope = per-collection result
  array `[{collection, status, topics, error}]`; per-tenant single-flight
  (409). Content guard INSIDE each collection's txn: if the collection
  already has topics and `force` is false the collection is reported
  `skipped_existing` and nothing is written — the server-side atomic
  backstop that today's `discover_skip_existing` preflight + persist-time
  guard provide (a duplicate submit or a client that forgot `force` cannot
  duplicate topics; concurrency is the 409's job, content is this guard's).
  `force=true` routes to the rebuild path (`_merge_labels` semantics).
- Job body (`taxonomy/TaxonomyDiscoverService`): per collection, JDBC-stream
  `chash, embedding_<dim>` from `nexus.chunks` (host-local, never
  materialised as JSON), cluster in Java, then in ONE transaction: `topics`
  insert, `topic_assignments` insert (`assigned_by='hdbscan' | 'kmeans'`),
  `taxonomy_centroids` upsert, `taxonomy_meta` record, then
  `taxonomy_cross_links` (B1). Rebuild's `_merge_labels` (old→new centroid
  cosine argmax; transfer operator labels + `accepted`) becomes a SQL step
  over the two centroid sets. Split = k=2 over one topic's chunks through
  the same code path.
- **Clustering: port HDBSCAN + k-means to Java** (DECIDED, Hal 2026-08-15):
  HDBSCAN for n ≤ 5000 with the current parameters (mutual reachability →
  Prim MST, O(n²) is fine at 5000 → single-linkage → condensed tree → EOM
  selection with the nexus-9b9oi `max_cluster_size` cap → centroid centres)
  and k-means++ / MiniBatch for n > 5000 (`k = max(10, √n/3)`, seed 42).
  Behaviour-preserving; native-image friendly. Ships with its own
  `taxonomy/VectorMath` (no reusable Java vector utility exists — Resolved
  Q8; ~1.3-1.5k LOC Java total before tests). Parity gate = the
  `hdbscan_rootgrab_400x1024_f16.npz` fixture (Resolved Q5).
  Rejected: k-means-only (changes small-collection taxonomy shape, forces
  re-discover on upgrade), engine-hosted Python worker (contradicts the
  native-binary engine deliverable).
- c-TF-IDF labels via SQL: `to_tsvector('english', text)` over each
  cluster's chunk texts, class-based tf-idf as one `GROUP BY (cluster,
  lexeme)` with cross-cluster document frequency, top-3 per cluster.
  Rank by stem, display the modal surface form (`ts_lexize('english_stem',
  word)` grouping). Spike-verified against sklearn on 8 proxy clusters
  (Resolved Q4, research-12): same top term 8/8. No Java tokenizer.
- Client: `discover_for_collection` → submit + poll + summary, with the same
  409-wait / 410-resubmit-once / failed-abort envelope handling as the A2
  reconcile client (post-index auto-discover keeps its per-collection
  failure isolation: a `failed` collection entry is logged and the loop
  continues); DELETE
  `_fetch_service_vectors`, `_discover_via_service`, `compute_discovered_topics`,
  `compute_rebuild_plan`, `compute_split`, `_cluster`, `_merge_labels`
  (`taxonomy_compute.py`), `_svc_fetch_all_embeddings`, `project_against`,
  `compute_cross_links`, `persist_cross_links`. Post-index auto-discover
  submits the job and reports; `nx taxonomy discover` waits. NO fallback.
- Tests: seeded deterministic Java clustering unit tests (labels stable,
  size cap enforced, noise handled); discover-job integration (testcontainers:
  seeded chunks → topics/assignments/centroids/meta rows in ONE txn, poll
  states, 409 on concurrent); RLS isolation; Python contract test: `nx
  taxonomy discover` against `ensure_engine` asserts ZERO `/vectors/get`,
  `/get-embeddings`, `/centroids/foreign` calls (the yu9w5 route-verification
  grep, made a test).

### Sequencing (DECIDED: A first, then B2)

1. **Phase 1 — A1 engine half** (changeset + repo + `EngineJobs` + handler +
   Java tests incl. the 20k-row commit perf gate).
2. **Phase 2 — A2 client half** (switch + deletion + contract tests) +
   warm-index perf probe (`service_catalog_op_stats`).
3. **Phase 3 — B1** SQL functions + labeler batch routes + ap8l0 EXPLAIN.
   Ships in the same engine cut as Phase 1.
4. **Engine cut #1** via the `engine-release` skill — migration-rehearsal
   `--candidate-migration` MANDATORY (db/changelog cut); floor bump in the
   same client release; deploy at client-tag push (paired-release
   choreography).
5. **Phase 4 — B2 engine job** (Java clustering port + discover service +
   routes + tests).
6. **Phase 5 — B2 client half** (submit/poll, deletions, contract test) +
   discover perf probe.
7. **Engine cut #2**, same gates.

Each phase: engine half + client half, dual-reviewed
(`/conexus:review-code` then `/conexus:substantive-critique`),
`/conexus:phase-review-gate` at the boundary.

## Alternatives Considered

- **Keep batching the client path** (another `_many` route, bigger pages,
  memoised owner list). Rejected: it is what 2026-07/08 already did; each
  step bought a constant factor and left three full-corpus downloads, a
  serial housekeeping loop, and no batch link route. The residual cost is
  round-trip COUNT (measured), and count only goes to O(1) when the reconcile
  logic moves to where the rows are.
- **Sync discover route / sync reconcile commit.** Rejected (commit made
  async at gate run 1): the TLS proxy 120 s timeout makes any request whose
  duration scales with corpus size commit-after-504 (GH #1390 class);
  RekeyJobs exists precisely because of it, and is generalised into
  `EngineJobs` in Phase 1 so both routes share it.
- **k-means-only server-side clustering.** Rejected (Hal): behaviour change
  for small collections, forced re-discover, loses automatic k.
- **Engine-hosted Python worker for clustering.** Rejected (Hal): puts
  Python on the engine host; the engine is a native binary by design
  (RDR-157/161).
- **Client-side clustering over server-streamed binary embeddings.** Would
  cut egress ~3x (JSON → float32) but leaves the round trips, the local
  numpy working set, and the `--all` double download. Not the design level.

## Consequences

- Positive: warm `nx index repo` catalog phase from O(files) round trips to
  O(1); `nx index md` / `store put` identity trips 6 → 1; discover CLUSTERING
  egress from ~GBs to ~0 and round trips from ~740 to a submit + polls (the
  LLM relabel still pulls per-topic sample texts by design — bounded, ~KBs
  per topic via `label_context_many`, RDR-070 non-goal preserved); the
  client sheds the reconcile state machine, sklearn/HDBSCAN from the
  discover path, and the centroid-cache staleness class named at
  `http_centroid_store.py:199-218`.
- Negative / cost: two engine cuts; a Java HDBSCAN port (the single largest
  item) with a parity obligation against the sklearn output; the
  `staging.catalog_reconcile` table and job registry are new operational
  surfaces (stale run_id rows are reaped by `catalog_reconcile_reap` at every `stage` call — Resolved Q9).
- Migration: none for data — the new procedures operate on existing tables.
  Existing ENGINE routes (`register_many`, `update_many`, per-row `update`/
  `delete`/`link`, `centroids/foreign`) remain served for other callers
  until a later retirement RDR; the nexus CLIENT (indexer, taxonomy CLI,
  store_put hook) stops calling them unconditionally. Version skew: a new
  client against an old engine 404s on the new routes and fails loud (no
  fallback); an old client against the new engine keeps working on the
  retained routes. The floor bump ships in the same client release as each
  engine cut (paired-release choreography), so no released client ever
  targets an engine below its own floor.
- Removes: `_run_housekeeping`, the Python change predicate, link_generator's
  list downloads, `_svc_fetch_all_embeddings`, `project_against`,
  `compute_cross_links`, `persist_cross_links`, `taxonomy_compute.py`'s
  clustering half.

## Resolved Questions (pinned 2026-08-15; research passes 1-2, T2 `nexus_rdr/193-research-1..13`)

Every question raised at draft time is now either resolved from the code, a
declared decision, or a spike scheduled at a named phase with a named gate.
Nothing here blocks the gate.

1. **Tumbler minting inside `catalog_reconcile_commit` — RESOLVED (research-1):**
   the claim moves into plpgsql. `claimNextSeq` is `SELECT … FOR UPDATE` on the
   owner row + `max(next_seq, highest_child_seq)+1` self-heal + `UPDATE`, one
   txn, no Java-side state; the SQL function reimplements the highest-child
   self-heal and a constraint-retry loop inline.
2. **Rename precedence — DECIDED as a DECLARED BEHAVIOUR CHANGE (research-2):**
   today's rule is last-write-wins over `by_owner`'s lexicographic
   `ORDER BY tumbler` — an accident. The SQL pins ONE rule: among `created`
   rows sharing a `content_hash` with a missing row, the lowest tumbler by
   numeric segment order wins; symmetrically the lowest-tumbler MISSING row
   is the rename source, each consumed at most once, leftovers fall to the
   miss_count branch; documented in the changeset header and tested with a
   multi-collision fixture.
   Link transfer reuses the engine's `ON CONFLICT` + `co_discovered_by` merge
   on `(tenant_id, from_tumbler, to_tumbler, link_type)`.
3. **`resolve_or_register` scope — RESOLVED (research-3):** keyed on
   `(owner, source_uri)` with `title_fallback` for the GHOST-only third-tier
   match; body carries `title`/`year` backfill (`COALESCE` on empty); the
   ephemeral-worktree guard stays client-side (filesystem predicate).
4. **c-TF-IDF via `to_tsvector` — RESOLVED BY SPIKE (research-6, research-12):**
   same top term as sklearn in 8/8 proxy clusters, top-10 prefix overlap
   4-8/10; rank by stem, display the modal surface form via
   `ts_lexize('english_stem', word)` (verified in SQL). No Java tokenizer.
   No downstream consumer depends on tokenisation.
5. **HDBSCAN parity gate — DECIDED (research-5, research-13):** fixture
   `tests/fixtures/hdbscan_rootgrab_400x1024_f16.npz` (real geometry; synthetic
   blobs do not reproduce eom root-grab). Java port must reproduce: capped run
   → 16 clusters, largest 40; uncapped run → 2 clusters [359, 23]; ARI ≥ 0.90
   vs sklearn labels (floor for tie-order; ~1.0 expected). Plus `_merge_labels`
   (greedy descending-similarity bipartite claim, threshold 0.8) and
   `compute_split` (KMeans n_init=10, seed 42) reproduced exactly on seeded
   fixtures. Noise chunks get no assignment row at discover — kept.
6. **Discover job shape — DECIDED (authorial call informed by research-8,
   which established RekeyJobs' single-outcome envelope does not settle it):**
   ONE job per submit; existing-topics content guard inside each collection's
   txn (`skipped_existing` unless `force`);
   `collections[]` processed sequentially, each collection ITS OWN txn
   (topics + assignments + centroids + `taxonomy_meta`/`record_discover_count`
   + cross-links); the poll envelope carries a per-collection result array
   `[{collection, status: pending|running|succeeded|failed, topics, error}]`
   so an engine death mid-job leaves a truthful partial record; per-tenant
   single-flight (409), epoch-fenced 410 semantics carried verbatim from
   RekeyJobs. `record_discover_count` moves inside the collection's txn.
7. **`assign_batch` / `compute_assignments` — RESOLVED (research-7):** zero
   production callers; B1 retires the CLIENT methods `assign_batch`,
   `compute_assignments`, `project_against`, `_svc_fetch_all_embeddings`,
   `compute_cross_links`, `persist_cross_links`, and the client's use of the
   `centroids/foreign` route (the engine-side HTTP route itself is retained
   until a later retirement RDR — see Consequences). Re-grep at Phase 3
   planning.
8. **Vector math — DECIDED (research-10):** no reusable Java utility exists;
   B2 ships a small `taxonomy/VectorMath` (dot, norm, cosine, pairwise
   distance, mean) alongside `Hdbscan`, `KMeans`, `LabelMerge`. Sizing:
   HDBSCAN ~500-700 LOC, k-means++/MiniBatch ~150-200, merge/split ~100,
   vector math ~100, discover service + jobs + handler ~400 — ~1.3-1.5k LOC
   Java before tests. Estimate revisited at Phase 4 planning against the
   actual port.
9. **Staging reaper — DECIDED (research-4):** `catalog_reconcile_commit`
   deletes its own `run_id` rows; `nexus.catalog_reconcile_reap(interval)`
   drops runs whose newest stage row is older than the interval (default
   24 h) and is invoked at the start of every `stage` call for the same
   tenant — self-cleaning, no scheduler.
10. **nexus-ap8l0 cause — SPIKE AT PHASE 3 (research-11):** `EXPLAIN (ANALYZE,
    BUFFERS)` of `assign_from_chashes_1024` at ~600 centroids / 1000 chashes
    against a populated tenant; gate: cross pass uses the per-dim partial
    HNSW on unified centroids. Not assumed either way.

## Phasing (draft)

| Phase | Deliverable | Beads |
|---|---|---|
| 1 | A1: catalog-032 changeset, `catalog_reconcile_commit` (+ `_reap`), `catalog_links_upsert_by_path`, `EngineJobs` registry (RekeyJobs generalised), 5 routes incl. commit poll, Java tests incl. 20k-row perf gate | epic + 1-2 |
| 2 | A2: client switch, deletions, contract tests, warm-index perf probe | 1-2 |
| 3 | B1: taxonomy-008 changeset, project/cross-links fns, labeler batch routes, ap8l0 EXPLAIN | 1-2 |
| — | Engine cut #1 + paired client release | release beads |
| 4 | B2 engine: Java HDBSCAN + k-means, `TaxonomyDiscoverService`, job routes, tests | 2-3 |
| 5 | B2 client: submit/poll, deletions, contract test, discover perf probe | 1 |
| — | Engine cut #2 + paired client release | release beads |

Beads to be created at accept; not before (RDR-024 guardrail).

## Research Findings

### Key Discoveries

- **✅ Verified** (source search) — Tumbler minting can move into plpgsql: `claimNextSeq` = owner-row `FOR UPDATE` + highest-child self-heal + `UPDATE`, one txn, no Java-only state. *Source: CatalogRepository.java:1037,1080-1083,1121-1133,1565-1575 (research-1)*
- **✅ Verified** (source search) — Rename precedence today is last-write-wins over `by_owner`'s lexicographic `ORDER BY tumbler`; the SQL rule is a declared behaviour change. `catalog_links` unique key `(tenant_id, from_tumbler, to_tumbler, link_type)` with an existing ON CONFLICT merge. *Source: indexer.py:1549-1600; CatalogRepository.java:2905-2912 (research-2)*
- **✅ Verified** (source search) — store_put URI is `chroma://<collection>/<title>`; `_find_ghost_by_title` is a GHOST-only third tier; `_catalog_markdown_hook` also backfills title/year and applies an ephemeral-worktree guard. *Source: store_hook.py:126,180; doc_indexer.py:565,2583 (research-3)*
- **✅ Verified** (source search) — Line refs: `update_many` at indexer.py:1250; `writeManifestMany` body/loop/withTenant at CatalogRepository.java:4351/4365/4388. Staging: 300-row batch load, FORCE RLS, no reaper. `miss_count` is meta-jsonb-only; meta merge is `||`. *Source: StagingHandler.java:245-310; staging-001 (research-4)*
- **✅ Verified** (source search) — Exact sklearn surface the Java port must reproduce (params, cap, eom, centroid store, MiniBatch branch, `_merge_labels` greedy algorithm @0.8, `compute_split` KMeans n_init=10) and what is incidental. *Source: taxonomy_compute.py:50,153-422 (research-5)*
- **✅ Verified** (source search) — Nothing downstream depends on c-TF-IDF tokenisation; `terms` feed only the LLM relabel prompt. *Source: taxonomy_cmd.py:1782-1788; http_taxonomy_store.py:1559-1581 (research-6)*
- **✅ Verified** (source search) — `assign_batch`/`compute_assignments` have zero production callers; live B1 targets are `project_against` (4 call sites) and the cross-links pair. *Source: grep 2026-08-15; http_taxonomy_store.py:1071-1083,1414-1446; taxonomy_cmd.py:188,2027,2167; commands/index.py:1126 (research-7)*
- **✅ Verified** (source search) — RekeyJobs wiring/route/epoch/409/retention shape; envelope is single-outcome; purge-trash VACUUM is fire-and-forget and NOT a template. *Source: RekeyJobs.java, RemapHandler.java, NexusService.java:323-326,462 (research-8)*
- **✅ Verified** (source search) — Discover CLI semantics to preserve (`--all` enumeration, `local_exclude_collections=["code__*"]` at config.py:969, preflight guard, `--force`→rebuild, `record_discover_count`, synchronous per-collection post-index auto-discover with failure isolation). *Source: taxonomy_cmd.py:41-65,294-314,596-629; commands/index.py:195-210,1289-1304 (research-9)*
- **✅ Verified** (source search) — Pure-Java clustering needs no native-image metadata; NO existing Java vector-math utility to reuse. *Source: pom.xml:115-121; TaxonomyCentroidRepository.java:123-131,390 (research-10)*
- **✅ Verified** (spike, PG 17.5 bundled) — `to_tsvector('english')` class-TF-IDF reproduces sklearn's top term in 8/8 proxy clusters (top-10 prefix overlap 4-8/10); stems display as modal surface form via `ts_lexize`. *Source: scratchpad tsvector_spike.py (research-12)*
- **✅ Verified** (source search) — Parity fixture exists: `tests/fixtures/hdbscan_rootgrab_400x1024_f16.npz` (capped → 16 clusters/largest 40; uncapped → 2 [359,23]). *Source: tests/fixtures/PROVENANCE-hdbscan-rootgrab.md (research-13)*
- **❓ Assumed** (spike, Phase 3) — ap8l0's 30 s/flush is a plan/HNSW-usage issue in the `assign_from_chashes` cross pass; ARI ≥ 0.9 floor holds on the fixture. *(research-11, narrowed by research-12/13)*

Measured baselines to compare against: T2 [21715] (7.4.0 flush-tail),
[21723] (round-trip floor + lock convoy), nexus-ap8l0 (production-scale
taxonomy leg 90 s / 3 flushes; prune-deleted-files 547 s — the latter already
addressed server-side by RDR-191 Phase 6).
