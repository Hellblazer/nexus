---
title: "Standing-Code Correctness Review: From Pilot to Decision"
id: RDR-214
type: Technical Debt
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-17
related_issues: [nexus-z2rvr, nexus-8tpw3, nexus-nc08w, nexus-6m9zy, nexus-v1zdu, nexus-thrh9, nexus-0stwc, nexus-r798p]
related_rdrs: [RDR-201]
---

# RDR-214: Standing-Code Correctness Review: From Pilot to Decision

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside the template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

**Provenance.** On 2026-09-17 a session working in a sibling project
(intrastate) ran one fixed review brief over three packages of this repository
as they stood at HEAD, with no diff to look at. The findings became the
remediation plan in T2 `nexus/remediation-plan-intrastate-priors-2026-09-17`,
and most of them were fixed the same day (scorecard: T2
`nexus/remediation-scorecard-intrastate-priors`). The plan's last item said: do
not adopt a cadence for this kind of review until an RDR decides one. This is
that RDR. It is a draft: the evidence is in, the decision is not.

## Problem Statement

Every review this project runs looks at a change. The per-bead reviewers
(`code-review-expert` and `substantive-critic`, the two agents that must both
report before a bead closes) read a diff. The lint bucket (the repo-wide
meta-tests marked `lint`) checks invariants somebody already thought to write
down. Nothing reads code that is not changing and asks whether it is right.

A standing-code review does that: a reviewer is given a list of paths, reads
them as they are, and reports only behaviour that is wrong for a concrete
input. The pilot below shows it finds defects the other two surfaces did not.
What is undecided is whether to run it again, how often, over what, and what
to do with what it finds.

### Enumerated gaps to close

#### Gap 1: No review surface reads code that is not changing

All 33 pilot findings were in code no open change touched. None had been
caught by the 77-file lint corpus or by the per-bead reviews, because neither
looks at standing code. One finding (a multibyte line truncated in
`src/nexus/chunker.py`) was a site that the sweep of a priority-1 bug four days
earlier had not reached.

#### Gap 2: There is no decided cadence or scope

The pilot was one run, chosen by hand, over three packages. Nothing says when
the next one runs, which paths it covers, or when a package is due again. A
practice that depends on someone remembering it is not a practice.

#### Gap 3: Findings have no defined landing place

The pilot's findings went into T2 memory records, then into a plan, then into
beads, by hand, across two sessions. A review whose output is a memory record
nobody is assigned to read produces nothing. The path from a finding to a
bead, with its reproduction attached, needs to be one step.

#### Gap 4: A repeat reviewer can read its own earlier findings

The pilot brief forbade consulting design records, trackers, prior reviews and
memory, so the reviewer judged the code and nothing else. On a second run over
the same package the earlier findings, their beads and their fixes all exist
in stores the reviewer can reach. Without a rule, a repeat run either
re-reports what is known or, worse, trusts a recorded "fixed" without reading
the code.

## Relationship to Prior RDRs

RDR-201 (checked tables) is the nearest relative: it replaced prose rules with
tables a checker proves. It is also one of the three packages the pilot
reviewed, and the pilot found nine defects in its checker and loader. That is
the point of this RDR in miniature: a mechanism built to prevent a class of
error still carried errors only a reader would find.

The per-commit reviewer that once ran on every commit was removed (commit
f17277f48) and is not proposed again here. This RDR is about standing code,
read on a schedule, not about changes.

## Context

### Background

The pilot used one brief
(`~/git/nexus-redo-probes-2026-09-17/REVIEW-BRIEF.md`, not yet tracked in any
repository) on one model. The brief asks for correctness defects only, each
with a concrete failing input, ranked, at most twelve per target, each marked
REPRODUCED (the reviewer ran it), READ (traced in code) or SUSPECTED. It
requires a "considered and dropped" section, which shows where the code was
sound.

### Technical Environment

Python 3.12 package under `src/nexus/`, a plugin surface under `conexus/`, and
release tooling under `scripts/`. Reviews ran read-only against the primary
checkout with scratch probes outside the repository.

## Research Findings

### Investigation

Measured, from the pilot records (T2 `intrastate` entries 26114, 26115, 26119)
and the remediation that followed:

| Target | Non-test lines | Findings | Reproduced | Highest severity | Agent-minutes |
|---|---|---|---|---|---|
| `src/nexus/tables/` and release tables | 5,322 | 9 | 8 | medium | about 7 |
| `rdr.py` and the RDR hook | 5,435 | 12 | 12 | high (1) | about 7.5 |
| indexing pipeline | 19,131 | 12 | 9 | high (1) | about 26 |

That is 29 reproduced defects over 29,888 lines: one per 1,031 lines, at 1.36
agent-minutes per thousand lines, and the cost per line was flat across the
three targets (1.32 to 1.38).

