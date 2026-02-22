# Nexus — Engineering Methodology

**Nexus is a complex multi-tier system requiring disciplined, test-first development across 8 phases. This document defines the engineering practices that ensure quality, resumability, and correctness.**

## Development Discipline

### TDD-First: Test Before Code

All features follow strict TDD:

1. **Write test first** — define expected behavior via pytest test cases
2. **Run test** — watch it fail (red)
3. **Implement** — write minimum code to pass test (green)
4. **Refactor** — clean up, extract common patterns (green+clean)
5. **Integration test** — verify the new feature integrates with existing code

**Why**:
- Forces clear thinking about interfaces before implementation
- Catches regressions early
- Enables confident refactoring
- Documents expected behavior via test cases

**Test Organization**:
```
tests/
├── unit/
│   ├── storage/
│   │   ├── test_t1_ephemeral.py      # T1 in-memory ChromaDB
│   │   ├── test_t2_memory.py         # T2 SQLite+FTS5
│   │   └── test_t3_cloud.py          # T3 CloudClient (mocked)
│   ├── indexing/
│   │   ├── test_code_chunking.py     # AST-based code splits
│   │   ├── test_pdf_extraction.py    # PyMuPDF4LLM pipeline
│   │   └── test_markdown_chunking.py # Semantic markdown splits
│   └── search/
│       ├── test_semantic_search.py
│       ├── test_hybrid_search.py
│       └── test_reranking.py
├── integration/
│   ├── test_t2_concurrent.py         # WAL mode, multi-session access
│   ├── test_serve_lifecycle.py       # Server start/stop/reload
│   └── test_cli_e2e.py               # End-to-end CLI workflows
└── fixtures/
    ├── sample_repos/                 # Test git repos for frecency
    ├── sample_pdfs/                  # Test PDFs (scanned, tables, etc.)
    └── chromadb_mocks.py             # Mock T3 CloudClient
```

### Bead Tracking: Zero TODOs

**All work is tracked via beads, not markdown TODOs.**

Beads represent:
- **Tasks**: Work items with start/end state (open → in_progress → done)
- **Epics**: Large features decomposed into sub-beads
- **Blockers**: Dependencies between beads (bd dep add A B means B blocks A)

**Bead workflow for a feature**:

1. **Create epic bead** (Phase planning):
   ```bash
   bd create "Implement T2 SQLite memory bank" -t epic -p 1
   # Output: returns bead ID, e.g., "ABC123"
   ```

2. **Create sub-beads** (design phase):
   ```bash
   bd create "T2 schema + WAL setup" -t task -p 1
   bd create "T2 CRUD operations" -t task -p 1
   bd create "CLI commands: nx memory put/get/search/list" -t task -p 1
   bd create "Unit tests for T2 operations" -t task -p 1
   bd create "Integration test: concurrent access" -t task -p 1
   ```

3. **Add dependencies** (if task B needs task A to complete first):
   ```bash
   bd dep add T2_CRUD T2_schema  # T2_CRUD blocked by T2_schema
   ```

4. **Work on ready beads** (ones with no blockers):
   ```bash
   bd ready  # List unblocked work
   bd show ABC123  # Review bead
   bd update ABC123 --status in_progress
   ```

5. **Close on completion**:
   ```bash
   bd update ABC123 --status done
   ```

**Why beads, not TODOs?**:
- Beads have **status** (open/in_progress/done) — you can't forget to mark progress
- Beads have **dependencies** — blockers are explicit, not implicit
- Beads have **estimates** — help prioritize and detect scope creep
- Beads are **queryable** — `bd list --status=ready` shows what can start now
- Beads survive **context switches** — bead state persists across sessions

### Plan → Audit → Implement Cycle

Multi-week features follow a three-stage gate:

**Stage 1: Plan** (strategic-planner agent)
- Create high-level feature breakdown: phases, success criteria, known risks
- Output: Feature plan document (stored in ChromaDB)

**Stage 2: Audit** (plan-auditor agent)
- Validate feasibility, uncover hidden dependencies, verify architecture
- Output: Audit report with GO/NO-GO verdict; if NO-GO, return to Stage 1

