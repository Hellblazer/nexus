# Changelog

All notable changes to Nexus are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.6.0] - 2026-03-06

### Added
- **`nx memory delete`** (RDR-022) — delete T2 memory entries by `--project`/`--title`,
  `--id`, or `--project`/`--all`. Confirmation prompt shows `project/title` and content
  preview. `--yes` bypasses prompts. `--all` requires `--project` and is mutually
  exclusive with `--title` and `--id`.
- **`nx store delete`** (RDR-022) — delete T3 knowledge entries by exact 16-char `--id`
  or by `--title` (exact metadata match, paginated to handle multi-chunk documents).
  `--collection` is required. `--yes` bypasses the `--title` confirmation prompt.
- **`nx scratch delete`** (RDR-022) — delete a T1 scratch entry by ID prefix (as shown
  by `nx scratch list`). No confirmation prompt (T1 is ephemeral). Session ownership is
  verified before deleting — entries from other sessions cannot be removed.
- `T2Database.delete()` overloaded with `id: int | None` keyword argument, matching the
  `get()` API pattern.
- `T3Database.delete_by_id()`, `find_ids_by_title()` (paginated), `batch_delete()`.
- `T1Database.delete()` with two-step session-ownership check.

### Changed
- `nx store list` now shows the full 16-char document ID (previously truncated to 12),
  enabling copy-paste into `nx store delete --id`.

## [1.5.3] - 2026-03-05

### Docs
- Corrected release notes: 1.5.2 CHANGELOG now includes RDR-020 Voyage AI timeout
  entries that were missing from the initial squash merge.

## [1.5.2] - 2026-03-05

### Added
- **Voyage AI read timeout** (RDR-020) — all `voyageai.Client` construction sites now
  receive `timeout=120.0` (configurable via `voyageai.read_timeout_seconds` in config or
  `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` env var) and `max_retries=3`. Prevents indefinite
  hangs on stalled Voyage AI API calls.
- **Voyage AI transient-error retry** — `_voyage_with_retry` wraps all six Voyage AI
  call sites (CCE embed, fallback embed, standard embed, code embed, rerank) with
  exponential backoff (1 → 2 → 4 s, capped at 10 s) retrying `APIConnectionError` and
  `TryAgain` up to 3 times. Errors handled by the built-in `max_retries` tenacity layer
  (Timeout, RateLimitError, ServiceUnavailableError) are kept disjoint.

### Refactor
- **`nexus.retry` leaf module** — moved `_chroma_with_retry`, `_is_retryable_chroma_error`,
  `_voyage_with_retry`, and `_is_retryable_voyage_error` from `db/t3.py` into a new
  `retry.py` with no `nexus.*` imports. Eliminates a local-import workaround in
  `scoring.py` that was required to avoid a circular-import test-isolation bug.

## [1.5.1] - 2026-03-04

### Fixed
- **ChromaDB transient error retry** — all ChromaDB Cloud network calls in `db/t3.py`,
  `indexer.py`, and `doc_indexer.py` are now wrapped with `_chroma_with_retry` (from
  `retry.py`): exponential
  backoff (2 → 4 → 8 → 16 → 30 s, capped) retrying up to 5 times on HTTP 429/502/503/504
  and transport-level errors (`ConnectError`, `ReadTimeout`). Non-retryable errors raise
  immediately. Fixes multi-thousand-file indexing runs aborted by a single transient 504.

### Docs
- **Transient Error Resilience section** added to `docs/repo-indexing.md` documenting
  retry behaviour and link to RDR-019.
- **Pre-push release checklist** added to `docs/contributing.md` to catch missing
  `uv.lock` commits before tagging.

### Tests
- Unit and integration tests for `_is_retryable_chroma_error` and `_chroma_with_retry`.
- `test_uv_lock_version_matches_pyproject` added to `TestMarketplaceVersion` — CI now
  enforces that `pyproject.toml`, `uv.lock`, and `marketplace.json` all carry the same
  version.

## [1.5.0] - 2026-03-04

### Added
- **Auto-provision T3 databases** — `nx config init` now creates the ChromaDB Cloud tenant
  and database automatically; `nx migrate` has been removed.

### Fixed
- `chroma_tenant` is now optional in credential validation.
- Resolve real tenant UUID before admin calls; use `get_database` for existence check.

