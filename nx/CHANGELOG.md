# Changelog

All notable changes to the nx plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.8.2] - 2026-04-11

Plugin release: RDR-065 close-time funnel hardening. New gates defend the
RDR close ritual against silent scope reduction.

### Added

- **RDR template scaffold (RDR-065 Gap 4)** — TEMPLATE.md grew an
  `### Enumerated gaps to close` subsection with `#### Gap N:` placeholders
  and a documented heading regex (`^#### Gap \d+:`). The close skill
  enumerates these.
- **Two-pass Problem Statement Replay preamble** in `commands/rdr-close.md`
  (RDR-065 Gap 1). Pass 1 lists gaps and exits cleanly; Pass 2 validates
  per-gap `--pointers` (key coverage + file existence) and sets a T1
  scratch active-close marker. ID-based grandfathering for pre-065 RDRs.
- **`### Step 1.5: Problem Statement Replay`** in `skills/rdr-close/SKILL.md`
  — user-facing wrapper around the preamble's four outcomes plus the
  verbatim "structural-not-semantic" framing prompt.
- **`hooks/scripts/divergence-language-guard.sh`** — new PostToolUse hook
  on `Write|Edit` for post-mortem files. Locked Rev 4 8-pattern regex bank;
  markdown header / table-row pre-filter; advisory only (never blocks).
- **`bd create` enforcement branch** in
  `hooks/scripts/pre_close_verification_hook.sh`. During an active RDR
  close, follow-up beads that mention the RDR must include `reopens_rdr`,
  `sprint`/`due`, and `drift_condition` metadata. Missing markers → deny.

## [3.8.1] - 2026-04-10

Plugin version aligned with Nexus CLI 3.8.1. Patch release — bug
fixes only, no plugin-level functional changes. All four fixes are
internal to the core CLI and MCP servers:

- T1.promote() overlap detection no longer misses similar-but-not-
  identical content (RDR-057 fix)
- T2Database.delete() now cascades to taxonomy topic_assignments
  so nx memory delete no longer leaves orphan rows
- nx catalog link --help lists `formalizes` among built-in types
- docs/mcp-servers.md corrects the `link-bulk` / `link-bulk-delete`
  command name

## [3.8.0] - 2026-04-10

Plugin version aligned with Nexus CLI 3.8.0 (RDR-063: T2 Domain Split).
No plugin-level functional changes — the T2 refactor is internal to the core
CLI and MCP servers. Documentation and README precision fixes only.

### Fixed

- `nx/README.md` header: 32 → 33 skills.
- `nx/README.md` "What You Get" bullets: 32 → 33 skills; 10 → 11 standalone
  skills (adds the missing `catalog` skill, which was orphaned from the
  listings).
- `nx/README.md` Directory Structure: added `catalog/` to the skills tree
  and refreshed the `hooks/scripts/` listing to enumerate all 10 scripts
  (session_start, rdr, post_compact, stop_failure, stop_verification,
  pre_close_verification, subagent-start, auto-approve-nx-mcp, plus the
  two shared helpers). The previous listing showed only 4 of 10.
- `nx/README.md` Standalone Skills (10) → (11), with a `catalog` row added.
- `nx/README.md` Hooks table rewritten to match `hooks.json` — removed
  `bd prime` entries that don't exist in the hook wiring; added missing
  `PostCompact` (`post_compact_hook.sh`), `Stop` (`stop_verification_hook.sh`),
  `StopFailure` (`stop_failure_hook.py`), `PreToolUse` (Bash, bd-close gate),
  and `PermissionRequest` (auto-approve MCP) entries.
- `nx/README.md` agent directory note: "14 specialized + 2 internal" reworded
  to "14 command-invoked + 2 query-dispatched" — the two dispatched agents
  (`analytical-operator`, `query-planner`) are not "internal"; they're
  invoked via the `query` skill's planner/operator pipeline.

## [3.7.0] - 2026-04-10

### Added
- **MCP dual-server architecture (RDR-062)** — Plugin now bundles two MCP
  servers instead of one: `nexus` (15 core tools: search, query, store,
  memory, scratch, plans, consolidation) and `nexus-catalog` (10 catalog
  tools with short names: search, show, list, register, update, link,
  links, link_query, resolve, stats — no `catalog_` prefix). Total 25
  registered tools (down from 30, with 6 admin operations demoted to
  CLI-only). Auto-approve hook updated; agents and skills migrated to
  new full tool names.
- **`memory_consolidate` tool** documentation in `nx/skills/nexus/reference.md`
  with `dry_run` and `confirm_destructive` safety gates explained
- **`formalizes` link type** added to catalog skill documentation
- **Contradiction flag rendering** in search output: `[CONTRADICTS ANOTHER RESULT]`
  labels surface when same-collection results have conflicting provenance
- **PromotionReport return format** documented under `scratch_manage` tool

### Changed
- All skills and agents using catalog tools migrated from
  `mcp__plugin_nx_nexus__catalog_*` to `mcp__plugin_nx_nexus-catalog__*`
  (24 files touched by mechanical sed + 6 manual content cleanups)
- Agent CONTEXT_PROTOCOL updated: T2 row mentions `memory_consolidate`
  and heat-weighted TTL; catalog row uses short tool names
- Session-start and subagent-start hooks inject capability summaries
  reflecting the new dual-server layout

### Fixed
- Permission auto-approve hook now covers all 15 core + 10 catalog tools
  (previously 14 core — missing `memory_consolidate`)
