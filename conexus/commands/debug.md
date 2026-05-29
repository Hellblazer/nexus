---
allowed-tools: Bash
description: Debug test failures using debugger agent
---

# Debug Request

!`nx command-context debug`

## Issue

$ARGUMENTS

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Action

Invoke the **debugging** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: debugger

**Task**: Investigate failure using hypothesis-driven debugging
**Bead**: [fill from active bead above or create bug bead]

### Input Artifacts
- Files: [fill from relevant source and test files]

### Context
- Error message: [fill from test output above]
- Stack trace: [fill key frames from test output]
- Failed attempts: [fill what was already tried, or 'first attempt']

### Deliverable
Root cause analysis with hypothesis chain, supporting evidence, proposed fix, and regression test recommendation.

### Quality Criteria
- [ ] Root cause definitively identified with evidence
- [ ] Hypothesis chain documented (explored and eliminated alternatives)
- [ ] Fix addresses root cause (not symptoms)
- [ ] Regression test recommended to prevent recurrence
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
