# Nexus Architecture

> **Implementation Status**: Sections marked `[v1-actual]` reflect the current implementation.
> Sections marked `[v2-planned]` describe aspirational design not yet implemented.
> When in doubt, check `src/nexus/` — the code is the ground truth.

## Executive Summary

Nexus is a self-hosted semantic search and knowledge system implemented in Python 3.12+.
It replaces expensive cloud ingest (Mixedbread) with locally-controlled indexing pipelines
while keeping ChromaDB in the cloud as the permanent knowledge store. The architecture
synthesizes patterns from mgrep (UX, citations), SeaGOAT (git frecency, hybrid search,
persistent server), and Arcaneum (PDF/markdown extraction and chunking).

### Objectives

1. Three-tier storage: ephemeral scratch (T1), local SQLite memory (T2), cloud ChromaDB (T3)
2. Indexing pipelines for code repos, PDFs, and markdown with appropriate embedding models
3. Hybrid search combining semantic vectors with ripgrep full-text and git frecency scoring
4. Project management lifecycle with archive synthesis for institutional memory
5. Claude Code integration via SKILL.md, hooks, and slash commands

---

## Module/Package Structure

### [v1-actual] Current Flat Layout

The implementation uses a flat module layout, not the hierarchical subpackages described
in the v2-planned section below. This is the ground truth as of the current codebase.

```
src/nexus/
|-- __init__.py
|-- __main__.py
|-- answer.py                      # answer synthesis (Haiku-based Q&A with citations)
|-- chunker.py                     # code chunker (AST via llama-index CodeSplitter)
|-- cli.py                         # Click CLI entry point
|-- config.py                      # config loading (YAML, env vars, merging)
|-- corpus.py                      # corpus utilities
|-- doc_indexer.py                 # document indexing orchestration
|-- errors.py                      # custom exception hierarchy (see below)
|-- formatters.py                  # output formatters (plain, JSON, vimgrep, etc.)
|-- frecency.py                    # git frecency scoring (ported from SeaGOAT)
|-- hooks.py                       # SessionStart/SessionEnd hook logic
|-- indexer.py                     # indexing pipeline orchestrator
|-- md_chunker.py                  # SemanticMarkdownChunker (ported from Arcaneum)
|-- pdf_chunker.py                 # PDF chunker (ported from Arcaneum)
|-- pdf_extractor.py               # PyMuPDF4LLM + pdfplumber + OCR
|-- pm.py                          # project management lifecycle
|-- polling.py                     # HEAD hash polling, re-index triggers
|-- registry.py                    # repo registry (~/.config/nexus/repos.json)
|-- ripgrep_cache.py               # mmap line cache (ported from SeaGOAT)
|-- scoring.py                     # scoring primitives (min_max_normalize, etc.)
|-- search_engine.py               # search orchestration (~175 lines, see [v1-actual] below)
|-- server.py                      # Flask app factory and routes
|-- server_main.py                 # server entry point / daemonization
|-- session.py                     # session ID management (PID-scoped file)
|-- ttl.py                         # TTL sentinel utilities
|-- types.py                       # shared dataclasses (Chunk, SearchResult, etc.)
|
|-- commands/                      # Click CLI command modules
|   |-- __init__.py
|   |-- collection.py              # nx collection list/info/delete/verify
|   |-- config_cmd.py              # nx config show/set
|   |-- index.py                   # nx index code/pdf/md
|   |-- memory.py                  # nx memory put/get/search/list/expire/promote
|   |-- pm.py                      # nx pm (all lifecycle subcommands)
|   |-- scratch.py                 # nx scratch put/get/search/list/clear/flag/promote
|   |-- search_cmd.py              # nx search
|   |-- serve.py                   # nx serve start/stop/status/logs
|   +-- store.py                   # nx store, nx store expire
|
+-- db/                            # Storage tier implementations
    |-- __init__.py
    |-- t1.py                      # EphemeralClient + DefaultEmbeddingFunction
    |-- t2.py                      # SQLite + FTS5 + WAL
    +-- t3.py                      # CloudClient + VoyageAIEmbeddingFunction
```

### [v2-planned] Aspirational Hierarchical Layout

The following hierarchical structure was the original design intent. It has NOT been
implemented. The subpackages `protocols/`, `storage/`, `indexing/`, `search/`, `answer/`,
`pm/`, `server/`, `cli/`, `formatting/`, and `integration/` do not exist.
Note: `answer.py`, `formatters.py`, and `scoring.py` NOW EXIST as flat modules (partial
progress toward the aspirational layout), but the full subpackage structure remains unimplemented.

