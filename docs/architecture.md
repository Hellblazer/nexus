# Architecture

> When in doubt, check `src/nexus/` -- the code is the ground truth.

## Reference Architecture

Four layers: queries come in at the top, get decomposed into plans, executed as a DAG of operators, backed by a catalog-aware knowledge graph. Modeled on the AgenticScholar four-layer reference architecture.

![Four-layer Nexus reference architecture: horizontal bands for Application, Planning, Execution, and Knowledge Representation, with labeled data flow from queries through plans to operators over a catalog-backed knowledge graph.](architecture-diagram.svg)

<details>
<summary>Detailed description of the diagram</summary>

The diagram shows four horizontal colored bands stacked vertically, each labeled in its upper-left corner and representing one layer of the Nexus architecture.

The top band (blue, "Application Layer") contains three side-by-side white boxes representing query categories that enter the system: Retrieval Queries (`nx search`, `search` MCP, `nx memory`); Extraction and Synthesis Queries (`query` MCP, `operator_extract`, `summarize`, `compare`); and Knowledge Discovery and Generation (`nx_answer`, `operator_generate`, `/conexus:analyze`).

The second band (peach, "LLM-Centric Hybrid Planning Layer") is the tallest. On its left edge, a small stack-of-documents icon labeled "Scholarly Queries" feeds horizontally into a Query Decomposer box (`/conexus:query`, `/conexus:plan-first`). A "Task" arrow branches upward and rightward into two parallel dashed-border subgroups: "Predefined Plan Selection" (containing `plan_match` with dimension and semantic rerank, an LLM-based rerank step, and a small PlanLibrary cylinder) and "Dynamic Plan Generator" (three stacked stages: High-level planning, Low-level operator instantiation, and Validation and self-correction). A horizontal dashed "miss" arrow connects Selection to Generator as a fallback.

Three arrows cross downward from the Planning band into the third band: a dashed "Scope" arrow directly below the Query Decomposer, a dashed "matched" arrow below the Predefined Plan Selection, and a solid "Execution Plan" arrow below the Dynamic Plan Generator on the right.

The third band (green, "Unified Execution Layer") contains, left to right: a cluster of four colored hexagons connected by lines representing the Execution Plan DAG; an Execution Engine subgroup containing a `plan_run` panel (with a miniature DAG glyph) and a Result Cache cylinder labeled T1, connected by a bidirectional arrow; and a Defined Operator Set box divided into three labeled columns — RETRIEVAL (Search, Query, Traverse, FindNode, Filter, GroupBy), SYNTHESIS (Extract, Summarize, Compare, Rank, Generate, Aggregate), and STATE (`memory_*`, `store_*`, `plan_*`, `scratch_*`, `catalog_link`, `operator_*`).

The fourth band (purple, "Knowledge Representation Layer") flows left to right: a stack-of-documents icon labeled "Source Documents" feeds an Inner-document Content Extractor (classifier, chunker via tree-sitter across 31 languages, `code_indexer`, `prose_indexer`, `pdf_extractor` routing Docling → MinerU → PyMuPDF, `bib_enricher`). An arrow labeled "Scholarly Document Knowledge" continues into Problem/Method Taxonomy Construction (`CatalogTaxonomy`, BERTopic plus HDBSCAN). Below Taxonomy, a Progressive Update box (`auto_linker`, `taxonomy_assign_hook`, `link_generator`) connects bidirectionally upward and receives a dashed "new documents" arrow from Source Documents. A "construct" arrow leads right from Taxonomy to the Nexus Knowledge Graph — rendered as a node-link cluster of orange and white circles — representing the three-tier store (T1 ChromaDB, T2 SQLite+FTS5, T3 ChromaDB Cloud) with tumbler addresses and typed links (`cites`, `implements`, `supersedes`, `relates`).
</details>

Source: [`architecture-diagram.svg`](architecture-diagram.svg) — edit the SVG directly, then re-render the PNG with `rsvg-convert -z 1.5 docs/architecture-diagram.svg -o docs/architecture-diagram.png`.

## How It Fits Together

Nexus has three layers: a CLI (for humans) and an MCP server (for agents) that
talk to three storage tiers, an indexing pipeline that fills them, and a search
engine that queries across them.

```
Human                   Agent (Claude Code)
  │                         │
  ▼                         ▼
CLI (cli.py)            MCP Server (mcp_server.py)
  │                         │
  └──────────┬──────────────┘
             │
    ├── Index: classify → chunk → embed → store
    │     code: classify(SKIP|CODE|PROSE|PDF) → tree-sitter AST → context prefix → voyage-code-3 → code__<repo>
    │     prose: SemanticMarkdownChunker (md) or line-split → voyage-context-3 → docs__<repo>
    │     rdr:   SemanticMarkdownChunker → voyage-context-3 → rdr__<repo>
    │     pdf:   auto-detect routing (Docling → MinerU → PyMuPDF) → table/formula detection → bib enrichment → voyage-context-3 → docs__<corpus>
    │     skip:  .xml/.json/.yml/.html/.css/.lock/etc → silently ignored
    │
    ├── Search: query → retrieve → rerank → topic-boost → group → format
    │     semantic, hybrid (+ frecency + ripgrep)
    │     topic boost: same-topic -0.1, linked-topic -0.05 distance adjustment
    │     topic grouping: T2 assignments (>50% coverage) → fallback Ward clustering
    │
    ├── Taxonomy: T3 embeddings → HDBSCAN → T2 topics → centroid ANN → incremental assign
    │     discover: nx index repo (auto) or nx taxonomy discover (manual)
    │     assign: taxonomy_assign_hook fires on every store_put
    │     boost+group: search_engine.py reads db.taxonomy per search call
    │
    ├── Catalog: JSONL truth → SQLite cache → typed link graph
    │     documents: tumbler addressing (1.owner.doc), FTS5 search
    │     links: cites, implements-heuristic, supersedes, relates, formalizes
    │     auto-generate: citation links (bib metadata), code-RDR (heuristic)
    │     surfaces: MCP nexus-catalog server (10 tools) + nx catalog CLI
    │

    └── Storage tiers (RDR-120 substrate split, daemon-mediated)
          T1: ChromaDB HTTP server (session scratch, shared across agent processes)
          T2: SQLite + FTS5 daemon ── nx daemon t2 start
                Eight domain stores behind T2Database / T2Client
                Transport: UDS (UID-gated) + 127.0.0.1 loopback TCP
                memory · plans · chash_index · taxonomy · telemetry ·
                document_aspects · aspect_queue · catalog
          T3: ChromaDB daemon ── nx daemon t3 start  (local mode only)
              OR ChromaDB Cloud + Voyage AI (cloud, higher quality;
                                              daemon does not apply)
                code__*       voyage-code-3 index + query
                docs__*       voyage-context-3 (CCE) index + query
                rdr__*        voyage-context-3 (CCE) index + query
                knowledge__*  voyage-context-3 (CCE) index + query
```

