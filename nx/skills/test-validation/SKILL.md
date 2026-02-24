---
name: test-validation
description: Use when implementation is complete and test coverage needs verification, before merge or pull request
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

Use the Task tool to invoke **test-validator**:

```markdown
## Relay: test-validator

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Test coverage report with gap analysis

### Quality Criteria
- [ ] All changed files have corresponding tests
- [ ] Test coverage meets project standards
- [ ] Edge cases covered
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Validation Methodology

The agent will:
1. Identify all changed/added production code
2. Map existing tests to the changes
3. Run relevant test suites
4. Analyze coverage gaps
5. Check test quality (assertions, edge cases, error paths)
6. Recommend additional tests if needed

## Agent-Specific PRODUCE

- **Session Scratch (T1)**: `nx scratch put "<snapshot>" --tags "test-run"` — test run snapshots and interim findings during session
- **nx memory**: `nx memory put "..." --project {project} --title test-validation-{date}.md` — quality metrics and coverage findings persisted across sessions

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
- [ ] T2 memory updated with session findings (if multi-session work)

**Session Scratch (T1)**: Agent uses `nx scratch` for ephemeral working notes during the session. Flagged items auto-promote to T2 at session end.
