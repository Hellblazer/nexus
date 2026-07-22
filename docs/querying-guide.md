# Querying Guide

Nexus has three retrieval interfaces. This page is the **decision guide** — when to reach for which, and how the search-quality mechanics work underneath.

For the **tool catalog** (every tool, parameters, which server it lives on), see [MCP Servers](mcp-servers.md).

## Which interface

| Interface | Use for | Returns | Latency |
|---|---|---|---|
| `nx search` (CLI) | Quick chunk lookup from the terminal | Text chunks with topic grouping | < 1s |
| `search()` MCP | Chunk search from agents, with topic scoping | Chunks grouped by topic, with boost | < 1s |
| `query()` MCP | Document-level retrieval with catalog routing | Best snippet per document + metadata | < 2s |
| `nx_answer` MCP / `/conexus:query` skill | Multi-step analytical queries | Synthesized answer | 5–15s |

**Rule of thumb**: start with `nx search` for quick lookups. Use `search()` MCP with `topic=` to narrow to a specific knowledge domain. Use `query()` when you need to scope by author, content type, or follow citation links. Use `nx_answer` for questions that require extracting, comparing, or generating across multiple sources.

```
User or Agent
     │
     ├─ nx search ──────────────────► T3 semantic search (chunks)
     │
     ├─ search() MCP ──► topic filter ──► T3 + topic boost + grouping
     │
     ├─ query() MCP ──► catalog ──────► T3 scoped + link boost
     │                    │
     │                    └─ link graph traversal (follow_links)
     │
     └─ nx_answer MCP / /conexus:query
              │
              ├─ Path 1: plan_match → plan_run
              ├─ Path 2: bundled operator chain
              └─ Path 3: inline planner (plan-miss)
```

All paths query T3 and benefit from topic-aware ranking. `search()` and `query()` use the T2 topic store for grouping, distance boosting, and optional pre-filtering.

## nx search (CLI)

```bash
nx search "authentication middleware"                    # basic semantic search
nx search "caching strategy" --corpus code               # search only code
nx search "schema design" --hybrid                       # semantic + frecency + ripgrep
nx search "database" --where bib_year>=2024              # metadata filter
nx search "error handling" -c --bat                      # show content with syntax highlighting
```

