---
title: "Claude Code Framework Alignment (v2.1.72–v2.1.81)"
id: RDR-039
type: Technical Debt
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-21
accepted_date: 2026-03-21
closed_date: 2026-03-22
close_reason: implemented
related_issues: []
---

# RDR-039: Claude Code Framework Alignment (v2.1.72–v2.1.81)

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Claude Code v2.1.72–v2.1.81 (March 10–20, 2026) introduced new plugin/agent/skill capabilities, fixed bugs that directly impacted nexus workflows, and added hook events the nx plugin does not yet exploit. The nexus plugin is leaving value on the table and carrying workarounds for issues that are now fixed upstream.

This RDR captures the full alignment gap, organizes the work into independent phases, and prioritizes by impact.

## Context

### Background

A systematic review of 10 Claude Code releases identified 24 items relevant to the nexus plugin, ranging from new frontmatter fields for agents and skills to critical bug fixes affecting worktree isolation, MCP tool schemas, and session stability.

The nexus plugin currently ships:
- 15 agents (with `model:` but no `effort`, `maxTurns`, or `disallowedTools`)
- 28 skills (no `effort` frontmatter)
- Hook events: SessionStart (6 commands), PreCompact (1), Setup (1), SubagentStart (1), PermissionRequest (1), PostToolUse (1)
- Heavy reliance on MCP tools (serena, context7, mixedbread, nx nexus)
- Extensive subagent dispatching in RDR and planning workflows

### Technical Environment

- Claude Code v2.1.81 (current)
- Nexus plugin: `nx/` directory — agents, skills, hooks, commands
- Python 3.12+ CLI: `src/nexus/`
- Plugin hook system: `nx/hooks/hooks.json`

### Changelog Source

Claude Code GitHub releases v2.1.72–v2.1.81, fetched 2026-03-21.

## Research Findings

### Investigation

All items below were identified by cross-referencing the Claude Code changelog against the nexus plugin's current configuration (agent frontmatter, hooks.json, skill files, known beads memories).

### Key Discoveries

#### New Capabilities (Not Yet Adopted)

| # | Feature | Version | Nexus Impact | Evidence |
|---|---------|---------|--------------|----------|
| 1 | `effort` frontmatter for agents | v2.1.78 | 15 agents could set appropriate effort levels. Values: `low`, `medium`, `high`, `max` (Opus 4.6 only) | **Verified** — official docs confirm field, values, and plugin agent support |
| 2 | `maxTurns` frontmatter for agents | v2.1.78 | Prevent runaway haiku agents (orchestrator, knowledge-tidier, pdf-processor) | **Verified** — official docs confirm field and behavior |
| 3 | `disallowedTools` frontmatter for agents | v2.1.78 | Read-only agents (plan-auditor, substantive-critic) should not have Edit/Write. Applied before `tools` allowlist | **Verified** — official docs confirm field and interaction with `tools` |
| 4 | `effort` frontmatter for skills/commands | v2.1.80 | 28 skills could set effort (high for design, low for queries). Same values as agents | **Verified** — official docs confirm field |
| 5 | `PostCompact` hook event | v2.1.76 | Re-inject critical context after compaction. Receives `compact_summary`, `trigger` (manual/auto). No decision control | **Verified** — official docs confirm input fields and limitations |
| 6 | `StopFailure` hook event | v2.1.78 | Observability-only. Matchers: `rate_limit`, `authentication_failed`, `billing_error`, `invalid_request`, `server_error`, `max_output_tokens`, `unknown`. Output/exit codes ignored | **Verified** — official docs confirm read-only nature |
| 7 | `${CLAUDE_PLUGIN_DATA}` variable | v2.1.78 | Resolves to `~/.claude/plugins/data/{id}/`. Survives re-clone. Auto-created on first reference. Deleted on uninstall unless `--keep-data` | **Verified** — official docs confirm persistence behavior |
| 8 | `--bare` flag for scripted `-p` calls | v2.1.81 | Faster automation if nexus spawns claude subprocesses | **Documented** |
| 9 | MCP elicitation support | v2.1.76 | nx MCP server could request structured input for disambiguation | **Documented** |
| 10 | `--channels` MCP push messages | v2.1.80 | Long-running operations could push progress updates | **Documented** — research preview |

