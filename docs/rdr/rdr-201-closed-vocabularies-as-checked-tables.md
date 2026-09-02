---
title: "Closed Vocabularies as Checked Tables"
id: RDR-201
type: Architecture
status: closed
closed_date: 2026-09-02
priority: high
author: Sam
created: 2026-09-01
accepted_date: 2026-09-01
reviewed-by: self
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
(2026-09-01), a Go kernel whose whole purpose is to hold
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
| RDR-024 RDR Process Guardrails | **Precedent** | Added soft, regex-detected warnings at three workflow points so implementation does not start on an unaccepted record. Deliberately advisory ("soft warnings are the ceiling", Finding 4). The hard guard on the accept edge itself, the status tuple in `rdr.py:985`, arrived later with `set-status` and nexus-qsryj (2026-07-22), not from this RDR. Precedent for guarding the lifecycle; this RDR replaces both the soft warnings' and the hard tuple's hand-written status lists with one table. |
| RDR-149 Unified Leased Service-Registry Substrate | **Precedent** | Established, for daemon lifecycle, the rule that a fix lands in one shared primitive plus a conformance suite, never in one tier's copy, and made it mechanically enforced and an `AGENTS.md` hot rule. Gap 2 is the same doctrine applied to the two release gates: nexus-hcdk3 was exactly a fix landing in one script's copy. |
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
17,900 cells; the reachable guard-chain leaves number about 101 (floor
88, precondition 13), an estimate from reading the guard chains rather than
a mechanical enumeration. The coverage breakdown the analysis gives (86
fully tested, 7 with no test at any layer, 2 library-layer only, 1
partial) sums to 96, so five leaves were counted without a coverage
verdict. Phase 2's first step is the mechanical enumeration that settles
both numbers; the sizing below does not depend on the exact figure.

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

1. **Table format**: enum-typed transition and decision tables authored as data (TOML), with declared states, events, guards over declared enum domains, and rows.
2. **Checker**: proves coverage and overlap over the declared domains, refuses to claim coverage over any undeclared or non-enum dimension, and reports a bare-green-by-default advisory when a row is a declared catch-all (the intrastate escape-row idea; bead nexus-1c7oq carries the advisory format).
3. **Three tables**, each linted in CI (the lifecycle table ships in the package, the release table under `docs/tables/`, see Revision History):
   - `rdr-lifecycle`: the record status machine, consumed by `nx rdr set-status` so an illegal transition is refused with a typed reason, and by `rdr-audit` so an out-of-vocabulary status is a finding.
   - `release-choreography`: the paired-release decision table, event-INVARIANT by ruling (nexus-j9z30.26, 2026-09-02, see Revision History: the 7.1.0/v0.1.62 fix changed a remedy string, not decision logic; the event-sensitivity is real but lives in the release / engine-release skill choreography, recorded as citations by `enumerate_release_cells.event_mode_matrix()`), consumed by both scripts through one shared module (`scripts/release_choreography.py`), which become thin evaluators over one table and so cannot disagree (Finding 2, O1).
   - `rdr-dependencies`: not a transition table but the edge list Gap 3 needs; a status change on a record marks every dependent's verdict `needs-reexamination`, surfaced by `rdr-audit`.

### Technical Design

**Status domain (Sam, 2026-09-01).** Six values, the lifecycle table's
`status` dimension:

| Status | Meaning | Terminal |
| --- | --- | --- |
| `draft` | entry state; under research or revision | no |
| `accepted` | gate passed; implementation may start | no |
| `deferred` | parked, from `draft` or `accepted`; resumes to `draft` only, never directly to `accepted`, so deferred work re-gates | no |
| `closed` | implemented and shipped; the close reason lives in T2 and the postmortem | yes |
| `superseded` | replaced by a named successor; the transition is guarded on `superseded_by` being present in frontmatter and refuses without it | yes |
| `abandoned` | not going to happen, whether before or after acceptance; the timing lives in T2 | yes |

