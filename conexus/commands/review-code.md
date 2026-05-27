---
allowed-tools: Bash
description: Review code changes using code-review-expert agent
---

# Code Review Request

!`nx command-context review-code -- "$ARGUMENTS"`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Focus Areas

$ARGUMENTS

## Action

Invoke the **code-review** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: code-review-expert

**Task**: Review the code changes for quality, security, and best practices
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from modified files list above]

### Deliverable
Structured code review with severity-rated findings, grouped by category (correctness, security, maintainability, performance).

### Quality Criteria
- [ ] All changed files reviewed
- [ ] Findings categorized by severity (critical, important, suggestion)
- [ ] Actionable fix recommendations for each finding

### Focus Areas
$ARGUMENTS
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