```
src/nexus/
|-- __init__.py                    # version, package metadata
|-- __main__.py                    # entry point for `python -m nexus`
|-- types.py                       # shared dataclasses (Chunk, SearchResult, etc.)
|-- errors.py                      # custom exception hierarchy
|-- config.py                      # config loading (YAML, env vars, merging)
|-- session.py                     # session ID management (UUID4)
|
|-- protocols/                     # ABCs and Protocols (NO implementations)
|   |-- __init__.py
|   |-- storage.py                 # MemoryStore, VectorStore Protocols
|   |-- embedding.py               # EmbeddingFunction Protocol
|   |-- chunking.py                # ChunkStrategy Protocol
|   |-- search.py                  # SearchPipeline Protocol
|   |-- indexing.py                # IndexPipeline Protocol
|   +-- formatting.py              # ResultFormatter Protocol
|
|-- storage/                       # Storage tier implementations
|   |-- __init__.py                # tier factory functions
|   |-- t1_ephemeral.py            # EphemeralClient + DefaultEmbeddingFunction
|   |-- t2_sqlite.py               # SQLite + FTS5 + WAL
|   +-- t3_cloud.py                # CloudClient + VoyageAIEmbeddingFunction
|
|-- indexing/                      # Indexing pipelines
|   |-- __init__.py
|   |-- code/                      # Code indexing pipeline
|   |   |-- __init__.py
|   |   |-- pipeline.py            # CodeIndexPipeline orchestrator
|   |   |-- frecency.py            # git frecency scoring (ported from SeaGOAT)
|   |   |-- chunker.py             # AST chunker via llama-index CodeSplitter
|   |   +-- ripgrep_cache.py       # mmap line cache (ported from SeaGOAT)
|   |-- pdf/                       # PDF indexing pipeline (ported from Arcaneum)
|   |   |-- __init__.py
|   |   |-- pipeline.py            # PDFIndexPipeline orchestrator
|   |   |-- extractor.py           # PyMuPDF4LLM + pdfplumber + OCR
|   |   |-- chunker.py             # PDF chunker
|   |   +-- ocr.py                 # OCR engine (Tesseract/EasyOCR)
|   +-- markdown/                  # Markdown indexing pipeline
|       |-- __init__.py
|       |-- pipeline.py            # MarkdownIndexPipeline orchestrator
|       +-- chunker.py             # SemanticMarkdownChunker (ported from Arcaneum)
|
|-- search/                        # Search subsystem
|   |-- __init__.py
|   |-- semantic.py                # ChromaDB vector search (T1/T3)
|   |-- fulltext.py                # ripgrep line cache search
|   |-- hybrid.py                  # hybrid scoring (0.7*vector + 0.3*frecency)
|   |-- cross_corpus.py            # cross-corpus retrieval + reranking
|   |-- agentic.py                 # multi-step query refinement (Haiku)
|   |-- mxbai.py                   # Mixedbread fan-out (read-only)
|   +-- scoring.py                 # min_max_normalize, scoring utilities
|
|-- answer/                        # Q&A synthesis
|   |-- __init__.py
|   +-- synthesizer.py             # Haiku-based answer synthesis with citations
|
|-- pm/                            # Project management lifecycle
|   |-- __init__.py
|   |-- lifecycle.py               # init/archive/restore/close state machine
|   |-- templates.py               # embedded PM document templates
|   +-- synthesis.py               # Haiku archive synthesis
|
|-- server/                        # Persistent server
|   |-- __init__.py
|   |-- app.py                     # Flask app factory
|   |-- daemon.py                  # daemonize, PID management
|   |-- registry.py                # repo registry (~/.config/nexus/repos.json)
|   +-- polling.py                 # HEAD hash polling, re-index triggers
|
|-- cli/                           # Click CLI commands
|   |-- __init__.py
|   |-- main.py                    # nx group + top-level commands
|   |-- search_cmd.py              # nx search
|   |-- memory_cmd.py              # nx memory put/get/search/list/expire/promote
|   |-- store_cmd.py               # nx store, nx store expire
|   |-- scratch_cmd.py             # nx scratch put/get/search/list/clear/flag/promote
|   |-- index_cmd.py               # nx index code/pdf/md
|   |-- serve_cmd.py               # nx serve start/stop/status/logs
|   |-- pm_cmd.py                  # nx pm (all lifecycle subcommands)
|   |-- collection_cmd.py          # nx collection list/info/delete/verify
|   |-- config_cmd.py              # nx config show/set
|   |-- doctor_cmd.py              # nx doctor
|   +-- install_cmd.py             # nx install/uninstall claude-code
|
|-- formatting/                    # Output formatting
|   |-- __init__.py
|   |-- plain.py                   # plain text, pipe-friendly
|   |-- highlighted.py             # bat/pygments syntax highlighting
|   |-- vimgrep.py                 # path:line:col:content
|   |-- json_fmt.py                # JSON output
|   +-- citations.py               # <cite i="N"> formatting for answer mode
|
+-- integration/                   # External tool integration
    |-- __init__.py
    |-- claude_code/               # Claude Code plugin
    |   |-- __init__.py
    |   |-- installer.py           # SKILL.md + hooks installation
    |   |-- hooks.py               # SessionStart/SessionEnd hook logic
    |   +-- skill_template.py      # SKILL.md template content
    +-- git_hooks.py               # post-commit hook for nx serve notification
```

### [v1-actual] Module Responsibility Summary

| Module | Responsibility |
|--------|---------------|
| `db/t1.py` | In-memory ChromaDB scratch (T1) |
| `db/t2.py` | SQLite + FTS5 memory bank (T2) |
| `db/t3.py` | ChromaDB cloud knowledge store (T3) |
| `indexer.py` | Code repo indexing pipeline (tree-sitter AST chunking + frecency) |
| `doc_indexer.py` | PDF and markdown indexing pipeline |
| `pdf_extractor.py` / `pdf_chunker.py` | PDF text extraction and chunking |
| `md_chunker.py` | Semantic markdown chunking |
| `search_engine.py` | Semantic, hybrid, cross-corpus, agentic search + reranking |
| `answer.py` | Haiku answer synthesis with citations |
| `scoring.py` | Frecency + hybrid score computation |
| `formatters.py` | Plain, vimgrep, JSON output formatting |
| `pm.py` | Project management lifecycle (T2 + T3) |
| `server.py` / `server_main.py` | Flask server + daemonization entry point |
| `registry.py` / `polling.py` | Repo registry and HEAD polling |
| `session.py` | Session ID management (`os.getsid(0)`) |
| `config.py` | Config loading, credential management, env var overrides |
| `corpus.py` | Collection name resolution and namespacing |
| `chunker.py` | Base code chunking via tree-sitter |
| `frecency.py` | Git-based frecency score computation |
| `ripgrep_cache.py` | Ripgrep index cache for hybrid search |
| `hooks.py` | SessionStart / SessionEnd hook logic |
| `ttl.py` | TTL string parsing (`30d`, `4w`, `permanent`, `never`) |
| `types.py` | Shared dataclasses and type definitions |
| `errors.py` | Custom exception hierarchy |
| `commands/` | Click CLI command definitions (one file per command group) |

