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
three targets (1.32 to 1.38). The one-per-1,031 figure is a floor: two of the
three reports hit the brief's cap of 12 findings (see the withdrawal below).

What the remediation added, also measured:

- All ten probes reproduced when re-run by a different session before any fix.
- A plan audit of the remediation plan found six blocking errors in the plan
  itself, including a sweep that omitted the one call site guarding a
  destructive command.
- Fixing the findings produced new defects of its own. Two stacked review
  rounds over the fixes each returned "partial", with nine further findings,
  all fixed. One fix (a new dimension on the RDR lifecycle table) broke seven
  existing tests, which went unseen because the next push cancelled its CI run.

### Second run: code chosen for being unpromising (2026-09-17)

Approach item 1 asked whether the pilot had only measured a backlog in
packages picked because they looked rich. The same brief, unchanged, was run
at HEAD 92e3de0f7 over two targets picked for the opposite reason:
`src/nexus/daemon` (governed by a conformance suite) and seven quiet modules
with little recent change. The reviewers had no access to memory, the tracker
or the RDRs. A prediction and a decision rule were recorded before either
result arrived (T2 `nexus_rdr/214-research-1`); the result is
`nexus_rdr/214-research-2`. Every reproduced finding was re-run by a second
session from the reviewer's own probe scripts.

| Target | Lines | Findings | Reproduced | High | Agent-minutes |
|---|---|---|---|---|---|
| `src/nexus/daemon` | 6,980 | 12 | 7 | 2 | about 7 |
| seven quiet modules | 3,828 | 12 | 10 counted (1 disputed) | 1 | about 5.5 |

The prediction was 5 reproduced and no high-severity finding. It was wrong on
both: 17 and 3.

**Withdrawn.** This section first concluded that unpromising code yielded more
defects per line than the pilot's targets (one per 636 lines against one per
1,031). That conclusion does not stand. The brief capped each report at 12
findings, and five of the six passes returned exactly 12. A per-line rate
computed from a capped count is mostly the target's line count divided by 12.
What the run does show is that both unpromising targets filled the cap. The
uncapped measurement is in the recapture section below
(T2 `nexus_rdr/214-research-4`).

Three things in the detail matter for the design:

- The reviewer under-called its most serious finding. It reported that a
  search query beginning `--pre=` might make ripgrep run a program and marked
  that unconfirmed. The second session deleted a stale marker file and re-ran
  the probe; the program ran. Severity came from the re-run, not the review.
  Fixed the same hour (commit 2c2b1b045).
- Two findings were siblings of defects fixed earlier the same day, in files
  those fixes' sweeps did not reach: a duplicate tail chunk in the markdown
  chunker (the PDF chunker was fixed in 482c4bb57) and a `git log` without
  `-z` in frecency (the index path was fixed in a8413b364).
- One finding was disputed and left out of the count: a test pins the
  behaviour as the contract. A reviewer that may not read history cannot know
  that, which is what Gap 4's matching step is for.

### Second pass over a remediated package (2026-09-17)

`src/nexus/commands/rdr.py` and the RDR hook were reviewed a second time after
all 12 first-pass findings, and about ten more from review rounds, had been
fixed. Prediction recorded first (T2 `nexus_rdr/214-research-3`): 4 reproduced,
none high. Result: 12 reproduced, 1 high, none a repeat of the first pass, none
matching any bead, about 3 in code written during that day's fixes. The
prediction was wrong, but the count is capped like the others, so it shows only
that one capped pass does not exhaust a package
(T2 `nexus_rdr/214-research-4`; beads under epic nexus-u1jxt).

### Recapture: a second pass over unfixed code (2026-09-17)

To replace the capped counts, the daemon package and the seven quiet modules
were each reviewed again at HEAD d8d45f761 by a fresh isolated reviewer under
the same brief with the cap removed (pass B, one reviewer per target). Pass A
is not a new run: it is the original capped pass on each target, reused as
the first sample, with the quiet-module pass's eight lower-ranked findings
included. Nothing was fixed between the passes except the ripgrep argument
fix, which is left out of the match. Findings were matched by hand. The
overlap between the two passes estimates the population they draw from
(Lincoln-Petersen: first-pass count times second-pass count, divided by the
overlap). Because pass A was capped, it is a truncated sample: that pushes
the overlap up and the estimate down, on top of the same-model correlation
noted below.

