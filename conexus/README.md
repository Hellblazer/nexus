# Nexus Claude Code Plugin

10 agents, 44 skills, session hooks, slash commands, and two bundled MCP servers for software engineering workflows — backed by the [Nexus CLI](../README.md) for semantic search, plan-centric retrieval via `nx_answer`, and knowledge management.

New to Nexus? The [install guide](https://hellblazer.github.io/nexus/) covers setup end to end, and [Getting started](https://hellblazer.github.io/nexus/getting-started.html) walks the first search, memory, scratch, and knowledge lessons; [Working with RDRs](https://hellblazer.github.io/nexus/rdr.html) covers the RDR lifecycle. This file is reference — what the plugin ships, not how to use it.

## Installation

**Marketplace** (recommended). Full walkthrough, prerequisites, and troubleshooting: [install guide](https://hellblazer.github.io/nexus/).

```bash
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

**Local development** (from the nexus repo checkout):

```bash
claude --plugin-dir ./nx
```

## Prerequisites

The `nx` CLI and plugin work independently, but the plugin's full agent and skill suite requires:

| Dependency | Required for | Install |
|-----------|-------------|---------|
| **`nx` CLI** | Hook scripts, indexing, and CLI-only operations (agents use MCP tools) | [Install guide](https://hellblazer.github.io/nexus/) |
| **`bd` (Beads)** | Task tracking in all agents | [github.com/BeadsProject/beads](https://github.com/BeadsProject/beads) |

Run `/conexus:nx-preflight` after installing to verify all dependencies are present.

A fresh `pip install conexus` + `/plugin install conexus@nexus-plugins`
yields a working substrate on first session with no daemon incantation
of any kind: T2 is served by the storage service, which `nx init`
provisions. The plugin's SessionStart hook no longer spawns anything for
T2 — the T2 daemon was retired in favour of that service. For a storage
service that survives reboots independent of Claude Code (recommended for
any host with regular `nx` CLI use), run `nx daemon service install
--autostart` once after install. See
[Container Integration](../docs/container-integration.md) for the full
story.

**Companion plugin:**
- **[sn](../sn/README.md)** — Serena (LSP code intelligence) + Context7 (library docs) with SubagentStart guidance injection. Install separately: `/plugin install sn@nexus-plugins`.

**Also required:**
- Python 3.12–3.13 (for hook scripts)

## What You Get

- **10 agents** matched to task complexity: opus for reasoning, sonnet for implementation, haiku for utility. The three MCP-tool redirect stubs (`knowledge-tidier`, `plan-auditor`, `plan-enricher`) were deleted (nexus-cnzei.4) — call `nx_tidy` / `nx_plan_audit` / `nx_enrich_beads` directly
- **44 skills** — infrastructure standalone, RDR-078 verb skills, MCP-tool pointer skills (RDR-080), agent-dispatcher skills, and RDR workflow skills
- **5 standard pipelines** — feature, bug, research, onboarding, architecture (`plan-auditor` / `plan-enricher` / `knowledge-tidier` steps now direct MCP tool invocations per RDR-080)
- **Session hooks** — surface T2 memory context, prime beads, health-check dependencies
- **Permission auto-approval** — safe commands and all nexus MCP tools skip the confirmation prompt
- **Two bundled MCP servers** — `nexus` (52 tools: search, query, store, memory, scratch, plans, traverse, scoped/graph-hop search, 10 LLM-backed operators, and 5 orchestration tools including `nx_answer` for plan-centric retrieval) and `nexus-catalog` (10 catalog tools) — plus `sequential-thinking` fetched via npx

### Pick your entry point

New to Nexus? Follow [Getting started](https://hellblazer.github.io/nexus/getting-started.html) instead — it walks these in order, with what you'll see at each step. This table is a lookup once you already know your way around.

| Goal | Start here |
|------|-----------|
| Explore an unfamiliar codebase | `/conexus:analyze-code` |
| Plan a feature or component | `/conexus:brainstorming-gate` → `/conexus:create-plan` |
| Debug a failure | `/conexus:debug` (after 2–3 failed attempts) |
| Review code before committing | `/conexus:review-code` |
| Research an unfamiliar topic | `/conexus:research` |
| Document a technical decision | `/conexus:rdr-create` → `/conexus:rdr-research` → `/conexus:rdr-accept` |
| Index PDFs into semantic search | `/conexus:pdf-process` |
| Not sure which agent to use | Check the skill directory in `using-nx-skills` |

## Directory Structure

```
conexus/
├── agents/
│   └── *.md                 # 10 agent definitions
├── resources/
│   ├── agent-shared/        # Shared resources referenced by all agents (not agent-discoverable — nexus-cnzei.4)
│   │   ├── CONTEXT_PROTOCOL.md  # Standard relay/context exchange protocol
│   │   ├── ERROR_HANDLING.md    # Common error patterns and recovery
│   │   ├── MAINTENANCE.md       # How to maintain/update agents
│   │   ├── README.md            # agent-shared directory guide (this section)
│   │   └── RELAY_TEMPLATE.md    # Canonical relay message format
│   └── rdr/                 # RDR templates and post-mortem scaffolding
├── commands/
│   └── *.md                 # Slash commands (/conexus:research, /conexus:create-plan, /conexus:review-code, etc.)
├── hooks/
│   ├── hooks.json                     # Hook event → script wiring (source of truth)
│   └── scripts/                       # Plugin-resident hooks and shared helpers.
│       │                              # Most hooks now live in the conexus WHEEL
│       │                              # (nexus.hooks.*) — see the table below.
│       ├── mailbox_drain.py           # UserPromptSubmit: render mail addressed to this session
│       ├── _interpreter.py            # Shared helper: re-exec under an interpreter that
│       │                              # can serve the hook (3.12 floor, and the
│       │                              # generation python that can import nexus)
│       ├── t2_prefix_scan.py          # Shared helper: T2 multi-namespace prefix scan
│       └── read_verification_config.py # Shared helper: read .nexus.yml verification block
├── .mcp.json                # Bundled MCP servers (nexus storage + sequential-thinking)
├── registry.yaml            # Single source of truth: agents, pipelines, aliases
├── CHANGELOG.md             # Version history (Keep a Changelog format)
└── skills/
    ├── brainstorming-gate/  # Standalone: design gate before implementation
    ├── catalog/             # Standalone: catalog operations + link graph patterns
    ├── cli-controller/      # Standalone: tmux-based interactive CLI control
    ├── nexus/               # Standalone: CLI reference (all tiers)
    ├── serena-code-nav/     # Standalone: navigate code by symbol (definitions, callers, renames)
    ├── using-nx-skills/     # Standalone: skill invocation discipline
    ├── writing-nx-skills/   # Standalone: plugin authorship guide
    ├── brainstorming-gate/  # Standalone: design gate before implementation
    ├── orchestration/       # Standalone: routing reference
    ├── mailbox/             # Standalone: RDR-205 mailbox/<address> tuple-space convention
    ├── peer-messaging/      # Standalone: messaging other sessions and agents, sharing one machine
    │
    │   # RDR-078 verb skills (dispatch plan_match + plan_run)
    ├── research/            # verb: research / design / architecture
    ├── review/              # verb: review / critique / audit change-set
    ├── analyze/             # verb: analyze / compare across corpora
    ├── debug/               # verb: debug / failing-path investigation
    ├── document/            # verb: document coverage / authoring
    ├── plan-first/          # gate: plan_match before any retrieval
    │
    │   # RDR-080 pointer skills (dispatch a specific MCP tool — no agent spawn)
    ├── query/               # → mcp__plugin_conexus_nexus__nx_answer
    ├── knowledge-tidying/   # → mcp__plugin_conexus_nexus__nx_tidy
    ├── enrich-plan/         # → mcp__plugin_conexus_nexus__nx_enrich_beads
    ├── plan-validation/     # → mcp__plugin_conexus_nexus__nx_plan_audit
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
    ├── rdr-show/            # RDR: show RDR details
    ├── rdr-research/        # RDR: delegate research to agents
    └── rdr-audit/           # RDR: audit project RDR lifecycle
```

## Standalone Skills (26)

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
| plan-first | Retrieval gate — try `plan_match` before falling through to `/conexus:query` |

### MCP-tool pointer skills (RDR-080)

| Skill | Delegates to |
|-------|--------------|
| query | `mcp__plugin_conexus_nexus__nx_answer` — multi-step retrieval |
| knowledge-tidying | `mcp__plugin_conexus_nexus__nx_tidy` — knowledge consolidation |
| enrich-plan | `mcp__plugin_conexus_nexus__nx_enrich_beads` — bead context enrichment |
| plan-validation | `mcp__plugin_conexus_nexus__nx_plan_audit` — plan audit |

### Infrastructure skills

| Skill | Purpose |
|-------|---------|
| brainstorming-gate | Design gate — requires exploration and user approval before implementation |
| catalog | Catalog operations + link graph patterns — resolve, link, context, seed |
| cli-controller | Expert guidance for controlling interactive CLI applications via tmux |
| composition-probe | Runtime composition smoke test for coordinator beads |
| finishing-branch | Guide branch completion — verify tests, present merge/PR/keep/discard |
| git-worktrees | Isolated workspace setup via git worktrees with safety verification |
| mailbox | RDR-205 mailbox/<address> tuple-space convention — send by tuple_out, drain by tuple_in before hand-back |
| nexus | Nexus CLI reference for all tiers (T1/T2/T3) |
| peer-messaging | Messaging other sessions and dispatched agents: channel choice, acknowledgement, trust boundary, sharing one machine |
| orchestration | Agent routing reference — routing tables, pipeline templates |
| phase-review-gate | Phase-boundary gate — cross-walks RDR §Approach against closing beads to block silent scope reduction |
| receiving-review | Technical evaluation of code review feedback |
| serena-code-nav | Navigate code by symbol — definitions, callers, type hierarchies |
| upgrade | Shows what `nx upgrade` would converge, then runs it |
| using-nx-skills | Skill invocation discipline — check skills before every response |
| writing-nx-skills | Guide for authoring conexus plugin skills |

## Agents (13)

See [`registry.yaml`](./registry.yaml) for full metadata (model, triggers, predecessors/successors).

### Active agents (10)

| Agent | Skill | Command | Model | Purpose |
|-------|-------|---------|-------|---------|
| architect-planner | architecture | `/conexus:architecture` | opus | Software architecture design, execution plans |
| code-review-expert | code-review | `/conexus:review-code` | sonnet | Code quality, security, best practices |
| codebase-deep-analyzer | codebase-analysis | `/conexus:analyze-code` | sonnet | Architecture, patterns, dependency mapping |
| debugger | debugging | `/conexus:debug` | opus | Hypothesis-driven debugging |
| deep-analyst | deep-analysis | `/conexus:deep-analysis` | opus | Complex problem investigation, root cause |
| deep-research-synthesizer | research-synthesis | `/conexus:research` | sonnet | Multi-source research with synthesis |
| developer | development | `/conexus:implement` | sonnet | TDD implementation, test-first methodology |
| strategic-planner | strategic-planning | `/conexus:create-plan` | opus | Implementation planning, task decomposition |
| substantive-critic | substantive-critique | `/conexus:substantive-critique` | sonnet | Constructive critique of plans/designs/code |
| test-validator | test-validation | `/conexus:test-validate` | sonnet | Test coverage and quality validation |

### Retired stub agents — call the MCP tool directly (RDR-080, deleted nexus-cnzei.4)

`knowledge-tidier`, `plan-auditor`, and `plan-enricher` were 40-line stubs that
did nothing but redirect to an MCP tool. They were deleted outright rather
than kept as a redirect layer — the corresponding pointer skills
(`knowledge-tidying`, `plan-validation`, `enrich-plan`) document the same call
shape without an agent-dispatch detour.

| Former stub agent | Replacement | Call shape |
|------------|-------------|------------|
| knowledge-tidier | nx_tidy | `mcp__plugin_conexus_nexus__nx_tidy(topic=..., collection="<subject>")` |
| plan-auditor | nx_plan_audit | `mcp__plugin_conexus_nexus__nx_plan_audit(plan_json=..., context="")` |
| plan-enricher | nx_enrich_beads | `mcp__plugin_conexus_nexus__nx_enrich_beads(bead_description=..., context="")` |

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

The `nx_plan_audit` / `nx_enrich_beads` / `nx_tidy` MCP-tool steps above replaced the
`plan-auditor`, `plan-enricher`, and `knowledge-tidier` agents per RDR-080; those
three stub agents were deleted at nexus-cnzei.4. Callers invoke the tool
directly instead of dispatching a sub-agent.

## Hooks

See `hooks/hooks.json` for exact wiring. A `hooks/scripts/...` path below uses
`$CLAUDE_PLUGIN_ROOT` as the plugin root. A bare `hook_*` name is an MCP tool served
by the nexus MCP server out of the conexus wheel (`nexus.hooks.*`), not a file in this
plugin — RDR-215 moved those hooks into the wheel so their logic ships and is tested as
Python. They have no command line to run by hand; `nx-hook <verb>` is the command tier,
and only the ledger verbs have one.

| Event | Handler | Purpose |
|-------|--------|---------|
| `SessionStart` | `nx-hook upgrade-auto` | Auto-converge the CLI to the plugin's minimum required version. Spawns `nx upgrade --auto`; the shell form's `2>/dev/null` and its version-skew `|| echo` guidance live in the verb (`nexus.hooks.upgrade_auto`) |
| `SessionStart` | `nx-hook self-gc` | Reap superseded install generations no live process is holding. Spawns `nx self gc`; the shell form's `>/dev/null 2>&1 \|\| true` is the verb's own silence (`nexus.hooks.self_gc`) |
| `SessionStart` | `nx-hook preflight` | Silent health check of skill-routed tool reachability; emits a `## nx Preflight: FAILED` marker on gaps (nexus-hwbj) |
| `SessionStart` | `nx-hook session-start` | Resolve/propagate session id; emit the skill-invocation guidance imperative (nexus-h33x8.4 — moved here from the pinned `cat .../using-nx-skills/SKILL.md` entry so guidance edits ship at PyPI-release/reinstall cadence instead of plugin-release cadence; see `nexus.session_start_guidance`) |
| `SessionStart` | `nx-hook session-context` | Surface T2 memory, ready beads, and scratch context at session start |
| `SessionStart` | `nx-hook rdr` | Reconcile RDR file frontmatter ↔ T2 metadata (self-healing on divergence) |
| `SessionStart` | `nx-hook behaviour-census` | Report the PREVIOUS session's raw thinking and decision counts (nexus-4lnn1) |
| `SessionStart` (matcher `startup`) | `nx-hook version-lockstep` | Detect plugin↔CLI version skew (RDR-143); nudge and dispatch a detached, extras-preserving upgrade that takes effect next session |
| `SessionEnd` | `nx-session-end-launcher` | Flush session-end bookkeeping (memory, beads, scratch) via a detached grandchild |
| `UserPromptSubmit` | `hooks/scripts/mailbox_drain.py` | Claim, ack and render this session's RDR-205 mailbox rows; the unconditional delivery floor beneath the channel |
| `SubagentStart` | `hook_subagent_start_tuple` | Project the ledger START tuple, as a sibling of the main hook so its failure does not take the projection with it |
| `SubagentStop` | `hook_subagent_stop_tuple` | Project the ledger REPORT tuple, same sibling shape |
| `PostCompact` | `hook_post_compact` | Re-prime context (memory, beads, scratch) after `/compact` |
| `Stop` | `hook_stop_verification` | Opt-in session-end verification: tests + git state (see [Configuration § Verification](../docs/configuration.md#verification)) |
| `StopFailure` | `hook_stop_failure` | Advisory on abnormal session termination |
| `PreToolUse` (`Bash`) | `nx-hook pre-close-verification` | Opt-in bd-close gate: verifies before `bd close` / `bd done` |
| `PreToolUse` (`Bash`) | `hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` | Deny index-writing / working-tree-destroying git verbs from subagents in the shared tree (RDR-184 Gap-4) |
| `PreToolUse` (`Bash`) | `hooks/scripts/routing/phase_review_close_requires_gate.py` | Deny `bd close` on a phase-review bead without a fresh PASSED gate sentinel (RDR-121 P2) |
| `PreToolUse` (`Agent\|Task`) | `hook_agent_dispatch_expect` | Write the RDR-184 EXPECT ledger row from the dispatch's own `subagent_type` + `run_in_background`, so orchestration doesn't have to hand-write it (nexus-qc4p1) |
| `PostToolUse` | `hook_divergence_language_guard` | Advisory scan of RDR post-mortem writes for divergence-language patterns (RDR-065 Gap 2) |
| `SubagentStart` | `hook_subagent_start` | Inject inherited context (active bead, session, MCP priority) into spawned subagents |
| `SubagentStart` | `hook_subagent_start_stamp` | Record the RDR-184 EXPECT-ledger START row (agent id + type) at dispatch time (nexus-ccs9v.16) |
| `SubagentStop` | `nx-hook subagent-stop` | Block a named background teammate's idle once if it never sent a completion report (RDR-184 Gap 1) |
| `PreToolUse` (`mcp__plugin_conexus_.*`) | `nx-hook auto-approve` | Auto-approve nexus and nexus-catalog MCP tool calls; paired with the PermissionRequest entry below because the two events fire in different permission modes |
| `PermissionRequest` (`mcp__plugin_conexus_.*`) | `nx-hook auto-approve` | Auto-approve nexus and nexus-catalog MCP tool calls |

**Why some rows are `nx-hook <verb>` and others are `hook_<name>`.** A
hook that returns a VERDICT — a `permissionDecision`, a `behavior`, a
stop `decision` — belongs on the command tier, because an `mcp_tool`
hook cannot return one. Claude Code names four hook types that carry a
decision (`prompt`, `agent`, `command`, `http`) and `mcp_tool` is not
among them; its output is read for context. The three deciding hooks
(`pre-close-verification`, `subagent-stop`, `auto-approve`) were wired as
`mcp_tool` in conexus 7.55.0 and all three were inert for that release —
the close gate let an unreviewed `bd close` through while returning a
correct deny to anything that called it directly. Each is still
registered on both tiers, because the verdict is useful as data; only the
command tier is wired. `nexus.mcp.hooks.DECIDING_HOOKS` is the list, and
`tests/test_deciding_hooks_are_command_tier.py` refuses a `hooks.json`
that moves any of them back.

## Slash Commands

**Agent commands** (`/command → agent`):
- `/conexus:research` → deep-research-synthesizer
- `/conexus:create-plan` → strategic-planner
- `/conexus:analyze-code` → codebase-deep-analyzer
- `/conexus:review-code` → code-review-expert
- `/conexus:test-validate` → test-validator
- `/conexus:implement` → developer
- `/conexus:debug` → debugger
- `/conexus:architecture` → architect-planner
- `/conexus:deep-analysis` → deep-analyst
- `/conexus:substantive-critique` → substantive-critic

`/conexus:architecture`, `/conexus:deep-analysis`, `/conexus:substantive-critique`,
`/conexus:phase-review-gate`, and `/conexus:upgrade` are now served solely by the
same-named skill — nexus-cnzei.4 deleted the redundant `commands/*.md` wrapper
for each (it duplicated the skill's own relay and had drifted into a
self-referential "invoke the X skill" loop). The bash-preamble context these
commands used to inject is not replaced; the skill's own MCP-tool
project-context calls cover the same ground.

**MCP-tool pointers** (RDR-080 — dispatch the named MCP tool directly). Some
are commands (`conexus/commands/*.md`) and some are skills
(`conexus/skills/*/SKILL.md`); Claude Code spells both `/conexus:<name>`, so
the invocation reads the same and the surface is marked per line below:
- `/conexus:query` (skill) → `nx_answer` (multi-step retrieval)
- `/conexus:knowledge-tidying` (skill) → `nx_tidy` *(was → knowledge-tidier agent; command `/conexus:knowledge-tidy` merged into this skill at nexus-cnzei.4)*
- `/conexus:plan-audit` (command) → `nx_plan_audit` *(was → plan-auditor agent)*
- `/conexus:enrich-plan` (skill) → `nx_enrich_beads` *(was → plan-enricher agent)*
- `/conexus:pdf-process` (command) → `nx index pdf` CLI *(was → pdf-chromadb-processor agent)*

**Utility commands** (no agent dispatch, no MCP call — direct local action):
- `/conexus:continuation [topic]` — write a paste-ready handoff prompt to `~/.cache/nexus/continuations/` capturing branch, in-progress beads, open PRs, and active T2 memory. Use at session close. Compressed prompt is emitted in chat as a copy-clickable code block.
- `/conexus:nx-preflight` — verify conexus plugin dependencies (CLI, doctor, beads).

**RDR commands**: `/conexus:rdr-create`, `/conexus:rdr-list`, `/conexus:rdr-show`, `/conexus:rdr-research`, `/conexus:rdr-gate`, `/conexus:rdr-accept`, `/conexus:rdr-close`, `/conexus:rdr-audit`. Four of these
(`rdr-gate`, `rdr-accept`, `rdr-audit`, plus `rdr-fix` not shown above) are backed by BOTH a `commands/*.md`
file and a same-named skill — a deliberately-kept dual surface (see `_KNOWN_COMMAND_SKILL_COLLISIONS`
in `tests/test_plugin_structure.py`). `rdr-list` is command-only (its skill depended on the command's
own bash-injected data and was deleted at nexus-cnzei.4); `rdr-create`/`rdr-close`/`rdr-research`/`rdr-show`
are skill-only (the redundant command was deleted at nexus-cnzei.4).


## MCP Servers

The plugin ships `.mcp.json` which Claude Code picks up automatically on install:

| Server | Purpose | Tools |
|--------|---------|-------|
| `nexus` | Retrieval + storage (core) | 52 tools — see below |
| `nexus-catalog` | Catalog access (RDR-062) | `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats` |
| `sequential-thinking` | Compaction-resilient reasoning chains | `sequentialthinking` |

### `nexus` MCP tool catalog (52 tools)

| Category | Tools |
|----------|-------|
| Retrieval (T3) | `search`, `query`, `store_put`, `store_get`, `store_get_many`, `store_list` |
| Scoped search (RDR-156, service mode) | `search_metadata_scoped`, `search_topic_scoped`, `search_graph_hop` |
| Memory (T2) | `memory_put`, `memory_get`, `memory_search`, `memory_delete`, `memory_consolidate` |
| Scratch (T1) | `scratch`, `scratch_manage` |
| Collections | `collection_list` |
| Plans (RDR-078) | `plan_save`, `plan_search`, `plan_delete`, `traverse` |
| Tuple space (RDR-205/206/211/213) | `tuple_out`, `tuple_rd`, `tuple_in`, `tuple_ack`, `tuple_nack`, `tuple_renew`, `tuple_release`, `tuple_registry`, `tuple_list`, `tuple_stats`, `tuple_subscribe`, `tuple_unsubscribe`, `tuple_subscriptions` |
| Operators (RDR-079/088/093) | `operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate`, `operator_filter`, `operator_groupby`, `operator_aggregate`, `operator_check`, `operator_verify` |
| Orchestration (RDR-080) | `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit` |
| Admin | `daemon_uninstall` |

**`nx_answer`** is the retrieval entry point for multi-step questions.
It runs `plan_match` against the library, executes the best-matching plan
via `plan_run`, and falls through to an inline planner on miss.  See
[`docs/querying-guide.md`](../docs/querying-guide.md) for the pattern.

### Nexus MCP Servers (`nx-mcp`, `nx-mcp-catalog`)

The nexus core server exposes 52 MCP tools and the nexus-catalog server exposes 10 catalog tools, for 62 registered tools total (3 tools demoted to Python-only). These give agents direct access to all three storage tiers and the catalog without requiring Bash. This eliminates failures in background agents and restricted permission contexts where Bash is unavailable.

**Pagination**: `search`, `store_list`, and `memory_search` return paged results. Pass `offset=N` for subsequent pages. Response footer: `--- showing X-Y of Z. next: offset=N` or `(end)`.

**Tool names** follow Claude Code's naming convention: `mcp__plugin_conexus_nexus__<tool_name>` for core tools, `mcp__plugin_conexus_nexus-catalog__<tool_name>` for catalog tools.

**Resource management**:
- T1 and T3 use thread-safe lazy singletons (expensive to initialize, reused across the session)
- T2 uses per-call context managers (`T2Database`, HTTP client to the engine's Postgres; the SQLite-backed version retired at RDR-158 P4)
- All errors return `"Error: {message}"` strings — no exceptions surface as framework errors

**Agent frontmatter**: Agents do NOT declare a `tools:` field — Claude Code has a confirmed bug (GitHub #13605, #21560, #25200) where explicit `tools:` in plugin-defined agents filters out MCP tools. Agents inherit all tools from the parent session. The PermissionRequest hook provides runtime enforcement. Agent body text references MCP tool syntax (not CLI commands). See RDR-035.

**Human CLI**: The `nx` CLI remains the primary interface for human users. All `docs/` documentation uses CLI syntax. The MCP server is transparent to human workflows.

### Sequential Thinking

No separate install required — `npx` fetches `@modelcontextprotocol/server-sequential-thinking` on first use.

## Key Concepts

### Agent Relay Format

When skills delegate to agents, they use a standardized relay format defined in `resources/agent-shared/RELAY_TEMPLATE.md`:

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

- **nexus MCP tools**: all `mcp__plugin_conexus_nexus__*` core tools and `mcp__plugin_conexus_nexus-catalog__*` catalog tools
- **sequential thinking**: `mcp__plugin_conexus_sequential-thinking__sequentialthinking`
- **beads**: `bd list`, `bd show`, `bd search`, `bd prime`, `bd ready`, `bd status`
- **git**: `git log`, `git diff`, `git status`, `git show`, `git branch -a`
- **nexus CLI**: `nx search`, `nx store list/get`, `nx memory list/get/search`, `nx scratch list`, `nx doctor`
- **maven**: `mvn help:*`, `mvn dependency:tree`, `mvn dependency:analyze`

Dangerous commands (force-push, `bd delete`, deploys) are always denied.
