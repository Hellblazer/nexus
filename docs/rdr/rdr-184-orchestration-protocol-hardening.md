---
title: "Multi-Agent Orchestration Protocol Hardening: Completion Reporting, Directive-Race Immunity, and Singleton-Resource Discipline"
id: RDR-184
type: Process
status: accepted
accepted_date: 2026-07-15
priority: high
author: Hal Hildebrand
reviewed-by: "substantive-critic (gate x2 + meta-critique); self"
created: 2026-07-15
related_issues: []
related_rdrs: [RDR-024, RDR-065, RDR-066, RDR-069, RDR-109, RDR-121, RDR-149]
supersedes: []
related_tests: []
---

# RDR-184: Multi-Agent Orchestration Protocol Hardening

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The 2026-07-15 session (6.10.0 → #1402 incident → 6.10.1, ~12h of
multi-agent orchestration) surfaced three recurring failure classes in
the dispatch protocol itself. Hal rated the observations critical. Each
individually cost round-trips; one was a near-miss on result integrity.

#### Gap 1: Completed agents idle without reporting

~8 occurrences (a 9th during this RDR's own research). Judges,
reviewers, and developers finished work — or their background jobs
finished — and went idle WITHOUT messaging results to the orchestrator.
Worst case: a **FAILED validation-run RESULT** sat unreported until the
orchestrator read the agent's raw task-output file from `/private/tmp`.
An unreported failure is the silent-failure class (the project's
central defect theme) in orchestration form.

#### Gap 2: Crossed-message races lose scope updates

~4 occurrences. Directives sent while an agent was mid-turn were absent
from its next hand-back (mailbox delivery lands between turns; the
agent composes its report against stale scope). Each occurrence
required a detect-and-resend round-trip; the failure mode if undetected
is shipping without the update.

#### Gap 3: Resource collisions from unconfirmed exits

3 occurrences, one integrity near-miss. Three overlapping Docker
rehearsal runs shared `dist/` and an image tag — a stale wheel could
have produced a FALSE pass or FALSE fail of a release gate. Separately,
orphaned duplicate pytest runs violated the one-suite-at-a-time rule
and produced a phantom 45-minute background task.

#### Gap 4: The git index is a shared mutable singleton

1 occurrence, live during this RDR's research (finding 6): one agent's
staged-but-unreviewed files were swept into the orchestrator's
unrelated whole-index commit and pushed to develop (backed out
index-only). "Stage by explicit path" does not defend against another
actor's staged content.

## Context

### Background

The nexus project is developed primarily through multi-agent
orchestration: a main session dispatches developers, reviewers, judges,
and researchers (synchronous Agent-tool calls and named/background
teammates), coordinating via the SendMessage mailbox and a shared git
working tree. The 2026-07-15 marathon (6.10.0 release, the GH #1402
incident, 6.10.1) was the largest such session to date and exposed the
protocol's failure classes at production intensity (10 idle-without-
report occurrences by final count — two of them agents working on this
very RDR).

### Technical Environment

- Claude Code hooks: SubagentStart (in use — conexus injects context
  via `conexus/hooks/scripts/subagent-start.sh`), SubagentStop and Stop
  (available; payloads and semantics verified in findings 4-5).
- Dispatch modes: synchronous Agent-tool calls (result returned as the
  tool result) vs named/background teammates (proactive SendMessage
  reporting required — the failing mode).
- Shared resources: one git working tree + ONE git index shared by all
  actors; e2e harnesses with fixed docker tags / sandbox paths; the
  one-pytest-suite-at-a-time rule.
- Artifact split: `conexus/` hook/agent files are plugin-shipped;
  `tests/e2e/` scripts and cc-validation scenarios are repo-local.

## Seed Design (under critique; see §Research)

Full draft: T2 `nexus/design-orchestration-protocol.md` (REVISED v2 per critique — see §Research). v1 summary below retained for history; v2 supersedes:

- **A. Completion protocol** — A1: mandatory terminal step in
  `conexus/agents/_shared/RELAY_TEMPLATE.md` + every agent's Completion
  Protocol section: SendMessage the full result (success/failure/blocked
  + live background task ids) to main BEFORE idling; unreported result =
  defect. A2: orchestrator-side rule — on idle-without-report, harvest
  the agent's task-output files immediately (filesystem is the source of
  truth); nudge only when disk is empty. A3 (optional ratchet):
  SubagentStop hook that detects a final turn lacking a SendMessage-to-
  main and injects a reminder.
