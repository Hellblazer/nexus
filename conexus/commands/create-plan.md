---
allowed-tools: Bash
description: Create implementation plan using strategic-planner agent
disable-model-invocation: true
---

# Planning Request

!`nx command-context create-plan`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Feature/Task to Plan

$ARGUMENTS

## Action

Invoke the **strategic-planning** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: strategic-planner

**Task**: Create comprehensive implementation plan for: $ARGUMENTS
**Bead**: [fill from the active epic bead above (create one where the project has `.beads/`), or "none"]

### Input Artifacts
- Files: [fill from relevant existing code for context]

### Requirements
$ARGUMENTS

### Deliverable
Phased plan with dependency graph, per-phase success criteria and test strategy, and, where the project has `.beads/`, beads for all trackable items; elsewhere skip the `bd` steps and put the task breakdown in your hand-back and, where the conexus nexus tools are available, in the T2 plan record `plan-{name}.md`.

### Quality Criteria
- [ ] Work broken into logical phases with clear boundaries
- [ ] Dependencies identified and ordered correctly
- [ ] Success criteria defined per phase (measurable)
- [ ] Test strategy included for each phase
- [ ] Where the project has `.beads/`, beads created for all trackable items

**IMPORTANT**: After planning completes, call `mcp__plugin_conexus_nexus__nx_plan_audit` for validation before implementation (a direct MCP call).
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/RELAY_TEMPLATE.md).
