# Nexus Claude Code Plugin

15 agents, 28 skills, session hooks, slash commands, and two bundled MCP servers for software engineering workflows — backed by the [Nexus CLI](../README.md) for semantic search and knowledge management.

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
| **superpowers plugin** | Cross-referenced skills (brainstorming, TDD, verification, writing-plans) | `/plugin marketplace add anthropics/claude-plugins-official` |

Run `/nx-preflight` after installing to verify all dependencies are present.

**Also required:**
- Python 3.12+ (for hook scripts)

## What You Get

- **15 agents** matched to task complexity: opus for reasoning, sonnet for implementation, haiku for utility
- **28 skills** — 6 standalone + 15 agent-delegating + 7 RDR workflow
- **5 standard pipelines** — feature, bug, research, onboarding, architecture
- **Session hooks** — surface T2 memory context, prime beads, health-check dependencies
- **Permission auto-approval** — safe commands and all nexus MCP tools skip the confirmation prompt
- **Two bundled MCP servers** — nexus (T1/T2/T3 storage tools) and sequential-thinking via `.mcp.json`

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
| Not sure which agent to use | `/nx:orchestrate` |

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
│   └── *.md                 # 15 specialized agent definitions
├── commands/
│   └── *.md                 # Slash commands (/nx:research, /nx:create-plan, /nx:review-code, etc.)
├── hooks/
│   ├── hooks.json           # Hook event → script wiring
│   └── scripts/
│       ├── bead_context_hook.py      # Bead context injection
│       ├── mcp_health_hook.sh        # MCP/nx health checks at session start
│       ├── permission-request-stdin.sh # Auto-approve safe read-only commands
│       ├── rdr_hook.py               # Report RDR document count and status
│       ├── session_start_hook.py     # Surface T2 memory, prime beads
│       ├── setup.sh                  # One-time setup checks
│       ├── subagent-start.sh         # Context prep for spawned subagents
│       └── t2_prefix_scan.py         # T2 multi-namespace prefix scan for session context
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
    ├── orchestration/       # → orchestrator agent
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

## Superpowers Delegation

The nx plugin delegates workflow discipline to the [superpowers](https://github.com/anthropics/claude-plugins-official/tree/main/superpowers) plugin rather than reimplementing it:

| Capability | Provided by |
|-----------|-------------|
| Verification before completion | `superpowers:verification-before-completion` |
| Receiving code review feedback | `superpowers:receiving-code-review` |
| Parallel agent dispatch | `superpowers:dispatching-parallel-agents` |
| TDD methodology | `superpowers:test-driven-development` |
| Git worktrees | `superpowers:using-git-worktrees` |
| Writing plans | `superpowers:writing-plans` |

## Standalone Skills (6)

Skills that provide guidance directly without delegating to an agent.

| Skill | Purpose |
|-------|---------|
| brainstorming-gate | Design gate — requires exploration and user approval before implementation |
| cli-controller | Expert guidance for controlling interactive CLI applications via tmux |
| nexus | Nexus CLI reference for all tiers (T1/T2/T3) |
| serena-code-nav | Navigate code by symbol — definitions, callers, type hierarchies, safe renames |
| using-nx-skills | Skill invocation discipline — check skills before every response |
| writing-nx-skills | Guide for authoring nx plugin skills |

## Agents (15)

See [`registry.yaml`](./registry.yaml) for full metadata (model, triggers, predecessors/successors).

| Agent | Skill | Command | Model | Purpose |
|-------|-------|---------|-------|---------|
| code-review-expert | code-review | `/nx:review-code` | sonnet | Code quality, security, best practices |
| codebase-deep-analyzer | codebase-analysis | `/nx:analyze-code` | sonnet | Architecture, patterns, dependency mapping |
| deep-analyst | deep-analysis | `/nx:deep-analysis` | opus | Complex problem investigation, root cause |
| substantive-critic | substantive-critique | `/nx:substantive-critique` | sonnet | Constructive critique of plans/designs/code |
| deep-research-synthesizer | research-synthesis | `/nx:research` | sonnet | Multi-source research with synthesis |
| architect-planner | architecture | `/nx:architecture` | opus | Software architecture design, execution plans |
| debugger | debugging | `/nx:debug` | opus | Hypothesis-driven debugging |
| developer | development | `/nx:implement` | sonnet | TDD implementation, test-first methodology |
| knowledge-tidier | knowledge-tidying | `/nx:knowledge-tidy` | haiku | Persist and organize knowledge in nx store |
| orchestrator | orchestration | `/nx:orchestrate` | haiku | Route requests to appropriate agents |
| pdf-chromadb-processor | pdf-processing | `/nx:pdf-process` | haiku | Index PDFs into nx store for semantic search |
| plan-auditor | plan-validation | `/nx:plan-audit` | sonnet | Validate plans before execution |
| plan-enricher | enrich-plan | `/nx:enrich-plan` | sonnet | Enrich beads with audit findings and execution context |
| strategic-planner | strategic-planning | `/nx:create-plan` | opus | Implementation planning, task decomposition |
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
| `Setup` | `setup.sh` | One-time dependency checks (bd, nx) |
| `SessionStart` | `nx hook session-start` | Initialize nx session |
| `SessionStart` | `mcp_health_hook.sh` | Verify nx and bd are healthy |
| `SessionStart` | `session_start_hook.py` | Surface T2 memory, prime bead state |
| `SessionStart` | `rdr_hook.py` | Report RDR document count and status |
| `SessionStart` | `bd prime` | Load beads context into session |
| `SessionStart` | `using-nx-skills/SKILL.md` | Inject skill invocation discipline |
| `SessionEnd` | `nx hook session-end` | Flush nx session state |
| `PreCompact` | `bd prime` | Re-prime bead context after compact |
| `SubagentStart` | `subagent-start.sh` | Inject context for spawned subagents |
| `PermissionRequest` | `permission-request-stdin.sh` | Auto-approve safe read-only commands |
| `PostToolUse` | `bead_context_hook.py` | Remind to include context pointer in `bd create` |

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
- `/nx:orchestrate` → orchestrator
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
| `nexus` | T1/T2/T3 storage tier access for agents (RDR-034) | `search`, `store_put`, `store_list`, `memory_put`, `memory_get`, `memory_search`, `scratch`, `scratch_manage` |
| `sequential-thinking` | Compaction-resilient reasoning chains | `sequentialthinking` |

### Nexus MCP Server (`nx-mcp`)

The nexus server exposes 8 structured MCP tools that give agents direct access to all three storage tiers without requiring Bash. This eliminates failures in background agents and restricted permission contexts where Bash is unavailable.

**Tool names** follow Claude Code's naming convention: `mcp__plugin_nx_nexus__<tool_name>` (e.g., `mcp__plugin_nx_nexus__search`).

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

- **nexus MCP tools**: all `mcp__plugin_nx_nexus__*` tools (search, store, memory, scratch)
- **sequential thinking**: `mcp__plugin_nx_sequential-thinking__sequentialthinking`
- **beads**: `bd list`, `bd show`, `bd search`, `bd prime`, `bd ready`, `bd status`
- **git**: `git log`, `git diff`, `git status`, `git show`, `git branch -a`
- **nexus CLI**: `nx search`, `nx store list/get`, `nx memory list/get/search`, `nx scratch list`, `nx doctor`
- **maven**: `mvn help:*`, `mvn dependency:tree`, `mvn dependency:analyze`

Dangerous commands (force-push, `bd delete`, deploys) are always denied.
