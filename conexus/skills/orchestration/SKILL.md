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

There is no orchestrator agent. The caller (main conversation or skill) dispatches agents directly using the routing tables.

## Background-Teammate Ledger (RDR-184: MANDATORY at dispatch time)

The declaration surface is a SHELL LIB, not an nx verb (nexus-3ra9h: `nx expectations` / `nx orchestration` / `nx guard` do not exist; declarations improvised into `nx scratch` are invisible to the audit). In a nexus checkout:

```bash
source tests/e2e/lib/expectations.sh   # plugin copy: conexus/hooks/scripts/expectations.sh
```

1. The EXPECT row is MECHANIZED. Do not hand-write one (nexus-qc4p1). A PreToolUse hook on the Agent tool (`conexus/hooks/scripts/agent-dispatch-expect.sh`) writes it from the dispatch's own `subagent_type` + `run_in_background`, before the dispatch lands. Hand-writing an extra row now DOUBLE-COUNTS: the ledger matches N EXPECT rows of a type against N STARTs of that type, so a manual row inflates the count and shows up as a spurious `EXPECTED_NO_START`. Call `expectations_expect` by hand only for a dispatch the hook cannot see (it fires on the Agent/Task tool only), and key it on the SUBAGENT TYPE, never on an invented name. The Agent tool has no `name` parameter, so a name-keyed row cannot pair with anything the SubagentStart hook records (nexus-nu7fo: 25 dispatches, zero recognised).
2. Put the completion protocol (SendMessage report: outcome, artifacts, blockers) in every background teammate's dispatch prompt. A unique name is still useful for YOUR bookkeeping and for the mailbox, but it is no longer what the ledger pairs on: two dispatches of one type are matched N-to-N, because no per-instance key exists in either hook payload (`tool_use_id` is absent from SubagentStart, and `prompt_id` is the turn id shared by every dispatch in the message).
3. At retro / session end:
   `expectations_census <session_id>` gives scripted counts; never hand-count (nexus-hybv1). `expectations_undeclared <session_id>` files a mechanization bead for any UNDECLARED row (Gap-1 escalation). CHECK ITS EXIT CODE. The full contract (locked T1 d40a5b53, nexus-suuja; re-keyed at nexus-houpu): exit 0 = clean; exit 1 = BLINDSPOT, meaning EXPECT rows present but ZERO STARTs walked, so the audit examined nothing, which is not a pass (nexus-mk3tw, re-aimed); exit 2 = `undeclared>0` deficit; exit 3 = no ledger file for this session, so nothing was checkable, which is not evidence of cleanliness (nexus-ahl9v; e.g. a mistyped session id). Since CC 2.1.251 (nexus-houpu) the payload carries no dispatch name: `agent_type` is the `subagent_type` and `agent_id` is opaque, so BOTH audits pair every START to its EXPECT row by type with N-of-type credit. A START whose type has no EXPECT row is named UNDECLARED per dispatch (a dispatch the PreToolUse hook cannot see, e.g. a Workflow-tool agent, reads that way unless you declared it by hand at dispatch time, per item 1). `expectations_census` is the report and shares only the rc=1 blind-spot rule; it never exits 2.

`BLOCKED` followed by `REPORTED` in the ledger means the stop-guard nudged the report out (guard success); a bare `BLOCKED` is genuinely unresolved.

## Subagent Dispatch: Design-of-Record Brief Template (MANDATORY at dispatch time)

Every subagent dispatch is briefed from a design-of-record written before dispatch, stored in T1, and referenced by id in every downstream brief (developer, both reviewers, every fix round). The agent never re-derives a decision (nexus-4kp77, T2 [21371] §Q4).

Mandatory-fields skeleton:

```
## <BEAD-ID> DESIGN OF RECORD (<role>, <date>)
PROBLEM: <one paragraph: observed behavior vs expected, with the pointer that proves it>
DECISION: <the chosen fix, named, with the authority that backs it (RDR/decision/file:line)>
REJECTED: <each rejected alternative + the concrete reason it breaks something, with file:line>

## TASK ITEMS  (numbered; one deliverable each)
<N>. <imperative statement>  <file:line for every site to touch>
     Constraint: <what must remain true>
     Test: <the specific assertion required>

## LOCKED INVARIANTS
- <wire contract / API shape / predicate that parallel halves must both honor, verbatim>
- <house patterns that must not be violated, with the precedent file:line>

## OUT OF SCOPE
- <explicitly deferred item> -> <residual bead id or "file a bead">

## VERIFY (commands, exactly as the agent must run them)
- <test command scoped to the change>
- <lint command>
- Report the collected test COUNT, not a description.
- Verification runs FOREGROUND inside the agent's own turn. Never `run_in_background`, never `Monitor`. A dispatched subagent cannot receive Monitor events or background-task-completion notifications; those route to the MAIN loop only. Waiting on either strands the agent (nexus-dn9xs).

## WRITE-BACK (mandatory, before returning)
mcp__plugin_conexus_nexus__scratch(action="put", content="<bead> <phase> ...", tags="<bead>,<role>,<phase>")
Report the returned entry_id VERBATIM. It is a UUID; do not invent a title-shaped id.

## HAND-BACK
SendMessage to orchestrator: outcome, artifact paths, deviations (each labelled DEVIATION with rationale), blockers.
Shared tree: never git add/commit. Hand back diffs + paths.
```