| Target | Lines | Pass A | Pass B (uncapped) | In both | Estimated population | Known so far |
|---|---|---|---|---|---|---|
| seven quiet modules | 3,834 | 19 | 46 | 17 | 51 | 48 |
| `src/nexus/daemon` | 6,980 | 12 (capped) | 26 | 10 | 31 | 28 |

Pass B took about 6 and 11 agent-minutes. Its findings were not re-run by a
second session. Result of record: T2 `nexus_rdr/214-research-5`.

What this shows:

- The set of defects this reviewer can see is finite, and one uncapped pass
  finds most of it. Pass B alone re-found 17 of 19 and 10 of 12, and held 46
  of the 48 and 26 of the 28 findings known after both. A second pass over an
  unchanged package added 2 to each: 4 and 8 percent of what one pass found.
- The estimate is a floor. Both passes are the same model under the same
  brief, so what one misses the other tends to miss. The figure bounds what the
  instrument sees, not how many defects the code has.
- Severity labels reproduced poorly. Of the 27 overlapping findings, 19
  carried a label on both sides (the quiet-module pass A's eight lower-ranked
  findings carry none). Of those 19, 11 kept their label and 8 moved,
  including both of pass A's highs: the defect behind the P1 bead
  nexus-cd1k0.1 (stop waits on a zombie, so the supervisor is killed and the
  operating system restarts the stack) went from high to low, and the
  fenced-supervisor defect from high to medium; two lows moved up to medium.
  Pass B's one high finding was absent from pass A. This is a small sample,
  and it says the label moves on about two findings in five, not that it is
  arbitrary. The finding reproduces; its label is unreliable as a sort key.
  (Census: T2 `nexus_rdr/214-research-6`.)
- Most of what uncapping added is low severity: 28 of 46 and 17 of 26, largely
  functions documented as never raising that raise on a malformed local file.

### What a finding affects

Severity says how wrong a result is. It does not say what the wrong result
touches, and a high-severity defect in this project's own RDR tooling is not
comparable to a high-severity loss of a user's indexed content. The 69 ranked
findings from the six capped passes, sorted by what each affects:

| Affects | Count |
|---|---|
| Security: something runs or escapes that should not | 1 |
| The user's indexed content: missing, truncated or never retried | 8 |
| Running the service: start, stop, upgrade, configuration | 16 |
| Search quality: worse results, nothing lost | 10 |
| This project's development-process tooling (RDR gates, hooks) | 21 |
| Release tooling (checked tables, choreography) | 8 |
| Messages and diagnostics only | 5 |

Half (34) touch only process tooling, release tooling and messages. That share
reflects where the reviewer was pointed (three of six passes), not the
codebase. Pass B sorted the same way: quiet modules 22 search quality, 20
service and configuration robustness, 4 indexed content; daemon 22 service
operations, 4 data-integrity risk. Unlike the severity label, this attribute
follows from the file and the failure scenario, so it can be assigned by rule.

### Key Discoveries

- **Verified**: a fixed brief, run without access to project history, finds
  real, reproducible defects in code that passes every existing gate.
- **Verified**: the larger target, which the reviewer split between two
  sub-reviewers, produced fewer defects per line but both of the targets with a
  high-severity finding were ones the reviewer dug into with probes. The
  effort-asymmetry note is in T2 `intrastate` entry 26124.
- **Verified** (spike, 2026-09-17): the yield is not confined to rich
  packages. Code chosen for being unpromising filled the 12-finding cap,
  including two defects in a package governed by a conformance suite. The
  per-line comparison first drawn from this run is withdrawn (capped counts).
  *Source: T2 `nexus_rdr/214-research-2` and `-4`; beads under epic
  nexus-cd1k0; probes in `~/git/nexus-rdr214-probes`.*
- **Verified** (spike, 2026-09-17): uncapped, the reviewer reports about 13
  findings per thousand lines in the quiet modules and about 4.5 in the daemon
  package, most of them low severity. Two independent passes put the
  population this reviewer can see at about 51 and 31, of which 48 and 28 are
  now known. This is a floor on the true count.
  *Source: T2 `nexus_rdr/214-research-5`.*
- **Verified** (spike, small sample): the reviewer's severity label moved
  on 8 of 19 overlapping findings that carried one on both sides, both highs
  among them; what the defect affects did not move.
