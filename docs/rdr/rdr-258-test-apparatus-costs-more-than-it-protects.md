---
title: "The test apparatus costs more than it protects: repo-accounting checks report instead of fail, gates retire on evidence, tests check behaviour"
id: RDR-258
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-23
accepted_date:
related_issues: []
related_rdrs: [RDR-109, RDR-214]
---

# RDR-258: The test apparatus costs more than it protects: repo-accounting checks report instead of fail, gates retire on evidence, tests check behaviour

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

The repository now spends more of its effort keeping its test and gate
apparatus green than shipping features users notice. This RDR measures that,
and grounds each proposed change in a named source or a measured repo figure.

Terms used throughout:

- **develop** is the integration branch. Sessions push to it directly, and
  every push starts a CI run (continuous integration: GitHub Actions
  `ci.yml`). A **red** is a CI run that failed; a **green** passed.
- The **merge gate** is the `pytest-gate` job in `ci.yml`. It reads the
  result of ten other jobs and decides whether the push is green.
- A **product test** exercises nexus code the way a user or a caller would.
- A **repo-accounting check** (an **accounting check** for short) does not
  exercise the product. It inspects the repository itself: the text of test
  or source files, a count of tests, a citation in a document, a byte size.
  Nexus has several kinds:
  - a **lint module**, a test file in the `-m lint` bucket that scans the
    tree;
  - a **census**, a test that enumerates code sites or tests and checks each
    against a rule or an allowlist;
  - a **ceiling**, a committed number that a measured count may not exceed
    (for example `tests/storage_touch_ceiling.toml`);
  - a **ratchet**, a ceiling meant to move only down;
  - a **ledger**, a committed file that must list each change of some kind
    (for example `docs/wire-contract-pending.md`).
- A **pin** is a test that asserts an exact value, such as a line of text in
  a repository file.
- A **product catch** is a red that was fixed by changing product code (under
  `src/` or `service/src/main`) rather than the test or a ledger.

### Enumerated gaps to close

#### Gap 1: Most reds are not product defects

Over sixty days of develop CI, 63.6% of red runs were accounting checks and
2.4% were product defects; the accounting reds kept develop red for 188 hours
(the red census, 258-research-1). A red that names no product defect still
stops every session that pushes behind it, and it trains readers to treat red
as noise.

#### Gap 2: Gates are added per incident and never retired

159 of the 200 lint modules were added since 2026-09-01, about one a day.
Ceiling commits since 2026-10-04 raised a ceiling 67 times and lowered one 4
times, and since 2026-10-04 no lint module was retired on its merits
(258-research-4). Gates leave only when Sam orders a burst of deletion. There is
no standing process that asks whether a gate still earns its cost.

#### Gap 3: Ledger and ceiling churn

31% of the non-merge commits in the window touched only tests, ledgers or
ceilings, and the wire-contract ledger alone took 562 commits (258-research-3).
Each of those commits is work that adds no feature and fixes no defect.

#### Gap 4: Tests coupled to structure and ambient state

10.9% of red runs came from behavioural tests that were stale after an intended
change, or that depended on state outside the test (ambient state: the host,
the clock, files another test left behind). The Windows rehearsal spent most of
its red time on test portability rather than on product failures
(258-research-1, 258-research-2).

#### Gap 5: No selection: every code push runs everything

Every code push runs the whole default loop of 59,278 tests in six shards plus
the lint, census and docs-reader legs, and the merge gate waits on ten jobs
(258-research-6). Selection exists only for integration tests and for
docs-only pushes (258-research-13). Accounting checks are not a function of the
change, so a red in one blocks a push that did not touch what it measures.

#### Gap 6: Accounting reds block on Sam

A ceiling value is a pre-declared test criterion. Sam's standing rules reserve
changes to pre-declared test or promotion criteria to him, and
`tests/storage_touch_ceiling.toml` says a raise "needs a reviewed reason in that
commit's message" (258-research-13). A new Liquibase changeset or a new
tenant-provisioning test moves a ceiling, so some ordinary product commits wait
on an ask to Sam before develop can go green (258-research-4). The person whose
time is scarcest is asked to approve bookkeeping.

## Relationship to Prior RDRs

