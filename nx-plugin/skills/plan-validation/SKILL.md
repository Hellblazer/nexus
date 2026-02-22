---
name: plan-validation
description: >
  Validate implementation plans before execution. Triggers: strategic-planner completes,
  before starting implementation, user says "validate plan" or "audit plan".
allowed-tools: Task, Read, Glob, Grep, Bash
# See ~/.claude/registry.yaml for full agent metadata
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

The plan-auditor agent uses sequential thinking:
1. Extract key components and assumptions from plan
2. Form hypothesis about each assumption's validity
3. Verify each component against actual codebase
4. Test build commands if specified
5. Check for missing dependencies or prerequisites
6. Identify inconsistencies with current code
7. Provide go/no-go recommendation

## Decision Outcomes

**GO**: Proceed to java-developer with validated plan
**NO-GO**: Return to strategic-planner with specific issues for revision

## Success Criteria

- [ ] All plan assumptions verified
- [ ] Dependencies confirmed present
- [ ] Build commands tested (if applicable)
- [ ] Risks documented and acceptable
- [ ] Clear go/no-go decision provided
- [ ] If no-go, specific issues identified
