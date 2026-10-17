---
name: orchestration
description: Use when unsure which agent or skill fits a task, or when dispatching and gating agents (briefs, review gate, debugger escalation, pipelines)
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
3. Dispatch the agent directly using the relay format from [RELAY_TEMPLATE.md](../../resources/agent-shared/RELAY_TEMPLATE.md)

There is no orchestrator agent. The caller (main conversation or skill) dispatches agents directly using the routing tables.

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
- Close the hand-back with a VERIFY block in the § VERIFY Line Convention shape below — one line per claim, not prose.

## WRITE-BACK (mandatory, before returning)
mcp__plugin_conexus_nexus__scratch(action="put", content="<bead> <phase> ...", tags="<bead>,<role>,<phase>")
Report the returned entry_id VERBATIM. It is a UUID; do not invent a title-shaped id.

## HAND-BACK
SubagentHandback (or SendMessage) to orchestrator: outcome, artifact paths, deviations (each labelled DEVIATION with rationale), blockers.
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

## Resuming a Worktree Agent After a `/clear` (nexus-xxvv3)

A background `worktree-developer` still mid-flight when the session `/clear`s can be resumed with `SendMessage` under the NEW session id, but the harness re-fires SubagentStart with `agent_type` collapsed to `general-purpose` (the original type is dropped) and the agent's cwd reset to the PRIMARY checkout, not its worktree. One consequence follows:

- **The cwd/git-guard problem is NOT fixed and cannot be from this side.** The resumed agent's cwd is the primary checkout, so the subagent Bash git-policy guard (nexus-3c92m) correctly refuses its `git add`/`commit` there — that guard is doing its job, protecting the shared primary. This is harness behavior (SendMessage resume drops the dispatch-time cwd and type), so there is no fix for it from this side. **The resumed agent itself must account for this when producing its own hand-back**: its Bash cwd is the primary, so a bare `git diff`/`git status` in its own turn reports on the PRIMARY's (usually clean) state, not its worktree's actual changes — its hand-back diff and status must use `git -C <worktree-path> diff` / `git -C <worktree-path> status --short` explicitly (read-only git is guard-exempt regardless of cwd, so this works from inside the resumed agent's own turn).

Prefer, in order: (1) dispatch a FRESH `worktree-developer` (a new Agent call, cwd genuinely inside the worktree) and relay the resumed agent's last-known state (branch, file list, what it had done) in the new dispatch's brief, rather than resuming the old one; (2) if resuming is unavoidable, have the COORDINATOR commit on the resumed agent's behalf, pathspec-limited, after reading its hand-back (`git -C <worktree-path> diff`, per above) — never work around the guard from inside the resumed agent's own turn.

## Sending a Mid-Turn Message to an Agent (RDR-205 Phase 5)

To send a directive or other message to a dispatched agent while it works, `tuple_out` to `mailbox/<agent id>` with a sender-minted nonce; the agent drains its mailbox with `tuple_in` before composing any hand-back, and a resend of the same nonce lands on the same tuple rather than piling up. Full convention, dead-letter rule, and the template shape: `/conexus:mailbox`. Choosing between `SendMessage`, the mailbox and T2, and messaging peer sessions rather than your own agents: `/conexus:peer-messaging`.

## VERIFY Line Convention (MANDATORY)

