# Nexus Claude Code Plugin

A Claude Code plugin that provides a full suite of specialized agents, skills, commands, and hooks for software engineering workflows — all backed by **Nexus** for semantic search, session memory, and persistent knowledge storage.

## Overview

**Storage tiers:**
- **T1** — `nx scratch`: ephemeral session scratch, cleared on exit
- **T2** — `nx memory`: per-project persistent notes (SQLite + FTS5)
- **T3** — `nx store` / `nx search`: permanent cross-session knowledge (ChromaDB cloud + Voyage AI)

## Directory Structure

```
nx-plugin/
├── .claude-plugin/
│   └── plugin.json          # Plugin manifest (name, version, license)
├── agents/
│   ├── _shared/             # Shared resources referenced by all agents
│   │   ├── CONTEXT_PROTOCOL.md  # Standard relay/context exchange protocol
│   │   ├── ERROR_HANDLING.md    # Common error patterns and recovery
│   │   ├── MAINTENANCE.md       # How to maintain/update agents
│   │   ├── README.md            # _shared directory guide (this section)
│   │   └── RELAY_TEMPLATE.md    # Canonical relay message format
│   └── *.md                 # 14 specialized agent definitions
├── commands/
│   └── *.md                 # Slash commands (/research, /plan, /code-review, etc.)
├── hooks/
│   ├── hooks.json           # Hook event → script wiring
│   └── scripts/
│       ├── bead_context_hook.py      # Bead context injection
│       ├── mcp_health_hook.sh        # MCP/nx health checks at session start
│       ├── permission-request-stdin.sh # Auto-approve safe read-only commands
│       ├── pre_compact_hook.py       # Save state before context compression
│       ├── session_start_hook.py     # Load PM context, prime beads
│       ├── setup.sh                  # One-time setup checks
│       └── subagent-start.sh         # Context prep for spawned subagents
├── registry.yaml            # Single source of truth: agents, pipelines, aliases
└── skills/
    ├── cli-controller/      # Standalone: tmux-based interactive CLI control
    ├── nexus/               # Standalone: nx CLI reference (all tiers)
    ├── code-review/         # → code-review-expert agent
    ├── codebase-analysis/   # → codebase-deep-analyzer agent
    ├── deep-analysis/       # → deep-analyst agent
    ├── substantive-critique/       # → substantive-critic agent
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

## Agents (14)

See [`registry.yaml`](./registry.yaml) for full metadata (model, triggers, predecessors/successors).

| Agent | Skill | Command | Model | Purpose |
|-------|-------|---------|-------|---------|
| code-review-expert | code-review | `/code-review` | sonnet | Code quality, security, best practices |
| codebase-deep-analyzer | codebase-analysis | `/analyze-code` | sonnet | Architecture, patterns, dependency mapping |
| deep-analyst | deep-analysis | `/deep-analysis` | opus | Complex problem investigation, root cause |
| substantive-critic | substantive-critique | `/substantive-critique` | sonnet | Constructive critique of plans/designs/code |
| deep-research-synthesizer | research-synthesis | `/research` | sonnet | Multi-source research with synthesis |
| java-architect-planner | java-architecture | `/java-architecture` | opus | Java architecture design, phased plans |
| java-debugger | java-debugging | `/java-debug` | opus | Hypothesis-driven Java debugging |
| java-developer | java-development | `/java-implement` | sonnet | TDD implementation, test-first methodology |
| knowledge-tidier | knowledge-tidying | `/knowledge-tidy` | haiku | Persist and organize knowledge in nx store |
| orchestrator | orchestration | `/orchestrate` | haiku | Route requests to appropriate agents |
| pdf-chromadb-processor | pdf-processing | `/pdf-process` | haiku | Index PDFs into nx store for semantic search |
| plan-auditor | plan-validation | `/plan-audit` | sonnet | Validate plans before execution |
| project-management-setup | project-setup | `/project-setup` | haiku | Create PM infrastructure for multi-week projects |
| strategic-planner | strategic-planning | `/plan` | opus | Implementation planning, task decomposition |
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
| `SessionStart` | `bd prime` | Load beads context into session |
| `SessionEnd` | `nx hook session-end` | Flush nx session state |
| `PreCompact` | `pre_compact_hook.py` | Save work state before context compression |
| `PreCompact` | `bd prime` | Re-prime bead context after compact |
| `SubagentStart` | `subagent-start.sh` | Inject context for spawned subagents |
| `PermissionRequest` | `permission-request-stdin.sh` | Auto-approve safe read-only commands |
| `PostToolUse` | `bead_context_hook.py` | Remind to include context pointer in `bd create` |

## Slash Commands

**Agent commands** (`/command → agent`):
- `/research` → deep-research-synthesizer
- `/plan` → strategic-planner
- `/plan-audit` → plan-auditor
- `/analyze-code` → codebase-deep-analyzer
- `/code-review` → code-review-expert
- `/test-validate` → test-validator
- `/java-implement` → java-developer
- `/java-debug` → java-debugger
- `/java-architecture` → java-architect-planner
- `/orchestrate` → orchestrator
- `/knowledge-tidy` → knowledge-tidier
- `/pdf-process` → pdf-chromadb-processor
- `/project-setup` → project-management-setup
- `/substantive-critique` → substantive-critic

**Session commands**: `/session-save`, `/session-load`, `/session-list`, `/session-delete`

**PM commands**: `/pm-new`, `/pm-status`, `/pm-list`, `/pm-archive`, `/pm-restore`, `/pm-close`

## Key Concepts

### Agent Relay Format

All agent handoffs use the relay template from `agents/_shared/RELAY_TEMPLATE.md`:

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

- **nx store titles**: use hyphens — `decision-architect-cache-strategy`, `research-auth-patterns`
- **nx memory keys**: `{project}_active/{doc}.md` — e.g., `myrepo_active/findings.md`
- **Bead IDs**: managed by `bd` CLI — e.g., `beads-abc123`

### Permission Auto-Approval

`hooks/scripts/permission-request-stdin.sh` auto-approves safe read-only operations:
- `bd list`, `bd show`, `bd search`, `bd prime`, `bd ready`, `bd status`
- `git log`, `git diff`, `git status`, `git show`, `git branch -a`, `git remote -v`
- `nx search`, `nx store list/get`, `nx memory list/get/search`, `nx scratch list`, `nx pm status`, `nx doctor`, `nx health`, `nx index`
- `mvn help:*`, `mvn dependency:tree`, `mvn dependency:analyze`, `mvn versions:display`

Dangerous commands (force-push, `bd delete`, deploys) are always denied.

## Prerequisites

- [Nexus](https://github.com/Hellblazer/nexus) — `nx` CLI installed and configured
- [Beads](https://github.com/BeadsProject/beads) — `bd` CLI for task tracking
- Python 3.12+ (for hook scripts)
