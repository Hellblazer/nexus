---
id: RDR-068
title: "Composition Failure Detection (Research)"
type: research
status: draft
priority: P3
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-10
accepted_date:
related_issues: ["RDR-065"]
---

# RDR-068: Composition Failure Detection (Research)

> Stub. **Research RDR**, not an implementation RDR. Phase 1 mines
> incidents to extract a regex bank; implementation phase is gated on
> the regex bank having sufficient precision to avoid training agents
> to dismiss its prompts.

## Problem Statement

ART's INT-3 (mid-session workaround gating) is the most direct
intervention against the silent scope reduction failure mode: when a
composition failure is detected mid-session, the agent stops and asks
the user to choose between "reopen the dependency chain" or "apply a
workaround," instead of defaulting silently to the workaround.

The intervention requires a **detector** for composition failures. ART
gestured at examples ("dim mismatch, NPE through an untested
composition, API mismatch") but did not provide a precise vocabulary.
Without a precise detector, INT-3 has two failure modes:

- **Too sensitive**: fires on every test failure, including legitimate
  bugs. Agent learns to dismiss the prompt; the gate becomes ceremonial.
- **Too loose**: misses real composition failures. The intervention
  doesn't catch the pattern it was built to catch.

This RDR is the **research** that has to happen before INT-3 can be
implemented responsibly. The deliverable is a regex bank (or pattern
classifier) extracted from real incidents, with measured precision and
recall against a held-out test set.

### Enumerated gaps to close

#### Gap 1: No vocabulary for "composition failure"

We do not currently have a precise definition of what counts as a
composition failure vs. a regular bug. ART's examples are illustrative,
not exhaustive. The vocabulary has to be extracted from real incidents
— there is no prior art we can lift directly.

#### Gap 2: No regex bank or pattern classifier exists

Even with a vocabulary, we do not have the actual patterns that would
match composition failures in test output. These have to be authored
from incident data and validated against held-out cases.

#### Gap 3: No mechanism for measuring detector precision

Without measured precision, we cannot make a responsible decision about
whether to ship INT-3. The first run of INT-3 against a noisy detector
would train the agent and the user to dismiss the prompt. We need a
held-out test corpus and a precision measurement before any production
hook fires.

## Context

### Background

This RDR is one of three siblings to RDR-065 — see RDR-065 §Context for
the full sibling landscape. This sibling is the **research-only** track:
its deliverable is data and documentation, not code that ships.

The data sources for this research are:

- **ART project incidents**: RDR-073 (CogEM Training Deployment, the
  triggering incident), RDR-066 (instar gate fix), RDR-075 (top-down
  feedback DEFERRED), and any other ART RDR with divergence language in
  its post-mortem
- **Nexus project incidents**: to be discovered by auditing
  `docs/rdr/post-mortem/*.md` and `knowledge__rdr_postmortem__nexus`
  for divergence language
- **Other LLM-driven projects**: as the `rdr_process` T2 collection
  grows (see RDR-067)

### Why deferred

The research itself can begin once RDR-065 ships, but should not begin
sooner. RDR-065's close-time funnel will produce its own data on which
failures slip through, and that data is the most relevant input to the
INT-3 detector. Starting the regex bank from ART data alone would bias
the detector toward ART-specific patterns.

### Drift condition

**If RDR-068 has not moved from `draft` into Phase 1 mining within
180 days of RDR-065 closing as `implemented`, reopen RDR-065 and
re-evaluate its close reason.** The 180-day window (longer than the
other siblings) exists because this is a research RDR and the
corpus takes time to accumulate — by 180 days, RDR-065 should have
produced at least 3-5 close-time incidents to add to the ART corpus,
making the regex bank authoring tractable. If no incidents have
accumulated by then, either RDR-065 is working perfectly (unlikely
to be silently true) or its data collection is broken (worth
investigating).

Also: if Phase 1 completes and the regex bank precision measures
below 60% on a held-out test set, this RDR is **explicitly
abandoned**, not deferred. Shipping a noisy detector trains agents
to dismiss its prompts, which is worse than not shipping. The
abandonment decision is a first-class outcome of the research, not
a failure.

### Technical Environment

- **Test failure detection**: INT-3 requires a hook that fires on test
  output, NOT on agent self-reports. The right mechanism is a
  PreToolUse or PostToolUse hook on `Bash` invocations that match
  pytest/cargo/jest/etc. patterns
- **Regex bank storage**: TBD — could be a YAML file in the plugin, a
  T2 entry, or hardcoded into the hook script
- **Decision prompt**: would use the same "ask user explicit
  confirmation" pattern that `nx:rdr-close` already uses for open-bead
  gating (lines 75-78 of `nx/skills/rdr-close/SKILL.md`)
- **No existing composition-failure detection machinery**: this is
  greenfield

## Research Findings

[Pending. Phase 1 mines incidents.]

### Investigation plan (Phase 1)

1. **Inventory ART incidents.** Read ART's RDR-073, RDR-066, RDR-075
   post-mortems. Extract every test failure description and classify
   it as composition failure vs. regular bug. Record the test output
   text verbatim where available.
2. **Inventory nexus incidents.** Grep `docs/rdr/post-mortem/*.md` for
   divergence language. Identify cases where the RDR's failure mode
   matches the silent-scope-reduction pattern. Extract test output text.
3. **Build a test corpus.** Combine the two inventories into a labeled
   corpus: composition failure (positive) vs. regular bug (negative).
   Aim for ≥20 of each.
4. **Author candidate patterns.** Hand-write regex patterns from the
   composition-failure positives. Examples (illustrative, not final):
   - `IllegalArgumentException.*dimension|shape|size mismatch`
   - `ValueError.*expected.*got`
   - `AttributeError.*'NoneType' object has no attribute` in code paths
     that traverse ≥2 modules
   - `TypeError.*incompatible.*types`
5. **Measure precision and recall.** Run the regex bank against the
   corpus. Hold out 30% as a test set. Compute precision (rate of true
   positives among all matches) and recall (rate of true positives
   among all positives).
6. **Iterate.** Refine patterns until precision ≥80% and recall ≥60%.
   These thresholds are starting points; adjust based on what the data
   says.

### Critical Assumptions

- [ ] **CA-1**: Composition failures have distinguishable surface
  signatures in test output. If they look identical to regular bugs,
  the regex bank cannot exist and INT-3 must be redesigned around a
  different signal (e.g., AST analysis, dependency graph).
  — **Status**: Unverified — **Method**: Phase 1 inventory + corpus
  construction
- [ ] **CA-2**: The ART incident corpus alone is sufficient to seed the
  regex bank. May need to wait for nexus incidents or third-project
  incidents before the bank generalizes.
  — **Status**: Unverified — **Method**: Phase 1 measurement
- [ ] **CA-3**: Phase 2 (implementation) is worth doing. If Phase 1
  produces a regex bank with precision <60%, Phase 2 should be
  abandoned and INT-3 should be either redesigned or dropped.
  — **Status**: Unverified — **Method**: Phase 1 outcome

## Proposed Solution

[Pending. Phase 1 deliverable is the regex bank; Phase 2 is the hook
implementation, conditional on Phase 1 outcome.]

### Sketched approach (Phase 2, conditional)

1. Package the regex bank as a YAML file in the nexus plugin.
2. Build `nx/hooks/scripts/composition-failure-guard.sh` as a
   PreToolUse or PostToolUse hook on `Bash` invocations matching
   pytest/cargo/jest/etc. The hook reads the test output, applies the
   regex bank, and on a match raises a structured prompt to the user
   with the same pattern as the existing open-bead gate.
3. The prompt offers two paths: reopen the responsible bead, or apply a
   workaround (with explicit acknowledgment that a follow-up bead with
   commitment metadata — see RDR-065 Gap 3 — must be created).
4. Log every prompt to a T2 entry for later precision measurement
   in production.

## Alternatives Considered

### Alternative 1: Skip the regex bank, use a one-shot LLM classifier

**Description**: instead of authoring patterns, dispatch a small LLM
call on every test failure to classify it as composition failure vs.
regular bug.

**Pros**: zero authoring effort; classifier improves over time.

**Cons**: cost per test failure; latency on the close path; LLM
classifier is itself subject to context bias (the same problem we're
trying to solve at the agent level); no way to audit decisions.

**Reason for tentative rejection**: defer until regex bank approach is
proven insufficient. The regex bank is auditable; the LLM is not.

## Trade-offs

[Pending.]

## Implementation Plan

[Pending. Phase 1 is the entire current scope.]

## References

- ART canonical writeup: `~/git/ART/docs/rdr/meta/RDR-PROCESS-FAILURE-MODE-silent-scope-reduction.md`
- T2 entry (cross-project): `rdr_process/failure-mode-silent-scope-reduction`
- ART RDR-073 (triggering incident — primary corpus source)
- ART RDR-066 (instar gate fix — secondary source)
- ART RDR-075 (top-down feedback deferred — secondary source)
- RDR-065 (close-time funnel sibling — produces nexus-side data this
  RDR will mine)
- RDR-066 (enrichment-time sibling)
- RDR-067 (cross-project observability sibling — provides the corpus
  this RDR mines once it grows)
- Existing pattern reference: `nx/skills/rdr-close/SKILL.md` lines
  75-78 (open-bead hard-gate prompt — model for the INT-3 prompt)

## Revision History

- 2026-04-10 — Stub created as deferred research sibling to RDR-065.
