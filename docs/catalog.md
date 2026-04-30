# Document Catalog

The catalog tracks every document nexus knows about and the relationships between them. Think of it as the index card system for your knowledge base — search by metadata, browse by relationship, trace provenance.

While T3 stores document *content* as vector embeddings, the catalog stores document *metadata* (title, author, collection) and *relationships* (citations, implementations, supersedes). Together they answer questions that neither can answer alone: "what cites this paper, and what are those papers about?"

## Setup

```bash
nx catalog setup
```

One command. Creates the catalog, populates it from your existing T3 collections and repos, backfills `chunk_text_hash` metadata on any chunks missing it, and generates links from metadata. After this, `search`, `show`, `links`, and content-hash spans all work immediately.

`nx doctor` will remind you if the catalog isn't set up yet. It's optional — everything else works without it.

## Find documents

```bash
nx catalog search "schema mappings"     # by title
nx catalog search "Fagin"               # matches title, author, corpus, file path
nx catalog list                          # browse all entries
nx catalog list --type paper             # filter by content type
```

This is metadata search (fast, exact), not semantic search. Use `nx search` for content-level queries. Use `nx catalog search` to find *which documents exist* and *which T3 collections they're in*.

## Show a document

```bash
nx catalog show "Inverting Schema Mappings"
nx catalog show 1.8.14                   # same thing, by tumbler
```

Shows full metadata plus all links in and out. The tumbler is a permanent address — once assigned, it never changes, even if the document is deleted and the catalog is compacted.

## Explore relationships

```bash
nx catalog links "Core Schema Mappings"              # all links to/from
nx catalog links "Core Schema Mappings" --depth 2    # two-hop traversal
nx catalog links --type cites                        # all citation links
nx catalog links --created-by bib_enricher           # links by creator
```

Without a positional argument, `links` queries the full link table with filters. With a positional argument, it does BFS graph traversal from that document.

## Create links

```bash
nx catalog link "Paper A" "Paper B" --type cites
nx catalog link 1.1.5 1.2.3 --type implements
```

Both arguments accept titles or tumblers. Link types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `quotes`, `relates`, `comments`, `formalizes`.

Duplicate links are merged — the second creator is recorded in `co_discovered_by`. Linking to a deleted or non-existent document is rejected by default.

### Spans (sub-document references)

Links can point to specific passages:

```bash
# Content-addressed span (preferred — survives re-indexing):
nx catalog link "Paper A" "Paper B" --type quotes \
  --from-span "chash:a1b2c3d4e5f6...64hexchars"

# Positional spans (legacy):
nx catalog link "Paper A" "Paper B" --type quotes --from-span "100-105" --to-span "42-57"
```

Span formats:

- `42-57` — line range (positional)
- `3:100-250` — chunk 3, characters 100-250 (positional)
- `chash:<sha256hex>` — content-addressed chunk identity (64-char SHA-256 hex, preferred)
- `chash:<sha256hex>:<start>-<end>` — character range within a content-addressed chunk

Content-hash spans (`chash:`) survive re-indexing when chunk boundaries are unchanged. Position-based spans may become stale after re-indexing — `nx catalog link-audit` detects this. `nx catalog show` resolves span text inline when available.

## How it gets populated

You don't have to register documents manually. Every indexing pathway does it automatically:

| Command | What gets registered |
|---------|---------------------|
| `nx index repo .` | Code files, prose, RDRs — plus auto-generates code→RDR links and topic links |
| `nx index pdf paper.pdf` | PDF with title, author, page count |
| `nx index rdr .` | RDR documents with frontmatter titles |
| `nx index md file.md` | Markdown documents |
| `nx enrich bib <collection>` | Adds Semantic Scholar metadata + enables citation link generation |
| `nx enrich aspects <collection>` | Extracts structured per-paper aspects (RDR-089) into T2 ``document_aspects`` |
| MCP `store_put` | Knowledge entries stored by agents |

After `nx enrich bib`, run `nx catalog setup` again (or `nx catalog generate-links`) to create citation links from the newly fetched references.

### Taxonomy and topic links

After each `nx index repo` run, the indexer auto-triggers `compute_topic_links` (from `nexus.commands.taxonomy_cmd`) for each indexed collection. This computes inter-topic link edges — which topics co-occur in documents — and persists them to the `topic_links` T2 table. The search engine uses these edges for semantic clustering. No manual step needed; the catalog must be initialized for topic links to populate.

## Agent use

