# Phase 7 — nx pm Full Lifecycle

**Bead**: nexus-c28
**Blocked by**: nexus-bjm (Phase 1), nexus-odd (Phase 3)
**Blocks**: nexus-roj (Phase 8)

**Duration**: 2–3 weeks
**Goal**: Complete project management lifecycle — init, phase tracking, archive (two-phase commit to T3), restore, reference.

## Scope

### Beads

| Bead | Task |
|------|------|
| nexus-17h | nx pm init, resume, and status commands |
| nexus-b2k | nx pm phase transitions, search, and blocker management |
| nexus-rf3 | nx pm archive with Haiku synthesis and two-phase commit |
| nexus-wpl | nx pm restore and reference commands |
| nexus-6qo | nx pm expire, promote, and close commands |

### Technical Decisions

**Entry criteria**: Phase 1 (T2) + Phase 3 (T3) complete. Note: `nx pm init/resume/status/phase/search/expire` use T2 only; `archive/restore/reference` use T3. Claude Code plugin installer (Phase 8, nexus-kzt) additionally requires Phase 2.

**nx pm namespace**: T2 project `{repo}_pm` (isolated from regular memory entries). Tags: `pm,phase:N,<doc-type>`.

**Standard PM docs**:
- CONTINUATION.md — session resumption context
- METHODOLOGY.md — engineering discipline and workflow
- AGENT_INSTRUCTIONS.md — instructions for spawned agents
- CONTEXT_PROTOCOL.md — context hierarchy and relay format
- phases/phase-N/context.md — phase-specific scope + TDD strategy

**Archive two-phase commit** (T2 → T3 → T2 decay):
1. Synthesize → T3 first: Haiku reads all PM docs from T2 (selection: always include 5 init docs + remaining sorted by most-recently-written; cap: 100 docs or 100K chars). Synthesis chunk stored in `knowledge__pm__{repo}` with `title="Archive: {repo}"`, plus `pm_doc_count` (int) and `pm_latest_timestamp` (ISO 8601) metadata fields.
2. Decay T2 second: `UPDATE memory SET ttl={NX_PM_ARCHIVE_TTL}, tags=replace(tags,'pm,','pm-archived,') WHERE project='{repo}_pm'`

**Idempotency check**: Before re-synthesizing on retry, query T3 for `title="Archive: {repo}"` latest chunk; compare `pm_doc_count` and `pm_latest_timestamp` against current T2 state. If match → skip re-synthesis. Time-based window is insufficient (user may investigate crash for >5 min before retry).

**Failure modes**:
- Haiku synthesis fails → abort, T2 untouched (safe)
- T3 write fails → abort, T2 untouched (safe)
- T2 decay fails → T3 chunk orphaned but harmless; retry uses idempotency check

**nx pm reference**: `collection.get(where={"store_type": "pm-archive", "project": "<arg>"})` — no embedding call for bare-identifier queries.

**BLOCKERS.md**: Lazy creation — only created when first blocker is added via `nx pm block add`. Not scaffolded by `nx pm init`.

## Entry Criteria

- Phase 1 complete (T2 SQLite + nx memory working)
- Phase 3 complete (T3 CloudClient working, VoyageAIEmbeddingFunction configured)
- `ANTHROPIC_API_KEY` configured (for Haiku synthesis)
- `VOYAGE_API_KEY` configured (for T3 knowledge__ collection)

## Exit Criteria

- [ ] `nx pm init` creates 5 standard PM docs in T2 ({repo}_pm namespace)
- [ ] `nx pm resume` outputs CONTINUATION.md content
- [ ] `nx pm status` shows phase, last-agent, blockers
- [ ] `nx pm phase next` creates new phase doc and updates CONTINUATION.md
- [ ] `nx pm search "query"` does FTS5 keyword search across PM docs (GLOB '*_pm')
- [ ] `nx pm block add/list/resolve` manages BLOCKERS.md (lazy creation)
- [ ] `nx pm archive` two-phase commit: Haiku synthesis → T3 + T2 decay
- [ ] `nx pm archive` idempotency: retry with unchanged T2 state skips synthesis
- [ ] `nx pm restore` reverses decay within TTL window; warns on partial expiry
- [ ] `nx pm reference "query"` semantic search on archived syntheses
- [ ] `nx pm expire` cleans up expired PM archive chunks
- [ ] `nx pm close` marks project complete and archives
- [ ] `nx pm promote` elevates PM docs to T3 semantic search
- [ ] pytest >85% coverage on pm/ module

## Testing Strategy

**Unit tests** (`tests/unit/pm/test_pm_lifecycle.py`):
- init: creates 5 standard docs in T2
- status: reads phase + last-agent from CONTINUATION.md
- phase next: creates phase doc, updates CONTINUATION.md
- archive: verify T3 write + T2 decay in correct order
- archive idempotency: second archive with same T2 state skips synthesis
- restore: reverses decay; warns on partial expiry

**Unit tests** (`tests/unit/pm/test_pm_archive.py`):
- Archive failure (Haiku error): T2 untouched
- Archive failure (T3 write error): T2 untouched
- Archive retry: idempotency check via pm_doc_count + pm_latest_timestamp
- pm_doc_count and pm_latest_timestamp stored in T3 metadata

**Integration tests** (`tests/integration/test_pm_round_trip.py`):
- Full lifecycle: init → add docs → archive → restore → reference
- Haiku mocked with stub response
- T3 mock VectorStore (no real CloudClient needed in integration tests)

## Key Files

| File | Purpose |
|------|---------|
| `src/nexus/pm/lifecycle.py` | State machine: init/archive/restore/close |
| `src/nexus/pm/templates.py` | Embedded document templates (5 standard docs) |
| `src/nexus/pm/synthesis.py` | Haiku archive synthesis |
| `src/nexus/pm/blockers.py` | BLOCKERS.md lazy creation and management |
| `src/nexus/cli/pm_cmd.py` | All nx pm subcommands |
| `tests/unit/pm/test_pm_lifecycle.py` | Lifecycle unit tests |
| `tests/unit/pm/test_pm_archive.py` | Archive idempotency tests |
| `tests/integration/test_pm_round_trip.py` | Full lifecycle integration test |

## Archive Synthesis Format

Haiku is prompted to extract from all PM docs:
- Current project state and objectives
- Phase completion status and next actions
- Key architectural decisions and rationale
- Blockers and their resolution paths
- Critical context for session resumption

Output stored as a single structured markdown document in T3 `knowledge__pm__{repo}` collection.
