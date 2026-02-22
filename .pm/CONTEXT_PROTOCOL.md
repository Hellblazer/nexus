# Nexus — Context Protocol

**How context flows through the Nexus project: storage hierarchy, relay format, recovery procedures.**

## Storage Hierarchy (What Lives Where)

### Priority 1: Beads (`bd` CLI)

**Beads are the source of truth for work tracking.**

- **What**: Task tracking system with status (open → in_progress → done) and dependencies
- **Where**: `./.beads/` directory (git-tracked, synced via `bd sync`)
- **Queries**: `bd list --status=ready` (what can start now), `bd show <id>` (detail), `bd list --status=in_progress` (current work)
- **Usage**: All work items tracked here; zero markdown TODOs in code
- **Example bead**:
  ```
  BD-001: "Implement T2 SQLite memory bank"
  Type: epic
  Status: open
  Phase: 1
  Context:
    - Spec: lines 65-149 (T2 schema + query patterns)
    - Files: nexus/storage/t2/memory.py, tests/unit/storage/test_t2_crud.py
    - Success: CRUD working + >80% test coverage
  ```

### Priority 2: ChromaDB

**Permanent knowledge: decisions, research, designs, cross-session context.**

- **What**: Indexed documents (research, architecture decisions, patterns)
- **Collections**:
  - `decision::nexus::<topic>` (e.g., `decision::nexus::collection-naming`, `decision::nexus::t1-vs-t3-trade-offs`)
  - `architecture::nexus::<component>` (e.g., `architecture::nexus::storage-abstraction`)
  - `research::nexus::<area>` (e.g., `research::nexus::voyage-ai-pricing`)
- **Queries**: Use mgrep or direct ChromaDB semantic search
- **Example**:
  ```
  ID: decision::nexus::collection-naming
  Title: "Why double underscore (__) not colon (:) for collection names"
  Content:
    FTS5 queries use `:` as delimiter. Single colon in collection names
    causes query ambiguity. Solution: `code__repo`, `docs__corpus`, `knowledge__topic`.
    Date: 2026-02-21
  ```

### Priority 3: Memory Bank (T2 SQLite)

**Session state: active work notes, agent findings, phase progress.**

- **What**: Structured notes with deterministic retrieval (project + filename key)
- **Where**: `~/.config/nexus/memory.db` (SQLite) + `nexus_pm` project namespace in T2 for PM docs
- **Usage**: Active project state + phase context lives here
- **Queries**: `nx memory get --project nexus_pm --title CONTINUATION.md` (read state), `nx memory search "query"` (FTS5 keyword search)
- **Example entries**:
  ```
  Project: nexus_pm
  Title: CONTINUATION.md
  Content: [project state, next action, blockers]
  TTL: permanent

  Project: nexus_pm
  Title: phases/phase-1/context.md
  Content: [phase 1 goals, success criteria, current progress]
  TTL: permanent
  ```

### Priority 4: .pm/ Directory (Filesystem)

**Detailed methodology, agent instructions, phase context.**

- **What**: Structured markdown files for workflows, conventions, agent guidance
- **Where**: `/Users/hal.hildebrand/git/nexus/.pm/`
- **Files**:
  - `CONTINUATION.md` — session state snapshot (mirrored in T2 for persistence)
  - `METHODOLOGY.md` — engineering practices, TDD discipline, Python conventions
  - `AGENT_INSTRUCTIONS.md` — how spawned agents should work
  - `CONTEXT_PROTOCOL.md` — this document
  - `phases/phase-N/context.md` — phase-specific goals and current state
- **Usage**: Reference for architecture and conventions; read on session start
- **Update frequency**: At session end (CONTINUATION.md); phase files when transitioning phases

### Priority 5: Code Comments & Docstrings

**Implementation details: why this design choice, known limitations, edge cases.**

- **What**: Google-style docstrings on functions; inline comments for non-obvious logic
- **Where**: Source files (`nexus/storage/t2/memory.py`, etc.)
- **Example**:
  ```python
  def put_memory(project: str, title: str, content: str, ttl: Optional[int] = 30) -> int:
      """Insert or upsert memory entry.

      Deterministic upsert on (project, title) key: if an entry exists,
      its content and TTL are updated; otherwise new entry created.

      See CONTINUATION.md for TTL translation between T2 (NULL for permanent)
      and T3 (ttl_days=0 for permanent).
      """
  ```

## Storage Hierarchy: Visual Map