### [v2-planned] Module Responsibility Summary

This table reflects the aspirational hierarchical layout. In the v1-actual flat layout,
the responsibilities map to: `db/t1.py`, `db/t2.py`, `db/t3.py` (storage tiers),
`scoring.py`, `answer.py`, `formatters.py` (search/formatting), `pm.py` (lifecycle),
`server.py` / `server_main.py` (server), `commands/` (CLI), and flat modules for indexing.

| Module | Responsibility | Dependencies |
|--------|---------------|--------------|
| `protocols/` | Abstract interfaces only | `types.py` only |
| `storage/t1_ephemeral.py` | In-memory ChromaDB scratch | `protocols/`, `types.py`, `chromadb` |
| `storage/t2_sqlite.py` | SQLite + FTS5 memory bank | `protocols/`, `types.py`, `sqlite3` (stdlib) |
| `storage/t3_cloud.py` | ChromaDB cloud knowledge | `protocols/`, `types.py`, `chromadb`, `voyageai` |
| `indexing/code/` | Code repo indexing | `protocols/storage.py (VectorStore)`, `protocols/`, git, llama-index |
| `indexing/pdf/` | PDF extraction + chunking | `protocols/storage.py (VectorStore)`, `protocols/`, pymupdf4llm |
| `indexing/markdown/` | Markdown chunking | `protocols/storage.py (VectorStore)`, `protocols/`, markdown-it-py |
| `search/` | All search strategies | `storage/`, `scoring.py` |
| `answer/` | Haiku synthesis | `search/`, `anthropic` SDK |
| `pm/` | Project management lifecycle | `storage/t2_sqlite.py`, `storage/t3_cloud.py`, `answer/` |
| `server/` | Flask/Waitress persistent process | `indexing/`, `search/`, `storage/` |
| `cli/` | Click command definitions | Everything above (leaf layer) |
| `formatting/` | Output rendering | `types.py` only |
| `integration/` | External tool hooks | `config.py`, `session.py` |

### [v2-planned] Dependency Rules (aspirational import structure)

These rules describe the intended architecture for the hierarchical subpackage layout.
In the v1-actual flat layout, these rules are approximated but not formally enforced via
import-linter (the `.importlinter` configuration file does not yet exist).

1. `protocols/` imports NOTHING from `storage/`, `indexing/`, `search/`, `cli/`
2. `storage/` imports only from `protocols/` and `types.py`
3. `indexing/` imports from `protocols/` only — **not** from `storage/t3_cloud.py` directly; receives a `VectorStore` protocol via constructor injection from `cli/index_cmd.py`
4. `search/` imports from `storage/` and `protocols/` (never from `indexing/` or `cli/`)
5. `cli/` imports from everything but NOTHING imports from `cli/`
6. `formatting/` imports only from `types.py`
7. No circular dependency paths exist

These rules are intended to be enforced by `import-linter` (`dev` dependency). Configuration in `.importlinter`. Run `lint-imports` as part of the CI gate alongside pytest, mypy, and ruff.

---

## Core Abstractions (Protocols) [v2-planned]

The `protocols/` subpackage described in this section does not exist in the v1-actual
flat layout. The protocol interfaces below document the intended contracts. In practice,
the v1-actual implementation wires dependencies via direct imports within the flat module
structure rather than formal Protocol injection. These definitions remain as the
aspirational interface specification.

### StorageTier Protocols

T1/T3 both use ChromaDB but differ in client type and embedding function.
T2 uses SQLite. Rather than forcing a single interface, we define tier-appropriate protocols.

```python
# protocols/storage.py

from typing import Protocol, runtime_checkable
from nexus.types import MemoryEntry, VectorResult, SearchResult

@runtime_checkable
class MemoryStore(Protocol):
    """T2 SQLite memory bank operations."""

    def put(self, project: str, title: str, content: str, *,
            tags: str = "", ttl: int | None = 30,
            session: str = "", agent: str = "") -> int:
        """Insert or replace a memory entry. Returns row ID."""
        ...

    def get(self, project: str, title: str) -> MemoryEntry | None:
        """Retrieve by (project, title) key. Primary access pattern."""
        ...

    def get_by_id(self, id: int) -> MemoryEntry | None:
        """Retrieve by row ID. Secondary access pattern."""
        ...

    def search(self, query: str, *, project: str | None = None) -> list[MemoryEntry]:
        """FTS5 keyword search. Optionally scoped to a project."""
        ...

    def list_entries(self, *, project: str | None = None,
                     agent: str | None = None, limit: int = 100) -> list[MemoryEntry]:
        """List entries with optional filters."""
        ...

    def expire(self) -> int:
        """Delete TTL-expired entries. Returns count deleted."""
        ...

    def close(self) -> None:
        """Close the database connection."""
        ...


@runtime_checkable
class VectorStore(Protocol):
    """ChromaDB vector store operations (used by both T1 and T3)."""

    def upsert(self, collection: str, ids: list[str],
               documents: list[str], metadatas: list[dict]) -> None:
        """Upsert documents into a collection."""
        ...

    def query(self, collection: str, query_texts: list[str],
              n_results: int = 10, *,
              where: dict | None = None) -> list[VectorResult]:
        """Semantic search within a collection."""
        ...

    def get(self, collection: str, *,
            ids: list[str] | None = None,
            where: dict | None = None) -> list[VectorResult]:
        """Retrieve documents by ID or metadata filter."""
        ...

    def delete(self, collection: str, ids: list[str]) -> None:
        """Delete documents by ID."""
        ...

    def list_collections(self) -> list[str]:
        """List all collection names."""
        ...

    def collection_count(self, collection: str) -> int:
        """Get document count for a collection."""
        ...
```

