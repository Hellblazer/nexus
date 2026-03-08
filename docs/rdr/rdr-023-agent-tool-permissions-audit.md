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
3. **Verify** that agents with explicit tools can execute their core workflows

## Current Agent Inventory

| Agent | Model | Primary Tools Needed |
|---|---|---|
| knowledge-tidier | haiku | Bash (nx CLI), Read, Grep, Glob |
| orchestrator | haiku | Read, Grep, Glob, Agent |
| code-review-expert | sonnet | Read, Grep, Glob, Bash (git) |
| plan-auditor | sonnet | Read, Grep, Glob |
| strategic-planner | opus | Read, Grep, Glob, Bash (bd), Write |
| substantive-critic | sonnet | Read, Grep, Glob |
| codebase-deep-analyzer | sonnet | Read, Grep, Glob, Bash (git log) |
| deep-analyst | opus | Read, Grep, Glob, Bash |
| deep-research-synthesizer | sonnet | Read, Grep, Glob, Bash (nx CLI), WebSearch, WebFetch |
| test-validator | sonnet | Read, Grep, Glob, Bash (test runners) |
| java-developer | sonnet | Read, Write, Edit, Grep, Glob, Bash |
| java-debugger | opus | Read, Grep, Glob, Bash |
| java-architect-planner | opus | Read, Grep, Glob, Write |
| pdf-chromadb-processor | haiku | Read, Bash (nx CLI, pdf tools) |

## Proposed Tool Assignments

### Read-only agents (analysis, review, critique)

```yaml
tools: ["Read", "Grep", "Glob"]
```

Agents: plan-auditor, substantive-critic

### Read + Bash agents (need CLI tools)

```yaml
tools: ["Read", "Grep", "Glob", "Bash"]
```

Agents: code-review-expert (git), codebase-deep-analyzer (git),
deep-analyst, test-validator, java-debugger

### Read + Bash + nx CLI agents (need nx store/memory/search)

```yaml
tools: ["Read", "Grep", "Glob", "Bash"]
```

Agents: knowledge-tidier (nx CLI), deep-research-synthesizer (nx CLI),
pdf-chromadb-processor (nx CLI)

Note: `nx` commands run via Bash. No separate MCP tool needed if
the agent uses `nx` CLI rather than direct MCP calls.

### Read + Write agents (produce files)

```yaml
tools: ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
```

Agents: strategic-planner, java-developer, java-architect-planner

### Orchestrator (delegates to other agents)

```yaml
tools: ["Read", "Grep", "Glob", "Agent"]
```

Agent: orchestrator

## MCP Tool Consideration

Some agents attempt to use MCP tools directly (e.g., `mcp__mixedbread__search_store`,
`mcp__plugin_serena_serena__find_symbol`). These should be evaluated case by case:

- **Serena tools**: Used by code navigation agents. If an agent needs symbol-level
  code navigation, add the relevant `mcp__plugin_serena_serena__*` tools.
- **Mixedbread tools**: Used for T3 semantic search. Most agents should use
  `nx search` via Bash instead (simpler, fewer permissions needed).
- **Sequential thinking**: Used by deep-analyst and plan-auditor. Add
  `mcp__plugin_nx_sequential-thinking__sequentialthinking` where needed.

## Implementation Plan

1. For each agent, review its system prompt for tool references
2. Determine minimum tool set from actual usage patterns
3. Add `tools` field to frontmatter
4. Test each agent with a representative task
5. Document any agents that need MCP tools explicitly

## Open Questions

**Q1**: Should we use `tools: ["*"]` (explicit all-access) vs omitting `tools`
(implicit all-access)? The behavior may differ under different permission modes.

**Q2**: Does the `tools` field in agent frontmatter actually control which tools
the agent can call, or does it only affect which tools are listed in the agent's
system prompt? If the latter, explicit `tools` may not solve the permission
denial issue — the fix would need to be in the Agent tool's `mode` parameter
(e.g., `mode: "bypassPermissions"`).

**Q3**: Should agents that need `nx` CLI commands use Bash directly, or should
there be dedicated MCP tools wrapping `nx` subcommands (avoiding the Bash
permission issue entirely)?

## Success Criteria

- [ ] All 14 agents have explicit `tools` in frontmatter
- [ ] knowledge-tidier can successfully run `nx store put` and `nx memory` commands
- [ ] No agent has broader tool access than its task requires
- [ ] Agents that were previously failing due to permission denials now work
