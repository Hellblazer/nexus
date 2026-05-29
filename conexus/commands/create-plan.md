---
allowed-tools: Bash
description: Create implementation plan using strategic-planner agent
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
**Bead**: [fill from active epic bead above or create new]

### Input Artifacts
- Files: [fill from relevant existing code for context]

### Requirements
$ARGUMENTS

### Deliverable
Phased execution plan with dependency graph, success criteria per phase, test strategy, and beads created for all trackable items.

### Quality Criteria
- [ ] Work broken into logical phases with clear boundaries
- [ ] Dependencies identified and ordered correctly
- [ ] Success criteria defined per phase (measurable)
- [ ] Test strategy included for each phase
- [ ] Beads created for all trackable items

**IMPORTANT**: After planning completes, call `mcp__plugin_conexus_nexus__nx_plan_audit` for validation before implementation (RDR-080 — direct MCP call).
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