- Plugin audit: all stale references to demoted tools
  (`store_delete`, `collection_info`, `collection_verify`, `catalog_unlink`,
  `catalog_link_audit`, `catalog_link_bulk`) removed from live agent and
  skill guidance
- Known-defect regression test for `get_topic_docs()` JOIN bug

## [3.6.5] - 2026-04-09

### Fixed
- Version bump only — no nx plugin changes in this release.

## [3.6.4] - 2026-04-09

### Fixed
- **plugin.json version stuck at 3.2.3** — Claude Code uses `nx/.claude-plugin/plugin.json` version to decide cache refresh. Was never bumped since initial creation, so no nx plugin updates were reaching users. Now bumped to 3.6.4 and added to release checklist.

## [3.6.3] - 2026-04-09

### Fixed
- **Phantom Serena tool names** — `rename_symbol`, `restart_language_server`, `get_current_config`, `activate_project` replaced or removed across serena-code-nav skill, registry.yaml, and 3 downstream skills.
- **Wrong MCP prefixes** — `mcp__plugin_serena_serena__` → `mcp__plugin_sn_serena__` in serena-code-nav and registry; `mcp__sequential-thinking__` → `mcp__plugin_nx_sequential-thinking__` in 7 skills + 1 command.

### Changed
- **Backend-agnostic Serena injection** — SubagentStart hook discovers tools via dual-variant ToolSearch (JetBrains + LSP) and delegates parameter docs to Serena's `initial_instructions`. Works for both backend configurations.
- **Generic Serena names in skills** — debugging, development, architecture skills use backend-neutral names in pseudocode.

## [3.6.1] - 2026-04-08

### Fixed
- **Subagent hook catalog context** — linked RDRs now shown for all agent types, including code-nav and review agents.

## [3.6.0] - 2026-04-08

### Added
- **`nx:catalog` skill** — agent-friendly catalog manipulation: resolve tumblers, create links, seed auto-linker context, discover unlinked entries.
- **Ambient catalog context** — subagent-start hook extracts file paths from task text and shows linked RDRs automatically.
- **Link-context seeding** across all T3-storing skills (15 total): code-review, codebase-analysis, plan-validation, substantive-critique, rdr-gate, knowledge-tidying, strategic-planning, rdr-close + the 6 previously seeding skills.

### Changed
- **Link-boosted query results** — `query` MCP tool now automatically boosts results from documents with `implements` links. No agent changes needed.
- **CONTEXT_PROTOCOL** — added catalog link graph as search source #4 for proactive agents.

## [3.5.2] - 2026-04-08

Plugin version aligned with Nexus CLI 3.5.2. No plugin-level changes.

## [3.5.1] - 2026-04-08

### Fixed
- **stop_failure_hook.py** now executable (was 644, hook would fail on StopFailure events).
- **All hook scripts hardened** — removed `set -euo pipefail` from advisory hooks (stop, close) and permission auto-approve hooks (nx, sn). Prevents silent failures under resource pressure.
- **Agent frontmatter** — 8 color mismatches and 2 version mismatches synced to registry.yaml.

### Docs
- README: 14→16 agents, 28→32 skills.

## [3.5.0] - 2026-04-08

Plugin version aligned with Nexus CLI 3.5.0. No plugin-level functional changes.

### Fixed
- **Advisory hooks hardened** — removed `set -euo pipefail` from stop and close verification hooks.
- **Hook stdout leak** — `nx catalog sync` output redirected to `/dev/null` in stop hook.

## [3.4.0] - 2026-04-08

### Removed
- **Orchestrator agent** — `nx/agents/orchestrator.md` deleted, removed from registry.yaml agent block and sonnet model group.

### Changed
- **Orchestration skill** — converted to standalone reference skill (no agent dispatch). Routing tables, pipeline templates, and decision framework preserved in new `reference.md`.
- **using-nx-skills** — Process Flow DOT graph extended with plan library nodes. New "Plan Reuse" section wires `plan_search`/`plan_save` into multi-agent dispatch.
- **Cross-references** — CONTEXT_PROTOCOL, RELAY_TEMPLATE, rdr-accept updated from "orchestrator" to "caller".
- **README** — 14 agents, 10 standalone skills. Orchestration directory comment updated.

### Added
- **`nx/skills/orchestration/reference.md`** — routing graph, quick reference table, decision framework, standard pipelines, and pipeline pattern catalog.
- **5 pipeline templates** in T2 plan library (permanent): RDR Chain, Plan-Audit-Implement, Research-Synthesize, Code Review, Debug.
- **`orchestration`** entry in `registry.yaml` `standalone_skills`.

## [3.3.1] - 2026-04-07

Plugin version aligned with Nexus CLI 3.3.1. No plugin-level functional changes.

## [3.3.0] - 2026-04-07

### Changed
- **Nexus reference skill** — `search` tool docs updated with `cluster_by` param, `section_type` filter example, automatic quality features note. `collection_verify` updated to multi-probe description.
- **Session start hook** — search capability line includes `cluster_by` and `section_type` hints.
- **Subagent start hook** — search tool signature includes `cluster_by` param and section_type filter hint.

### Docs
- RDR-056 closed (implemented).

## [3.2.5] - 2026-04-07

### Fixed
- **voyage-4 eradication** — removed from rdr-close skill, E2E test harness, and all user-facing docs.

### Docs
- RDR-056, RDR-057, RDR-058, RDR-059 added to RDR index.