- **B. Directive ledger** — B1: orchestrator numbers every directive
  monotonically across a dispatch's lifetime; every hand-back MUST open
  with "Directives received: 1..N; addressed: …; outstanding: …",
  forcing an inbox-vs-work reconciliation at composition time. B2:
  orchestrator refuses hand-backs lacking ledger entries for sent items.
  OPEN QUESTION (critique in flight): does the ledger actually survive
  the observed race (update landing AFTER hand-back composition), or
  does it only make the staleness honest? Candidate strengthening: the
  agent re-checks its inbox as the LAST act before sending the
  hand-back.
- **C. Singleton-resource discipline** — C1: preflight text (assert no
  live process/container of the class; confirm exit before relaunch).
  C2: **mechanical** flock guards in the harness scripts themselves
  (`run.sh`, `local-service-gate.sh`, `upgrade-shakeout.sh`) so a second
  concurrent invocation fails loudly instead of corrupting shared build
  artifacts — per the scripted-not-ambient gates rule.

## Constraints

- **No prose in skills** (standing rule): skill files are re-read every
  turn and must stay terse directives; the protocol text belongs in
  RELAY_TEMPLATE / agent definition files (loaded once per dispatch),
  NOT in always-on skill bodies. The design must respect this split.
- Artifact split: `conexus/` agent/template files are plugin-shipped
  (changes ride a plugin release); harness scripts are repo-local
  (land on develop immediately). A hook (A3) is a settings/plugin
  decision with its own noise budget.
- No new orchestration framework; work within current mailbox/idle
  semantics of the Agent/SendMessage tools.

## Success Criteria

- Zero idle-without-report occurrences across a comparable
  multi-agent session (measurable from teammate-message transcripts).
- Zero lost scope updates: every mid-flight directive is either in the
  hand-back's addressed list or explicitly outstanding — never absent.
- Concurrent invocation of any guarded harness script fails loudly
  within 1s (mkdir-based lock per finding 3), never runs; verified by a
  concurrent-invocation test per script.
