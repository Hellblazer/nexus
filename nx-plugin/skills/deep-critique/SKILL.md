---
name: deep-critique
description: >
  Deep constructive critique of code, documentation, plans, or designs. Triggers:
  reviewing architectural decisions, validating implementations against specs,
  verifying claims against evidence, comprehensive system audits.
  (Workaround for substantive-critic framework bug)
allowed-tools: Task, Read, Glob, Grep, Bash
memory: user
# See ~/.claude/registry.yaml for full agent metadata
---

# Deep Critique Skill

Delegates to the **deep-critic** agent. See [registry.yaml](../../registry.yaml) for details.

**Note**: This is a workaround for the substantive-critic framework bug (`classifyHandoffIfNeeded is not defined`). Use this agent for comprehensive critiques and audits until the framework bug is fixed.

## When This Skill Activates

- After completing a design document or specification
- When reviewing architectural decisions
- After plan-auditor for additional depth
- When validating implementation against specification
- When cross-referencing documentation for consistency
- When verifying claims against evidence
- Before major milestones or releases

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

## Critique Methodology

The agent uses sequential thinking:
1. Establish context and purpose of artifact
2. Identify criteria/specifications it should conform to
3. Gather evidence from nx store (`nx search --corpus knowledge`) and related artifacts
4. Analyze structural integrity
5. Analyze logical consistency
6. Assess completeness
7. Synthesize findings by priority (Critical/Significant/Minor)
8. Formulate actionable recommendations

## Success Criteria

- [ ] Context and purpose established
- [ ] Evidence gathered from relevant sources
- [ ] All major dimensions analyzed (structure, logic, completeness)
- [ ] Findings prioritized by impact
- [ ] Recommendations are specific and actionable
- [ ] No surface-level issues dominate over substantive ones