#### Bug Fixes Resolving Known Nexus Pain Points

| # | Fix | Version | Nexus Impact | Evidence |
|---|-----|---------|--------------|----------|
| 11 | SessionStart hooks firing twice on `--resume` | v2.1.73 | T1 server double-start, duplicate session files | **Verified** — nexus has SessionStart hook starting T1 server |
| 12 | `--worktree` not loading skills/hooks from worktree dir | v2.1.78 | Worktree agents were missing nx toolkit entirely | **Verified** — relates to beads memory `worktree-bug` |
| 13 | Stale-worktree cleanup deleting resumed agent worktree | v2.1.77 | Active agent worktrees destroyed mid-run | **Verified** — relates to beads memory `worktree-bug` |
| 14 | Worktree isolation: Task tool resume not restoring cwd | v2.1.72 | Background task notifications missing worktree context | **Documented** |
| 15 | Background agent task output hanging indefinitely | v2.1.81 | Agents appearing stuck between polling intervals | **Documented** |
| 16 | `--resume` dropping parallel tool results | v2.1.80 | Lost results in multi-agent workflows | **Documented** |
| 17 | Deferred tools losing input schemas after compaction | v2.1.76 | MCP tools (serena, context7, etc.) uncallable post-compact | **Verified** — nexus relies heavily on deferred MCP tools |
| 18 | Deadlock with many skill file changes | v2.1.73 | `git pull` on nx/ with ~25 skills could freeze Claude Code | **Documented** |
| 19 | `cc log` / `--resume` truncating large sessions with subagents | v2.1.78 | RDR workflows with many subagents lost history at >5MB | **Documented** |
| 20 | Invisible hook attachments inflating message count | v2.1.81 | 6 SessionStart hooks producing hidden context bloat | **Verified** — nexus has 6 SessionStart hooks |
| 21 | JSON hooks injecting no-op messages into model context | v2.1.73 | Wasted context tokens every turn | **Documented** |
| 22 | Background bash processes from subagents not cleaned up | v2.1.73 | Leaked `bd` and `nx` processes accumulating | **Documented** |
| 23 | Prompt cache invalidation in SDK `query()` calls | v2.1.73 | Up to 12x input token cost reduction | **Documented** |
| 24 | Auto-compaction circuit breaker (3 attempts) | v2.1.76 | Prevents infinite compaction loops | **Documented** |

#### Performance Improvements (Passive Benefits)

- Opus 4.6 default max output: 64k tokens, upper bound 128k (v2.1.77) — all `model: opus` agents benefit
- Startup memory reduction: ~80MB on large repos (v2.1.80)
- `--resume` 45% faster for fork-heavy sessions (v2.1.77)
- MCP tool calls collapse into single line (v2.1.81) — cleaner output from nx MCP

#### Critical Discovery: Plugin Agent Frontmatter Restrictions

From official Claude Code docs:

> "For security reasons, plugin subagents do not support the `hooks`, `mcpServers`, or `permissionMode` frontmatter fields. These fields are ignored when loading agents from a plugin."

All 15 nexus agents are plugin-shipped (`nx/agents/`). This means:
- `hooks` in agent frontmatter: **silently ignored** — cannot add per-agent validation hooks
- `mcpServers` in agent frontmatter: **silently ignored** — agents inherit parent MCP connections
- `permissionMode` in agent frontmatter: **silently ignored** — cannot enforce read-only via mode

However, `effort`, `maxTurns`, `disallowedTools`, `tools`, `skills`, `memory`, `background`, and `isolation` **are all supported** for plugin agents per the official docs. The RDR-039 Phase 1 plan uses only supported fields.

#### Critical Discovery: `disallowedTools` Has Known Bugs with MCP Tools