Every implementation or review hand-back ends with a machine-checkable VERIFY block, one line per claim, never prose (nexus-pjzz8, T2 [21371] §Q5; extended by nexus-cnzei.6 item 1 after a 2026-09-13 measurement: reports arrived on time but made false claims — a docs agent reported changing a CLI default it had not touched, and an implementation agent's gate record omitted the one test that would have caught its own NameError. Delivery was never the gap; verification was).

Three line shapes, any number of lines, in any order:

```
VERIFY: commit=<sha>
VERIFY: <exact command> => rc=<code> <count> passed
VERIFY: t2=<project>/<title>
```

- `commit=<sha>` names the sha the change actually landed on — never "will land on" or a branch name.
- `<exact command> => rc=<code> <count> passed` names the command verbatim (copy-pasteable, not paraphrased), its exit code, and the count it reported.
- `t2=<project>/<title>` names a T2 write-back the orchestrator can `memory_get` to confirm.

An agent with nothing checkable to claim (a pure read, a recommendation with no code touched) says so explicitly rather than omitting the block, so an absent block is never silently indistinguishable from "nothing to verify."

The orchestrator re-runs each reported COMMAND once per round, before accepting the round — it never trusts the reported count. Reading the command line and re-running it yourself is the orchestrator's own job: a fabricated `rc=0 12 passed` line for a command that was never run, or a real but too-narrow command that would not have caught the regression it claims to verify, both read identically from the report alone.

## Review Gate (caller side, MANDATORY on every implementation)

The reviewers cannot dispatch and the developer does not commit, so you drive the gate. Do not stop at the developer's return.

1. **State the acceptance bar** in the relay's Context Notes before either reviewer runs: "Ship when <X>." X is the condition that ends the gate, never "no criticals" (unfalsifiable against an adversarial reviewer). Example: "Ship when the local-upgrade path cannot wedge and the migration walk is proven atomic; anything else becomes a bead."
2. **Run both reviewers**; they catch different, non-overlapping classes. `code-review-expert`: line-level bugs, security, missing edge cases, convention violations. `substantive-critic`: unvalidated assumptions, silent scope reduction, vacuous test assertions, spec-vs-implementation drift. A clean code review never excuses skipping the critic. Brief the critic with the locked spec (RDR `## Decision`, plan, phase boundary).
3. **Round 1 is a full review and reports everything**: recall is the reviewer's job, triage is yours. Triage each finding as a ship-blocker against the bar, or a bead. Never both, never undecided, never re-litigated by a later round.
4. **Round 2 only if a ship-blocker was fixed**: ONE confirmation pass per reviewer (template below), never a second full review. Round 3+ needs the human to ask for it by name (§ Review Rounds).
5. Test-only, comment-only and prose-only fixes do not re-enter review, EXCEPT a test change that weakens, removes or vacuates an assertion: that changes the gate itself and re-enters like a behavioral fix.
6. **Commit only after both reviewers are clean**: explicit paths, never `git add -A`, bead reference, then close the bead. A new check, gate, census or lint pastes its first live run into its bead before close, with the counts it examined and skipped, so a pass over input it never examined is visible when it ships.
7. `test-validator` follows when coverage needs verifying; it is not automatic after Ship.

**Model.** `code-review-expert` defaults to sonnet; pass `model: "opus"` for cycle-level or architectural diffs. Briefing rigour beats model choice: a named-suspect sonnet review outperforms an unbriefed opus one.

**First-pass reviewer brief.** Friendly relays return friendly reviews, so name what to suspect:

- Suspect categories for the diff class: ordering/race for concurrency, lock scope for transactions, vacuity for tests, handshake/boundary shapes for APIs, silent fallback for error paths.
- The locked spec, so the reviewer checks implementation against spec, not style.
- What was NOT changed on purpose, so scope creep is flagged instead of recommended.
- Verification already run, so the reviewer checks claims instead of re-running suites.
- Every added or modified test rated CAN FAIL or CANNOT FAIL, with the concrete production edit that would turn it red.

**Confirmation-pass brief.** It names the findings under confirmation and asks whether each is closed. It does not name new suspect categories or ask the reviewer to pressure-test, enumerate paths or find what was missed; those instructions manufacture findings on any artifact.

```markdown
## Relay: code-review-expert — CONFIRMATION PASS (round 2 of at most 2)

Confirming these findings, and nothing else:
  1. <finding, verbatim from round 1> — fixed at <file:line>

For each: emit CONFIRMED-CLOSED or NOT-CLOSED with the evidence line.
Do not open new attack axes. Observations outside this list go in a final
"For beads" section, one line each; they are never grounds for another round.
ONE exception: a defect INTRODUCED by the fix under confirmation that is
ship-blocking. Report it as a Critical and name the fix line that introduced it.
```

## Implementer and Debugger Dispatch

**Implementer brief checklist.** A brief that says only WHAT to change leaves these to the reviewer, and each cost a fix round when omitted. Answer them in the brief, or write `UNKNOWN: resolve first` and the agent resolves it before its first edit (the SubagentStart hook injects the same five questions into every implementer dispatch):

1. **Consumers.** Every caller and consumer of what changes, and which of them filter, re-rank or page after it.
2. **Final point.** Where the output's real final order or display is decided (after boosts, caps, paging), and that the change acts there, not on a proxy.
3. **Modes.** Behaviour in local and cloud mode, and which was measured. A mode nobody measured is measured first or left unchanged.
4. **Bounds.** Which pool, permit, memory or thread limit the change draws on, and its size against that limit on small and large boxes.
5. **State.** Every cache or shared state touched, who invalidates it, the invalidate-versus-in-flight-fill race, the first-write and empty cases, the test reset hooks.

**Debugger.** Defaults to opus; pass `model: "sonnet"` for a single failure with a clear stack trace. Use `/conexus:serena-code-nav` for symbol-level tracing. Pass a failing test's error, hypotheses and what was tried in the relay.

**Developer escalation.** If the developer returns `ESCALATION: Debugger Required` (scan for `<!-- ESCALATION -->`), its circuit breaker fired. Do not re-dispatch the developer. Guard first: `scratch(action="search", query="circuit-breaker-fired-for-<bead>")`. If found, this is the second firing: stop and report to the user that the debugger's fix did not hold and human investigation is needed. If not found, write that scratch entry (tag `escalation-guard`), then dispatch the debugger with the escalation's Failing test, Error, What I tried, Hypothesis and Diagnostic suggestion fields plus scratch entries tagged `failed-approach`. When it resolves, re-dispatch the developer with the debugger's one-sentence root cause, the files it touched, and "do not retry approaches listed under scratch tag `failed-approach`".

**Link context.** When the work references an RDR or bead, seed T1 scratch tag `link-context` before dispatch so patterns the agent stores to T3 auto-link (`/conexus:catalog`). Skip for ad-hoc work.

## Review Rounds (MANDATORY)

Before each reviewer dispatch, write the round marker:

```bash
nx scratch put "review-round bead=<id> n=<N> bar=<the acceptance bar, one line>" --tags "review,<id>"
```

Include `round N of at most 2` in the reviewer's brief (the cap is `review-rounds.toml`'s, contract `code-review`; plan audits and RDR gates have their own rows there). A reviewer that knows
it is a confirmation pass behaves like one (see § Review Gate above, confirmation-pass brief). N >= 3 requires the human to have asked for it in this
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

