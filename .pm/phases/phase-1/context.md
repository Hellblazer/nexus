# Phase 1 — Foundation: T2 SQLite + nx memory

**Duration**: 2–3 weeks
**Goal**: Build the local, persistent memory bank (T2) — replacing MCP memory bank with SQL-backed storage + FTS5 keyword search.

## Scope

Phase 1 delivers:

1. **T2 SQLite Storage Layer** (`nexus/storage/t2/`)
   - Schema: memory table (project, title, content, tags, timestamp, ttl, session, agent)
   - FTS5 virtual table with external-content mode (no duplication)
   - Triggers: keep FTS5 in sync with INSERT/UPDATE/DELETE on memory table
   - Connection management: WAL mode (PRAGMA journal_mode=WAL) for concurrent multi-session access
   - CRUD: put, get, delete, list, search (keyword), expire (TTL cleanup)
   - Dataclasses: MemoryEntry, SearchResult

2. **CLI Commands** (`nexus/cli/memory_commands.py`)
   - `nx memory put "content" --project PROJECT --title TITLE [--tags TAGS] [--ttl TTL]`
   - `nx memory get [--id ID | --project PROJECT --title TITLE]`
   - `nx memory delete --id ID`
   - `nx memory list [--project PROJECT] [--agent AGENT] [--limit N]`
   - `nx memory search "query" [--project PROJECT] [--limit N]`
   - `nx memory expire` (manual TTL cleanup)

3. **CLI Infrastructure**
   - Click-based CLI root (nexus/cli/main.py)
   - Config system: `~/.config/nexus/config.yml` + environment variable overrides
   - Config dataclass (ConfigT2, ServerConfig, EmbeddingConfig)
   - T2 database initialization on first command

4. **Test Suite** (TDD-First)
   - Unit tests: schema correctness, indexes, triggers, CRUD ops
   - Integration tests: concurrent WAL access, multi-session simulation
   - Fixtures: isolated test databases, sample data
   - Target: >85% coverage

5. **Project Structure**
   - pyproject.toml: dependencies + build config (Python 3.12+, sqlite3 stdlib)
   - nexus/__init__.py, nexus/__main__.py (CLI entry point)
   - README.md: quick start guide for Phase 1

## Success Criteria

### Functional
- [ ] T2 schema creates successfully on first run (idempotent)
- [ ] put/get/delete/list/search operations all working
- [ ] FTS5 keyword search returns ranked results (rank by FTS5 relevance)
- [ ] TTL cleanup: expire() removes entries where days_since_write > ttl
- [ ] WAL mode verified: multiple concurrent readers + one writer work correctly
- [ ] Deterministic upsert on (project, title) key: put() with same key updates content+TTL

### Quality
- [ ] Test coverage >85% (nexus/storage/t2 module)
- [ ] All public functions have full type hints
- [ ] No circular imports
- [ ] Custom exceptions: StorageError, TTLFormatError raised on failure
- [ ] Docstrings on MemoryDB class and public methods

### Integration
- [ ] CLI commands work end-to-end: `nx memory put ... && nx memory get ... && nx memory search ...`
- [ ] Database file: `~/.config/nexus/memory.db` (created on first command)
- [ ] No external dependencies beyond Python stdlib sqlite3 (until Phase 3)
- [ ] SessionStart hook (deferred to Phase 2) will read from T2

## Key Files to Create

| File | Purpose | Status |
|------|---------|--------|
| `nexus/__init__.py` | Package marker | To create |
| `nexus/__main__.py` | CLI entry point (python -m nexus) | To create |
| `nexus/cli/main.py` | Click CLI root with subcommand routing | To create |
| `nexus/cli/memory_commands.py` | nx memory subcommands | To create |
| `nexus/config.py` | Config dataclasses + env var loading | To create |
| `nexus/storage/t2/memory.py` | MemoryDB class + CRUD ops | To create |
| `nexus/storage/t2/schema.py` | SQLite DDL (CREATE TABLE, triggers) | To create |
| `nexus/storage/t2/models.py` | MemoryEntry, SearchResult dataclasses | To create |
| `tests/conftest.py` | Pytest fixtures (memory_db, config, etc.) | To create |
| `tests/unit/storage/test_t2_schema.py` | Schema correctness, indexes, triggers | To create |
| `tests/unit/storage/test_t2_crud.py` | put/get/delete/list operations | To create |
| `tests/unit/storage/test_t2_fts5.py` | FTS5 search, ranking | To create |
| `tests/integration/test_t2_concurrent.py` | WAL mode multi-session access | To create |
| `pyproject.toml` | Dependencies, build config | To create |
| `README.md` | Quick start guide | To create |