Non-negotiable fields: bead id; design-of-record T1 id (referenced, never restated); file:line for every touch site; locked invariants; verify commands (foreground-only, per above); write-back tags; deviation-reporting instruction.

## Notification Handling (idempotent, MANDATORY)

Task notifications are at-least-once, unbounded-delay, and cross session boundaries. A resumed agent re-notifies under the same task-id, and a notification can surface in the NEXT session after the agent already finished (nexus-62wt7, T2 [21371] §Q3).

1. Key on task-id. Maintain a handled set; a repeat notification for a handled id is a no-op unless its content differs.
2. Never act on a notification's claims directly. Re-derive ground truth first: `git status --porcelain`, file mtimes, and the agent's own T1 write-back (the write-back, not the notification, is the artifact of record). Where to look: T1 has three distinct scopes. MCP-tool `scratch` is frozen to the session id at MCP-server spawn time and survives `/clear` and `/resume`, so a background agent's write-back lands in the ORIGINAL session's scope. `nx` CLI `scratch` is scoped to the current transcript session if a live lease exists, else a shared fallback (nexus-f7xyq). `~/.config/nexus/current_session` is machine-wide, last-writer-wins, and can be owned by a concurrent session. Check the MCP scope before declaring a write-back lost.
3. Before treating a write-back as missing, confirm the agent has actually terminated. Searching T1 while the agent is still running and treating absence as loss is the recurring false-loss pattern (cdypx, 2026-08-03).
4. A late notification arriving after recovery work is already complete is informational only. Reconcile it, and CORRECT any recovery note that recorded a now-falsified claim.
5. Never background-dispatch near a session pause; a live agent at session end cannot be recovered except by hand.

## VERIFY Line Convention (MANDATORY)

Agent write-backs end with a machine-checkable line, not prose (nexus-pjzz8, T2 [21371] §Q5):

```
VERIFY: <command> => <count> passed
```

The orchestrator re-runs that ONE command once per round, before accepting the round. It never trusts the reported count. Divergence is surfaced, not silently accepted.

## Review Rounds (MANDATORY)

Before each reviewer dispatch, write the round marker:

```bash
nx scratch put "review-round bead=<id> n=<N> bar=<the acceptance bar, one line>" --tags "review,<id>"
```

Include `round N of at most 2` in the reviewer's brief. A reviewer that knows
it is a confirmation pass behaves like one (see `/conexus:code-review` §
Confirmation Pass). N >= 3 requires the human to have asked for it in this
session, by name; record the ask in the marker (`asked-by=human`). An
orchestrator may not self-authorize round 3, with ONE carve-out: a
ship-blocking defect INTRODUCED by the fix under confirmation (the
Confirmation Pass exception) may receive exactly one orchestrator-authorized
extension pass verifying that defect's fix, recorded in the marker as
`ext=fix-introduced`. The extension does not renumber the episode; anything
beyond it is round 3 and needs the human.

Counter-metric that travels with this bound (from the ratchet's history test,
T2 nexus/prompt-ratchet-history-test-2026-08-17): findings per round must
RISE while rounds per bead FALL. If both fall, the bound is suppressing
recall; about 20% of historical later-round Criticals were pre-existing
defects a confirmation-scoped pass would not have found. Revert to
full-review rounds and surface it, rather than celebrating the speed.

## Scope Discipline (cross-reference)

See [CONTEXT_PROTOCOL.md § Scope](../../agents/_shared/CONTEXT_PROTOCOL.md).
Deliver at the scope intended: a plan or bead graph is a record of intended
work, not a standing order; goal-met is a stop, not a license to continue; a
blast radius exploding mid-flight (more than about 20 tests broken by one
change, or a repair fleet forming) is a stop-and-surface point; narrowing
without a DEVIATION line is still silent scope reduction. Applies to every
dispatch below.

## Parallel-Orchestration Discipline (MANDATORY, multi-arc sessions)

