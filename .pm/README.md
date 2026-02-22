# Nexus Project Management Infrastructure

This directory contains the project management infrastructure for Nexus — a self-hosted semantic search and knowledge system replacing Mixedbread cloud ingest with local indexing pipelines.

## Quick Navigation

**Start here**:
1. Read [`CONTINUATION.md`](./CONTINUATION.md) — project state, current phase, blockers, next action
2. Read [`AGENT_INSTRUCTIONS.md`](./AGENT_INSTRUCTIONS.md) — how to work on Nexus
3. Understand [`METHODOLOGY.md`](./METHODOLOGY.md) — engineering practices: TDD-first, bead tracking, Python conventions

**If you're an agent starting work**:
- Check `bd ready` — what work is unblocked right now?
- Read `.pm/AGENT_INSTRUCTIONS.md` — patterns to follow
- Review `.pm/phases/phase-N/context.md` — current phase scope
- Search `.pm/CONTEXT_PROTOCOL.md` for context recovery if something is missing

**If you're resuming after a break**:
- Read `CONTINUATION.md` first
- Check `bd list --status=in_progress` — what was being worked on?
- Review active phase context
- Query ChromaDB or Memory Bank for recent decisions

## Files in This Directory

| File | Purpose | Read If |
|------|---------|---------|
| **CONTINUATION.md** | Session state snapshot (project phase, progress, blockers, next action) | Starting session or resuming after break |
| **METHODOLOGY.md** | Engineering practices: TDD-first, bead tracking, type hints, dataclasses, pytest | Writing code on Nexus |
| **AGENT_INSTRUCTIONS.md** | Instructions for spawned agents: patterns, examples, bead context template, CLI design | You're an agent starting work |
| **CONTEXT_PROTOCOL.md** | How context flows: storage hierarchy (beads → ChromaDB → T2 → .pm/ → code), relay format, recovery protocol | Context is missing or unclear |
| **phases/phase-1/context.md** | Phase 1 (Foundation): T2 SQLite + nx memory — scope, success criteria, files to create, design patterns | Phase 1 is current or planning Phase 1 |
| **phases/phase-2/context.md** | Phase 2: T1 Session Scratch + nx scratch — SessionStart/End hooks, session ID management | Phase 2 is current or planning Phase 2 |
| **phases/phase-3/context.md** | Phase 3: T3 Cloud + Code Indexing Foundation — ChromaDB CloudClient, Voyage AI, code chunking | Phase 3 is current or planning Phase 3 |
| **phases/phase-4/context.md** | Phase 4: Persistent Server + Hybrid Search — nx serve, HEAD polling, ripgrep cache, scoring | Phase 4 is current or planning Phase 4 |

## Understanding the Project

**What is Nexus?** A self-hosted semantic search + knowledge system that replaces expensive Mixedbread cloud ingest with local indexing pipelines, while keeping ChromaDB in the cloud for permanent storage.

**Why is it needed?**
- Current tools (mgrep, SeaGOAT, Arcaneum, MCP memory bank) are fragmented
- Mixedbread ingest costs money; Nexus indexes locally (free)
- Claude Code agents need unified search + memory that works offline

**How does it work?**
- **T1 (in-memory)**: Session scratch — fast, local, ephemeral
- **T2 (SQLite)**: Persistent memory bank — no network, supports concurrent access
- **T3 (cloud)**: Permanent knowledge — ChromaDB + Voyage AI embeddings

**What does it do?**
- Index code repos (AST-based chunking + git frecency + hybrid search)
- Index PDFs (PyMuPDF4LLM extraction + semantic chunking)
- Index markdown notes (semantic chunking + FTS5 keyword search)
- Answer questions via Haiku synthesis
- Support Claude Code agent workflows (memory, scratch, storage)

## Workflow: A Typical Session

### 1. Session Start

```bash
# Read CONTINUATION.md to know where we are
cat .pm/CONTINUATION.md

# Check what work is ready (unblocked)
bd ready

# Read current phase context
cat .pm/phases/phase-1/context.md  # (or phase-N as appropriate)
```

### 2. Pick a Bead and Work

```bash
# Choose a ready bead
bd show BD-001

# Move it to in_progress
bd update BD-001 --status in_progress

# Follow METHODOLOGY.md: TDD-first
# - Write test first
# - Implement to pass test
# - Refactor
# - Use type hints, dataclasses, proper error handling

# Run tests frequently
pytest tests/ --cov=nexus --cov-report=term-missing
```

### 3. Checkpoint (Every 2 Hours)

```bash
# Create a checkpoint bead when work reaches a stable point
bd create "Checkpoint: T2 CRUD complete, 89% coverage" -t task

# Note what's working, what's stubbed, next action
# (Include in bead context)
```

### 4. Session End

```bash
# Update CONTINUATION.md
nano .pm/CONTINUATION.md
# - What % complete is current phase?
# - What was the last checkpoint?
# - What's the immediate next action?
# - Any blockers or unknowns?

# Sync bead changes
bd sync

# Commit code (if complete feature)
git add -A && git commit -m "..."

# Update phase context if transitioning to next phase
nx memory put "Phase 2: 80% complete, session ended..." \
  --project nexus_pm --title phases/phase-2/context.md
```

## The Three-Layer Context Stack