### EmbeddingFunction Protocol

```python
# protocols/embedding.py

from typing import Protocol, runtime_checkable

@runtime_checkable
class EmbeddingFunction(Protocol):
    """Wraps ChromaDB embedding function interface."""

    def __call__(self, input: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        ...

    @property
    def model_name(self) -> str:
        """Return the model identifier (e.g., 'voyage-code-3')."""
        ...
```

**Implementations:**

| Class | Tier | Model | Network |
|-------|------|-------|---------|
| `DefaultEmbedding` | T1 | all-MiniLM-L6-v2 (local ONNX) | No |
| `VoyageCodeEmbedding` | T3 code | voyage-code-3 | Yes |
| `VoyageDocsEmbedding` | T3 docs/knowledge | voyage-4 | Yes |

### ChunkStrategy Protocol

```python
# protocols/chunking.py

from typing import Protocol, runtime_checkable
from nexus.types import Chunk

@runtime_checkable
class ChunkStrategy(Protocol):
    """Strategy for splitting content into embeddable chunks."""

    def chunk(self, content: str, source_path: Path | None, metadata: dict) -> list[Chunk]:
        """Split content into chunks with metadata.

        source_path: filesystem path to the source file (required by CodeChunker for
        language detection via file extension and line attribution; ignored by
        PDFChunker and SemanticMarkdownChunker, which use metadata instead).
        """
        ...
```

**Implementations:**

| Class | Pipeline | Source |
|-------|----------|--------|
| `CodeChunker` | Code | llama-index CodeSplitter (AST) + line-based fallback |
| `PDFChunker` | PDF | Ported from Arcaneum `PDFChunker` |
| `SemanticMarkdownChunker` | Markdown | Ported from Arcaneum `SemanticMarkdownChunker` |

### IndexPipeline Protocol

```python
# protocols/indexing.py

from typing import Protocol, runtime_checkable
from pathlib import Path
from nexus.types import IndexResult

@runtime_checkable
class IndexPipeline(Protocol):
    """Pipeline for indexing content into T3."""

    def index(self, path: Path) -> IndexResult:
        """Index content at the given path. Returns stats."""
        ...

    def needs_reindex(self, path: Path) -> bool:
        """Check if content at path needs re-indexing."""
        ...
```

**Implementations:**

| Class | Chunker | Embedding | Collection Pattern |
|-------|---------|-----------|-------------------|
| `CodeIndexPipeline` | `CodeChunker` | `VoyageCodeEmbedding` | `code__{repo}` |
| `PDFIndexPipeline` | `PDFChunker` | `VoyageDocsEmbedding` | `docs__{corpus}` |
| `MarkdownIndexPipeline` | `SemanticMarkdownChunker` | `VoyageDocsEmbedding` | `docs__{corpus}` |

### SearchPipeline Protocol

```python
# protocols/search.py

from typing import Protocol, runtime_checkable
from nexus.types import SearchResult, SearchOptions

@runtime_checkable
class SearchPipeline(Protocol):
    """Strategy for searching across storage tiers."""

    def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        """Execute a search and return scored results."""
        ...
```

**Implementations:**

| Class | Scope | Dependencies |
|-------|-------|-------------|
| `SemanticSearch` | T1 or T3 | ChromaDB query |
| `FulltextSearch` | Ripgrep cache | subprocess + mmap |
| `HybridSearch` | T3 code + ripgrep | SemanticSearch + FulltextSearch + scoring |
| `CrossCorpusSearch` | Multiple T3 | Per-corpus retrieval + Voyage rerank-2.5 |
| `AgenticSearch` | T3 | Haiku refinement loop |
| `MixedbreadFanout` | Mixedbread cloud | Mixedbread SDK (read-only) |

### ResultFormatter Protocol

```python
# protocols/formatting.py

from typing import Protocol, runtime_checkable
from nexus.types import SearchResult, FormatOptions

@runtime_checkable
class ResultFormatter(Protocol):
    """Strategy for formatting search results for display."""

    def format(self, results: list[SearchResult], options: FormatOptions) -> str:
        """Format results into a displayable string."""
        ...
```

**Implementations:** `PlainFormatter`, `HighlightedFormatter`, `VimgrepFormatter`,
`JsonFormatter`, `CitationFormatter`

---

## Component Architecture

### CLI Dispatch Flow [v2-planned]

The module paths in this diagram reflect the aspirational hierarchical layout.
In the v1-actual layout, the equivalent modules are: `commands/search_cmd.py`,
`commands/memory.py`, `commands/store.py`, `commands/scratch.py`, `commands/index.py`,
`commands/serve.py`, `commands/pm.py`, `commands/collection.py`, `commands/config_cmd.py`,
`commands/doctor.py`, `commands/install.py`; storage tiers are in `db/t1.py`, `db/t2.py`,
`db/t3.py`; and search/answer/formatting live in flat modules `search_engine.py`,
`answer.py`, `formatters.py`, `scoring.py`.