Searched the `docs/rdr/README.md` index for titles naming test, gate, lint,
ratchet, census, ledger, flake, suite or budget, and read the two that bear on
this problem. No draft RDR overlaps.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-109 | Origin of the mode-declarations census | Its Phase 1 added `test_mode_declarations_are_explicit` as "a grep-based heuristic with a documented exclusion list", when cloud-mode tests were "~30 out of 7000+". The file is 1,713 lines at e4d7345726 and is the third-largest source of accounting reds (258-research-2). The rationale (no test may pass on leaked mode state) still holds; the means (a census that fails the build) is what this RDR revisits. |
| RDR-214 | Precedent | Abandoned 2026-09-18 by Sam, in part because "the design adds process tooling". This RDR must remove more process than it adds: its one new tool is a report script, and nothing it adds fails a build. |

The 2026-10-03 cleanup (bead nexus-0r1uz) was not an RDR; it is the one-off
alternative below.

## Context

### Background

Four read-only research strands measured the apparatus on 2026-11-23 (T2
`nexus/test-apparatus-rdr-evidence-2026-11-23`):

- A, composition of the suite at tree 67f0a414ba;
- B, the red history of develop from 2026-09-23 to 2026-11-23;
- C, where each gate came from and whether any was retired;
- D, what published practice says about the cost and value of tests (T3
  `nexus-engineering-practice` / `sources-test-cost-and-value-2026-11-23`).
  The digest marks every source VERIFIED (fetched, quote checked), SEARCH or
  UNVERIFIED. This RDR cites only VERIFIED sources.

On 2026-11-22/23 there were eight develop reds. One was a product defect; seven
were accounting checks (the wire ledger, the cloud-leg route ledger, the plugin
ledger, the context-budget byte ceiling, conf-line pins, the WAL ceiling and the
storage-touch ceiling). The sixty-day census behind this RDR is 258-research-1.

### Technical Environment

Python suite under pytest with xdist, collected into a default loop
(`-m "not integration and not slow and not lint"`), a `lint` bucket, a census
job, a docs-reader job and an affected-integration job; the Java engine suite
runs in Service CI. Develop CI runs on GitHub-hosted runners plus the
self-hosted lab pool. The repo's CI cost rules (`.claude/rules/ci-and-runners.md`)
apply to every workflow change here: expensive jobs run only when their inputs
changed, full breadth only at merge boundaries, a tree that passed is not tested
twice.

## Research Findings

### Investigation

Strand B downloaded all failed-job logs in the window, extracted the failing
tests and steps, labelled each red run by rule plus a fix signal (did the
commits up to the next green touch the failing test, and did they touch product
code?), and hand-judged a stratified sample of 60 runs to convert the automatic
labels into a population estimate. Strand C dated every lint module and
ceiling-file commit from git history and read the commit bodies for the incident
each gate answered. Strand A collected the whole suite and classified items with
an AST heuristic, checked by hand on 26 items. Strand D fetched the sources.
This RDR also read the five Phase-1 checks' module docstrings and the existing
advisory and selection mechanisms in `ci.yml` (258-research-13, 258-research-14).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `.github/workflows/ci.yml` pytest-gate | Yes | mcpb and constraints resolve gates already block only on a PR into main and warn elsewhere (258-research-13) |
| `scripts/ddl_ratchet.py`, storage-touch fan-in | Yes | `--advisory`, `--bytes-advisory` and a report mode exist (258-research-13) |
| `scripts/select_affected_integration_tests.py` | Yes | direct-import selection; 146 of 200 develop commits selected nothing (258-research-13) |
| The five Phase-1 checks | Yes | each guards a named incident (258-research-14) |

### Key Discoveries

- **✅ Verified** (source search): the red census. 787 red runs in sixty days.
  Product defect 19 (2.4%); accounting 501 (63.6%, 188 h of CI red time);
  flake or infrastructure 119 (15.1%, about 80% of it flaky tests); stale or
  ambient-coupled behavioural tests 86 (10.9%); harness and CI-tooling
  self-tests 59 (7.4%); build or ruff 4 (0.5%). The automatic accounting label
  was right in 22 of 22 sampled runs; the behavioural labels are noisier (39 of
  60 overall agreement), so the 10.9% and 7.4% figures are the least certain.
  *Source: 258-research-1.*
- **✅ Verified** (source search): the cost of red. CI was red 32% of decided
  runs and about 19% of the window's wall-clock. Red runs used about 399 hosted
  hours and 650 self-hosted runner-hours, of which the accounting class took 278
  and 461. By check, the top sources of red runs were the storage-touch ratchet
  68, `docs_reference_rot` 56, the mode-declarations census 47,
  `cli_runner_output_invariant` 27, `module_seam` 26, wire pairing 21 and
  `tmp_root` 20. *Source: 258-research-2.*
