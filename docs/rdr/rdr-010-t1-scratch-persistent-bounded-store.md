---
title: "T1 Scratch: Replace EphemeralClient with Persistent Bounded Store"
id: RDR-010
type: architecture
status: open
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-01
related_issues:
  - RDR-008
---

## RDR-010: T1 Scratch: Replace EphemeralClient with Persistent Bounded Store

## Summary

T1 scratch uses `chromadb.EphemeralClient` — pure in-process memory. When a
subagent is spawned via the Agent tool, it starts a new Python process with a
new, empty `EphemeralClient`. No scratch data crosses the process boundary.
This makes the RECOVER step's `nx scratch search` a no-op for virtually every
real agent invocation: agents cannot see notes written by the parent session or
by sibling subagents.

The session ID _is_ propagated for Bash tool calls via
`~/.config/nexus/current_session` (written by the SessionStart hook), but this
mechanism is unreliable for Agent-tool spawns because each spawn runs its own
SessionStart hook and overwrites the file.

**Decision**: Replace `EphemeralClient` with a SQLite-backed store at a
per-session path, bounded by a frecency eviction policy. Retain FTS5 for
keyword search (sufficient for working notes; avoids per-write ONNX embedding
overhead). Add a stable session-ID propagation mechanism that survives
Agent-tool spawns.

## Motivation

1. **The core promise of T1 is broken.** RECOVER (`nx scratch search "[topic]"`)
   is documented in every agent file as a way to recover in-session working
   notes. In practice it returns nothing because the data lives in a dead
   process. This is documentation debt masquerading as a feature.

2. **The fix is shallow.** The `T1Database` interface is already correct: it has
   `put`, `get`, `search`, `flag`, `promote`, `clear`. The only change is the
   backing store — from `EphemeralClient` to SQLite — and the search
   implementation — from ChromaDB vector query to FTS5. The public interface
   does not change.

3. **The tier is the right abstraction.** T1 (fast, bounded, session-scoped),
   T2 (durable, project-scoped), T3 (permanent, semantic) is a sound hierarchy.
   T1 deserves an implementation that matches its spec, not removal.

4. **Bounded store is safe.** An unbounded SQLite would grow with every session.
   A frecency eviction policy caps entries per session and auto-cleans old
   session databases at startup. Resource footprint stays predictable.

## Evidence Base

### Current Implementation

- `src/nexus/db/t1.py`: `T1Database.__init__` calls `chromadb.EphemeralClient()`
  unconditionally when no `client` arg is passed. The docstring says
  "EphemeralClient holds data in-memory only; nothing is written to disk."
- `src/nexus/session.py:10-11`: `CLAUDE_SESSION_FILE = ~/.config/nexus/current_session`
  — flat file written by SessionStart hook. Comment: "Shared by all Bash
  subprocesses within one Claude Code conversation." No mention of Agent-tool
  subagents (a different spawn mechanism).
- `session.py` also retains a legacy `getsid`-keyed path and explicitly notes
  at line 6-8: "os.getsid(0) is NOT used: Claude Code spawns each Bash(...) call
  in its own process session, making getsid different per invocation."

### The Two Failure Modes

**Mode 1 — Bash tool calls**: Session ID is consistent via `current_session`
flat file, but T1 data is EphemeralClient (in-process). A Bash call that runs
`nx scratch search` gets a fresh EphemeralClient with no data. The session ID
matches; the data store is empty.

**Mode 2 — Agent tool spawns**: A new Claude instance runs SessionStart,
potentially overwriting `current_session` with a new UUID. Even if T1 were
file-backed, the session ID key changes between parent and child. The child
cannot find parent entries by session ID.

### What SQLite + FTS5 Provides

- Cross-process reads: any process with filesystem access can read the db
- Atomic writes: SQLite WAL mode handles concurrent writers safely
- Keyword search: FTS5 `MATCH` queries serve working-note recall without
  embedding overhead
- Bounded eviction: standard SQL `DELETE WHERE id IN (SELECT ... ORDER BY score
  ASC LIMIT N)` is sufficient
- Session cleanup: `DELETE WHERE created_at < (now - 24h)` at startup

### Session ID Propagation for Agent-Tool Spawns

The parent session ID needs to survive Agent-tool spawn boundaries. Options:

1. **Env var**: Parent writes `NX_PARENT_SESSION_ID` to environment before
   spawning. Child reads this before running SessionStart — if present, adopts
   parent session rather than generating a new one.
2. **Relay field**: Include session ID as a field in the task relay. Agent
   reads it, passes to `nx scratch` via `NX_SESSION_ID` env var override.
3. **Single well-known path**: Remove session-ID from the file path entirely;
   use `current_session` as the sole key. Pros: simplest. Cons: concurrent
   Claude Code windows collide (regression from current design).

Option 1 is preferred: non-breaking, opt-in, and consistent with the existing
`NX_SESSION_PID` env var pattern already in `session.py`.

## Proposed Solution

### Storage

Replace `chromadb.EphemeralClient()` with a SQLite database at:

```
~/.config/nexus/scratch/{session_id}.db
```

Schema:

```sql
CREATE TABLE entries (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    content     TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '',
    flagged     INTEGER NOT NULL DEFAULT 0,
    flush_project TEXT NOT NULL DEFAULT '',
    flush_title   TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,       -- Unix timestamp
    last_accessed_at REAL NOT NULL,  -- updated on get/search hits
    access_count INTEGER NOT NULL DEFAULT 1
);
CREATE VIRTUAL TABLE entries_fts USING fts5(
    content,
    content='entries',
    content_rowid='rowid'
);
```

### Search

Replace ChromaDB vector query with FTS5 `MATCH`. Return results ordered by
FTS5 rank. The `distance` field in returned dicts is set to `0.0` (FTS5 rank
is not a distance; callers use it for ordering only).

### Frecency Eviction

After each `put`, if `COUNT(*) WHERE session_id = ?` exceeds `MAX_ENTRIES`
(default: 200), delete the `EVICT_BATCH` (default: 20) lowest-scoring entries:

```
frecency_score = access_count / (now - last_accessed_at + 1.0)
```

Entries with low access frequency and old last-access time are evicted first.

### Session Cleanup

On `T1Database.__init__`, delete session databases older than `SESSION_TTL`
(default: 24h). This runs lazily at first use, not on a schedule.

### Session ID Propagation

Add `NX_SESSION_ID` env var override to `session.py::read_claude_session_id()`.
When set, this value is used as the session ID instead of reading the flat file.

Document in agent CONTEXT_PROTOCOL.md: when spawning a subagent that should
share scratch, pass `NX_SESSION_ID={current_session_id}` in the environment
or relay.

## Scope

### Files changed
- `src/nexus/db/t1.py` — new SQLite implementation, same public interface
- `src/nexus/session.py` — add `NX_SESSION_ID` env var override
- `tests/test_t1.py` (or existing scratch tests) — update fixtures, add
  eviction and cross-process session ID tests

### Not in scope
- Semantic/vector search for T1 (FTS5 is sufficient for scratch)
- Server-side session coordination
- Changes to T2 or T3
- Agent file changes (session ID propagation is a documentation addition only)

## Alternatives Considered

### ChromaDB PersistentClient

Swap `EphemeralClient` for `chromadb.PersistentClient(path=...)`. Retains
semantic search. Rejected because: ChromaDB persistent stores don't expose
FTS-style keyword search, native frecency eviction requires custom logic on
top of the ChromaDB API (awkward), and startup cost is higher. The semantic
search advantage over FTS5 is marginal for working notes.

### Keep EphemeralClient, Fix Documentation

Mark RECOVER scratch step as "same-process only; no-op across Agent spawns."
Rejected: documents a broken feature rather than fixing it. The scratch tier
has real value if it works; it is dead weight if it does not.

### Remove T1 Entirely

Collapse T1 into T2 for all scratch use. Rejected: T1's bounded, ephemeral
character is distinct from T2's durable project-scoped semantics. The abstraction
is correct; only the implementation is wrong.

## Trade-offs

### Positive
- RECOVER step becomes reliable for Bash tool call chains and Agent-tool spawns
  (with `NX_SESSION_ID` propagation)
- Resource-bounded: no unbounded growth; old sessions auto-cleaned
- Zero new dependencies: SQLite is stdlib (`import sqlite3`)
- Public interface of `T1Database` is unchanged — no agent file updates required

