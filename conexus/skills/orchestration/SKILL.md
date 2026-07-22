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

## Background-Teammate Ledger (RDR-184 — MANDATORY at dispatch time)

The declaration surface is a SHELL LIB, not an nx verb (nexus-3ra9h: `nx expectations` / `nx orchestration` / `nx guard` do not exist; declarations improvised into `nx scratch` are invisible to the audit). In a nexus checkout:

```bash
source tests/e2e/lib/expectations.sh   # plugin copy: conexus/hooks/scripts/expectations.sh
```

1. BEFORE every named background Agent dispatch:
   `expectations_expect <session_id> <name> background`
   (write-before-dispatch is load-bearing; a fast teammate can stop before a post-dispatch write lands)
2. Give every background teammate a UNIQUE name and put the completion protocol (SendMessage report: outcome, artifacts, blockers) in its dispatch prompt.
3. At retro / session end:
   `expectations_census <session_id>` — scripted counts, never hand-count (nexus-hybv1); `expectations_undeclared <session_id>` — any UNDECLARED row files a mechanization bead (Gap-1 escalation).

`BLOCKED` followed by `REPORTED` in the ledger means the stop-guard nudged the report out (guard success); a bare `BLOCKED` is genuinely unresolved.

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
