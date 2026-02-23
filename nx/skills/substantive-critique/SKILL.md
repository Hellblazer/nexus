---
name: substantive-critique
description: >
  Deep constructive critique of code, documentation, plans, or designs. Triggers:
  reviewing architectural decisions, validating implementations against specs,
  verifying claims against evidence, comprehensive system audits.
  (Workaround for substantive-critic framework bug)
# See ../../registry.yaml for full agent metadata
allowed-tools: Task, Read, Glob, Grep, Bash
memory: user
---

# Deep Critique Skill

Delegates to the **substantive-critic** agent. See [registry.yaml](../../registry.yaml) for details.

**Note**: The `classifyHandoffIfNeeded is not defined` framework error can affect any custom agent including this one. Work is complete despite this error. See CLAUDE.md Known Issues section.

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
- nx scratch: [scratch IDs or "none"]           # optional: ephemeral T1 items
- nx pm context: [Phase N, active blockers or "none"]  # optional: from nx pm status
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
- [ ] T2 memory updated with session findings (if multi-session work)

**Session Scratch (T1)**: Agent uses `nx scratch` for ephemeral working notes during the session. Flagged items auto-promote to T2 at session end.