- **Verified** (spike): one capped pass does not exhaust a package. A second
  capped pass over remediated code found 12 defects, none a repeat.
- **Verified** (spike): an independent re-run of the reviewer's probes is not
  a formality. It reproduced all of them and raised one finding from
  "possible lost results" to "a search query can execute a program".
- **Not measured**: what the instrument cannot see. Both recapture passes are
  the same model under the same brief, so their overlap says nothing about
  defects neither would find.
- **Not measured**: the cost of acting on findings against the cost of finding
  them. One day's remediation of the pilot's findings took 40 commits, drew
  about 30 reviewer findings against the fixes, turned develop red four times
  and shipped at least two regressions. A pass costs minutes.
- **Not measured**: whether the rate holds for the two largest packages
  (`db`, about 28,700 lines; `commands`, about 45,000), which no run has
  covered whole.

### Critical Assumptions

- **Verified, was Assumed**: the yield holds for packages not chosen for
  being rich. Two unpromising targets filled the cap, and uncapped they
  returned 46 and 26 findings (recapture, above).
- **Assumed**: the cost stays near 1.4 agent-minutes per thousand lines for a
  different model. Measured for the uncapped brief: about 1.6 for both
  recapture targets.
- **Assumed**: a finding's "affects" class can be assigned by rule from its
  file and failure scenario. It was assigned by hand here, once, by one
  session.
- **Assumed**: findings of this kind are worth more than the remediation they
  trigger costs. The pilot's remediation took one working day of one session
  plus its sub-agents; no cost figure was kept for it, which is itself a gap
  (the sibling project keeps a per-run cost scorecard and this one does not).

## Proposed Solution

Review each package of standing code once with an uncapped brief, and return
to a package only after it has changed. Decide what happens to each finding
from what it affects, not from the reviewer's severity label. Limit how much
remediation is in flight, because the measurements say remediation, not
review, is the expensive and risky half.

The research changed the question. The draft asked how often to review. The
measurements show that review is cheap (about 1.6 agent-minutes per thousand
lines) and productive to the point of excess: 4.5 to 13 findings per thousand
lines, most of them low severity. About 165,000 lines under `src/nexus/` are
still unreviewed. At those rates a full sweep costs under five agent-hours and
returns between 750 and 2,100 findings. One day of fixing about 55 findings
took 40 commits, drew about 30 reviewer findings against the fixes, turned
the integration branch red four times and shipped two regressions. Fixing
everything a sweep finds is not possible at that cost, so the design has to
say which findings are acted on.

### Approach

1. **One uncapped pass per package.** The brief loses its cap of 12 findings.
   One uncapped pass alone re-found 89 and 83 percent of what the earlier
   capped pass had found, and held 96 and 93 percent of everything the two
   passes found together. A second pass over unchanged code added 4 and 8
   percent. A package counts as reviewed after one pass, unless Phase 2's
   two-pass check on `db` fails its threshold, in which case Phase 3 runs two
   passes per package until three in a row come in under it.
2. **Every finding carries an "affects" class.** The class is one of eight
   values in a checked table (below). It is assigned by rule from the file and
   the failure scenario. The reviewer proposes it and the session that takes
   the run confirms it. The severity label stays in the report as the
   reviewer's opinion and decides nothing, because it did not reproduce
   between passes.
3. **The class decides what happens to the finding.** Three tiers:
   - *Act now*: security, user content, data integrity. Each finding's probe
     is re-run by a second session, then it becomes its own bead and is fixed
     before the next run starts.
   - *Act if reachable by default*: service operations, and release tooling
     that can report a false pass. A finding reachable on a shipped default
     path (the installed service unit, the default configuration, a release
     gate as the release skill runs it) becomes its own bead. Otherwise it
     joins the package's batch bead.
   - *Batch*: search quality, process tooling, other release tooling,
     diagnostics. One batch bead per package lists them with their probes.
     They are fixed when someone next changes that file for another reason.
4. **Remediation in flight is bounded.** No run starts while an act-now bead
   from an earlier run is open. This is the rule that stops the loop the
   pilot fell into, where each round of fixes fed the next round of findings.