## Design Patterns (From METHODOLOGY.md)

### 1. Dataclasses for Models

```python
@dataclass
class MemoryEntry:
    """Single row in memory table."""
    id: int
    project: str
    title: str
    content: str
    session: Optional[str]
    agent: Optional[str]
    tags: Optional[str]
    timestamp: datetime
    ttl: Optional[int]  # days from write; None = permanent
```

### 2. Custom Exceptions

```python
class NexusError(Exception):
    """Base exception."""
    pass

class StorageError(NexusError):
    """SQLite operation failed."""
    pass

class TTLFormatError(NexusError):
    """Invalid TTL format."""
    pass
```

### 3. Type Hints on All Public Functions

```python
def put(self, project: str, title: str, content: str,
        tags: Optional[str] = None, ttl: Optional[int] = 30) -> int:
    """Insert or upsert memory entry. Returns ID."""
    ...
```

### 4. TDD-First: Test Before Implementation

1. Write test: `def test_put_and_get(memory_db):`
2. Run: `pytest tests/unit/storage/test_t2_crud.py::test_put_and_get` (red)
3. Implement: `def put(...): ...` (green)
4. Refactor: extract common patterns, clean up

## Testing Strategy

### Unit Tests

**`tests/unit/storage/test_t2_schema.py`**:
- Schema creation is idempotent (run twice, no errors)
- All indexes exist (idx_memory_project_title, idx_memory_project, etc.)
- Triggers fire on INSERT/UPDATE/DELETE
- FTS5 virtual table exists and responds to queries

**`tests/unit/storage/test_t2_crud.py`**:
- put() creates new entry; returns ID
- put() with same (project, title) updates content + TTL (upsert)
- get() by ID; get() by (project, title)
- get() non-existent raises MemoryNotFoundError
- delete() removes entry from memory + memory_fts (trigger)
- list() returns entries in project, ordered by timestamp DESC
- list(agent=...) filters by agent name
- expire() removes entries where julianday('now') - julianday(timestamp) > ttl

**`tests/unit/storage/test_t2_fts5.py`**:
- search("query") returns matches ranked by FTS5 BM25 relevance
- search("query", project=...) scopes to project
- search("phrase in quotes") matches exact phrase
- search("word1 OR word2") matches either term
- TTL doesn't affect search results (expire doesn't run automatically)

**`tests/integration/test_t2_concurrent.py`**:
- WAL mode: multiple readers while one writer active
- Session isolation: entries from session A don't interfere with session B
- Trigger correctness under concurrent load: FTS5 stays in sync

### Fixtures

```python
@pytest.fixture
def memory_db(tmp_path):
    """Isolated T2 database for each test."""
    db_path = tmp_path / "memory.db"
    db = MemoryDB(str(db_path))
    db.init_schema()
    yield db
    db.close()

@pytest.fixture
def sample_entries(memory_db):
    """Pre-populate test database."""
    memory_db.put(project="test", title="notes.md", content="# Notes\n\nPhase 1 progress")
    memory_db.put(project="nexus_pm", title="CONTINUATION.md", content="# Continuation...")
    return [...]
```

## TTL Semantics

**TTL format** (--ttl flag):
- `30d` → 30 days from write time
- `4w` → 28 days (4 weeks = 4 * 7 days)
- `permanent` / `never` / `None` → NULL in database (no expiry)
- Default when omitted: `30d`

**Cleanup**:
```sql
DELETE FROM memory
WHERE ttl IS NOT NULL
  AND julianday('now') - julianday(timestamp) > ttl;
```

**Examples**:
```bash
# Permanent entry
nx memory put "strategic decision" --project nexus_pm --title decisions.md --ttl permanent

# 30 days (default)
nx memory put "scratch notes" --project scratch --title notes.md

# 4 weeks
nx memory put "research findings" --project research --title papers.md --ttl 4w

# Cleanup (manual; SessionEnd hook will also call this)
nx memory expire
```

## Configuration System

**T2 database location**: `~/.config/nexus/memory.db` (same dir as config.yml, repos.json in future phases)

**Config file** (`~/.config/nexus/config.yml`):
```yaml
storage:
  t2:
    database: ~/.config/nexus/memory.db

server:
  port: 7890
  headPollInterval: 10

embeddings:
  codeModel: voyage-code-3
  docsModel: voyage-4
  rerankerModel: rerank-2.5
```

**Environment overrides**:
```bash
NX_STORAGE_T2_DATABASE=/custom/path/memory.db
NX_SERVER_PORT=7891
```

