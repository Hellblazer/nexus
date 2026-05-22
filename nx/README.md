# Nexus Claude Code Plugin

13 agents (10 active + 3 stubs pointing at MCP tools), 43 skills, session hooks, slash commands, and two bundled MCP servers for software engineering workflows — backed by the [Nexus CLI](../README.md) for semantic search, plan-centric retrieval via `nx_answer`, and knowledge management.

## Installation

**Marketplace** (recommended):

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install nx@nexus-plugins
```

**Local development** (from the nexus repo checkout):

```bash
claude --plugin-dir ./nx
```

## Prerequisites

The `nx` CLI and plugin work independently, but the plugin's full agent and skill suite requires:

| Dependency | Required for | Install |
|-----------|-------------|---------|
| **`nx` CLI** | Hook scripts, indexing, and CLI-only operations (agents use MCP tools) | See [Getting Started](../docs/getting-started.md) |
| **`bd` (Beads)** | Task tracking in all agents | [github.com/BeadsProject/beads](https://github.com/BeadsProject/beads) |

Run `/nx:nx-preflight` after installing to verify all dependencies are present.

The plugin's SessionStart hook auto-spawns the T2 daemon (`nx daemon
t2 ensure-running --quiet`) on every Claude Code session start, so a
fresh `pip install conexus` + `/plugin install nx@nexus-plugins`
yields a working substrate on first session without any manual
`nx daemon t2 start` incantation. For a daemon that survives
across reboots independent of Claude Code (recommended for any host
with regular `nx` CLI use), run `nx daemon t2 install --autostart`
once after install. See [Container Integration](../docs/container-integration.md)
for the full story including dev-container TCP and Claude Cowork
SDK-bridge transport.

**Companion plugin:**
- **[sn](../sn/README.md)** — Serena (LSP code intelligence) + Context7 (library docs) with SubagentStart guidance injection. Install separately: `/plugin install sn@nexus-plugins`.

**Also required:**
- Python 3.12–3.13 (for hook scripts)

## What You Get

- **13 agents** (10 active + 3 MCP-tool redirect stubs) matched to task complexity: opus for reasoning, sonnet for implementation, haiku for utility
- **43 skills** — 12 infrastructure standalone + 9 RDR-078 verb skills + 4 MCP-tool pointer skills (RDR-080) + 10 agent-dispatcher skills + 8 RDR workflow skills
- **5 standard pipelines** — feature, bug, research, onboarding, architecture (`plan-auditor` / `plan-enricher` / `knowledge-tidier` steps now direct MCP tool invocations per RDR-080)
- **Session hooks** — surface T2 memory context, prime beads, health-check dependencies
- **Permission auto-approval** — safe commands and all nexus MCP tools skip the confirmation prompt
- **Two bundled MCP servers** — `nexus` (26 tools: search, query, store, memory, scratch, plans, traverse, 5 LLM-backed operators, and 4 orchestration tools including `nx_answer` for plan-centric retrieval) and `nexus-catalog` (10 catalog tools) — plus `sequential-thinking` fetched via npx

### Pick your entry point

| Goal | Start here |
|------|-----------|
| Explore an unfamiliar codebase | `/nx:analyze-code` |
| Plan a feature or component | `/nx:brainstorming-gate` → `/nx:create-plan` |
| Debug a failure | `/nx:debug` (after 2–3 failed attempts) |
| Review code before committing | `/nx:review-code` |
| Research an unfamiliar topic | `/nx:research` |
| Document a technical decision | `/nx:rdr-create` → `/nx:rdr-research` → `/nx:rdr-accept` |
| Index PDFs into semantic search | `/nx:pdf-process` |
| Not sure which agent to use | Check the skill directory in `using-nx-skills` |

## Directory Structure

```
nx/
├── agents/
│   ├── _shared/             # Shared resources referenced by all agents
│   │   ├── CONTEXT_PROTOCOL.md  # Standard relay/context exchange protocol
│   │   ├── ERROR_HANDLING.md    # Common error patterns and recovery
│   │   ├── MAINTENANCE.md       # How to maintain/update agents
│   │   ├── README.md            # _shared directory guide (this section)
│   │   └── RELAY_TEMPLATE.md    # Canonical relay message format
│   └── *.md                 # 14 command-invoked + 2 query-dispatched = 16 agent definitions
├── commands/
│   └── *.md                 # Slash commands (/nx:research, /nx:create-plan, /nx:review-code, etc.)
├── hooks/
│   ├── hooks.json                     # Hook event → script wiring (source of truth)
│   └── scripts/
│       ├── session_start_hook.py      # SessionStart: surface T2 memory, beads, scratch context
│       ├── rdr_hook.py                # SessionStart: RDR file↔T2 status reconciliation
│       ├── post_compact_hook.sh       # PostCompact: re-prime context after /compact
│       ├── stop_failure_hook.py       # StopFailure: advisory on session-end failures
│       ├── stop_verification_hook.sh  # Stop: opt-in session-end verification (tests, git)
│       ├── pre_close_verification_hook.sh  # PreToolUse (Bash): bd-close gate
│       ├── subagent-start.sh          # SubagentStart: inject context for spawned subagents
│       ├── auto-approve-nx-mcp.sh     # PermissionRequest: auto-approve nx MCP tools
│       ├── t2_prefix_scan.py          # Shared helper: T2 multi-namespace prefix scan
│       └── read_verification_config.py # Shared helper: read .nexus.yml verification block
├── .mcp.json                # Bundled MCP servers (nexus storage + sequential-thinking)
├── registry.yaml            # Single source of truth: agents, pipelines, aliases
├── CHANGELOG.md             # Version history (Keep a Changelog format)
└── skills/
    ├── brainstorming-gate/  # Standalone: design gate before implementation
    ├── catalog/             # Standalone: catalog operations + link graph patterns
    ├── cli-controller/      # Standalone: tmux-based interactive CLI control
    ├── nexus/               # Standalone: nx CLI reference (all tiers)
    ├── serena-code-nav/     # Standalone: navigate code by symbol (definitions, callers, renames)
    ├── using-nx-skills/     # Standalone: skill invocation discipline
    ├── writing-nx-skills/   # Standalone: plugin authorship guide
    ├── brainstorming-gate/  # Standalone: design gate before implementation
    ├── orchestration/       # Standalone: routing reference
    │
    │   # RDR-078 verb skills (dispatch plan_match + plan_run)
    ├── research/            # verb: research / design / architecture
    ├── review/              # verb: review / critique / audit change-set
    ├── analyze/             # verb: analyze / compare across corpora
    ├── debug/               # verb: debug / failing-path investigation
    ├── document/            # verb: document coverage / authoring
    ├── plan-first/          # gate: plan_match before any retrieval
    ├── plan-author/         # meta-seed: author new plan templates
    ├── plan-inspect/        # meta-seed: inspect plan metrics
    ├── plan-promote/        # meta-seed: rank promotion candidates
    │
    │   # RDR-080 pointer skills (dispatch a specific MCP tool — no agent spawn)
    ├── query/               # → mcp__plugin_nx_nexus__nx_answer
    ├── knowledge-tidying/   # → mcp__plugin_nx_nexus__nx_tidy
    ├── enrich-plan/         # → mcp__plugin_nx_nexus__nx_enrich_beads
    ├── plan-validation/     # → mcp__plugin_nx_nexus__nx_plan_audit
    │
    │   # Agent-dispatcher skills
    ├── code-review/         # → code-review-expert agent
    ├── codebase-analysis/   # → codebase-deep-analyzer agent
    ├── deep-analysis/       # → deep-analyst agent
    ├── substantive-critique/# → substantive-critic agent
    ├── architecture/        # → architect-planner agent
    ├── debugging/           # → debugger agent
    ├── development/         # → developer agent
    ├── research-synthesis/  # → deep-research-synthesizer agent
    ├── strategic-planning/  # → strategic-planner agent
    ├── test-validation/     # → test-validator agent
    │
    │   # RDR workflow skills
    ├── rdr-create/          # RDR: create new RDR from template
    ├── rdr-gate/            # RDR: quality gate before finalizing
    ├── rdr-accept/          # RDR: accept a gated RDR
    ├── rdr-close/           # RDR: close RDR, bead advisory
    ├── rdr-list/            # RDR: list RDRs with status
    ├── rdr-show/            # RDR: show RDR details
    ├── rdr-research/        # RDR: delegate research to agents
    └── rdr-audit/           # RDR: audit project RDR lifecycle