- **✅ Verified** (source search): the commit mix. 2,521 of 8,165 non-merge
  commits (31%) touched only tests, ledgers or ceilings, 0.74 for every product
  commit; 67% of red-fix commits touched no product code. Ledger churn:
  `docs/wire-contract-pending.md` 562 commits, `conexus/PENDING_RELEASE.md`
  402, `tests/ddl_ceiling.toml` 110, `tests/storage_touch_ceiling.toml` 81.
  *Source: 258-research-3.*
- **✅ Verified** (source search): gate origins. The lint bucket is 3,380 tests
  in 200 modules; 159 modules were added since 2026-09-01. About half answer an
  incident (67 born in a `fix()` commit, 103 fix-born or citing a failure); the
  rest came from research adoption (the nexus-3ceda batch: file-size ratchet,
  context budgets, scrub gate, changelog fragments, sleep ratchet) or from cost
  ratchets after the 2026-10-18 pool halt (DDL, WAL, storage-touch). Ceilings:
  67 raises, 4 lowers since 2026-10-04. Retirement: only in bursts Sam ordered;
  none on merit since 2026-10-04. *Source: 258-research-4.*
- **✅ Verified** (source search): the checks that guard a failure a user can
  reach are few: wire pairing (its incident nexus-sh9v2 damaged 910 live
  documents), mcpb and constraints resolve, native embedded resources,
  mineru-weights-published, user-surface verb rot and CLI reference coverage.
  204 of 601 failed develop CI runs (34%) were red only on lint, census,
  docs-reader or ratchet legs. *Source: 258-research-5.*
- **✅ Verified** (source search): suite composition. 64,223 items collected,
  59,278 in the default loop; about 64% product tests; exact-text asserts about
  15%, mostly product CLI messages, of which about 386 pin repository content.
  The heuristic is good to about ten points. *Source: 258-research-6.*
- **✅ Verified** (source search): the 2026-10-03 cleanup cut the lint bucket
  from 1,228 to 1,097 tests; seven weeks later it is 3,380, 3.1 times its
  post-cleanup size. *Source: 258-research-7.*
- **⚠️ Documented** (docs only): published practice on brittle tests. Google's
  definition: "a brittle test is one that fails in the face of an unrelated
  change to production code"; "write a test for each behavior." Beck's Test
  Desiderata: tests should be structure-insensitive and "cheap to write relative
  to the cost of the code being tested." Dodds: implementation-detail tests "can
  give you a false negative when you refactor." No verified source gives a rule
  for deleting tests. *Source: 258-research-8.*
- **⚠️ Documented** (docs only): a fitness function (an automated check of an
  architectural property) "will issue a warning or maybe even 'break' (stop)
  the build" (Much, InfoQ, quoting Ford, Parsons and Kua). Warning is a
  recognised mode; trending is not in the verified text and is this RDR's
  inference. *Source: 258-research-9.*
- **⚠️ Documented** (docs only): flaky tests. Fowler: quarantine them, "but fix
  quarantined tests quickly", because "tests keep getting thrown into
  quarantine and forgotten." Gradle runs its quarantine in a nightly job and
  isolates it "from the CI pipelines for developer feedback." Google: "as you
  approach 1% flakiness, the tests begin to lose value." *Source:
  258-research-10.*
- **⚠️ Documented** (docs only): test selection. Machalica et al.'s predictive
  selection at Facebook halved the infrastructure cost of testing changes while
  still reporting over 99.9% of faulty changes; it needed to model flakiness.
  Google runs large tests in "a separate post-submit continuous build." Google's
  presubmit/postsubmit details for ordinary tests are not verified in the
  digest and are not relied on. *Source: 258-research-11.*
- **⚠️ Documented** (docs only): Google's mutation testing is diff-based and
  surfaced in code review to 6,000 engineers; the verified abstract gives no
  usefulness figure. *Source: 258-research-12.*
- **✅ Verified** (source search): nexus already has the mechanisms the design
  needs: warn-on-develop, block-on-release-PR gates; advisory modes on the DDL
  and storage-touch ratchets; a nightly-only workflow; and two change-based
  selectors. *Source: 258-research-13.*