5. **Return on change, not on a calendar.** A ledger records, per package,
   the commit reviewed, the date, the line count, the brief's hash and the
   finding counts by class. A package is due again when more than a fifth of
   its non-test lines differ from the reviewed commit. The fraction is a
   starting value and is not measured. Each return run records a predicted
   finding count first. Checkpoint: after the third return run, compare
   predicted with actual across the three. If actual is below half of
   predicted on all three, the fraction is too small and doubles; if above
   twice predicted on any, it halves; otherwise it stands. The revision is a
   ledger entry with the three pairs, never a silent edit.
6. **The reviewer stays isolated; matching happens afterwards.** The reviewer
   reads no memory, tracker or RDR. After the run, a separate step drops
   findings that match an open bead, a closed bead or a recorded disposition.
   A disposition is a finding already judged to be intended behaviour. The
   recapture re-reported one such finding (the frontmatter rule that a test
   pins as the contract), so dispositions are kept in the ledger and matched
   like beads.
7. **Each run records its own cost.** The ledger row gets, when the run's
   act-now beads close: commits made, reviewer findings against the fixes,
   red runs on the integration branch, and regressions shipped. After the
   third run under the rule (the `db` run in Phase 2 is the first), those
   rows decide whether the practice continues.

### Technical Design

Nothing here needs a new service or store. Phase 1 adds two checked tables
and their tests; the reviewers are ordinary agents and beads are the tracker.

**The brief.** `docs/review/standing-code-review-brief.md`, moved into the
repository from the probe directory so its hash means something. Changes from
the recapture version: each finding states its affects class and whether the
failing path is reachable with default settings.

**The affects table.** `docs/tables/standing-review-affects.toml`, a checked
table in the sense of `docs/rdr/AGENTS.md`. The loader
(`src/nexus/tables/load.py`) knows two kinds, state machines and decision
tables, with every dimension a declared enum and every row emitting one
result. This one is a decision table: dimensions `class` (the eight values
below) and `reachable` (by default, not by default, not applicable), rows
emitting the disposition (act now, own bead, batch). The one-sentence test
for each class and the path patterns that make it a file's default are
documentation beside the domain, not checked content.

| Class | The finding means | Tier |
|---|---|---|
| security | something runs, is read or is written that the user did not authorise | act now |
| user-content | indexed or stored content is missing, truncated, duplicated into loss, or never retried | act now |
| data-integrity | service state can be corrupted: schema lock, queue rows, leases that lie | act now |
| service-operations | start, stop, upgrade, install or configuration behaves wrongly | act if reachable by default |
| release-tooling | a release gate or checked table can report a wrong result | act if it can pass falsely, else batch |
| search-quality | results are worse or mislabelled, and nothing is lost | batch |
| process-tooling | this project's RDR gates, hooks and review tooling | batch |
| diagnostics | a message, log line or exit text is wrong | batch |

When a finding fits two classes, the higher tier wins. A function documented
as never raising that raises on a malformed local file takes the class of
what the caller then loses, which is usually diagnostics or service
operations.

**The ledger.** `docs/tables/standing-review-ledger.toml`. One row per run:
package, reviewed commit, date, lines, brief hash, findings by class, the
predicted finding count when one was recorded before the run, beads filed,
and the cost fields of Approach item 7. A batch-bead fix made later carries
the run's row id in its commit message, so its cost is attributed to the run
that found it. A second section lists dispositions: file, one-line
description, the test or decision that settles it. A third section holds the
Approach item 5 checkpoint entries: the three predicted-and-actual pairs and
the fraction before and after. Dates, commits and counts are not enum
values, so this file is checked by its own loading test in the sense of
`docs/rdr/AGENTS.md`, not through the state-machine and decision-table
loader the affects table uses. The six capped passes and two recapture
passes are entered as the first rows so the record starts complete.

**Order of the sweep.** By where act-now findings are most likely, from the
classes of what each package handles: `db`, `catalog`, the indexing and
search modules at the top level not yet covered, `mcp`, then `commands`
(largest, mostly dispatch), `plans`, and the rest. The plugin hooks and
`scripts/` come last: they are process and release tooling.

### Decision Rationale

- **Uncapped, one pass.** The cap made every count meaningless (five of six
  passes returned exactly 12). With it removed, one pass reaches close to the
  ceiling of what this reviewer can see.
- **Affects over severity.** The label moved on 8 of 19 overlapping
  findings, and the same defect was high in one pass and low in the next.
  What it affects follows from the file and the failure scenario and did not
  move. A high-severity defect in this project's own
  gate tooling was fixed first on the pilot day, ahead of defects that
  silently dropped pages from a user's index. That ordering was wrong, and
  the label caused it.