```

## Standalone Skills (24)

Skills that dispatch a tool or agent directly — no relay to a full sub-agent.
This includes RDR-078 verb skills, RDR-080 MCP-tool pointers, and infrastructure skills.

### Verb skills (RDR-078) — `plan_match` + `plan_run`

| Skill | Purpose |
|-------|---------|
| research | Design / architecture / planning — walks RDR/prose into code |
| review | Critique / audit / code-review against a change set |
| analyze | Cross-corpus analysis and synthesis |
| debug | Dev / debug from a failing code path |
| document | Documentation authoring or coverage audit |
| plan-first | Retrieval gate — try `plan_match` before falling through to `/nx:query` |
| plan-author | Author a new plan template |
| plan-inspect | Inspect plan metrics or the dimension registry |
| plan-promote | Rank promotion candidates by library metrics |

### MCP-tool pointer skills (RDR-080)

| Skill | Delegates to |
|-------|--------------|
| query | `mcp__plugin_nx_nexus__nx_answer` — multi-step retrieval |
| knowledge-tidying | `mcp__plugin_nx_nexus__nx_tidy` — knowledge consolidation |
| enrich-plan | `mcp__plugin_nx_nexus__nx_enrich_beads` — bead context enrichment |
| plan-validation | `mcp__plugin_nx_nexus__nx_plan_audit` — plan audit |

### Infrastructure skills

| Skill | Purpose |
|-------|---------|
| brainstorming-gate | Design gate — requires exploration and user approval before implementation |
| catalog | Catalog operations + link graph patterns — resolve, link, context, seed |
| cli-controller | Expert guidance for controlling interactive CLI applications via tmux |
| composition-probe | Runtime composition smoke test for coordinator beads |
| finishing-branch | Guide branch completion — verify tests, present merge/PR/keep/discard |
| git-worktrees | Isolated workspace setup via git worktrees with safety verification |
| nexus | Nexus CLI reference for all tiers (T1/T2/T3) |
| orchestration | Agent routing reference — routing tables, pipeline templates |
| receiving-review | Technical evaluation of code review feedback |
| serena-code-nav | Navigate code by symbol — definitions, callers, type hierarchies |
| using-nx-skills | Skill invocation discipline — check skills before every response |
| writing-nx-skills | Guide for authoring nx plugin skills |

## Agents (13)

See [`registry.yaml`](./registry.yaml) for full metadata (model, triggers, predecessors/successors).

### Active agents (10)

| Agent | Skill | Command | Model | Purpose |
|-------|-------|---------|-------|---------|
| architect-planner | architecture | `/nx:architecture` | opus | Software architecture design, execution plans |
| code-review-expert | code-review | `/nx:review-code` | sonnet | Code quality, security, best practices |
| codebase-deep-analyzer | codebase-analysis | `/nx:analyze-code` | sonnet | Architecture, patterns, dependency mapping |
| debugger | debugging | `/nx:debug` | opus | Hypothesis-driven debugging |
| deep-analyst | deep-analysis | `/nx:deep-analysis` | opus | Complex problem investigation, root cause |
| deep-research-synthesizer | research-synthesis | `/nx:research` | sonnet | Multi-source research with synthesis |
| developer | development | `/nx:implement` | sonnet | TDD implementation, test-first methodology |
| strategic-planner | strategic-planning | `/nx:create-plan` | opus | Implementation planning, task decomposition |
| substantive-critic | substantive-critique | `/nx:substantive-critique` | sonnet | Constructive critique of plans/designs/code |
| test-validator | test-validation | `/nx:test-validate` | sonnet | Test coverage and quality validation |

### Stub agents — redirect to MCP tools (RDR-080)

These 40-line stubs remain in the registry so legacy workflows and references
don't break.  They direct callers to the named MCP tool — you can invoke the
MCP tool directly and skip the agent spawn entirely.

| Stub agent | Replacement | Call shape |
|------------|-------------|------------|
| knowledge-tidier | nx_tidy | `mcp__plugin_nx_nexus__nx_tidy(topic=..., collection="knowledge")` |
| plan-auditor | nx_plan_audit | `mcp__plugin_nx_nexus__nx_plan_audit(plan_json=..., context="")` |
| plan-enricher | nx_enrich_beads | `mcp__plugin_nx_nexus__nx_enrich_beads(bead_description=..., context="")` |

### Removed in RDR-080

`query-planner` + `analytical-operator` were consolidated into the single
`nx_answer` MCP tool (plan-match → plan-run → record).  `pdf-chromadb-processor`
was removed — use `nx index pdf <file>` or the `/pdf-process` slash command.

## Standard Pipelines

Defined in `registry.yaml`:

- **feature**: strategic-planner → `nx_plan_audit` *(MCP)* → `nx_enrich_beads` *(MCP, conditional)* → architect-planner → developer → code-review-expert → test-validator
- **bug**: debugger → developer → code-review-expert → test-validator
- **research**: deep-research-synthesizer → `nx_tidy` *(MCP)*
- **onboarding**: codebase-deep-analyzer → strategic-planner
- **architecture**: codebase-deep-analyzer → deep-analyst → strategic-planner → architect-planner

MCP-tool steps replaced the `plan-auditor`, `plan-enricher`, and
`knowledge-tidier` agents per RDR-080.  Callers invoke the tool directly
instead of dispatching a sub-agent.
- **architecture**: codebase-deep-analyzer → deep-analyst → strategic-planner → plan-auditor → architect-planner

## Hooks

See `hooks/hooks.json` for exact wiring. Paths below use `$CLAUDE_PLUGIN_ROOT` as the plugin root.

| Event | Script | Purpose |
|-------|--------|---------|
| `SessionStart` | `nx hook session-start` | Initialize per-session T1 ChromaDB server, sweep stale sessions |
| `SessionStart` | `hooks/scripts/session_start_hook.py` | Surface T2 memory, ready beads, and scratch context at session start |
| `SessionStart` | `hooks/scripts/rdr_hook.py` | Reconcile RDR file frontmatter ↔ T2 metadata (self-healing on divergence) |
| `SessionStart` | `skills/using-nx-skills/SKILL.md` | Inject skill invocation discipline reminder |
| `PostCompact` | `hooks/scripts/post_compact_hook.sh` | Re-prime context (memory, beads, scratch) after `/compact` |
| `Stop` | `hooks/scripts/stop_verification_hook.sh` | Opt-in session-end verification: tests + git state (see [Configuration § Verification](../docs/configuration.md#verification)) |
| `StopFailure` | `hooks/scripts/stop_failure_hook.py` | Advisory on abnormal session termination |
| `PreToolUse` (`Bash`) | `hooks/scripts/pre_close_verification_hook.sh` | Opt-in bd-close gate: verifies before `bd close` / `bd done` |
| `SubagentStart` | `hooks/scripts/subagent-start.sh` | Inject inherited context (active bead, session, MCP priority) into spawned subagents |
| `PermissionRequest` (`mcp__plugin_nx_.*`) | `hooks/scripts/auto-approve-nx-mcp.sh` | Auto-approve nexus and nexus-catalog MCP tool calls |

## Slash Commands

**Agent commands** (`/command → agent`):
- `/nx:research` → deep-research-synthesizer
- `/nx:create-plan` → strategic-planner
- `/nx:analyze-code` → codebase-deep-analyzer
- `/nx:review-code` → code-review-expert
- `/nx:test-validate` → test-validator
- `/nx:implement` → developer
- `/nx:debug` → debugger
- `/nx:architecture` → architect-planner
- `/nx:deep-analysis` → deep-analyst
- `/nx:substantive-critique` → substantive-critic

**MCP-tool pointer commands** (RDR-080 — dispatch the named MCP tool directly):
- `/nx:query` → `nx_answer` (multi-step retrieval)
- `/nx:knowledge-tidy` → `nx_tidy` *(was → knowledge-tidier agent)*
- `/nx:plan-audit` → `nx_plan_audit` *(was → plan-auditor agent)*
- `/nx:enrich-plan` → `nx_enrich_beads` *(was → plan-enricher agent)*
- `/nx:pdf-process` → `nx index pdf` CLI *(was → pdf-chromadb-processor agent)*

**RDR commands**: `/nx:rdr-create`, `/nx:rdr-list`, `/nx:rdr-show`, `/nx:rdr-research`, `/nx:rdr-gate`, `/nx:rdr-accept`, `/nx:rdr-close`, `/nx:rdr-audit`


## MCP Servers

The plugin ships `.mcp.json` which Claude Code picks up automatically on install:

| Server | Purpose | Tools |
|--------|---------|-------|
| `nexus` | Retrieval + storage (core) | 26 tools — see below |
| `nexus-catalog` | Catalog access (RDR-062) | `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats` |
| `sequential-thinking` | Compaction-resilient reasoning chains | `sequentialthinking` |

### `nexus` MCP tool catalog (26 tools)

| Category | Tools |
|----------|-------|
| Retrieval (T3) | `search`, `query`, `store_put`, `store_get`, `store_get_many`, `store_list` |
| Memory (T2) | `memory_put`, `memory_get`, `memory_search`, `memory_delete`, `memory_consolidate` |
| Scratch (T1) | `scratch`, `scratch_manage` |
| Collections | `collection_list` |
| Plans (RDR-078) | `plan_save`, `plan_search`, `traverse` |
| Operators (RDR-079) | `operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate` |
| Orchestration (RDR-080) | `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit` |

**`nx_answer`** is the retrieval entry point for multi-step questions.
It runs `plan_match` against the library, executes the best-matching plan
via `plan_run`, and falls through to an inline planner on miss.  See
[`docs/querying-guide.md`](../docs/querying-guide.md) for the pattern.

### Nexus MCP Servers (`nx-mcp`, `nx-mcp-catalog`)

The nexus core server exposes 15 MCP tools and the nexus-catalog server exposes 10 catalog tools, for 25 registered tools total (6 tools demoted to Python-only). These give agents direct access to all three storage tiers and the catalog without requiring Bash. This eliminates failures in background agents and restricted permission contexts where Bash is unavailable.

**Pagination**: `search`, `store_list`, and `memory_search` return paged results. Pass `offset=N` for subsequent pages. Response footer: `--- showing X-Y of Z. next: offset=N` or `(end)`.

**Tool names** follow Claude Code's naming convention: `mcp__plugin_nx_nexus__<tool_name>` for core tools, `mcp__plugin_nx_nexus-catalog__<tool_name>` for catalog tools.

**Resource management**:
- T1 and T3 use thread-safe lazy singletons (expensive to initialize, reused across the session)
- T2 uses per-call context managers (SQLite WAL, microsecond open)
- All errors return `"Error: {message}"` strings — no exceptions surface as framework errors

**Agent frontmatter**: Agents do NOT declare a `tools:` field — Claude Code has a confirmed bug (GitHub #13605, #21560, #25200) where explicit `tools:` in plugin-defined agents filters out MCP tools. Agents inherit all tools from the parent session. The PermissionRequest hook provides runtime enforcement. Agent body text references MCP tool syntax (not CLI commands). See RDR-035.

**Human CLI**: The `nx` CLI remains the primary interface for human users. All `docs/` documentation uses CLI syntax. The MCP server is transparent to human workflows.

### Sequential Thinking

No separate install required — `npx` fetches `@modelcontextprotocol/server-sequential-thinking` on first use.

## Key Concepts

### Agent Relay Format

When skills delegate to agents, they use a standardized relay format defined in `agents/_shared/RELAY_TEMPLATE.md`:

```markdown
## Relay: {agent-name}

