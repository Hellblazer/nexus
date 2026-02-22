# Phase 2 — T1 Session Scratch + nx scratch Commands

**Duration**: 1–2 weeks
**Goal**: Build the in-memory session-local scratch tier (T1) — ephemeral agentic working state using ChromaDB EphemeralClient + DefaultEmbedding.

## Scope

Phase 2 delivers:

1. **T1 In-Memory Storage** (`nexus/storage/t1/`)
   - ChromaDB EphemeralClient (in-process, no persistence)
   - DefaultEmbeddingFunction (local ONNX, all-MiniLM-L6-v2, no API calls)
   - Session ID generation via SessionStart hook (UUID4 → `~/.config/nexus/current_session`)
   - Metadata on each chunk: session_id (for per-session filtering), created_at, tags
   - CRUD: put, get, search, list, clear, flag (for auto-flush to T2)

2. **CLI Commands** (`nexus/cli/scratch_commands.py`)
   - `nx scratch put "content" [--tags TAGS] [--persist]`
   - `nx scratch get <id>`
   - `nx scratch search "query" [--limit N]`
   - `nx scratch list [--limit N]`
   - `nx scratch flag <id> [--project PROJECT --title TITLE]` (mark for SessionEnd flush)
   - `nx scratch unflag <id>`
   - `nx scratch promote <id> --project PROJECT --title TITLE` (manual T1 → T2, immediate)
   - `nx scratch clear` (explicit clear; also happens on SessionEnd)

3. **SessionStart Hook**
   - Generate session ID (UUID4) on session start
   - Write to `~/.config/nexus/current_session` (read by all nx subcommands)
   - Initialize T1 EphemeralClient (fresh, empty on each session)
   - Print "Nexus ready. T1 scratch initialized (session: {session_id})"
   - (Deferred Phase 2+) If PM project exists: inject CONTINUATION.md from T2

4. **SessionEnd Hook**
   - Flush T1 entries marked with flag() to T2 (using project+title stored in metadata)
   - Run `nx memory expire` (clean up TTL-expired T2 entries)
   - Run `nx store expire` (clean up TTL-expired T3 knowledge chunks — Phase 3+)
   - Clear T1 (automatic on session end)

5. **Integration with T2**
   - T1 entries flagged for persistence: metadata stores target (project, title, agent, tags)
   - SessionEnd hook converts T1 flagged entries → T2 memory entries via `nx memory put`
   - Auto-destination: if no explicit (project, title), use `scratch_sessions/{session_id}_{entry_id}`

## Success Criteria

### Functional
- [ ] T1 EphemeralClient initializes on first nx scratch command
- [ ] put/get/search/list all working
- [ ] Search returns results ranked by cosine similarity (no Voyage API calls)
- [ ] flag/unflag marks entries for SessionEnd auto-flush
- [ ] promote() manually copies T1 entry to T2 immediately
- [ ] SessionStart hook generates session ID, writes to `~/.config/nexus/current_session`
- [ ] SessionEnd hook flushes flagged entries to T2 (tested via mock)

### Quality
- [ ] Test coverage >85% (nexus/storage/t1 module)
- [ ] All public functions have full type hints
- [ ] No external API calls during T1 operations (DefaultEmbedding is local)
- [ ] Custom exceptions: StorageError, SessionError

### Integration
- [ ] T1 + T2 work together: promote() creates T2 entry
- [ ] Session ID persists across all nx commands in same session
- [ ] SessionEnd hook behavior testable (can mock hook in unit tests)

## Key Files to Create

| File | Purpose | Status |
|------|---------|--------|
| `nexus/storage/t1/ephemeral.py` | EphemeralClient wrapper + CRUD | To create |
| `nexus/storage/t1/models.py` | ScratchEntry dataclass | To create |
| `nexus/cli/scratch_commands.py` | nx scratch subcommands | To create |
| `nexus/session.py` | Session ID generation + management | To create |
| `.claude/hooks/sessionstart.sh` | SessionStart hook script | To create |
| `.claude/hooks/sessionend.sh` | SessionEnd hook script | To create |
| `tests/unit/storage/test_t1_scratch.py` | T1 CRUD operations | To create |
| `tests/integration/test_t1_t2_promote.py` | T1 → T2 promotion | To create |
| `tests/integration/test_sessionstart_hook.py` | SessionStart hook (mocked) | To create |

## Design Patterns

### 1. Session ID Lifecycle

```
SessionStart hook:
  1. Generate UUID4: session_id = str(uuid.uuid4())
  2. Write to ~/.config/nexus/current_session
  3. Initialize fresh EphemeralClient
  4. Print "Nexus ready. T1 scratch initialized (session: {session_id})"

All nx commands:
  1. Read session_id from ~/.config/nexus/current_session
  2. All T1 operations store session_id in entry metadata
  3. Enables per-session filtering + multi-session safety

SessionEnd hook:
  1. Flush flagged T1 entries to T2
  2. Clear T1 EphemeralClient
  3. (Session ID persists in .config for history; cleaned up manually if needed)
```

