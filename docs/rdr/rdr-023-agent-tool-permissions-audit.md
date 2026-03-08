---
id: RDR-023
title: "Agent Tool Permissions Audit and Remediation"
type: enhancement
status: closed
priority: P1
created: 2026-03-07
accepted_date: 2026-03-07
closed_date: 2026-03-07
close_reason: implemented
reviewed_by: self
---

# RDR-023: Agent Tool Permissions Audit and Remediation

## Problem Statement

The knowledge-tidier agent failed in production when delegated a T3 knowledge
store update task. It was denied access to `Bash` (needed for `nx` CLI commands),
`Write`, and MCP tools (`mcp__mixedbread__*`, `mcp__plugin_serena_serena__*`).
The task had to be completed manually from the main conversation.

**Root cause**: None of the 14 nx agents specify the `tools` frontmatter field.
When `tools` is omitted, Claude Code grants agents access to "all tools" in
theory, but in practice the user's permission mode may require interactive
approval for each tool — which subagents cannot request. The result is silent
denial.

This is not unique to knowledge-tidier. Any agent that needs `Bash` (for `nx`
CLI), `Write` (for file output), or MCP tools (for Serena, mixedbread) will
hit the same issue.

## Scope

1. **Audit all 14 nx agents** for their actual tool requirements
2. **Add explicit `tools` frontmatter** to each agent, following the principle
   of least privilege
3. **Expand PermissionRequest hook** to auto-approve safe tools for subagents
4. **Verify** that agents with explicit tools can execute their core workflows

## Research Findings

### Finding 1: No agents have `tools` frontmatter

Confirmed via `grep -c '^tools:' nx/agents/*.md` — zero matches across all 14
agent files. All agents share the same frontmatter structure: name, version,
description, model, color.

### Finding 2: PermissionRequest hook only handles Bash

The existing `permission-request-stdin.sh` hook only checks `$TOOL == "Bash"`
with allow/deny rules for specific commands. For any non-Bash tool (Read, Write,
Edit, WebSearch, etc.), the hook falls through to "ask user" — which means silent
denial for subagents. **The hook is the actual enforcement layer** — it determines
what subagents can use at runtime regardless of other configuration.

### Finding 3: Sequential thinking is pervasive

12 of 14 agents reference `mcp__plugin_nx_sequential-thinking__sequentialthinking`
in their system prompts. This is a reasoning primitive with no side effects — it
should not be restricted.

### Finding 4: Hook JSON schema is validated

The hook uses `.tool` and `.command` field names. These are confirmed working in
production (commit 39f9c02 added nx auto-approval using these fields). The stale
"TBD" comment in the hook was misleading.

## Alternatives Considered

**Approach A — tools frontmatter + expanded PermissionRequest hook only**: Add
`tools` to each agent and expand the hook to auto-approve tools per category.
Rejected because it requires maintaining two sources of truth with no proven
interaction between them.

**Approach B — tools frontmatter + `mode` parameter in skill invocations**: Have
each skill pass `mode: "bypassPermissions"` when spawning agents, with the tools
list as the security boundary. Rejected because skills must remember to set mode,
and it grants blanket access within the listed tools without hook-level filtering.

**Approach C (chosen) — Hybrid defense-in-depth**: Add tools frontmatter (intent
documentation + possible enforcement) AND expand the hook (guaranteed enforcement).
The hook is the known-working layer; tools frontmatter provides documentation and
may also enforce — but the design does not depend on that assumption.

## Decision: Hybrid Defense-in-Depth (Approach C)

Two independent layers:

1. **`tools` frontmatter** — documents what each agent *should* use and may
   restrict tool availability at the Claude Code runtime level (unverified —
   see Q2 below)
2. **PermissionRequest hook expansion** — the guaranteed enforcement layer that
   ensures agents *can* use their declared tools without silent denial

### Tool Assignments