## [3.2.4] - 2026-04-07

Plugin version aligned with Nexus CLI 3.2.4. No plugin-level functional changes.

## [3.2.3] - 2026-04-07

Plugin version aligned with Nexus CLI 3.2.3. No plugin-level functional changes.

## [3.2.2] - 2026-04-07

### Fixed
- Added `nx/.claude-plugin/plugin.json` manifest
- Fixed 9 agents with non-standard color values (amber, teal, mint, gold, coral, emerald, indigo, lime)
- Added `sequential-thinking` to MCP auto-approve hook

## [3.2.1] - 2026-04-07

### Fixed
- MCP auto-approve hook uses explicit full tool names (28 tools) instead of wildcard
- 5 agents self-seed link-context from task prompt when dispatched without a skill
- Mandatory T3 store_put with HARD-GATE enforcement in 5 analysis/research agents
- rdr-gate stores critique to T3 on both pass and fail
- rdr-research seeds link-context before agent dispatch

## [3.2.0] - 2026-04-06

### Added
- 5 dispatching skills (development, debugging, research-synthesis, deep-analysis, architecture) now seed T1 scratch with `link-context` before agent dispatch, enabling automatic catalog link creation at storage boundaries.

## [3.1.2] - 2026-04-06

### Added
- SubagentStart hook documents sub-chunk span format `chash:<hex>:<start>-<end>`

## [3.1.1] - 2026-04-06

Plugin version aligned with Nexus CLI 3.1.1. No plugin-level functional changes.

## [3.1.0] - 2026-04-06

### Added
- `catalog_link_audit` MCP tool now performs content-hash span verification against T3 automatically
- Agents creating links can use `chash:<sha256hex>` spans for content-addressed chunk references (preferred over positional spans)
- All 7 link-creating agent tool signatures include `from_span`/`to_span` with chash: format
- SubagentStart hook injects chash: span guidance alongside catalog tools
- CONTEXT_PROTOCOL updated with Catalog tier in storage table and catalog-aware search options
- Session start hook surfaces `chunk_text_hash` and catalog routing params

## [3.0.0] - 2026-04-05

### Added
- Catalog tools injected for RDR lifecycle, knowledge, research, debug, and analysis agent contexts (SubagentStart hook)
- 12 agents/skills wired with catalog link creation: debugger (`relates`), deep-analyst (`relates`), codebase-analyzer (`relates`, `supersedes`), architect-planner (`relates`, `cites`), developer (`implements`), knowledge-tidier (`supersedes`, `relates`), deep-research-synthesizer (`cites`), all 7 RDR skills (`supersedes`, `cites`, `relates`)
- Query planner updated for `catalog_links` `{nodes, edges}` return format

### Changed
- `SubagentStart` hook catalog injection regex broadened — triggers on references, follow-on, catalog, RDR lifecycle, knowledge, research, debug, architecture, and analysis keywords

## [2.12.0] - 2026-04-04

Plugin version aligned with Nexus CLI 2.12.0. No plugin-level functional changes.

## [2.11.2] - 2026-04-03

Plugin version aligned with Nexus CLI 2.11.2. No plugin-level functional changes.

## [2.11.1] - 2026-04-03

Plugin version aligned with Nexus CLI 2.11.1. No plugin-level functional changes.

## [2.11.0] - 2026-04-03

### Added
- `query` MCP tool — document-level semantic search with full metadata
- `store_delete`, `memory_delete` MCP tools
- `search`/`query` `where` filter for metadata filtering
- `store_list` `docs` mode for document-level view
- `collection_info` peek (sample titles)
- `scratch` delete action

### Fixed
- All agent/skill tool references use full `mcp__plugin_nx_nexus__` prefix
- Fixed `mcp__sequential-thinking__` prefix in 13 agent files
- Real offset pagination in T3 `list_store`
- `source_title` fallback in store_list and search
- FTS5 title search corrected in docs
- `search` param `n` → `limit`

### Changed
- MCP tool count: 12 → 17
- Session start hook references MCP tools (not CLI commands)
- Subagent start hook uses `Tool:` prefix with full names

## [2.10.8] - 2026-04-02

Plugin version aligned with Nexus CLI 2.10.8. No plugin-level functional changes.

## [2.10.7] - 2026-04-02

Plugin version aligned with Nexus CLI 2.10.7. No plugin-level functional changes.

## [2.10.6] - 2026-04-02

### Fixed
- **RDR close bead gate** — `rdr-close` skill and command now hard-gate
  on open beads instead of treating them as advisory. Agent must display
  open beads and get explicit user confirmation before closing.

## [2.10.5] - 2026-04-02

Plugin version aligned with Nexus CLI 2.10.5. No plugin-level functional changes.

## [2.10.4] - 2026-04-01

### Removed
- **PostToolUse prompt hook** — `type: "prompt"` is not valid for
  PostToolUse hooks (only `command` and `http` are supported), causing
  `PostToolUse:Bash hook error` on every Bash tool call. Removed
  entirely; `/nx:debug` remains available on demand.

## [2.10.3] - 2026-04-01

### Added
- **PostToolUse prompt hook** — enforces `/nx:debug` invocation on
  repeated test failures. Fires after Bash commands, has full
  conversation context, zero subprocess overhead. One manual retry is
  acceptable; two consecutive failures without `/nx:debug` triggers
  the enforcement prompt.

## [2.10.2] - 2026-04-01

