---
name: test-validation
description: >
  Validate test coverage and quality after implementation. Triggers: finishing feature,
  after fixing a bug, before merge/PR, user says "check tests" or "validate coverage",
  CI/CD check needed, verifying code has adequate tests.
allowed-tools: Task, Read, Glob, Grep, Bash
# See ~/.claude/registry.yaml for full agent metadata
---

# Test Validation Skill

Delegates to the **test-validator** agent. See [registry.yaml](../../registry.yaml) for details.

## When This Skill Activates

- After completing feature implementation
- After fixing a bug
- Before marking work as complete
- Before creating pull request
- Before merge to main branch
- When verifying test coverage for changed code
- When test quality assessment is needed

## Agent Invocation

## Relay Template (Use This Format)

When invoking this agent via Task tool, use this exact structure:

```markdown
## Relay: {agent-name}

**Task**: [1-2 sentence summary of what needs to be done]
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- Files: [key files or "none"]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] [Criterion 3]
```

**Required**: All fields must be present. Agent will validate relay before starting.

For additional optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Validation Methodology

The agent will:
1. Identify all changed/added production code
2. Map existing tests to the changes
3. Run relevant test suites
4. Analyze coverage gaps
5. Check test quality (assertions, edge cases, error paths)
6. Recommend additional tests if needed

## Bead Integration

After validation:
- **Tests pass + coverage adequate**: `bd close <id>` with success note
- **Tests fail**: Keep in_progress, may trigger java-debugging
- **Coverage gaps**: Create new bead for missing tests

## Success Criteria

- [ ] All changed files have corresponding tests
- [ ] Test coverage meets project standards
- [ ] All tests pass
- [ ] Edge cases covered
- [ ] No obvious test gaps remain
- [ ] Bead status updated appropriately