Decisions folded in: `scrapped` and `abandoned` were two names for one
terminal state and are merged (the seven `scrapped` files migrate to
`abandoned` in Phase 1, with `scrapped` recorded in T2). `implemented`,
`reverted`, `proposed`, `locked`, `open`, `final` and `revised` are retired
from `_KNOWN_STATUSES`: nothing writes them and no file carries them.
The five out-of-vocabulary values in the wild (`companion-note`, `frozen`,
`frozen-pending-question-set`, `complete`, `revised-after-implementation`)
are not statuses; those documents get `kind: companion` in frontmatter and
no lifecycle status, and the audit skips them.

**Table format.** TOML, one file per table, with three sections:

```toml
[table]
id = "rdr-lifecycle"
kind = "state-machine"          # or "decision-table"

[dimensions.status]             # every dimension is a declared enum
domain = ["draft", "accepted", "deferred", "closed", "superseded", "abandoned"]
[dimensions.event]
domain = ["accept", "close", "supersede", "abandon", "defer", "resume"]
[dimensions.gate]
domain = ["passed", "blocked", "none"]
[dimensions.successor]            # superseded_by present in frontmatter?
domain = ["named", "absent"]

[[row]]
id = "accept"
match = { status = "draft", event = "accept" }
guard = { gate = "passed" }
to = { status = "accepted" }

[[row]]
id = "accept-blocked"
match = { status = "draft", event = "accept" }
guard = { gate = ["blocked", "none"] }
refuse = "gate-not-passed"

[[row]]
id = "accept-otherwise"
match = { status = ["accepted", "deferred", "closed", "superseded", "abandoned"], event = "accept" }
escape = true
refuse = "illegal-transition"

[[row]]
id = "supersede"
match = { status = ["draft", "accepted", "deferred", "closed"], event = "supersede" }
guard = { successor = "named" }
to = { status = "superseded" }

[[row]]
id = "supersede-unnamed"
match = { status = ["draft", "accepted", "deferred", "closed"], event = "supersede" }
guard = { successor = "absent" }
refuse = "successor-not-named"

[[row]]
id = "resume"
match = { status = "deferred", event = "resume" }
to = { status = "draft" }
```

`match` scopes a group (the rows sharing one match assignment); `guard`
atoms are the dimensions coverage is proved over, per group, as the
cross-product of their declared domains. Every row in a table must name
the same set of match keys; a list value expands into one row per member,
exactly as intrastate's `in` does, so `accept-otherwise` above is six rows
in six groups, each closing its group's remainder. A row that names fewer
match keys than its siblings is refused at load (`match-keys-mismatch`),
which is what keeps a broad "otherwise" from silently overlapping a narrow
group: the checker never has to decide whether `{event=accept}` and
`{status=draft, event=accept}` are the same group, because the shape
forbids the question. A row carries exactly one of
`to` (a state machine's write), `emit` (a decision table's answer), or
`refuse` (a typed refusal code). At most one `escape = true` row per group
closes the group's remainder and takes the `closed-by-escape` advisory.

**Checker.** `src/nexus/tables/check.py` (name provisional), stdlib only,
the prototype from Finding 3 productionised: load, validate every literal
against its declared domain, group rows by match assignment, prove
coverage and overlap per group, emit typed findings as JSON
(`coverage-gap`, `overlap`, `closed-by-escape`, `unprovable-coverage`,
`unknown-literal`). Exit 1 on any blocking finding, 0 with advisories
listed. A lint-bucket test runs it over every table under `docs/tables/`
and asserts, non-vacuously, that a planted gap and a planted overlap are
reported.

**Evaluator.** `src/nexus/tables/resolve.py`: given a table and an
assignment of every declared dimension, return the single matching row or
a typed refusal (`no-match`, `ambiguous-match`, `unknown-value`). The
evaluator never breaks a tie; ambiguity at runtime is a defect the checker
should have caught, and it is reported as such.

**Three consumers.**

