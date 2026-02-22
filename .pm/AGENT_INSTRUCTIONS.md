# Nexus — Instructions for Spawned Agents

**This document tells agents how to work effectively on the Nexus project.**

## Before You Start

1. **Read the CONTINUATION.md** — project state, current phase, blockers, next action
2. **Read the spec.md** — 910-line specification at `/Users/hal.hildebrand/git/nexus/spec.md`
3. **Read METHODOLOGY.md** — engineering discipline: TDD-first, bead tracking, Python conventions
4. **Understand the problem**: Nexus is a multi-tier semantic search + knowledge system replacing Mixedbread cloud ingest with local indexing pipelines.

## Work Within the .pm/ Infrastructure

- **No markdown TODOs** — use beads (`bd create`, `bd update`, `bd close`) for all work
- **Bead tracking**: Before starting work, check `bd ready` — only work on unblocked beads
- **Dependencies**: If your work depends on another bead, add it via `bd dep add <your-bead> <blocker-bead>`
- **Context links in beads**: Every bead description should include:
  - Related files (what to modify)
  - Success criteria (how to verify it works)
  - Context links (ChromaDB IDs, .pm/ file paths, spec line numbers)
  - Design pattern to follow (e.g., "Use FTS5 external-content mode like T2 does")

## Project Structure

```
/Users/hal.hildebrand/git/nexus/
├── spec.md                    ← Full specification (read first!)
├── .pm/                       ← Project management infrastructure
│   ├── CONTINUATION.md        ← Session state + next action
│   ├── METHODOLOGY.md         ← Engineering practices
│   ├── CONTEXT_PROTOCOL.md    ← Context management + relay format
│   └── phases/
│       ├── phase-1/context.md ← Foundation: T2 SQLite + nx memory
│       ├── phase-2/context.md ← T1 scratch
│       ├── phase-3/context.md ← T3 cloud
│       └── phase-4/context.md ← nx serve + code indexing
├── README.md                  ← User guide (currently empty)
├── nexus/                     ← Source code (to be created in Phase 1)
├── tests/                     ← Unit + integration tests
├── pyproject.toml             ← Dependencies + build config
└── .git/                      ← Version control

Repository: /Users/hal.hildebrand/git/nexus/
Remote: TBD
Language: Python 3.12+
Primary Agents: strategic-planner (design), java-developer (implementation), code-review-expert (review)
```

## Key Files to Know

| File | Purpose | Status |
|------|---------|--------|
| `/Users/hal.hildebrand/git/nexus/spec.md` | Full specification (910 lines) | Complete, finalized |
| `/Users/hal.hildebrand/git/nexus/.pm/CONTINUATION.md` | Project state + next action | Active |
| `/Users/hal.hildebrand/git/nexus/.pm/METHODOLOGY.md` | Engineering discipline | Active |
| `/Users/hal.hildebrand/git/nexus/.pm/phases/phase-1/context.md` | Phase 1 scope | Created |
| `/Users/hal.hildebrand/git/nexus/pyproject.toml` | Dependencies + build | To be created Phase 1 |
| `/Users/hal.hildebrand/git/nexus/nexus/__main__.py` | CLI entry point | To be created Phase 1 |

## GitHub Collection Naming: `code__repo` NOT `code::repo`

**Critical**: ChromaDB collection names use double underscore (`__`), not single colon (`:`).

**Why**: FTS5 metadata queries use `:` as a delimiter. Single colon in collection names causes query ambiguity.

Correct naming:
```
code__nexus              ← code repository
code__arcaneum
docs__papers            ← document corpus
docs__api-docs
knowledge__caching      ← knowledge/decisions
knowledge__pm__nexus    ← PM archive for nexus project
```

Incorrect (will cause query failures):
```
code::nexus             ← ❌ single colon
docs::papers            ❌
knowledge::caching      ❌
```

## Python Conventions (Mandatory)

### Type Hints on Every Public Function

```python
from typing import Optional, List

def put_memory(project: str, title: str, content: str) -> int:
    """Insert memory entry. Returns ID."""
    ...

def search_memory(query: str, project: Optional[str] = None) -> List[MemoryEntry]:
    """Full-text search. Returns ranked results."""
    ...
```