> **Note**: "sequential-thinking" in the table below abbreviates the full tool
> identifier `mcp__plugin_nx_sequential-thinking__sequentialthinking`. The
> design doc and impl-plan contain the full identifiers as deployed to agent
> frontmatter.

| Category | Tools | Agents |
|----------|-------|--------|
| Read-only | Read, Grep, Glob, sequential-thinking | plan-auditor, substantive-critic |
| Read + Bash | Read, Grep, Glob, Bash, sequential-thinking | code-review-expert, codebase-deep-analyzer, deep-analyst, test-validator, java-debugger, knowledge-tidier, pdf-chromadb-processor |
| Read + Bash + Web | Read, Grep, Glob, Bash, WebSearch, WebFetch, sequential-thinking | deep-research-synthesizer |
| Read + Write + Bash | Read, Write, Edit, Grep, Glob, Bash, sequential-thinking | strategic-planner, java-developer, java-architect-planner |
| Orchestrator | Read, Grep, Glob, Agent, sequential-thinking | orchestrator |

### Hook Expansion

| Tool | Action | Rationale |
|------|--------|-----------|
| Read, Grep, Glob | Always allow | Read-only local tools, always safe |
| Write, Edit | Always allow | Local file ops; if `tools` frontmatter does NOT enforce at runtime, any agent can write files via the hook — acceptable risk given existing deny rules on destructive Bash commands |
| WebSearch, WebFetch | Always allow | Read-only external, no mutations |
| Agent | Always allow | Orchestrator delegation |
| Sequential thinking | Always allow | Reasoning primitive, no side effects |
| Bash (expanded allowlist) | Allow specific commands | git read, uv run pytest, bd management (create/update/close/dep/remember/memories/sync/stats/doctor), nx CLI |
| Bash (destructive) | Deny | git push --force, git reset --hard, git clean -f, bd delete, bd sync --force, nx collection delete, ./mvnw deploy |
| MCP tools (mixedbread, serena) | Ask user | Not auto-approved; agents should use `nx` CLI via Bash instead. If an agent legitimately needs an MCP tool with no `nx` equivalent, its tool list and the hook must both be updated. |

## Resolved Questions

**Q1** (tools: ["*"] vs omitting): Explicit per-agent tool lists are better than
either option. They document intent and may enforce least privilege at runtime.

**Q2** (does `tools` control access or just system prompt?): **Unverified.** We
believe `tools` frontmatter restricts which tools are available to the agent, but
no Claude Code documentation explicitly confirms runtime enforcement vs.
system-prompt-only behavior. The hook provides fallback coverage regardless.
Validation (nexus-ryjo) will test whether an agent without Bash in its tools list
can actually execute shell commands — this will confirm the enforcement model.

**Q3** (nx CLI via Bash vs dedicated MCP tools): Agents should use `nx` via Bash.
The PermissionRequest hook already auto-approves `nx` commands. No need for
dedicated MCP wrappers.

**Q4** (sequential thinking): Added to all 14 agents uniformly. It's a reasoning
primitive with no side effects — no security reason to restrict it.

## Success Criteria

Pre-conditions (verified before gate):

- [x] All 14 agents have explicit `tools` in frontmatter (verified: `grep '^tools:' nx/agents/*.md` — 14 matches)
- [x] PermissionRequest hook auto-approves safe tools (verified: JSON payload tests — all allow/deny/ask-user scenarios pass)
- [x] Existing deny rules preserved (verified: destructive commands still denied in hook tests)

Post-conditions (require validation bead nexus-ryjo):

- [ ] knowledge-tidier can successfully run `nx store put` and `nx memory` commands
- [ ] Agents that were previously failing due to permission denials now work
- [ ] Confirm whether `tools` frontmatter enforces access at runtime (negative test: agent without Bash attempts shell command)

## Implementation

- **Design**: `docs/plans/2026-03-07-rdr-023-agent-tool-permissions-design.md`
- **Plan**: `docs/plans/2026-03-07-rdr-023-agent-tool-permissions-impl-plan.md`
- **PR**: #74
- **Epic**: nexus-qic4