| Table | Consumer | What changes |
| --- | --- | --- |
| `docs/tables/rdr-lifecycle.toml` | `nx rdr set-status` (`src/nexus/commands/rdr.py:413`) | Replaces `_KNOWN_STATUSES` membership with a `resolve()` over (current status, requested status as event); an illegal edge refuses with the row's `refuse` code. `rdr_hook.py`'s `_STATUS_ORDER` and the test's `_README_STATUS_WORDS` are derived from the table's `status` domain, ending the three-way disagreement in Finding 1. `rdr-audit` reports any file status outside the domain. |
| `docs/tables/release-choreography.toml` | `check_engine_release_floor.py`, `check_client_release_precondition.py` | The scripts keep their sensors (git, gh, HTTP) and reduce each to its finite outcome, then call `resolve()` on one table with an explicit `event` dimension (`pre-tag`, `tag-push`, `deploy`, `post-deploy-verify`). Messages move to a catalog keyed by row id. O1 becomes impossible by construction; O2 becomes a visible row pair the author must either keep or merge. |
| `docs/tables/rdr-dependencies.toml` | RDR indexer, `set-status`, `rdr-audit` | Not a transition table: an edge list `(from, relation, to)` seeded at index time from the existing `supersedes`, `superseded_by`, `parent_rdr`, `related_rdrs` frontmatter (Finding 4) into catalog links, after a canonical-tumbler rule. `set-status` on a record marks each dependent's T2 entry `needs-reexamination: <from> <old>-><new>`; `rdr-audit` lists them. |

### Existing Infrastructure Audit

- `scripts/check_wire_contract_pairing.py` already holds one shared ledger
  parser and, since nexus-hcdk3, one shared classifier; the release table
  extends that pattern from one dimension to all of them.
- `tests/scripts/conftest.py` already isolates gate tests onto fixture
  ledgers; the table fixtures reuse the same mechanism.
- `nx rdr set-status` already exists as the single writer of record status
  (Finding 1); the table replaces its membership check, not its role.
- The catalog's `link`, `traverse` and `search_graph_hop` already implement
  typed edges and BFS (Finding 4); Gap 3 adds edges, not machinery.
- `tests/test_docs_citation_rot.py` and RDR-081's validator establish the
  report-only lint posture the checker follows.
- Nothing in the repo evaluates a data-driven decision table today; the
  evaluator is new code, sized by the prototype at under 150 lines.

### Decision Rationale

The alternative is to keep patching prose and branches. Both production
incidents, the twelve-status drift, and the live O1 overlap found during
this RDR's research are the measured cost of that. A table is the smallest
artifact that can be proved complete, the checker is small because the
domains are enums, and one table consumed by two scripts is the only design
under which they cannot disagree.

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

Three new data files and a checker join the lint bucket. The release scripts shrink only slightly (net 52 lines across both; the decision blocks became emit calls carrying their substitutions) while the system grows: table, catalog and shared evaluator add about 1,500 lines -- see the 2026-09-02 Revision History entry for the measured figures against Finding 2's estimate. `nx rdr set-status` gains a refusal path.

### Risks and Mitigations

- The checker itself is a small state-machine evaluator and can have the bugs
  it exists to find. Mitigation: test it against planted coverage gaps and
  overlaps (non-vacuity assert), and against the two recorded incidents as
  fixtures that must be reported.
- Over-formalising vocabularies that are in fact open. Mitigation: the checker
  refuses to claim coverage over a non-enum dimension rather than pretending.
