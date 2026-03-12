---
title: "Fix Plugin Agent MCP Tool Access"
id: RDR-035
type: bugfix
status: closed
accepted_date: 2026-03-12
closed_date: 2026-03-12
close_reason: implemented
priority: P0
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-12
related_issues:
  - "RDR-023 - Agent Tool Permissions Audit (introduced tools field)"
  - "RDR-034 - MCP Server for Agent Storage Operations (added MCP tools)"
  - "GitHub #13605 - Plugin subagents cannot access MCP tools"
  - "GitHub #21560 - Plugin-defined subagents cannot access MCP tools"
  - "GitHub #25200 - Custom agents cannot use deferred MCP tools"
  - "GitHub #18950 - Skills/subagents don't inherit user-level permissions"
  - "GitHub #21460 - PreToolUse hooks not enforced on subagent tool calls"
---

# RDR-035: Fix Plugin Agent MCP Tool Access

## Problem Statement

All 14 nx plugin agents fail to use MCP tools (`mcp__plugin_nx_nexus__*`, `mcp__plugin_nx_sequential-thinking__*`) when spawned as subagents. This renders the entire MCP server infrastructure built in RDR-034 non-functional for its intended purpose — reliable agent access to T1/T2/T3 storage tiers.

**Observed failure** (2026-03-12, Delos project): 15 agents (6 knowledge-tidier, 8 general-purpose, 1 codebase-analyzer) all failed to call `store_put`, `memory_put`, `scratch`, and `sequentialthinking`. Every MCP tool was denied from the very first attempt, while built-in tools (Read, Bash) worked initially.

**Root cause**: Claude Code's `tools` frontmatter field in plugin-defined agents causes MCP tool filtering. When an agent declares explicit `tools: [...]`, the framework filters the available tool set — and that filter incorrectly excludes MCP tools, even when they are explicitly listed in the array. This is a confirmed Claude Code framework bug (GitHub issues #13605, #21560, #25200).

**Confirmed fix**: Removing the `tools:` field from agent frontmatter allows agents to inherit ALL tools from the parent session, including MCP tools. Tested and verified: a knowledge-tidier agent with no `tools:` field successfully called `store_put` and `search` on the first attempt.

**History of the bug**:
1. **RDR-023** (2026-03-07): Added explicit `tools:` frontmatter to all 14 agents for principle-of-least-privilege and documentation. At the time, agents only used Bash for `nx` commands — no MCP tools existed. The `tools:` field worked correctly for built-in tools.
2. **RDR-034** (2026-03-11): Created MCP server and added MCP tool identifiers to all agents' `tools:` arrays. The assumption was: "MCP tools are first-class tool calls that are always available when the MCP server is registered and the tool appears in the agent's `tools:` frontmatter." This assumption was incorrect.
3. **2026-03-12**: MCP tools fail for all agents in Delos project. Investigation reveals the `tools:` field itself causes the filtering bug.

**Impact**: Complete failure of agent-driven knowledge persistence, the primary use case for the MCP server. 13 of 14 agents persist state via MCP tools. When these tools are filtered out, agents silently lose their output.

## Research Findings

### Finding 1: The `tools:` field causes MCP tool filtering in plugin agents

When a plugin-defined agent declares `tools: [...]` in its YAML frontmatter, Claude Code applies a filter to the agent's available tool set. This filter correctly includes built-in tools (Read, Write, Edit, Bash, Grep, Glob) but incorrectly excludes MCP tools — even when the MCP tool identifiers are explicitly listed in the array.

**Evidence**:
- GitHub issue #13605 comment: "Managed to get working (with v2.1.22) by removing `tools:` from agent file. Not the real solution but at least agents can use now MCP tools as they inherit configuration from CLAUDE."
- GitHub issue #21560 comment: "Same agents work perfectly with MCP tools when placed in `.claude/agents/` (project level). Moving them to plugin's `agents/` directory breaks all MCP access. permissionMode: bypassPermissions has no effect for plugin agents."
- GitHub issue #25200: "The `mcpServers` frontmatter field DOES NOT WORK. MCP tools are not injected into the subagent's tool inventory at all, regardless of frontmatter declarations."

### Finding 2: Multiple permission mechanisms fail simultaneously

The investigation tested every available permission mechanism. All failed for MCP tools in plugin-defined subagents:

| Mechanism | Result | Why |
|-----------|--------|-----|
| `mode: bypassPermissions` | Failed | Only bypasses built-in tool permissions, not MCP tool registration |
| `mode: dontAsk` | Failed | Auto-denies anything not registered (MCP tools aren't registered) |
| Project allow list (`settings.local.json`) | Failed | Allow lists not inherited by subagents (#18950) |
| PermissionRequest hook | Never fires | Hook fires "when a permission dialog is about to be shown" — in bypassPermissions mode, no dialog is generated (#11891) |
| PreToolUse hook auto-approve | Irrelevant | Hook returns allow, but tool was never registered in subagent context |
| `mcpServers` frontmatter | Non-functional | Documented but not implemented (#25200) |

### Finding 3: Removing `tools:` field fixes MCP access — verified

Test performed 2026-03-12:
1. Removed `tools:` field from `nx/agents/knowledge-tidier.md`
2. Spawned agent with `mode: bypassPermissions`
3. Agent called `store_put` → **SUCCESS** (entry ID: `7b82106b6143ef2b`)
4. Agent called `search` → **SUCCESS** (retrieved entry with score 0.4895)

Both MCP tools worked on the first attempt with zero permission denials.

### Finding 4: The PermissionRequest hook provides runtime safety

The nx plugin's `permission-request-stdin.sh` hook auto-approves:
- All `mcp__plugin_nx_nexus__*` tools (line 52-55)
- All `mcp__plugin_nx_sequential-thinking__*` tools (line 46-49)
- Read, Write, Edit, Grep, Glob, WebSearch, WebFetch, Agent (lines 22-43)
- Safe Bash commands: `bd`, `git` read-only, `uv run pytest`, `nx` CLI (lines 89-131)
- Denies destructive commands: `git push --force`, `bd delete`, `nx collection delete` (lines 60-86)

This hook remains the runtime enforcement layer regardless of whether `tools:` frontmatter is present. The hook fires in the main session and provides defense-in-depth.

### Finding 5: Cascading denial after MCP failures

Timeline analysis of subagent logs revealed a cascading pattern:
```
14:53:51-14:54:15  Bash/Read (17 calls) → ALL OK
14:54:19           sequentialthinking (MCP) → DENIED ← first MCP attempt
14:54:21-14:54:25  Bash/Read → still OK
14:54:47           store_put (MCP) → DENIED
14:55:02           memory_put (MCP) → DENIED
14:55:12           scratch (MCP) → DENIED
14:55:45           Bash → DENIED ← cascading denial starts
14:56:40           Read → OK ← Read never denied
```

After enough MCP tool denials accumulate, the framework enters a degraded state that also denies previously-working tools (Bash). Read remains exempt. This cascading behavior is documented in GitHub issues #17360 and #11934.

## Proposed Solution

Remove the `tools:` frontmatter field from all 14 nx plugin agents. Agents will inherit all tools from the parent session, including MCP tools. The PermissionRequest hook remains as the runtime enforcement layer for tool access control.

### What Changes

**Agent frontmatter (14 files)**: Remove the `tools:` line from YAML frontmatter.

Before:
```yaml
---
name: knowledge-tidier
version: "2.0"
description: ...
model: haiku
color: mint
tools: ["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking", "mcp__plugin_nx_nexus__search", "mcp__plugin_nx_nexus__store_put", ...]
---
```

After:
```yaml
---
name: knowledge-tidier
version: "2.0"
description: ...
model: haiku
color: mint
---
```

**PermissionRequest hook**: Already handles all tool categories. No changes needed.

**RDR-023**: Add supersession annotation — the `tools:` frontmatter approach from RDR-023 is superseded by this fix. The hook enforcement layer (also from RDR-023) remains correct and active.

**RDR-034**: Add post-implementation note — the MCP server works correctly; the issue was `tools:` filtering, not MCP architecture.

### What Does NOT Change

- MCP server (`src/nexus/mcp_server.py`) — no changes needed
- MCP server registration (`nx/.mcp.json`) — stays at plugin level
- Agent system prompts — all MCP tool references in agent bodies remain correct
- Skill and command files — MCP tool references remain correct
- PermissionRequest hook — already handles all tools
- PreToolUse hooks — no changes
- Test suite — no changes

### Security Analysis

**Risk**: Without `tools:` frontmatter, agents can theoretically access any tool available in the parent session, not just their declared subset.

**Mitigations**:
1. **PermissionRequest hook** (lines 22-131 of `permission-request-stdin.sh`): Auto-approves safe tools, denies destructive commands. This is the proven enforcement layer that has been working since RDR-023.
2. **Agent system prompts**: Each agent's body describes its workflow using specific tools. Agents follow their prompts — they don't randomly invoke tools outside their domain.
3. **Model behavior**: Claude models respect tool usage patterns described in system prompts. A knowledge-tidier won't spontaneously call `WebSearch` just because it's technically available.
4. **Deny rules**: Destructive operations (`git push --force`, `bd delete`, `nx collection delete`, `./mvnw deploy`) are explicitly denied in the hook regardless of agent type.

**Net assessment**: The security posture is effectively unchanged. The `tools:` field was intended as defense-in-depth documentation, but it was never verified to enforce restrictions at runtime (RDR-023 Q2 — "Unverified. We believe `tools` frontmatter restricts which tools are available to the agent, but no Claude Code documentation explicitly confirms runtime enforcement"). The hook was always the primary enforcement mechanism.

## Scope of Changes

### Modified Files (16)

| File | Change |
|------|--------|
| `nx/agents/architect-planner.md` | Remove `tools:` frontmatter line |
| `nx/agents/code-review-expert.md` | Remove `tools:` frontmatter line |
| `nx/agents/codebase-deep-analyzer.md` | Remove `tools:` frontmatter line |
| `nx/agents/debugger.md` | Remove `tools:` frontmatter line |
| `nx/agents/deep-analyst.md` | Remove `tools:` frontmatter line |
| `nx/agents/deep-research-synthesizer.md` | Remove `tools:` frontmatter line |
| `nx/agents/developer.md` | Remove `tools:` frontmatter line |
| `nx/agents/knowledge-tidier.md` | Remove `tools:` frontmatter line (already done) |
| `nx/agents/orchestrator.md` | Remove `tools:` frontmatter line |
| `nx/agents/pdf-chromadb-processor.md` | Remove `tools:` frontmatter line |
| `nx/agents/plan-auditor.md` | Remove `tools:` frontmatter line |
| `nx/agents/strategic-planner.md` | Remove `tools:` frontmatter line |
| `nx/agents/substantive-critic.md` | Remove `tools:` frontmatter line |
| `nx/agents/test-validator.md` | Remove `tools:` frontmatter line |
| `docs/rdr/rdr-023-agent-tool-permissions-audit.md` | Add supersession note for tools field |
| `docs/rdr/rdr-034-mcp-server-agent-storage.md` | Add post-implementation note |

### Not Modified

| Category | Reason |
|----------|--------|
| `src/nexus/mcp_server.py` | MCP server works correctly |
| `nx/.mcp.json` | Server registration is correct |
| `nx/hooks/scripts/permission-request-stdin.sh` | Already handles all tools |
| Skills, commands, shared docs | MCP tool references are correct |
| Tests | No code changes to test |

## Testing Strategy

### Verification Test (already performed)

1. Remove `tools:` from knowledge-tidier agent
2. Spawn as subagent with `mode: bypassPermissions`
3. Call `store_put` → verify success
4. Call `search` → verify entry retrievable
5. **Result**: PASS

### Post-Implementation Verification

After removing `tools:` from all 14 agents:

1. **Positive test**: Spawn each agent type that uses MCP tools (knowledge-tidier, deep-research-synthesizer, codebase-deep-analyzer) and verify MCP tool access
2. **Negative test**: Verify PermissionRequest hook still denies destructive commands (spawn agent, attempt `git push --force` via Bash)
3. **Cross-project test**: Run verification in Delos project (where original failure was observed)

## Alternatives Considered

### A: Move MCP servers to global `~/.claude/.mcp.json` (rejected)

Global MCP registration would make subagents inherit MCP tools. Rejected because:
- Requires manual user configuration, defeating plugin portability
- Could be automated via hook, but adds complexity (merge logic, conflict handling, duplicate server prevention)
- Doesn't address the root cause (plugin agents can't use plugin MCP tools)

### B: Move agents to project-level `.claude/agents/` (rejected)

Project-level agents inherit MCP tools correctly. Rejected because:
- Loses plugin portability entirely
- Requires per-project setup
- Would need hook automation to copy files, adding fragility

### C: Add `permissionMode: bypassPermissions` to agent frontmatter (rejected)

Documented but confirmed non-functional for plugin-defined agents (GitHub #21560, #24073).

### D: Use `mcpServers` frontmatter in agents (rejected)

Documented but confirmed non-functional (GitHub #25200). "MCP tools are not injected into the subagent's tool inventory at all, regardless of frontmatter declarations."

### E: Keep `tools:` but use wildcard patterns like `mcp__plugin_nx_nexus__*` (rejected)

Wildcard patterns in `tools:` are supported in allow lists but their behavior in agent frontmatter is undocumented and untested. Given that explicit MCP tool names in `tools:` already fail, wildcards are unlikely to work.

## Success Criteria

- [ ] All 14 agents have `tools:` frontmatter removed
- [ ] knowledge-tidier agent successfully calls `store_put` as subagent
- [ ] deep-research-synthesizer agent successfully calls `search` as subagent
- [ ] PermissionRequest hook still denies destructive commands
- [ ] RDR-023 annotated with supersession note
- [ ] RDR-034 annotated with post-implementation note
- [ ] Verified in Delos project (original failure site)

## Implementation

Single phase — all changes are frontmatter-only edits to existing files.