```
nx <command> [subcommand] [args] [flags]
     |
     v
cli/main.py (Click group)
     |
     +-- search_cmd.py ----> search/ (SemanticSearch | HybridSearch | CrossCorpusSearch)
     |                            \--> answer/synthesizer.py (if -a)
     |                            \--> formatting/ (selected by flags)
     |
     +-- memory_cmd.py ----> storage/t2_sqlite.py
     |
     +-- store_cmd.py -----> storage/t3_cloud.py
     |
     +-- scratch_cmd.py ---> storage/t1_ephemeral.py
     |                            \--> storage/t2_sqlite.py (on promote/flag-flush)
     |
     +-- index_cmd.py -----> indexing/{code,pdf,markdown}/pipeline.py
     |                            \--> storage/t3_cloud.py (upsert)
     |
     +-- serve_cmd.py -----> server/daemon.py + server/app.py
     |
     +-- pm_cmd.py --------> pm/lifecycle.py
     |                            \--> storage/t2_sqlite.py (T2 ops)
     |                            \--> storage/t3_cloud.py (archive to T3)
     |                            \--> pm/synthesis.py (Haiku synthesis)
     |
     +-- collection_cmd.py -> storage/t3_cloud.py
     +-- config_cmd.py ----> config.py
     +-- doctor_cmd.py ----> (validates all tiers + APIs)
     +-- install_cmd.py ---> integration/claude_code/installer.py
```

### Server Architecture (`nx serve`) [v1-actual: server.py / server_main.py]

The aspirational `server/` subpackage does not exist. The server is implemented in the
flat modules `src/nexus/server.py` (Flask app factory + routes) and
`src/nexus/server_main.py` (daemonization/entry point). Registry and polling live in
`src/nexus/registry.py` and `src/nexus/polling.py` respectively.

```
nx serve start
     |
     v
server_main.py (daemonization)   [v1-actual: server_main.py]
  |-- check_stale_pid()
  |-- write PID to ~/.config/nexus/server.pid
  |-- Popen(start_new_session=True) for daemonization
     |
     v
server.py (Flask app factory)    [v1-actual: server.py]
  |-- /search          POST  -> search pipeline
  |-- /index/status    GET   -> indexing progress per repo
  |-- /status          GET   -> server health + repo accuracy %
     |
     +-- registry.py             [v1-actual: registry.py]
     |     |-- repos.json: [{path, collection, last_head, ...}]
     |     |-- register(path) / unregister(path)
     |
     +-- polling.py              [v1-actual: polling.py]
           |-- per-repo thread: every N seconds (default 10)
           |   |-- git rev-parse HEAD
           |   |-- if changed: trigger indexer.py / doc_indexer.py
           |-- accuracy sigmoid (SeaGOAT pattern):
                 chunks_analyzed / total_chunks -> estimated accuracy %
```

### Storage Tier Interaction

```
                    +-----------+
                    |   T1      |    EphemeralClient + DefaultEmbeddingFunction
                    | (scratch) |    In-memory, session-scoped, local ONNX
                    +-----+-----+
                          |  scratch promote/flag-flush
                          v
+-------------------------+--------------------------+
|                         T2                         |
|                   (SQLite + FTS5)                   |
|   ~/.config/nexus/memory.db                        |
|   WAL mode, concurrent readers                     |
|   Schema: memory table + memory_fts virtual table  |
+-------------------------+--------------------------+
                          |  memory promote / pm archive
                          v
                    +-----------+
                    |   T3      |    CloudClient + VoyageAIEmbeddingFunction
                    | (ChromaDB)|    Cloud-backed, permanent
                    |  cloud    |    Collections: code__, docs__, knowledge__
                    +-----------+
```

**Data flows upward only** (T1 -> T2 -> T3). There is no reverse flow (T3 never
writes to T2 except `nx pm restore` which operates on T2 metadata only).

---

## nx pm Lifecycle

### State Machine

```
                     nx pm init
                         |
                         v
                    +---------+
                    | ACTIVE  |  T2 {repo} namespace (tagged pm), ttl=permanent
                    |         |  Full CRUD, FTS5 search, phase management
                    +----+----+
                         |
                    nx pm archive
                         |
              +----------+----------+
              |                     |
         1. Synthesize         2. Decay T2
         (Haiku -> T3)        (ttl=90d, tags pm->pm-archived)
              |                     |
              v                     v
        +----------+        +------------+
        | T3 chunk |        | T2 (decay) |
        | permanent|        | 90d window |
        +----------+        +-----+------+
                                   |
                              nx pm restore (within 90d)
                                   |
                                   v
                            +---------+
                            | ACTIVE  |  ttl reset to NULL, tags restored
                            +---------+

                            (after 90d: T2 entries expire, only T3 synthesis remains)
```

### Component Ownership [v1-actual: pm.py]

In the v1-actual flat layout, all PM lifecycle logic lives in `src/nexus/pm.py` and the
CLI commands in `src/nexus/commands/pm.py`. The aspirational `pm/lifecycle.py` and
`pm/synthesis.py` submodules do not exist.

| Operation | Primary Module | Storage Touched |
|-----------|---------------|-----------------|
| `nx pm init` | `pm/lifecycle.py` | T2 write (4 standard docs) |
| `nx pm resume` | `pm/lifecycle.py` | T2 computed resume from phase/blockers/activity |
| `nx pm status` | `pm/lifecycle.py` | T2 read (phase tags, BLOCKERS.md) |
| `nx pm phase next` | `pm/lifecycle.py` | T2 write (new phase doc) |
| `nx pm search` | `pm/lifecycle.py` | T2 FTS5 query (tag-filtered for pm) |
| `nx pm block/unblock` | `pm/lifecycle.py` | T2 write/update (BLOCKERS.md) |
| `nx pm archive` | `pm/lifecycle.py` + `pm/synthesis.py` | T2 read (all docs) -> Haiku -> T3 write -> T2 update (decay) |
| `nx pm close` | `pm/lifecycle.py` | Alias for archive --status=completed |
| `nx pm restore` | `pm/lifecycle.py` | T2 update (reset ttl + tags) |
| `nx pm reference` | `pm/lifecycle.py` | T3 query (knowledge__pm__* collections) |
| `nx pm promote` | `pm/lifecycle.py` | T2 read -> T3 write (knowledge__pm__*) |
| `nx pm expire` | `pm/lifecycle.py` | T2 delete (TTL-expired entries) |