**Daemon-mediated storage (RDR-120, 4.34.0+).** Local mode now
requires the T2 + T3 daemons to be running. Cloud mode is
unaffected (CloudClient is already HTTP-served). The previous
``NX_STORAGE_MODE=direct`` flag is honoured-as-daemon with a
``DeprecationWarning`` for one release; the env-var itself is
removed in the release after.

For container deployments (Claude Co-Work and similar): containers
reach the host's daemon via the loopback TCP socket exposed by
``nx daemon t2 status``. Pattern:

```
# macOS Docker Desktop:
docker run --rm \
    -e NX_T2_ADDR=host.docker.internal:<port> \
    -e NX_T3_ADDR=host.docker.internal:<t3_port> \
    <image-with-conexus>

# Linux (default bridge):
docker run --rm \
    --add-host=host.docker.internal:host-gateway \
    -e NX_T2_ADDR=host.docker.internal:<port> \
    <image>
```

UDS-mount works on native Linux Docker (validated by nexus-3d1ph
MVV) but NOT through Docker Desktop's macOS/Windows VM file-
sharing layer (returns ``ENOTSUP``); use the TCP path when the
host is macOS or Windows.

For the full container-integration story (operator-facing setup,
Claude Cowork SDK transport, diagnostic recipes, failure-mode
table) see [`docs/container-integration.md`](container-integration.md).

Data flows upward (T1 → T2 → T3).

## Catalog & Link Graph

The catalog is a document registry that sits alongside T3. While T3 stores document
*content* as vector embeddings, the catalog stores document *metadata* (title, author,
collection, tumbler address) and *relationships* (citations, implementations, supersedes).

**What populates it**: Indexing (`nx index repo`, `nx index pdf`, `nx index rdr`) auto-registers
entries via catalog hooks. MCP `store_put` also registers entries. `nx enrich` adds bibliographic
metadata and enables citation link generation.

**What agents use it for**: Finding which T3 collection a paper is in (`catalog_search` →
`physical_collection`), traversing citations (`catalog_links` with `link_type="cites"`),
and scoping semantic search to relevant collections instead of searching everything.

**Link types in use**:
- `cites` -- citation relationships (auto-created by `nx enrich` from Semantic Scholar references)
- `implements-heuristic` -- code→RDR links (auto-created by indexer from title substring matching)
- `supersedes` -- created by RDR close and knowledge-tidier when documents are replaced
- `relates` -- created by agents (debugger, deep-analyst, codebase-analyzer) linking related findings
- `implements`, `quotes`, `comments` -- available for manual use

**Span formats** for sub-document link references:
- `42-57` -- line range (positional, may become stale on re-index)
- `3:100-250` -- chunk:char range (positional)
- `chash:<sha256hex>` -- content-addressed chunk identity (preferred, survives re-indexing)
- `chash:<sha256hex>:<start>-<end>` -- character range within a content-addressed chunk

Content-hash spans reference chunks by `chunk_text_hash` metadata (SHA-256 of stored chunk text). All 5 indexers (code, prose, doc PDF, doc markdown, streaming PDF pipeline) emit `chunk_text_hash` alongside the existing file-level `content_hash`. For existing collections, `nx catalog setup` or `nx collection backfill-hash` adds the field without re-embedding. `link_audit()` verifies chash spans resolve in T3.

### Metadata field semantics (chunk vs document level)

Two hash fields look similar but mean very different things. Confusing them produces false-positive panic findings (e.g. "94% redundancy across the corpus" turns out to be 94% of chunks share a doc-level hash, which is correct: every chunk of one paper has the same `content_hash`). The table below locks the contract; consult before drawing conclusions from a metadata distribution.

| Metadata field | Level | Keyed on | Set by | Used for |
|---|---|---|---|---|
| `content_hash` | document | `sha256(file_bytes)` | every indexer at register time (`indexer.py:1198`) | document-level dedup; staleness comparison; backup-snapshot identity |
| `chunk_text_hash` | chunk | `sha256(chunk_text)` (full 64 chars) | every indexer per chunk; backfilled by `nx collection backfill-hash` | content-addressed link spans (`chash:<hex>`); `nx t3 reidentify` natural-ID source (first 32 chars); cross-collection chunk dedup |
| `chunk_text_hash[:32]` | chunk | first 32 chars of the SHA | `nx t3 reidentify` upsert (RDR-108 Phase 2) | Chroma natural ID for the chunk; the join key from `document_chunks.chash` |
| `source_uri` | document | `file://...` or `x-devonthink-item://<uuid>` etc. | indexer / MCP write paths | persistent URI identity; aspect-extraction routing; audit-membership home detection |
| `source_path` | document | absolute or repo-relative file path | indexer | display + grep targets; legacy path predating `source_uri` |
| `chunk_start_char` / `chunk_end_char` | chunk | char offsets in the source file | indexer per chunk | `chunk:char` span resolution; UI highlight |
| `section_title` / `section_type` | chunk | tree-sitter / Markdown section header | code/prose chunkers | search-time filtering (`section_type!=references`) |
| `embedding_model` | document | model id string | every write through `T3Database` | `voyage-code-3` vs `voyage-context-3` routing; quota validation |

`doc_id`, `chunk_index`, and `chunk_count` were ALSO chunk-level metadata pre-RDR-108. RDR-108 Phase 3 retired them; the catalog `document_chunks` manifest is the single source of truth for chunk position within a document. Read paths that need chunk order consult `Catalog.get_manifest(doc_id)` (see `_attach_doc_ids_from_catalog` in `search_engine.py` for the standard fallback).

Legacy fields (`corpus`, `store_type`, `extraction_method`, `expires_at`) were dropped in RDR-101 Phase 5c. They are not present in current writes; older collections still carry them as cargo until `nx t3 reidentify` runs the canonical-schema funnel and normalizes them away.

For operator runbooks built on this vocabulary see [`docs/operations/t3-health.md`](operations/t3-health.md) (when `nx catalog doctor` reports X) and [`docs/operations/audit-membership-interpretation.md`](operations/audit-membership-interpretation.md) (the 3 contamination axes).

### Catalog manifest as authoritative doc structure (RDR-108)

The catalog `document_chunks` table is the authoritative graph layer of the git/IPFS-style blob+tree split that addresses document identity:

- `documents` carries the Document graph node (tumbler-addressable).
- `document_chunks` records the ordered `(doc_id, position) -> chash` references that compose each Document.
- T3 stores chunks as content-addressed blobs; the chunk's Chroma natural ID is `sha256(chunk_text)[:32]`.

Schema:

```sql
CREATE TABLE document_chunks (
    doc_id      TEXT NOT NULL REFERENCES documents(tumbler) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    chash       TEXT NOT NULL,
    chunk_index INTEGER,
    line_start  INTEGER,
    line_end    INTEGER,
    char_start  INTEGER,
    char_end    INTEGER,
    PRIMARY KEY (doc_id, position)
);
CREATE INDEX idx_document_chunks_chash ON document_chunks(chash);
```

Properties:

- **Identical content collapses to one T3 row.** Two documents that contain the same chunk text in the same collection share one chunk in T3; the manifest records position separately for each. This is a design goal of D1 (no duplicate embeddings, no duplicate vector storage).
- **Re-indexing is idempotent at the chunk layer.** Re-indexing the same content produces the same Chroma natural ID; the upsert is a no-op. Manifest rows reflect the latest indexed structure.
- **Document deletion cascades to manifest, then to T3 via GC.** `cat.delete_document(tumbler)` removes the document row; FK `ON DELETE CASCADE` drops the manifest rows; the manifest-based GC (`indexer._prune_deleted_files`) sweeps the orphaned T3 chunks on the next `nx index` run after the document is evicted. Note: when the trigger is a deleted source file (rather than a direct `delete_document` call), `_run_housekeeping` waits for `miss_count >= 2` before evicting -- a one-run rename-detection grace window. T3 cleanup of the orphaned chunks therefore lands on the **second** `nx index` after the file disappears, not the first. One-run latency on cleanup, never on correctness.
- **Position is in the manifest, not in chunk metadata.** Phase 3 (RDR-108) retired `doc_id`, `chunk_index`, and `chunk_count` from chunk metadata; the manifest is the single source of truth for chunk position within a document. Retrieval call sites that need order (e.g. `synthesizer.py`'s ChunkIndexed event emission) consult `Catalog.get_manifest(doc_id)`.

**Catalog read API for the manifest**:
- `Catalog.get_manifest(doc_id) -> list[ManifestRow]` -- ordered rows.
- `Catalog.get_chunk_chashes(doc_id) -> list[str]` -- ordered chash sequence.
- `Catalog.docs_for_chashes(chashes) -> dict[chash, list[doc_id]]` -- reverse map; one chash can map to multiple docs (identical content shared).
- `Catalog.chashes_for_collection(physical_collection) -> set[str]` -- chash[:32] set referenced by the manifest for this collection. Used by GC to identify orphan T3 chunks.

**ChashIndex (T2 routing table)**: `chash_index` maps `(chash, physical_collection)` to enable global `chash:<hex>` link resolution without scanning every collection. Post-RDR-108 it is a pure routing table (chash + collection + created_at, no denormalized chunk_chroma_id). Stale rows (collection no longer exists in T3) are self-healed by `Catalog.resolve_chash` on access. A bulk reconcile sweep is on the post-Phase-4 follow-up backlog.

### Migration runbook (RDR-108 Phase 4 -> Phase 5)

For operators upgrading an existing nexus deployment to the post-RDR-108 storage shape:

1. **Deploy the new code + restart the MCP server** so the indexer / GC / retrieval paths use the manifest-aware code paths.
2. **`nx collection backfill-hash --all`** . Adds `chunk_text_hash` to any chunk that lacks it (pre-RDR-053 collections). Two-pass walk: pass 1 collects ids with a lightweight `include=[]` payload, pass 2 fetches by exact id and re-upserts with canonical-schema-normalized metadata. The two-pass design sidesteps ChromaDB Cloud's offset-pagination instability (a naive offset+update loop misses chunks); the canonical-normalize step drops legacy cargo so chunks with 32+ metadata keys land back under the per-row `NumMetadataKeys` quota. Idempotent; chunks that already have the hash short-circuit.
3. **`nx t3 reidentify --all-collections --dry-run --max-workers 4`** . Preview what will migrate. Should return `0 error(s)`. Errors at this stage typically mean a collection still has chunks without `chunk_text_hash`; re-run step 2 on the named collection or re-index from source.
4. **`nx t3 reidentify --all-collections --no-dry-run --max-workers 4`** . Actual migration. Each collection is re-upserted under content-derived natural IDs (`chunk_text_hash[:32]`); old IDs are batch-deleted. The `--max-workers` flag parallelizes across collections (default 4); each collection has an independent ID namespace so concurrent execution is safe. Bound by ChromaDB Cloud rate limits, not local CPU.
5. **Verify**: rerun the dry-run from step 3; expect `would migrate 0` corpus-wide. Spot-check `(source_path, chunk_index)` dupe-key count on a previously-affected collection (was 1,630 in `code__1-2188` before migration; should be 0 after, since the positional fields no longer exist).

Properties of the verbs:
- **Idempotent**: re-running on a fully-migrated collection performs zero writes.
- **Crash-resumable**: re-invoking after an interrupted run safely sweeps un-deleted old IDs.
- **Carve-outs surface as structured errors**: `taxonomy__*` collections are auto-skipped (centroids use `centroid_hash`, not `chunk_text_hash`). Pre-RDR-053 chunks missing `chunk_text_hash` raise `MissingChunkHashError` rather than silently dropping; the message names the collection and tells the operator to re-index from source.

Pre-existing drift surfaced during Phase 5 verification (filed as separate beads):
- `chash_index` may carry rows for collections that no longer exist in T3 (`Catalog.resolve_chash` self-heals on access; bulk sweep tracked in `nexus-w9vq`).
- `document_aspects` may carry rows whose `source_uri` no longer matches a catalog Document (FK CASCADE + optional one-shot GC tracked in `nexus-urj4`).

**Tumbler ordering**: Comparison operators (`<`, `<=`, `>`, `>=`) use -1 sentinel padding for cross-depth ordering -- parent tumblers sort before their children. `Tumbler.spans_overlap()` detects positional span overlap using these operators.

**Two graph views**: `catalog_links` returns only links between live documents (deleted nodes excluded).
`catalog_link_query` returns all links including orphans -- useful for admin/audit.

**CCE single-chunk note**: For CCE collections (`docs__*`, `rdr__*`, `knowledge__*`), documents with only one chunk are embedded via `contextualized_embed(inputs=[[chunk]])`.

## Taxonomy

Taxonomy (RDR-070) builds a topic hierarchy over T3 collections using existing embeddings, without re-embedding. HDBSCAN clusters the vectors already stored in ChromaDB, labels them with c-TF-IDF, and persists topic assignments to T2 SQLite. Every subsequent `store_put` call assigns the new document to the nearest centroid via ANN lookup. Search then uses these assignments to boost same-topic results and group output.

In local mode, `code__*` collections are excluded by default because MiniLM clusters code poorly. Cloud mode uses `voyage-code-3` and is unaffected. `nx index repo` triggers discovery automatically after indexing.

### Data Flow

```
nx index repo / nx taxonomy discover
  │
  ▼
discover_for_collection()          # taxonomy_cmd.py
  │  fetch ids + texts + embeddings from T3 (page_size=250)
  │  fall back to MiniLM re-embed only when T3 embeddings absent
  ▼
CatalogTaxonomy.discover_topics()  # db/t2/catalog_taxonomy.py
  │  sklearn HDBSCAN on N×D float32
  │  c-TF-IDF labels (CountVectorizer + TfidfTransformer)
  │  persist: topics, topic_assignments → T2 SQLite
  │  upsert cluster centroids → ChromaDB taxonomy__centroids (cosine/HNSW)
  ▼
taxonomy_assign_hook()             # mcp_infra.py  (fires on every store_put)
  │  fetch new doc's T3 embedding
  │  CatalogTaxonomy.assign_single(): ANN query against taxonomy__centroids
  │  nearest centroid → topic_id → INSERT OR IGNORE topic_assignments
  ▼
search_cross_corpus()              # search_engine.py
  │  get_assignments_for_docs(result_ids) → topic_assignments dict
  │  apply_topic_boost(): distance -= 0.1 (same topic), -= 0.05 (linked topic)
  │  topic grouping when assignment coverage >50%
  │  otherwise fall back to Ward hierarchical clustering
```

### Storage

**T2 SQLite tables** (owned by `CatalogTaxonomy`):

| Table | Purpose |
|-------|---------|
| `topics` | One row per discovered topic: label, collection, centroid_hash, doc_count, review_status, terms |
| `topic_assignments` | doc_id → topic_id mapping, assigned_by (hdbscan or centroid) |
| `taxonomy_meta` | Per-collection discover stats (last_discover_at, last_discover_doc_count) |
| `topic_links` | Aggregated inter-topic link counts derived from catalog link graph |

**T3 ChromaDB collection** (`taxonomy__centroids`): created with `embedding_function=None` and `hnsw:space=cosine`. One entry per topic holds the centroid vector, collection, topic_id, and label. Used exclusively by `assign_single()` for ANN lookup. Never goes through `t3.get_or_create_collection()` (that path would inject the wrong embedding function and L2 space).

### Centroid Lifecycle

| Operation | What happens |
|-----------|-------------|
| `discover` | Creates centroids for all topics in a collection |
| `rebuild` (`--force`) | Runs HDBSCAN on updated embeddings, matches new centroids to old via cosine similarity (`_merge_labels`), transfers operator labels and `accepted` status |
| `split` | Replaces the parent centroid with two child centroids |
| `delete` / `merge` | Removes orphaned centroid entries |

Manual labels survive rebuild via `_merge_labels`.

### Connection to the Catalog Link Graph

`nx taxonomy links --collection <col>` reads the catalog link graph and aggregates which topics are connected via document-level links. Results are stored in `topic_links` and read by the search engine via `get_topic_link_map()` to apply the linked-topic distance boost (-0.05).

### CLI (`nx taxonomy`)

| Command | Purpose |
|---------|---------|
| `status` | Health overview: collections, coverage, review state |
| `discover` | Run HDBSCAN on a collection (auto or manual) |
| `rebuild` | Re-discover with merge strategy (preserves labels) |
| `list` | List topics with doc counts and review status |
| `show` | Detail for a single topic: terms, docs, links |
| `review` | Interactive accept/reject workflow |
| `label` | Claude haiku auto-labeling for a collection |
| `assign` | Manually assign a doc to a topic |
| `rename` | Rename a topic label |
| `merge` | Merge two topics into one |
| `split` | Split a topic on a keyword pivot |
| `links` | Compute and persist inter-topic links from catalog |
| `project` | Cross-collection projection with `--use-icf` hub suppression (RDR-077) |

### Projection quality (RDR-077)

`topic_assignments` also carries `similarity` (raw cosine), `assigned_at`,
and `source_collection` for projection rows. Operator guide:
[docs/exploration/taxonomy-projection-tuning.md](exploration/taxonomy-projection-tuning.md) —
threshold calibration, ICF rationale, upsert semantics, troubleshooting.

### Config (`taxonomy` section in `.nexus.yml`)

| Key | Default | Effect |
|-----|---------|--------|
| `auto_label` | `true` | Run Claude haiku labeling after `discover` |
| `local_exclude_collections` | `["code__*"]` | Skip these collections in local mode |

## Post-Store Hooks

Three parallel hook contracts in `src/nexus/mcp_infra.py` cover the three real workload shapes for per-document enrichment that fires after a write. All three chains fire from every storage event, MCP `store_put` and CLI bulk ingest alike; consumers register in exactly one shape based on the grain of work and whether the work benefits from batched dependency calls. All use the same per-hook failure-isolation pattern (capture, persist to T2 `hook_failures` with a `chain` column distinguishing the source, never propagate).

| Shape | Register | Fire | Where it fires from | Current consumers |
|-------|----------|------|---------------------|-------------------|
| Single-document (RDR-070) | `register_post_store_hook(fn)` | `fire_post_store_hooks(doc_id, collection, content)` | MCP `store_put` (once per call) and every CLI ingest path (once per doc in the batch) | empty by default; reserved for future per-doc consumers that key on `doc_id` |
| Batch (RDR-095) | `register_post_store_batch_hook(fn)` | `fire_post_store_batch_hooks(doc_ids, collection, contents, embeddings, metadatas)` | every CLI ingest path with the full batch; MCP `store_put` with a 1-element batch | `chash_dual_write_batch_hook` (RDR-086), `taxonomy_assign_batch_hook` (RDR-070) |
| Document-grain (RDR-089) | `register_post_document_hook(fn)` | `fire_post_document_hooks(source_path, collection, content)` | MCP `store_put` (once per call) and every CLI ingest path (once per source document) | `aspect_extraction_enqueue_hook` (RDR-089: enqueues to `aspect_extraction_queue`, async worker drains) |

The batch contract exists because some enrichments collapse N dependency calls into one batched call (e.g. `taxonomy.assign_batch` issues one ChromaDB Cloud `query()` for N nearest-centroid lookups; the per-doc path issues N sequential queries). For corpus-scale ingest the difference is roughly 1000x. The single-document chain serves work that does not benefit from batching but keys on `doc_id`. The document-grain chain serves work that needs the source document boundary as a stable identity (RDR-089 aspect extraction, where each paper is one extraction regardless of chunk count) — its key is `source_path`, not `doc_id`, and the chain fires once per source document at every CLI ingest entry point as well as at MCP `store_put`.

`taxonomy_assign_batch_hook` accepts `embeddings=None` from the MCP path and fetches them from T3 inline (with a local-MiniLM fallback when the T3 row is unavailable). One hook body covers both the bulk path and the single-document path; there is no separate single-doc taxonomy hook to keep in sync.

`aspect_extraction_enqueue_hook` is the document-grain consumer. The hook persists `(collection, source_path, content)` to `aspect_extraction_queue` (microsecond-scale T2 INSERT) and lazy-spawns a daemon worker that drains the queue and invokes the synchronous `extract_aspects` extractor. The async dispatch is necessary because Critical Assumption #2 in RDR-089 (per-document extraction <3 s) was invalidated by the P1.3 spike (median 26.5 s, p95 38.1 s) — synchronous-inline would block the ingest path for ~25 s per document.

**Content-sourcing contract.** The document-grain dispatcher signature is `(source_path, collection, content)`. MCP `store_put` passes `content=<full document text>` literally — the text is in scope at the boundary. CLI ingest sites accumulate chunks rather than full documents and pass `content=""` as the contract signal that the hook may need to read `source_path` itself. `aspect_extraction_enqueue_hook` persists `content` to the queue row when non-empty (covering the MCP path where `source_path` is a doc_id rather than a real filesystem path) so the worker has the text without re-reading from disk; CLI rows where `content` was not in scope rely on the worker's source-path-read fallback.

**Registration order is load-bearing within the batch chain.** In `mcp/core.py`, `chash_dual_write_batch_hook` is registered before `taxonomy_assign_batch_hook`. This mirrors the legacy CLI call-site ordering (chash dual-write always preceded taxonomy assignment at every site) and preserves the invariant that chash rows exist before topic assignment runs. The single-document and document-grain chains have no inter-hook ordering constraint at present.

**Failure capture.** Per-hook exceptions are caught in the fire function, logged via structlog, and persisted to T2 `hook_failures`. The `chain` column (T2 4.14.2 migration, RDR-089) carries an enum value of `'single'`, `'batch'`, or `'document'` distinguishing the chain that fired. Single-document failures store the scalar `doc_id` in the legacy column. Batch failures store a representative scalar (first id) in `doc_id`, the JSON-encoded list in `batch_doc_ids`, and dual-write `is_batch=1` for back-compat with pre-4.14.2 readers. Document-grain failures store the `source_path` in the legacy `doc_id` column (the column carries 'subject of failure' regardless of chain shape). The `nx taxonomy status` reader surfaces all three shapes and reports `affecting M document(s)` whenever a batch row is present (M > scalar count).

**Drift guard.** `tests/test_hook_drift_guard.py` uses `ast.walk` to detect any ImportFrom, Attribute, or bare-Name reference to a guarded hook outside the explicit allowlist. Two guards: `GUARDED_NAMES = {taxonomy_assign_batch_hook, chash_dual_write_batch_hook}` (allowlist `mcp_infra.py` + `mcp/core.py`); `DOCUMENT_HOOK_GUARDED_NAMES = {aspect_extraction_enqueue_hook}` (allowlist `aspect_worker.py` + `mcp/core.py`). String literals, comments, and docstrings are ignored. Adding a new per-document or batch enrichment registers through the appropriate `register_post_*_hook` entry point; a regression where a new module imports a hook directly fails CI. A separate runtime test `test_index_pdf_fires_document_hook_exactly_once` (in `tests/test_doc_indexer.py`) drives a sample PDF through `index_pdf` with a counting probe hook registered, asserting the document-chain fires exactly once per source document — pinning the runtime invariant the AST count guard alone cannot.

**Out of scope by design** (RDR-095 Decision Rationale, intentional non-twins of the batch-hook pattern):

- Three catalog-registration mechanisms (`_catalog_store_hook` in `commands/store.py`, `_catalog_pdf_hook` in `pipeline_stages.py`, `indexer.py:250` ad-hoc registration) each capture different per-domain metadata: knowledge curator + doc_id for ad-hoc store; corpus curator + file_path + author + year + chunk_count for PDFs; repo owner + rel_path + source_mtime + file_hash for repo files. Consolidating would either lose information or branch internally on origin. Three legitimate per-domain registrations, not three copies of the same hook.
- `_catalog_auto_link` reads T1 scratch entries tagged `link-context` that agents seed before calling MCP `store_put`. CLI bulk ingest has no equivalent pre-declaration semantics; it uses entirely separate post-hoc linkers in `catalog/link_generator.py` (`generate_citation_links`, `generate_code_rdr_links`, `generate_rdr_filepath_links`). MCP-only auto-linking is intentional path-shape coupling.

The partial-commit failure mode (a batch hook commits an early sub-step then raises before completing) is documented in RDR-095 Failure Modes. The framework captures the doc_id list and exception per hook invocation; per-sub-step capture is hook-internal, not framework-level. A future RDR can introduce a `record_partial_progress` helper if a consumer needs it.

## T2 Domain Stores

`src/nexus/db/t2/` is a Python package split into seven domain-specific
stores. Each store owns its own tables in a shared SQLite file and runs
against its own `sqlite3.Connection` in WAL mode. Reads in one domain
are never blocked by writes in another (the Phase 1 global Python
mutex is gone); concurrent writes across domains still serialize at
SQLite's single-writer WAL lock, but `busy_timeout=5000` absorbs the
brief contention without raising `OperationalError`.

| Store             | Class                     | Attribute              | Responsibility                                                             |
|-------------------|---------------------------|------------------------|----------------------------------------------------------------------------|
| Memory            | `MemoryStore`             | `db.memory`            | Persistent notes, project context, FTS5 search, access tracking, TTL       |
| Plans             | `PlanLibrary`             | `db.plans`             | Plan templates, plan search, plan TTL                                      |
| Taxonomy          | `CatalogTaxonomy`         | `db.taxonomy`          | HDBSCAN topic discovery, centroid ANN assignment, merge strategy, review workflow (RDR-070) |
| Telemetry         | `Telemetry`               | `db.telemetry`         | Relevance log (query/chunk/action triples), retention-based expiry         |
| Chash index       | `ChashIndex`              | `db.chash_index`       | Global chash → (collection, doc_id) lookup; populated via dual-write at every T3 upsert site (RDR-086 Phase 1) |
| Document aspects  | `DocumentAspects`         | `db.document_aspects`  | Per-document structured aspects (problem, method, datasets, baselines, results, extras) keyed by `(collection, source_path)`; populated by the async aspect-extraction worker (RDR-089 P1.1) |
| Aspect queue      | `AspectExtractionQueue`   | `db.aspect_queue`      | Durable WAL buffer feeding the aspect-extraction worker; FIFO `claim_next` with cross-process compare-and-swap atomicity; `reclaim_stale` recovers rows from crashed workers (RDR-089 follow-up) |

`T2Database` is a composing facade: it constructs the seven stores in
order (memory → plans → taxonomy → telemetry → chash_index →
document_aspects → aspect_queue), re-exposes the memory-domain public
methods as thin delegates for backward compatibility, and runs
cross-domain operations like `expire()` over all of them. The
chash_index, taxonomy, document_aspects, and aspect_queue domains are
accessed directly via their attributes -- no facade delegates exist
for them. The facade holds no database connection of its own; every
SQL statement runs through a specific domain store.

**Preferred call style for new code**:

```python
db = T2Database(path)
db.memory.search("fts query", project="myproj")   # domain method
db.plans.save_plan(query, plan_json)               # domain method
db.telemetry.log_relevance(query, ...)             # domain method
```

Existing call sites that use `db.search(...)`, `db.save_plan(...)`,
etc. continue to work via facade delegation -- no migration required.

### Concurrency Model (RDR-063 Phase 2)

Phase 2 replaced a single shared connection with per-store connections:

| Phase      | Connection                | Lock                          | Cross-domain writes     |
|------------|---------------------------|-------------------------------|-------------------------|
| Phase 1    | one `SharedConnection`    | one `threading.Lock`          | serialized in Python    |
| Phase 2    | one per store             | one `threading.Lock` per store | coordinated in SQLite   |

Phase 2 consequences:

- **Cross-domain reads no longer block on unrelated writes**: a
  `memory_search` on one thread and a `plan_save` on another run in
  parallel because the Phase 1 shared Python mutex is gone. Concurrent
  *writes* across domains still serialize at SQLite's single-writer
  WAL lock, but `busy_timeout=5000` absorbs the brief queue so callers
  do not see `OperationalError: database is locked`.
- **Telemetry no longer interferes with search**: MCP relevance-log
  writes run on the telemetry connection, so `memory_search` is not
  blocked by access-tracking hooks.
- **Cluster rebuilds don't freeze memory**: `CatalogTaxonomy.discover_topics`
  runs on the taxonomy connection. The long numpy clustering phase holds
  no T2 locks, so interactive memory operations continue during the
  bulk of the rebuild. (The initial embedding-fetch snapshot still briefly
  acquires the taxonomy connection's lock, as any read does.)
- **Parallel writes to the same store are serialized** by that store's
  own `threading.Lock` plus the SQLite file-level write lock -- callers
  never see `OperationalError: database is locked`.

**Migration Registry** (RDR-076): All T2 schema migrations are centralised in
`src/nexus/db/migrations.py`. The `MIGRATIONS` list contains version-tagged
`Migration(introduced, name, fn)` entries. `apply_pending(conn, current_version)`
runs migrations between the last-seen version (stored in `_nexus_version` table)
and the current CLI version. Each migration function is idempotent via
`PRAGMA table_info()` or `sqlite_master` guards.

`T2Database.__init__()` opens a transient connection, calls `apply_pending()`,
closes it, then constructs the four domain stores. The `_upgrade_done` set
(guarded by `_upgrade_lock`) provides a process-level fast path — subsequent
constructions skip all DB access. Domain stores retain their own
`_migrated_paths` guards for standalone construction outside `T2Database`.

**T3 Upgrade Steps**: `T3UpgradeStep(introduced, name, fn)` entries in the
`T3_UPGRADES` list handle ChromaDB operations (backfills, re-indexing) that
require a `T3Database` client. These run via `nx upgrade` (not `--auto` mode).

**Auto-upgrade**: `nx upgrade --auto` runs as the first SessionStart hook,
applying T2 migrations silently. T3 steps are skipped in auto mode.

**In-memory SQLite**: Tests that want an ephemeral database should use
a temp file path, not `":memory:"` -- `:memory:` databases are
per-connection, so the four stores would each see a distinct empty
database and `test_t2_concurrency.py` would no longer exercise the
cross-domain WAL path.

See `src/nexus/db/t2/__init__.py` for the facade source and
`tests/test_t2_concurrency.py` for the concurrency test suite.

## Module Map

| Area | Files | What they do |
|------|-------|-------------|
| **Entry** | `cli.py`, `commands/` | Click CLI, one file per command group |
| **Catalog** | `catalog/catalog.py`, `catalog/catalog_db.py`, `catalog/tumbler.py`, `catalog/link_generator.py`, `catalog/auto_linker.py`, `catalog/consolidation.py` | Git-backed document registry + typed link graph (JSONL + SQLite). Tumbler addressing, `descendants()`/`ancestors()`/`lca()` hierarchy helpers, `resolve_chunk()` ghost element resolution, idempotent link upsert, composable query, bulk ops, audit. Auto-linker creates links from T1 link-context on every `store_put`. `consolidation.py` merges per-paper collections into corpus-level collections |
| **Storage** | `db/t1.py`, `db/t2/`, `db/t3.py`, `db/chroma_quotas.py`, `db/local_ef.py` | Tier implementations. T2 is a package split into seven domain stores (see § T2 Domain Stores). Plans table has `ttl` column for auto-expiry. `chroma_quotas.py` is the single source of truth for ChromaDB Cloud quota constants and validators. `local_ef.py` provides the local ONNX embedding function |
| **Indexing** | `indexer.py`, `code_indexer.py`, `prose_indexer.py`, `index_context.py`, `indexer_utils.py`, `classifier.py`, `chunker.py`, `md_chunker.py`, `doc_indexer.py`, `pdf_extractor.py`, `pdf_chunker.py`, `bib_enricher.py`, `languages.py`, `pipeline_buffer.py`, `pipeline_stages.py`, `checkpoint.py` | Repo indexing pipeline (decomposed per RDR-032). `bib_enricher.py` queries Semantic Scholar for bibliographic metadata; `pdf_extractor.py` auto-detects math-heavy PDFs via FormulaItem counting and routes to MinerU (default-installed since nexus-2fyb) for LaTeX extraction; non-math PDFs use Docling. MinerU absence at runtime raises a `RuntimeError` rather than silently falling back to formula-stripped Docling — the prior silent fallback wiped formulas from every PDF indexed for weeks. MinerU processes large PDFs in 5-page subprocess batches for memory isolation (prevents OOM on formula-dense documents). Chunk metadata includes `has_formulas` boolean. `pipeline_buffer.py` provides a WAL-mode SQLite buffer for the three-stage streaming pipeline (RDR-048); `pipeline_stages.py` implements the concurrent extractor/chunker/uploader stages and orchestrator; `checkpoint.py` handles batch-path crash recovery for smaller documents (RDR-047) |
| **Export** | `exporter.py` | Collection export/import for T3 backup and migration (.nxexp format) |
| **DEVONthink** | `devonthink.py`, `commands/dt.py` | macOS-only `nx dt` integration verbs (RDR-099). `devonthink.py` exposes 5 selector helpers (`_dt_selection`, `_dt_uuid_record`, `_dt_tag_records`, `_dt_group_records`, `_dt_smart_group_records`) over a centralised `_run_osascript` spawn; the smart-group helper does an sdef-canonical three-property read (`search predicates` PLURAL + `search group` + `exclude subgroups`) and re-executes the search to honour user-authored scope. `commands/dt.py` is the Click surface: `nx dt index` dispatches per-record by extension (.pdf/.md) into the existing `nexus.doc_indexer` entry points, and `nx dt open` round-trips tumblers/UUIDs back to DT via `open(1)`. Substrate `meta.devonthink_uri` reverse-lookup shipped in 4.17.0 (nexus-srck) |
| **Plans** | `plans/matcher.py`, `plans/runner.py`, `plans/bundle.py`, `plans/session_cache.py`, `plans/loader.py`, `plans/match.py`, `plans/scope.py`, `plans/schema.py`, `plans/seed_loader.py`, `plans/promote.py`, `plans/purposes.py` | Plan-centric retrieval stack. `matcher.py`: T1 cosine + T2 FTS5 fallback with RDR-091 scope filter/re-rank. `runner.py`: `plan_run` executes step DAGs — contiguous operator runs collapse into a single `claude -p` call via the bundle path (v4.10.0). `bundle.py`: operator-bundle module — segmentation, composite-prompt composition with source attribution + deferred-ref rendering, single-dispatch execution, 200k-char size guard with per-step fallback. `session_cache.py`: `plans__session` T1 cosine cache (MiniLM). `loader.py` + `seed_loader.py`: YAML plan loading + seeding of builtin templates. `match.py`: `Match` dataclass contract. `scope.py`: scope normalization + scope-fit weight. `schema.py`: step schema validation. `promote.py`: plan promotion heuristics. `purposes.py`: typed-link purpose registry for `traverse` operator |
| **Console** | `console/` (`app.py`, `watchers.py`, `config.py`, `routes/`), `commands/console.py` | Embedded web UI for monitoring agentic Nexus activity (`nx console`). FastAPI/uvicorn server with live-updating routes for activity, campaigns, health, and partials. `commands/console.py` handles start/stop lifecycle and PID file management |
| **Search** | `search_engine.py`, `search_clusterer.py`, `scoring.py`, `frecency.py`, `ripgrep_cache.py`, `filters.py` | Query, rank, rerank. `scoring.py` applies topic boost (`apply_topic_boost`: same-topic -0.1, linked-topic -0.05). `search_engine.py` does topic grouping (T2 assignments when >50% coverage) with fallback to Ward hierarchical clustering. `filters.py` also contains `sanitize_query()` (RDR-071) which strips LLM prompt contamination from search queries before embedding |
| **Context** | `context.py`, `commands/context_cmd.py` | L1 project context cache (RDR-072). `generate_context_l1()` builds a ~200 token topic map from taxonomy, cached as flat file at `~/.config/nexus/context/<repo>-<hash>.txt`. Injected by SessionStart hook for agent cold-start acceleration. Auto-refreshed after `taxonomy discover` and `index repo` |
| **Taxonomy** | `db/t2/catalog_taxonomy.py`, `commands/taxonomy_cmd.py`, `taxonomy.py` (shim) | HDBSCAN topic discovery from T3 embeddings (RDR-070). T2 tables: `topics`, `topic_assignments`, `taxonomy_meta`, `topic_links`. ChromaDB `taxonomy__centroids` (cosine/HNSW) for centroid ANN. `discover_for_collection()` is the shared entry point for CLI and `nx index repo`. `taxonomy_assign_hook` in `mcp_infra.py` fires on every `store_put` for incremental assignment. `taxonomy.py` is a backward-compatibility shim that forwards old call sites to `db.taxonomy` |
| **Hooks** | `commands/hooks.py`, `commands/hook.py` | `hooks.py`: Git hook install/uninstall/status, sentinel-bounded stanza management. `hook.py`: Claude Code SessionStart/SessionEnd lifecycle runners |
| **Verification** | `config.py` (verification section), `conexus/hooks/scripts/stop_verification_hook.sh`, `conexus/hooks/scripts/pre_close_verification_hook.sh`, `conexus/hooks/scripts/read_verification_config.py` | Opt-in mechanical enforcement: Stop hook (session-end checks), PreToolUse hook (bd-close gate), standalone config reader. See [Verification config](configuration.md#verification) |
| **MCP Servers** | `mcp/core.py`, `mcp/catalog.py`, `mcp_infra.py`, `mcp_server.py` (shim) | Dual-server FastMCP architecture (RDR-062). `nexus` core server (26 tools: storage, retrieval, operators, orchestration) + `nexus-catalog` (10 tools: catalog and link graph). Short-name convention: catalog tools drop the redundant `catalog_` prefix since the server namespace already provides context. Six destructive / maintenance operations are intentionally kept CLI-only. Backward-compat shim at `mcp_server.py` re-exports every function. `query()` has catalog-aware routing (author, content_type, subtree, follow_links, depth); singletons and test injection live in `mcp_infra.py`. **For the full tool catalog see [MCP Servers](mcp-servers.md).** |
| **Enrichment** | `bib_enricher.py`, `aspect_extractor.py`, `aspect_worker.py`, `commands/enrich.py` | Two enrichment surfaces. (1) Bibliographic via Semantic Scholar (`bib_enricher.py` lookup + `nx enrich bib` CLI). (2) Structured aspects via Claude CLI (`aspect_extractor.py` synchronous extractor + `aspect_worker.py` async-queue daemon worker registered as the document-grain post-store hook + `nx enrich aspects` CLI). Aspect extraction is `knowledge__*` only in Phase 1 (RDR-089); the worker drains `aspect_extraction_queue` and writes to `document_aspects` |
| **Health** | `health.py`, `logging_setup.py` | `health.py`: health check data model and runner used by `nx doctor` and `nx console`. `logging_setup.py`: structured logging configuration for CLI, console, MCP, and hook entry points (stderr + rotating file handler) |
| **Support** | `config.py`, `registry.py`, `corpus.py`, `session.py`, `hooks.py`, `ttl.py`, `formatters.py`, `types.py`, `errors.py`, `retry.py`, `commands/_helpers.py`, `commands/_provision.py` | Configuration, naming, formatting, session lifecycle, transient-error retry. `_helpers.py`: shared CLI helpers (e.g. `default_db_path()`). `_provision.py`: ChromaDB Cloud database provisioning (tenant resolution, database creation) |

### Builtin plan templates

The plan-centric retrieval stack ships fifteen builtin templates under `conexus/plans/builtin/`. The seed loader (`nexus.plans.seed_loader.load_seed_directory`) upserts them into `PlanLibrary` on first run; idempotent thereafter. Each template pins a `verb` dimension (and usually `scope: global`); the matcher uses verb to filter candidates before cosine ranking.

Grouped by verb:

- **verb=query**
  - `abstract-themes`: CheapRAG community-summary pipeline (`search` → `groupby` → `aggregate` → `summarize`) for theme extraction, topic landscape, and summary-of-findings questions. RDR-098.
- **verb=analyze**
  - `analyze-default`: Cross-corpus synthesis across prose and code. Gathers from both sides, walks reference chains, hydrates candidates, ranks against the caller's intent.
- **verb=research**
  - `research-default`: Concept → prose → implementing code. Walks from RDRs/docs/knowledge into the modules that implement them, then surfaces concrete code context.
  - `citation-traversal`: Trace the citation chain around a seed document. Walks `cites` edges inward and outward, hydrates matches, summarises.
  - `find-by-author`: Author-index lookup. Routes through the catalog's author index, hydrates matching documents, summarises contributions.
  - `type-scoped-search`: Single-content-type semantic search. Resolves the content-type bucket and runs the query against only those collections.
- **verb=lookup**
  - `hybrid-factual-lookup`: Factual claim, named entity, or specific data point. Fuses vector recall with FTS lexical match for narrow-target retrieval.
  - `traverse-then-generate`: Expand from a known seed tumbler. Walks `cites`/`implements`/related edges and generates a factual answer from the linked documents.
- **verb=document**
  - `document-default`: Documentation authoring or audit. Gathers prose and code touching the area, walks documentation-for edges, hydrates both corpora.
- **verb=review**
  - `review-default`: Change-set critique. Resolves changed files to catalog entries, walks decision-evolution history (RDRs superseded or cited), hydrates the RDR context.
- **verb=debug**
  - `debug-default`: Dev work from a concrete failure. Catalog per-file lookup as the primary link walk; multi-hop graph traversal is delegated to Serena.
- **verb=plan-author**
  - `plan-author-default`: Authoring a new plan template. Fetches the authoring guide and dimension registry, surveys prior art for the target verb, drafts a candidate `plan_json`.
- **verb=plan-inspect**
  - `plan-inspect-default`: Single-plan runtime metrics and match history (`use_count`, `match_count`, `match_conf_sum`, success/failure counts).
  - `plan-inspect-dimensions`: Enumerate registered dimensions and count plans per axis. Surfaces the dimension registry to authoring agents.
- **verb=plan-promote**
  - `plan-promote-propose`: Rank promotion candidates from runtime metrics against the configured thresholds.

## Design Decisions

1. **Protocols over ABCs** -- `typing.Protocol` for structural subtyping, no inheritance coupling.
2. **No ORM** -- Direct `sqlite3` for T2. Schema is simple; WAL + FTS5 are stdlib.
3. **Constructor injection** -- Dependencies via constructor, no global singletons.
4. **Ported, not imported** -- SeaGOAT and Arcaneum patterns rewritten in Nexus module structure.
5. **PPID-chain session propagation** -- The `SessionStart` hook starts a per-session ChromaDB HTTP server (using the `chroma` entry-point co-installed with the package) and writes its address to `~/.config/nexus/sessions/{ppid}.session`, keyed by the Claude Code process PID. Child agents walk the OS PPID chain to find the nearest ancestor session file and connect to the same server, sharing T1 scratch across the entire agent tree. Concurrent independent windows stay isolated via disjoint process trees. Falls back to `EphemeralClient` when the server cannot start or the PPID chain yields no record.
6. **MCP tools over agent-spawns for utility operations** (RDR-080) -- Operations that formerly required spawning a named agent are now MCP tools that execute in-process. Agent files are retained as stubs that redirect to the MCP tool.

   **Boundary rule**: If an operation can be expressed as a deterministic function of its inputs and completes in under one API call, it is an MCP tool. If it requires multi-turn reasoning, tool selection, or context accumulation across turns, it is an agent.

   | Capability | Before RDR-080 | After RDR-080 |
   |------------|---------------|---------------|
   | Knowledge consolidation | `knowledge-tidier` agent | `mcp__plugin_conexus_nexus__nx_tidy` |
   | Plan audit | `plan-auditor` agent | `mcp__plugin_conexus_nexus__nx_plan_audit` |
   | Bead enrichment | `plan-enricher` agent | `mcp__plugin_conexus_nexus__nx_enrich_beads` |
   | Multi-step retrieval | `query-planner` + `analytical-operator` agents | `mcp__plugin_conexus_nexus__nx_answer` |
   | PDF indexing | `pdf-chromadb-processor` agent | `nx index pdf` CLI / direct ingest |

   When authoring agent/skill instructions, always use the full MCP tool name (`mcp__plugin_conexus_nexus__<tool>`) — short names fail at runtime.

   See [MCP Tools vs Agents](exploration/mcp-vs-agents.md) for the full boundary rule, the stub-agent pattern, and guidance on where to place new capabilities. See [Plan-Centric Retrieval](plan-centric-retrieval.md) for how `nx_answer` + the plan library replaced the earlier retrieval-agent chain.

## Heritage

| Tool | What Nexus borrows |
|------|-------------------|
| **mgrep** | UX patterns, citation format, Claude Code integration |
| **SeaGOAT** | Git frecency scoring, hybrid search, persistent server |
| **Arcaneum** | PDF extraction + chunking pipelines, RDR process |

Storage (ChromaDB + Voyage AI) and embedding layers are Nexus's own.

