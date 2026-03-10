# Changelog

All notable changes to the nx plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  Commands: `/java-implement` → `/implement`, `/java-debug` → `/debug`,
  `/java-architecture` → `/architecture`.
- **Registry updated** — all pipelines, predecessor/successor chains, naming aliases,
  and model summary reflect new agent names.
- **18 cross-reference files updated** — orchestrator, strategic-planner, test-validator,
  plan-auditor, deep-analyst, deep-research-synthesizer, codebase-deep-analyzer,
  shared protocols, 6 skill files, and orchestrate command.

### Added
- **CLAUDE.md preflight check** in `/nx-preflight` — validates language, build system,
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