---

## Cross-cutting Concerns

### Session ID Management [v1-actual]

Implementation: `src/nexus/session.py`. Uses `os.getsid(0)` as the stable PID anchor
(with `NX_SESSION_PID` env var override for testing). Session file path:
`~/.config/nexus/sessions/{pid}.session`.

- Generated via `os.getsid(0)` (session group leader PID) by SessionStart hook
- Written to `~/.config/nexus/sessions/{getsid}.session`
- Read from file by all `nx` subcommands that need it (scratch, memory, store)
- Fallback: if file missing, generate lazily on first access (UUID4)
- Note: PID-scoped path is intentional — the flat `~/.config/nexus/current_session` design was rejected as race-prone when multiple Claude Code windows run concurrently
- Stored as metadata on T1 documents (enables per-session filtering)
- Stored in T2 `session` column (provenance tracking)
- Stored in T3 `session_id` metadata field (provenance tracking)

### TTL Sentinel Conventions

| Tier | Permanent Sentinel | Finite TTL |
|------|-------------------|------------|
| T2 SQLite | `ttl IS NULL` | `ttl = N` (integer days) |
| T3 knowledge__ | `ttl_days = 0` AND `expires_at = ""` | `ttl_days = N` AND `expires_at = "<ISO 8601>"` |

**Translation on `nx memory promote`:**
- T2 `NULL` -> T3 `ttl_days=0, expires_at=""`
- T2 `N` -> T3 `ttl_days=N, expires_at=<computed ISO 8601>`

**Guarded expire query for T3:**
```python
# CORRECT: guard against empty string sorting before ISO timestamps
collection.get(where={
    "$and": [
        {"ttl_days": {"$gt": 0}},
        {"expires_at": {"$ne": ""}},
        {"expires_at": {"$lt": current_iso_time}}
    ]
})
```

The guard `{"expires_at": {"$ne": ""}}` prevents deleting permanent entries whose
empty-string `expires_at` would sort lexicographically before any ISO timestamp.

### Configuration Hierarchy

```
Environment variables (highest priority)
  CHROMA_API_KEY, VOYAGE_API_KEY, ANTHROPIC_API_KEY, MXBAI_API_KEY
  NX_PM_ARCHIVE_TTL, NX_ANSWER (convenience overrides)
     |
     v
Per-repo config: .nexus.yml (in repo root)
     |
     v
Global config: ~/.config/nexus/config.yml (lowest priority)
```

Loaded by `config.py` with deepmerge (repo overrides global, env overrides both).

### Error Hierarchy [v1-actual]

Implementation: `src/nexus/errors.py`. The actual hierarchy is simpler than originally
designed. The aspirational `StorageError`, `T2Error`, `T3Error`, `T3OfflineError`,
`SearchError`, `ConfigError`, `SessionError`, and `SynthesisError` classes do NOT exist.

```python
# errors.py  [v1-actual]

class NexusError(Exception):
    """Base exception for all Nexus errors."""

class T3ConnectionError(NexusError):
    """Failed to connect to or use the T3 ChromaDB cloud backend."""

class IndexingError(NexusError):
    """Error during document indexing pipeline."""

class CredentialsMissingError(NexusError):
    """A required API key or credential is absent."""

class CollectionNotFoundError(NexusError):
    """The requested ChromaDB collection does not exist."""
```

The aspirational (v2-planned) hierarchy below was NOT implemented:

```python
# errors.py  [v2-planned — NOT YET IMPLEMENTED]

class StorageError(NexusError): ...      # not implemented
class T2Error(StorageError): ...         # not implemented
class T3Error(StorageError): ...         # not implemented
class T3OfflineError(T3Error): ...       # not implemented
class SearchError(NexusError): ...       # not implemented
class ConfigError(NexusError): ...       # not implemented
class SessionError(NexusError): ...      # not implemented
class SynthesisError(NexusError): ...    # not implemented
```

### Search Engine Split [v1-actual]

The original design described `search_engine.py` as a monolithic search module. As of
nexus-895, the search responsibilities have been split into focused flat modules:

- `src/nexus/scoring.py` — scoring primitives (min_max_normalize, frecency weighting, etc.)
- `src/nexus/answer.py` — answer synthesis (Haiku-based Q&A with `<cite>` formatting)
- `src/nexus/formatters.py` — output formatters (plain, JSON, vimgrep, highlighted, citations)
- `src/nexus/search_engine.py` — search orchestration only (~175 lines)

The aspirational `search/` subpackage with separate `semantic.py`, `fulltext.py`,
`hybrid.py`, `cross_corpus.py`, `agentic.py`, and `mxbai.py` modules [v2-planned] does
not exist. All search orchestration remains in `search_engine.py`.

