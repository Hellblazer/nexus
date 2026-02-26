---
name: plan-validation
description: Use when a plan has been created and needs validation before implementation begins, or when reviewing an existing plan for gaps
---

# Plan Validation Skill

Delegates to the **plan-auditor** agent (sonnet). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- **Always** after strategic-planner creates a plan (required step)
- Before starting implementation of a planned feature
- When reviewing an existing plan for accuracy
- When validating that a plan aligns with the codebase
- Before committing significant effort to a plan

## Agent Invocation

Use the Task tool to invoke **plan-auditor**:

```markdown
## Relay: plan-auditor

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Plan validation report with pass/fail/warn items

### Quality Criteria
- [ ] All plan assumptions verified
- [ ] Dependencies confirmed present
- [ ] Clear go/no-go decision provided
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Validation Methodology

The plan-auditor agent uses `mcp__sequential-thinking__sequentialthinking`:
1. Extract key components and assumptions from plan
2. Form hypothesis about each assumption's validity
3. Verify each component against actual codebase
4. Test build commands if specified
5. Check for missing dependencies or prerequisites
6. Identify inconsistencies with current code
7. Provide go/no-go recommendation

**REQUIRED BACKGROUND:** Understand nx:strategic-planning for plan structure and conventions.

## Decision Outcomes

**GO**: Proceed to java-developer with validated plan
**NO-GO**: Return to strategic-planner with specific issues for revision

## Agent-Specific PRODUCE

- **Session Scratch (T1)**: `nx scratch put "<notes>" --tags "audit"` — audit working notes during session; flagged items auto-promote to T2 at session end
- **nx store**: `echo "..." | nx store put - --collection knowledge --title "validation-plan-{plan-id}" --tags "validation,audit"` — audit results for the validated plan
- **Beads**: creates gap/risk beads (`bd create "..." -t task`) for major issues found during validation

## Success Criteria

- [ ] All plan assumptions verified
- [ ] Dependencies confirmed present
- [ ] Build commands tested (if applicable)
- [ ] Risks documented and acceptable
- [ ] Clear go/no-go decision provided
- [ ] If no-go, specific issues identified

**Session Scratch (T1)**: Agent uses `nx scratch` for ephemeral working notes during the session. Flagged items auto-promote to T2 at session end.