**Beads** (work tracking — primary)
- What to do right now
- Which tasks are blocked, which are ready
- Dependencies between work items
- Query: `bd ready` (what's unblocked), `bd list --status=in_progress` (current work)

**ChromaDB** (architectural knowledge — permanent)
- Why we made certain decisions
- Architectural patterns we're following
- Research findings
- Query: mgrep or direct ChromaDB semantic search

**T2 Memory + .pm/** (session state — reference)
- Where we are in the project (phase, progress, blockers)
- How to work on this project (methodology, conventions)
- Current phase scope and success criteria

**All three keep in sync**:
- Beads drive daily work
- ChromaDB stores decisions
- T2 Memory tracks progress + next action
- .pm/ files document methodology

## Key Principles

### 1. No Markdown TODOs

**Bad**: Scattered `# TODO:` comments in code
**Good**: Use `bd create` for all work items. Track via `bd list --status=ready`

### 2. TDD-First

**Bad**: Write code, then hope tests pass
**Good**: Write test first (red), implement (green), refactor (green+clean)

### 3. Type Hints Everywhere

**Bad**: Function with implicit `Any` types
**Good**: `def put(project: str, title: str) -> int: ...` (full type hints)

### 4. Bead Context Matters

**Bad**: "Fix bug" (vague)
**Good**: "Fix race condition in T2 WAL concurrent access (see spec line 70-75, design pattern in METHODOLOGY.md, test: tests/integration/test_t2_concurrent.py)"

### 5. Beads Have Dependencies

**Bad**: "I'll do X, then Y, then Z" (implicit ordering)
**Good**: `bd dep add Y X` (Y is blocked by X; makes ordering explicit)

## Common Commands

```bash
# Check what's unblocked and ready to work
bd ready

# See current work in progress
bd list --status=in_progress

# Create new work item
bd create "Implement T2 expire() function" -t task -p 1

# Update bead status
bd update BD-001 --status in_progress

# Add dependency (mark BD-001 blocked by BD-002)
bd dep add BD-001 BD-002

# Sync bead changes to git
bd sync

# Show detailed bead info
bd show BD-001

# View recent beads
bd list | head -10

# List beads by phase
bd list -p 1  # Phase 1 beads
```

## Phase Overview

| Phase | Duration | Focus | Status |
|-------|----------|-------|--------|
| **Phase 1** | 2-3w | T2 SQLite + nx memory | Starting |
| **Phase 2** | 1-2w | T1 scratch + SessionStart/End hooks | Planned |
| **Phase 3** | 2-3w | T3 cloud + code indexing foundation | Planned |
| **Phase 4** | 2-3w | nx serve + hybrid search + ripgrep | Planned |
| **Phase 5** | 2w | PDF + markdown indexing | Planned |
| **Phase 6** | 1-2w | Agentic search + Mixedbread fan-out | Planned |
| **Phase 7** | 1-2w | nx pm project management | Planned |
| **Phase 8** | 1w | Claude Code plugin + integration | Planned |

## Integration Points

- **ChromaDB**: First used in Phase 3 (T3 CloudClient)
- **Voyage AI**: First used in Phase 3 (embedding API)
- **Anthropic API**: First used in Phase 3+ (Haiku synthesis)
- **Ripgrep**: First used in Phase 4 (hybrid search cache)
- **PyMuPDF4LLM**: First used in Phase 5 (PDF extraction)
- **SessionStart/End hooks**: First used in Phase 2 (T1 init + T1→T2 flush)

## Success Criteria (Overall)

Project succeeds when:

- [ ] All 8 phases complete (T1, T2, T3 storage tiers, nx serve, indexing, hybrid search, PM, plugin)
- [ ] T2 tested with >85% coverage (Phase 1 validation)
- [ ] T1 + SessionStart/End hooks working (Phase 2 validation)
- [ ] T3 + code indexing working (Phase 3 validation)
- [ ] nx serve + hybrid search working (Phase 4 validation)
- [ ] PDF + markdown indexing working (Phase 5 validation)
- [ ] Agentic + Mixedbread fan-out working (Phase 6 validation)
- [ ] nx pm project management working (Phase 7 validation)
- [ ] Claude Code plugin installed + slash commands working (Phase 8 validation)
- [ ] All tests passing, coverage >85% throughout
- [ ] No circular imports, all functions type-hinted
- [ ] Documentation complete + accurate

## Troubleshooting

**"I don't know what to work on next"**
→ Run `bd ready` and pick the first one. Check its bead description for context links.

**"I need architectural context"**
→ Read `/Users/hal.hildebrand/git/nexus/spec.md` (full spec). Search ChromaDB for related decisions.

**"Code is failing but I don't understand why"**
→ Check METHODOLOGY.md for error handling patterns. Add logging via Python `logging` module (not print).

**"I'm stuck for 2+ hours"**
→ Create a "blocked" bead with your blocker. Update CONTINUATION.md. Document the unknown. Context survives across sessions.

**"I finished something but where do I commit?"**
→ Use `git add <files> && git commit -m "..."` (no AI attribution per company policy). Reference bead ID in commit message: `References: BD-001`. Always use PRs; never push main directly.

## References

- **Specification**: `/Users/hal.hildebrand/git/nexus/spec.md` (910 lines, full architecture)
- **Tech Stack**: Python 3.12+, SQLite + FTS5, ChromaDB, Voyage AI, Flask/Waitress, PyMuPDF4LLM, ripgrep
- **Similar Tools**: mgrep (UX patterns), SeaGOAT (frecency, hybrid search), Arcaneum (PDF extraction), MCP memory bank (session state)
- **Beads Documentation**: Run `bd help` for full CLI reference
- **Python Conventions**: See METHODOLOGY.md for type hints, dataclasses, pytest, error handling

## Questions?

- **"How do I work on this project?"** → AGENT_INSTRUCTIONS.md
- **"What should I do right now?"** → bd ready + current phase context
- **"Why did we design it this way?"** → CONTEXT_PROTOCOL.md (storage hierarchy) or spec.md (detailed rationale)
- **"How do I debug a failing test?"** → METHODOLOGY.md (testing section)
- **"Where is context stored?"** → CONTEXT_PROTOCOL.md (storage hierarchy)