### Fixed
- **Restore skill routing guardrails** — Skill Directory, Process Flow,
  Storage Tier Protocol, and Red Flags table restored to SessionStart
  injection. Agents were not invoking debugger, architect, and other
  specialized skills after these were trimmed for compactness.

## [2.10.1] - 2026-04-01

### Fixed
- **Both hooks advisory-only** — removed test suite execution from
  Stop and PreToolUse hooks. Stop warns about uncommitted changes and
  open beads. PreToolUse warns about missing review markers. Neither
  blocks.
- **PreToolUse output format** — uses `hookSpecificOutput` /
  `permissionDecision` (correct PreToolUse protocol).
- **Bead ID extraction** — BSD sed compatible on macOS.

## [2.10.0] - 2026-04-01

### Added
- **Post-implementation verification hooks** (RDR-045) — two opt-in
  mechanical enforcement hooks that catch premature session closure and
  premature bead closure.
  - **Stop hook** (`on_stop: true`) — blocks session end on uncommitted
    git changes, open beads, or test failures. On retry, test failures
    are let through with a warning; mechanical issues continue to block.
  - **PreToolUse close gate** (`on_close: true`) — intercepts `bd close`
    and `bd done`, blocks on test failure, emits advisory when no
    review marker found in T1 scratch.
  - Standalone `read_verification_config.py` config reader for hooks
    (no nexus package imports).
  - hooks.json: `Stop` (timeout 180s) and `PreToolUse` (matcher `Bash`,
    timeout 300s) entries registered.

### Changed
- **code-review skill** — writes T1 scratch marker
  (`review-completed bead=<id>`) on successful completion, enabling the
  PreToolUse hook to verify review happened before bead closure.

## [2.9.1] - 2026-03-31

### Added
- **receiving-review skill** — technical evaluation of code review feedback
  with 6-step pattern (READ→UNDERSTAND→VERIFY→EVALUATE→RESPOND→IMPLEMENT),
  YAGNI check via serena-code-nav, pushback correction guidance.
- **git-worktrees skill** — isolated workspace setup with directory selection
  priority, safety verification, auto-detect setup (uv/pip/npm/cargo/go),
  Agent tool `isolation: "worktree"` guidance.
- **finishing-branch skill** — branch completion workflow: verify tests,
  present merge/PR/keep/discard options, typed "discard" confirmation,
  worktree detection, beads close + dolt push integration.
- **using-nx-skills routing** — git workflow section and common mistakes
  table updated for new skills.

### Changed
- **All agents and skills** — migrated `bd` CLI references to `/beads:`
  skill invocations. Skills are native to the Skill tool — more reliable
  for subagents, no shell escaping needed. Only `bd dolt push` retained
  (no skill equivalent). 29 files, 117 replacements.
- **registry.yaml** — 3 new standalone skills registered; removed
  superpowers reference from nx-preflight description.

### Fixed
- **Auto-detect MinerU fallback** — reuses already-computed fast_result
  instead of re-converting PDF through Docling (review finding).
- **test_plugin_structure.py** — consolidated STANDALONE_SKILLS into
  single module-level constant; added 3 new skills to exclusion set.

## [2.8.0] - 2026-03-30

### Added
- **analytical-operator agent** — executes 5 analytical operations (extract,
  summarize, rank, compare, generate) over retrieved content. Dispatched by
  the `/nx:query` skill. (RDR-042)
- **query-planner agent** — decomposes complex analytical questions into
  step-by-step JSON execution plans with `$step_N` references. (RDR-042)
- **`/nx:query` skill** — multi-step analytical query execution driver.
  Orchestrates query-planner → analytical-operator dispatch loop with T1
  scratch for step persistence and T2 plan library for reuse. (RDR-042)
- **`plan_save` / `plan_search` MCP tools** — expose T2 plan library to
  agents. Project-scoped FTS5 search over saved query plans. (RDR-042)
- **Orchestrator failure relay protocol** — distinguishes ESCALATION
  sentinels (circuit breaker, route to debugger) from incomplete output
  (retry up to 2x). (RDR-042)
- **SubagentStart hook** — now injects plan library and analytical operator
  guidance so all subagents know about the query pipeline. (RDR-042)

### Fixed
- **Serena hook tool names** — sn plugin SubagentStart hook now uses full
  MCP-prefixed names (`mcp__plugin_sn_serena__*`). Short names were invisible
  to subagents.

## [2.7.1] - 2026-03-28

### Added
- **nx MCP tool guidance injection** — SubagentStart hook now injects three-tier
  storage tool signatures (T1 scratch, T2 memory, T3 search/store) into ALL
  subagents, not just nx agents. Any arbitrary agent can now participate in
  inter-agent communication via T1 scratch and access project knowledge.

## [2.7.0] - 2026-03-28

### Added
- **Sequential thinking MCP injection** — SubagentStart hook now injects usage
  guidance for `sequentialthinking` MCP tool (when to use, parameter patterns
  for `needsMoreThoughts`, `isRevision`, branching).

## [2.6.1] - 2026-03-27

### Fixed
- **rdr-accept planning detection** — broadened section and heading matching,
  flipped default from "no" to "yes". RDRs with `## Approach` / `### Step`
  headings now correctly trigger planning handoff.
- **rdr-gate** — accepts Approach/Steps as valid plan sections (not just
  Implementation Plan).

## [2.6.0] - 2026-03-26

