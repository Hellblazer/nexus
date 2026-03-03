# Architecture

> When in doubt, check `src/nexus/` — the code is the ground truth.

## How It Fits Together

Nexus has three layers: a CLI that talks to three storage tiers, an indexing
pipeline that fills them, and a search engine that queries across them.

```
User / Agent
    │
    ▼
CLI (cli.py + commands/)
    │
    ├── Index: classify → chunk → embed → store
    │     code: classify(SKIP|CODE|PROSE|PDF) → tree-sitter AST → context prefix → voyage-code-3 → code__<repo>
    │     prose: SemanticMarkdownChunker (md) or line-split → voyage-context-3 → docs__<repo>
    │     rdr:   SemanticMarkdownChunker → voyage-context-3 → rdr__<repo>
    │     pdf:   PyMuPDF4LLM → voyage-context-3 → docs__<corpus>
    │     skip:  .xml/.json/.yml/.html/.css/.lock/etc → silently ignored
    │
    ├── Search: query → retrieve → rerank → format
    │     semantic, hybrid (+ frecency + ripgrep)
    │
    └── Storage tiers
          T1: ChromaDB HTTP server (session scratch, shared across agent processes)
          T2: SQLite + FTS5 (persistent memory, project context)
          T3: ChromaDB Cloud (four databases) + Voyage AI (permanent knowledge)
                {base}_code      → code__*       voyage-code-3 index / voyage-4 query
                {base}_docs      → docs__*       voyage-context-3 (CCE) index + query
                {base}_rdr       → rdr__*        voyage-context-3 (CCE) index + query
                {base}_knowledge → knowledge__*  voyage-context-3 (CCE) index + query
```

Data flows upward (T1 → T2 → T3).

## Module Map

| Area | Files | What they do |
|------|-------|-------------|
| **Entry** | `cli.py`, `commands/` | Click CLI, one file per command group |
| **Storage** | `db/t1.py`, `db/t2.py`, `db/t3.py` | Tier implementations |
| **Indexing** | `indexer.py`, `classifier.py`, `chunker.py`, `md_chunker.py`, `doc_indexer.py`, `pdf_extractor.py`, `pdf_chunker.py` | Repo indexing pipeline |
| **Search** | `search_engine.py`, `scoring.py`, `frecency.py`, `ripgrep_cache.py` | Query, rank, rerank |
| **Server** | `server.py`, `server_main.py`, `polling.py` | Daemon, HEAD polling, auto-reindex |
| **Support** | `config.py`, `registry.py`, `corpus.py`, `session.py`, `hooks.py`, `ttl.py`, `formatters.py`, `types.py`, `errors.py` | Configuration, naming, formatting, session lifecycle |

## Design Decisions

1. **Protocols over ABCs** — `typing.Protocol` for structural subtyping, no inheritance coupling.
2. **No ORM** — Direct `sqlite3` for T2. Schema is simple; WAL + FTS5 are stdlib.
3. **Constructor injection** — Dependencies via constructor, no global singletons.
4. **Ported, not imported** — SeaGOAT and Arcaneum patterns rewritten in Nexus module structure.
5. **PPID-chain session propagation** — The `SessionStart` hook starts a per-session ChromaDB HTTP server (using the `chroma` entry-point co-installed with the package) and writes its address to `~/.config/nexus/sessions/{ppid}.session`, keyed by the Claude Code process PID. Child agents walk the OS PPID chain to find the nearest ancestor session file and connect to the same server, sharing T1 scratch across the entire agent tree. Concurrent independent windows stay isolated via disjoint process trees. Falls back to `EphemeralClient` when the server cannot start or the PPID chain yields no record.

## Heritage

| Tool | What Nexus borrows |
|------|-------------------|
| **mgrep** | UX patterns, citation format, Claude Code integration |
| **SeaGOAT** | Git frecency scoring, hybrid search, persistent server |
| **Arcaneum** | PDF extraction + chunking pipelines, RDR process |

Storage (ChromaDB + Voyage AI) and embedding layers are Nexus's own.

For the original verbose architecture document, see [historical/architecture.md](historical/architecture.md).