> **Original Phased Implementation Plan**: Superseded. See [Appendix A](#appendix-a-original-phased-implementation-plan) for historical phase structure and bead IDs. Current authoritative plan: `.pm/PLAN.md`.

---

## Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| 1 | **llama-index-core / tree-sitter-language-pack version incompatibility** | High | High (code indexing broken) | Pin exact versions in pyproject.toml. Test pair in CI. Document verified-good versions. Add integration test that parses a multi-language sample set. |
| 2 | **ChromaDB collection naming constraints** (3-63 chars, alphanum start/end) | Medium | Medium (index creation fails for long/special-char repo names) | Sanitize collection names in storage/t3_cloud.py: strip non-alphanum, truncate to 59 chars + 4-char hash suffix if over limit. Validate on nx index / nx pm init. |
| 3 | **Voyage AI model name verification** (SDK accepts any string, fails at first API call) | Medium | High (silent failure at runtime) | Add VOYAGE_MODELS constant with verified names. `nx doctor` tests a single embedding call per model. Server startup validates model names before accepting index requests. |
| 4 | **Session ID generation** (no CLAUDE_SESSION_ID env var) | Low | Medium (scratch operations fail without session) | Generate UUID4 lazily if file missing. Session ID optional for non-scratch commands. PID-based fallback for non-Claude-Code contexts. |
| 5 | **T3 ChromaDB cloud unavailability** (network outage) | Medium | High (all T3 ops fail) | Graceful degradation: T2 works offline. T3 commands print clear error. `nx doctor` checks connectivity. Cache last-known-good T3 state in config for status display. |
| 6 | **SQLite WAL concurrent access** (multiple Claude Code sessions) | Low | Medium (write contention) | `PRAGMA busy_timeout=5000`. Wrap writes in explicit transactions. Test concurrent read/write scenarios. FTS5 triggers are within-transaction so atomicity is maintained. |
| 7 | **Ripgrep line cache exceeds 500MB** (large monorepos) | Medium | Low (degraded hybrid search) | Soft limit with logged warning. Low-frecency files omitted from cache but remain semantic-searchable. Configurable limit via config.yml. |
| 8 | **PyMuPDF4LLM Type3 font hang** | Low | High (indexing blocks indefinitely) | Port Arcaneum's Type3 font pre-check. Add per-page extraction timeout (30s). Fallback chain: markdown -> normalized -> skip with warning. |
| 9 | **Haiku API dependency for archive and answer mode** | Medium | Medium (PM archive and -a flag fail) | Archive failure leaves T2 untouched (already in spec). Retry with exponential backoff (3 attempts). Clear error messages. Answer mode gracefully degrades to showing raw results. |
| 10 | **Mixedbread SDK authentication** | Low | Low (--mxbai silently skips) | Warning message when MXBAI_API_KEY unset. `nx doctor` validates when mxbai.stores configured. Graceful skip does not affect core search functionality. |
| 11 | **ChromaDB CloudClient rate limits during bulk indexing** | Medium | Medium (upsert failures during large repo initial index) | Implement exponential backoff with jitter on `upsert()` (max 5 retries, base 1s). Log retry attempts. Resume upsert from last successful batch using chunk IDs as progress markers. |
| 12 | **Voyage AI free tier exhaustion during iterative development** | Medium | Medium (embedding calls silently fail or return 402) | Track approximate embedding token consumption in `nx doctor`. Display usage warning after each index operation. Cache embeddings locally in a SQLite sidecar to avoid re-embedding unchanged chunks on re-index. |
| 13 | **ripgrep not on PATH at install time** | Medium | Low (hybrid search silently unavailable) | `nx doctor` checks `which rg` and prints install instructions per platform. `nx serve start` logs a warning if ripgrep absent. Hybrid search gracefully falls back to semantic-only with a warning message (not an error). |
| 14 | **SQLite WAL corruption under abnormal termination** | Low | High (T2 data inaccessible) | Run `PRAGMA integrity_check` on database open; abort with clear error if corrupt rather than continuing with broken state. Document recovery path: `sqlite3 nexus.db .dump > recovery.sql && sqlite3 nexus-new.db < recovery.sql`. |
| 15 | **Daemon daemonization differs between macOS and Linux** | Low | Low (nx serve start behaves differently across platforms) | Use `subprocess.Popen(start_new_session=True)` for platform-portable session detachment instead of `os.fork()`. Document tested platforms in README. Add cross-platform CI jobs (ubuntu-latest, macos-latest) for serve lifecycle tests. |

---

## Key Design Decisions

1. **Protocols over ABCs**: Using `typing.Protocol` (structural subtyping) rather than
   `abc.ABC` (nominal subtyping). This allows duck typing without inheritance coupling
   and enables easier testing with simple mock objects.

2. **No ORM for T2**: Direct `sqlite3` with explicit SQL. The schema is simple and fixed;
   an ORM would add complexity without benefit. WAL mode and FTS5 are stdlib features.

3. **Constructor injection**: All major components accept their dependencies via constructor
   parameters. No global singletons or service locators. Example: `HybridSearch.__init__`
   takes `semantic: SemanticSearch, fulltext: FulltextSearch, scoring: ScoringConfig`.

4. **Ported code (not imported)**: Arcaneum and SeaGOAT code is ported (rewritten in the
   Nexus module structure), not imported as libraries. This avoids version coupling and
   allows adapting to ChromaDB/Voyage AI instead of Qdrant/fastembed.

5. **Click for CLI**: Matches the Python ecosystem standard. Click's group/command structure
   maps cleanly to the `nx <group> <command>` pattern.

6. **Separate formatting layer**: Output formatting is decoupled from search logic. The
   same search results can be rendered as plain text, JSON, vimgrep, or highlighted
   depending on flags, without modifying search code.

7. **Lazy session ID**: Session ID is generated on first access, not at import time.
   This prevents errors when running commands that do not need a session (e.g., `nx config show`).

---

## Appendix A: Original Phased Implementation Plan

> **SUPERSEDED**: This plan was the original scaffold for the project. The bead-by-bead execution plan has moved to `.pm/PLAN.md`, which is the single authoritative source for phase structure, bead IDs, and dependency tracking. Bead IDs listed here (`nexus-5v7`, `nexus-c4b`, etc.) were superseded by the comprehensive plan in `.pm/PLAN.md`. Always use `.pm/PLAN.md` for tracking and `AGENT_INSTRUCTIONS.md` for the canonical bead reference.

### Dependency Graph

```
Phase 1 (scaffold + T2)
  |         \
  v          v
Phase 2    Phase 3 (T3)
(T1)         |       \
  |          v        v
  |       Phase 4   Phase 5
  |       (server)  (PDF/md)
  |          \       /
  |           v     v
  |        Phase 6 (hybrid + rerank)
  |            |
  +------+     |
         v     v
       Phase 7 (PM + integration)
```

### Phase Summary

| Phase | Bead ID | Focus | Blocked By |
|-------|---------|-------|------------|
| Epic | `nexus-c4b` | Overall project epic | - |
| 1 | `nexus-5v7` | Scaffold + T2 SQLite + nx memory | - |
| 2 | `nexus-8zh` | T1 EphemeralClient + nx scratch + session | nexus-5v7 |
| 3 | `nexus-evn` | T3 CloudClient + nx store + nx search | nexus-5v7 |
| 4 | `nexus-wjd` | nx serve + code indexing pipeline | nexus-evn |
| 5 | `nexus-yut` | PDF/markdown indexing pipelines | nexus-evn |
| 6 | `nexus-0sp` | Hybrid search + reranking + answer mode | nexus-wjd, nexus-yut |
| 7 | `nexus-wdm` | nx pm lifecycle + Claude Code integration | nexus-0sp, nexus-8zh |

**Parallelizable phases:** 2+3 after Phase 1; 4+5 after Phase 3; Phase 7 last.

### Phase 1: Project Scaffold + T2 SQLite + nx memory CRUD

**Deliverables:** `pyproject.toml` (version-pinned), package skeleton, `protocols/` with all Protocol definitions, `types.py`, `errors.py`, `config.py`, `storage/t2_sqlite.py` (WAL + FTS5), `cli/main.py` + `memory_cmd.py`, tests.

**Exit criteria:** `nx memory put/get/search/list/expire` all work; pytest >80% coverage on t2_sqlite.py.

### Phase 2: T1 EphemeralClient + nx scratch + Session ID

**Deliverables:** `session.py`, `storage/t1_ephemeral.py`, `cli/scratch_cmd.py`, `integration/claude_code/hooks.py`, tests.

**Exit criteria:** Session ID written to `~/.config/nexus/sessions/{getsid}.session`; full `nx scratch` subcommand set working with local ONNX embeddings.

### Phase 3: T3 CloudClient + nx store + nx search (semantic)

**Deliverables:** `storage/t3_cloud.py`, `cli/store_cmd.py`, `cli/search_cmd.py`, `cli/collection_cmd.py`, `search/semantic.py`, `search/cross_corpus.py`, `search/scoring.py`, `formatting/plain.py` + `json_fmt.py`, `cli/doctor_cmd.py`, tests.

**Exit criteria:** `nx store`, `nx search`, `nx collection list`, `nx doctor`, `nx store expire` (guarded query), `nx memory promote` all working.

### Phase 4: nx serve (Flask/Waitress) + Code Indexing Pipeline

**Deliverables:** `server/app.py`, `server/daemon.py`, `server/registry.py`, `server/polling.py`, `indexing/code/frecency.py`, `indexing/code/chunker.py`, `indexing/code/ripgrep_cache.py`, `indexing/code/pipeline.py`, `cli/serve_cmd.py`, `cli/index_cmd.py` (code), `integration/git_hooks.py`, tests.

**Exit criteria:** Full `nx serve` lifecycle; `nx index code` registers repo + triggers indexing + builds ripgrep cache; HEAD polling detects changes; frecency matches SeaGOAT formula.

### Phase 5: PDF/Markdown Indexing Pipelines

**Deliverables:** `indexing/pdf/extractor.py`, `indexing/pdf/chunker.py`, `indexing/pdf/ocr.py`, `indexing/pdf/pipeline.py`, `indexing/markdown/chunker.py`, `indexing/markdown/pipeline.py`, `cli/index_cmd.py` additions (pdf, md), tests.

**Exit criteria:** `nx index pdf` with PyMuPDF4LLM → pdfplumber → OCR fallback; Type3 font hang prevention; `nx index md` with SemanticMarkdownChunker; incremental sync via SHA256.

### Phase 6: Hybrid Search + Cross-corpus Reranking + Answer Mode

**Deliverables:** `search/fulltext.py`, `search/hybrid.py`, `search/agentic.py`, `search/mxbai.py`, `answer/synthesizer.py`, `formatting/highlighted.py`, `formatting/vimgrep.py`, `formatting/citations.py`, complete `cli/search_cmd.py`, tests.

**Exit criteria:** `--hybrid`, cross-corpus reranking, `-a` answer mode with `<cite>` formatting, `--agentic` multi-step refinement, `--mxbai` fan-out, all output format flags (`--vimgrep`, `--json`, `-B/-A/-C`).

### Phase 7: nx pm Full Lifecycle + Claude Code Plugin Integration

**Deliverables:** `pm/lifecycle.py`, `pm/templates.py`, `pm/synthesis.py`, `cli/pm_cmd.py`, `integration/claude_code/installer.py`, `integration/claude_code/skill_template.py`, `cli/install_cmd.py`, `cli/config_cmd.py`, integration tests.

**Exit criteria:** Full `nx pm` state machine (init → archive → restore); `nx install claude-code`; SessionStart/SessionEnd hooks; `nx config show/set`; end-to-end integration test.