### Added
- **T1 scratch inter-agent context sharing** — tag vocabulary in CONTEXT_PROTOCOL,
  sibling context for relay-reliant agents, developer writes failed approaches,
  reviewer and debugger search scratch for predecessor findings.
- **Escalation relay improvements** — debugger relay includes `nx scratch` field,
  re-dispatch developer relay template with structured artifacts.
- **Escalation guard** — prevents infinite developer→debugger loop.

## [2.5.0] - 2026-03-25

### Added
- **Developer agent circuit breaker** — hard stop after 2 consecutive test
  failures with structured ESCALATION report for debugger dispatch.
- **Debugger escalation section** in development skill with relay template.
- **Developer → debugger escalation edge** in orchestration routing.

## [2.4.2] - 2026-03-25

Plugin version aligned with Nexus CLI 2.4.2. No plugin-level functional changes.

## [2.4.1] - 2026-03-24

Plugin version aligned with Nexus CLI 2.4.1. No plugin-level functional changes.

## [2.4.0] - 2026-03-24

### Bug Fixes (Track C)
- **C1**: Single-chunk CCE documents now use `contextualized_embed()` instead of falling back to `voyage-4`, fixing a model mismatch for short documents
- **C2/C3**: Paginated all unbounded `col.get()` calls in `indexer.py` to handle >300 chunks (ChromaDB Cloud hard cap)
- **C4**: Partial CCE embedding failure now re-embeds entire document with voyage-4 for consistency, preventing mixed-model vectors
- **C5**: MCP server collection cache uses atomic tuple assignment to eliminate race condition

### Post-Mortem Gap Closure (Track A)
- **A1**: Added retrieval quality unit tests that assert semantic rank ordering, not just `len(results) > 0`
- **A2**: Enhanced `nx collection verify --deep` with known-document probe and distance reporting; shared `verify_collection_deep()` function in `db/t3.py`
- **A3**: Added cross-model invariant regression test — fails if CCE index/query models diverge
- **A4**: New `nx collection reindex <name>` command with pre-delete safety check, per-type dispatch, and post-reindex verification
- **A5**: Per-chunk progress callback for pdf/md indexing — `--monitor` now shows tqdm bar during embedding

### MCP Server Enhancement (Track B)
- **B1**: `search` tool default changed from `corpus="knowledge"` to `corpus="knowledge,code,docs"` with `"all"` alias
- **B2**: New `collection_list` tool — lists all collections with document counts and models
- **B3**: New `collection_info` tool — detailed collection metadata
- **B4**: New `collection_verify` tool — known-document retrieval health probe

### Documentation (Track D)
- Updated CLI reference, architecture docs, MCP tool reference, and CLAUDE.md for all changes above

### References
- RDR-040: CCE Post-Mortem Gap Closure & MCP Server Enhancement
- Post-mortem: cce-query-model-mismatch

## [2.3.6] - 2026-03-23

Plugin version aligned with Nexus CLI 2.3.6. No plugin-level functional changes.

## [2.3.5] - 2026-03-23

### Docs
- **Unprefixed skill references** — all `/rdr-create` → `/nx:rdr-create` etc.
  across documentation and RDR files.
- **Python version** — updated to 3.12–3.13 in plugin README prerequisites.

## [2.3.4] - 2026-03-23

### Fixed
- **Unprefixed skill references** — corrected `/rdr-create` → `/nx:rdr-create` etc.
  across 11 documentation and RDR files.

## [2.3.3] - 2026-03-23

Plugin version aligned with Nexus CLI 2.3.3. No plugin-level functional changes.

## [2.3.2] - 2026-03-22

### Fixed
- **rdr-accept**: PROHIBITION block prevents orchestrator from bypassing
  planning chain. Chain mandatory for multi-phase RDRs. Subagent failure
  clause blocks "let me finish this directly" compensation. Dead T2
  idempotency code removed; self-healing uses live memory_get results.
  Unbound placeholders fixed with `<ID>` notation.
- **plan-enricher**: `bd update --description` replaced with Write tool →
  `--body-file` pattern. Prevents silent content corruption from shell escaping.
- **enrich-plan skill**: Standalone invocation path updated to match agent fix.

### Added
- **writing-nx-skills**: Known Pitfalls section for `--description` corruption.

## [2.3.1] - 2026-03-22

### Fixed
- StopFailure hook guarded behind `CLAUDECODE` env var — no more junk beads from test runs.

## [2.3.0] - 2026-03-22

### Added
- PostCompact hook (`post_compact_hook.sh`) — re-injects active beads and T1 scratch
  after compaction. Buffers output and only emits header when content exists.
- StopFailure hook (`stop_failure_hook.py`) — logs API failures to beads memory,
  creates blocker bead on rate limits. Python 3.9+ compatible, null-safe.

### Fixed
- PostCompact scratch test adapted for empty-scratch environments (CI).

## [2.2.0] - 2026-03-21

### Added
- `effort` frontmatter on all 15 agents and 28 skills (RDR-039 Phase 1)
- `maxTurns` on 2 haiku agents (knowledge-tidier=20, pdf-chromadb-processor=30)
- `HARD-CONSTRAINT` on pdf-chromadb-processor — must use `nx index pdf`, never manual extraction
- `_rdr_dir()` in rdr_hook.py — reads `.nexus.yml` for RDR path instead of hardcoding `docs/rdr`
- `closed` status to rdr_hook.py `_STATUS_ORDER` (was missing, caused wrong reconciliation)
- Essential MCP Tools section in using-nx-skills (sequential thinking + storage tiers)

