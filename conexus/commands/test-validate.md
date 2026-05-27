---
allowed-tools: Bash
description: Validate tests using test-validator agent
---

# Test Validation Request

!`nx command-context test-validate -- "$ARGUMENTS"`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Focus Area

$ARGUMENTS

## Action

Invoke the **test-validation** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: test-validator

**Task**: Validate test coverage and quality for recent changes
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from changed source files and their test files above]

### Changes to Validate
[fill from recently modified files list above]

### Focus Area
$ARGUMENTS

### Deliverable
Test coverage report with gap analysis: mapping of source files to test files, identified coverage gaps, assessment of test quality (meaningful vs padding), and pass/fail status.

### Quality Criteria
- [ ] All changed source files mapped to corresponding test files
- [ ] Coverage gaps identified with specific missing scenarios
- [ ] Tests validated as meaningful (not just coverage padding)
- [ ] All tests pass (verified by running test suite)
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