**Painful history**: RDR-023 added `tools:` (allowlist) to all agents. RDR-035 emergency-removed it because the `tools:` field in plugin agents causes MCP tool filtering — a confirmed Claude Code bug (GitHub #13605, #21560, #25200) that broke ALL agent MCP access. The PermissionRequest hook is the only proven enforcement layer.

Now RDR-039 proposes adding `disallowedTools` (denylist). Relevant bug reports:
- **GitHub #12863**: `--disallowedTools` CLI flag correctly blocks built-in tools (Edit, Write, Bash) but does NOT block MCP tools. Closed "Not Planned."
- **GitHub #20617**: `allowedTools`/`disallowedTools` in `.mcp.json` completely ignored. Closed as duplicate.

**Why this is probably safe for our use case but needs a spike**:
1. We only propose blocking Edit and Write (built-in tools). `disallowedTools` correctly blocks built-in tools per #12863.
2. We are NOT trying to block MCP tools — MCP writes (memory_put, store_put, scratch) must remain available for agent completion protocols.
3. `disallowedTools` is a subtract-from-inherited mechanism, NOT a filter-to-allowlist mechanism like `tools:`. The MCP filtering bug was in the allowlist filter, not in subtraction.
4. **BUT**: Neither bug report tests `disallowedTools` in plugin agent frontmatter. The RDR-035 trauma warrants verification before deployment.

**Fallback if `disallowedTools` breaks or is silently ignored**: Modify the PermissionRequest hook to be agent-type-aware. The hook input includes `agent_type` — we can deny Edit/Write specifically for plan-auditor, substantive-critic, and codebase-deep-analyzer without affecting other agents.

### Critical Assumptions

- [x] Agent `effort`/`maxTurns` frontmatter is supported in plugin-shipped agents — **Status**: Verified — **Method**: Source (official docs explicitly list supported fields). These fields have NO tool filtering involvement — safe to add without spike.
- [ ] Agent `disallowedTools` frontmatter works in plugin agents without breaking MCP tool access — **Status**: Unverified — **Method**: Needs Spike. The `tools:` allowlist field had a confirmed MCP filtering bug in plugin agents (RDR-035). `disallowedTools` is a different mechanism (denylist vs allowlist) but is untested in this context. Spike: add `disallowedTools: Edit, Write` to one agent, verify (a) Edit/Write actually blocked, (b) MCP tools still work.
- [x] `PostCompact` hook fires after compaction with access to `compact_summary` — **Status**: Verified — **Method**: Source (official docs show input fields including `compact_summary`)
- [x] `StopFailure` hook is observability-only (output/exit codes ignored) — **Status**: Verified — **Method**: Source (official docs explicitly state read-only)
- [x] `${CLAUDE_PLUGIN_DATA}` persists across plugin re-clone — **Status**: Verified — **Method**: Source (official docs: "persistent directory for plugin state that survives updates", resolves to `~/.claude/plugins/data/{id}/`)
- [ ] Worktree fixes (v2.1.73/77/78) collectively resolve the known worktree bug — **Status**: Unverified — **Method**: Needs Spike (test parallel worktree agents end-to-end)

## Proposed Solution

### Approach

Organize into four independently deployable phases by category. Each phase can be shipped independently. Note: Phase 2 (PostCompact hook) should be validated after Phase 1 (maxTurns) is applied, since maxTurns may interact with compaction timing — an agent near its turn limit when compaction fires receives PostCompact output with limited turns remaining.

### Technical Design

#### Phase 1: Agent & Skill Frontmatter Enrichment

Add `effort`, `maxTurns`, and `disallowedTools` to agent and skill frontmatter where beneficial.

**Agent effort mapping:**

| Agent | Model | Effort | maxTurns | disallowedTools |
|-------|-------|--------|----------|-----------------|
| orchestrator | sonnet (upgraded from haiku — routing ambiguous requests needs reasoning depth) | medium | — | — |
| knowledge-tidier | haiku | medium | 20 (conservative starting point; tune after observation) | — |
| pdf-chromadb-processor | haiku | low | 30 (conservative — PDF processing involves multiple chunk/embed cycles; tune after observation) | — |
| code-review-expert | sonnet | high | — | — |
| plan-auditor | sonnet | high | — | Edit, Write (filesystem only — MCP write tools like `memory_put`, `store_put`, `scratch` must remain available for the agent's completion protocol) |
| substantive-critic | sonnet | high | — | Edit, Write (filesystem only — same rationale as plan-auditor) |
| plan-enricher | sonnet | medium | — | — |
| codebase-deep-analyzer | sonnet | medium | — | Edit, Write (filesystem only — analysis agent, should not modify source) |
| deep-research-synthesizer | sonnet | medium | — | — |
| developer | sonnet | high | — | — |
| test-validator | sonnet | high | — | — |
| architect-planner | opus | high | — | — |
| debugger | opus | high | — | — |
| deep-analyst | opus | high | — | — |
| strategic-planner | opus | high | — | — |

**Skill effort mapping (representative):**

| Category | Skills | Effort |
|----------|--------|--------|
| Design/analysis | brainstorming-gate, architecture, strategic-planning, deep-analysis | high |
| Process/review | code-review, plan-validation, substantive-critique, test-validation | high |
| Query/display | rdr-list, rdr-show, nexus, orchestration | low |
| Standard workflow | development, debugging, research-synthesis, all RDR lifecycle | medium |

#### Phase 2: New Hook Events

Add `PostCompact` and `StopFailure` hooks to `nx/hooks/hooks.json`.

**PostCompact hook:** Re-inject critical session context after compaction. Uses the same pattern as the existing `bd prime` in PreCompact — calls external commands to query live state, not relying on the `compact_summary` to know what was lost.
```
PostCompact → run script that:
  - Calls `bd list --status=in_progress --limit=5` for active bead IDs
  - Reads session file from ~/.config/nexus/sessions/ for T1 scratch pointer
  - Runs `nx hook session-start` (reuses existing hook) for MCP health
  Output kept under 20 lines to avoid context bloat.
  NOTE: Overlaps with SubagentStart hook payload — but PostCompact fires
  in the MAIN session after compaction, while SubagentStart fires when
  spawning a new subagent. Different contexts, same data need.
```

**StopFailure hook:** Observability-only (output/exit codes ignored by Claude Code). Used for logging, not recovery.
```
StopFailure → run script that:
  - Logs failure type and details to `bd remember`
  - Optionally creates a blocker bead for rate_limit failures
  - Syncs any in-progress bead state via `bd dolt push`
```

#### Phase 3: Worktree Re-evaluation

Three upstream fixes (v2.1.73, v2.1.77, v2.1.78) collectively address the known worktree isolation bug recorded in beads memory. This phase:

1. Tests parallel worktree agent dispatching end-to-end
2. Verifies skills/hooks load correctly in worktree context
3. Updates or removes the beads memory `worktree-bug` entry based on results

#### Phase 4: Plugin Data Migration & Future Capabilities

- Evaluate `${CLAUDE_PLUGIN_DATA}` for any nexus state that should survive plugin updates
- Assess MCP elicitation for nx server interactive disambiguation
- Monitor `--channels` research preview for progress push opportunities

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| PostCompact hook | `nx/hooks/hooks.json` (has PreCompact) | Extend — add PostCompact entry |
| Agent frontmatter | `nx/agents/*.md` (15 files) | Extend — add new fields |
| Skill frontmatter | `nx/skills/*/SKILL.md` (28 files) | Extend — add effort field |
| Plugin data dir | None | New — adopt `${CLAUDE_PLUGIN_DATA}` if spike confirms persistence |

### Decision Rationale

Organizing as four independent phases allows incremental adoption. Phase 1 (frontmatter) is highest value with lowest risk — purely declarative changes. Phase 2 (hooks) adds resilience. Phase 3 (worktree) validates upstream fixes. Phase 4 (future) defers speculative work.

## Alternatives Considered

### Alternative 1: Single Monolithic PR

**Description**: Implement all changes in one PR.

**Cons**:
- Hard to review, hard to bisect if something breaks
- Worktree testing may take time, blocking frontmatter improvements

**Reason for rejection**: Independent phases ship value faster with less risk.

### Briefly Rejected

- **Do nothing**: Leaves token waste (missing effort levels), context loss (no PostCompact), and stale workaround memories in place.
- **Wait for more releases**: The current gap is already actionable; waiting gains nothing.

## Trade-offs

### Consequences

- Positive: Better token efficiency from effort levels, fewer runaway agents from maxTurns, safer read-only agents from disallowedTools
- Positive: Context recovery after compaction prevents mid-session confusion
- Positive: Removing stale worktree bug workarounds if fixes confirmed
- Negative: Frontmatter values are heuristic — may need tuning after observation

### Risks and Mitigations

- **Risk**: Wrong effort levels degrade agent quality
  **Mitigation**: Start conservative (medium default), tune based on observation
- **Risk**: PostCompact hook output too large, wasting context
  **Mitigation**: Keep output under 20 lines, summary format only
- **Risk**: `disallowedTools` in plugin agent frontmatter triggers MCP tool filtering (repeat of RDR-035 trauma)
  **Mitigation**: Spike on single agent first. Explicit fallback plan: modify PermissionRequest hook to be agent-type-aware if frontmatter approach fails. `effort` and `maxTurns` are deployed first as they have zero tool filtering risk.
- **Risk**: `${CLAUDE_PLUGIN_DATA}` behavior differs from documentation
  **Mitigation**: Verified via official docs; spike only if actual state storage is needed

### Failure Modes

- Bad `maxTurns` on an agent causes premature termination → visible as incomplete output, easy to diagnose and raise the limit
- PostCompact hook fails → session continues without re-injected context, same as current behavior (no regression)
- Worktree test reveals fixes are incomplete → update beads memory with new findings, no code change needed

## Implementation Plan

### Prerequisites

- [x] Claude Code v2.1.81 or later installed
- [x] `${CLAUDE_PLUGIN_DATA}` persistence verified via official docs (resolves to `~/.claude/plugins/data/{id}/`)
- [ ] Verify worktree fixes end-to-end (Phase 3 only)

### Minimum Viable Validation

Phase 1 frontmatter changes applied → verify agents respect `effort`, `maxTurns`, `disallowedTools` by dispatching each modified agent type once.

### Phase 1: Agent & Skill Frontmatter Enrichment

Split into two sub-phases due to `disallowedTools` risk (see Critical Assumptions).

#### Step 1a: Add `effort` and `maxTurns` to all 15 agents (safe — no spike needed)

These fields have no tool filtering involvement. Add per the mapping table above.

#### Step 1b: Add `effort` frontmatter to all 28 skills

Add `effort:` to skill YAML frontmatter per the category mapping.

#### Step 1c: Validate effort and maxTurns

Dispatch representative agents and skills, confirm frontmatter is respected (check turn counts, effort depth).

#### Step 2a: Spike `disallowedTools` on one plugin agent

Add `disallowedTools: Edit, Write` to plan-auditor only. Verify:
- (a) Agent cannot call Edit or Write
- (b) MCP tools (memory_put, store_put, scratch, search) still work
- (c) No cascading denial pattern (per RDR-035 Finding 5)

If spike PASSES: apply `disallowedTools` to remaining read-only agents (substantive-critic, codebase-deep-analyzer).

If spike FAILS: implement fallback — modify PermissionRequest hook to check `agent_type` and deny Edit/Write for specific agents. The hook input JSON includes `agent_type` field.

#### Step 2b: Validate disallowedTools

Dispatch plan-auditor with a task requiring file analysis, confirm it uses Read/Grep/Glob/MCP tools but cannot call Edit or Write.

### Phase 2: New Hook Events

#### Step 1: Create PostCompact hook script

Write `nx/hooks/scripts/post_compact_hook.sh` that outputs active bead IDs, T1 session pointer, and MCP health.

#### Step 2: Create StopFailure hook script

Write `nx/hooks/scripts/stop_failure_hook.py` that logs failure context to beads memory.

#### Step 3: Register hooks in hooks.json

Add `PostCompact` and `StopFailure` entries to `nx/hooks/hooks.json`.

### Phase 3: Worktree Re-evaluation

#### Step 1: Test worktree agent dispatching

Dispatch parallel agents with `isolation: "worktree"`, verify skills and hooks load correctly in worktree context.

#### Step 2: Update beads memory

Based on test results, update or remove the `worktree-bug` beads memory entry.

### Phase 4: Plugin Data & Future Capabilities

#### Step 1: Spike `${CLAUDE_PLUGIN_DATA}`

Test that the directory survives a plugin re-clone cycle.

#### Step 2: Evaluate MCP elicitation and channels

Assess whether nx MCP server should adopt elicitation for interactive disambiguation. Monitor `--channels` maturity.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
|---|---|---|---|---|---|
| Agent frontmatter fields | N/A | `head -10 nx/agents/*.md` | N/A | Dispatch agent, check behavior | Git |
| Hook scripts | `cat nx/hooks/hooks.json` | Read script | Remove entry | Trigger event, check output | Git |
| Beads memory entries | `bd memories worktree` | `bd memories` | `bd forget <key>` | Check accuracy | Dolt |

### New Dependencies

None. All changes use existing Claude Code plugin infrastructure.

## Test Plan

- **Scenario**: Dispatch `orchestrator` (sonnet) with a multi-stage routing task → **Agent**: orchestrator → **Input**: "Route this to the appropriate agent: analyze the indexer module" → **Pass**: Agent correctly identifies and routes to codebase-deep-analyzer → **Fail**: Agent misroutes or fails to produce a relay
- **Scenario**: Dispatch `plan-auditor` (sonnet, disallowedTools: Edit, Write) with a plan that has obvious issues → **Agent**: plan-auditor → **Input**: Plan text with a known gap → **Pass**: Agent produces critique using Read/Grep/Glob/MCP tools, cannot call Edit or Write; MCP write tools (memory_put, store_put) still work → **Fail**: Agent errors on MCP write tools, or successfully calls Edit/Write
- **Scenario**: Dispatch `pdf-chromadb-processor` (haiku, effort: low) vs `architect-planner` (opus, effort: high) → **Agent**: both → **Input**: Simple PDF index task, complex architecture task → **Pass**: Haiku agent produces concise output (low effort), Opus agent produces deep reasoning (high effort); observable difference in thinking depth → **Fail**: No discernible difference in output quality/depth
- **Scenario**: Run `/compact` in a session with active beads and PostCompact hook installed → **Agent**: main session → **Input**: `/compact` command → **Pass**: PostCompact hook fires, output contains `bd list` results showing active bead IDs + T1 session pointer in ≤20 lines → **Fail**: No hook output, or output exceeds 20 lines
- **Scenario**: Observe next API rate limit (429) with StopFailure hook installed → **Agent**: main session → **Input**: Natural rate limit during heavy usage → **Pass**: `bd memories` shows a new entry with failure type and timestamp → **Fail**: No memory entry created (note: output is ignored by Claude Code, so this tests the script's side effects only)
- **Scenario**: Dispatch two agents with `isolation: "worktree"` in parallel → **Agent**: developer + test-validator → **Input**: Two independent tasks in different modules → **Pass**: Both agents load nx skills/hooks in their worktrees; both produce results; no cross-contamination of file changes → **Fail**: Missing skills/hooks in worktree, or agents write to main worktree instead of isolated one

## Validation

### Testing Strategy

1. **Phase 1**: Dispatch one agent per effort tier (haiku/low, sonnet/high, opus/high) and one read-only agent. Confirm effort and tool restrictions.
2. **Phase 2**: Trigger compaction manually (`/compact`), verify PostCompact output. Simulate StopFailure by observing next rate limit hit.
3. **Phase 3**: Run `superpowers:dispatching-parallel-agents` with `isolation: "worktree"` on two independent tasks.
4. **Phase 4**: `plugin install` cycle, check `${CLAUDE_PLUGIN_DATA}` contents.

### Performance Expectations

Phase 1 effort levels should reduce token usage for low-effort skills (rdr-list, rdr-show) and improve output quality for high-effort agents (architect-planner, debugger). No benchmarks — observe qualitatively.

## Finalization Gate

### Contradiction Check

Checked: (1) research finding #5 (PostCompact has no decision control) vs Phase 2 design (uses it for context injection, not decision control — consistent). (2) Research finding critical discovery (plugin agents ignore `permissionMode`) vs Phase 1 design (uses `disallowedTools` not `permissionMode` — consistent). (3) Research finding #6 (StopFailure output/exit codes ignored) vs Phase 2 StopFailure design (uses side effects via `bd remember`, not output — consistent).

No contradictions found.

### Assumption Verification

4 of 5 assumptions verified via official documentation source reading. 1 remains unverified:
- Worktree fixes completeness → Phase 3 prerequisite (spike required)

`${CLAUDE_PLUGIN_DATA}` persistence was verified via official docs (research-004) and is no longer a blocking assumption.

### Scope Verification

Minimum viable validation (Phase 1 agent dispatch test) is in scope and will be the first thing executed.

### Cross-Cutting Concerns

- **Versioning**: N/A — plugin frontmatter changes, no API versioning
- **Build tool compatibility**: N/A — markdown files only
- **Licensing**: N/A — no new dependencies
- **Deployment model**: Plugin updates via git push
- **IDE compatibility**: N/A
- **Incremental adoption**: Each phase ships independently
- **Secret/credential lifecycle**: N/A
- **Memory management**: PostCompact hook output kept under 20 lines to avoid context bloat

### Proportionality

This RDR is intentionally broad — it catalogs a framework alignment gap across 10 releases. The four phases are thin; most are frontmatter edits and short hook scripts. The RDR's value is in the organized inventory, not deep design.

## References

- Claude Code CHANGELOG.md: v2.1.72–v2.1.81 (March 10–20, 2026)
- Claude Code GitHub releases: https://github.com/anthropics/claude-code/releases
- Claude Code official docs — Subagents: https://code.claude.com/docs/en/sub-agents (frontmatter fields, plugin agent restrictions)
- Claude Code official docs — Skills: https://code.claude.com/docs/en/skills (skill frontmatter fields, effort levels)
- Claude Code official docs — Hooks: https://code.claude.com/docs/en/hooks (PostCompact, StopFailure, all event types)
- Claude Code official docs — Plugins Reference: https://code.claude.com/docs/en/plugins-reference (`${CLAUDE_PLUGIN_DATA}`, plugin variables)
- Nexus plugin: `nx/` directory (agents, skills, hooks, commands)
- RDR-023: Agent Tool Permissions Audit — introduced `tools:` frontmatter and PermissionRequest hook
- RDR-035: Fix Plugin Agent MCP Tool Access — removed `tools:` due to MCP filtering bug (GitHub #13605, #21560, #25200)
- GitHub #12863: `--disallowedTools` flag does not affect MCP server tools (built-in tools correctly blocked)
- GitHub #20617: `allowedTools`/`disallowedTools` in `.mcp.json` ignored (duplicate of #12863)
- Beads memory: `worktree-bug-when-using-isolation-worktree-with-agent`
- T2 research entries: `039-research-001` through `039-research-006` in `nexus_rdr` project

## Revision History

- 2026-03-21: Initial draft from changelog analysis of v2.1.72–v2.1.81
- 2026-03-21: Research verification — upgraded 7 items from Documented to Verified via official docs. Discovered plugin agent frontmatter restriction (hooks/mcpServers/permissionMode silently ignored). Confirmed `${CLAUDE_PLUGIN_DATA}` persistence. 5 T2 research entries recorded.
- 2026-03-21: Gate round 1 — substantive-critic identified 2 critical, 4 significant, 5 observations. Fixed: (1) corrected hook inventory to include PermissionRequest event, (2) enumerated explicit `disallowedTools` lists (filesystem-only: Edit, Write — MCP write tools preserved for completion protocols), (3) corrected skill count from ~25 to 28, (4) changed "independent phases" to "independently deployable" with Phase 1→2 interaction noted, (5) raised maxTurns values with "conservative starting point, tune after observation" framing, (6) made PostCompact hook design explicit (calls external commands like `bd prime` pattern, not relying on compact_summary), (7) expanded test plan with concrete agents, inputs, pass/fail criteria, (8) substantiated Finalization Gate sections with specific checks performed.
- 2026-03-21: Post-gate deep dive — investigated RDR-023/035 `tools:` trauma history and `disallowedTools` bug reports (GitHub #12863, #20617). Found: `disallowedTools` correctly blocks built-in tools but NOT MCP tools; untested in plugin agent frontmatter. Split Phase 1 into sub-phases: effort/maxTurns (safe, no spike) vs disallowedTools (needs spike + fallback via agent-type-aware PermissionRequest hook). Added research entry 039-research-006.