- Zero foreign files in any orchestrator commit: every commit in a
  multi-agent session is pathspec-limited, and no commit's file list
  exceeds its stated scope (retro-checkable from `git log --stat` of
  the session's commits) — Gap 4's measurable criterion.
- Prompt-token overhead of the protocol text stays small enough that it
  is applied to EVERY dispatch (if it's too heavy to always use, it
  will be skipped — which is failure).

## Alternatives Considered

1. **Transcript-heuristic report-owing discrimination** (any SendMessage
   use = owes a report) — REJECTED at gate: misclassifies the primary
   case (zero-SendMessage background teammate -> never blocked) and
   false-blocks sync dispatches that used SendMessage mid-run
   (gate critique Criticals/Significants). Replaced by dispatch-time
   declaration.
2. **Own-session Stop hook with a teammate session marker** (finding 5
   proved it fires) — viable, not chosen: requires marker plumbing into
   every teammate session and hooks firing in N sessions instead of one
   spawner-side point; kept as the documented fallback if the
   expectations-file mechanism proves unreliable in Phase 1.
3. **Prose-only protocol (v1 design)** — rejected by the design critique:
   unenforced text in every dispatch degrades under context pressure;
   the observed failures happened WITH instructions present. Project-local
   precedent (meta-critique 2026-07-15): the daemon-lifecycle class
   consumed ~10 RDRs of tier-local prose fixes before RDR-149 ended it
   with a shared primitive + conformance suite + lint; RDR-109's
   mode-lint promotion promise sat unkept 2+ months until an incident
   forced today's exact-count ratchet. In this codebase, prose
   commitments decay silently; mechanized ones (ratchets, conformance
   suites, hooks) hold.
4. **SubagentStop reminder (non-blocking)** — rejected: highest-frequency
   failure warrants the mechanical ratchet; block-once with the
   stop_hook_active guard bounds the cost (verified 21c).
5. **flock-based harness locks** — rejected: flock absent on darwin
   (finding 3); mkdir chosen.
6. **Agent-side self-reported directive ledger** (v1) — rejected: cannot
   report unread mail; replaced by orchestrator-side send-log diffing +
   final-inbox-poll row.
7. **Surface-narrowing: sync-only dispatch for shared-state work**
   (meta-critique 2026-07-15) — PARTIALLY ADOPTED for Gaps 2/4. Finding 4
   already establishes synchronous dispatches are structurally immune to
   Gaps 1/2 (result returns as the tool result; no mailbox race window).
   Dispatch policy: work that will stage/commit files in the shared tree
   or is likely to receive mid-flight scope updates routes through
   synchronous dispatch by default; background/named teammates are
   reserved for genuinely parallel work on non-shared state (worktree
   isolation, read-only review, independent harness runs). Not adopted
   as a hard ban: long-running suites and watch-style tasks legitimately
   need background mode — for those the Gap-1/2 mechanisms plus the
   tripwires below are the defense.

## Trade-offs

- **Blocking hook**: guarantees a report or a bounded extra turn; costs
  one extra turn on true positives and risks noise on
  misclassification — bounded by the once-guard and gated on Phase-1
  scenarios 21d/21e + aggregate measurement (RDR-069 spike-before-ship
  precedent).
- **Expectations file**: one more session artifact to write/clean; in
  exchange, ground-truth discrimination with zero inference. Stale
  entries (orchestrator crash) expire with the session directory.
- **Expectations-file declaration (Gap 1)**: the dispatch-time write is
  itself a manual orchestrator step — the same discipline-failure class
  this RDR treats, relocated (agent-forgets-to-report becomes
  orchestrator-forgets-to-declare). Residual risk accepted for Phase 1
  because the SubagentStart-stamp variant may close it mechanically
  (Assumption (b)); mitigated meanwhile by the Phase-2
  declaration-completeness audit and write-BEFORE-dispatch ordering
  (below). A missed declaration degrades to today's behavior (no
  block), never worse.
- **Orchestrator-procedural fixes (Gap 2, Gap 4 orchestrator side)**:
  bind via memory directives, not mechanism. Accepted: the mechanical
  alternatives (message middleware, commit hooks) exceed proportionality
  for a single-user workflow today. BUT — per the meta-critique — the
  "revisit if retros show recurrence" conditional is itself the promise
  shape that decayed twice in this project (RDR-109 mode-lint, the
  pre-RDR-149 daemon-lifecycle class), so Gaps 2/4 get cheap TRIPWIRES
  now rather than a deferred intention: (a) a commit-scope audit helper
  (Phase 0) that lists every session commit's file set from
  `git log --stat` for foreign-file detection, and (b) a crossed-resend
  counter (T1 scratch, incremented at each detect-and-resend). Escalation
  threshold, concrete: ANY foreign-file commit, or >2 crossed-resends in
  a session, triggers mechanization (commit hook / middleware) as a
  follow-on bead — not another retro note. Surface-narrowing
  (Alternative 7) shrinks the exposed window in the meantime.
- **Plugin-shipped hook**: rides a plugin release; interim protection is
  the memory-directive discipline already active.

## Implementation Plan

- **Phase 0 (repo-local, immediate)**: mkdir lock helper + guards in the
  four audited harnesses (finding 2) + per-script concurrent-invocation
  tests; plus the Gap-4 commit-scope audit helper (session-ranged
  `git log --stat` file-set listing for foreign-file detection).
  Independent of the plugin cycle.
- **Phase 1 (plugin)**: expectations-file write path (orchestrator
  dispatch convention + SubagentStart stamp if payload allows) — the
  write happens BEFORE the dispatch call, never after, closing the
  fast-stopping-teammate race;
  subagent-stop.sh blocking hook; the two heredoc rows; cc-validation
  scenarios 21d/21e + aggregate false-block measurement on a real
  session. Default-on ONLY after 21d/21e pass and the measured
  false-block rate is acceptable to Hal.
