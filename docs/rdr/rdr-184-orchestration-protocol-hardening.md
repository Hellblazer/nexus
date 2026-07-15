---
title: "Multi-Agent Orchestration Protocol Hardening: Completion Reporting, Directive-Race Immunity, and Singleton-Resource Discipline"
id: RDR-184
type: Process
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: ""
created: 2026-07-15
related_issues: []
related_rdrs: [RDR-024, RDR-065, RDR-066, RDR-069, RDR-121]
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

1. **Completed agents idle without reporting** (~8 occurrences).
   Judges, reviewers, and developers finished work — or their
   background jobs finished — and went idle WITHOUT messaging results
   to the orchestrator. Worst case: a **FAILED validation-run RESULT**
   sat unreported until the orchestrator read the agent's raw
   task-output file from `/private/tmp`. An unreported failure is the
   silent-failure class (the project's central defect theme) in
   orchestration form.
2. **Crossed-message races lose scope updates** (~4 occurrences).
   Directives sent while an agent was mid-turn were absent from its
   next hand-back (mailbox delivery lands between turns; the agent
   composes its report against stale scope). Each occurrence required
   a detect-and-resend round-trip; the failure mode if undetected is
   shipping without the update.
3. **Resource collisions from unconfirmed exits** (3 occurrences, one
   integrity near-miss). Three overlapping Docker rehearsal runs shared
   `dist/` and an image tag — a stale wheel could have produced a FALSE
   pass or FALSE fail of a release gate. Separately, orphaned duplicate
   pytest runs violated the one-suite-at-a-time rule and produced a
   phantom 45-minute background task.

## Seed Design (under critique; see §Research)

Full draft: T2 `nexus/design-orchestration-protocol.md`. Summary:

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
- Concurrent invocation of any harness script fails loudly within 1s
  (flock), never runs; verified by test.
- Prompt-token overhead of the protocol text stays small enough that it
  is applied to EVERY dispatch (if it's too heavy to always use, it
  will be skipped — which is failure).

## Research

- Substantive-critic once-over of the seed design: IN FLIGHT
  (pressure-testing ledger-vs-race soundness, aspirational-vs-mechanical
  enforcement layering, per-fix token cost, missed observations,
  artifact-split landmines). Findings land here.
- Interim mitigations already active via orchestrator memory
  (feedback_orchestration_friction_2026_07_15): completion-protocol
  line in every dispatch prompt, ack-by-item on scope updates,
  harvest-on-silent-idle, singleton preflight.

## Decision

(Open — draft. Adoption decision is Hal's after the critique lands.)