Agents access the catalog through the enhanced `query` MCP tool, which handles catalog routing internally:

```
query(question="schema mappings", author="Fagin")           # author-scoped
query(question="indexing pipeline", content_type="code")     # type-scoped
query(question="architecture", subtree="1.1")                # subtree-scoped
query(question="related work", follow_links="cites")         # citation-enriched
```

For simple scoped queries, `query()` with catalog params is a single MCP call — no agent dispatch needed. For complex analytical queries (compare, extract, generate), call `mcp__plugin_nx_nexus__nx_answer` directly — it runs `plan_match` against the library, executes the best match via `plan_run`, and falls through to an inline planner on miss. The `/nx:query` skill is now a pointer to `nx_answer` (RDR-080 consolidation — replaces the earlier query-planner + analytical-operator agent pair).

Individual catalog MCP tools on the `nexus-catalog` server use short names without the `catalog_` prefix (since RDR-062). The 10 registered tools are: `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats`. Full names follow the pattern `mcp__plugin_nx_nexus-catalog__<tool>` (e.g., `mcp__plugin_nx_nexus-catalog__search`). Three operations (`unlink`, `link_audit`, `link_bulk`) are demoted — they are plain Python functions not exposed on the MCP surface.

Agents also create links during their work — the debugger creates `relates` links between findings, the developer creates `implements` links to RDRs, and the `nx_tidy` MCP tool (formerly the knowledge-tidier agent, RDR-080) creates `supersedes` links when consolidating documents.

## Link types

| Type | Meaning | When to use | Created by |
|------|---------|-------------|------------|
| `cites` | Citation reference | Paper A references Paper B | `nx enrich bib` (auto), agents, manual |
| `implements-heuristic` | Code→RDR (auto-detected) | Indexer found title substring match | Indexer hook (automatic) |
| `implements` | Code→RDR (confirmed) | Code intentionally realizes a design doc | Developer agent, manual |
| `supersedes` | Document replaced by another | Retiring old doc, consolidating duplicates | RDR close, `nx_tidy` MCP tool, manual |
| `relates` | Related findings | Cross-cutting concerns, similar topics | Debugger, deep-analyst, manual |
| `quotes` | Direct quotation with spans | Citing a specific passage as evidence | Manual |
| `comments` | Commentary or annotation | Metadata notes about a document | Manual |
| `formalizes` | Progressive formalization | A higher-level representation (e.g., extracted entities, RDF triples) formalizes a raw L0 text chunk | RDR-057 progressive formalization, agents |

