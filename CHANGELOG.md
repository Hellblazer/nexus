# Changelog

All notable changes to Nexus are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0-rc3] - unreleased

## [1.0.0-rc2] - 2026-02-26

### Added
- Six RDR slash commands with live context injection (`/rdr-create`, `/rdr-list`, `/rdr-show`, `/rdr-research`, `/rdr-gate`, `/rdr-close`)
  - Each command pre-fetches project state (RDR dir, existing IDs, T2 metadata, active beads, git branch) before invoking the corresponding skill
  - Mirrors the context-injection pattern used by agent commands (`/review-code`, `/create-plan`, etc.)
- Plugin test suite: 752 unit + structural tests covering install simulation, `$CLAUDE_PLUGIN_ROOT` reference integrity, markdown link resolution, hook script presence, and marketplace version consistency
- E2E debug-load scenario (scenario 00): validates plugin load diagnostics, hook script execution, and component discovery via Claude `-p` mode without a live interactive session
- E2E test sandbox now includes locally-cached superpowers plugin alongside nx, enabling cross-plugin skill validation
- E2E isolation guard: verifies `nx@nexus-plugins` loads from dev repo, not the installed v1 cache

### Changed
- RDR skills now read the RDR directory from `.nexus.yml` `indexing.rdr_paths[0]` (default: `docs/rdr`) instead of hardcoding the path — consistent with the nx repo indexer config
- `registry.yaml` RDR skill entries updated with `command_file` references linking skills to their context-injecting command counterparts

### Fixed
- Marketplace version corrected from `1.0.0-rc1` to `1.0.0-rc2` (plugin structure test caught mismatch)
- E2E test harness: Python 3.14 incompatibility with chromadb/pydantic resolved by pinning install to Python 3.12

## [1.0.0-rc1] - 2026-02-25

### Added
- `nx thought` command group: session-scoped sequential thinking chains backed by T2 SQLite
  - `nx thought add CONTENT` — append thought, return full accumulated chain + MCP-equivalent metadata
  - `nx thought show` / `close` / `list` — chain lifecycle management
  - Chains scoped per session via `os.getsid(0)`, expire after 24 hours
  - Semantic equivalence with sequential-thinking MCP server: `thoughtHistoryLength`, `branches[]`, `nextThoughtNeeded`, `totalThoughts` auto-adjustment
  - Compaction-resilient: state stored externally in T2, not in Claude's context window