```
┌─────────────────────────────────────────────────────────────┐
│ Beads (bd CLI) — PRIMARY                                    │
│ ├─ All work items: open | in_progress | done                │
│ ├─ Dependencies: bd dep add, bd ready                       │
│ └─ Query: bd list --status=ready, bd show <id>             │
└─────────────────────────────────────────────────────────────┘
                          ↑
                   (work organized via)
                          ↑
┌─────────────────────────────────────────────────────────────┐
│ ChromaDB — PERMANENT KNOWLEDGE                              │
│ ├─ Decisions, research, architecture patterns               │
│ ├─ Collections: decision::*, architecture::*, research::*  │
│ └─ Query: semantic search (mgrep), direct ChromaDB API     │
└─────────────────────────────────────────────────────────────┘
                          ↑
               (architectural context)
                          ↑
┌─────────────────────────────────────────────────────────────┐
│ T2 Memory Bank (SQLite) — SESSION STATE                     │
│ ├─ Active work notes, phase progress, CONTINUATION          │
│ ├─ Project: nexus_pm; titles: CONTINUATION.md, phases/*    │
│ ├─ Persistence: survives restarts, supports concurrent access
│ └─ Query: nx memory get, nx memory search (FTS5)            │
└─────────────────────────────────────────────────────────────┘
                          ↑
                  (detailed workflows)
                          ↑
┌─────────────────────────────────────────────────────────────┐
│ .pm/ Filesystem — METHODOLOGY & CONTEXT                     │
│ ├─ CONTINUATION.md, METHODOLOGY.md, AGENT_INSTRUCTIONS.md   │
│ ├─ phases/phase-N/context.md                                │
│ └─ Read on session start; update at session end             │
└─────────────────────────────────────────────────────────────┘
                          ↑
                (implementation details)
                          ↑
┌─────────────────────────────────────────────────────────────┐
│ Source Code Comments — IMPLEMENTATION                       │
│ ├─ Docstrings on all public functions                       │
│ ├─ Inline comments for non-obvious logic                    │
│ └─ Link to .pm/ files and spec where appropriate            │
└─────────────────────────────────────────────────────────────┘
```

## Context Flow for Agents

### When Agent Starts (SessionStart)

1. **Read CONTINUATION.md** — project state, current phase, blockers, next action
2. **Scan active beads** — `bd list --status=ready | head -5` — what work is unblocked?
3. **Load phase context** — `nx memory get --project nexus_pm --title phases/phase-N/context.md`
4. **Query ChromaDB** (optional) — if researching architecture: search for relevant decisions
5. **Review METHODOLOGY.md** — refresh on engineering practices for this project

### When Agent Works

- **Create beads** for new work items — no markdown TODOs
- **Link to context** — every bead description includes:
  - Spec references (line numbers in spec.md)
  - File paths to create/modify
  - Success criteria (how to verify it works)
  - Design patterns to follow (reference METHODOLOGY.md)
- **Store findings** — if uncovering architectural decisions or research:
  - Create ChromaDB document (e.g., `decision::nexus::async-session-storage`)
  - Use RelayProtocol format (see below)

### When Agent Completes (SessionEnd)

- **Update CONTINUATION.md**:
  - Current phase progress (% complete)
  - Last checkpoint (bead ID of last stable point)
  - Next action (one sentence)
  - Any blockers or unknowns
- **Sync beads**: `bd sync` (persists bead state changes)
- **Store phase summary** (optional): Create T2 memory entry for phase retrospective
- **Relay to successor** (if needed): Use RelayProtocol format below

## Relay Protocol (Agent-to-Agent Handoff)

When one agent completes and passes work to another, use this format:

### Relay Template