### Changed
- Orchestrator upgraded from haiku to sonnet (routing ambiguous requests needs reasoning)
- plan-enricher version 1.0 → 2.0
- plan-auditor routing: substantive-critic added as first successor option
- using-nx-skills rewritten: routing decision tree replaces flat tables, Common Mistakes table
- writing-nx-skills updated for effort field, Agent tool reference, using-nx-skills update reminder
- pdf-process command simplified to delegate to skill (respects quick path for single PDFs)
- rdr_hook.py: terminal conflicts warn instead of auto-reconciling, explicit log messages
- subagent-start.sh: `python3` instead of `uv run python`
- All hooks now have explicit timeouts

### Removed
- `/nx:orchestrate` command (routing tree in using-nx-skills replaces it)
- `mcp_health_hook.sh` (redundant with `nx hook session-start`)
- `setup.sh` (redundant, Setup event rarely fires)
- `bead_context_hook.py` (broken output format)
- `permission-request-stdin.sh` (dead code — wrong field names, settings bypass)
- Setup, PostToolUse, PermissionRequest hook events from hooks.json
- Duplicate T2 memory output from `session_start()` (session_start_hook.py is single source)

## [2.1.1] - 2026-03-15

### Fixed
- **Fully-qualify all skill slash command references** — all 19 files across agents,
  commands, hooks, skills, and README now use `/nx:skill-name` instead of `/skill-name`.
  Short-form references were not invocable by users because Claude Code requires the
  `/<plugin>:<skill>` format for plugin-namespaced skills.

## [2.1.0] - 2026-03-15

Plugin version aligned with Nexus CLI 2.1.0. Local T3 backend (RDR-038) enables zero-config semantic search — agents and MCP tools work with local embeddings when no cloud credentials are configured. No plugin-level API changes.

## [2.0.0] - 2026-03-14

Plugin version aligned with Nexus CLI 2.0.0. T3 backend consolidated from 4 databases to 1 (RDR-037). No plugin-level API changes — agents and skills work unchanged.

## [1.12.1] - 2026-03-14

Plugin version aligned with Nexus CLI 1.12.1. No plugin-level functional changes.

## [1.12.0] - 2026-03-13

Plugin version aligned with Nexus CLI 1.12.0. No plugin-level functional changes.

## [1.11.1] - 2026-03-13

### Fixed
- **rdr-accept chain orchestration** — skill now explicitly dispatches all three
  agents sequentially (strategic-planner → plan-auditor → plan-enricher) instead
  of relying on agent-to-agent relay, which was impossible (subagents cannot spawn
  subagents)
- **Agent handoff model rewrite** — all 15 agents: "Successor Enforcement" →
  "Recommended Next Step" output blocks. Shared templates (`RELAY_TEMPLATE.md`,
  `CONTEXT_PROTOCOL.md`, `MAINTENANCE.md`, `README.md`) and 2 skills updated
  to match
- **Template variable mismatches** — `{rdr_file_path}`/`{path}` → `{rdr_file}`
  in rdr-accept command and skill
- **Stale "spawn" imperatives** in architect-planner, developer, orchestrator
  rewritten to output-oriented language
- **enrich-plan skill** added to using-nx-skills directory table

## [1.11.0] - 2026-03-12

### Added
- **plan-enricher agent** — enriches beads with audit findings, execution context, and
  codebase alignment after plan-auditor validates (sonnet, emerald)
- **enrich-plan skill + `/nx:enrich-plan` command** — invoke plan-enricher standalone or
  via RDR planning chain
- **Planning handoff in `/nx:rdr-accept`** — Step 7 auto-detects multi-phase RDRs and
  offers to dispatch strategic-planner → plan-auditor → plan-enricher chain
- **Conditional successor routing in plan-auditor** — T1 `rdr-planning-context` tag
  with RDR ID correlation routes to plan-enricher only in RDR planning context

### Changed
- **`/nx:rdr-close` bead decomposition → bead status advisory** — close no longer creates
  beads; shows read-only status table, human decides which to close
- **strategic-planner Phase 3** renamed "Audit Handoff"; removed "iterate" instruction
- Registered plan-enricher in `registry.yaml` (agents, feature pipeline, model summary)
- Updated `rdr-accept` description in registry to mention planning dispatch
- Updated `rdr-close` description in registry, using-nx-skills, workflow docs
- Agent count: 14 → 15; Skill count: 27 → 28

## [1.10.3] - 2026-03-12

Plugin version aligned with Nexus CLI 1.10.3. No plugin-level functional changes.

## [1.10.2] - 2026-03-12

### Fixed
- **Remove `tools:` frontmatter from all 14 agents** (RDR-035) — Claude Code bug
  where explicit `tools:` in plugin agents filters out MCP tools. Agents now inherit
  all tools from the parent session. PermissionRequest hook remains as enforcement.

## [1.10.1] - 2026-03-11

### Fixed
- Removed `SessionEnd` hook — cancelled by Claude Code during process teardown,
  producing spurious error on every exit. T1 server stops with process tree; hook
  was a no-op.

## [1.10.0] - 2026-03-11

### Added
- **Nexus MCP server** (RDR-034) — bundled FastMCP server (`nx-mcp`) exposing 8
  structured tools for direct T1/T2/T3 storage access. Agents no longer depend on
  Bash for storage operations. Declared in `.mcp.json` alongside sequential-thinking.
