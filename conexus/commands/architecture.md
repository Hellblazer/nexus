---
allowed-tools: Bash
description: Design architecture and create phased execution plans using architect-planner agent
---

# Architecture Request

!`nx command-context architecture -- "$ARGUMENTS"`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Feature/Component to Architect

$ARGUMENTS

## Action

Invoke the **architecture** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: architect-planner

**Task**: Design architecture for: $ARGUMENTS
**Bead**: [fill from active epic/feature bead above or create new]

### Input Artifacts
- Files: [fill from key existing source files for context]

### Requirements
$ARGUMENTS

### Deliverable
Comprehensive architecture design with component boundaries, interface contracts, dependency graph, phased execution plan with beads, and risk assessment with mitigations.

### Quality Criteria
- [ ] All requirements addressed in design
- [ ] Component boundaries clearly defined with interface contracts
- [ ] Integration points with existing code identified
- [ ] Phased execution plan created with beads and dependencies
- [ ] Risks identified with concrete mitigations
- [ ] Design follows project conventions (check CLAUDE.md)
- [ ] Ready for nx_plan_audit validation

**IMPORTANT**: After architecture is designed, MUST delegate to nx_plan_audit for validation before implementation begins.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