- **Phase 2 (process)**: session-retro checklist entries (directive-diff
  audit, commit pathspec audit, declaration-completeness audit — diff
  the expectations file against the session's actual background/named
  dispatches) added to the continuation/retro skill;
  Gap-2 and Gap-4 orchestrator disciplines measured against the Success
  Criteria in the next comparable multi-agent session.

## Finalization Gate

- **Contradiction check**: Decision item 1 no longer relies on the
  transcript heuristic its own finding never verified; Success
  Criteria no longer name flock; each of the four gaps has a decision
  item, a success criterion, and either a finding or an explicit
  Phase-1/Phase-2 verification (Gap 2 and Gap 4 are
  procedural-with-retro-verification, stated as such in Trade-offs).
- **Assumption verification**: (a) false-block rate acceptable —
  UNVERIFIED, gated in Phase 1 (21d/21e + measurement) before
  default-on; (b) SubagentStart can see background-ness — UNVERIFIED,
  non-load-bearing (orchestrator-side expectations write is the
  fallback and is sufficient alone); (c) all other load-bearing claims
  carry findings 1-6.
- **Scope verification**: no engine/service changes; no new
  orchestration framework; plugin surface limited to one hook + two
  heredoc rows; repo surface limited to the four audited harnesses +
  scenarios. Forked-skill/nested dispatches: out of scope (platform
  disallows subagent-spawned subagents today; revisit if that changes).
