# Nexus — Continuation & Project State

**Project**: nexus
**Created**: 2026-02-21
**Status**: Ready for Phase 1 Implementation

## Current State

### Spec Completion
- Comprehensive 910-line specification completed and finalized
- Covers all 8 phases: Foundation → T2 SQLite → T1 scratch → T3 cloud → nx serve → code/PDF/md indexing → hybrid search → nx pm → Claude Code plugin
- Architecture, storage tiers, indexing pipelines, CLI surface, integration points fully specified
- Technology stack locked: Python 3.12+, ChromaDB (T1/T3), SQLite+FTS5 (T2), Voyage AI, Flask/Waitress, PyMuPDF4LLM, ripgrep

### Dual Audit Status
- **Internal audit (Phase 0)**: PASS — all 25 architectural gaps identified and remediated:
  - TTL translation (T2 NULL ↔ T3 ttl_days=0)
  - Session ID via UUID4 hook (CLAUDE_SESSION_ID doesn't exist)
  - Collection naming: `code__repo` not `code::repo` (FTS5 delimiter conflict)
  - Ripgrep line cache memory-mapped (500MB soft cap with low-frecency fallback)
  - Cross-corpus reranking strategy (per-corpus retrieval + Voyage AI reranker)
  - PM archive synthesis + T2 decay pattern (two-phase, abortable at each step)
  - All metadata schemas (docs__, code__, knowledge__)

- **External audit (post-Phase 0)**: PENDING (awaiting GO verdict)

### Key Decisions Made
1. **Voyage AI embedding API** (not local ONNX) — free tier 200M tokens/month (verified 2026-02-21)
2. **Persistent `nx serve` process** — faster queries, warm ripgrep cache, HEAD polling for auto-reindex
3. **SQLite T2 for memory bank only** — don't over-engineer; T3 ChromaDB for knowledge storage
4. **Mixedbread fan-out via `--mxbai` flag** — opt-in, keeps normal searches fully local
5. **10-second HEAD polling** — matches SeaGOAT baseline; configurable; post-commit hook is optional trigger
6. **Single `nx serve` manages multiple repos** — registry in `~/.config/nexus/repos.json`; per-repo state
7. **T1 uses DefaultEmbeddingFunction** — session scratch doesn't need Voyage semantic fidelity
8. **Cross-corpus: separate retrieval + reranking** — `voyage-code-3` and `voyage-4` scores not comparable
9. **`nx pm` uses T2** — PM docs are a named-file workspace; FTS5 covers keyword search; T3 opt-in via promote
10. **Archive synthesizes to T3** — raw PM docs are drafts; Haiku distills signal into one rich chunk per project
11. **T3 = ChromaDB CloudClient** — already running; no new infrastructure

### Gaps Remediated
- Frecency staleness documentation added (known limitation: frecency-only reindex not v1)
- Voyage model name verification emphasized (SDK doesn't enumerate valid names at import time)
- `nx pm` SessionStart hook defined canonically with T2 SQL query (not filesystem check)
- PM archive idempotency check: skip re-synthesis if T3 chunk written within 5 min
- `nx memory promote` TTL translation: T2 NULL → T3 ttl_days=0, expires_at=""
- Ripgrep 500MB cache: soft limit with warning; low-frecency files omitted but remain searchable via semantic
- `nx pm restore` partial decay handling: restores surviving docs + lists expired titles (doesn't abort)
- `nx pm reference` dispatch rules clarified (semantic query vs project-name lookup)
- `nx collection delete` nuclear option documented (no undo)
- Archive synthesis max 1200 tokens, split into ≤3 chunks if needed

## Next Action

**Phase 1: Foundation (2-3 weeks)**

Start with T2 (SQLite + FTS5) infrastructure:

1. **Core T2 module** (`nexus/storage/t2/memory.py`):
   - SQLite schema setup (memory table, memory_fts virtual table, triggers)
   - WAL mode initialization (PRAGMA journal_mode=WAL)
   - Connection pooling for concurrent multi-session access
   - CRUD operations: put, get, search (keyword), list, expire
   - TTL cleanup job (daily, runs via nx serve and SessionEnd hook)
   - Idempotent operations (upsert on project+title key)

2. **CLI commands** (`nexus/cli/memory_commands.py`):
   - `nx memory put "content" --project ... --title ... --tags ... --ttl`
   - `nx memory get [id | --project ... --title ...]`
   - `nx memory search "query" [--project ...]`
   - `nx memory list [--project ...] [--agent ...]`
   - `nx memory expire` (manual + scheduled)

3. **Test suite** (TDD-first, pytest):
   - Schema correctness (indexes, triggers)
   - Concurrent access under WAL mode
   - TTL semantics: `30d`, `4w`, `permanent`, `never`
   - FTS5 keyword search ranking
   - Edge cases: empty project, very long content (500+ lines)

4. **Integration**:
   - T2 database file: `~/.config/nexus/memory.db` (same location as repos.json, config.yml)
   - Initialize on first `nx memory` command if not present
   - Connection context manager for cleanup

## Recent Learnings

- **Session ID generation**: Claude Code doesn't provide `CLAUDE_SESSION_ID` env var (open feature requests #13733, #17188). Solution: SessionStart hook generates UUID4 and writes to `~/.config/nexus/current_session`, read by all nx subcommands.

- **Collection naming**: ChromaDB + FTS5 metadata queries require careful naming. Double underscore (`code__repo`, `docs__corpus`, `knowledge__topic`) avoids conflicts with FTS5 delimiters. Single colon (Arcaneum pattern `code::repo`) is safe, but `:` in ChromaDB metadata queries can be ambiguous. Stuck with `__` to be explicit.

- **Cross-corpus reranking**: Naively merging results from `voyage-code-3` and `voyage-4` embedding spaces is invalid — similarity scores are not comparable. Must: (1) retrieve independently per corpus, (2) combine, (3) rerank using Voyage's `rerank-2.5` model. This is more complex than flat merging but semantically correct.

- **PM archive pattern**: Two-phase operation (T3 synthesis first, then T2 decay) with abort on any failure ensures PM docs never become permanently inaccessible. T2 can be restored from T3 synthesis if needed. Idempotency check (5-min window) prevents re-synthesis spam if user retries within a window.

## Active Hypotheses

- **H1**: Session scratch (T1) with DefaultEmbeddingFunction is fast enough for agentic iteration without Voyage API calls. Validation: benchmark T1 scratch search (<100ms target) in Phase 2.
- **H2**: HEAD polling every 10 seconds provides adequate freshness for code repos with typical change velocity. Validation: user study with repos that update multiple times per minute; if unsatisfactory, implement inotify/FSEvents file watching (Phase 2+).
- **H3**: Ripgrep line cache at 500MB soft cap (with low-frecency fallback) covers 95%+ of typical code repos without manual tuning. Validation: telemetry from nx serve logs during Phase 1–4 rollout.

## Blockers

None at project start. Will update as development progresses.

## Metrics Summary

| Metric | Target | Status |
|--------|--------|--------|
| Spec completeness | 100% | ✓ Complete (910 lines, 11 decisions, 25 gaps remediated) |
| Phase 1 start date | 2026-02-21 | ✓ Ready |
| Audit verdict | GO | Pending (external) |

## Files Modified

- **Created**: `/Users/hal.hildebrand/git/nexus/.pm/` (entire directory)
- **Active**: `/Users/hal.hildebrand/git/nexus/spec.md` (finalized, no further changes expected in Phase 1)

## Integration Points

- **ChromaDB**: Not active until Phase 3 (T3 CloudClient implementation)
- **Voyage AI**: Not active until Phase 3
- **Anthropic API**: Not active until Phase 3+ (answer synthesis, agentic mode)
- **Mixedbread SDK**: Optional until Phase 6+ (read-only fan-out)
- **ripgrep**: Active from Phase 4 (code indexing + hybrid search)
- **PyMuPDF4LLM**: Active from Phase 5 (PDF extraction)