- **Plugin-wide MCP migration** — all 14 agents, `_shared/` protocols
  (`CONTEXT_PROTOCOL.md`, `ERROR_HANDLING.md`), and 9 skills updated from CLI syntax
  to MCP tool syntax (`mcp__plugin_nx_nexus__*`). Human-facing docs retain CLI syntax.
- **Permission auto-approval** for all `mcp__plugin_nx_nexus__*` tools in the
  PermissionRequest hook.

### Changed
- `id` parameter renamed to `entry_id` in scratch tool calls across all agent and
  skill files (avoids Python builtin shadow).
- Plugin README rewritten: MCP Servers section expanded with full tool documentation,
  prerequisites table updated, permission section updated.

## [1.9.1] - 2026-03-10

### Changed
- Plugin version aligned with Nexus CLI 1.9.1. No plugin-level functional changes.

## [1.9.0] - 2026-03-10

### Changed
- **PDF agent rewrite** (RDR-033) — `pdf-chromadb-processor` agent v3.0 now delegates
  entirely to `nx index pdf` instead of reimplementing extraction in bash. Eliminates
  sandbox permission failures and context limit issues.

### Added
- **`nx store export`/`import` in pdf-processing skill** — agent can now suggest
  backup workflows using the new export/import commands.

## [1.8.0] - 2026-03-08

### Changed
- **Language-agnostic agents** (RDR-025) — renamed `java-developer` → `developer`,
  `java-debugger` → `debugger`, `java-architect-planner` → `architect-planner`.
  Agents use CLAUDE.md delegation for language/build/test detection at runtime.
- **Skill and command renames** — `java-development/` → `development/`,
  `java-debugging/` → `debugging/`, `java-architecture/` → `architecture/`.
  Commands: `/java-implement` → `/nx:implement`, `/java-debug` → `/nx:debug`,
  `/java-architecture` → `/nx:architecture`.
- **Registry updated** — all pipelines, predecessor/successor chains, naming aliases,
  and model summary reflect new agent names.
- **18 cross-reference files updated** — orchestrator, strategic-planner, test-validator,
  plan-auditor, deep-analyst, deep-research-synthesizer, codebase-deep-analyzer,
  shared protocols, 6 skill files, and orchestrate command.

### Added
- **CLAUDE.md preflight check** in `/nx:nx-preflight` — validates language, build system,
  and test command presence. Warnings only, not errors.

## [1.7.1] - 2026-03-07

### Added
- Project-local `/release` skill to enforce release checklist.

## [1.7.0] - 2026-03-07

### Added
- **Agent tool permissions** (RDR-023) — explicit `tools` frontmatter on all 14 agents
  with least-privilege assignments and sequential thinking MCP tool.
- **PermissionRequest hook expansion** (RDR-023) — auto-approve Read, Grep, Glob, Write,
  Edit, WebSearch, WebFetch, Agent, and sequential thinking for subagents. Expanded Bash
  allowlist with `uv run pytest`, additional `bd` subcommands, read-only `git branch`/`git tag`.
- **RDR process guardrails** (RDR-024) — soft-warning pre-checks in brainstorming-gate
  skill, strategic-planner relay validation, and bead context hook to catch implementation
  on ungated RDRs.

### Fixed
- **git branch/tag hook patterns** — restricted to read-only forms only.

## [1.6.1] - 2026-03-06

### Changed
- Plugin version aligned with Nexus CLI 1.6.1. PermissionRequest hook now auto-approves
  all nx subcommands with a deny guard on nx collection delete.

## [1.6.0] - 2026-03-06

### Changed
- Plugin version aligned with Nexus CLI 1.6.0. No plugin-level functional changes.

## [1.5.3] - 2026-03-05

### Changed
- Plugin version aligned with Nexus CLI 1.5.3. No plugin-level functional changes.

## [1.5.2] - 2026-03-05

### Changed
- Plugin version aligned with Nexus CLI 1.5.2. No plugin-level functional changes
  this release; all changes (retry helpers moved to `nexus.retry` leaf module) are
  in the CLI.

## [1.5.1] - 2026-03-04

### Changed
- Plugin version aligned with Nexus CLI 1.5.1. No plugin-level functional changes
  this release; all changes (ChromaDB transient retry, release process improvements)
  are in the CLI.

## [1.5.0] - 2026-03-04

### Changed
- Plugin version aligned with Nexus CLI 1.5.0. No plugin-level functional changes
  this release; all changes (auto-provision T3 databases, nx migrate removal, UX polish)
  are in the CLI.

## [1.4.0] - 2026-03-03

### Changed
- Plugin version aligned with Nexus CLI 1.4.0. No plugin-level functional changes
  this release; all changes (file lock, git hooks, `nx serve` removal) are in the CLI.

## [1.3.0] - 2026-03-03

### Changed
- Plugin version aligned with Nexus CLI 1.3.0. No plugin-level functional changes
  this release; all changes (`--force`, `--monitor`, auto-TTY, byte cap, AST line
  ranges) are in the CLI.

## [1.2.0] - 2026-03-03

### Changed
- Plugin version aligned with Nexus CLI 1.2.0. No plugin-level functional changes
  this release; all changes (SKIP class, context prefix, AST expansion) are in the CLI.

## [1.1.1] - 2026-03-02

### Fixed
- **`rdr-close` pre-check** — status check now correctly accepts `"accepted"` (or
  `"final"`) matching actual command behaviour; warning message shows `{current_status}`
  instead of the hardcoded `"Draft"`.