- The release rewrite touches the scripts that gate releases. Mitigation:
  Phase 2 keeps the old decision path behind the new one and asserts
  identical verdicts over all 101 enumerated cells before the old path is
  deleted (Finding 2's cell table is the fixture).
- Migrating the status vocabulary retires seven never-written values,
  merges `scrapped` into `abandoned` across seven files, and rejects five
  out-of-vocabulary values in the wild. Mitigation: the migration is one
  scripted sweep in Phase 1 with a before/after census committed alongside
  it; the companion documents get `kind: companion` rather than a status.

### Failure Modes

| Failure | Detection | Consequence |
| --- | --- | --- |
| Checker has a bug and passes a gap | Planted-defect tests in the lint bucket (non-vacuity) | Same exposure as today, not worse |
| A sensor reduces an open value to the wrong outcome | Phase 2 parity assertion over the 101-cell fixture | Wrong verdict from a right table; caught before the old path is deleted |
| Message text drifts while exit code and classifier marker are preserved | Phase 2's (exit_code, message_key) parity alone does not catch this (critique T2 nexus/critique-nexus-j9z30-14-2026-09-02 [24073] finding (a)). During the cutover it was closed by a live old-vs-new full-TEXT byte-equality assertion (23 mismatches found, six real). Since P2.6 deleted the old path it is closed by two standing assertions in `tests/scripts/test_release_table_parity.py`: `test_real_function_text_matches_frozen_oracle` byte-compares every cell's stdout and stderr, separately and normalized, against `tests/scripts/fixtures/release_cell_texts.json` (the text frozen at the cutover, when it was proven identical to the deleted branches on both streams), and `test_real_function_fills_every_catalog_placeholder` catches a substitution key a call site forgot. Regenerating the oracle is a deliberate act in the same commit as a message change | A content-level regression (dropped caveat, wrong remedy pointer, stale URL) in the catalog's operator-facing prose ships silently once the old, verified-correct text is deleted at Phase 2's cutover |
| Two rows disagree at runtime (`ambiguous-match`) | Evaluator refuses with the row ids | Release or status change halts loudly; the checker should have caught it, so this is also a checker defect |
| A record cites a dependency the catalog cannot resolve to one tumbler | Index-time warning naming both candidates | Edge not created; audit shows the record as unlinked |
| Table file missing or unparsable | Consumer refuses to run, exit 2 | No silent fallback to the old imperative path |

## Implementation Plan

### Prerequisites

- nexus-hcdk3 closed (done 2026-09-01), so Phase 2 starts from two gates
  that already agree.
- The status domain ruling, given 2026-09-01 and recorded in the
  Technical Design.

### Minimum Viable Validation

The lifecycle table lints clean in CI; `nx rdr set-status <id> closed` on a
`draft` record refuses with `illegal-transition`; the same command on an
`accepted` record succeeds; a planted second `accept` row makes CI red with
`overlap`. All four run in one test module.

### Phase 1: Table format, checker, evaluator, lifecycle table

1. Land `src/nexus/tables/` (check, resolve, TOML loader) with the
   prototype's 17 tests ported and the non-vacuity pair.
2. Author `docs/tables/rdr-lifecycle.toml`; lint it in the lint bucket.
3. Rewire `set-status`, `rdr_hook.py`'s ranking, and the tripwire test's
   status words to the table's domain; delete the three literals.
4. Scripted sweep of `docs/rdr`: `scrapped` to `abandoned` (seven files,
   T2 keeps `scrapped`); companions and RDR-200 sub-documents to
   `kind: companion`; the one `revised-after-implementation` to `closed`
   plus `kind: companion`; README index rows updated by the same script;
   before/after census committed with it.

### Phase 2: Release choreography

1. Enumerate the reachable cells mechanically (Finding 2 estimates about
   101) as a fixture with today's verdicts; this is also where the five
   leaves Finding 2 left without a coverage verdict are settled.
2. Author `docs/tables/release-choreography.toml`; lint clean, no escape
   rows on blocking groups. (Authored with an event column at P2.3; the
   column came out at P2.6 under the nexus-j9z30.26 ruling, and a lint
   test now refuses its return -- see Revision History.)
3. Route both scripts through `resolve()`; run old and new paths side by
   side over the fixture; assert identical verdicts; delete the old path.
4. Decide O2 (paired vs paired-auto order asymmetry) as a row pair, with
   Sam's ruling recorded in the table comment.

### Phase 3: Dependency edges

1. Canonical-tumbler rule for RDR documents; collapse the ~10 owner ids.
2. Seed edges from existing frontmatter at index time.
3. `set-status` marks dependents `needs-reexamination`; `rdr-audit` lists
   them; the first run's list is the measured backlog.

### Day 2 Operations

Adding a status or a release mode is one row plus its domain member; CI
refuses the change until every affected group is covered again. Nothing to
run by hand.

### New Dependencies

None. `tomllib` is standard library on 3.12.

## Test Plan

- Unit: the ported prototype suite (17), plus loader rejection of unknown
  literals and duplicate row ids.
- Lint bucket: every table under `docs/tables/` lints clean; a planted gap
  and a planted overlap in a fixture copy are reported (non-vacuity).
- Phase 2 parity: 101-cell fixture, old path vs new path, identical
  verdicts; then the old path is deleted and the fixture pins the table.
- `tests/scripts/test_ledger_gate_parity.py` (already landed) stays as the
  standing agreement test between the two release gates.
- Phase 3: an index of `docs/rdr` produces exactly the 6 `supersedes`
  edges the frontmatter actually declares (non-vacuity floor). The "23"
  this line first carried counted files with a `supersedes:` key; 19 of
  them declare `supersedes: []` and one names a non-RDR path -- the
  arithmetic was wrong, not the generator (Phase 3 critique [24089]).

## Validation

### Testing Strategy

Every table change is exercised by the checker in CI before any consumer
sees it. Consumers are tested against fixtures, never the live ledger,
except the one `real_ledger` agreement test that exists precisely because
fixture isolation hid O1.

### Performance Expectations

Tables have under 200 rows and under six dimensions; the product bound is
in the low thousands. Checker and evaluator run in milliseconds; no
performance work is expected or planned.

## Finalization Gate

### Contradiction Check

The problem statement originally called both release incidents "an overlap
and a coverage gap"; Finding 2 corrected that, and the Approach now carries
the event column the correction requires. No other internal contradiction
found on re-read.

### Assumption Verification

All four Critical Assumptions carry Verified or Refuted labels backed by a
T2 research entry with file:line evidence.

### Scope Verification

In scope: three tables, one checker, one evaluator, the three named
consumers. Out of scope: the upgrade ladder (RDR-185), the plan-selection
policy (open domain), any wire-level refusal vocabulary for MCP tools.

### Cross-Cutting Concerns

- The lifecycle table ships inside the package at
  `src/nexus/tables/rdr-lifecycle.toml` (only `src/nexus` reaches the wheel,
  and `nx rdr set-status` runs in any repo), with a byte-identical copy under
  `conexus/resources/tables/` for the SessionStart hook, which runs under
  bare system Python. The release table stays repo-only under
  `docs/tables/`; its consumers are scripts that never ship. `AGENTS.md`
  gains one line pointing at both once Phase 1 lands.
- The four beads filed with this RDR (nexus-tpuct, jh86x, 1c7oq, 7mudt) are
  independent; nexus-1c7oq's advisory format is what `closed-by-escape`
  should print.

### Proportionality

Net code is roughly neutral (Finding 2: about 120 lines saved on the
release scripts, about 340 added for the checker). The gain is that three
hand-maintained vocabularies become provable, and one live overlap of the
class this RDR names was found and fixed during its own research.

## References

- `cwensel/intrastate` at `bc9f2a0`; `models/rdr.toml`; `docs/model-authoring.md` (references Mealy 1955, Dijkstra 1975, King 1968, OMG DMN 1.5).
- T3: `analysis-codebase-intrastate-runtime-kernel-2026-09-01`, `analysis-deep-intrastate-rdr-apparatus-2026-09-01`; T2 `nexus/critique-intrastate-nexus-comparison-2026-09-01` [23971].
- Beads filed from the same comparison: nexus-tpuct, nexus-jh86x, nexus-1c7oq, nexus-7mudt.

## Revision History

- 2026-09-01: Created (draft).
- 2026-09-01: Research findings 1-4 recorded; Alternative 1 refuted; incident classification corrected (event dimension); nexus-hcdk3 filed from Finding 2.
- 2026-09-01: Status domain ruled by Sam: six values, scrapped merged into abandoned, deferred resumes to draft only, supersede guarded on superseded_by. Recorded in Technical Design.
- 2026-09-01: Accepted; planning chain run (epic nexus-j9z30, T2 [23998], audit residuals [23999], enrichment deltas T2 nexus/plan-rdr-201-enrichment-deltas). Path correction: the lifecycle table lives in the package, not `docs/tables/`; the release table stays in `docs/tables/`.
- 2026-09-01: Sam ruled defer is legal from `accepted` as well as `draft` (P1.3 open question).
- 2026-09-01: Phase 1 complete on develop (epic nexus-j9z30 beads .1-.10): table format, loader, checker (with unmatched-assignment, unused-dimension and zero-dim closed-by-escape added by review), evaluator, packaged lifecycle table with the six-value ruling, set-status and every status list derived from it, docs/rdr swept to the domain, rdr-audit vocabulary findings. Approach item 3 lands one of its three tables in this phase; the release table is Phase 2 (.13), the dependency edges Phase 3. Phase gate: Item1=.1, Item2=.2, Item3=.3.
- 2026-09-01: Gate critique [23988], 4 significant: RDR-024 relabelled Precedent (the hard accept guard came with set-status, not RDR-024); RDR-149 added as the shared-primitive precedent; match-key shape rule added to the Technical Design; the 101-cell figure marked as an estimate with its 96-cell coverage sum reconciled to Phase 2.
- 2026-09-02: Sam ruled event-invariance CORRECT (nexus-j9z30.26). The 7.1.0/v0.1.62 fix (79fff05a9) changed a remedy string and prose, no decision logic; the event-sensitivity is real but lives in the release / engine-release skill choreography, not in the two scripts' inputs, so a table over those inputs is event-invariant by construction. The table-wide `event`/`mode` dimensions P2.3 declared were removed at P2.6; Approach item 3 and Phase 2 step 2 amended above; the table header records why and `test_choreography_table_declares_no_event_or_mode_dimension` refuses their return.
- 2026-09-02: Sam ruled O2 KEEP both rows (nexus-j9z30.17): `--paired-deploy-auto` is the unattended path (release.yml) and trusts the cloud probe; `--paired-deploy` is attended, where refusing and asking is cheap. Recorded as comments at both rows and in the table header; zero behaviour change.
- 2026-09-02: Phase 2 complete on develop (nexus-j9z30.11-.17, nexus-w2x5x): 89 cells enumerated, table authored, both scripts routed through one shared module (`scripts/release_choreography.py`), old path deleted after per-cell parity on verdict and per-stream text; one cell's stream (`main_bare_tracker_opt_out`, exit 0 on stderr) carried by an `emit.stream` field. Measured size against Finding 2's estimate (about 580 replaced, about 460 added, net 120 saved): the two scripts shrank 52 lines; the table (834), catalog (554) and shared evaluator (124) added about 1,500 -- net about +1,460, not -120. The auditability case (scattered guards becoming checked, disjoint, total rows) stands; the size claim did not, and Trade-offs is amended. The message-drift Failure Modes row is restated for the post-cutover harness (frozen per-stream text oracle plus placeholder check).
- 2026-09-02: Phase 3 complete on develop (nexus-j9z30.20-.24): canonical-tumbler rule, dependency edges seeded from frontmatter (6 `supersedes`, ~259 `relates`), `set-status` marks the records a flipped record SUPERSEDES `needs-reexamination`, the edge named on the marker (Sam, 2026-09-02: `supersedes` edges only, and successor -> predecessor only -- a successor's flip leaves its predecessor's `superseded` verdict stale; a predecessor's flip marks nobody), `rdr-audit` lists markers and, since the SessionStart reconciler was deleted under nexus-e19sa, prints a `DRIFT:` line per file-vs-T2 status disagreement -- detection, not reconciliation. Markers are cleared by hand after re-examination. Test Plan's Phase 3 floor corrected from 23 to the 6 real edges. Measured backlog at landing: no markers exist yet; of the 6 edges, three predecessors carry a verdict inconsistent with being superseded (RDR-014 closed, RDR-112 abandoned, RDR-159 closed) -- recorded on nexus-j9z30.22, not marked retroactively.
