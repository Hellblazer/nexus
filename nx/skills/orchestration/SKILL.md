---
name: orchestration
description: Use when unsure which agent to use for a task, or when coordinating work across multiple agents in a pipeline
---

# Orchestration Skill

Delegates to the **orchestrator** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- When the task is ambiguous about which agent to use
- When coordinating work across multiple agents
- When user needs help choosing the right approach
- When setting up multi-agent pipelines
- When workflow routing decisions are needed

```dot
digraph routing {
    rankdir=TB;
    "Request type?" [shape=diamond];

    "Plan a feature" [shape=box];
    "Implement code" [shape=box];
    "Debug issue" [shape=box];
    "Review code" [shape=box];
    "Research topic" [shape=box];
    "Analyze system" [shape=box];

    "strategic-planner" [shape=ellipse];
    "plan-auditor" [shape=ellipse];
    "architect-planner" [shape=ellipse];
    "developer" [shape=ellipse];
    "code-review-expert" [shape=ellipse];
    "test-validator" [shape=ellipse];
    "debugger" [shape=ellipse];
    "deep-analyst" [shape=ellipse];
    "substantive-critic" [shape=ellipse];
    "deep-research-synthesizer" [shape=ellipse];
    "knowledge-tidier" [shape=ellipse];
    "codebase-deep-analyzer" [shape=ellipse];

    "Request type?" -> "Plan a feature" [label="plan/design"];
    "Request type?" -> "Implement code" [label="build/implement"];
    "Request type?" -> "Debug issue" [label="test failure/bug"];
    "Request type?" -> "Review code" [label="review/quality"];
    "Request type?" -> "Research topic" [label="research/investigate"];
    "Request type?" -> "Analyze system" [label="explore/understand"];

    "Plan a feature" -> "strategic-planner";
    "strategic-planner" -> "plan-auditor" [label="then"];
    "plan-auditor" -> "architect-planner" [label="then"];

    "Implement code" -> "developer";
    "developer" -> "code-review-expert" [label="then"];
    "code-review-expert" -> "test-validator" [label="then"];

    "Debug issue" -> "debugger";
    "debugger" -> "deep-analyst" [label="if cross-cutting"];

    "Review code" -> "code-review-expert";
    "code-review-expert" -> "substantive-critic" [label="if critical"];

    "Research topic" -> "deep-research-synthesizer";
    "deep-research-synthesizer" -> "knowledge-tidier" [label="then"];

    "Analyze system" -> "codebase-deep-analyzer";
    "codebase-deep-analyzer" -> "deep-analyst" [label="if deep"];
}
```

## Agent Invocation

Use the Task tool to invoke **orchestrator**:

```markdown
## Relay: orchestrator

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Routing decision with recommended agent

### Quality Criteria
- [ ] User goal clearly understood
- [ ] Appropriate agent(s) identified
- [ ] Clear rationale provided
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Routing Quick Reference

| Request Type | Primary Agent | Pipeline |
|-------------|---------------|----------|
| Plan a feature | strategic-planner | -> plan-auditor -> architect-planner |
| Implement code | developer | -> code-review-expert -> test-validator |
| Debug issue | debugger | -> (if cross-cutting) deep-analyst |
| Review code | code-review-expert | -> (if critical) substantive-critic |
| Research topic | deep-research-synthesizer | -> knowledge-tidier |
| Analyze system | codebase-deep-analyzer | -> (if deep) deep-analyst |


## Success Criteria

- [ ] User goal clearly understood
- [ ] Appropriate agent(s) identified
- [ ] Workflow makes sense for the task
- [ ] Clear rationale provided
- [ ] User can proceed with confidence

## Agent-Specific PRODUCE

- **Routing Decisions**: Document in response; for significant routing patterns store in nx T3:
  store_put tool: content="# Routing Pattern: {pattern}\n{rationale}", collection="knowledge", title="pattern-orchestrator-{scenario}", tags="routing,orchestration"
- **Pipeline Plans**: Relay to first agent in pipeline using standard relay format
- **Escalation Notes**: Create blocker beads when routing is blocked
- **Routing Notes**: Use T1 scratch during complex pipeline analysis:
  scratch tool: action="put", content="Routing hypothesis: {agent} because {reason}", tags="routing,pipeline"
