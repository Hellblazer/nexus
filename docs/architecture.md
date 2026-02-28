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
    │     code: tree-sitter AST → voyage-code-3 → code__<repo>
    │     prose: markdown splitter → voyage-context-3 → docs__<repo>
    │     rdr:   markdown splitter → voyage-context-3 → rdr__<repo>
    │     pdf:   PyMuPDF4LLM → voyage-context-3 → docs__<corpus>
    │
    ├── Search: query → retrieve → rerank → format
    │     semantic, hybrid (+ frecency), answer (+ Haiku), agentic
    │
    └── Storage tiers
          T1: in-memory ChromaDB (session scratch)
          T2: SQLite + FTS5 (persistent memory, PM state)
          T3: ChromaDB cloud + Voyage AI (permanent knowledge)
```

Data flows upward (T1 → T2 → T3). No reverse flow except `nx pm restore`.

## Module Map

| Area | Files | What they do |
|------|-------|-------------|
| **Entry** | `cli.py`, `commands/` | Click CLI, one file per command group |
| **Storage** | `db/t1.py`, `db/t2.py`, `db/t3.py` | Tier implementations |
| **Indexing** | `indexer.py`, `classifier.py`, `chunker.py`, `md_chunker.py`, `doc_indexer.py`, `pdf_extractor.py`, `pdf_chunker.py` | Repo indexing pipeline |
| **Search** | `search_engine.py`, `scoring.py`, `frecency.py`, `ripgrep_cache.py`, `answer.py` | Query, rank, synthesize |
| **Server** | `server.py`, `server_main.py`, `polling.py` | Daemon, HEAD polling, auto-reindex |
| **Support** | `config.py`, `registry.py`, `corpus.py`, `session.py`, `hooks.py`, `ttl.py`, `formatters.py`, `pm.py`, `types.py`, `errors.py` | Configuration, naming, formatting, PM |

## Design Decisions

1. **Protocols over ABCs** — `typing.Protocol` for structural subtyping, no inheritance coupling.
2. **No ORM** — Direct `sqlite3` for T2. Schema is simple; WAL + FTS5 are stdlib.
3. **Constructor injection** — Dependencies via constructor, no global singletons.
4. **Ported, not imported** — SeaGOAT and Arcaneum patterns rewritten in Nexus module structure.
5. **Lazy session ID** — UUID4, generated on first access. File path keyed by `os.getsid(0)` for terminal-session isolation.

## Heritage

| Tool | What Nexus borrows |
|------|-------------------|
| **mgrep** | UX patterns, citation format, Claude Code integration |
| **SeaGOAT** | Git frecency scoring, hybrid search, persistent server |
| **Arcaneum** | PDF extraction + chunking pipelines, RDR process |

Storage (ChromaDB + Voyage AI) and embedding layers are Nexus's own.

For the original verbose architecture document, see [historical/architecture.md](historical/architecture.md).