- **Bounded remediation.** Review costs minutes and remediation costs days
  and produces defects. The bound is on the expensive half.
- **Batch beads are not a way to forget.** The project rule is to fix what is
  found instead of filing it. That rule was written for a residual found
  while working in a file. A sweep that returns hundreds of low-impact
  findings in files nobody is working in is a different case. The pilot day
  measured the cost of fixing many findings at once, which Approach item 4
  bounds; it did not measure the cost of fixing one finding in a file nobody
  is otherwise in, so the untouched-file part of this argument is inferred,
  not measured. The batch bead keeps the probes, and the fix happens with
  the file open for a real reason, when the sibling sweep and the tests are
  already being paid for. The ledger's cost rows are where this inference
  gets tested.

## Alternatives Considered

### Alternative 1: Do nothing further

Rejected on evidence. Code chosen for being unpromising returned 46 and 26
findings uncapped, including a supervisor that never stands down when fenced
and a search query that could run a program.

### Alternative 2: Review every package on a fixed rotation

Simple to state and to check. It spends the same effort on unchanged code as
on changed code, and the recapture shows a repeat pass over unchanged code
adds 4 to 8 percent. Rejected in favour of return on change.

### Alternative 3: Fix every finding as it arrives

This is what the pilot day did. The cost is in the Proposed Solution above.
At sweep scale it is weeks of remediation whose own defect rate is measured
and not small. Rejected.

### Alternative 4: Two independent passes per package

It gives a population estimate for every package. It doubles the cost for
4 to 8 percent more findings, mostly low. Kept as a measuring tool: the
first large package in Phase 2 and the return runs. Rejected as the routine.

### Briefly Rejected

- **A per-commit reviewer.** Removed once already (f17277f48); it reviews
  changes, which the per-bead reviewers already do.
- **More lint tests instead.** A lint checks an invariant someone has already
  named. None of the pilot's 33 findings was an instance of a named invariant.
- **Triage by the reviewer's severity.** The label does not reproduce.

## Trade-offs

### Consequences

- Most findings will sit in batch beads for a long time. That is the design
  and not a backlog to be ashamed of, but the beads must stay findable by
  file, so each carries the paths it covers.
- Findings in release tooling or the plugin surface ride release trains other
  sessions own, so a run is timed against them.
- The affects class is a judgment. Two people can disagree on whether a wrong
  line number is search quality or diagnostics. The tier is what matters, and
  those two share one.

### Risks and Mitigations

- **The affects rule is untested.** It was applied once, by hand, by one
  session. Phase 1 measures it before anything depends on it.
- **An act-now finding mislabelled into a batch.** The second session that
  confirms classes reads every batch entry's failure scenario, not only its
  class. The probe re-run covers the act-now tier only.
- **The instrument's blind spot.** Both recapture passes are one model under
  one brief. Nothing here measures what that reviewer cannot see. The design
  claims to find defects, not to certify their absence.
- **A reviewer that reports style as defects.** The brief's rule (state an
  input and a wrong result) held: 43 of 46 and 16 of 26 reproduced.
- **Findings nobody acts on.** No run starts without a named session to take
  its beads, and no run starts over an open act-now bead.

### Failure Modes

A run that finds nothing is ambiguous: the code may be sound or the reviewer
may have examined little. The brief requires the lines reviewed, the time
spent and a "not reviewed" list; a run is accepted only with those, and its
ledger row records them.

## Implementation Plan

No implementation starts before this RDR is accepted.

**Phase 1: the rule, measured.**
1. Move the brief into the repository and add the affects and reachability
   fields.
2. Add the affects table and the ledger with their loading tests, and enter
   the eight existing passes.
3. Measure the rule. Two sessions that have not seen this RDR's
   classification each classify the 141 existing finding reports (some
   describe one defect twice) from the table alone. Recorded before the result: they agree with each other on the tier
   for at least 90 percent. Below that, the table's tests are rewritten and
   the measurement repeated before Phase 2.