- **✅ Verified** (source search): what each Phase-1 check guards, from its own
  docstring. *Source: 258-research-14.*

### Critical Assumptions

- [ ] CA-1: An accounting red does not hide a product defect, so demoting the
  class to a report loses no product catch. — **Status**: Verified for the
  window (22 of 22 sampled accounting runs had no product fix; 258-research-1);
  re-checked by Phase 0's report on each check before it is demoted.
  — **Method**: Source Search
- [ ] CA-2: The release PR into main and the engine-release workflow both run
  the wire-pairing check, so moving its blocking role there still stops an
  undeclared both-halves change before any user receives it. — **Status**:
  Unverified. — **Method**: Source Search, at Phase 1 step 5, before the change.
- [ ] CA-3: A check that reports instead of failing is still acted on.
  Fowler's warning about forgotten quarantines says it may not be.
  — **Status**: Unverified; measured by the Day-2 metrics and the review
  cadence. — **Method**: Spike (one review cycle).
- [ ] CA-4: Direct-import selection on develop pushes misses few failures that
  the full run would catch. The integration selector exists, but the evidence
  has no miss rate for it. — **Status**: Unverified. — **Method**: Spike (a
  shadow window, Phase 5).

## Proposed Solution

### Approach

Treat the test apparatus the way Google's and Beck's definitions treat a single
test: it earns its place by catching product failures, and its cost is counted.
Eight elements, each grounded below.

**(a) Two classes of check.** Every check that is not a product test is either
*user-reachable* (class U: it guards a failure a user can receive, and keeps
failing the build) or *accounting* (class A: it reports its value and trend as a
fitness function in warning mode, is reviewed on a cadence, and fails only on a
large regression past an explicit alarm line written in the check). Grounds: the
warn mode of fitness functions (258-research-9); CA-1; the red census
(258-research-1). The class of each named check, from strand C's inventory
(258-research-5):