See [CONTEXT_PROTOCOL.md § Scope](../../resources/agent-shared/CONTEXT_PROTOCOL.md).
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
- service/ builds: one builder at a time. Share a mutable build-output directory across agents and serialize around it, the way you would a database migration or a generated-code directory — parallelizing it corrupts the build mid-way, it doesn't just race harmlessly. This repo's own mechanics (a build-lease script, the exact env vars, which lint enforces coverage) are nexus-specific and live in this repo's own .claude/rules/releases-and-engine.md § Engine-service release, not restated here.

## Quick Routing

The pipeline order below is `registry.yaml`'s own `pipelines:` section, not a
second copy — this table restates it for quick lookup; `registry.yaml` is
the one place to change it. `feature`/`bug` there always run BOTH reviewers
(non-interchangeable, see § Review Gate above); an "Implement code" pipeline that omits either one is a stale
drift, not a variant.

| Request Type | Primary Agent | Pipeline |
|-------------|---------------|----------|
| Plan a feature | strategic-planner | -> nx_plan_audit -> architect-planner |
| Implement code | developer | -> code-review-expert -> substantive-critic -> test-validator |
| Debug issue | debugger | -> (if cross-cutting) deep-analyst |
| Review code | code-review-expert | -> substantive-critic (always, both reviewers — same gate as "Implement code" above) |
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
(`code-review-expert` + `substantive-critic`, mandatory and non-
interchangeable — see § Quick Routing and § Review Gate above) or to independent-state fork fleets
(disjoint files, no shared mutable state). Both do genuinely more than "a
handful of tool calls" worth of independent work, and serializing either
buys no safety — the opposite case, sharing a mutable resource, IS where
serialization is required: see "service/ builds: one builder at a time"
above (§ Parallel-Orchestration Discipline items) for the concrete example.
See § Parallel-Orchestration Discipline for the fleet-size cap that bounds
the second case.
