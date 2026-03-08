# RDR-023: Agent Tool Permissions — Implementation Plan

**Epic**: nexus-qic4 (RDR-023: Agent Tool Permissions Audit and Remediation)
**Design**: docs/plans/2026-03-07-rdr-023-agent-tool-permissions-design.md
**RDR**: docs/rdr/rdr-023-agent-tool-permissions-audit.md

## Executive Summary

This plan implements defense-in-depth agent tool permissions for all 14 nx agents.
Two independent work streams run in parallel: (1) adding explicit `tools` frontmatter
to each agent file, and (2) expanding the PermissionRequest hook to auto-approve safe
tools for subagents. A validation phase follows to confirm agents can execute their
core workflows.

## Dependency Graph

```
nexus-ngy5 (Agent Frontmatter) ──┐
                                  ├──> nexus-ryjo (Validation) ──> nexus-if5a (Finalize RDR)
nexus-eyih (Hook Expansion) ─────┘
```

**Critical path**: Either frontmatter or hook task (whichever finishes last) -> Validation -> Finalize.
**Parallelization**: nexus-ngy5 and nexus-eyih are fully independent and can run simultaneously.

## Phase 1: Agent Frontmatter (nexus-ngy5)

**Goal**: Add `tools:` YAML frontmatter to all 14 agent `.md` files.

### Execution Instructions

For each agent file in `nx/agents/*.md`, insert a `tools:` line after the existing
frontmatter fields (after `color:`) but before the closing `---`.

#### Tool Assignments

