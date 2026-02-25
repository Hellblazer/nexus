# Nexus Claude Code Plugin

15 agents, 26 skills, session hooks, and slash commands for software engineering workflows — backed by the [Nexus CLI](../README.md) for semantic search and knowledge management.

## Installation

Claude Code auto-discovers this plugin when you work in a repo that contains the `nx/` directory. No manual installation needed for the nexus repo itself.

For repos without the plugin directory, `nx install claude-code` provides lightweight CLI hooks (session start/end) and a skill reference — but not the agents, slash commands, or full hook suite.

## Prerequisites

- [Nexus](https://github.com/Hellblazer/nexus) — `nx` CLI installed and configured
- [Beads](https://github.com/BeadsProject/beads) — `bd` CLI for task tracking
- Python 3.12+ (for hook scripts)
- [superpowers](https://github.com/anthropics/claude-plugins-official/tree/main/superpowers) plugin installed

## What You Get

- **15 agents** matched to task complexity: opus for reasoning, sonnet for implementation, haiku for utility
- **26 skills** — 5 standalone + 15 agent-delegating + 6 RDR workflow
- **5 standard pipelines** — feature, bug, research, onboarding, architecture
- **Session hooks** — auto-load PM context, prime beads, health-check dependencies
- **Permission auto-approval** — safe read-only commands skip the confirmation prompt

## Directory Structure

```
nx/
├── .claude-plugin/
│   └── plugin.json          # Plugin manifest (name, version, license)
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
│       ├── session_start_hook.py     # Load PM context, prime beads
│       ├── setup.sh                  # One-time setup checks
│       └── subagent-start.sh         # Context prep for spawned subagents
├── registry.yaml            # Single source of truth: agents, pipelines, aliases
├── CHANGELOG.md            # Version history (Keep a Changelog format)
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
    └── test-validation/     # → test-validator agent
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

**PM commands**: `/pm-new`, `/pm-status`, `/pm-list`, `/pm-archive`, `/pm-restore`, `/pm-close`

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