```
## Relay: [Target Agent Name]

**Task**: [1-2 sentence description of what the target should do]

**Bead**: [ID] (status: [open | in_progress | done])
- If work is blocked, list blockers: "blocked by XYZ-123"
- If work depends on multiple beads, list all

### Input Artifacts

**ChromaDB**:
- `decision::nexus::t1-vs-t3-trade-offs` (why in-memory T1 uses DefaultEmbedding, not Voyage)
- `architecture::nexus::storage-abstraction` (MemoryBackend ABC design)
- Or "none" if no architectural context needed

**Memory Bank (T2)**:
- `nexus_pm/CONTINUATION.md` — current project state
- `nexus_pm/phases/phase-1/context.md` — phase 1 scope
- Or "none" if not applicable

**Files**:
- `/Users/hal.hildebrand/git/nexus/spec.md` — specification (read first!)
- `/Users/hal.hildebrand/git/nexus/.pm/METHODOLOGY.md` — engineering practices
- `/Users/hal.hildebrand/git/nexus/nexus/storage/t2/memory.py` — T2 implementation (partial)

### Deliverable

[What the receiving agent should produce]

Example:
- "Complete T2 CRUD implementation: get, search, expire operations with >85% test coverage"
- "Code review of nexus/storage/t2/memory.py; identify potential race conditions in WAL mode"
- "Test validator report: coverage gaps, missing edge cases"

### Quality Criteria

- [ ] [Criterion 1 — testable and specific]
- [ ] [Criterion 2 — includes expected threshold if applicable]
- [ ] [Criterion 3]

Example:
- [ ] CRUD operations (put, get, delete, search) all passing with >85% coverage
- [ ] No type hint gaps on public functions
- [ ] FTS5 ranking validated against expected query result order
- [ ] No circular imports in storage layer
```

### Example Relay: Strategic Planner → Implementation Agent

```
## Relay: java-developer

**Task**: Implement T2 SQLite memory bank core (CRUD + schema). T2 is the persistent local storage replacing the MCP memory bank. Complete Phase 1 foundation to unblock T1 scratch (Phase 2).

**Bead**: BD-001 (status: in_progress)
- Depends on: CONTINUATION.md finalized (done)
- Blockers: none (ready to start)

### Input Artifacts

**ChromaDB**:
- `decision::nexus::collection-naming` (why __ not ::)
- `architecture::nexus::storage-abstraction` (MemoryBackend ABC)

**Memory Bank**:
- `nexus_pm/CONTINUATION.md` (project state)
- `nexus_pm/phases/phase-1/context.md` (Phase 1 scope)

**Files**:
- `/Users/hal.hildebrand/git/nexus/spec.md` lines 65-149 (T2 specification)
- `/Users/hal.hildebrand/git/nexus/.pm/METHODOLOGY.md` (TDD-first, type hints, dataclasses)
- `/Users/hal.hildebrand/git/nexus/.pm/AGENT_INSTRUCTIONS.md` (bead context template)

### Deliverable

- `nexus/storage/t2/memory.py` — MemoryDB class with put/get/delete/search/expire operations
- `nexus/storage/t2/schema.py` — SQLite DDL (CREATE TABLE, indexes, triggers)
- `nexus/storage/t2/models.py` — MemoryEntry, SearchResult dataclasses
- `tests/unit/storage/test_t2_crud.py` — unit tests (TDD-first: >85% coverage)
- `tests/integration/test_t2_concurrent.py` — WAL mode concurrent access validation
- Updated `pyproject.toml` with SQLite version pins (if any)
- Updated CONTINUATION.md with Phase 1 progress

### Quality Criteria

- [ ] All CRUD operations (put, get, delete, list, search, expire) implemented and passing tests
- [ ] Test coverage >85% for storage/t2 module
- [ ] All public functions have full type hints (including Optional, List, etc.)
- [ ] FTS5 external-content mode correctly synced via triggers (INSERT/UPDATE/DELETE)
- [ ] WAL mode (PRAGMA journal_mode=WAL) verified in schema setup
- [ ] TTL semantics tested: 30d, 4w, permanent, None all handled correctly
- [ ] No circular imports (storage layer doesn't import CLI)
- [ ] Docstrings on MemoryDB, MemoryEntry, and key functions
- [ ] No TODO comments in code (use beads for future work)
- [ ] Code review approved by code-review-expert
```

### Example Relay: Implementation → Code Review

```
## Relay: code-review-expert

**Task**: Review T2 implementation for style, patterns, and correctness. Identify potential bugs, race conditions under WAL mode, type hint gaps.

**Bead**: BD-001 (status: in_progress) → (will move to done after review)

### Input Artifacts

**Code**: Pull request with commits:
- nexus/storage/t2/memory.py (main implementation)
- nexus/storage/t2/schema.py (DDL)
- nexus/storage/t2/models.py (dataclasses)
- tests/unit/storage/test_t2_crud.py
- tests/integration/test_t2_concurrent.py

**Context**:
- `/Users/hal.hildebrand/git/nexus/spec.md` lines 65-149 (T2 spec)
- `/Users/hal.hildebrand/git/nexus/.pm/METHODOLOGY.md` (conventions)

### Deliverable

Code review report with:
- ✓ Pass / ✗ Fail on type hints, style, patterns, potential bugs
- Specific line numbers for issues
- Suggested fixes or alternative approaches
- Category tags: "type-hint", "race-condition", "fts5", "style", "test"

### Quality Criteria

- [ ] All public functions have complete type hints
- [ ] No obvious race conditions under concurrent WAL access
- [ ] FTS5 triggers correctly maintain sync
- [ ] Dataclasses used for all data models (not dicts)
- [ ] Error handling via custom exceptions (StorageError, TTLFormatError)
- [ ] No TODO comments (or TODOs are tracked as beads)
- [ ] Follows METHODOLOGY.md Python conventions
```