**Task**: [1-2 sentence summary]
**Bead**: [bead-id] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- Files: [key files or "none"]

### Deliverable
[What the agent should produce]

### Quality Criteria
- [ ] Criterion 1
- [ ] Criterion 2
```

### Storage Naming Conventions

- **nx store titles**: hyphens — `decision-cache-strategy`, `research-auth-patterns`
- **nx memory projects**: `{repo}` (general notes), `{repo}_rdr` (RDR metadata), `{repo}_knowledge` (findings)
- **Bead IDs**: managed by `bd` CLI

### Permission Auto-Approval

The permission hook auto-approves safe operations:

- **nexus MCP tools**: all `mcp__plugin_nx_nexus__*` core tools and `mcp__plugin_nx_nexus-catalog__*` catalog tools
- **sequential thinking**: `mcp__plugin_nx_sequential-thinking__sequentialthinking`
- **beads**: `bd list`, `bd show`, `bd search`, `bd prime`, `bd ready`, `bd status`
- **git**: `git log`, `git diff`, `git status`, `git show`, `git branch -a`
- **nexus CLI**: `nx search`, `nx store list/get`, `nx memory list/get/search`, `nx scratch list`, `nx doctor`
- **maven**: `mvn help:*`, `mvn dependency:tree`, `mvn dependency:analyze`

Dangerous commands (force-push, `bd delete`, deploys) are always denied.