**Phase 2: the first run under the rule.** Review `db` with the new brief,
as two independent passes (Alternative 4), because the near-ceiling result
comes from two targets under 7,000 lines and `db` is four times larger.
Recorded before the run: the second pass adds at most 15 percent of what
the first found. Above that, the sweep in Phase 3 runs two passes per
package until three consecutive packages come in under it. Match, classify,
re-run act-now probes, file beads by tier. Fix the act-now beads. Fill in
the cost fields.

**Phase 3: the sweep.** One package per run in the order above, each gated
by Approach item 4. After the third run, read the cost rows and decide with
Sam whether to continue, change the tiers or stop.

## Test Plan

- The affects table loads under the decision-table loader; the ledger loads
  under its own test: every ledger row names classes that exist in the
  affects table, every affects row emits a disposition that exists, every
  disposition entry names a file that exists.
- The Phase 1 agreement measurement, with its prediction recorded first.
- Each run's acceptance check: lines reviewed, time, and the not-reviewed
  list are present, and the reviewed commit is an ancestor of the branch.

## Finalization Gate

### Contradiction Check

The draft's own first conclusion contradicted its later evidence, and the
contradiction is left visible rather than edited out: the second-run section
concluded that unpromising code yielded more defects per line, and the
withdrawal that follows it says why that count was an artifact of the brief's
cap. The Research Findings, the Proposed Solution, the Decision Rationale and
the Implementation Plan all rest on the uncapped recapture
(T2 `nexus_rdr/214-research-5`), not on the withdrawn figure. The one
tension the research surfaced is stated in Decision Rationale: the batch tier
departs from the project's fix-do-not-file rule, and the argument for that is
made there, not hidden.

### Assumption Verification

The five research records are spikes, each with a prediction recorded before
its result and two of the predictions wrong (T2 `nexus_rdr/214-research-1`
to `-5`). Three assumptions remain and are listed as such under Critical
Assumptions: the cost holding for a different model, the affects rule being
assignable by rule, and findings being worth more than the remediation they
trigger. The second is what Phase 1 measures before anything depends on it,
with its agreement threshold recorded in advance. The third is what the
ledger's cost rows measure, with the decision point at the third run
(Approach item 7). The return-on-change fraction is a design parameter, not
an assumption of record; Approach item 5 gives it a prediction, a threshold
and a checkpoint.

#### API Verification

| Surface | Verification |
| --- | --- |
| Reviewer dispatch under the brief | Spike: eight passes, six capped and two uncapped, all probes re-run by a second session for the capped ones |
| Checked tables under `docs/tables/` | Source Search: `docs/rdr/AGENTS.md` and the release-choreography table already load this way |
| Beads as the landing place | Spike: run 2 filed 16 beads with probes in one step |

### Scope Verification

The RDR decides the practice: brief, cadence, disposition rule, landing
place, cost ledger. It does not fix any finding; the findings from the two
uncapped passes are recorded in T2 and deliberately unfiled until the
disposition rule is accepted. The plan adds two checked tables and a brief file, no service or store.

### Cross-Cutting Concerns

- **Versioning**: the brief lives in the repository and its hash is recorded
  per run; a changed brief resets the return rule for that package.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: N/A; reviews run on a developer box.
- **Incremental adoption**: the first run under the rule is one package.
  Nothing changes for packages not yet reviewed.

### Proportionality

Review costs about 1.6 agent-minutes per thousand lines. One day of
remediating about 55 findings cost 40 commits, four red runs on the
integration branch and two shipped regressions. The design spends its rules
on the expensive half and leaves the cheap half nearly unconstrained.

## Revision History

- 2026-09-17: created as a draft from the pilot's three reports and the
  remediation scorecard (bead nexus-z2rvr).
- 2026-09-17: second run, second pass and recapture recorded; the capped
  per-line comparison withdrawn; affects classification added
  (T2 `nexus_rdr/214-research-2` to `-5`).
- 2026-09-17: Proposed Solution, Technical Design, alternatives and plan
  written from the research.
- 2026-09-18: Gate round 1 — BLOCKED (1 Critical, 6 Significant, 0 ship-blocker(s)); commit `f908e70dd`; critique `nexus_rdr/214-gate-critique-2026-09-18-r1`.
- 2026-09-18: Gate round 2 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); commit `08bce74d6`; critique `nexus_rdr/214-gate-critique-2026-09-18-r2`.
- 2026-09-18: Gate round 3 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); commit `5fe31be98`; critique `nexus_rdr/214-gate-critique-2026-09-18-r3`.