| Agent File | tools Value |
|---|---|
| plan-auditor.md | `["Read", "Grep", "Glob", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| substantive-critic.md | `["Read", "Grep", "Glob", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| code-review-expert.md | `["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| codebase-deep-analyzer.md | `["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| deep-analyst.md | `["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| test-validator.md | `["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| java-debugger.md | `["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| knowledge-tidier.md | `["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| pdf-chromadb-processor.md | `["Read", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| deep-research-synthesizer.md | `["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| strategic-planner.md | `["Read", "Write", "Edit", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| java-developer.md | `["Read", "Write", "Edit", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| java-architect-planner.md | `["Read", "Write", "Edit", "Grep", "Glob", "Bash", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |
| orchestrator.md | `["Read", "Grep", "Glob", "Agent", "mcp__plugin_nx_sequential-thinking__sequentialthinking"]` |

#### Example Change

Before:
```yaml
---
name: knowledge-tidier
version: "2.0"
description: Reviews and consolidates...
model: haiku
color: mint
---
```

After:
```yaml
---
name: knowledge-tidier
version: "2.0"
description: Reviews and consolidates...
model: haiku
color: mint
tools: ["Read", "Grep", "Glob", "Bash"]
---
```

### Success Criteria

- [ ] All 14 files in `nx/agents/*.md` contain a `tools:` line in frontmatter
- [ ] Tool assignments match the table above exactly
- [ ] No other frontmatter fields are modified
- [ ] Verify with: `grep -c '^tools:' nx/agents/*.md` — expect 14 matches

### Test Strategy

1. `grep '^tools:' nx/agents/*.md` — all 14 files match
2. Parse each file's YAML frontmatter and validate the tools array matches the assignment table
3. Verify no syntax errors by checking YAML parses cleanly

---

## Phase 2: Hook Expansion (nexus-eyih)

**Goal**: Expand `nx/hooks/scripts/permission-request-stdin.sh` to auto-approve safe
tool types beyond Bash.

### Current State

The hook currently only handles `$TOOL == "Bash"` with:
- Deny rules for destructive git/bd/mvn commands
- Allow rules for read-only git, bd, nx, mvn commands
- Default: ask user

### Changes Required

Add new sections BEFORE the existing Bash handling to auto-approve non-Bash tools:

```bash
# --- Auto-approve safe tool types ---

# Always safe: read-only local tools
if [[ "$TOOL" == "Read" || "$TOOL" == "Grep" || "$TOOL" == "Glob" ]]; then
  echo "allow"
  exit 0
fi

# Safe: local file operations (controlled by agent tools list)
if [[ "$TOOL" == "Write" || "$TOOL" == "Edit" ]]; then
  echo "allow"
  exit 0
fi

# Safe: read-only external (no mutations)
if [[ "$TOOL" == "WebSearch" || "$TOOL" == "WebFetch" ]]; then
  echo "allow"
  exit 0
fi

# Safe: orchestrator delegation
if [[ "$TOOL" == "Agent" ]]; then
  echo "allow"
  exit 0
fi
```

Expand the Bash allowlist with additional safe commands:

```bash
# Git read commands (expand existing)
if [[ "$COMMAND" =~ ^git\ (log|diff|status|show|branch|tag|rev-parse|describe) ]]; then

# Test runners
if [[ "$COMMAND" =~ ^uv\ run\ pytest ]]; then
  echo "allow"
  exit 0
fi

# Bead management commands (expand existing)
if [[ "$COMMAND" =~ ^bd\ (list|show|search|prime|ready|status|create|update|close|dep|remember|memories) ]]; then
```

### Success Criteria

- [ ] Read, Grep, Glob always auto-approved
- [ ] Write, Edit auto-approved
- [ ] WebSearch, WebFetch auto-approved
- [ ] Agent auto-approved
- [ ] `uv run pytest` auto-approved
- [ ] `bd create`, `bd update`, `bd close`, `bd dep`, `bd remember`, `bd memories` auto-approved
- [ ] `git log`, `git diff`, `git show`, `git branch`, `git tag` auto-approved
- [ ] Existing deny rules preserved (destructive git, bd delete, etc.)
- [ ] MCP tools (`mcp__*`) still NOT auto-approved (default: ask user)
- [ ] Default behavior unchanged: unknown tools still prompt user

### Test Strategy

Test the hook script directly by piping JSON payloads:

```bash
# Should output "allow"
echo '{"tool":"Read"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Grep"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Glob"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Write"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Edit"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"WebSearch"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"WebFetch"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Agent"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Bash","command":"uv run pytest tests/"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Bash","command":"bd create \"test\" -t task"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Bash","command":"bd dep add a b"}' | bash nx/hooks/scripts/permission-request-stdin.sh

# Should output "deny"
echo '{"tool":"Bash","command":"git push --force origin main"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Bash","command":"bd delete nexus-abc1"}' | bash nx/hooks/scripts/permission-request-stdin.sh

# Should output nothing (ask user)
echo '{"tool":"mcp__mixedbread__search_store"}' | bash nx/hooks/scripts/permission-request-stdin.sh
echo '{"tool":"Bash","command":"rm -rf /tmp/foo"}' | bash nx/hooks/scripts/permission-request-stdin.sh
```

---

## Phase 3: Validation (nexus-ryjo)

**Blocked by**: nexus-ngy5, nexus-eyih

**Goal**: Verify that agents can execute their core workflows with the new permissions.

### Validation Plan

1. **knowledge-tidier** (Read+Bash): Invoke with a simple nx CLI task (e.g., `nx memory list --project nexus`). Verify Bash is not denied.

2. **plan-auditor** (Read-only): Invoke to review a small plan file. Verify Read/Grep/Glob work without prompts.

3. **orchestrator** (Agent): Invoke with a routing request. Verify Agent tool is available.

4. **strategic-planner** (Read+Write+Bash): Invoke with a small planning task. Verify Write/Edit/Bash all work.

5. **deep-research-synthesizer** (Web): Invoke with a research query. Verify WebSearch/WebFetch are available.

### Success Criteria

- [ ] No "permission denied" errors for tools listed in agent frontmatter
- [ ] Agents without Bash cannot execute shell commands (negative test)
- [ ] MCP tools still require user approval (not auto-approved)
- [ ] Existing workflows (bd commands, nx commands, git read) continue to work

---

## Phase 4: Finalize (nexus-if5a)

**Blocked by**: nexus-ryjo

**Goal**: Close out the RDR.

### Steps

1. Update `docs/rdr/rdr-023-agent-tool-permissions-audit.md` frontmatter: `status: accepted`
2. Update `docs/rdr/README.md` if RDR-023 is tracked there
3. Close beads: `bd close nexus-qic4 --reason "RDR-023 implemented and validated"`

### Success Criteria

- [ ] RDR-023 status is `accepted`
- [ ] All beads closed
- [ ] Changes merged via PR

---

## Risk Factors

| Risk | Impact | Mitigation |
|---|---|---|
| `tools` frontmatter doesn't control actual access | High — agents still get denied | Hook expansion provides defense-in-depth; both layers needed |
| Hook regex too broad | Medium — over-permits dangerous commands | Deny rules checked FIRST (before allow rules); conservative regex |
| Hook regex too narrow | Low — agents still prompted | Easy to iterate; just expand patterns |
| Agent needs tool not in its list | Low — agent fails on specific task | Add tool to frontmatter, redeploy |

## PR Strategy

Single PR for Phases 1+2 (implementation changes). Branch: `feature/nexus-qic4-agent-tool-permissions`.
Phase 3 is manual validation after merge. Phase 4 is a follow-up commit or small PR.
