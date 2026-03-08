---
id: RDR-023
title: "Agent Tool Permissions Audit and Remediation"
type: enhancement
status: draft
priority: P1
created: 2026-03-07
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
denial for subagents.

### Finding 3: Sequential thinking is pervasive

12 of 14 agents reference `mcp__plugin_nx_sequential-thinking__sequentialthinking`
in their system prompts. This is a reasoning primitive with no side effects — it
should not be restricted.

### Finding 4: Hook JSON schema is validated

The hook uses `.tool` and `.command` field names. These are confirmed working in
production (commit 39f9c02 added nx auto-approval using these fields). The stale
"TBD" comment in the hook was misleading.

## Decision: Hybrid Defense-in-Depth (Approach C)

Two independent layers:

1. **`tools` frontmatter** — defines what each agent *should* use (least privilege)
2. **PermissionRequest hook expansion** — ensures agents *can* use their tools
   without silent denial

### Tool Assignments

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
| Write, Edit | Always allow | Local file ops, controlled by agent tools list |
| WebSearch, WebFetch | Always allow | Read-only external, no mutations |
| Agent | Always allow | Orchestrator delegation |
| Sequential thinking | Always allow | Reasoning primitive, no side effects |
| Bash (expanded allowlist) | Allow specific commands | git read, uv run pytest, bd management, nx CLI |
| Bash (destructive) | Deny | git push --force, bd delete, etc. |
| MCP tools (mixedbread, serena) | Ask user | Not auto-approved; agents use nx CLI instead |

## Resolved Questions

**Q1** (tools: ["*"] vs omitting): Explicit per-agent tool lists are better than
either option. They document intent, enforce least privilege, and serve as the
security boundary.

**Q2** (does `tools` control access or just system prompt?): The `tools` field
controls which tools appear in the agent's system prompt AND which tools are
available. But the PermissionRequest hook is the enforcement layer — both are
needed for defense-in-depth.

**Q3** (nx CLI via Bash vs dedicated MCP tools): Agents should use `nx` via Bash.
The PermissionRequest hook already auto-approves `nx` commands. No need for
dedicated MCP wrappers.

**Q4** (sequential thinking): Added to all 14 agents uniformly. It's a reasoning
primitive with no side effects — no security reason to restrict it.

## Success Criteria

- [x] All 14 agents have explicit `tools` in frontmatter
- [ ] knowledge-tidier can successfully run `nx store put` and `nx memory` commands
- [x] No agent has broader tool access than its task requires
- [ ] Agents that were previously failing due to permission denials now work
- [x] PermissionRequest hook auto-approves safe tools (tested with JSON payloads)
- [x] Existing deny rules preserved (destructive git, bd delete, etc.)

## Implementation

- **Design**: `docs/plans/2026-03-07-rdr-023-agent-tool-permissions-design.md`
- **Plan**: `docs/plans/2026-03-07-rdr-023-agent-tool-permissions-impl-plan.md`
- **PR**: #74
- **Epic**: nexus-qic4