**Choosing the right type:** Use `cites` for bibliographic references. Use `implements` when code directly realizes a design (not just mentions it — that's `relates`). Use `supersedes` when one document fully replaces another. Use `quotes` when you need to pin a specific passage with a span reference. Use `formalizes` when creating a higher-abstraction representation that points back to its raw source (multi-representation equivalence per RDR-057).

Every link carries `created_by` provenance — you can always tell who asserted a relationship and filter by it:

```bash
nx catalog links --created-by bib_enricher    # all auto-generated citation links
nx catalog links --created-by user            # all manually created links
```

## Span lifecycle and staleness

Spans identify specific passages within documents. The three formats have different durability characteristics:

| Format | Survives re-indexing? | When to use |
|--------|----------------------|-------------|
| `42-57` (line range) | No — line numbers shift if file content changes | Quick references to source files you control |
| `3:100-250` (chunk:char) | No — chunk boundaries shift on re-indexing | Rare; prefer chash: instead |
| `chash:<sha256hex>` | Yes, if chunk text is unchanged | Preferred for all durable references |
| `chash:<sha256hex>:<start>-<end>` | Yes, if chunk text is unchanged | Character range within a content-addressed chunk |

**What happens when spans go stale:** If you re-index a document, positional spans (`42-57`, `3:100-250`) may point to the wrong text. Content-hash spans (`chash:`) continue to resolve correctly as long as the chunk text hasn't changed.

**Detecting stale spans:** Run `nx catalog link-audit` (the `catalog_link_audit` operation is CLI-only since RDR-062 — demoted from the MCP surface). The audit reports:
- `stale_spans` — positional spans on documents that were re-indexed after the link was created
- `stale_chash` — content-hash spans that no longer resolve to any chunk in T3 (chunk was deleted or text changed)

Each `stale_chash` entry includes a `reason` field: `missing` (chunk deleted), `document_deleted`, or `error` (infrastructure issue).

**Recommendation:** Use `chash:` spans for any link you expect to survive re-indexing. For existing positional spans, they continue to work — `link-audit` will flag them if the underlying document changes.

## How it's stored

The catalog lives in its own git repository at `~/.config/nexus/catalog/`. You never need to touch this directory — `nx catalog setup` creates it, and all commands manage it internally.

```
~/.config/nexus/catalog/
  .git/                  # auto-created, tracks JSONL history
  owners.jsonl           # registered repos, curators, knowledge sources
  documents.jsonl        # every indexed document with metadata
  links.jsonl            # every typed link between documents
  .catalog.db            # SQLite query cache (gitignored, rebuilt automatically)
  .gitignore             # excludes .catalog.db
```

**JSONL is the truth.** The three `.jsonl` files are append-only logs. Every registration, update, link creation, and deletion appends a line. SQLite is a disposable query cache — if it disappears, the system rebuilds it from JSONL on the next access.

**Git is the history.** `nx catalog sync` commits the current JSONL state. This gives you version history for free — you can always see when a document was registered, when a link was created, or roll back a bad change with standard git tools. If you never call `sync`, the catalog still works — git is the durability layer, not the operational layer.

**SQLite is the speed.** FTS5 full-text search, indexed link queries, and graph traversal all run against SQLite. It's rebuilt automatically whenever JSONL files change (detected by mtime).

### Tumbler addressing

Every document gets a permanent hierarchical address called a **tumbler**. The format is `store.owner.document[.chunk]`:

| Segment | Meaning | Example |
|---------|---------|---------|
| Store | Installation (always `1` for a single nexus instance) | `1` |
| Owner | Repository or knowledge source (auto-assigned per repo) | `1.2` |
| Document | Sequential number within the owner (monotonically increasing) | `1.2.5` |
| Chunk | Optional — specific chunk within the document | `1.2.5.3` |

**Examples:**
- `1.1.42` — Document 42 from owner 1 (e.g., your main repo)
- `1.2.5.3` — Chunk 3 of document 5 from owner 2 (e.g., a paper collection)
- `1.1` — Owner-level address (refers to all documents under that owner)

Tumblers are assigned once and never reused. If you delete document `1.2.5` and compact the catalog, the number 5 is retired — the next document under that owner gets `1.2.6`. This means any external reference to a tumbler remains valid indefinitely.

Tumbler comparison uses integer ordering with parent-before-child semantics: `1.1.3 < 1.1.3.0 < 1.1.10`. The `sorted()` function produces correct document ordering.

### Compaction

Over time, JSONL files accumulate overwrites and tombstones. Two compaction modes:

- **`defrag()`** — deduplicates overwrites but keeps tombstones (deletion markers). Runs automatically during `nx catalog sync` when files exceed 3x the live record count. Safe — no history lost.
- **`compact()`** — removes everything except live records, including tombstones. Explicit admin action via `nx catalog compact`. Erases deletion history from JSONL (though git preserves it).

## Durability and remote sync

**Local mode users** (ONNX embeddings, no cloud): the catalog is as durable as your disk. If that's fine, you're done.

**Cloud mode users** (ChromaDB Cloud + Voyage AI): your T3 content lives in the cloud, but the catalog — the only record of what's indexed, how documents relate, and what their tumblers are — is local. If you lose the disk, the catalog is gone. T3 content survives but you'd have to `backfill` to reconstruct the registry, and all links would be lost.

**Fix this by adding a git remote:**

```bash
# Create a private repo (GitHub, GitLab, etc.) then:
nx catalog init --remote git@github.com:you/nexus-catalog.git

# Or add a remote to an existing catalog:
cd ~/.config/nexus/catalog
git remote add origin git@github.com:you/nexus-catalog.git
```

The catalog auto-syncs at session close — if JSONL files have changed, the Stop hook runs `nx catalog sync` automatically. This commits locally and pushes to the remote if one is configured. No manual sync needed during normal use.

For manual sync:

```bash
nx catalog sync                # commit + push to remote
nx catalog pull                # pull from remote + rebuild SQLite
```

**New machine restore**: `nx catalog setup --remote <url>` clones from the remote instead of creating an empty catalog. Your tumblers, links, and full document registry are restored instantly.

**CI/ephemeral environments**: configure `NEXUS_CATALOG_PATH` to point at a persistent volume, or use `init --remote` on each run to clone from the remote. The catalog rebuilds SQLite from JSONL in milliseconds.

## Admin and maintenance

### Link health

```bash
nx catalog link-audit
```

Reports link graph health: total counts by type and creator, orphaned links (pointing to deleted documents), duplicate links, stale positional spans, and stale content-hash spans. When T3 is available, verifies each `chash:` span resolves to an actual chunk.

Orphaned links are kept as historical record — they are not auto-deleted. Use `link-bulk-delete` to clean them up if needed.

### Cross-project source_uri guard (nexus-3e4s)

`Catalog.register()` and `Catalog.update()` enforce a register-time invariant: for `repo` owners with a `repo_root`, the entry's `source_uri` must resolve inside that root. A `file://` URI that lands outside the owner's tree raises a `ValueError` with both URIs in the message. This is the load-bearing guard against the contamination class that produced ~6,500 mis-attributed rows in the wild: entries whose `source_uri` pointed at one project's tree but were attributed to a different project's owner, silently breaking aspect extraction.

The guard skips:

- Curator owners (legitimately span sources: papers, mirrored docs, etc.).
- Pre-RDR-060 repo owners with empty `repo_root` (back-compat).
- Non-`file://` URIs (`chroma://`, `https://`, `x-devonthink-item://`). They have no filesystem identity to compare.
- Empty `source_uri`. Synthesized records with no path identity.

To detect or remediate pre-existing contamination see [`nx catalog audit-membership`](cli-reference.md#nx-catalog-audit-membership), including `--all-collections` for a single-shot health check across the entire catalog.

Set `NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1` to bypass the guard for emergency recovery only. Never the right answer for normal indexing.

### Backfill and recovery

```bash
nx catalog backfill            # re-populate catalog from T3 + backfill chunk_text_hash
nx collection backfill-hash    # backfill chunk_text_hash on one collection (or --all)
```

`backfill` re-discovers documents from existing T3 collections and registered repos without re-indexing. Also adds `chunk_text_hash` metadata to any chunks missing it. Use after data recovery or if the catalog gets out of sync with T3.

`backfill-hash` is the targeted version — updates metadata on a single collection without touching embeddings or documents.

### Bulk operations

```bash
nx catalog link-bulk-delete --type implements-heuristic --created-by indexer --dry-run
```

Preview and bulk-delete links by type and/or creator. Always use `--dry-run` first.

### Discovery and observability

```bash
nx catalog orphans --no-links           # entries with zero links
nx catalog coverage                     # % of entries with links, by content type
nx catalog coverage --owner 1.1         # scoped to a specific repo
nx catalog suggest-links --limit 20     # unlinked code-RDR pairs by name overlap
nx catalog links-for-file src/foo.py    # all docs linked to a file
nx catalog session-summary              # recently modified files + linked RDRs
```

Use `coverage` to track link graph completeness. Use `orphans` to find documents that need linking. `links-for-file` shows the design context for any source file.

### Link generation

```bash
nx catalog link-generate                # full batch scan — all linkers
nx catalog link-generate --dry-run      # preview without creating
```

Normal `nx index repo` runs generate links incrementally (only for newly indexed files). Use `link-generate` for the full O(n×m) batch scan after bulk imports or initial setup.

### Housekeeping

```bash
nx catalog gc                           # delete entries missed in 2+ index runs
nx catalog gc --dry-run                 # preview
nx catalog compact                      # remove tombstones from JSONL
```

The indexer automatically tracks `miss_count` for each catalog entry. Files deleted or renamed are detected: renames (same content hash at a new path) transfer links to the new entry; true deletions are evicted after 2 consecutive missed index runs. `gc` provides the manual escape hatch.

### Path migration

```bash
nx doctor --fix-paths --dry-run         # preview absolute→relative migration
nx doctor --fix-paths                   # apply migration (catalog + T3 source_path)
```

After upgrading to RDR-060, run `--fix-paths` once to migrate existing absolute `file_path` entries to relative paths. Curator-owned entries (PDFs, standalone docs) are skipped — they keep absolute paths by design.

### Troubleshooting

**SQLite cache disappeared:** Delete `.catalog.db` (or let it be deleted). The system rebuilds it from JSONL on next access — no data lost.

**JSONL and SQLite disagree:** Delete `.catalog.db` and let it rebuild. JSONL is always the source of truth.

**Links point to deleted documents:** Run `nx catalog link-audit` to find orphans. Decide whether to keep them (historical record) or delete with `link-bulk-delete`.

**Spans show wrong text after re-indexing:** Positional spans (`42-57`) become stale when file content changes. Run `link-audit` to identify stale spans. Prefer `chash:` spans for durable references.
