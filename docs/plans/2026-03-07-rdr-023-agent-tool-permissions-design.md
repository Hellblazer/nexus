# RDR-023: Agent Tool Permissions — Design

**Approved approach: Hybrid (Option C)**

## Summary

Add explicit `tools` frontmatter to all 14 nx agents (least privilege) AND expand the
PermissionRequest hook to auto-approve safe tools for subagents. Defense-in-depth:
the tools list defines what an agent *should* use, the hook ensures it *can* use it.

## Agent Tool Assignments

### Read-only agents (analysis, review, critique)

```yaml
tools: ["Read", "Grep", "Glob"]
```

Agents: plan-auditor, substantive-critic

### Read + Bash agents (need CLI tools)

```yaml
tools: ["Read", "Grep", "Glob", "Bash"]
```

Agents: code-review-expert (git), codebase-deep-analyzer (git log),
deep-analyst, test-validator (test runners), java-debugger

### Read + Bash + Web agents (need nx CLI + web research)

```yaml
tools: ["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]
```

Agents: deep-research-synthesizer

### Read + Bash agents (nx CLI heavy)

```yaml
tools: ["Read", "Grep", "Glob", "Bash"]
```

Agents: knowledge-tidier, pdf-chromadb-processor

### Read + Write + Bash agents (produce files)

```yaml
tools: ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
```

Agents: strategic-planner, java-developer, java-architect-planner

### Orchestrator (delegates to other agents)

```yaml
tools: ["Read", "Grep", "Glob", "Agent"]
```

Agent: orchestrator

## PermissionRequest Hook Expansion

Current hook only handles Bash commands. Expand to:

1. **Always auto-approve** (safe, local-only): `Read`, `Grep`, `Glob`
2. **Auto-approve for subagents**: `Write`, `Edit` (controlled by tools list)
3. **Bash**: Keep existing allowlist pattern, expand with:
   - `git log`, `git diff`, `git status`, `git show`, `git branch`
   - `uv run pytest` (test runners)
   - `bd create`, `bd update`, `bd close`, `bd dep`, `bd remember`, `bd memories`
4. **WebSearch, WebFetch**: Auto-approve (read-only external)
5. **Agent**: Auto-approve (orchestrator needs this)
6. **MCP tools**: NOT auto-approved (agents should use `nx` CLI via Bash instead)

## Sequential Thinking (Audit Finding 1 — Option C)

All 14 agents get `mcp__plugin_nx_sequential-thinking__sequentialthinking` in their tools list.
12 of 14 agents reference it in their system prompts. It's a reasoning primitive with no
side effects — no security reason to restrict it. Adding uniformly is simpler than per-agent.

## Non-Goals

- No changes to agent system prompts (content)
- No changes to how skills invoke agents (no `mode` parameter changes)
- No MCP tool access for agents EXCEPT sequential thinking (reasoning primitive, no side effects)