### Negative
- FTS5 replaces semantic search: "authentication errors" won't match "JWT middleware
  throws on missing claims" without keyword overlap. For working notes this is
  acceptable; for conceptual recall, T3 semantic search is the right tier.
- Agent-tool session sharing requires explicit `NX_SESSION_ID` propagation — it
  is not automatic. Agents that don't pass the env var still get isolated scratch.

### Risks and Mitigations
- **Risk**: Concurrent writes from parallel subagents corrupt the database.
  **Mitigation**: SQLite WAL mode + per-session database file (single writer per
  session ID in practice).
- **Risk**: Eviction deletes an entry the agent was about to access.
  **Mitigation**: Eviction only fires when `MAX_ENTRIES` is exceeded; default of
  200 entries per session is conservative for any single conversation.

## Implementation Plan

1. Implement new `T1Database` using SQLite + FTS5 (same interface)
2. Add `NX_SESSION_ID` env var override to `session.py`
3. Update tests (remove EphemeralClient mocking, add SQLite fixtures)
4. Smoke test: write an entry in one process, read it in a subprocess with the
   same session ID — verify retrieval works

## Research Findings

### Finding 010-01: Hook pair solves Agent-tool session propagation for all agents, including third-party

The session ID propagation problem for Agent-tool spawns (not just Bash subprocesses) can be
solved entirely within the hook layer — no changes needed inside spawned agents.

**Mechanism**:

```
Parent: PreToolUse on "Agent" tool
  → write current session_id to ~/.config/nexus/agent_handoff
    (file includes a UTC timestamp for TTL enforcement)

Child: SessionStart hook
  → read ~/.config/nexus/agent_handoff
  → if file exists AND age < 3 seconds:
      adopt that session_id (write to current_session, delete handoff file)
  → else:
      generate new session_id as normal
```

The 3-second window is the resource management mechanism: tight enough to prevent
accidental session sharing between independent Claude Code windows, but comfortable
given typical subprocess startup latency.

This works for **any** spawned agent — superpowers, third-party, or agents not
written by this project — because the hook is system-level (Claude Code plugin config),
not embedded in the agent's prompt. The agent itself never needs to know about
session IDs.

**SubagentStop** (child side) is the matching post-hook. It fires when the spawned
agent's session ends and can be used to flush flagged scratch entries to T2 before
the db file ages out. Caveat: the global CLAUDE.md documents a known framework bug
where `classifyHandoffIfNeeded is not defined` can appear after SubagentStop runs.
The hook's work completes successfully; the error is cosmetic.

**Implication for Scope**: The `NX_SESSION_ID` env var override proposed in the
original scope (session.py change) is still needed, but the handoff file mechanism
is the primary propagation path, not manual env var passing in the relay. The env
var becomes a manual override / escape hatch.

---

### Finding 010-02: Current hooks (global settings.json) use only SessionStart — Agent PreToolUse is unoccupied

Inspection of `~/.claude/settings.json` shows hooks configured:
- `PreCompact`: `bd prime`
- `SessionStart`: `bd prime`

No `PreToolUse`, `PostToolUse`, `SubagentStop`, or `SessionEnd` hooks exist yet.
The Agent PreToolUse hook (for writing the handoff file) and the SubagentStop hook
(for flushing scratch on session end) are both available slots — no conflicts.

The handoff file path `~/.config/nexus/agent_handoff` is a single flat file (not
per-session, since it's a transient handoff). Concurrent Claude Code windows spawning
agents simultaneously could race on this file. Mitigation: use a `.lock`-style
approach — the child's SessionStart deletes the handoff file after adopting it, and
the write in PreToolUse uses `O_CREAT | O_TRUNC` (atomic on POSIX). The 3-second
TTL further limits the collision window.

## Open Questions

- **NX_SESSION_ID propagation UX**: Should `nx scratch` print the current
  session ID at startup so it can be copy-pasted into a relay? Or is env var
  passthrough the assumed mechanism?
- **MAX_ENTRIES default**: 200 per session is a guess. Should this be
  configurable via `.nexus.yml`?
- **FTS5 vs trigram**: Standard FTS5 tokenizes on whitespace; trigram mode
  enables substring search. Is trigram needed for code symbols
  (e.g. `camelCase` terms)?
