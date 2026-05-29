---
allowed-tools: Bash
description: Constructive critique of code, plans, designs, or documentation using substantive-critic agent
---

# Deep Critique Request

!`nx command-context substantive-critique`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Artifact to Critique

$ARGUMENTS

## Action

Invoke the **substantive-critique** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: substantive-critic

**Task**: Provide deep constructive critique of: $ARGUMENTS
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from artifact files to critique]

### Artifact
$ARGUMENTS

### Deliverable
Structured critique with findings categorized by priority (Critical/Significant/Minor), each with specific actionable recommendations. Covers structure, logical consistency, completeness, and spec conformance.

### Quality Criteria
- [ ] Context and purpose of artifact established
- [ ] Relevant specifications and conformance criteria identified
- [ ] Structure, logic, and completeness all analyzed
- [ ] Findings prioritized by impact (Critical > Significant > Minor)
- [ ] Each recommendation is specific and actionable
- [ ] Substantive issues surfaced (not just style/surface-level)
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