### Docs
- White-glove UX polish: help text, wizard flow, troubleshooting, plugin agents/skills,
  and RDR documentation.
- RDR-001, 002, 017, 018 closed as implemented.

### Tests
- Coverage gaps from test-validator audit closed.

## [1.4.0] - 2026-03-03

### Added
- **File lock on `index_repository`** — per-repo `fcntl.flock` prevents concurrent
  indexing of the same repository. Supports `--on-locked skip` (return immediately,
  default) and `--on-locked wait` (block until lock released).
- **`nx hooks install / uninstall / status`** — installs `post-commit`, `post-merge`,
  and `post-rewrite` git hooks that automatically trigger `nx index repo` on each
  commit/merge. Hooks use a sentinel-bounded stanza so they compose safely with
  pre-existing hook scripts.
- **Hooks reminder in `nx index repo`** — on first successful index, if no hooks are
  installed the CLI prints a one-time suggestion to run `nx hooks install`.
- **`nx doctor` hooks check** — reports hook installation status and checks the index
  log for recent errors.

### Removed
- **`nx serve` / Flask / Waitress** — the polling server and all associated code
  (`server.py`, `server_main.py`, `polling.py`, `commands/serve.py`) have been
  deleted. Git hooks replace the auto-indexing use-case. Dependencies `flask>=3.0`
  and `waitress>=3.0` removed from `pyproject.toml`.

### Docs
- `cli-reference.md`: `nx serve` section replaced with `nx hooks` section.
- `repo-indexing.md`: HEAD polling explanation replaced with git hooks explanation.
- `architecture.md`: Server module row replaced with Hooks module row.
- `configuration.md`: `server.port` / `server.headPollInterval` rows removed.
- `contributing.md`: `nx hooks install` added to development setup steps.

## [1.3.0] - 2026-03-03

### Added
- **`--force` flag** on all four `nx index` subcommands (`repo`, `pdf`, `md`, `rdr`) —
  bypasses staleness check and re-chunks/re-embeds in-place. Mutually exclusive with
  `--frecency-only` (repo) and `--dry-run` (pdf).
- **`--monitor` flag** on all four `nx index` subcommands — prints per-file progress
  lines with file name, chunk count, and elapsed time. For `pdf` and `md`, prints
  page range, title, author, and section count after indexing.
- **Auto-enable monitor in non-TTY contexts** — per-file output is now emitted
  automatically when stdout is not a TTY (piped, backgrounded, CI), without needing
  `--monitor`. The flag remains available to force output in interactive sessions.
- **tqdm progress bar** on `repo` and `rdr` subcommands — shows a file-count bar in
  interactive TTY sessions; auto-suppressed when piped or backgrounded.
- **`on_start` / `on_file` progress callbacks** on the indexer layer — `index_repository`
  and `batch_index_markdowns` accept optional callbacks for real-time progress reporting.
- **`return_metadata`** parameter on `index_pdf` and `index_markdown` — returns a dict
  with chunk count, page range, title, author, and section count instead of a plain int.
- **Proactive 12 KB chunk byte cap** (`SAFE_CHUNK_BYTES = 12_288`) — single constant in
  `chroma_quotas.py` enforced across all three chunkers:
  - `chunker.py` escape hatch fixed: single oversized lines are now truncated at the
    UTF-8 boundary instead of emitted as-is.
  - `md_chunker.py` byte cap post-pass added after semantic/naive splitting.
  - `pdf_chunker.py` byte cap post-pass added after char splitting.
  - `t3.py _write_batch` last-resort drop-and-warn for any document exceeding
    `MAX_DOCUMENT_BYTES` (16 384) before upsert.

### Fixed
- **AST chunk line ranges** (RDR-016) — line numbers now derived from
  `node.start_char_idx` / `node.end_char_idx` instead of a hardcoded formula that
  produced systematically wrong ranges.
- **`_run_index` missing registry entry** — returns `{}` instead of raising when the
  path is not registered, preventing unhandled exceptions on first-run edge cases.

### Changed
- **Indexer helpers** return `int` chunk count instead of `bool` — callers get
  actionable count rather than a success/failure flag.

### Docs
- `cli-reference.md` updated with full `nx index` flag coverage: `--force`, `--monitor`
  (with auto-enable note), `--collection`, `--dry-run`, and `--frecency-only` mutual
  exclusion.

## [1.2.0] - 2026-03-03