**Config loading** (nexus/config.py):
```python
@dataclass
class StorageT2Config:
    database: str = "~/.config/nexus/memory.db"

@dataclass
class NexusConfig:
    storage_t2: StorageT2Config
    server: ServerConfig
    embeddings: EmbeddingConfig

    @classmethod
    def from_env(cls):
        """Load from env vars; fall back to YAML or defaults."""
        ...
```

## Notes and Unknowns

### Known Limitations (Phase 1)

1. **No concurrent writes to same (project, title) key** — SQLite serializes writes anyway (good enough for multi-session agents)
2. **FTS5 ranking tuning** — BM25 parameters use defaults; Phase 2+ can tune if needed
3. **Large content handling** — SQLite TEXT supports multi-MB; Phase 1 focuses on typical use (< 1MB per entry). Larger docs (500+ lines) are fine.

### Deferred to Future Phases

- **T1 in-memory scratch** (Phase 2) — will use EphemeralClient + DefaultEmbedding
- **T3 cloud storage** (Phase 3) — will use ChromaDB CloudClient + Voyage AI
- **SessionStart hook** (Phase 2) — will inject CONTINUATION.md from T2
- **SessionEnd hook** (Phase 2) — will flush T1 scratch to T2 + run expire()
- **Semantic search** (Phase 3+) — T2 is keyword-only; T3 handles vector search

### Open Questions

1. **Session ID generation**: Claude Code doesn't provide CLAUDE_SESSION_ID env var. Solution implemented in Phase 2: SessionStart hook uses `os.getsid(0)` (session group leader PID), writes to `~/.config/nexus/sessions/{getsid}.session`. The flat `current_session` design was rejected as race-prone for concurrent sessions.
2. **Database file permissions**: Should .config/nexus be mode 0700 (user-only) or 0755 (world-readable)? T2 stores agent findings + session notes (semi-sensitive). Decision: 0700 for user-only.

## Dependencies (Phase 1)

**Python 3.12+** (no external deps needed for T2):
- `sqlite3` — stdlib module, always available
- `dataclasses` — stdlib, available in 3.12
- `click` — CLI framework (add to pyproject.toml)
- `pyyaml` — config parsing (add to pyproject.toml)
- `pytest` — testing (dev dependency)
- `pytest-cov` — coverage reporting (dev dependency)

**pyproject.toml** (Phase 1):
```toml
[project]
dependencies = [
    "click==8.1.8",
    "PyYAML==6.0.1",
]

[project.optional-dependencies]
dev = [
    "pytest==8.3.1",
    "pytest-cov==5.0.0",
    "mypy==1.14.1",
]
```

## Bead Structure for Phase 1

Create this epic and sub-beads:

```bash
bd create "Phase 1: Foundation — T2 SQLite + nx memory" -t epic -p 1

# Core storage layer
bd create "T2 schema + WAL setup (schema.py)" -t task -p 1
bd create "T2 CRUD operations (memory.py)" -t task -p 1
bd create "T2 dataclasses + errors (models.py)" -t task -p 1

# CLI layer
bd create "CLI infrastructure (main.py, config.py)" -t task -p 1
bd create "nx memory commands (memory_commands.py)" -t task -p 1

# Tests
bd create "T2 unit tests: schema, CRUD, FTS5" -t task -p 1
bd create "T2 integration tests: WAL concurrent access" -t task -p 1

# Project setup
bd create "pyproject.toml + README" -t task -p 1

# Verify
bd create "Phase 1 validation: all CLI commands working" -t task -p 1
```

Then use `bd dep add` to establish dependencies (e.g., CLI commands depend on core storage layer).

## Next Actions (Phase End Transition)

At end of Phase 1 (after validation passes):

1. Update CONTINUATION.md:
   - Phase 1: 100% complete (T2 working, tested, integrated)
   - Phase 2 ready to start (T1 scratch layer)

2. Create Phase 2 context doc:
   - `nx memory put ... --project nexus_pm --title phases/phase-2/context.md`

3. Relay to Phase 2 agent (strategic-planner):
   - Input artifacts: completed Phase 1 code + tests
   - Deliverable: Phase 2 plan (T1 EphemeralClient + nx scratch commands)

## Validation Checklist (Phase 1 Complete)

- [ ] T2 schema creates successfully; idempotent
- [ ] All CRUD ops working: put, get, delete, list, search, expire
- [ ] FTS5 searches return ranked results
- [ ] Concurrent access verified (WAL mode working)
- [ ] Test coverage >85% (nexus/storage/t2)
- [ ] All public functions type-hinted
- [ ] No circular imports
- [ ] CLI end-to-end: `nx memory put/get/search/list` working
- [ ] Config system: env vars override YAML
- [ ] README complete with quick-start examples
- [ ] Beads closed/done for Phase 1
- [ ] CONTINUATION.md updated with Phase 1 complete status
