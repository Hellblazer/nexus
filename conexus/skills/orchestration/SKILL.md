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
   `expectations_census <session_id>` — scripted counts, never hand-count (nexus-hybv1); `expectations_undeclared <session_id>` — any UNDECLARED row files a mechanization bead (Gap-1 escalation). CHECK ITS EXIT CODE: exit 1 + `BLINDSPOT` means it recognised none of the dispatches it saw, which is a false-clean, not a pass (nexus-mk3tw).

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

## Route by shape, not only by task type

The table above routes by what KIND of work it is. It does not route by how
much raw material the work drags through your context. Both axes decide.

### Distill early — an un-distilled result is paid once per remaining turn

An agentic loop re-sends prior tool results as input on every subsequent
turn. So the cost of not distilling is `payload x turns_remaining`, not
`payload`. A 50k dump on turn 3 of a 40-turn session costs far more than
the identical dump on turn 38.

- Decide what the ONE line you need is BEFORE running the command.
  `grep -c` over `cat`; `| head -5` over full output; a `python3` heredoc
  that prints the verdict over a dump you then read.
- Aggression should scale with how much session remains. Early is where
  it pays; late-session sloppiness is nearly free.
- This is not a nexus mechanism. The shell is already the sandbox — the
  distinction is only whether intermediate bytes cross into context.

### Dispatch a subagent for bulk READS, not only for judgment

A subagent's entire transcript stays in its own context; only its return
crosses into yours. That property is independent of whether the task
needs judgment.

| Shape | Route |
|---|---|
| Needs reading many files/outputs, you want only the conclusion | subagent — even if mechanical |
| One known file, one known fact | read it directly; a dispatch costs more than the read |
| Many items, mechanical, output is the answer itself | one shell command, not N tool calls |
| Needs judgment AND bulk reading | subagent (the default case) |

The habitual error is reaching for a subagent only when judgment is
wanted, and doing "find every caller of X and tell me which three
matter" inline — paying full payload for material you discard.
