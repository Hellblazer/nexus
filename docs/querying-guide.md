# Querying Guide

Nexus has three query interfaces, each suited to different needs.

## Which interface to use

| Interface | Use for | Returns | Latency |
|-----------|---------|---------|---------|
| `nx search` | Semantic chunk search from the CLI | Text chunks with source location | < 1s |
| `query()` MCP tool | Document-level retrieval with catalog routing | Best snippet per document + metadata | < 2s |
| `/nx:query` skill | Complex multi-step analytical queries | Synthesized answer | 5-15s |

**Rule of thumb**: Start with `nx search` for quick lookups. Use `query()` when you need to scope by author, content type, or follow citation links. Use `/nx:query` for questions that require extracting, comparing, or generating across multiple sources.

---

## nx search (CLI)

Semantic search across T3 knowledge collections. Returns individual chunks ranked by relevance.

```bash
nx search "authentication middleware"                    # basic semantic search
nx search "caching strategy" --corpus code               # search only code collections
nx search "schema design" --hybrid                       # semantic + frecency + ripgrep
nx search "database" --where bib_year>=2024              # metadata filter
nx search "error handling" -c --bat                      # show content with syntax highlighting
```

See [CLI Reference](cli-reference.md#nx-search) for all flags.

---

## search() MCP tool

The `search()` MCP tool provides chunk-level semantic search from agents — equivalent to `nx search` but accessible via MCP.

```python
# Basic search
search(query="authentication middleware")

# Search specific corpora
search(query="caching", corpus="knowledge,docs")

# With metadata filter
search(query="schema design", where="bib_year>=2024")

# With semantic clustering
search(query="error handling patterns", cluster_by="semantic")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | (required) | Search query text |
| `corpus` | string | `knowledge,code,docs` | Comma-separated corpus prefixes or full names. `all` searches everything |
| `limit` | int | `10` | Page size |
| `offset` | int | `0` | Skip this many results (pagination) |
| `where` | string | `""` | Metadata filter (`KEY=VALUE` format, comma-separated) |
| `cluster_by` | string | `""` | Set to `semantic` to group results by Ward clustering |

---

## query() MCP tool

The `query()` MCP tool is the primary interface for agents. It combines semantic search with catalog-aware routing — scoping results by author, content type, document subtree, or citation links before searching.

### Basic usage

```python
# Simple semantic search (same as nx search, but from an agent)
query(question="caching strategies")

# Scope to a specific corpus
query(question="indexing pipeline", corpus="code")
```

### Catalog-aware routing

When you add catalog parameters, the tool first resolves matching documents via the catalog, then searches only those collections:

```python
# Search only papers by a specific author
query(question="schema mappings", author="Fagin")

# Search only RDR documents
query(question="architecture decisions", content_type="rdr")

# Search within a document subtree (all docs under owner 1.1)
query(question="indexing pipeline", subtree="1.1")

# Include documents cited by matching papers
query(question="database design", follow_links="cites", depth=1)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `question` | string | Search query text (required) |
| `corpus` | string | Collection prefix or comma-separated list (default: `knowledge,code,docs,rdr`) |
| `author` | string | Filter by document author (catalog lookup) |
| `content_type` | string | Filter by type: `code`, `prose`, `rdr`, `paper`, `knowledge` |
| `subtree` | string | Tumbler prefix — search only documents under this address (e.g., `1.1`) |
| `follow_links` | string | Link type to follow (e.g., `cites`) — enriches results with linked documents |
| `depth` | integer | How many hops to follow in the link graph (default: 1) |
| `n` | integer | Maximum results (default: 10) |
| `where` | string | ChromaDB metadata filter (e.g., `bib_year>=2024`) |

### How routing works

1. If `author`, `content_type`, or `subtree` is set: query the catalog for matching documents, extract their `physical_collection` values, and search only those collections.
2. If `follow_links` is set: find matching documents, then BFS-traverse their link graph to the given `depth`, collecting all linked documents' collections.
3. If no catalog parameters: fall through to corpus-based search (same as `nx search`).

This means `query(question="X", author="Fagin")` is faster and more precise than `query(question="X")` because it searches fewer collections.

---

## /nx:query skill (analytical queries)

For questions that require multiple retrieval steps — comparing sources, extracting structured data, or generating from evidence — the `/nx:query` skill orchestrates a multi-step plan.

### Three-path dispatch

The skill routes queries through three paths in order of complexity:

**Path 1 — Single query**: If the question can be answered by a single `query()` call with catalog params (author, content_type, subtree), it does that directly. Fastest.

**Path 2 — Template match**: If the question matches a pre-built query template (e.g., "compare X across corpora"), it uses the cached plan. The 5 built-in templates are:

| Template | Matches questions like |
|----------|----------------------|
| Author search | "Find papers by [author]" |
| Citation chain | "What does [paper] cite?" |
| Provenance chain | "What implements [design doc]?" |
| Cross-corpus compare | "Compare [topic] across code and docs" |
| Type-scoped search | "Find all [content type] about [topic]" |

Custom plans that succeed are cached for 30 days and matched on subsequent similar queries.

**Path 3 — Planner**: For novel analytical questions, a planner agent decomposes the question into retrieval + analysis steps.

### Example analytical queries

```
# Cross-source consistency
/nx:query Compare what the architecture docs say about caching with what the code actually does

# Citation chain analysis
/nx:query What papers does the Delos survey cite, and which of those are in our knowledge base?

# Evidence-grounded extraction
/nx:query Extract all error handling patterns from the indexing pipeline code, with file locations

# Multi-corpus comparison
/nx:query How does our RDR process compare to what the literature recommends?
```

---

## Relationship between search interfaces

```
User or Agent
     │
     ├─ nx search ──────────────────► T3 semantic search (chunks)
     │
     ├─ query() MCP ──► catalog ──► T3 scoped search (documents)
     │                    │
     │                    └─ link graph traversal (follow_links)
     │
     └─ /nx:query skill
              │
              ├─ Path 1: single query() call
              ├─ Path 2: template match (cached plan)
              └─ Path 3: planner agent (novel decomposition)
```

All three paths ultimately query the same T3 collections — the difference is how they scope, route, and compose the search.

---

## Search quality features

Several features work automatically to improve result quality across all search interfaces.

### Distance thresholds (automatic noise filtering)

Results exceeding per-corpus distance thresholds are filtered before reaching the caller. This removes the "noise tail" — irrelevant chunks that pad the bottom of result lists. Thresholds are calibrated for Voyage AI embeddings (cloud mode only) and configurable via `.nexus.yml`.

| Corpus | Threshold | Effect |
|--------|-----------|--------|
| `code__*` | 0.45 | Functionally inert post-RDR-059 (all relevant code <0.43) — guards future model changes |
| `knowledge__*`, `docs__*`, `rdr__*` | 0.65 | Relevant results end ~0.59, noise starts ~0.67 |
| Cross-corpus default | 0.55 | 93% of relevant results below this threshold |

### Section-type metadata filtering

Markdown chunks carry `section_type` metadata (abstract, introduction, methods, results, discussion, conclusion, references, acknowledgements, appendix). Use `--where section_type!=references` to exclude reference sections, which account for ~76% of noise in knowledge collections.

```bash
nx search "caching strategy" --where section_type!=references
```

### Corpus-specific over-fetch

Knowledge, docs, and RDR collections fetch 4x the requested result count before filtering (vs 2x for code). This compensates for the higher noise ratio in prose collections, ensuring enough quality results survive threshold filtering.

### Semantic clustering (opt-in)

When `cluster_by="semantic"` is passed to the MCP `search()` tool, results are grouped by Ward hierarchical clustering. Each result gets a `_cluster_label` metadata key identifying its thematic group. Disabled by default — enable globally via `search.cluster_by: semantic` in `.nexus.yml`.

### Catalog pre-filtering

When metadata filters have high selectivity (<5% of documents match), Nexus pre-fetches matching file paths from the catalog SQLite database and passes them as a `source_path` filter to ChromaDB. This avoids HNSW/SPANN stalling in predicate-sparse graph regions. Happens automatically when a catalog is available — no configuration needed.

### Multi-probe collection health

`nx collection verify --deep` probes up to 5 documents per collection and reports a hit rate. A hit rate below 100% indicates degraded retrieval quality — run `nx doctor --fix` (local mode) or re-index the collection.
