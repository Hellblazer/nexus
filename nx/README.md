# Nexus Claude Code Plugin

16 agents, 32 skills, session hooks, slash commands, and two bundled MCP servers for software engineering workflows — backed by the [Nexus CLI](../README.md) for semantic search and knowledge management.

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

**Companion plugin:**
- **[sn](../sn/README.md)** — Serena (LSP code intelligence) + Context7 (library docs) with SubagentStart guidance injection. Install separately: `/plugin install sn@nexus-plugins`.

**Also required:**
- Python 3.12–3.13 (for hook scripts)

## What You Get

- **16 agents** matched to task complexity: opus for reasoning, sonnet for implementation, haiku for utility
- **32 skills** — 10 standalone + 15 agent-delegating + 7 RDR workflow
- **5 standard pipelines** — feature, bug, research, onboarding, architecture
- **Session hooks** — surface T2 memory context, prime beads, health-check dependencies
- **Permission auto-approval** — safe commands and all nexus MCP tools skip the confirmation prompt
- **Two bundled MCP servers** — `nexus` (15 core tools: storage, memory, scratch, plans, consolidation) and `nexus-catalog` (10 catalog tools: search, show, link, resolve, stats) — plus `sequential-thinking` fetched via npx

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
│   └── *.md                 # 14 specialized agent definitions (+ 2 internal)
├── commands/
│   └── *.md                 # Slash commands (/nx:research, /nx:create-plan, /nx:review-code, etc.)
├── hooks/
│   ├── hooks.json           # Hook event → script wiring
│   └── scripts/
│       ├── rdr_hook.py               # RDR file↔T2 status reconciliation
│       ├── session_start_hook.py     # Surface T2 memory, beads, scratch context
│       ├── subagent-start.sh         # Context prep for spawned subagents
│       └── t2_prefix_scan.py         # T2 multi-namespace prefix scan (shared)
├── .mcp.json                # Bundled MCP servers (nexus storage + sequential-thinking)
├── registry.yaml            # Single source of truth: agents, pipelines, aliases
├── CHANGELOG.md             # Version history (Keep a Changelog format)
└── skills/
    ├── brainstorming-gate/  # Standalone: design gate before implementation
    ├── cli-controller/      # Standalone: tmux-based interactive CLI control
    ├── nexus/               # Standalone: nx CLI reference (all tiers)
    ├── serena-code-nav/     # Standalone: navigate code by symbol (definitions, callers, renames)
    ├── using-nx-skills/     # Standalone: skill invocation discipline
    ├── writing-nx-skills/   # Standalone: plugin authorship guide
    ├── code-review/         # → code-review-expert agent
    ├── codebase-analysis/   # → codebase-deep-analyzer agent
    ├── deep-analysis/       # → deep-analyst agent
    ├── substantive-critique/# → substantive-critic agent
    ├── architecture/        # → architect-planner agent
    ├── debugging/           # → debugger agent
    ├── development/         # → developer agent
    ├── knowledge-tidying/   # → knowledge-tidier agent
    ├── orchestration/       # routing reference (no agent)
    ├── pdf-processing/      # → pdf-chromadb-processor agent
    ├── plan-validation/     # → plan-auditor agent
    ├── research-synthesis/  # → deep-research-synthesizer agent
    ├── strategic-planning/  # → strategic-planner agent
    ├── test-validation/     # → test-validator agent
    ├── rdr-accept/          # RDR workflow: accept a gated RDR
    ├── enrich-plan/         # → plan-enricher agent
    ├── rdr-close/           # RDR workflow: close RDR, bead advisory
    ├── rdr-create/          # RDR workflow: create new RDR from template
    ├── rdr-gate/            # RDR workflow: quality gate before finalizing
    ├── rdr-list/            # RDR workflow: list RDRs with status
    ├── rdr-research/        # RDR workflow: delegate research to agents
    └── rdr-show/            # RDR workflow: show RDR details