## Context Recovery (RECOVER Protocol)

**If expected context is missing when you start:**

1. **Search ChromaDB** — `mgrep search "topic" --store art -a` (or directly query ChromaDB)
2. **Check Memory Bank** — `nx memory list --project nexus_pm` (last 10 entries)
3. **Query beads** — `bd list --status=in_progress` (current work), `bd show <id>` (detail)
4. **Read spec.md** — if architectural context is unclear, spec is authoritative
5. **Read .pm/ files** — METHODOLOGY.md, AGENT_INSTRUCTIONS.md, CONTINUATION.md
6. **Flag incomplete relay** — if critical context is missing, create a bead: "Context recovery needed for [component]"

If context recovery uncovers gaps:
- Document the gaps in a bead
- Add as a blocker for dependent work
- Update CONTINUATION.md with unknowns

## Naming Conventions

### ChromaDB Collection IDs

Format: `{domain}::{agent-type}::{topic}`

Examples:
```
decision::nexus::collection-naming
decision::nexus::voyage-pricing-2026-02-21
architecture::nexus::storage-abstraction
research::nexus::llama-index-versioning
discovery::nexus::code-chunking-strategies
```

**Not**:
```
nexus_decisions_collection_naming  (too verbose, not machine-readable)
T2_schema_thoughts                 (vague domain/agent)
```

### Memory Bank (T2) Entries

**Project**: `{repo}_pm` (e.g., `nexus_pm`, `BFDB_active_pm`)

**Titles** (filename-like keys):
```
CONTINUATION.md                      (session state)
METHODOLOGY.md                       (engineering practices)
AGENT_INSTRUCTIONS.md               (agent workflow)
CONTEXT_PROTOCOL.md                 (this document)
phases/phase-1/context.md           (phase 1 scope)
phases/phase-2/context.md           (phase 2 scope)
BLOCKERS.md                         (open blockers)
retrospective-phase-1.md            (phase completion summary)
decisions-2026-02-21.md             (archived decisions)
```

**Tags**: Comma-separated, machine-readable
```
pm,phase:1,context                  (phase 1 context doc)
pm,phase:2,findings                 (phase 2 research)
pm-archived,phase:1,completed       (archived project)
decision,caching                    (decision document)
architecture,storage                (architecture notes)
```

### Beads

**IDs**: Tool-generated (bd automatically assigns)

**Titles**: Concise action-oriented phrases
```
"Implement T2 SQLite memory bank"
"Debug WAL mode race condition under concurrent load"
"Code review: storage layer T2 implementation"
"Research: Voyage AI pricing 2026-02-21"
"Fix: FTS5 trigger for UPDATE operation"
```

**Types**: epic | task | bug | feature | chore

**Dependencies**: Express via `bd dep add <bead-a> <bead-b>` (bead-a blocked by bead-b)

## Summary: The Three-Layer Context Stack

| Layer | Purpose | Example |
|-------|---------|---------|
| **Beads** | Work tracking (PRIMARY) | BD-001: "Implement T2 CRUD" (open, no blockers) |
| **ChromaDB** | Permanent decisions & research | `decision::nexus::collection-naming` (why __ not ::) |
| **T2 Memory + .pm/** | Session state + methodology | CONTINUATION.md (Phase 1: 60% complete, next: implement CRUD) |

All three layers are kept in sync:
- Beads drive daily work ← what to do now
- ChromaDB stores decisions ← why we do it that way
- T2 Memory tracks progress ← where we are

On session start: Read in order (1) CONTINUATION.md, (2) active beads, (3) ChromaDB decisions.
On session end: Update (1) CONTINUATION.md, (2) bead status, (3) ChromaDB if new architectural insights emerge.
