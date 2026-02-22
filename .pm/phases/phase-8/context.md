# Phase 8 — Claude Code Plugin, Hooks, Slash Commands, and nx doctor

**Bead**: nexus-roj
**Blocked by**: nexus-683 (Phase 6), nexus-c28 (Phase 7), nexus-cas (Phase 2)
**Blocks**: nothing (final phase)

**Duration**: 1–2 weeks
**Goal**: Complete integration — Claude Code plugin installer, SessionStart/SessionEnd hooks, nx install/doctor, end-to-end integration tests.

## Scope

### Beads

| Bead | Task |
|------|------|
| nexus-kzt | nx install/uninstall claude-code with SKILL.md generation |
| nexus-1xg | SessionStart hook with T1 init and PM-aware context injection |
| nexus-21o | SessionEnd hook with T1 flush and TTL expiry |
| nexus-cxn | nx doctor health check for all dependencies |

### Technical Decisions

**nx install claude-code**:
- Writes SKILL.md to `~/.claude/skills/nexus/SKILL.md`
- Installs hooks in `~/.claude/settings.json` (SessionStart, SessionEnd)
- Hook scripts: `~/.config/nexus/hooks/sessionstart.sh`, `sessionend.sh`
- SKILL.md template content: nx command reference + store names + common workflows
- `nx uninstall claude-code`: removes SKILL.md + hooks from settings.json; leaves data intact

**SessionStart hook** (full implementation):
- Generate UUID4 session ID; write to `~/.config/nexus/sessions/{ppid}.session` (PID-scoped)
- Initialize T1 EphemeralClient (in-process; hook calls `nx scratch init`)
- PM-aware context injection: detect if CWD has a git repo → look up `{repo}_pm` in T2 → if exists, print CONTINUATION.md content
- Print "Nexus session started (ID: {session_id})"
- Performance target: <2s total hook runtime

**SessionEnd hook** (full implementation):
- Read session ID from `~/.config/nexus/sessions/{ppid}.session`
- Flush T1 flagged entries to T2 (via `nx scratch flush-flagged`)
- Run `nx memory expire` (T2 TTL cleanup)
- Run `nx store expire` (T3 TTL cleanup; guarded query)
- Remove session file `~/.config/nexus/sessions/{ppid}.session`
- Print "Nexus session ended. Flushed {N} entries to T2."

**nx doctor** health checks:
- Python 3.12+ version check
- chromadb installed + CloudClient connectivity (if CHROMA_API_KEY present)
- voyageai installed + test embedding call (voyage-code-3, voyage-4)
- anthropic installed + API key present
- ripgrep on PATH (`which rg`)
- SQLite WAL mode: connect to memory.db, check journal_mode=WAL
- `PRAGMA integrity_check` on memory.db
- Config file exists at `~/.config/nexus/config.yml`
- Sessions dir exists and is writable
- Output: colored pass/fail per check; summary of required vs optional failures

## Entry Criteria

- Phase 2 complete (T1 scratch working, session management working)
- Phase 6 complete (full search working, answer mode working)
- Phase 7 complete (nx pm lifecycle working)
- All earlier phases complete

## Exit Criteria

- [ ] `nx install claude-code` writes SKILL.md + adds hooks to settings.json
- [ ] `nx uninstall claude-code` cleanly removes plugin artifacts
- [ ] SessionStart hook: generates PID-scoped session ID, injects PM context if present
- [ ] SessionStart hook runtime: <2s
- [ ] SessionEnd hook: flushes flagged T1 entries, runs expire, removes session file
- [ ] `nx doctor` checks all 9 dependencies; clear pass/fail output
- [ ] `nx doctor` exits 0 if all required checks pass; exits 1 if any required check fails
- [ ] End-to-end integration test: index → search → answer → store → pm lifecycle → archive
- [ ] pytest >85% coverage on integration/ module
- [ ] SKILL.md template content accurate (tested against real nx commands)

## Testing Strategy

**Unit tests** (`tests/unit/integration/test_install.py`):
- SKILL.md template renders correctly
- settings.json hook injection is idempotent
- Uninstall removes only nexus entries from settings.json

**Unit tests** (`tests/unit/integration/test_doctor.py`):
- Each check independently testable (mockable)
- Missing dependency → check fails with clear message
- All checks pass → doctor exits 0

**Integration tests** (`tests/integration/test_session_hooks.py`):
- SessionStart creates session file at correct PID-scoped path
- SessionEnd flushes flagged entries and removes session file
- Two concurrent sessions don't interfere (PID isolation)

**End-to-end test** (`tests/e2e/test_full_lifecycle.py`):
- `nx index md tests/fixtures/docs/` → indexes markdown
- `nx search "query" --corpus docs` → returns results
- `nx search "query" -a` → produces answer with citations
- `nx store tests/fixtures/docs/doc.md --collection knowledge` → stores
- `nx pm init --project testproject` → creates PM docs
- `nx pm archive` → synthesizes (mock Haiku) → T3 upsert
- `nx pm restore` → reverses decay
- All assertions on output format and exit codes

## Key Files

| File | Purpose |
|------|---------|
| `src/nexus/integration/claude_code/installer.py` | SKILL.md + hooks installation |
| `src/nexus/integration/claude_code/hooks.py` | SessionStart/SessionEnd logic |
| `src/nexus/integration/claude_code/skill_template.py` | SKILL.md template |
| `src/nexus/cli/install_cmd.py` | nx install/uninstall commands |
| `src/nexus/cli/doctor_cmd.py` | nx doctor health checks |
| `tests/unit/integration/test_install.py` | Installer unit tests |
| `tests/unit/integration/test_doctor.py` | Doctor unit tests |
| `tests/integration/test_session_hooks.py` | Hook integration tests |
| `tests/e2e/test_full_lifecycle.py` | End-to-end test |

## SKILL.md Template Content

The generated SKILL.md should include:
- nx memory commands (put/get/search/list/expire)
- nx scratch commands (put/search/flag/promote)
- nx store commands (+ expire guard explanation)
- nx search usage (--corpus, --hybrid, -a, --agentic, --mxbai)
- nx pm commands (init/resume/status/phase/archive/restore/reference)
- nx serve commands (start/stop/status/logs)
- nx collection management
- nx config show/set
- nx doctor
- Common workflows (store findings → search later; PM lifecycle; cross-corpus search)

## Notes

- This is the final phase; after this, Nexus is feature-complete for v1
- All earlier phases should be fully tested before Phase 8 begins
- The end-to-end test is the acceptance test for the entire system
- After Phase 8, create a release tag and update CONTINUATION.md to "complete"