```

## Standalone Skills (10)

Skills that provide guidance directly without delegating to an agent.

| Skill | Purpose |
|-------|---------|
| brainstorming-gate | Design gate — requires exploration and user approval before implementation |
| cli-controller | Expert guidance for controlling interactive CLI applications via tmux |
| finishing-branch | Guide branch completion — verify tests, present merge/PR/keep/discard options |
| git-worktrees | Isolated workspace setup via git worktrees with safety verification |
| nexus | Nexus CLI reference for all tiers (T1/T2/T3) |
| orchestration | Agent routing reference — routing tables, pipeline templates, decision framework |
| receiving-review | Technical evaluation of code review feedback — verify before implementing |
| serena-code-nav | Navigate code by symbol — definitions, callers, type hierarchies, safe renames |
| using-nx-skills | Skill invocation discipline — check skills before every response |
| writing-nx-skills | Guide for authoring nx plugin skills |

## Agents (16)

See [`registry.yaml`](./registry.yaml) for full metadata (model, triggers, predecessors/successors).

| Agent | Skill | Command | Model | Purpose |
|-------|-------|---------|-------|---------|
| analytical-operator | query | *(dispatched)* | sonnet | Structured extraction, summarization, ranking, comparison |
| architect-planner | architecture | `/nx:architecture` | opus | Software architecture design, execution plans |
| code-review-expert | code-review | `/nx:review-code` | sonnet | Code quality, security, best practices |
| codebase-deep-analyzer | codebase-analysis | `/nx:analyze-code` | sonnet | Architecture, patterns, dependency mapping |
| debugger | debugging | `/nx:debug` | opus | Hypothesis-driven debugging |
| deep-analyst | deep-analysis | `/nx:deep-analysis` | opus | Complex problem investigation, root cause |
| deep-research-synthesizer | research-synthesis | `/nx:research` | sonnet | Multi-source research with synthesis |
| developer | development | `/nx:implement` | sonnet | TDD implementation, test-first methodology |
| knowledge-tidier | knowledge-tidying | `/nx:knowledge-tidy` | haiku | Persist and organize knowledge in nx store |
| pdf-chromadb-processor | pdf-processing | `/nx:pdf-process` | haiku | Index PDFs into nx store for semantic search |
| plan-auditor | plan-validation | `/nx:plan-audit` | sonnet | Validate plans before execution |
| plan-enricher | enrich-plan | `/nx:enrich-plan` | sonnet | Enrich beads with execution context |
| query-planner | query | *(dispatched)* | sonnet | Decompose analytical questions into execution plans |
| strategic-planner | strategic-planning | `/nx:create-plan` | opus | Implementation planning, task decomposition |
| substantive-critic | substantive-critique | `/nx:substantive-critique` | sonnet | Constructive critique of plans/designs/code |
| test-validator | test-validation | `/nx:test-validate` | sonnet | Test coverage and quality validation |

## Standard Pipelines

Defined in `registry.yaml`:

- **feature**: strategic-planner → plan-auditor → plan-enricher *(conditional)* → architect-planner → developer → code-review-expert → test-validator
- **bug**: debugger → developer → code-review-expert → test-validator
- **research**: deep-research-synthesizer → knowledge-tidier
- **onboarding**: codebase-deep-analyzer → strategic-planner
- **architecture**: codebase-deep-analyzer → deep-analyst → strategic-planner → plan-auditor → architect-planner

## Hooks

| Event | Script | Purpose |
|-------|--------|---------|
| `SessionStart` | `nx hook session-start` | Initialize T1 server, sweep stale sessions |
| `SessionStart` | `session_start_hook.py` | Surface T2 memory, beads, scratch context |
| `SessionStart` | `rdr_hook.py` | RDR file↔T2 status reconciliation |
| `SessionStart` | `bd prime` | Load beads context into session |
| `SessionStart` | `using-nx-skills/SKILL.md` | Inject skill invocation discipline |
| `PreCompact` | `bd prime` | Re-prime bead context before compact |
| `SubagentStart` | `subagent-start.sh` | Inject context for spawned subagents |

## Slash Commands

**Agent commands** (`/command → agent`):
- `/nx:research` → deep-research-synthesizer
- `/nx:create-plan` → strategic-planner
- `/nx:plan-audit` → plan-auditor
- `/nx:analyze-code` → codebase-deep-analyzer
- `/nx:review-code` → code-review-expert
- `/nx:test-validate` → test-validator
- `/nx:implement` → developer
- `/nx:debug` → debugger
- `/nx:architecture` → architect-planner
- `/nx:knowledge-tidy` → knowledge-tidier
- `/nx:pdf-process` → pdf-chromadb-processor
- `/nx:deep-analysis` → deep-analyst
- `/nx:substantive-critique` → substantive-critic
- `/nx:enrich-plan` → plan-enricher

**RDR commands**: `/nx:rdr-create`, `/nx:rdr-list`, `/nx:rdr-show`, `/nx:rdr-research`, `/nx:rdr-gate`, `/nx:rdr-accept`, `/nx:rdr-close`


## MCP Servers

The plugin ships `.mcp.json` which Claude Code picks up automatically on install:

| Server | Purpose | Tools |
|--------|---------|-------|
| `nexus` | T1/T2/T3 storage (core) | `search`, `query`, `store_put`, `store_get`, `store_list`, `memory_put`, `memory_get`, `memory_search`, `memory_delete`, `scratch`, `scratch_manage`, `collection_list`, `plan_save`, `plan_search` |
| `nexus-catalog` | Catalog access (RDR-062) | `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats` |
| `sequential-thinking` | Compaction-resilient reasoning chains | `sequentialthinking` |

### Nexus MCP Servers (`nx-mcp`, `nx-mcp-catalog`)

The nexus core server exposes 14 MCP tools and the nexus-catalog server exposes 10 catalog tools, for 24 registered tools total (6 tools demoted to Python-only). These give agents direct access to all three storage tiers and the catalog without requiring Bash. This eliminates failures in background agents and restricted permission contexts where Bash is unavailable.

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