### Changed
- **Agent and skill counts** corrected throughout plugin docs after PM removal
  (14 agents, 27 skills).
- **`nexus` skill description** — "project management" replaced with "indexing".

## [1.1.0] - 2026-03-02

### Removed
- **`nx pm` command layer** — six slash commands (`/pm-archive`, `/pm-close`, `/pm-list`,
  `/pm-new`, `/pm-restore`, `/pm-status`), the `project-management-setup` agent, and
  the `project-setup` command and skill. T2 memory (`nx memory`) replaces all PM
  functionality directly; the layer added overhead without benefit.
- **`--mxbai` reference** removed from `nexus/reference.md` (Mixedbread integration
  removed from CLI).
- **Superpowers check** removed from `mcp_health_hook.sh` — superpowers is an optional
  plugin and should not produce session-start warnings.

### Changed
- `mcp_health_hook.sh`: `bd` not-found message now includes the install URL.
- `setup.sh`: prints a warning (rather than silently skipping) when `bd` is absent.
- `nx-preflight.md`: added `uv` prerequisite check as section 5.

## [1.0.0] - 2026-03-01

### Changed
- Plugin version aligned with Nexus CLI 1.0.0 release.
- Package name corrected in hook scripts.
- Skill count updated in README.
- Free-tier callout added to prerequisite table.

## [0.7.0] - 2026-03-01

### Added
- **Storage Tier Protocol** in `using-nx-skills` SKILL.md: T3→T2→T1 read-widest-first
  table and T1→persist→knowledge-tidy write path — gives every agent an explicit data
  discipline so they don't re-research what siblings already found.

## [0.6.0] - 2026-03-01

### Added
- **`serena-code-nav` skill**: navigate code by symbol — definitions, callers, type
  hierarchies, safe renames — without reading whole files.
- **SubagentStart T1 injection**: `subagent-start.sh` now injects live T1 scratch entries
  into every spawned agent's context; agents see session-wide discoveries immediately.
- **`using-nx-skills` polish**: 29-skill directory table with 5 categories, Announce step
  in process flow, 12 red flags (restored from 7), `brainstorming-gate` replaces
  `verification-before-completion` in Skill Priority.
- Registry trigger conditions sharpened: knowledge-tidier, orchestrator, substantive-critic.

### Fixed
- SessionStart hook matcher tightened to `startup|resume|clear|compact` (was match-all `""`).
- Wrong comment in `subagent-start.sh` claiming T1 is per-agent-scoped corrected; actual
  behavior (PPID-chain shared) documented inline.

## [0.5.0] - 2026-02-28

### Added (RDR-007: Claude Adoption — Session Context and Search Guidance)
- T2 multi-namespace prefix scan (`t2_prefix_scan.py`) — SubagentStart hook now surfaces all `{repo}*` namespaces, not just the bare project namespace
- `get_projects_with_prefix()` on T2Database with LIKE metacharacter escaping
- Cap algorithm: 5 entries with snippet + 3 with title-only + remainder as count per namespace; 15-entry cross-namespace hard cap
- `nx index repo --chunk-size N` flag — configurable lines-per-chunk for code files (default 150, min 1)
- `nx index repo --no-chunk-warning` flag — suppress large-file pre-scan warning
- Large-file pre-scan warning: detects code files exceeding 30× chunk size lines before indexing and suggests `--chunk-size 80`
- `chunk_lines` parameter threaded through `index_repository` → `_run_index` → `_index_code_file` → `chunk_file`
- Nexus skill `reference.md` updated: T2 namespace naming table, T2 Search Constraints section (FTS5 literal token rules, title-search caveat), Code Search guidance (nx vs Grep), RDR-006 precision note

### Changed
- `AST_EXTENSIONS` in `chunker.py` renamed from `_AST_EXTENSIONS` to public constant
- Warning suggestion is adaptive: recommends `--chunk-size 80` when no chunk size specified, or `max(10, current // 2)` when already set

## [0.4.0] - 2026-02-24

### Added
- brainstorming-gate skill: design gate before implementation (S1)
- verification-before-completion skill: evidence before claims (S2)
- receiving-code-review skill: technical rigor for review feedback (S3)
- using-nx-skills skill: skill invocation discipline (S4)
- dispatching-parallel-agents skill: parallel agent coordination (O3)
- writing-nx-skills meta-skill: plugin authorship guide (O5)
- Graphviz flowcharts in decision-heavy skills (O2)
- REQUIRED SUB-SKILL cross-reference markers (O4)
- Companion reference.md for nexus skill (O6)
- CHANGELOG.md
- SessionStart hook for using-nx-skills injection

### Changed
- All skill descriptions rewritten to CSO "Use when [condition]" pattern (C1, C2)
- Removed non-standard frontmatter fields from all skills (S6)
- Removed YAML comments from description block scalars (S5)
- Replaced inline relay templates with hybrid cross-reference to RELAY_TEMPLATE.md (O6)
- Simplified agent-delegating commands with pre-filled relay parts (C3)
- Added disable-model-invocation to pure-bash pm commands (O1)
- PostToolUse hook now has matcher for bd create commands only (S7)
- Nexus skill split into quick-ref SKILL.md + detailed reference.md

### Fixed
- PostToolUse hook performance (was firing Python on every tool use)

## [0.3.2] - 2026-02-23

### Added
- RDR workflow skills (rdr-create, rdr-list, rdr-show, rdr-research, rdr-gate, rdr-close)
- cli-controller skill with raw tmux commands
