# Nexus Claude Code Plugin

15 agents, 27 skills, session hooks, slash commands, and a bundled MCP server for software engineering workflows — backed by the [Nexus CLI](../README.md) for semantic search and knowledge management.

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
| **`nx` CLI** | All hooks and skills that run `nx` commands | See [Getting Started](../docs/getting-started.md) |
| **`bd` (Beads)** | Task tracking in all agents | [github.com/BeadsProject/beads](https://github.com/BeadsProject/beads) |
| **superpowers plugin** | Cross-referenced skills (brainstorming, TDD, verification, writing-plans) | `/plugin marketplace add anthropics/claude-plugins-official` |

Run `/nx-preflight` after installing to verify all dependencies are present.

**Also required:**
- Python 3.12+ (for hook scripts)

## What You Get

- **15 agents** matched to task complexity: opus for reasoning, sonnet for implementation, haiku for utility
- **27 skills** — 5 standalone + 15 agent-delegating + 7 RDR workflow
- **5 standard pipelines** — feature, bug, research, onboarding, architecture
- **Session hooks** — auto-load PM context, prime beads, health-check dependencies
- **Permission auto-approval** — safe read-only commands skip the confirmation prompt
- **Bundled MCP server** — sequential-thinking via `.mcp.json` (no separate install)

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
│   └── *.md                 # Slash commands (/research, /create-plan, /review-code, etc.)
├── hooks/
│   ├── hooks.json           # Hook event → script wiring
│   └── scripts/
│       ├── bead_context_hook.py      # Bead context injection
│       ├── mcp_health_hook.sh        # MCP/nx health checks at session start
│       ├── permission-request-stdin.sh # Auto-approve safe read-only commands
│       ├── rdr_hook.py               # Report RDR document count and status
│       ├── session_start_hook.py     # Load PM context, prime beads
│       ├── setup.sh                  # One-time setup checks
│       └── subagent-start.sh         # Context prep for spawned subagents
├── .mcp.json                # Bundled MCP servers (sequential-thinking)
├── registry.yaml            # Single source of truth: agents, pipelines, aliases
├── CHANGELOG.md             # Version history (Keep a Changelog format)
└── skills/
    ├── brainstorming-gate/  # Standalone: design gate before implementation
    ├── cli-controller/      # Standalone: tmux-based interactive CLI control
    ├── nexus/               # Standalone: nx CLI reference (all tiers)
    ├── using-nx-skills/     # Standalone: skill invocation discipline
    ├── writing-nx-skills/   # Standalone: plugin authorship guide
    ├── code-review/         # → code-review-expert agent
    ├── codebase-analysis/   # → codebase-deep-analyzer agent
    ├── deep-analysis/       # → deep-analyst agent
    ├── substantive-critique/# → substantive-critic agent
    ├── java-architecture/   # → java-architect-planner agent
    ├── java-debugging/      # → java-debugger agent
    ├── java-development/    # → java-developer agent
    ├── knowledge-tidying/   # → knowledge-tidier agent
    ├── orchestration/       # → orchestrator agent
    ├── pdf-processing/      # → pdf-chromadb-processor agent
    ├── plan-validation/     # → plan-auditor agent
    ├── project-setup/       # → project-management-setup agent
    ├── research-synthesis/  # → deep-research-synthesizer agent
    ├── strategic-planning/  # → strategic-planner agent
    ├── test-validation/     # → test-validator agent
    ├── rdr-accept/          # RDR workflow: accept a gated RDR
    ├── rdr-close/           # RDR workflow: close RDR, create beads
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

## Standalone Skills (5)

Skills that provide guidance directly without delegating to an agent.

| Skill | Purpose |
|-------|---------|
| brainstorming-gate | Design gate — requires exploration and user approval before implementation |
| cli-controller | Expert guidance for controlling interactive CLI applications via tmux |
| nexus | Nexus CLI reference for all tiers (T1/T2/T3) |
| using-nx-skills | Skill invocation discipline — check skills before every response |
| writing-nx-skills | Guide for authoring nx plugin skills |

## Agents (15)

See [`registry.yaml`](./registry.yaml) for full metadata (model, triggers, predecessors/successors).

| Agent | Skill | Command | Model | Purpose |
|-------|-------|---------|-------|---------|
| code-review-expert | code-review | `/review-code` | sonnet | Code quality, security, best practices |
| codebase-deep-analyzer | codebase-analysis | `/analyze-code` | sonnet | Architecture, patterns, dependency mapping |
| deep-analyst | deep-analysis | `/deep-analysis` | opus | Complex problem investigation, root cause |
| substantive-critic | substantive-critique | `/substantive-critique` | sonnet | Constructive critique of plans/designs/code |
| deep-research-synthesizer | research-synthesis | `/research` | sonnet | Multi-source research with synthesis |
| java-architect-planner | java-architecture | `/java-architecture` | opus | Java architecture design, execution plans |
| java-debugger | java-debugging | `/java-debug` | opus | Hypothesis-driven Java debugging |
| java-developer | java-development | `/java-implement` | sonnet | TDD implementation, test-first methodology |
| knowledge-tidier | knowledge-tidying | `/knowledge-tidy` | haiku | Persist and organize knowledge in nx store |
| orchestrator | orchestration | `/orchestrate` | haiku | Route requests to appropriate agents |
| pdf-chromadb-processor | pdf-processing | `/pdf-process` | haiku | Index PDFs into nx store for semantic search |
| plan-auditor | plan-validation | `/plan-audit` | sonnet | Validate plans before execution |
| project-management-setup | project-setup | `/project-setup` | haiku | Create PM infrastructure for multi-week projects |
| strategic-planner | strategic-planning | `/create-plan` | opus | Implementation planning, task decomposition |
| test-validator | test-validation | `/test-validate` | sonnet | Test coverage and quality validation |

## Standard Pipelines

Defined in `registry.yaml`:

- **feature**: strategic-planner → plan-auditor → java-architect-planner → java-developer → code-review-expert → test-validator
- **bug**: java-debugger → java-developer → code-review-expert → test-validator
- **research**: deep-research-synthesizer → knowledge-tidier
- **onboarding**: codebase-deep-analyzer → strategic-planner
- **architecture**: codebase-deep-analyzer → deep-analyst → strategic-planner → plan-auditor → java-architect-planner

## Hooks

| Event | Script | Purpose |
|-------|--------|---------|
| `Setup` | `setup.sh` | One-time dependency checks (bd, nx) |
| `SessionStart` | `nx hook session-start` | Initialize nx session |
| `SessionStart` | `mcp_health_hook.sh` | Verify nx and bd are healthy |
| `SessionStart` | `session_start_hook.py` | Load PM context, prime bead state |
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
- `/research` → deep-research-synthesizer
- `/create-plan` → strategic-planner
- `/plan-audit` → plan-auditor
- `/analyze-code` → codebase-deep-analyzer
- `/review-code` → code-review-expert
- `/test-validate` → test-validator
- `/java-implement` → java-developer
- `/java-debug` → java-debugger
- `/java-architecture` → java-architect-planner
- `/orchestrate` → orchestrator
- `/knowledge-tidy` → knowledge-tidier
- `/pdf-process` → pdf-chromadb-processor
- `/project-setup` → project-management-setup
- `/deep-analysis` → deep-analyst
- `/substantive-critique` → substantive-critic

**RDR commands**: `/rdr-create`, `/rdr-list`, `/rdr-show`, `/rdr-research`, `/rdr-gate`, `/rdr-accept`, `/rdr-close`

**PM commands**: `/pm-new`, `/pm-status`, `/pm-list`, `/pm-archive`, `/pm-restore`, `/pm-close`

## MCP Servers

The plugin ships `.mcp.json` which Claude Code picks up automatically on install:

| Server | Purpose |
|--------|---------|
| `sequential-thinking` | Compaction-resilient reasoning chains via `mcp__sequential-thinking__sequentialthinking` |

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
- **nx memory projects**: `{repo}_active`, `{repo}_rdr`
- **Bead IDs**: managed by `bd` CLI

### Permission Auto-Approval

The permission hook auto-approves safe read-only operations:

- **beads**: `bd list`, `bd show`, `bd search`, `bd prime`, `bd ready`, `bd status`
- **git**: `git log`, `git diff`, `git status`, `git show`, `git branch -a`
- **nexus**: `nx search`, `nx store list/get`, `nx memory list/get/search`, `nx scratch list`, `nx pm status`, `nx doctor`
- **maven**: `mvn help:*`, `mvn dependency:tree`, `mvn dependency:analyze`

Dangerous commands (force-push, `bd delete`, deploys) are always denied.