- `nx:sequential-thinking` skill: replaces external MCP dependency; uses `nx thought add` for compaction-resilient chains
- `/nx-preflight` slash command: checks all plugin dependencies (nx CLI, nx doctor, bd, superpowers) with PASS/FAIL per check
- Plugin prerequisites section in `nx/README.md` with dependency table and install commands
- Smart repository indexing: code routed to `code__` collections, prose to `docs__`, PDFs to `docs__`
- 12-language AST chunking via tree-sitter (Python, JS, TS, Java, Go, Rust, C, C++, Ruby, C#, Bash, TSX)
- Semantic markdown chunking via markdown-it-py with section-boundary awareness
- RDR (Research-Design-Review) document indexing into dedicated `rdr__` collections
- `nx index rdr` command for manual RDR indexing
- Frecency scoring: git commit history decay weighting for hybrid search ranking
- `--frecency-only` reindex flag: update scores without re-embedding
- Hybrid search: semantic + ripgrep keyword scoring with `--hybrid` flag
- Agentic search mode: multi-step Haiku query refinement with `--agentic` flag
- Answer synthesis mode: cited answers via Haiku with `--answer`/`-a` flag
- Reranking via Voyage AI `rerank-2.5` with automatic fallback
- Path-scoped search with `[path]` positional argument
- `--where` filter support for metadata queries
- `-A`/`-B`/`-C` context lines flags for `nx search`
- `--vimgrep` and `--files` output formats
- `nx pm` full lifecycle: init, status, resume, search, archive, restore
- `nx store list` subcommand
- `nx collection verify --deep` deep verification
- Background server HEAD polling for auto-reindex on commit
- Claude Code plugin (`nx/`): 15 agents, 26 skills, session hooks, slash commands
- RDR workflow skills: rdr-create, rdr-list, rdr-show, rdr-research, rdr-gate, rdr-close
- E2E test suite requiring no API keys (1258 tests)
- Integration test suite with real API keys (`-m integration`)

### Changed
- `sequential-thinking` skill now uses `nx thought add` as its tool-call mechanism (compaction-resilient by design)
- All agents previously using `mcp__sequential-thinking__sequentialthinking` updated to use `nx:sequential-thinking` skill
- All 11 agents with sequential-thinking now have domain-specific thought patterns, When to Use, and control reminders
- `nx doctor` improved: Python version check, inline credential fix hints, non-fatal server check
- CLI help text audited and aligned with `docs/cli-reference.md`; 15+ mismatches corrected
- Renamed `nx index code` → `nx index repo`
- Collection names use `__` separator (never `:`)
- Session ID scoped by `os.getsid(0)` (terminal group leader PID) for worktree isolation
- Stable collection names across git worktrees via `git rev-parse --git-common-dir`
- Embedding models: `voyage-code-3` for code indexing, `voyage-context-3` (CCE) for docs/knowledge, `voyage-4` for all queries
- T1 session architecture: shared EphemeralClient store + `getsid(0)` anchor
- Plugin discovery: `.claude-plugin/marketplace.json` at repo root (replaces `nx/.claude-plugin/plugin.json`)
- `nx pm` namespace collapsed; session hooks simplified
- Plugin slash commands: `/plan` → `/create-plan`, `/code-review` → `/review-code`

### Fixed
- CCE fallback metadata bug
- Search round-robin interleaving
- Collection name collision on overflow
- Registry resilience under concurrent access
- Credential TOCTOU race condition
- `nx serve stop` dead code removed
- Indexer ignorePatterns filtering
- Upsert idempotency in doc pipeline
- T1/T2 thread-safe reads

### Removed
- `nx install` / `nx uninstall` legacy commands
- `nx pm migrate` command
- Homebrew tap formula (superseded by `uv tool install`)
- `nx/.claude-plugin/` legacy plugin discovery directory

## [0.4.0] - 2026-02-24

### Added
- nx plugin v0.4.0: brainstorming-gate, verification-before-completion, receiving-code-review, using-nx-skills, dispatching-parallel-agents, writing-nx-skills skills
- Graphviz flowcharts in decision-heavy skills
- REQUIRED SUB-SKILL cross-reference markers
- Companion reference.md for nexus skill
- SessionStart hook for using-nx-skills injection
- PostToolUse hook with bd create matcher

### Changed
- All skill descriptions rewritten to CSO "Use when [condition]" pattern
- Relay templates deduplicated: hybrid cross-reference to RELAY_TEMPLATE.md
- Agent-delegating commands simplified with pre-filled relay parts
- Nexus skill split into quick-ref SKILL.md + detailed reference.md

### Fixed
- PostToolUse hook performance: now fires only on bd create, not every tool use
- Removed non-standard frontmatter fields from all skills

## [0.3.2] - 2026-02-22

### Added
- E2E tests for indexer pipeline and HEAD-polling logic

### Fixed
- `nx serve stop` dead code path

## [0.3.1] - 2026-02-22

### Added
- `nx store list` subcommand
- Integration test improvements: knowledge corpus scoping

### Changed
- README full readability pass: clearer setup path, optional vs required deps

## [0.3.0] - 2026-02-22

### Added
- Voyage AI CCE (`voyage-context-3`) for docs and knowledge collections at index time
- Ripgrep hybrid search: `rg` cache wired to `--hybrid` retrieval
- `--content` flag and `[path]` path-scoping for `nx search`
- `--where` metadata filter, `-A`/`-B`/`-C` context flags, `--reverse`, `-m` alias
- P0 regression test suite
- T3 factory extraction (`make_t3()`) with `_client`/`_ef_override` injection for tests
- `nx pm promote` and `NX_ANSWER` env override
- `nx collection verify --deep` and info enhancements
- Frecency-only reindex flag

### Changed
- Removed pdfplumber in favour of pymupdf4llm
- `search_engine.py` refactored into focused modules (`scoring.py`, `search_engine.py`, `answer.py`, `types.py`, `errors.py`)
- structlog migration

### Fixed
- 10 P0 bugs, 10 P1 bugs, 10 P2 bugs, 5 P3 observations
- CCE fallback metadata bug; `batch_size` dead parameter removed
- `serve` status/stop lifecycle, collection collision, registry resilience
- Credential TOCTOU, env override error handling
- T1 session architecture (getsid anchor, thread-safe reads)

## [0.2.0] - 2026-02-21

### Added
- `nx config` command with credential management and `config init` wizard
- Integration test suite (requires real API keys)
- E2E test suite (no API keys, 505 tests at release)
- T1 session architecture overhaul: shared EphemeralClient + getsid(0) anchor
- Scratch tier fix for CLI use outside Claude Code

### Changed
- Full README rewrite: installation, quickstart, command reference, architecture

### Fixed
- Scratch tier session isolation
- 5-stream global code review: 15 critical/significant fixes (mxbai chunk ID, security, resilience)

## [0.1.0] - 2026-02-21

### Added
- Project scaffold: `src/nexus/` package, `nx` CLI entry point via Click
- T1: `chromadb.EphemeralClient` + ONNX MiniLM, session-scoped scratch (`nx scratch`)
- T2: SQLite + FTS5 WAL, per-project persistent memory (`nx memory`)
- T3: `chromadb.CloudClient` + Voyage AI, permanent knowledge store (`nx store`, `nx search`)
- `nx index repo` (originally `nx index code`): git-aware code indexing with tree-sitter AST
- `nx serve`: Flask/Waitress background daemon with HEAD polling for auto-reindex
- `nx pm`: project management lifecycle (init, status, resume, search, archive, restore)
- `nx doctor`: prerequisite health check
- Claude Code plugin (`nx/`): initial agents, skills, hooks, registry
- Config system: 4-level precedence (defaults → global → per-repo → env vars)
- Hybrid search: semantic + ripgrep keyword scoring
- Answer synthesis: Haiku with cited `<cite i="N">` references
- Agentic search: multi-step Haiku query refinement
- Phase 1–8 implementations covering all CLI surface

[Unreleased]: https://github.com/Hellblazer/nexus/compare/v1.0.0-rc1...HEAD
[1.0.0-rc1]: https://github.com/Hellblazer/nexus/compare/v0.4.0...v1.0.0-rc1
[0.4.0]: https://github.com/Hellblazer/nexus/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/Hellblazer/nexus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Hellblazer/nexus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hellblazer/nexus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hellblazer/nexus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Hellblazer/nexus/releases/tag/v0.1.0