- **Cross-cutting**: consistent with RDR-121 (mechanical backstop for
  soft guidance; different hook event, same philosophy — an explicit
  override token is intentionally omitted because the once-guard
  auto-releases, unlike RDR-121's permanent blocks); RDR-069 precedent
  for spike-before-ship measurement.
- **Proportionality**: three text rows + one hook script + one lock
  helper + scenarios, against a measured cost of one 12-hour session's
  compounding failures and one near-miss on release-gate integrity.

## Research

- **Substantive-critic once-over: COMPLETE (2026-07-15), verdict
  not-ready-as-written; all findings verified against repo files and
  folded into the REVISED seed design (T2 v2).** Key findings:
  1. A1 targeted the wrong artifact — the live injection path is
     `conexus/hooks/scripts/subagent-start.sh`'s inline heredoc, which
     deliberately replaced RELAY_TEMPLATE.md dumping ("Keep compact");
     protocol lines belong there, template stays documentation.
  2. The self-report directive ledger CANNOT close the observed race:
     an agent can't report a directive it never read. Replaced with
     orchestrator-side send-log diffing (ground truth) + a mandatory
     agent-side final inbox poll before hand-back composition.
  3. `flock` does not exist on macOS (verified) — the dev platform.
     Lock mechanism replaced with mkdir-based atomic locking.
  4. Dispatch-mode conflation: synchronous Agent-tool dispatches return
     results as the tool result (idle-without-report impossible); all
     Failure-1/2 fixes scope to named/background teammates only.
  5. SubagentStop should be the LOAD-BEARING mechanical fix for
     Failure 1 (the Start-hook infra + cc-validation pattern already
     exist; Stop hooks can `{"decision": "block"}`, not just remind).
  6. The harness audit surface is ≥6 scripts (incl. tests/e2e/gc-ab/
     run-ab.sh, zero guard today), not the 3 named anecdotally —
     full-surface inverse audit required before scoping the lock bead.
  7. A2/B2 as skill prose would violate the standing no-prose-in-skills
     rule — cut; they live as orchestrator memory directives + the hook.
  8. Every fix needs a verification method (cc-validation scenario for
     the Stop hook; concurrent-invocation shell test for the locks).
- OPEN (needs empirical research before gate): can SubagentStop's input
  payload discriminate teammate vs sync dispatches and detect a final
  SendMessage-to-main; false-block noise budget on a real session.
- Interim mitigations already active via orchestrator memory
  (feedback_orchestration_friction_2026_07_15): completion-protocol
  line in every dispatch prompt, ack-by-item on scope updates,
  harvest-on-silent-idle, singleton preflight.

## Research Findings

1. **[VERIFIED 2026-07-15] Injection path** — live subagent injection is
   `conexus/hooks/scripts/subagent-start.sh:139-153`: a compact inline
   `RELAY` heredoc TABLE ("was: awk-truncated RELAY_TEMPLATE.md. Keep
   compact"). The v2 protocol lines fit as two table rows (Completion:
   SendMessage full result to main before idling; Inbox: re-check inbox
   immediately before composing a hand-back). RELAY_TEMPLATE.md stays
   documentation. Method: direct file read.

2. **[VERIFIED 2026-07-15] Harness-surface audit (lock scope)** — full
   tests/e2e enumeration (~20 scripts). Fixed shared mutable resources
   (the lock scope): migration-rehearsal/run.sh (fixed docker tag +
   shared dist/ — the observed near-miss site); gc-ab/run-ab.sh (named
   containers/network + shared out/, zero guard); release-sandbox.sh
   (fixed ~/nexus-sandbox + tmux session name); upgrade-shakeout.sh
   (fixed ~/nexus-upgrade-sandbox). Already mktemp-isolated:
   local-service-gate.sh (singleton-by-policy only), index-throughput-
   bench, scenarios/*. Method: pattern sweep across every script.

3. **[VERIFIED 2026-07-15] mkdir atomic locking works on darwin AND
   debian:trixie-slim** (live execution both platforms). POSIX-atomic,
   no flock (absent on darwin). Design consequence: needs stale-lock
   handling (pid + liveness inside the lockdir) — no auto-release on
   crash.

4. **[DOCS-RESEARCHED 2026-07-15; one claim gates on empirical
   verification] SubagentStop payload + block semantics** (official
   docs + hooks reference + GH #20221): payload carries agent_id/
   agent_type/last_assistant_message/transcript access/stop_hook_active;
   `{"decision":"block","reason":...}` confirmed (re-runs the subagent
   with the reason; server-side block threshold; docs prefer
   additionalContext over blocking). **CRITICAL CAVEAT: the research
   indicates SendMessage-addressed background teammates do NOT fire
   SubagentStop, and the payload lacks a teammate-vs-sync
   discriminator.** If confirmed, F1-PRIMARY moves to a Stop hook in
   the TEAMMATE'S OWN session (plugin hooks apply there) with a session
   marker; the final-turn SendMessage detection transfers unchanged.
   GATE ITEM → resolved by finding 5. Session evidence footnote:
   idle-without-report occurrence #9 was the research agent for this
   very finding.

5. **[VERIFIED 2026-07-15, empirical — cc-validation scenario 21, all
   three legs PASSED] Stop-event topology + block round-trip.**
   - 21a (sync control): SubagentStop fires for plain Task dispatch;
     observed payload fields: session_id, transcript_path, cwd,
     prompt_id, permission_mode, agent_id, agent_type, effort,
     hook_event_name, stop_hook_active, **agent_transcript_path**.
   - 21b (background teammate): **BOTH SubagentStop (spawner side) AND
     the teammate's own-session Stop fired** — finding 4's docs caveat
     is REFUTED; both hook points are available. Design consequence:
     a single SubagentStop hook covers sync AND background dispatches,
     so the needed discriminator is not "which event" but "does THIS
     dispatch owe a report" — sync dispatches return results as the
     tool result (blocking them = false-block); the hook must inspect
     `agent_transcript_path` (present, verified) for a final-turn
     SendMessage only when the dispatch is report-owing, or the
     own-session Stop hook can be used with a teammate session marker.
     Either mechanism is now proven viable; choice is a design-phase
     trade (single spawner-side hook vs per-session marker).
   - 21c (block round-trip): `{"decision":"block","reason":...}` from a
     Stop hook with a `stop_hook_active` once-guard: session continued,
     complied with the reason, then stopped cleanly. The production
     block-once pattern works as documented.
   Scenario: tests/cc-validation/scenarios/21_stop_event_topology.sh
   (kept — it IS the regression test finding 8's verification
   requirement asked for).

6. **[LIVE INCIDENT 2026-07-15, same session] Fourth failure class:
   the git INDEX is a shared mutable singleton.** While recording
   finding 5, the orchestrator's `git commit` swept in another agent's
   staged-but-unreviewed in-progress work (8 files, ~840 lines) —
   staging by one actor + whole-index commit by another pushed
   unreviewed code to develop (5fa9a473; backed out index-only in
   ed9fdc37, working tree preserved). "Stage by explicit path" does
   not protect against ANOTHER actor's staged content. Mechanical
   fixes for the design: (a) agents in a shared tree never `git add`
   (hand-back is diff-only); (b) orchestrator commits use
   pathspec-limited `git commit -- <paths>` (commits named paths
   regardless of index) or assert `git diff --cached --name-only`
   equals the intended set pre-commit. Method: lived it.


## Decision

(Proposed — pending gate + Hal's accept.)

Adopt the v2 seed design with the hook-point choice settled by finding 5:

1. **Gap 1 (report-before-idle)** — ONE `SubagentStop` hook (fires for
   both sync and background dispatches; finding 5). Report-owing-ness is
   DECLARED at dispatch time, never inferred from transcript heuristics:
   the dispatching side records an expectation (`agent_id` -> owes-report)
   in a session-scoped expectations file (written by the orchestrator at
   dispatch, and/or by the SubagentStart hook if the start payload
   exposes background-ness — an implementation-phase determination). The
   SubagentStop hook consults the file: agents NOT listed are never
   blocked (sync dispatches stay unblockable even if they used
   SendMessage mid-run — kills the gate critique's Significant-3 false
   block); agents listed are blocked once (`stop_hook_active` guard;
   round-trip verified, 21c) when their final turn lacks a
   SendMessage-to-main (checked via `agent_transcript_path`, verified
   present). This catches the zero-SendMessage background teammate — the
   gate critique's Critical-2 case — by construction, because listing
   happens at dispatch, not by traffic. **Phase-1 gates before
   default-on: scenario 21d (zero-SendMessage background teammate gets
   blocked, then complies) and 21e (sync dispatch that used SendMessage
   mid-run is NOT blocked), plus an aggregate false-block measurement on
   a real session.** Plus the two terse table rows in
   subagent-start.sh's heredoc (finding 1).
2. **Gap 2 (directive races)** — orchestrator-side send-log diffing +
   the final-inbox-poll heredoc row; ledger header retained only as the
   diff's input format. Orchestrator behaviors bind via the durable
   memory directive, not skill prose. Update-likely work prefers
   synchronous dispatch (Alternative 7); each detect-and-resend
   increments the T1 crossed-resend counter, and >2 in a session trips
   the mechanization escalation (Trade-offs).
3. **Gap 3 (singleton resources)** — mkdir-based lock helper (verified
   both platforms; stale-lock pid+liveness handling) across the four
   audited fixed-resource harnesses (finding 2), each with a
   concurrent-invocation regression test.
4. **Gap 4 (shared index)** — agents in a shared tree never `git add`
   (heredoc row); orchestrator commits are pathspec-limited
   (`git commit <paths>`) — both already binding via the memory
   directive; the heredoc row makes the agent side injected rather
   than remembered. Commit-adjacent work prefers synchronous dispatch
   or worktree isolation (Alternative 7); the Phase-0 commit-scope
   audit helper is the tripwire, and ANY foreign-file commit trips the
   mechanization escalation (Trade-offs).

Verification set: cc-validation scenario 21 (standing), a new scenario
for the production Stop-hook behavior, and the per-harness lock tests.
Plugin-shipped artifacts (hook + heredoc rows) ride the next plugin
release; repo-local locks land immediately.

## Revision History

- 2026-07-15 — Draft from the 6.10.0/6.10.1 marathon's orchestration
  observations; v1 seed design critiqued (8 findings), revised to v2.
- 2026-07-15 — Research findings 1-6 (injection path, harness lock
  surface, mkdir locking, stop-event docs, empirical scenario 21,
  live git-index incident).
- 2026-07-15 — Gate round 1 BLOCKED (2 Critical): section set completed;
  transcript-heuristic discriminator replaced with dispatch-time
  expectations-file declaration.
- 2026-07-15 — Gate round 2 PASSED (0C/1S/4O); Significant-1
  (declaration-discipline residual risk) folded in same day.
- 2026-07-15 — Meta-critique folds: RDR-149/RDR-109 precedent cited in
  Alternative 3; Alternative 7 (sync-only surface narrowing) partially
  adopted for Gaps 2/4; Gap-2/4 tripwires with a concrete escalation
  threshold replace the "revisit if retros show recurrence" conditional.