### 2. T1 Entry Structure

```python
@dataclass
class ScratchEntry:
    """In-memory session scratch entry."""
    id: str  # ChromaDB-assigned ID (UUID)
    session_id: str  # From ~/.config/nexus/current_session
    content: str  # Text snippet
    tags: Optional[str]  # Comma-separated
    embedding: List[float]  # From DefaultEmbeddingFunction
    created_at: datetime
    flagged: bool  # Whether to auto-flush to T2 on SessionEnd
    # If flagged: where to send on SessionEnd
    flush_project: Optional[str]  # e.g., "nexus_pm", "scratch_sessions"
    flush_title: Optional[str]  # e.g., "findings.md", None for auto
```

### 3. Embedding Strategy

T1 uses DefaultEmbeddingFunction (local ONNX, no API calls):

```python
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

embedding_fn = DefaultEmbeddingFunction()
# Internally: sentence-transformers all-MiniLM-L6-v2 (ONNX format)
# No network calls; ~1-2s latency for typical scratch operations
```

**Why not Voyage AI for T1?**
- Session scratch is ephemeral (wiped at session end)
- Voyage API calls add latency to every search (defeats in-memory cache purpose)
- DefaultEmbedding is fast + good enough for agentic working state
- Saves Voyage API tokens for T3 (persistent knowledge)

## Testing Strategy

### Unit Tests

**`tests/unit/storage/test_t1_scratch.py`**:
- put() creates entry with ID, embedding, session_id
- put() + search() end-to-end: store entry → search for content
- search() returns results ranked by similarity
- flag() marks entry for flush; sets flush_project/flush_title
- unflag() clears flag
- list() returns entries (all or filtered by tag)
- get() by ID
- clear() wipes all entries in session

### Integration Tests

**`tests/integration/test_t1_t2_promote.py`**:
- promote() copies T1 entry to T2 immediately
- T1 entry deleted after promote (or kept with flag)
- T2 entry queryable via `nx memory get`
- TTL correctly set on promoted T2 entry

**`tests/integration/test_sessionstart_hook.py`**:
- Mock SessionStart hook: generates session ID, writes to temp file
- All nx commands read session ID correctly
- Multiple sessions have different session IDs

### Fixtures

```python
@pytest.fixture
def session_id(tmp_path):
    """Generate test session ID."""
    session_file = tmp_path / "current_session"
    sid = str(uuid.uuid4())
    session_file.write_text(sid)
    return sid

@pytest.fixture
def t1_store(session_id, tmp_path):
    """Isolated T1 (EphemeralClient) for tests."""
    # Mock to avoid chromadb dependency in unit tests
    # Or use real EphemeralClient for integration tests
    ...
```

## Session ID Management

### Creation (SessionStart)

```bash
#!/bin/bash
# ~/.claude/hooks/sessionstart.sh

SESSION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
mkdir -p ~/.config/nexus
echo "$SESSION_ID" > ~/.config/nexus/current_session

# Initialize T1 (Python)
python3 -c "
import sys
sys.path.insert(0, '/Users/hal.hildebrand/git/nexus')
from nexus.storage.t1 import EphemeralT1
t1 = EphemeralT1(session_id='$SESSION_ID')
"

echo "Nexus ready. T1 scratch initialized (session: $SESSION_ID)."
```

### Reading (All nx Commands)

```python
# nexus/session.py
def get_current_session_id() -> str:
    """Read session ID from ~/.config/nexus/current_session."""
    session_file = Path.home() / ".config" / "nexus" / "current_session"
    if not session_file.exists():
        raise SessionError("Session not initialized. SessionStart hook may not have run.")
    return session_file.read_text().strip()

# In CLI commands:
@click.command()
def scratch_search(query: str):
    session_id = get_current_session_id()
    t1 = EphemeralT1(session_id=session_id)
    results = t1.search(query)
    ...
```

### Cleanup (SessionEnd)

```bash
#!/bin/bash
# ~/.claude/hooks/sessionend.sh

SESSION_ID=$(cat ~/.config/nexus/current_session 2>/dev/null)

# Flush flagged T1 entries to T2
if [ ! -z "$SESSION_ID" ]; then
  python3 -c "
    import sys
    sys.path.insert(0, '/Users/hal.hildebrand/git/nexus')
    from nexus.storage.t1 import EphemeralT1
    from nexus.storage.t2 import MemoryDB

    t1 = EphemeralT1(session_id='$SESSION_ID')
    t2 = MemoryDB()

    # Flush flagged entries
    for entry in t1.list(flagged=True):
      t2.put(
        project=entry.flush_project or 'scratch_sessions',
        title=entry.flush_title or f'{SESSION_ID}_{entry.id}',
        content=entry.content,
        tags=entry.tags,
      )

    # Cleanup
    t2.expire()
  "
fi
```

## TTL for T1 → T2

When promoting or flushing T1 entries to T2:

```python
def promote(t1_entry_id: str, project: str, title: str, ttl: Optional[int] = 30) -> int:
    """Promote T1 entry to T2.

    Args:
        t1_entry_id: Entry ID in T1
        project: Destination project (e.g., 'nexus_pm')
        title: Destination title (e.g., 'findings.md')
        ttl: TTL in days (default: 30d; can be 'permanent')

    Returns:
        T2 entry ID (int)
    """
    t1_entry = self.t1.get(t1_entry_id)
    t2_entry_id = self.t2.put(
        project=project,
        title=title,
        content=t1_entry.content,
        tags=t1_entry.tags,
        ttl=ttl,
    )
    # Optionally delete from T1 after promote
    return t2_entry_id
```

## Configuration (Phase 2)

**No new config needed for Phase 2** — T1 uses existing `~/.config/nexus/` directory.

**Session file**: `~/.config/nexus/current_session` (readable only by user; mode 0600)

## Dependencies (Phase 2)

**New dependencies**:
- `chromadb` — added to pyproject.toml (already a Phase 3+ dep, can pull forward)
- (Optional) `sentence-transformers` — if DefaultEmbeddingFunction is not bundled with chromadb

**pyproject.toml** (Phase 2 addition):
```toml
[project]
dependencies = [
    # ... existing from Phase 1 ...
    "chromadb==0.5.8",
]
```

## Bead Structure for Phase 2

```bash
bd create "Phase 2: T1 Session Scratch + nx scratch" -t epic -p 1

# Core T1 layer
bd create "T1 EphemeralClient wrapper (ephemeral.py)" -t task -p 1
bd create "T1 CRUD operations (put/get/search/list)" -t task -p 1
bd create "T1 flag/unflag for SessionEnd auto-flush" -t task -p 1

# Session management
bd create "Session ID generation + ~/.config/nexus/current_session" -t task -p 1

# CLI layer
bd create "nx scratch commands (scratch_commands.py)" -t task -p 1
bd create "T1 → T2 promote operation" -t task -p 1

# Hooks
bd create "SessionStart hook: generate session ID, init T1" -t task -p 1
bd create "SessionEnd hook: flush flagged T1 to T2, cleanup" -t task -p 1

# Tests
bd create "T1 unit tests: CRUD, search, flag/unflag" -t task -p 1
bd create "T1 integration tests: promote, SessionEnd flush" -t task -p 1

# Verify
bd create "Phase 2 validation: T1 + T2 integration working" -t task -p 1
```

## Open Questions (Phase 2)

1. **Session ID cleanup**: Should `~/.config/nexus/current_session` be deleted on SessionEnd, or kept for historical reference? Decision: keep file (may be useful for debugging); documented as auto-generated on SessionStart, no manual intervention needed.

2. **Multi-session T1 safety**: Can multiple Claude Code sessions run concurrently? If yes, each session has its own UUID4 (in separate `current_session` files, or read from env?). Decision: each session gets its own SessionStart hook run, so separate UUID4. T1 EphemeralClient is per-process (in-memory), so no cross-session collision. T2 (SQLite) uses WAL mode for concurrent access.

3. **DefaultEmbedding latency**: Is ~1-2s per T1 search acceptable? Expected usage: agents do 2-5 scratch searches per session. If acceptable, proceed. If too slow, revisit (Phase 3+).

## Known Limitations (Phase 2)

1. **No persistence across sessions** — T1 is wiped on SessionEnd by design (working state only)
2. **No explicit embeddings API** — DefaultEmbedding is bundled with ChromaDB; if version mismatches, may fail (add version pinning to pyproject.toml)
3. **Session ID per-session only** — no global session history yet (future: archive session IDs + metadata)

## Deferred to Phase 3+

- **T3 cloud storage** — T1 will promote to T2 (Phase 2); T2 can promote to T3 (Phase 3+)
- **Cross-session search** — T1 is session-local; T2 can search across projects
- **SessionStart PM injection** — CONTINUATION.md injection deferred (need PM infrastructure first)

## Next Actions (Phase 2 Complete)

1. Update CONTINUATION.md:
   - Phase 2: 100% complete (T1 working, SessionStart/End hooks installed)
   - Phase 3 ready to start (T3 CloudClient + code indexing)

2. Create Phase 3 context doc

3. Relay to Phase 3 (strategic-planner)

## Validation Checklist (Phase 2 Complete)

- [ ] T1 EphemeralClient initializes on first command
- [ ] All CRUD ops working: put, get, search, list, flag, promote
- [ ] DefaultEmbedding: no API calls, local inference only
- [ ] Session ID generated + written to ~/.config/nexus/current_session
- [ ] Test coverage >85% (nexus/storage/t1)
- [ ] SessionStart hook: installs in ~/.claude/hooks/
- [ ] SessionEnd hook: flushes flagged T1 entries to T2
- [ ] T1 → T2 promotion working end-to-end
- [ ] Multiple concurrent sessions don't collide (safety verified)
- [ ] All public functions type-hinted
- [ ] No circular imports
- [ ] CLI end-to-end: `nx scratch put/search/promote` working
- [ ] Beads closed/done for Phase 2
- [ ] CONTINUATION.md updated with Phase 2 complete status
