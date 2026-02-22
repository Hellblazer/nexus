---
name: orchestration
description: >
  Route requests to appropriate specialized agents and manage multi-agent pipelines.
  Triggers: task ambiguous, coordinating multiple agents, user says "which agent".
# See ../../registry.yaml for full agent metadata
allowed-tools: Task, Read, Glob, Grep, Bash
---

# Orchestration Skill

Delegates to the **orchestrator** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- When the task is ambiguous about which agent to use
- When coordinating work across multiple agents
- When user needs help choosing the right approach
- When setting up multi-agent pipelines
- When workflow routing decisions are needed

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

## Routing Quick Reference

| Request Type | Primary Agent | Pipeline |
|-------------|---------------|----------|
| Plan a feature | strategic-planner | -> plan-auditor -> java-architect-planner |
| Implement code | java-developer | -> code-review-expert -> test-validator |
| Debug issue | java-debugger | -> (if cross-cutting) deep-analyst |
| Review code | code-review-expert | -> (if critical) deep-critic |
| Research topic | deep-research-synthesizer | -> knowledge-tidier |
| Analyze system | codebase-deep-analyzer | -> (if deep) deep-analyst |

Note: subagent-start hook auto-injects nx pm context when `.pm/` directory exists.

## Success Criteria

- [ ] User goal clearly understood
- [ ] Appropriate agent(s) identified
- [ ] Workflow makes sense for the task
- [ ] Clear rationale provided
- [ ] User can proceed with confidence

**Session Scratch (T1)**: Agent uses `nx scratch` for ephemeral working notes during the session. Flagged items auto-promote to T2 at session end.