- Fleet size: more than 3 concurrent agents on one bead is a
  stop-and-surface event (see § Scope Discipline above), not a scaling
  decision. It is the signal the change was too big, not a reason to
  parallelize harder.
- Parallel arcs: multiple dev arcs may run concurrently ONLY on disjoint file sets, declared in each brief ("touch ONLY: `<files>`"; name what siblings own). One file = one arc, never two writers.
- Shared files (PENDING_RELEASE.md, CHANGELOG, registration files): the ORCHESTRATOR edits them at commit time; agents never touch them.
- Parallel halves of one feature (e.g. engine + client): the wire contract is LOCKED verbatim in the design-of-record BEFORE dispatch; both briefs cite it; reviewers verify contract compatibility across the halves.
- Design gate first: when a plan marks an item DESIGN DECISION FIRST, the orchestrator locks the design-of-record in T1 before any code dispatch; deviations from a plan's recommendation need the human's nod, named fallbacks do not.
- Review at every seam: each arc gets the stacked dual review independently; cross-arc integration points get named in reviewer briefs.
- Commit order: arcs commit pathspec-limited in dependency order (lib contract before doc text referencing it), one arc per commit.
- service/ builds: one builder at a time. Never dispatch a bare `./mvnw`/`mvn` invocation while another agent might also be building `service/`; both write `service/target` and a collision corrupts jOOQ codegen mid-build (nexus-c00dw). Any ad hoc Maven call goes through `scripts/mvnw-leased.sh <args>` (it takes the `scripts/lib/build-lease.sh` lease, cds into `service/`, and passes args through); `scripts/build-gate-jar.sh` takes the same lease internally for the stamped-jar path. A refusal (rc 75) names the live holder; wait for it, don't bypass it. Coverage (every in-repo caller, not just these two) is enforced by `scripts/lib/bare_mvnw_lint_test.sh`.

## Quick Routing

| Request Type | Primary Agent | Pipeline |
|-------------|---------------|----------|
| Plan a feature | strategic-planner | -> nx_plan_audit -> architect-planner |
| Implement code | developer | -> code-review-expert -> test-validator |
| Debug issue | debugger | -> (if cross-cutting) deep-analyst |
| Review code | code-review-expert | -> substantive-critic (always, both reviewers) |
| Research topic | deep-research-synthesizer | -> store_put (direct) |
| Analyze system | codebase-deep-analyzer | -> (if deep) deep-analyst |

For the full routing graph, decision framework, and standard pipelines, see [reference.md](./reference.md).

## Route by shape, not only by task type

The table above routes by what KIND of work it is. It does not route by how
much raw material the work drags through your context. Both axes decide.

### Distill early: an un-distilled result is paid once per remaining turn

An agentic loop re-sends prior tool results as input on every subsequent
turn. So the cost of not distilling is `payload x turns_remaining`, not
`payload`. A 50k dump on turn 3 of a 40-turn session costs far more than
the identical dump on turn 38.

- Decide what the ONE line you need is BEFORE running the command.
  `grep -c` over `cat`; `| head -5` over full output; a `python3` heredoc
  that prints the verdict over a dump you then read.
- Aggression should scale with how much session remains. Early is where
  it pays; late-session sloppiness is nearly free.
- This is not a nexus mechanism. The shell is already the sandbox; the
  distinction is only whether intermediate bytes cross into context.

### Dispatch a subagent for bulk READS, not only for judgment

A subagent's entire transcript stays in its own context; only its return
crosses into yours. That property is independent of whether the task
needs judgment.

| Shape | Route |
|---|---|
| Needs reading many files/outputs, you want only the conclusion | subagent, even if mechanical |
| One known file, one known fact | read it directly; a dispatch costs more than the read |
| Many items, mechanical, output is the answer itself | one shell command, not N tool calls |
| Needs judgment AND bulk reading | subagent (the default case) |

The habitual error is reaching for a subagent only when judgment is
wanted, and doing "find every caller of X and tell me which three
matter" inline, paying full payload for material you discard.

### Restraint is scoped to exploratory and lookup dispatch

Do not delegate work you can finish in a handful of tool calls; a mechanical
sweep that fits in one shell command is one shell command; never dispatch a
subagent to verify your own work.

This restraint does NOT apply to the two-reviewer gate
(`code-review-expert` + `substantive-critic`, `~/.claude/CLAUDE.md` §
Review Discipline) or to independent-state fork fleets (disjoint files, no
shared mutable state). Both do genuinely more than "a handful of tool
calls" worth of independent work, and serializing either buys no safety
(see `~/.claude/CLAUDE.md` § Testing, "serial-vs-parallel: share a mutable
resource -> serialize"). See § Parallel-Orchestration Discipline for the
fleet-size cap that bounds the second case.
