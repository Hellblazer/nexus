---
name: orchestration
description: Use when unsure which agent to use for a task, or when coordinating work across multiple agents in a pipeline
effort: low
---

# Orchestration Skill

Reference skill for agent routing and pipeline coordination. See [reference.md](./reference.md) for routing tables, pipeline templates, and the decision framework.

## When This Skill Activates

- When the task is ambiguous about which agent to use
- When coordinating work across multiple agents
- When user needs help choosing the right approach
- When setting up multi-agent pipelines
- When workflow routing decisions are needed

## How to Use

1. Consult [reference.md](./reference.md) for the routing graph and decision framework
2. Match the request to the appropriate agent or pipeline
3. Dispatch the agent directly using the relay format from [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md)

There is no orchestrator agent — the caller (main conversation or skill) dispatches agents directly using the routing tables.

## Quick Routing

| Request Type | Primary Agent | Pipeline |
|-------------|---------------|----------|
| Plan a feature | strategic-planner | -> nx_plan_audit -> architect-planner |
| Implement code | developer | -> code-review-expert -> test-validator |
| Debug issue | debugger | -> (if cross-cutting) deep-analyst |
| Review code | code-review-expert | -> (if critical) substantive-critic |
| Research topic | deep-research-synthesizer | -> store_put (direct) |
| Analyze system | codebase-deep-analyzer | -> (if deep) deep-analyst |

For the full routing graph, decision framework, and standard pipelines, see [reference.md](./reference.md).