### Added
- **`ContentClass.SKIP`** — fourth classification category silently ignores known-noise
  files (config, markup, shader, lock) instead of emitting them into `docs__` collections.
  18 extensions skipped: `.xml`, `.json`, `.yml`, `.yaml`, `.toml`, `.properties`,
  `.ini`, `.cfg`, `.conf`, `.gradle`, `.html`, `.htm`, `.css`, `.svg`, `.cmd`, `.bat`,
  `.ps1`, `.lock`.
- **Expanded code extensions** — 9 new extensions classified as CODE: `.proto`, `.cl`,
  `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`, `.hlsl` (Protobuf and GPU
  shaders now indexed into `code__` with `voyage-code-3`).
- **Shebang detection** — extensionless files are classified as CODE when their first two
  bytes are `#!`, SKIP otherwise (catches `Makefile`, `LICENSE`, etc. correctly).
- **Context prefix injection (embed-only)** — each code chunk's embedding text is
  prefixed with `// File: X  Class: Y  Method: Z  Lines: N–M`. The raw chunk text is
  stored in ChromaDB unchanged; only the Voyage AI embedding call sees the prefix.
  Improves recall for algorithm-level queries in domain-specific codebases.
- **14-language class/method extraction** via tree-sitter `DEFINITION_TYPES` mapping
  (Python, Java, Go, TypeScript, Rust, C, C++, C#, Ruby, PHP, Swift, Kotlin, Scala).
  Used to populate the `class_name` and `method_name` fields in the context prefix.
- **AST language expansion** — `AST_EXTENSIONS` expanded from 16 to 28 mappings across
  19 parsers: Kotlin, Scala, Swift, PHP, Lua, Objective-C now receive AST-aware chunking.
- **`preserve_code_blocks`** — `SemanticMarkdownChunker` now defaults to
  `preserve_code_blocks=True`, preventing fenced code blocks from being split mid-content.
- **`_STRUCTURAL_TOKEN_TYPES` blocklist** — `paragraph_open`, `list_item_open`,
  `tr_open`, and similar structural markdown-it-py tokens are filtered so content
  appears exactly once per chunk (eliminates duplication from open/close token pairs).

### Changed
- **Chunk metadata** now includes `class_name`, `method_name`, and `embedding_model`
  fields on all code chunks.

### Removed
- **`--chunk-size` and `--no-chunk-warning`** flags removed from `nx index repo` —
  chunk size is not user-configurable; these flags were dead after the AST-first pipeline.

## [1.1.1] - 2026-03-02

### Fixed
- **`nx doctor` server check** — optional Nexus server now shows `✓` with status in
  detail string instead of `✗` with a Fix: hint, preventing false failures in
  preflight scripts that check exit code.

### Changed
- **Release process docs** — added explicit `uv sync` step and `uv.lock` to the
  `git add` list so lock file is never missed in a release commit.

### Docs
- RDR skill docs: `rdr-close` pre-check aligned with actual command behaviour
  (`"accepted"` not `"final"`); agent and skill counts corrected after PM removal.

## [1.1.0] - 2026-03-02

### Removed
- **`nx pm` command layer** — `nx pm new/status/close/list/archive/restore` commands
  removed. T2 memory (`nx memory`) serves this purpose directly with less overhead.
- **Mixedbread integration** — `--mxbai` search flag and `fetch_mxbai_results()` removed.
  Voyage AI via ChromaDB Cloud covers all semantic search needs.

### Added
- **`bd` and `uv` checks in `nx doctor`** — both reported as optional (informational only,
  no exit 1); `bd` includes install URL when absent.

### Fixed
- **`chroma` CLI no longer required on PATH** — `start_t1_server()` now locates the
  `chroma` entry-point relative to `sys.executable`, so it is always found when
  `conexus` is installed via `uv tool install` or `uv sync`. No separate install step.

## [1.0.0] - 2026-03-01

First stable release. Promoted from rc10 after live validation. No functional changes
from rc10 — this entry marks the API, CLI, and plugin contract as stable.

### Changed
- `Development Status` classifier promoted from `4 - Beta` to `5 - Production/Stable`.

## [1.0.0rc10] - 2026-03-01

### Changed
- Version bump to rc10 for release candidate validation prior to 1.0.0 final.
- Polish pass: CHANGELOG entries for rc7/rc8/rc9, hook script package name fix
  (conexus not nexus), skill count corrected to 28, serena-code-nav added to
  plugin README, free tier callout for ChromaDB and Voyage AI.

## [1.0.0rc9] - 2026-03-01

### Added
- **Storage tier awareness for agents**: SubagentStart hook injects live T1 scratch entries
  into every spawned agent's context — agents see what siblings and parent agents already
  discovered this session without duplicating work.
- **Storage Tier Protocol** in `using-nx-skills` SKILL.md: T3→T2→T1 read-widest-first
  table and T1→persist→knowledge-tidy write path, giving all agents a clear data discipline.

### Fixed
- **T2 FTS5 search crash on hyphenated queries**: `nx memory search "foo-bar"` raised
  `sqlite3.OperationalError: no such column: bar` — FTS5 was interpreting hyphens as column
  filter separators. Added `_sanitize_fts5()` helper that quotes special-character tokens
  before `MATCH`. Trailing `*` prefix wildcard preserved. Applies to `search()`,
  `search_glob()`, and `search_by_tag()`.

## [1.0.0rc8] - 2026-03-01

### Added
- **T1 ChromaDB HTTP server** (RDR-010): replaced `EphemeralClient` with a per-session
  `chroma run` subprocess. All agents spawned from the same Claude Code window share one
  T1 scratch namespace via PPID chain propagation — cross-process `nx scratch` reads and
  writes work correctly across separate shell invocations.
- **`serena-code-nav` skill**: navigate code by symbol — find definitions, all callers,
  type hierarchies, and safe renames without reading whole files.
- **`nx hook session-start` / `session-end`** (RDR-008): nx workflow integration hooks
  for session lifecycle management; T1 server is started on session-start and stopped on
  session-end.
- **`using-nx-skills` skill polish**: full 29-skill directory table with 5 categories,
  Announce step in process flow, 12 red flags (up from 7), `brainstorming-gate` replaces
  `verification-before-completion` in Skill Priority. Registry trigger conditions sharpened
  for knowledge-tidier, orchestrator, and substantive-critic. SessionStart hook matcher
  tightened to `startup|resume|clear|compact`.

### Removed
- **`--agentic` and `--answer` flags** removed from `nx search` (RDR-009): both modes
  required Anthropic API key and added latency for marginal benefit. Answer synthesis and
  agentic refinement are now agent responsibilities via the plugin skill suite.

### Fixed
- **T1 server startup**: removed `--log-level ERROR` from `chroma run` invocation — flag
  was dropped in chroma 1.x and silently caused every T1 start to exit code 2, falling
  back to isolated per-process EphemeralClient.
- **Session file keyed to grandparent PID**: `hooks.py` now calls `_ppid_of(os.getppid())`
  to reach the stable Claude Code PID rather than the transient shell subprocess that dies
  immediately after writing the session file.
- **T1 SESSIONS_DIR test isolation**: added `autouse` pytest fixture redirecting
  `SESSIONS_DIR` to `tmp_path`, preventing tests from discovering a live server's session
  records.

## [1.0.0rc7] - 2026-02-28

### Added
- **File-size scoring penalty for code search** (RDR-006): chunks from large files are
  down-ranked proportionally — `score *= min(1.0, 30 / chunk_count)`. Applied unconditionally
  to all `code__` results regardless of `--hybrid`. Files ≤ 30 chunks are unaffected.
- `nx search --max-file-chunks N`: pre-filters code results to files with at most N chunks
  via a ChromaDB `chunk_count $lte` where filter. Combines with `--where` using `$and`.
- **T2 multi-namespace prefix scan** (RDR-007): SubagentStart hook surfaces all
  `{repo}*` T2 namespaces (not just the bare project namespace) with a cap algorithm:
  5 entries with snippet + 3 title-only + remainder as count per namespace; 15-entry
  cross-namespace hard cap.
- `nx index repo --chunk-size N`: configurable lines-per-chunk for code files
  (default 150, minimum 1).
- `nx index repo --no-chunk-warning`: suppress the large-file pre-scan warning.
- **Large-file pre-scan warning**: detects code files exceeding 30× chunk size before
  indexing and suggests `--chunk-size 80`; adaptive recommendation when chunk size is
  already set.

## [1.0.0rc6] - 2026-02-28

### Fixed
- **CCE query model mismatch** (P0, affected rc1–rc5): `docs__`, `knowledge__`, and `rdr__`
  collections were indexed with `voyage-context-3` (CCE) but queried with `voyage-4`.
  These two models produce vectors in incompatible geometric spaces (cosine similarity ≈ 0.05
  — effectively random noise). All three collection types were returning semantically
  meaningless results since rc1. `code__` collections were unaffected.
  Fix: `corpus.py` returns `voyage-context-3` for CCE collections; `T3Database.search()`
  bypasses the ChromaDB `VoyageAIEmbeddingFunction` for CCE collections and calls
  `contextualized_embed([[query]], input_type="query")` directly. `T3Database.put()`
  likewise uses `contextualized_embed` with `input_type="document"` so single entries
  stored via `nx store put` land in the same CCE vector space as indexed chunks.
  **All CCE-indexed collections (`docs__*`, `knowledge__*`, `rdr__*`) must be re-indexed
  after upgrading from rc1–rc5.**

## [1.0.0rc5] - 2026-02-28

### Added
- **Four-store T3 architecture** (RDR-004): T3 now routes collections to four dedicated
  ChromaDB Cloud databases (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`),
  one per content type. All routing is internal to `T3Database`; no CLI commands change.
- `nx migrate t3`: new command that copies collections from an old single-database T3 store
  to the new four-store layout. Idempotent; copies embeddings verbatim (no re-embedding).
- `nx doctor` now checks connectivity to all four T3 databases when credentials are present.

### Fixed
- Eliminated spurious per-corpus warning noise during `nx search`: warnings now fire once per unmatched corpus term across all collections, not once per internal resolver call

## [1.0.0rc4] - 2026-02-27

### Added
- `/rdr-accept` slash command with gate-result verification and T2 status synchronization
- `rdr-accept` skill: accepts gated RDRs, updates T2 and file frontmatter atomically (RDR-002)
- `-v`/`--verbose` flag for `nx` CLI: enables debug logging for network calls and index operations
- RDR indexing status shown in `nx index repo` output (count of RDRs indexed)
- MCP sequential-thinking server (`.mcp.json`): replaces `nx thought` for compaction-resilient reasoning chains

### Changed
- `nx thought` session isolation now uses Claude session ID instead of `getsid(0)` (RDR-002)
- SessionStart hook: T2 session records synchronized on startup (RDR-002)
- Sequential-thinking skill updated to use `mcp__sequential-thinking__sequentialthinking` instead of `nx thought add`

### Fixed
- `rdr-gate`: strip fenced code blocks before extracting section headings (false negatives on structured RDRs)
- `rdr-list` and all RDR commands: handle `RDR-NNN` naming convention; single-pass Python heredoc
- `hooks.json` format: wrap in `{hooks:{}}` with matcher/hooks nesting (plugin hook discovery was silently failing)
- Empty strings filtered from embedding batches before Voyage AI calls (prevented API errors on sparse content)
- Suppressed `llama_index` pydantic `validate_default` warning and `httpx`/`httpcore` wire-trace noise in `-v` mode
- structlog level in tests follows `pytest --log-level` (default: WARNING); debug logs surface on failure
- `rdr-accept` skill: description, relay template, PRODUCE section, and `nx scratch` reference now conform to plugin structure tests

## [1.0.0rc3] - 2026-02-26

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

[Unreleased]: https://github.com/Hellblazer/nexus/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc10...v1.0.0
[1.0.0rc10]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc9...v1.0.0rc10
[1.0.0rc10]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc9...v1.0.0rc10
[1.0.0rc9]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc8...v1.0.0rc9
[1.0.0rc8]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc7...v1.0.0rc8
[1.0.0rc7]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc6...v1.0.0rc7
[1.0.0rc6]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc5...v1.0.0rc6
[1.0.0rc5]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc4...v1.0.0rc5
[1.0.0rc4]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc3...v1.0.0rc4
[1.0.0rc3]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc2...v1.0.0rc3
[1.0.0rc2]: https://github.com/Hellblazer/nexus/compare/v1.0.0-rc1...v1.0.0rc2
[1.0.0-rc1]: https://github.com/Hellblazer/nexus/compare/v0.4.0...v1.0.0-rc1
[0.4.0]: https://github.com/Hellblazer/nexus/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/Hellblazer/nexus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Hellblazer/nexus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hellblazer/nexus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hellblazer/nexus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Hellblazer/nexus/releases/tag/v0.1.0
