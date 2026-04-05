# Document Catalog

The catalog tracks every document nexus knows about and the relationships between them. Think of it as the index card system for your knowledge base — search by metadata, browse by relationship, trace provenance.

While T3 stores document *content* as vector embeddings, the catalog stores document *metadata* (title, author, collection) and *relationships* (citations, implementations, supersedes). Together they answer questions that neither can answer alone: "what cites this paper, and what are those papers about?"

## Setup

```bash
nx catalog setup
```

One command. Creates the catalog, populates it from your existing T3 collections and repos, and generates links from metadata. After this, `search`, `show`, and `links` work immediately.

`nx doctor` will remind you if the catalog isn't set up yet. It's optional — everything else works without it.

## Find documents

```bash
nx catalog search "schema mappings"     # by title
nx catalog search --author Fagin         # by author (from bib enrichment)
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

Both arguments accept titles or tumblers. Link types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `quotes`, `relates`, `comments`.

Duplicate links are merged — the second creator is recorded in `co_discovered_by`. Linking to a deleted or non-existent document is rejected by default.

### Spans (sub-document references)

Links can point to specific passages:

```bash
nx catalog link "Paper A" "Paper B" --type quotes --from-span "100-105" --to-span "42-57"
```

Span formats: `42-57` (line range) or `3:100-250` (chunk 3, characters 100-250). `nx catalog show` resolves span text inline when available.

## How it gets populated

You don't have to register documents manually. Every indexing pathway does it automatically:

| Command | What gets registered |
|---------|---------------------|
| `nx index repo .` | Code files, prose, RDRs — plus auto-generates code→RDR links |
| `nx index pdf paper.pdf` | PDF with title, author, page count |
| `nx index rdr .` | RDR documents with frontmatter titles |
| `nx index md file.md` | Markdown documents |
| `nx enrich <collection>` | Adds Semantic Scholar metadata + enables citation link generation |
| MCP `store_put` | Knowledge entries stored by agents |

After `nx enrich`, run `nx catalog setup` again (or `nx catalog generate-links`) to create citation links from the newly fetched references.

## Agent use

Agents access the catalog through MCP tools. The primary workflow:

1. `catalog_search(query="topic")` — discover which documents and collections are relevant
2. `catalog_links(tumbler="1.8.14", direction="in", link_type="cites")` — traverse the citation graph
3. `search(query="topic", corpus="docs__collection-name")` — search the specific collection

The `/nx:query` skill automates this as a multi-step plan: catalog search → link traversal → scoped semantic search → summarize.

Agents also create links during their work — the debugger creates `relates` links between findings, the developer creates `implements` links to RDRs, the knowledge-tidier creates `supersedes` links when consolidating documents.

## Link types

| Type | Meaning | Created by |
|------|---------|------------|
| `cites` | Citation relationship | `nx enrich` (from Semantic Scholar), agents, manual |
| `implements-heuristic` | Code→RDR (substring title match) | Indexer hook (automatic) |
| `implements` | Code→RDR (confirmed) | Developer agent, manual |
| `supersedes` | Replacement | RDR close, knowledge-tidier, manual |
| `relates` | Related documents | Debugger, deep-analyst, codebase-analyzer, manual |
| `quotes` | Direct quotation (with spans) | Manual |
| `comments` | Commentary | Manual |

Every link carries `created_by` provenance — you can always tell who asserted a relationship and filter by it.

## Architecture

The catalog is a git-backed JSONL append log with a SQLite query cache:

- **JSONL files** (`owners.jsonl`, `documents.jsonl`, `links.jsonl`) are the source of truth
- **SQLite** (`.catalog.db`) is rebuilt automatically when JSONL changes — provides FTS5 search and indexed queries
- **Git** tracks the JSONL history — `nx catalog sync` commits; add a remote for multi-machine sync

Tumblers are permanent hierarchical addresses (e.g., `1.2.5` = store 1, owner 2, document 5). Once assigned, a tumbler is never reused — even after deletion and compaction.

`defrag()` deduplicates JSONL without erasing deletion history (runs automatically during `sync`). `compact()` is a full purge that removes tombstones (explicit admin action).

## Multi-machine sync (optional)

```bash
nx catalog init --remote git@github.com:you/catalog.git
nx catalog sync                # commit + push
nx catalog pull                # pull + rebuild
```

Most users don't need this. The catalog works fine as a local-only store.

## Admin commands

These are hidden from `--help` but available:

```bash
nx catalog link-audit          # orphan detection, stats by type/creator
nx catalog link-bulk-delete    # bulk delete with dry-run preview
nx catalog backfill            # re-populate from T3 (like setup, but without init)
```