**Stage 3: Implement** (development agents: java-developer, etc.)
- Execute plan with approved architecture
- Output: Code + tests, ready for review

**Why this discipline?**:
- Prevents re-architecture mid-feature (expensive rework)
- Catches scope creep early
- Distributes risk: plan-auditor is independent of planner
- Enables parallel work: multiple features can plan → audit → implement in parallel

**Example workflow for Phase 2 (T1 scratch)**:

```
Week 1: Plan T1
  → strategic-planner creates design doc
  → bd create "Phase 2 T1 Implementation" -t epic
  → blockers: none (can start after Phase 1 complete)

Week 1 (end): Audit Plan
  → plan-auditor reviews T1 design
  → Output: "GO — proceed with implementation"

Week 2–3: Implement T1
  → TDD-first: tests for EphemeralClient setup, session ID handling
  → Implement T1 module
  → code-review-expert reviews
  → test-validator checks coverage
  → merge to main
```

## Python Conventions

### Type Hints Everywhere

All functions have full type hints (no implicit `Any`):

```python
from typing import Optional, List
from nexus.storage.t2 import MemoryEntry

def put_memory(
    project: str,
    title: str,
    content: str,
    tags: Optional[str] = None,
    ttl: Optional[int] = 30,  # days, or None for permanent
) -> int:
    """Insert or upsert a memory entry. Returns ID."""
    ...

def search_memory(query: str, project: Optional[str] = None, limit: int = 10) -> List[MemoryEntry]:
    """Full-text search across content+tags. Returns ranked results."""
    ...
```

**Why**:
- Self-documents expected input/output types
- Enables IDE autocomplete and type checking (pyright/mypy)
- Catches bugs at development time, not runtime
- Required for library usability (agents will rely on type stubs)

### Dataclasses for Data Models

Use `dataclasses` for structured data (not ORM, not dicts):

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class MemoryEntry:
    """Single row in the memory table."""
    id: int
    project: str
    title: str
    content: str
    session: Optional[str]
    agent: Optional[str]
    tags: Optional[str]
    timestamp: datetime
    ttl: Optional[int]  # days from write; None = permanent

@dataclass
class SearchResult:
    """Result from FTS5 search."""
    entry: MemoryEntry
    rank: float  # FTS5 rank (negative, more negative = better)
```

**Why**:
- No ORM overhead (SQLite is simple; raw SQL + dataclasses is cleaner)
- Immutable by default (`frozen=True` for value objects)
- JSON-serializable (via dataclasses.asdict)
- Easier to reason about than dicts

### Pytest Fixtures for Shared Test State

Test setup via pytest fixtures (not setUp/tearDown):

```python
import pytest
from nexus.storage.t2 import MemoryDB

@pytest.fixture
def memory_db(tmp_path):
    """Create isolated T2 database for each test."""
    db_path = tmp_path / "memory.db"
    db = MemoryDB(str(db_path))
    db.init_schema()
    yield db
    db.close()

def test_put_and_get(memory_db):
    """Store and retrieve a memory entry."""
    memory_db.put(project="test", title="notes.md", content="# Test")
    result = memory_db.get(project="test", title="notes.md")
    assert result.content == "# Test"

def test_concurrent_access(memory_db):
    """WAL mode supports multiple readers."""
    # Test concurrent operations
    ...
```

**Why**:
- Fixtures are composable (one fixture can use another)
- Automatic cleanup (yield-based teardown is guaranteed)
- Isolated state (each test gets a fresh database)
- Readable test names (no TestClass boilerplate)

### Protocol for Polymorphism

**Nexus uses `typing.Protocol` for all interfaces** (per ARCHITECTURE.md Key Design Decision). `abc.ABC` is not used in this codebase — structural subtyping (Protocol) enables duck typing without inheritance coupling, which simplifies mocking in tests.

```python
from typing import Protocol, runtime_checkable
from pathlib import Path