See [CLI Reference — nx search](cli-reference.md#nx-search) for all flags.

## search() MCP tool

Chunk-level semantic search from agents — equivalent to `nx search` but accessible via MCP.

```python
search(query="authentication middleware")
search(query="caching", corpus="knowledge,docs")
search(query="schema design", where="bib_year>=2024")
search(query="error handling patterns", cluster_by="semantic")
search(query="extraction pipeline", topic="Math-aware PDF Extraction")
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | (required) | Search query text |
| `corpus` | string | `knowledge,code,docs` | Comma-separated prefixes or full names. `all` searches everything |
| `limit` | int | `10` | Page size |
| `offset` | int | `0` | Skip this many results (pagination) |
| `where` | string | `""` | Metadata filter (`KEY=VALUE`, comma-separated; supports `=`, `>=`, `<=`, `>`, `<`, `!=`) |
| `cluster_by` | string | `""` | Set to `semantic` to group results by topic (Ward fallback below 50% coverage) |
| `topic` | string | `""` | Pre-filter to a named topic. Run `nx taxonomy list` for available topics |
| `threshold` | float | per-corpus config | Override the distance threshold uniformly (raw cosine distance, lower = stricter; `inf` disables filtering — RDR-087) |
| `structured` | boolean | `false` | Return `{ids, tumblers, distances, collections}` instead of the human-readable string (plan-runner use) |

## query() MCP tool

The primary retrieval interface for agents. Combines semantic search with catalog-aware routing — scoping by author, content type, document subtree, or citation links before searching.

```python
# Catalog-aware routing
query(question="schema mappings", author="Fagin")           # papers by an author
query(question="architecture decisions", content_type="rdr") # RDR documents only
query(question="indexing pipeline", subtree="1.1")           # under tumbler prefix 1.1
query(question="database design", follow_links="cites", depth=1)  # + citation graph
```

| Parameter | Type | Description |
|---|---|---|
| `question` | string | Search query text (required) |
| `corpus` | string | Collection prefix or comma-separated list (default `knowledge`; pass `all` for every corpus) |
| `author` | string | Filter by document author (catalog lookup) |
| `content_type` | string | `code`, `prose`, `rdr`, `paper`, `knowledge` |
| `subtree` | string | Tumbler prefix — search only documents under this address |
| `follow_links` | string | Link type to follow (e.g., `cites`) — enriches with linked documents |
| `depth` | integer | Hops to follow in the link graph (default 1) |
| `limit` | integer | Maximum results (default 10) |
| `where` | string | Vector-store metadata filter (e.g. `bib_year>=2020,section_type!=references`) |
| `structured` | boolean | Return a structured dict instead of the human-readable string (plan-runner use; default false) |

### How catalog routing works

1. If `author`, `content_type`, or `subtree` is set: query the catalog for matching documents, extract their `physical_collection` values, and search only those collections.
2. If `follow_links` is set: find matching documents, BFS-traverse their link graph to `depth`, collect all linked collections.
3. If no catalog parameters: fall through to corpus-based search (same as `nx search`).

`query(question="X", author="Fagin")` is faster and more precise than `query(question="X")` — fewer collections to search, hits constrained by the catalog before embedding.

### Link-aware scoring

`query()` automatically boosts results from documents with outgoing `implements` links. Code linked to an RDR ranks higher than unlinked code at similar semantic distance. The boost is additive (+0.15 × link signal) with per-type weights:

| Link type | Weight | Rationale |
|---|---|---|
| `implements` | 1.0 | Precise — manually authored or filepath-extracted |
| `relates` / `cites` | 0.5 | Moderate signal |
| `implements-heuristic` | 0.0 | Too noisy (87% of links, substring-matched) |
| `supersedes` | 0.0 | Historical, not relevance |

`search()` does **not** apply link boost; it does apply topic boost. Use `query()` when you want both the link graph and topic ranking to shape results.

## /conexus:query skill → nx_answer MCP tool (analytical queries)

For questions that require multiple retrieval steps — comparing sources, extracting structured data, generating from evidence — invoke the `nx_answer` MCP tool. The `/conexus:query` skill is a thin pointer to it (RDR-080 consolidation; replaces the earlier `query-planner` + `analytical-operator` agent pair).

### The trunk: plan-match → plan-run → record

`nx_answer` runs `plan_match` → `plan_run` → record on every call, with operator bundling collapsing contiguous LLM-operator steps into a single subprocess and a plan-miss path falling back to an inline planner. See [plan-centric-retrieval.md](plan-centric-retrieval.md) for the full mechanism, including bundling-latency measurements.

### Builtin scenario plans (RDR-078)

`nx catalog setup` seeds YAML plan templates under `conexus/plans/builtin/` — see [plan-centric-retrieval.md](plan-centric-retrieval.md) for the current builtin plan catalog.

### Verb skills

The verb skills (`/conexus:research`, `/conexus:review`, `/conexus:analyze`, `/conexus:debug`, `/conexus:document`) route directly to `plan_match` scoped to the matching verb, falling through to `/conexus:query` on miss. See [plan-centric-retrieval.md](plan-centric-retrieval.md) for the full verb-routing table.

### Example analytical queries

```
# Cross-source consistency
/conexus:query Compare what the architecture docs say about caching with what the code actually does

# Citation chain analysis
/conexus:query What papers does the Delos survey cite, and which of those are in our knowledge base?

# Evidence-grounded extraction
/conexus:query Extract all error handling patterns from the indexing pipeline code, with file locations

# Multi-corpus comparison
/conexus:query How does our RDR process compare to what the literature recommends?
```

## Search quality features

Several mechanisms run automatically across all interfaces.

### Topic-aware ranking

> **Note (6.0):** Topic *discovery*, *rebuild*, and per-document *assignment* run on the nexus-service backend (the default since 6.0) — `nx taxonomy discover` and `nx index repo` work normally (nexus-7ydks). `nx taxonomy split` / `project` and the cross-collection projection pass are still being ported and refuse cleanly on the service.

After `nx index repo` (or `nx taxonomy discover --all`), topics are clustered via HDBSCAN with Claude-Haiku auto-labels. Topic-aware ranking then works three ways:

- **Topic boost** — results sharing a topic cluster get a distance reduction of 0.1; results in adjacent linked topics get 0.05. Automatic on `search` and `query`.
- **Topic grouping** — pass `cluster_by="semantic"` on `search()` to group results by topic label when more than 50% of results carry topic assignments. Falls back to Ward clustering below that threshold.
- **Topic-scoped search** — the `topic` parameter on `search()` pre-filters results to documents in a single named topic. Run `nx taxonomy list` to see available topics.

```python
search(query="consensus protocol", topic="Byzantine Fault Tolerant Consensus")
```

`store_put` auto-assigns new documents to the nearest topic via centroid lookup. Operator-curated labels survive `nx taxonomy rebuild` via centroid-matching (cosine similarity > 0.8). See [Document Catalog § Topic taxonomy](catalog.md#topic-taxonomy) and [CLI Reference — nx taxonomy](cli-reference.md#nx-taxonomy).

### Distance thresholds (automatic noise filtering)

Results exceeding per-corpus distance thresholds are filtered before reaching the caller. Calibrated for Voyage AI embeddings (cloud mode only); configurable in `.nexus.yml`.

| Corpus | Threshold | Effect |
|---|---|---|
| `code__*` | 0.45 | Functionally inert post-RDR-059 (all relevant code < 0.43) |
| `knowledge__*`, `docs__*`, `rdr__*` | 0.65 | Relevant ends ~0.59, noise starts ~0.67 |
| Cross-corpus default | 0.55 | 93% of relevant results below this threshold |

### Section-type metadata filtering

Markdown chunks carry `section_type` metadata (abstract, introduction, methods, results, discussion, conclusion, references, acknowledgements, appendix). Use `--where section_type!=references` to exclude reference sections, which account for ~76% of noise in knowledge collections.

```bash
nx search "caching strategy" --where section_type!=references
```

### Corpus-specific over-fetch

Knowledge, docs, and RDR collections fetch 4x the requested result count before filtering (vs 2x for code), compensating for higher noise in prose collections.

### Catalog pre-filtering

When metadata filters have high selectivity (<5% of documents match), Nexus pre-fetches matching file paths from the catalog SQLite database and passes them as a `source_path` filter to the vector store. This reduces the scan space before retrieval, avoiding the latency cliff an ANN index hits in predicate-sparse regions. Automatic when a catalog is available.

### Multi-probe collection health

`nx collection verify --deep` probes up to 5 documents per collection and reports a hit rate. Below 100% indicates degraded retrieval — run `nx doctor --fix` (local mode) or re-index.

### Contradiction detection (RDR-057 Phase 3a)

When two results from the same collection have near-identical embeddings (cosine distance < 0.3) but different `source_agent` provenance, both are flagged with `_contradiction_flag` in metadata. The MCP `search` tool renders this as a `[CONTRADICTS ANOTHER RESULT]` suffix:

```
[0.1234] Caching strategy notes [CONTRADICTS ANOTHER RESULT]
  The authoritative cache layer is Redis with 24h TTL...
[0.1267] Caching strategy notes [CONTRADICTS ANOTHER RESULT]
  We use Memcached for session cache with 1h expiry...
```

Two agents recorded conflicting claims; investigate and consolidate. The flag is informational; neither result is dropped.

Enabled by default. Opt out via `search.contradiction_check: false` in `.nexus.yml`. The check adds one extra embedding fetch per collection (shared with clustering when both are enabled). See [Configuration](configuration.md).

## See also

- [MCP Servers](mcp-servers.md) — every tool, every server, every parameter
- [CLI Reference](cli-reference.md) — every `nx` subcommand and flag
- [Plan-Centric Retrieval](plan-centric-retrieval.md) — the full `nx_answer` trunk, plan library, dimensions
- [Plan Authoring Guide](plan-authoring-guide.md) — schema for authoring new plans
- [Document Catalog](catalog.md) — catalog concepts, link types, purposes, topic taxonomy
- [MCP Tools vs Agents](exploration/mcp-vs-agents.md) — why `nx_answer` replaced the agent pair
