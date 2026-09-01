---
title: "Closed Vocabularies as Checked Tables"
id: RDR-201
type: Architecture
status: draft
priority: high
author: Sam
created: 2026-09-01
accepted_date:
related_issues: [nexus-tpuct, nexus-jh86x, nexus-1c7oq, nexus-7mudt]
---

# RDR-201: Closed Vocabularies as Checked Tables

## Problem Statement

Nexus authors several small, closed vocabularies: the statuses a decision
record may hold, the states a paired client-and-engine release passes
through, the relationship between a decision record and the records that
depend on it. Each is finite. Each is authored by hand. None is checked
anywhere for the two properties a finite vocabulary can actually be checked
for: that every situation the vocabulary is supposed to cover has exactly one
answer (coverage), and that no situation has two (overlap).

The vocabularies live as prose in `AGENTS.md` and skill files, or as
control flow in Python scripts. Prose drifts: the record lifecycle was
defined with six statuses in RDR-001 and today carries eleven in the wild.
Control flow hides its state space: the release choreography is 1,690 lines
across two scripts, and it has failed in both directions in production, once
by covering a state it should have refused (GH #1402, a floor-bumped client
published with no deploy armed) and once by leaving a state uncovered (the
7.1.0 / v0.1.62 inversion, a client shipped pinned to an engine lacking its
own features). In the vocabulary of a transition table those are an overlap
and a coverage gap, and a table would have reported both before either
shipped.

This came into view through a comparison with `cwensel/intrastate`
(2026-09-01, artifact `7e86ba27`), a Go kernel whose whole purpose is to hold
such vocabularies as linted data: a TOML model of tags, rules, guards and
writes, proved complete and unambiguous before it runs, refusing at runtime
rather than guessing. Its first customer is its author's own record
lifecycle, modelled in 359 lines and linted in CI on every push. Nexus has no
entry in that layer of the stack; the nearest things are a plan-selection
policy (`PLAN_CHOICE_CONFIDENCE_BAND`) that breaks ties by position, and the
release scripts above.

### Enumerated gaps to close

#### Gap 1: The record lifecycle has no transition table

RDR-001 defined six statuses. `docs/rdr/` today holds `closed`, `draft`,
`scrapped`, `superseded`, `deferred`, `companion-note`, `accepted`, `frozen`,
`frozen-pending-question-set`, `complete`, `abandoned`, and
`revised-after-implementation`. No document says which of these are legal,
which transitions between them are legal, or which command performs each.
`/conexus:rdr-accept` is the only guarded edge, and it checks that a gate
PASSED, not that `draft → accepted` is the transition being taken.
`/conexus:rdr-audit` checks frontmatter shape, not transitions.

#### Gap 2: The paired-release choreography is a state machine written as control flow

`scripts/check_engine_release_floor.py` (1,348 lines) and
`scripts/check_client_release_precondition.py` (342 lines) decide, over
{pinned engine tag, newest published tag, cloud-deployed version, client tag,
wire-ledger direction tokens, paired-deploy mode}, whether a release may
proceed. The state space is finite and enumerable. It is nowhere enumerated.
Each production incident was fixed by adding a branch, which is how the
scripts reached their size; nothing proves the branches are exhaustive or
disjoint.

#### Gap 3: Decisions have no memory across amendment

205 records cite one another. When one is superseded, scrapped, or has a
directive amended, nothing identifies the records or directives that depend
on it, and nothing marks their verdicts as needing re-examination. intrastate
exhibited the same defect at eleven records: a pair certified clean sat
uninspected for two months after one member was re-locked, and was
re-examined only because a wider pass happened to be run. At 205 records the
exposure is larger, and nexus's own history records the consequence: a rule
that lived in two places until "the stale one wins an argument it should
not" (`~/.claude/CLAUDE.md`, Hypothesis-Driven Work, 2026-08-21).

## Relationship to Prior RDRs

Scanned the full index for lifecycle, status, gate, validator, lockstep and
audit. Overlaps:

| RDR | Relationship | Note |
| --- | --- | --- |
| RDR-001 RDR Process Validation | **Origin** | P5 defined the six-status model because "one gate returned 'Conditional Accept', a status not in the lifecycle model". That rationale still holds and is exactly Gap 1: the model was defined once, in prose, and the vocabulary has since grown to twelve values with no check. This RDR moves the model from prose to a checked table. |
| RDR-002 T2 Status Synchronization | **Precedent** | Reached the diagnosis that status drifts when it lives in two places (file and T2), and shipped a write-order fix plus self-healing. It fixed *consistency* between two copies; it did not define *legality* of a transition. Gap 1 is the legality half. |
| RDR-024 RDR Process Guardrails | **Origin** | Added the accept-before-implement guard, the one guarded edge. Its regex-scan design is deliberately passive. This RDR generalises the guard from one edge to the whole table. |
| RDR-065 / RDR-067 Close-time funnel; cross-project audit loop | **Precedent** | Diagnosed silent scope reduction as invisible within one record and visible only across records. Gap 3 is the same shape, one level up: invalidation invisible within one record, visible only across the dependency graph. |
| RDR-081 Stale-Reference Validator | **Precedent** | Shipped a validator that reports prose drift against the live system without rewriting it. The same posture (report, do not auto-fix) is adopted here; bead nexus-tpuct extends that validator to commit, bead and record citations independently of this RDR. |
| RDR-121 Hook-Enforced Tool Routing | **Precedent** | Established that soft guidance gets a mechanical backstop. Gap 1's enforcement point is the same hook family. |
| RDR-143 Plugin/CLI Version Lockstep | **Precedent** | A two-value vocabulary (plugin version, CLI version) that must agree, enforced by a SessionStart check. The smallest instance of this RDR's pattern, already shipped. |
| RDR-185 Single-Ladder Convergent Upgrade | **Adjacent** | Owns the upgrade ladder's state model. Out of scope here; Gap 2 is the release-time choreography, not the install-time ladder. |

No prior RDR proposes holding any of these vocabularies as a checked table.

## Context

### Background

A closed vocabulary is a set of values fixed by the author: a status enum, a
gate outcome, a release mode. A transition table over one is a list of rows,
each saying: in this state, on this event, under this guard, go to that state.
Two properties are checkable over such a table without running anything.
Coverage: every (state, event) pair the author declares reachable has a row.
Overlap: no (state, event, guard-assignment) matches two rows. A decision
table is the stateless case: conditions in, answer out, same two properties.
These are old techniques (limited-entry decision tables, 1957; DMN 1.5 today)
and intrastate implements them faithfully, with one deliberate divergence: it
has no hit policy, so overlap is a lint failure rather than something a
priority order resolves.

### Technical Environment

- Record lifecycle: `conexus/commands/rdr-{create,research,gate,accept,close,audit,list,show}.md`, `nx rdr set-status`, T2 project `nexus_rdr`, `docs/rdr/README.md` index.
- Release choreography: the two scripts above, `src/nexus/engine_version.py`, the release and engine-release skills, `AGENTS.md` § Cutting a release.
- Dependency graph: RDR frontmatter has `related_issues` but no `depends_on` or `supersedes` field beyond the free-text supersede convention; the catalog has typed links (`supersedes`, `implements`, `cites`) over indexed documents, unused for this purpose.

## Research Findings

Four questions, four investigations, 2026-09-01. Each finding is recorded
in T2 under `nexus_rdr/201-research-N` with file:line evidence; the
full agent reports are in T3 (titles cited per finding). Every headline
number below was reproduced by the orchestrator on this tree.

### Finding 1: the record lifecycle, as implemented (Verified)

T2 `201-research-1`. The only code that writes an RDR's status is
`nx rdr set-status` (`src/nexus/commands/rdr.py:413-475`). The
`rdr-accept` and `rdr-close` preambles never write; they validate the
current status against an inline tuple (`("draft","open","accepted")` at
:985, `("accepted","final")` at :1162) and print the `set-status` command
for the calling skill to run. So `draft → accepted` is not one code path;
it is a check, an agent, and a separate write.