@runtime_checkable
class EmbeddingFunction(Protocol):
    """Any object that can embed texts."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts. Returns list of embedding vectors."""
        ...

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query."""
        ...

class DefaultEmbedding:
    """Local ONNX all-MiniLM-L6-v2 for T1 scratch. Satisfies EmbeddingFunction protocol."""
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_query(self, query: str) -> list[float]:
        ...

class VoyageEmbedding:
    """Voyage AI API wrapper for T3. Satisfies EmbeddingFunction protocol."""
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_query(self, query: str) -> list[float]:
        ...

# Test mock — no inheritance required:
class MockEmbedding:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]
    def embed_query(self, query: str) -> list[float]:
        return [0.1] * 1024
```

**Why Protocol over ABC**:
- Mock implementations satisfy the interface without inheritance boilerplate
- No import of the base class needed in concrete implementations
- `@runtime_checkable` enables `isinstance(obj, EmbeddingFunction)` checks when needed
- Cleaner test fixtures: mock just implements the required methods, nothing else
- Matches ChromaDB's own embedding function pattern (structural, not nominal)

### Config via dataclass + environment variables

No YAML/TOML parsing in-code; config is strongly typed:

```python
from dataclasses import dataclass, field
from typing import Optional
import os

@dataclass
class ServerConfig:
    port: int = 7890
    head_poll_interval: int = 10  # seconds
    ignore_patterns: List[str] = field(default_factory=lambda: ["node_modules", "__pycache__"])

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """Load from environment variables (with defaults)."""
        return cls(
            port=int(os.environ.get("NX_SERVER_PORT", "7890")),
            head_poll_interval=int(os.environ.get("NX_SERVER_HEAD_POLL_INTERVAL", "10")),
        )

@dataclass
class EmbeddingConfig:
    code_model: str = "voyage-code-3"
    docs_model: str = "voyage-4"
    reranker_model: str = "rerank-2.5"
    voyage_api_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        """Load API keys from environment."""
        return cls(
            code_model=os.environ.get("NX_CODE_MODEL", "voyage-code-3"),
            docs_model=os.environ.get("NX_DOCS_MODEL", "voyage-4"),
            reranker_model=os.environ.get("NX_RERANKER_MODEL", "rerank-2.5"),
            voyage_api_key=os.environ.get("VOYAGE_API_KEY"),
        )
```

**Why**:
- Type-safe: can't accidentally pass a string to an int field
- Single source of truth: config structure is in code, not config files
- Environment variables are the standard 12-factor pattern
- No parsing errors at runtime

### Logging via Python Logging Module (not print)

All I/O goes through `logging`:

```python
import logging

logger = logging.getLogger(__name__)

def build_ripgrep_cache(repo_path: str) -> int:
    """Build line cache for a repository."""
    logger.info(f"Building ripgrep cache for {repo_path}")

    if len(files) > CACHE_LIMIT:
        logger.warning(f"Ripgrep cache exceeds 500MB; omitting {skipped} low-frecency files")

    logger.debug(f"Indexed {file_count} files in {elapsed_ms}ms")
    return file_count
```

Session initialization (SessionStart hook) sets up logging:

```python
import logging.config

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "[%(levelname)s] %(name)s: %(message)s"
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "default",
        },
        "file": {
            "class": "logging.FileHandler",
            "filename": "~/.config/nexus/serve.log",
            "level": "DEBUG",
            "formatter": "default",
        }
    },
    "root": {
        "level": "DEBUG",
        "handlers": ["console", "file"]
    }
})
```

**Why**:
- Controlled verbosity (info for users, debug for developers)
- Log levels allow filtering without code changes
- File logging for post-mortem analysis (serve.log)
- Structured logging enables monitoring/alerting in future

## Code Organization

### Directory Structure

```
nexus/
├── __main__.py                 # CLI entry point (python -m nexus)
├── cli/
│   ├── __init__.py
│   ├── main.py                 # Click CLI root
│   ├── memory_commands.py      # nx memory subcommands
│   ├── search_commands.py      # nx search subcommands
│   ├── index_commands.py       # nx index subcommands
│   ├── store_commands.py       # nx store subcommands
│   ├── scratch_commands.py     # nx scratch subcommands
│   ├── serve_commands.py       # nx serve lifecycle
│   ├── pm_commands.py          # nx pm subcommands
│   └── config_commands.py      # nx config / nx doctor
├── storage/
│   ├── __init__.py
│   ├── t1/
│   │   ├── __init__.py
│   │   └── ephemeral.py        # T1 in-memory EphemeralClient wrapper
│   ├── t2/
│   │   ├── __init__.py
│   │   ├── memory.py           # T2 SQLite+FTS5 core
│   │   ├── schema.py           # SQL DDL (schema, triggers)
│   │   └── models.py           # MemoryEntry, SearchResult dataclasses
│   └── t3/
│       ├── __init__.py
│       ├── cloud.py            # T3 ChromaDB CloudClient wrapper
│       └── models.py           # SearchResult, ChunkMetadata dataclasses
├── indexing/
│   ├── __init__.py
│   ├── code/
│   │   ├── __init__.py
│   │   ├── chunker.py          # llama-index CodeSplitter wrapper
│   │   ├── frecency.py         # Git log → frecency scores
│   │   ├── ripgrep_cache.py    # Ripgrep line cache builder
│   │   └── metadata.py         # code__ metadata builder
│   ├── pdf/
│   │   ├── __init__.py
│   │   ├── extractor.py        # PyMuPDF4LLM + pdfplumber + OCR
│   │   ├── chunker.py          # PDF → markdown chunks
│   │   └── metadata.py         # docs__ metadata builder
│   └── markdown/
│       ├── __init__.py
│       ├── chunker.py          # SemanticMarkdownChunker (Arcaneum port)
│       └── metadata.py         # docs__ metadata builder
├── search/
│   ├── __init__.py
│   ├── semantic.py             # Single-corpus semantic search
│   ├── hybrid.py               # Code ripgrep + semantic merge
│   ├── reranking.py            # Voyage rerank-2.5 cross-corpus merge
│   └── models.py               # SearchResult, Citation dataclasses
├── qa/
│   ├── __init__.py
│   └── synthesis.py            # Haiku → answer synthesis
├── config.py                   # Config loading + validation
├── version.py                  # __version__ constant
└── logging_config.py           # Logging setup

tests/
├── conftest.py                 # Pytest fixtures
├── unit/
│   ├── storage/
│   │   ├── test_t2_schema.py
│   │   ├── test_t2_crud.py
│   │   ├── test_t2_fts5.py
│   │   ├── test_t1_scratch.py
│   │   └── test_t3_cloud.py
│   ├── indexing/
│   │   ├── test_code_chunking.py
│   │   ├── test_pdf_extraction.py
│   │   ├── test_markdown_chunking.py
│   │   └── test_frecency.py
│   └── search/
│       ├── test_semantic_search.py
│       ├── test_hybrid_search.py
│       └── test_reranking.py
├── integration/
│   ├── test_cli_memory.py
│   ├── test_cli_search.py
│   ├── test_serve_lifecycle.py
│   └── test_e2e_workflows.py
└── fixtures/
    ├── __init__.py
    ├── chromadb_mocks.py
    ├── sample_repos/
    └── sample_pdfs/
```

### Module Imports and Circular Dependencies

**Rule**: Avoid circular imports via careful layering:

```
Config (base) ←─┐
  ↑             │
Logging        │
  ↑             │
Models (dataclasses, only deps: typing, datetime)
  ↑             │
Storage (T1, T2, T3 implementations)  ← doesn't import CLI or Search
  ↑             │
Indexing (code, pdf, markdown)        ← doesn't import CLI
  ↑             │
Search (semantic, hybrid, reranking)  ← doesn't import CLI
  ↑             │
QA (synthesis)
  ↑             │
CLI (orchestrates everything)         ← imports Search, Storage, Indexing, QA
```

**No cross-layer imports** (e.g., Storage must not import from Search).

### Error Handling

Use custom exceptions for predictable failures:

```python
class NexusError(Exception):
    """Base exception for all Nexus errors."""
    pass

class StorageError(NexusError):
    """T1/T2/T3 operation failed."""
    pass

class MemoryNotFoundError(StorageError):
    """Memory entry not found (by ID or project+title)."""
    pass

class TTLFormatError(NexusError):
    """Invalid TTL format (e.g., '30d', '4w', 'permanent')."""
    pass

class IndexError(NexusError):
    """Indexing pipeline failed."""
    pass

class ChromaAuthError(StorageError):
    """ChromaDB cloud auth failed (CHROMA_API_KEY missing or invalid)."""
    pass

class VoyageAuthError(NexusError):
    """Voyage API auth failed (VOYAGE_API_KEY missing or invalid)."""
    pass
```

CLI catches these and prints user-friendly messages:

```python
@click.command()
def search(query: str):
    try:
        results = nx_search(query)
        click.echo(results)
    except ChromaAuthError as e:
        click.secho(f"Error: ChromaDB auth failed. Set CHROMA_API_KEY environment variable.", fg="red")
        raise SystemExit(1)
    except NexusError as e:
        click.secho(f"Error: {e}", fg="red")
        raise SystemExit(1)
```

## Review and Validation Gates

### Code Review (Before Merge)

Every PR to main requires:
- **code-review-expert** review for style, patterns, potential bugs
- **test-validator** review for test coverage (target: >80% coverage)

Merge blockers:
- ❌ Coverage drops below 80%
- ❌ Type hints missing on any public function
- ❌ No tests added (TDD violation)
- ❌ TODO comments in code (use beads instead)

### Test Coverage Reporting

Every test run reports coverage:

```bash
pytest --cov=nexus --cov-report=term-missing --cov-report=html
# Output:
#   nexus/storage/t2.py: 94%   (12 missing lines)
#   nexus/indexing/code.py: 78%   (45 missing lines — below target!)
#   Overall: 88%
```

Coverage reports stored in beads (Phase retrospective):

```bash
bd create "Coverage analysis — Phase 1" -t chore
# Add context: which modules need more tests, patterns to backfill
```

## Documentation Standards

### Docstrings on All Public Functions

Google-style docstrings for IDE navigation:

```python
def put_memory(
    project: str,
    title: str,
    content: str,
    tags: Optional[str] = None,
    ttl: Optional[int] = 30,
) -> int:
    """Insert or upsert a memory entry into T2 SQLite.

    Deterministic upsert on (project, title) key: if an entry with the same
    project+title exists, its content and TTL are updated; otherwise a new
    entry is created. Agent and session metadata are auto-captured if available.

    Args:
        project: Namespace key, e.g. 'BFDB_active', 'nexus_pm', 'scratch_sessions'
        title: Filename-like key within project, e.g. 'findings.md', 'phase-1/context.md'
        content: Full text (any size; 500+ lines is fine for SQLite TEXT)
        tags: Comma-separated tags for filtering, e.g. 'phase1,decision,architecture'
        ttl: TTL in days from write time (30 = default); None or 'permanent' for no expiry

    Returns:
        Database row ID (int) of the inserted/updated entry.

    Raises:
        StorageError: SQLite operation failed (disk full, permissions, etc.)
        TTLFormatError: ttl argument is invalid (not an int, not 'permanent', etc.)

    Example:
        >>> memory_db = MemoryDB()
        >>> mid = memory_db.put(
        ...     project='nexus_pm',
        ...     title='phase-1/context.md',
        ...     content='# Phase 1: Foundation\n\n...',
        ...     tags='pm,phase:1,context',
        ...     ttl=None  # permanent
        ... )
        >>> fetched = memory_db.get(mid)
        >>> fetched.project
        'nexus_pm'
    """
    ...
```

### README Sections for Each Module

`nexus/storage/t2/README.md`:

```markdown
# T2 — Local SQLite Memory Bank

## Purpose
T2 is the local, persistent (no network) structured storage tier. It replaces the current MCP memory bank with a SQL-backed system that supports concurrent access via WAL mode and full-text search via FTS5.

## Schema
See schema.py for CREATE TABLE and trigger definitions.

## Usage
```python
from nexus.storage.t2 import MemoryDB

db = MemoryDB()
db.init_schema()  # idempotent
mid = db.put(project='nexus_pm', title='findings.md', content='# Findings\n\n...')
entry = db.get(project='nexus_pm', title='findings.md')
results = db.search('query', project='nexus_pm')  # FTS5 keyword search
db.expire()  # clean up TTL-expired entries (call daily)
```

## Implementation Notes
- WAL mode (PRAGMA journal_mode=WAL) enables concurrent readers during writes
- FTS5 virtual table is external-content mode (no duplication of content/tags)
- Triggers keep FTS5 in sync with DML operations
- Connection pooling: set SQLITE_THREADSAFE=2 (serialized) globally
```

## Session Continuity

### Checkpoint Discipline

After every ~2 hours of work, create a checkpoint bead:

```bash
bd create "Checkpoint: T2 CRUD complete, tests 89% coverage" -t task
# Context in bead:
# - What works: put/get/list/search/delete all passing
# - What's stubbed: expire() scheduled job not yet integrated
# - Next: integrate expire into SessionEnd hook
# - Blockers: none
# - Test status: 89% coverage; missing: edge cases for TTL=0 permanent
```

Checkpoints answer: "If I close the session now, can I resume in a week?"

### CONTINUATION Discipline

At session end or context limit, update `.pm/CONTINUATION.md`:
- Current phase progress (% complete)
- Last checkpoint location (bead ID)
- Immediate next action (one sentence)
- Any unresolved blockers or unknowns

On session resumption, read CONTINUATION.md first.

## Dependencies and Version Pinning

All production dependencies pinned in `pyproject.toml`:

```toml
[project]
dependencies = [
    "chromadb==0.5.8",                 # verified good with voyageai
    "voyageai==1.2.3",                 # supports VoyageAIEmbeddingFunction
    "llama-index-core==0.12.18",       # ⚠️ known breaking with tree-sitter-language-pack <0.25
    "tree-sitter-language-pack==0.25.1",  # exact match required
    "PyMuPDF==1.24.8",
    "pdfplumber==0.11.2",
    "easyocr==1.8.0",
    "flask==3.0.3",
    "waitress==3.0.1",
    "anthropic==0.35.5",               # Haiku synthesis
    "click==8.1.8",                    # CLI
    "PyYAML==6.0.1",                   # Config
]

[project.optional-dependencies]
dev = [
    "pytest==8.3.1",
    "pytest-cov==5.0.0",
    "pytest-asyncio==0.23.2",
    "mypy==1.14.1",
    "ruff==0.6.3",
]
```

**Why pin versions?**
- Prevents surprise breakage from transitive dependencies
- Reproducible builds (anyone can run `uv sync` and get the same environment)
- Safety: a newer version of package X might have a breaking change
- Reproducibility: CI and production match local development exactly

**Version update workflow**:
1. Create a bead: "Update dependency: chromadb 0.5.7 → 0.5.8"
2. Run `uv update chromadb`
3. Test: `pytest`, integration tests
4. If pass: commit. If fail: investigate before upgrading.

## Summary of Key Principles

| Principle | Why | How |
|-----------|-----|-----|
| **TDD-First** | Forces clear thinking; catches regressions early | Write test first, then implementation |
| **Bead Tracking** | No TODOs get forgotten; dependencies visible | All work tracked via `bd` CLI; zero markdown TODOs |
| **Type Hints** | Catches errors at dev time; enables IDE autocomplete | Every function has full type hints |
| **Dataclasses** | Clear data structure definition; no ORM overhead | Use `@dataclass` for all data models |
| **Layered Architecture** | Prevents circular imports; clear separation of concerns | Storage ← Indexing ← Search ← CLI |
| **Pytest Fixtures** | Isolated test state; automatic cleanup | Use `@pytest.fixture` with yield for setup/teardown |
| **Configuration** | Type-safe config; follows 12-factor pattern | `@dataclass` + environment variables; no YAML parsing in code |
| **Logging** | Visibility for debugging; controlled verbosity | All I/O via `logging` module; no print() |
| **Error Handling** | User-friendly messages; predictable failures | Custom exceptions; caught in CLI layer |
| **Code Review** | Catches bugs; enforces standards | Every PR requires code-review-expert + test-validator |
| **Checkpointing** | Continuity across sessions | Update CONTINUATION.md at session end |
| **Version Pinning** | Reproducible builds; no surprise breakage | All deps pinned in pyproject.toml; `uv sync` for reproducibility |

