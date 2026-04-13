# Topic Taxonomy

Nexus automatically discovers topic clusters across your indexed documents, labels them with human-readable names, and uses them to improve search quality. This guide covers how to use taxonomy, how to configure it, and how it works under the hood.

## Quick start

```bash
# First time (after installing or upgrading)
nx taxonomy discover --all        # discover topics for all T3 collections

# After any nx index repo (automatic)
nx taxonomy status                # see what was discovered

# Curate
nx taxonomy review                # interactive: accept, rename, merge, delete, skip
nx taxonomy label                 # batch re-label with Claude haiku
```

## Commands

| Command | Description |
|---------|-------------|
| `nx taxonomy status` | Overview: collections, topic count, coverage, review state |
| `nx taxonomy discover --all` | Discover topics for all eligible collections |
| `nx taxonomy discover -c NAME` | Discover for a single collection (`--force` to re-cluster) |
| `nx taxonomy list` | Topic tree with doc counts |
| `nx taxonomy show ID` | Documents assigned to a topic |
| `nx taxonomy review` | Interactive review: accept, rename, merge, delete, skip |
| `nx taxonomy label` | Batch-relabel pending topics with Claude haiku |
| `nx taxonomy assign DOC LABEL` | Manually assign a document to a topic |
| `nx taxonomy rename OLD NEW` | Rename a topic |
| `nx taxonomy merge SOURCE TARGET` | Merge source topic into target |
| `nx taxonomy split LABEL --k N` | Split a topic into N sub-topics via KMeans |
| `nx taxonomy links` | Show inter-topic relationships from catalog link graph |
| `nx taxonomy rebuild -c NAME` | Full rebuild with merge strategy (preserves operator labels) |

## Search integration

Topic taxonomy improves search through three mechanisms.

### Topic boost

Results that share a topic cluster with other results receive a distance reduction:
- Same topic: -0.1
- Linked topics (via catalog link graph): -0.05

This is automatic. No parameter needed. It promotes coherent result sets.

### Topic grouping

When using the `search()` MCP tool with `cluster_by="semantic"` (the default for agents), Nexus groups results by topic label:

```
── ChromaDB Transient Retry Logic ──
[0.12] rdr-019-chromadb-transient-retry.md  ...
[0.15] rdr-020-voyage-chromadb-read-timeout.md  ...

── Math-aware PDF Extraction ──
[0.18] rdr-044-math-aware-pdf-extraction.md  ...
[0.21] rdr-046-mineru-server-backed-extraction.md  ...
```

When topic coverage is below 50%, Nexus falls back to Ward hierarchical clustering.

### Topic-scoped search

Pre-filter results to a specific topic cluster:

```python
search(query="extraction pipeline", topic="Math-aware PDF Extraction")
```

Use `nx taxonomy list` to see available topic labels.

## Configuration

In `.nexus.yml`:

```yaml
taxonomy:
  auto_label: true                       # Label with Claude haiku (default: true)
  local_exclude_collections: ["code__*"] # Skip code in local mode (default)
```

| Key | Default | Description |
|-----|---------|-------------|
| `auto_label` | `true` | Auto-label topics with Claude haiku after discover. Requires the `claude` CLI. |
| `local_exclude_collections` | `["code__*"]` | Glob patterns to skip in local mode. MiniLM embeddings cluster poorly on code. Cloud mode ignores this setting. Set to `[]` to enable all collections locally. |

## Local vs cloud quality

| Mode | Embedding model | Code quality | Document quality |
|------|----------------|-------------|-----------------|
| Local | MiniLM 384d (ONNX) | Poor (excluded by default) | Good (8 topics from 120 docs) |
| Cloud | Voyage 1024d | Excellent (124 topics from 5K chunks) | Excellent (88 topics, 78% assigned) |

## Operator curation

### Interactive review

```bash
nx taxonomy review --limit 15
```

For each topic, Nexus shows the label, doc count, c-TF-IDF terms (keywords that distinguish this cluster), and sample documents. Actions:

- **[a]ccept** — mark as reviewed
- **[r]ename** — provide a new label
- **[m]erge** — merge into another topic
- **[d]elete** — remove the topic and its assignments
- **[S]kip** — leave for later

### Label preservation

When you rebuild taxonomy (`nx taxonomy rebuild --force`), Nexus preserves your operator-curated labels via a centroid-matching merge strategy:

1. Nexus reads old centroids before clearing
2. HDBSCAN re-clusters the (possibly changed) corpus
3. Each new centroid is matched to the nearest old centroid (cosine similarity)
4. If similarity > 0.8, the old label transfers to the new topic
5. Manual assignments (`assigned_by='manual'`) are routed to the matching new topic

Your curation work survives corpus changes.

## How it works

After `nx index repo` (or `nx taxonomy discover --all`), Nexus runs four steps:

1. **Fetches embeddings** from each T3 collection (Voyage on cloud, MiniLM on local)
2. **Clusters** documents via HDBSCAN density-based clustering (`min_cluster_size=5`)
3. **Labels** each cluster with c-TF-IDF keywords, then refines the label via Claude haiku (when the `claude` CLI is available)
4. **Stores** topics in T2 (SQLite) and cluster centroids in a ChromaDB collection (`taxonomy__centroids`, cosine space)

From then on:

- Every `store_put` MCP call auto-assigns the new document to its nearest topic via centroid lookup
- Every search call boosts results that share a topic cluster
- With `cluster_by="semantic"`, search results are grouped by topic label when more than 50% have assignments

## Upgrading

For existing users with already-indexed collections:

```bash
uv sync                           # install new version
nx taxonomy discover --all        # backfill topics for existing collections
nx taxonomy status                # verify
```

The `--all` flag scans every T3 collection, discovers topics, and auto-labels them. This is a one-time operation. Subsequent `nx index repo` calls trigger discovery automatically.