| Check | Class | Why |
| --- | --- | --- |
| Wire pairing (`scripts/check_wire_contract_pairing.py`) | U | nexus-sh9v2 damaged 910 live documents; origin nexus-1vogq, released clients failed manifest writes for 34+ h |
| mcpb resolve, constraints resolve | U | a broken install reaches users; already warn on develop, block on the release PR |
| Native embedded resources | U | a missing resource ships in the native binary |
| mineru-weights-published | U | PDF extraction fails for users without the weights |
| User-surface verb rot, CLI reference coverage | U | a user follows a documented command that does not exist |
| Storage-touch ratchet, DDL and WAL ceilings | A, cost | CI tenant and disk cost (2026-10-18 pool halt) |
| File-size ratchet, context budgets, sleep ratchet | A, hygiene | repo shape; no user path |
| Scrub gate | A, hygiene (strand C's class) | confirm in Phase 0 that it guards nothing that is published |
| `docs_reference_rot` | A, hygiene | citations inside RDRs and docs |
| Mode-declarations census, `module_seam`, vacuity floors, shard partition | A, apparatus | they guard the test suite's own correctness |
| Plugin ledger, changelog fragments, ci-evidence, push script | A, process | release bookkeeping; the release itself has its own gates |
| `cli_runner_output_invariant`, `tmp_root`, the other lint modules | to classify in Phase 0 | not classified by strand C |

The class of the remaining lint modules is written into each module by Phase 0,
not typed into this RDR.

**(b) Retirement is part of the normal process.** Each check carries three
header lines: `owner`, `guards` (the incident or bead it answers) and `class`.
Its red count and product-catch count are derived, never hand-kept: a report
script reads CI history the way strand B did (failing item per red run, plus the
fix signal) and prints, per check, reds, product catches and red hours in a
window. An accounting check with no product catch in N days is retired at the
next review; N and the review cadence are Sam's (Finalization Gate). Grounds:
the 67-to-4 ceiling record and the absence of any merit retirement since
2026-10-04 (258-research-4); the 3.1x regrowth after a burst cleanup
(258-research-7). No verified source gives a retirement rule; this element rests
on the measured figures.

**(c) Budgets that alert on a trend instead of exact ceilings.** Each ceiling
(storage-touch, DDL/WAL, the 288 file-size exceptions, context budgets, sleep)
becomes a reported value with its trend over recent runs and one alarm line set
well above today's value. Moving the value no longer needs a commit; crossing
the alarm line fails, and the failing message names the commits since the last
report. Grounds: a ceiling that was raised 67 times and lowered 4 is a counter
that every change must increment (258-research-4); Google's "brittle" and Beck's
structure-insensitive property (258-research-8); warn mode (258-research-9).

**(d) Behaviour tests, not repository pins.** The about 386 tests that pin
repository content are rewritten as tests of the behaviour they stand for, or
deleted when no user-visible behaviour stands behind them. The stale and
ambient-coupled behavioural tests (Gap 4) are fixed to own their state, using
Fowler's named causes (isolation, the clock, leaked resources) as the checklist.
Grounds: 258-research-6, 258-research-8, 258-research-10. Deletion has no
verified source; the rule here is the brittleness definition applied to each
pin.

**(e) Quarantine flaky tests off the merge gate.** A test seen flaky gets a
quarantine marker that removes it from the merge gate and runs it in a nightly
job; each quarantine carries a deadline, and the report lists every quarantined
test with its age. Grounds: Fowler and Gradle (258-research-10); flakes are
15.1% of red runs (258-research-1). The existing nightly workflow is the model
(258-research-13).

**(f) Test selection on develop pushes.** A develop push runs the tests the
change selects (files it touches, plus tests that directly import a changed
module, the rule `select_affected_integration_tests.py` already uses); the full
suite and every class-A check run on a schedule and on the release PR into main.
Grounds: post-submit runs for the broad set (258-research-11); the existing
selector (258-research-13). The Machalica result is for a predictive selector,
a different method, so its cost and catch figures do not transfer; it is cited
for the shape (select at push, keep a full backstop) and for its warning that
flaky outcomes corrupt selection, which is why (e) comes first.

**(g) Optional: mutation testing to measure test value.** Mutation testing
changes product code on purpose and asks which tests notice. A diff-scoped run
on review, as Google does, would show which tests catch nothing
(258-research-12). Optional because the verified source gives no usefulness
figure.

**(h) Stop adding a gate per incident.** The review gate's habit of answering a
finding with a new check becomes: fix the behaviour and add a behaviour test of
the product. A new accounting check needs an owner, an incident and an alarm
line, and it lands in report mode. Grounds: about half of the lint modules were
born from incidents (258-research-4); Google's "a test for each behavior"
(258-research-8).

### Technical Design

The three pieces of new code are small and reuse existing patterns.

- **Check header.** Three comment lines at the top of each check module:
  `owner:`, `guards:`, `class: U | A`. A check without them is listed by the
  report as a finding; no test fails on it (a new gate to police the gates
  would repeat Gap 2).
- **Report script** (`scripts/check_value_report.py`, illustrative name):
  input is a date window; it reads run and job data through `gh api`
  (read-only, one call per page, no polling) and the git history; output is a
  table per check (reds, product catches, red hours, class, owner, quarantine
  age) and the four Day-2 metrics. No stored state: it recomputes from the
  record each time. Its first output must reproduce strand B's automatic-label
  counts for 2026-09-23 to 2026-11-23 (Minimum Viable Validation).
- **Report mode for class A.** Reuse the `--advisory` and `::warning::`
  pattern already in `ci.yml` and `ddl_ratchet.py`: the job runs where it runs
  now, prints its value and trend to the job summary, and returns failure only
  past its alarm line. The pytest-gate reads class-A jobs as advisory on
  develop and as blocking only past the alarm line.

```text
// Illustrative: one class-A check result
check: storage_touch   class: A   value: <n>   trend(10 runs): <+k>
alarm_line: <set by Sam>   verdict: report   (fails only when value > alarm_line)
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Report mode for class A | `ci.yml` mcpb/constraints warn-on-develop; `ddl_ratchet.py --advisory`; storage-touch report mode | Reuse: same pattern, extended to each class-A check |
| Nightly quarantine and full run | `local-service-gate-nightly.yml` | Extend: one more nightly job, same caching and concurrency rules |
| Develop-push selection | `select_affected_integration_tests.py`, `select_docs_reader_tests.py` | Extend: the direct-import rule applied to the unit shards |
| Red and catch counts | strand B's one-off scripts (session scratchpad) | Replace with one committed script; the scratchpad copies are not durable |
| Gate inventory | none; each docstring names its incident | Extend: three header lines per check |

### Decision Rationale

The measurements say the apparatus fails for reasons that do not reach users,
and that gates accumulate because nothing removes them. The cheapest move that
addresses both is to change what a class-A red means (a report, not a stop) and
to count each check's catches so that removal has evidence. Every mechanism
already exists in the repo; the design widens their use. It keeps every class-U
check failing, so the guard against the wire-pairing class of damage stays.

## Alternatives Considered

### Alternative 1: Status quo

**Description**: Keep every check failing the build; fix each red as it comes.

**Pros**:

- No change to any pre-declared criterion.

**Cons**:

- The red census and the commit mix stay as they are (258-research-1,
  258-research-3); about one lint module a day keeps arriving
  (258-research-4).

**Reason for rejection**: It is the state Sam asked to leave.

### Alternative 2: Another one-off cleanup like 2026-10-03

**Description**: Delete a batch of checks in one ordered burst.

**Pros**:

- Fast, and proven to land (five batches in one day).

**Cons**:

- The lint bucket regrew to 3.1 times its post-cleanup size in seven weeks
  (258-research-7), because the habits that add gates did not change.

**Reason for rejection**: It treats the stock and not the flow. Its deletion
lists are reused in Phase 1, but only with (b) and (h) in place.

### Alternative 3: Delete all meta tests

**Description**: Remove every lint, census, ceiling and ledger check.

**Pros**:

- Largest immediate cut in red time and churn.

**Cons**:

- Removes the class-U checks too. Wire pairing guards the failure that damaged
  910 live documents (258-research-5); the resolve gates guard installs.

**Reason for rejection**: The few checks that guard users are worth their cost;
the class split keeps them.

### Briefly Rejected

- **Automate the ledger and ceiling edits**: a bot that raises a ceiling on
  each change keeps the churn and makes the ceiling meaningless; the 67 raises
  would simply become free.
- **Predictive (machine-learned) selection now**: it needs stable outcomes and
  history labels, which a suite with 15.1% flaky reds cannot yet give
  (258-research-11).

## Trade-offs

### Consequences

- Positive: most reds by count stop blocking develop; commits that only feed a
  ledger or a ceiling stop being needed; Sam stops being asked to approve
  ceiling raises.
- Positive: removal of a gate rests on its measured catch count, not on a
  cleanup burst.
- Negative: a class-A regression is found at review or at the alarm line, not
  at the push that caused it.
- Negative: with selection, a unit-test failure in an unselected test is found
  by the nightly run, hours after the push.

### Risks and Mitigations

- **Risk**: what each retired or demoted check guarded comes back. The five
  Phase-1 checks guard, in order: CI tenant cost; dangling citations in docs;
  tests passing on leaked mode state; a fake `time.sleep` that hung a run for
  about 50 minutes; and an undeclared engine-plus-client wire change
  (258-research-14).
  **Mitigation**: each keeps running and reporting; only its failing role
  changes. Wire pairing stays class U and keeps failing at the boundaries that
  reach users (CA-2).
- **Risk**: wire pairing reporting on develop lets a both-halves change sit on
  develop undeclared; nexus-sh9v2 shows what such a change can do once
  deployed. **Mitigation**: blocking stays on the release PR and the engine
  release, verified by CA-2 before the change; if CA-2 fails, wire pairing
  keeps failing on develop and only its ledger bookkeeping is simplified.
- **Risk**: report-only checks are ignored (CA-3; Fowler's "forgotten").
  **Mitigation**: the review cadence, the alarm line, and the quarantine
  deadline; the Day-2 metrics show whether reports are read.
- **Risk**: selection misses a failure (CA-4). **Mitigation**: a shadow window
  runs selection and the full suite side by side before selection gates
  anything; the release PR always runs everything.

### Failure Modes

- A class-A value drifts up without a red: visible in the job summary and the
  report; it fails once it passes its alarm line.
- A quarantined test is never fixed: the report lists its age; past its
  deadline it is fixed or deleted at review.
- A selection miss: the nightly full run turns red on a commit whose push was
  green; the report counts these as selection misses, which is the CA-4
  measurement.

## Implementation Plan

Items marked **[Sam]** change a pre-declared test criterion, a gate's failing
role, or a file under Sam's rules (AGENTS.md, CLAUDE.md, the plugin's review
guidance). Each needs his decision before it lands; acceptance of this RDR is
not that decision unless he says so.

### Prerequisites

- [ ] CA-2 verified by source search before Phase 1 step 5.
- [ ] Sam's answers to the Finalization Gate questions (alarm lines, N, review
  cadence, quarantine deadline, metric targets).

### Minimum Viable Validation

The report script, run over 2026-09-23 to 2026-11-23, reproduces strand B's
automatic-label counts (787 red runs; the per-check counts in 258-research-2).
The automatic labels come from rules, so the counts should match exactly; any
difference is explained run by run. That proves the measurement this RDR will be judged
by is repeatable. GitHub expires workflow logs after the repository's retention
period (not checked here), so the run happens before the window's oldest logs
expire, or against strand B's saved extraction (`items.json`, `runlabels.json`)
copied out of the session scratchpad into a durable place first.

### Phase 0: Measure and label

#### Step 1: Report script

Commit the report script, derived from strand B's method. No CI change.

#### Step 2: Check headers

Add `owner`, `guards`, `class` to every check module; class the modules strand
C did not class; confirm the scrub gate's class. No failing role changes.

### Phase 1: The five largest accounting reds

Ordered by measured red runs (258-research-2):

#### Step 1: Storage-touch ratchet (68 red runs) [Sam]

Run the fan-in in its existing report mode on develop; fail only past an alarm
line; stop editing `tests/storage_touch_ceiling.toml` per change.

#### Step 2: `docs_reference_rot` (56) [Sam]

Report on develop and in the nightly job; fix dangling citations in batches at
review.

#### Step 3: Mode-declarations census (47) [Sam]

The census keeps running in the default loop and emits a warning instead of a
failure. In the same step, test whether the leak it guards against (cloud-mode
state carried between tests) can be removed at its cause, the isolation item on
Fowler's list (258-research-10); if it can, the census is retired.

#### Step 4: `module_seam` (26) [Sam]

Report only. A fake that hangs a run still shows up as a red (a timed-out run),
which is an inference, not a measured fact; Phase 0's report checks it.

#### Step 5: Wire pairing (21) [Sam]

After CA-2 holds: report on develop, block on the release PR into main and in
the engine release. If CA-2 fails: keep blocking on develop and only simplify
the ledger entries.

### Phase 2: Ceilings become budgets [Sam]

DDL/WAL, file-size exceptions, context budgets and the sleep ceiling move to
value, trend and alarm line, as in Phase 1 step 1. The file-size hot rule in
AGENTS.md changes with it.

### Phase 3: Flake quarantine [Sam]

Add the quarantine marker and the nightly quarantine job; move each test that
the report shows flaky; set deadlines.

### Phase 4: Behaviour tests [Sam for deletions]

Rewrite or delete the about 386 repository pins; fix the stale and
ambient-coupled tests and the Windows portability failures to own their state.

### Phase 5: Selection on develop pushes [Sam]

Shadow window first (selection and full suite side by side, misses counted, CA-4),
then selection gates develop pushes, with the full suite nightly and on the
release PR.

### Phase 6: Standing retirement and the review habit [Sam]

The review cadence runs the report and retires class-A checks with no product
catch in N days. The orchestration review gate's "new check per finding" becomes
"fix the behaviour; add a behaviour test" (conexus plugin guidance and the
AGENTS.md hot rule on review gates).

### Phase 7 (optional): Mutation testing pilot [Sam]

A diff-scoped mutation run on review for one module, to see whether it finds
tests that catch nothing.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Report script | N/A (one script) | its output | N/A | MVV reproduction | in git |
| Check headers | report lists all | report row | removed with the check | report flags missing headers | in git |
| Quarantine marker | report lists quarantined tests with age | report row | marker removed when fixed | nightly job runs them | in git |

No persistent store is created; the report recomputes from CI history and git.

### New Dependencies

None.

## Test Plan

- **Scenario**: a class-A check below its alarm line regresses — **Verify**: the
  job summary shows the new value and trend, and the merge gate stays green.
- **Scenario**: a class-A check passes its alarm line — **Verify**: the merge
  gate fails and names the check and the commits since the last report.
- **Scenario**: an undeclared both-halves commit on a release PR into main —
  **Verify**: wire pairing fails the PR (a kill-control commit in a test repo,
  as the existing lint's non-vacuity tests do).
- **Scenario**: a quarantined test — **Verify**: it is absent from the merge
  gate's selection and present in the nightly job's.
- **Scenario**: a push that changes one `src/nexus` module — **Verify**: the
  selector picks the tests that import it, and the shadow run records any test
  the full run failed that selection did not pick.
- **Scenario**: the report over the baseline window — **Verify**: it
  reproduces strand B's automatic-label counts (MVV).

## Validation

### Testing Strategy

1. **Scenario**: each phase's change
   **Expected**: the Day-2 metrics, measured by the report over the sixty days
   after the phase, move toward Sam's targets; a phase that moves none of them
   is reverted.

### Performance Expectations

Fewer red runs and fewer runner-hours on red follow from the red census if the
demoted checks stop failing (258-research-2). Selection's saving is measured in
the shadow window, not predicted here.

## Finalization Gate

### Day-2 metrics

Each is measured by the report script the same way strand B measured it, over a
sixty-day window. Baselines are from Research Findings. The targets are Sam's to
set; this RDR does not propose numbers.

| Metric | Baseline | Target |
| --- | --- | --- |
| Red share by category (product, accounting, flake/infra, stale/ambient, harness, build) | 2.4% / 63.6% / 15.1% / 10.9% / 7.4% / 0.5% (258-research-1) | Question for Sam: what accounting share, and what flake share, are acceptable? |
| CI red time | about 280 h, 19% of the window; accounting 188 h (258-research-1, 258-research-2) | Question for Sam: how many red hours per sixty days? |
| Test- and ledger-only commit share | 31% (258-research-3) | Question for Sam: what share? |
| Lint module count | 200 modules, 3,380 tests (258-research-4) | Question for Sam: a number, or "no net growth"? |

Further questions for Sam: the alarm line for each class-A check; N, the days
without a product catch after which a class-A check retires; the review
cadence; the quarantine deadline.

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. One tension is stated: (f) cites Machalica et al. for a
different selector than the one proposed, and says so.

### Assumption Verification

CA-1 is verified for the window and re-checked per check in Phase 0. CA-2 is
verified by source search before Phase 1 step 5. CA-3 and CA-4 are measured by
Phase 1's first review cycle and Phase 5's shadow window.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `gh api` run and job listing | GitHub REST | Source Search (strand B used it for the baseline) |

### Scope Verification

The MVV (the report reproducing strand B's counts) is Phase 0 step 1 and is in
scope.

### Cross-Cutting Concerns

- **Versioning**: N/A (no shipped surface changes).
- **Build tool compatibility**: CI workflow changes follow the repo's CI cost
  rules.
- **Licensing**: N/A
- **Deployment model**: N/A; release and engine-release gates keep the class-U
  checks.
- **IDE compatibility**: N/A
- **Incremental adoption**: each phase stands alone and is reversible by
  restoring a check's failing role.
- **Secret/credential lifecycle**: the report uses the existing `gh` token
  read-only.
- **Memory management**: N/A

### Proportionality

The document carries one design per gap and the decisions Sam must make; the
per-module classification is left to Phase 0 rather than listed here.

## References

- T2 `nexus/test-apparatus-rdr-evidence-2026-11-23` (strands A-D).
- T3 `nexus-engineering-practice` / `sources-test-cost-and-value-2026-11-23`.
- T2 `nexus/test-and-machinery-cleanup-outcome-2026-10-03`.
- T2 `nexus_rdr/258-research-1` through `258-research-14`.
- Winters, Manshreck, Wright, *Software Engineering at Google*, ch11, ch12,
  ch14: https://abseil.io/resources/swe-book/html/ch11.html,
  https://abseil.io/resources/swe-book/html/ch12.html,
  https://abseil.io/resources/swe-book/html/ch14.html
- Beck, Test Desiderata: https://testdesiderata.com/
- Dodds, Testing Implementation Details:
  https://kentcdodds.com/blog/testing-implementation-details
- Fowler, Eradicating Non-Determinism in Tests:
  https://martinfowler.com/articles/nonDeterminism.html
- Gradle, How We Handle Flaky Tests in Gradle:
  https://blog.gradle.org/how-we-handle-flaky-tests-in-gradle
- Much, Fitness Functions for Your Architecture (InfoQ), quoting Ford, Parsons,
  Kua, *Building Evolutionary Architectures*:
  https://www.infoq.com/articles/fitness-functions-architecture/
- Machalica, Samylkin, Porth, Chandra, Predictive Test Selection, ICSE-SEIP
  2019: https://ar5iv.arxiv.org/html/1810.05286
- Petrović, Ivanković, State of Mutation Testing at Google, ICSE-SEIP 2018:
  https://research.google/pubs/state-of-mutation-testing-at-google/

## Revision History