What the remediation added, also measured:

- All ten probes reproduced when re-run by a different session before any fix.
- A plan audit of the remediation plan found six blocking errors in the plan
  itself, including a sweep that omitted the one call site guarding a
  destructive command.
- Fixing the findings produced new defects of its own. Two stacked review
  rounds over the fixes each returned "partial", with nine further findings,
  all fixed. One fix (a new dimension on the RDR lifecycle table) broke seven
  existing tests, which went unseen because the next push cancelled its CI run.

### Key Discoveries

- **Verified**: a fixed brief, run without access to project history, finds
  real, reproducible defects in code that passes every existing gate.
- **Verified**: the larger target, which the reviewer split between two
  sub-reviewers, produced fewer defects per line but both of the targets with a
  high-severity finding were ones the reviewer dug into with probes. The
  effort-asymmetry note is in T2 `intrastate` entry 26124.
- **Documented, not measured here**: whether yield falls on a second pass over
  the same package. One run cannot show that.

### Critical Assumptions

- **Assumed**: the yield of one defect per thousand lines holds for packages
  not yet reviewed. The three pilot targets were chosen because they looked
  rich; an arbitrary package may yield less.
- **Assumed**: the cost stays near 1.4 agent-minutes per thousand lines for a
  different model or a changed brief.
- **Assumed**: findings of this kind are worth more than the remediation they
  trigger costs. The pilot's remediation took one working day of one session
  plus its sub-agents; no cost figure was kept for it, which is itself a gap
  (the sibling project keeps a per-run cost scorecard and this one does not).

## Proposed Solution

Not decided. The options below are what the research phase has to choose
between, with the evidence each one needs.

### Approach

1. **Decide whether to repeat at all.** Run the same brief once over two
   packages nobody expects to be rich. If the yield there is near zero, the
   pilot measured a backlog, and one sweep of the remaining packages, once,
   is the whole answer.
2. **Decide the scope rule.** Candidates: every package once, then only
   packages whose non-test lines changed by more than a threshold since their
   last review; or a fixed rotation. The first needs a record of what was
   reviewed when, which does not exist.
3. **Decide where findings land.** The proposal to test: the reviewer's output
   is a list of bead drafts, each carrying its reproduction, and nothing is
   written to memory stores except a one-line index of the run. A finding with
   no reproduction becomes a bead whose first task is to write one.
4. **Decide the isolation rule for repeat runs.** The proposal to test: the
   reviewer keeps the pilot's rule (no history, judge the code), and a
   separate, cheap step afterwards matches its findings against open and closed
   beads so known items are dropped before anyone reads them.

### Technical Design

To be written once the four decisions above are made. Nothing here needs new
infrastructure: the brief exists, the reviewers are ordinary agents, and beads
are the tracker. If a record of "reviewed when" is wanted, it is one checked
table under `docs/tables/`, not a new store.

### Decision Rationale

Deferred to the research phase.

## Alternatives Considered

### Alternative 1: Do nothing further

The pilot cleared a backlog and the existing gates carry on. This is the
correct choice if the second measurement in Approach item 1 comes back near
zero. It costs nothing and risks the next backlog growing unseen.

### Alternative 2: Review every package on a fixed rotation

Simple to state and to check. At the pilot's cost, the roughly 183,000
non-blank lines under `src/nexus/` (measured 2026-09-17) would take about four
agent-hours per full pass, before any remediation. It spends the same effort on code that has not
changed as on code that has.

### Briefly Rejected

- **A per-commit reviewer.** Removed once already (f17277f48); it reviews
  changes, which the per-bead reviewers already do.
- **More lint tests instead.** A lint checks an invariant someone has already
  named. None of the 33 findings was an instance of a named invariant.

## Trade-offs

### Consequences

- Each run produces remediation work, and the pilot shows remediation produces
  its own defects. A cadence commits the project to that loop.
- Findings in release tooling or the plugin surface ride the release trains
  other sessions own, so a run has to be timed against them.

### Risks and Mitigations

- **A reviewer that reports style as defects.** The brief's rule (state an
  input and a wrong result) held in the pilot: 29 of 33 reproduced.
- **Findings nobody acts on.** Addressed by Gap 3: no run is started without a
  named session to take its beads.

### Failure Modes

A run that finds nothing is ambiguous: the code may be sound or the reviewer
may have examined little. The brief already requires the lines reviewed, the
time spent and a "not reviewed" list; a run is accepted only with those.

## Implementation Plan

To be written after the decisions in Approach. No implementation starts before
this RDR is accepted.

## Test Plan

Not applicable until there is a design.

## Finalization Gate

Not yet run.