Three different enumerations of the vocabulary exist and disagree:

| Enumeration | Where | Members |
| --- | --- | --- |
| `_KNOWN_STATUSES`, the only write gate | `rdr.py:314-317` | draft, open, proposed, accepted, closed, deferred, superseded, scrapped, abandoned, revised, locked, final |
| `_STATUS_ORDER` / `_TERMINAL`, the SessionStart reconciliation ranking | `conexus/hooks/scripts/rdr_hook.py:23-36` | draft, open, accepted, implemented, closed, reverted, abandoned, superseded |
| `_README_STATUS_WORDS`, a test copy that claims to be kept in sync | `tests/test_rdr_close_tripwire.py:126-129` | as `_KNOWN_STATUSES` minus `open` |

Consequences. `implemented` and `reverted`, the primary terminal states
RDR-001 §P5 defined, are ranked by the hook but rejected by `set-status`;
no command can write them, and in practice every close reason writes
`closed`. `scrapped` and `deferred` are writable but unranked, so the hook's
`.get(status, -1)` (`rdr_hook.py:246-247`) lets a SessionStart reconcile
silently overwrite a legally-set `scrapped` file with any ranked T2 value.
`proposed` and `locked` are accepted by the gate and used nowhere.

Of the twelve statuses in the wild: seven are legal (`closed`, `draft`,
`accepted`, `scrapped`, `superseded`, `deferred`, `abandoned`), though the
last four have no producing command and reached files by hand-edit, the
exact path `set-status` was built to close. Five are out of vocabulary:
`companion-note` (three sibling notes, deliberately outside the lifecycle),
`frozen`, `frozen-pending-question-set`, `complete` (RDR-200's sub-documents)
and `revised-after-implementation` (a near-miss of the legal, unused
`revised`). The hook's file-only reconcile (`rdr_hook.py:191-211`) does
not touch the README index, so a hook-driven flip leaves the index stale
until CI's tripwire runs.

### Finding 2: the release choreography, as implemented (Verified)

T2 `201-research-4`; T3
`analysis-deep-paired-release-state-space-rdr201-2026-09-01`.

The two scripts decide over five genuinely finite enums, ten open
dimensions that each reduce to a finite comparison outcome (below / equal /
above / unreachable and the like), and two that do not reduce: the
gate-report directory contents (delegated to `deploy_tracker.py:249-296`)
and the free-text `--no-record-deploy` reason. The nominal product is about
17,900 cells; the reachable guard-chain leaves number 101 (floor 88,
precondition 13). Of those, 86 are fully tested, 7 have no test at any
layer, 2 are tested only at the library layer, 1 partially.

Three overlaps, one of them live on this tree:

- **O1, live.** The `[additive]` direction-safety token is honored at
  `check_client_release_precondition.py:134` and appears zero times in
  `check_engine_release_floor.py` and zero times in its 1,919-line test
  file. On the checked-in ledger, which holds two `[additive]` entries,
  `check_engine_release_floor.py --ledger-only` exits 1 and
  `check_client_release_precondition.py` exits 0. `.github/workflows/ci.yml:153`
  runs `--ledger-only` on every pull request to `main`, merge-blocking, so
  main-bound PRs are red on entries the sibling gate certifies safe, and both
  release skills direct the operator down the branch the floor script blocks.
  No test pins either behaviour: `tests/scripts/conftest.py:43-51` isolates
  every floor test onto an empty ledger. Filed as **nexus-hcdk3** (P1).
- **O2, order-dependent by design.** `{cloud ≥ floor, ledger dirty}` exits 1
  under `--paired-deploy` (battery before probe, `check_floor:1014`) and 0
  under `--paired-deploy-auto` (probe first, :929-938, ledger never read).
  Both are test-pinned; the asymmetry is a decision nobody wrote down.
- **O3, historical.** The nexus-k1c08 freshness guard re-disjoined a row that
  had overlapped since the script was written.

The incident mapping corrects this RDR's own problem statement. The
7.1.0 / v0.1.62 fix (`79fff05a9`) changed no decision logic, only a remedy
string (`precondition:79-87`) and prose in `AGENTS.md` and two skills. The
guard was right; the event it was bound to (pre-tag rather than deploy) was
wrong. That is neither a coverage gap nor an overlap: it is an
**un-encoded event dimension**, and a table for this choreography needs an
event column, not only guards. Corroborating: the precondition script has
zero workflow or script callers; only skill markdown invokes it. GH #1402
had no mechanized cell at incident time (the gate script postdates it,
`764a1dc23 → 3cb14f96e`), so the overlap framing describes the aftermath,
not the incident.

Size. The two scripts are 870 code lines, 352 of them (40%) message text at
65 print sites, with 72 `if`s and 95 `return`s. A table plus evaluator
replaces about 580 lines and costs about 460 (120 rows, a 200-line message
catalog, a 140-line evaluator): net 120 lines. The case does not rest on
line count; it rests on 72 scattered guards becoming 101 rows a checker
proves exhaustive and disjoint. About 315 lines of sensors (git, gh, HTTP
probes) and 90 lines of argument help stay imperative.

### Finding 3: an enum-only checker is small, and intrastate is not a dependency (Verified)

T2 `201-research-3`; prototype in the session scratchpad, pointer T2
`nexus/rdr-201-q3-enum-checker-prototype-sizing`.

A stdlib-only Python checker implementing intrastate's enum-only subset
(single-valued enum tags, `eq`/`in` atoms, match blocks scoping a group,
guard atoms as product dimensions, coverage as union-of-rows equals
product-of-domains, overlap as two rows on one assignment, one declared
escape row closing a group with an advisory, refusal to claim coverage over
any undeclared or non-enum dimension) is 446 lines (338 non-blank) with 375
lines of tests, 17 passing. Non-vacuity was verified by neutering overlap
detection and watching both planted-overlap tests fail. Fixtures: the record
lifecycle (7 states × 9 events) and a 36-cell release decision table. What
the subset cannot express (int ranges, sets, three-valued guards over
optional keys, accessors, write-readback, dead-end analysis) none of the
three tables needs.

Alternative 1 is refuted by measurement: intrastate has zero tags and zero
releases, would add a Go toolchain to nexus, ships a fixture-grade gate
binding, and its grammar is far larger than the need.

### Finding 4: the catalog gives dependency tracking its primitives and nothing else (Verified)

T2 `201-research-2`; T3
`rdr201-q4-catalog-link-graph-rdr-dependency-tracking-2026-09-01`.

The catalog has typed links and BFS traversal (`traverse`,
`search_graph_hop`). Nothing produces RDR-to-RDR links: the one
RDR-targeting generator (`src/nexus/catalog/link_generator.py:216`) links
records to code. `set-status` has no catalog import. The four
frontmatter-superseded records (106, 107, 123, 124) have no links at all;
catalog-wide there are three `supersedes` links, none between records. The
text carries `RDR-NNN` cross-references in 208 files (RDR-110 the top target
at 185 hits) with zero corresponding links. Frontmatter already carries
`related_rdrs` (50 files), `supersedes` (23), `superseded_by` (4),
`parent_rdr` (6); no `depends_on` exists; no code reads any of them.

One complication precedes any design: the 206 on-disk records are
registered under about ten catalog owner ids, with a stale legacy `prose`
content-type registration beside the current `rdr` one. Edges mean nothing
until a canonical tumbler per record is resolved.

### Critical Assumptions

- **Verified, with correction**: the vocabularies are finite. Gap 2's two
  non-reducing dimensions are outside the decision (a delegated tracker read
  and a free-text reason) and stay imperative.
- **Refuted in part**: "both incidents are an overlap and a coverage gap."
  One was an un-encoded event; the other predates the gate. The table
  design gains an event column as a result.
- **Verified**: a report-only checker is sufficient for lint. Gap 1's
  `set-status` refusal and Gap 2's CI gate are the two places a table's
  verdict must block, and both already block today through imperative code;
  the table changes what decides, not whether it blocks.
- **New, verified**: a live overlap in the release gates is blocking PRs to
  `main` now, independent of this RDR (nexus-hcdk3).

## Proposed Solution

### Approach

One table format, one checker, three tables.

1. A table format for enum-typed transition and decision tables, authored as data (TOML or YAML), with declared states, events, guards over declared enum domains, and rows.
2. A checker that proves coverage and overlap over the declared domains, refuses to claim coverage over any undeclared or non-enum dimension, and reports a bare-green-by-default advisory when a row is a declared catch-all (the intrastate escape-row idea; bead nexus-1c7oq carries the advisory format).
3. Three tables under `docs/` or `conexus/`, each linted in CI:
   - `rdr-lifecycle`: the record status machine, consumed by `nx rdr set-status` so an illegal transition is refused with a typed reason, and by `rdr-audit` so an out-of-vocabulary status is a finding.
   - `release-choreography`: the paired-release decision table with an explicit event column (pre-tag, tag-push, deploy, post-deploy verify), consumed by both scripts, which become thin evaluators over one table and so cannot disagree (Finding 2, O1).
   - `rdr-dependencies`: not a transition table but the edge list Gap 3 needs; a status change on a record marks every dependent's verdict `needs-reexamination`, surfaced by `rdr-audit`.

### Decision Rationale

The alternative is to keep patching prose and branches. Both production
incidents and the twelve-status drift are the measured cost of that. A table
is the smallest artifact that can be proved complete, and the checker is small
because the domains are enums.

## Alternatives Considered

### Alternative 1: Depend on intrastate (refuted by Finding 3)

Use the Go binary as the checker and resolver. Pros: the kernel is sound,
three-valued guards and write-readback are already built. Cons: unreleased,
zero tags, Go toolchain in the nexus dependency set, and its accessor layer is
fixture-grade. Finding 3 measured the alternative: the enum-only subset the
three tables need is ~340 lines of stdlib Python. Rejected.

### Alternative 2: Prose plus a stronger audit

Keep the vocabularies in prose and extend `rdr-audit` to enumerate the legal
statuses. Closes the twelve-status drift only. Does nothing for Gap 2 or 3,
and the legal list is itself prose that will drift.

### Briefly Rejected

Modelling the vocabularies in the catalog as documents and links. The catalog
holds relationships well but has no notion of coverage; it would serve Gap 3
and nothing else.

## Trade-offs

### Consequences

Three new data files and a checker join the lint bucket. The release scripts
shrink. `nx rdr set-status` gains a refusal path.

### Risks and Mitigations

- The checker itself is a small state-machine evaluator and can have the bugs
  it exists to find. Mitigation: test it against planted coverage gaps and
  overlaps (non-vacuity assert), and against the two recorded incidents as
  fixtures that must be reported.
- Over-formalising vocabularies that are in fact open. Mitigation: the checker
  refuses to claim coverage over a non-enum dimension rather than pretending.

## Implementation Plan

_Phased plan to be produced after acceptance. Expected shape: Phase 1 table
format and checker with the lifecycle table; Phase 2 release choreography;
Phase 3 dependency edges and re-examination marking._

## References

- Comparison artifact, 2026-09-01: https://claude.ai/code/artifact/7e86ba27-3818-443a-ba02-b3ecfbd5526a
- `cwensel/intrastate` at `bc9f2a0`; `models/rdr.toml`; `docs/model-authoring.md` (references Mealy 1955, Dijkstra 1975, King 1968, OMG DMN 1.5).
- T3: `analysis-codebase-intrastate-runtime-kernel-2026-09-01`, `analysis-deep-intrastate-rdr-apparatus-2026-09-01`; T2 `nexus/critique-intrastate-nexus-comparison-2026-09-01` [23971].
- Beads filed from the same comparison: nexus-tpuct, nexus-jh86x, nexus-1c7oq, nexus-7mudt.

## Revision History

- 2026-09-01: Created (draft).
- 2026-09-01: Research findings 1-4 recorded; Alternative 1 refuted; incident classification corrected (event dimension); nexus-hcdk3 filed from Finding 2.