### Dataclasses for Data Models

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class MemoryEntry:
    id: int
    project: str
    title: str
    content: str
    timestamp: datetime
    ttl: Optional[int]  # days from write; None = permanent
```

### Pytest for Tests (TDD-First)

```bash
# Run tests with coverage
pytest tests/ --cov=nexus --cov-report=term-missing

# Expected: >80% coverage
```

### No TODOs in Comments

**Bad**:
```python
def index_repo(path: str):
    # TODO: add frecency scoring
    pass
```

**Good**:
```python
# Create bead:
bd create "Add frecency scoring to code indexing" -t task -p 1

# In code:
def index_repo(path: str):
    # Frecency scoring computed at index time; staleness known limitation (see spec line 186)
    pass
```

### Documentation Strings on Public Functions

```python
def put_memory(project: str, title: str, content: str) -> int:
    """Insert or upsert a memory entry into T2 SQLite.

    Deterministic upsert on (project, title) key: if an entry with the same
    key exists, content and TTL are updated; otherwise new entry created.

    Args:
        project: Namespace (e.g., 'nexus_pm', 'BFDB_active')
        title: Filename-like key (e.g., 'findings.md', 'phase-1/context.md')
        content: Full text (any size)

    Returns:
        Database row ID (int)

    Raises:
        StorageError: SQLite operation failed
    """
```

## CLI Design Patterns (Follow Existing Tools)

When implementing CLI commands, match patterns from:
- **mgrep**: `mgrep search <query> --store <name> -a -m <count>`
- **SeaGOAT**: `sg status`, `sg index <path>`, `sg install`
- **Arcaneum**: `arc store <path>`, `arc search <query>`

Examples for Nexus:
```bash
# Memory bank (T2) — matches mgrep/SeaGOAT patterns
nx memory put "content" --project nexus_pm --title findings.md
nx memory get --project nexus_pm --title findings.md
nx memory search "query"
nx memory list --project nexus_pm

# Scratch (T1) — session-local, similar to git stash
nx scratch put "hypothesis"
nx scratch search "query"
nx scratch promote <id> --project nexus_pm --title output.md  # T1 → T2

# Search — unified semantic + hybrid interface
nx search "query" --corpus code --hybrid -a
nx search "auth" --corpus docs --mxbai
nx search "caching decisions" --corpus knowledge

# Index — starts/registers with persistent server
nx index code /path/to/repo
nx index pdf /path/to/paper.pdf
nx index md /path/to/notes.md

# Server lifecycle
nx serve start
nx serve stop
nx serve status
```

## Design Patterns to Follow

### 1. Storage Layer Abstraction

Define an interface; implement multiple backends (T1, T2, T3):

```python
from abc import ABC, abstractmethod
from typing import List

class MemoryBackend(ABC):
    """Abstract storage backend."""

    @abstractmethod
    def put(self, project: str, title: str, content: str) -> int:
        pass

    @abstractmethod
    def get(self, project: str, title: str) -> MemoryEntry:
        pass

class MemoryT2(MemoryBackend):
    """SQLite implementation."""
    def put(self, project: str, title: str, content: str) -> int:
        # SQLite upsert
        ...

class MemoryT1(MemoryBackend):
    """In-memory implementation (session scratch)."""
    def put(self, project: str, title: str, content: str) -> int:
        # EphemeralClient put
        ...
```

### 2. Chunking Abstraction

Each indexing pipeline (code, PDF, markdown) implements the same interface:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Chunk:
    text: str
    metadata: dict  # flat: source_path, line_start, page_number, etc.

class ChunkingStrategy(ABC):
    @abstractmethod
    def chunk(self, source_path: str, content: str) -> List[Chunk]:
        """Split source into chunks with metadata."""
        pass

class CodeChunker(ChunkingStrategy):
    def chunk(self, source_path: str, content: str) -> List[Chunk]:
        # llama-index CodeSplitter
        ...

class PDFChunker(ChunkingStrategy):
    def chunk(self, source_path: str, content: str) -> List[Chunk]:
        # PyMuPDF4LLM → markdown chunks
        ...
```

### 3. Error Handling

Define custom exceptions; catch in CLI layer:

```python
class NexusError(Exception):
    """Base exception."""
    pass

class StorageError(NexusError):
    """T1/T2/T3 operation failed."""
    pass

class VoyageAuthError(NexusError):
    """Voyage API key missing or invalid."""
    pass

# CLI:
@click.command()
def search(query: str):
    try:
        results = nx_search(query)
    except VoyageAuthError as e:
        click.secho("Error: VOYAGE_API_KEY not set", fg="red")
        raise SystemExit(1)
    except NexusError as e:
        click.secho(f"Error: {e}", fg="red")
        raise SystemExit(1)
```

## Bead Context Template

When creating a bead, include this context in the description:

```
## Bead: <Title>

**Type**: task | epic | bug | feature | chore
**Phase**: 1-4
**Status**: open | in_progress | done

### Context
- Spec reference: lines XXX-YYY (link to spec section)
- Related beads: <depends on | blocked by>
- .pm/ files: CONTINUATION.md, METHODOLOGY.md (as applicable)

### Success Criteria
- [ ] Feature X implemented and tested
- [ ] Test coverage >80%
- [ ] Code review approved
- [ ] No regression in <component>

### Files to Create/Modify
- `/Users/hal.hildebrand/git/nexus/nexus/storage/t2/memory.py` (create)
- `/Users/hal.hildebrand/git/nexus/tests/unit/storage/test_t2_crud.py` (create)
- `/Users/hal.hildebrand/git/nexus/pyproject.toml` (modify)

### Design Pattern
- **Storage abstraction**: Implement `MemoryBackend` ABC (see METHODOLOGY.md)
- **TDD-first**: Write test → implement → refactor
- **Error handling**: Raise `StorageError` on SQLite failures (custom exception defined)
- **Type hints**: Full hints on all public functions

### Notes
- WAL mode (PRAGMA journal_mode=WAL) is required for multi-session access
- FTS5 external-content mode avoids duplicate content storage
- Session ID is generated by SessionStart hook via `os.getsid(0)`; written to `~/.config/nexus/sessions/{getsid}.session`
```

## Code Review Expectations

Before merging, your code should pass:

- **code-review-expert**: Style, patterns, bugs, maintainability
- **test-validator**: Coverage >80%, no logic untested

Merge blockers:
- ❌ Type hints missing on public functions
- ❌ Coverage drops (current: 0% → target: >80%)
- ❌ No tests added (TDD violation)
- ❌ TODO comments in code (use beads instead)
- ❌ Circular imports (violates layered architecture)
- ❌ Collection names use `:` instead of `__`

## Session Continuity

At session end, update `.pm/CONTINUATION.md`:

1. **Current phase progress** — what % complete?
2. **Last checkpoint** — bead ID of last stable point
3. **Next action** — one sentence: what comes next?
4. **Blockers** — any unknowns or dependencies?

Example:

```markdown
## Current Phase: 1 (Foundation/T2)

### Progress
- T2 schema + WAL: 100% (schema.py complete + tested)
- T2 CRUD: 75% (put/get/list working; delete edge cases pending)
- CLI commands: 0% (blocked by CRUD completion)
- Next checkpoint: Close bead "T2 CRUD complete"

### Last Checkpoint
- Bead: ABC123 "T2 schema + WAL complete; 94% test coverage"
- Location: tests/unit/storage/test_t2_schema.py:test_wal_pragma_set

### Next Action
Implement T2 delete() operation with cascade rules for FTS5 triggers.

### Blockers
None — ready to continue.
```

## Contact and Questions

- **Spec questions**: See `/Users/hal.hildebrand/git/nexus/spec.md` lines 1-910
- **Architecture questions**: See `.pm/METHODOLOGY.md` and CONTEXT_PROTOCOL.md
- **Status and blockers**: Check `.pm/CONTINUATION.md` and active beads (`bd list --status=in_progress`)
- **Session history**: Read `.pm/phases/phase-N/context.md` for phase-specific decisions

## Summary: The Golden Rule

**All work is tracked via beads; all code is type-hinted; all features are tested first.**

- **Before**: Chaotic markdown TODOs, unclear status, no test discipline
- **After**: Clear beads with dependencies, type-safe code, TDD-enforced correctness

Work within this discipline. It's not overhead — it's the foundation that makes complex multi-month projects manageable.
