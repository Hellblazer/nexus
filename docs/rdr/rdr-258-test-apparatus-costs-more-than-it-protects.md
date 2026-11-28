---
title: "The test apparatus costs more than it protects: repo-accounting checks report instead of fail, gates retire on evidence, tests check behaviour"
id: RDR-258
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-23
accepted_date: 2026-11-27
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

Much of the repository's CI red time now goes to keeping its test and gate
apparatus green rather than to product defects: in sixty days, 63.6% of red
runs were accounting checks and 2.4% were product defects, and 7% of non-merge
commits touched only ledgers, ceilings and tests. This RDR measures that, and
grounds each proposed change in a named source or a measured repo figure.

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
  `src/` or `service/src/main`) rather than the test or a ledger. Research
  found this signal noisy: most such fixes were gate reds on in-flight feature
  commits (258-research-15), so Phase 0 checks each one by hand.

### Enumerated gaps to close

#### Gap 1: Most reds are not product defects

Over sixty days of develop CI, 63.6% of red runs were accounting checks and
2.4% were product defects; the accounting reds kept develop red for 188 hours
(the red census, 258-research-1). A red that names no product defect still
stops every session that pushes behind it, and it trains readers to treat red
as noise.

#### Gap 2: Gates are added per incident and never retired

159 of the 200 lint modules were added since 2026-09-01, about two a day.
Ceiling commits since 2026-10-04 raised a ceiling 67 times and lowered one 4
times, and since 2026-10-04 no lint module was retired on its merits
(258-research-4). Gates leave only when Sam orders a burst of deletion. There is
no standing process that asks whether a gate still earns its cost.

#### Gap 3: Ledger and ceiling churn

545 of the 8,165 non-merge commits in the window (7%) touched only ledgers or
ceilings, with or without tests, and 1,067 (13%) touched one of the five ledger
and ceiling files; the wire-contract ledger alone took 562 commits
(258-research-3). The 1,872 tests-only commits (23%) are not counted as churn:
they include new product tests and test-only fixes.

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
| RDR-214 | Precedent | Abandoned 2026-09-18 by Sam, in part because "the design adds process tooling". This RDR must remove more process than it adds. It adds a report script, three header lines per check, a quarantine marker with a nightly job, and a 2-week review. It removes the per-change ceiling and ledger edits, the per-raise asks to Sam, and the failing role of each demoted check. What it adds fails a build only past an alarm line Sam set, and, from Phase 2, in the engine release's new pairing step. |

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
storage-touch ceiling). Since 2026-11-22, four of those seven (the wire ledger,
the cloud-leg route ledger, the plugin ledger and the context-budget ceiling)
also refuse a push before it lands, through `scripts/push_check_gate.py`
(258-research-18). The sixty-day census behind this RDR is 258-research-1.

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
This RDR also read the module docstrings of the five checks Phases 1 and 2
change, and the existing advisory and selection mechanisms in `ci.yml`
(258-research-13, 258-research-14).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `.github/workflows/ci.yml` pytest-gate | Yes | mcpb and constraints resolve gates already block only on a PR into main and warn elsewhere (258-research-13) |
| `scripts/ddl_ratchet.py`, storage-touch fan-in | Yes | `--advisory`, `--bytes-advisory` and a report mode exist (258-research-13) |
| `scripts/select_affected_integration_tests.py` | Yes | direct-import selection; 146 of 200 develop commits selected nothing (258-research-13) |
| The five checks of Phases 1 and 2 | Yes | each guards a named incident (258-research-14) |
| `scripts/push_check_gate.py`, `nx ci check-hold` | Yes | nine checks run against the pushed tree and refuse a push on a red; check-hold holds pushes while develop is red (258-research-18) |
| `scripts/check_wire_contract_pairing.py`, `engine-service-release.yml` | Yes | `--rev-range` defaults to `<newest published v* tag>..HEAD`; the engine-release workflow does not run the script (258-research-16, 258-research-18) |

### Key Discoveries

- **✅ Verified** (source search): the red census. 787 red runs in sixty days,
  by workflow CI 602, Windows rehearsal 85, Service CI 62, plugin drift ledger
  24, other 14. By category: product defect 19 (2.4%); accounting 501 (63.6%,
  188 h of CI red time); flake or infrastructure 119 (15.1%, about 80% of it
  flaky tests); stale or ambient-coupled behavioural tests 86 (10.9%); harness
  and CI-tooling self-tests 59 (7.4%); build or ruff 4 (0.5%). The category
  counts are estimates corrected by a 60-run hand sample, not automatic-label
  counts, and they sum to 788 against 787 runs; the record does not explain the
  difference. The automatic accounting label was right in 22 of 22 sampled
  runs; the behavioural labels are noisier (39 of 60 overall agreement), so the
  10.9% and 7.4% figures are the least certain. *Source: 258-research-1.*
- **✅ Verified** (source search): the cost of red. CI was red 32% of decided
  runs and about 19% of the window's wall-clock. Red runs used about 399 hosted
  hours and 650 self-hosted runner-hours, of which the accounting class took 278
  and 461. By check, the top sources of red runs were the storage-touch ratchet
  68, `docs_reference_rot` 56, the mode-declarations census 47,
  `cli_runner_output_invariant` 27, `module_seam` 26, wire pairing 21 and
  `tmp_root` 20. *Source: 258-research-2.*
- **✅ Verified** (source search): the commit mix. Of 8,165 non-merge commits,
  3,427 (42%) touched product code; 1,872 (23%) touched only tests; 545 (7%)
  touched only ledgers or ceilings, with or without tests. The 31% accounting
  total (2,521) adds the tests-only commits, which include new product tests.
  67% of red-fix commits touched no product code. Ledger churn:
  `docs/wire-contract-pending.md` 562 commits (strand B counts 535; the method
  difference is not recorded), `conexus/PENDING_RELEASE.md` 402,
  `tests/ddl_ceiling.toml` 110, `tests/storage_touch_ceiling.toml` 81,
  `scripts/file-size-exceptions.txt` 16; 1,067 commits (13%) touched one of
  these five files. *Source: 258-research-3.*
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
- **✅ Verified** (source search): what each of the five checks of Phases 1 and
  2 guards, from its own docstring. *Source: 258-research-14.*
- **✅ Verified** (source search): the accounting label is not a safe unit for
  demotion. Of 856 items the census labelled accounting, 18 were followed by a
  fix in `src/` or `service/src/main/`, and three checks in that class caught
  real product defects: `test_answer_shape_query_envelope` (fix f278defe1a),
  `TaxonomyCoverageTest` (fix 2413db4999, coverage past its 30 s bound on a fresh
  install) and `test_hooks_json_verb_release_floor` (fix 47bc97a20f, older CLIs
  exit 2 on new hook verbs and block prompts). Most of the other 15 were gate
  reds on in-flight feature commits. The label is a file-name and message regex,
  so it holds some product tests; the 22-of-22 sample measured label agreement,
  not product catch. None of the 18 followed a red in the five checks of Phases
  1 and 2. *Source: 258-research-15.*
- **✅ Verified** (source search): the release PR into main runs wire pairing
  (`tests/test_wire_contract_pairing_lint.py` is in test-lint, pytest-gate fails
  on it, and main's protection requires pytest-gate). The engine release does
  not: `engine-service-release.yml` has no pairing check, the engine-release
  skill's tag step checks only Service CI's Java tests and the native trip-wire,
  and `check_engine_release_floor.py` reads `## Unshipped`, which cannot see an
  undeclared commit. Engines are tagged from develop. *Source: 258-research-16.*
- **✅ Verified** (source search): on the 124-run replay at 4c2c7b593e, before
  the selection cap existed, `scripts/select_affected_tests.py` (transitive
  import closure plus file references and meta/docs rules) misses no
  diff-caused red when run with `--service-substrate`. Of 36 misses, 13 are
  accounting checks (the replay had no meta-test manifest), 8 flakes, 8
  ambient-coupled, 4 stale tests fixed without product code, 1 a timing flake
  and 2 unresolved, one pre-existing and one diff-caused. The diff-caused one
  (run 44843227733, `test_doctor_cmd` after the vectors-064/065 data move) is
  missed without the flag and selected with it. The 4 stale-test misses were
  not shown to be outside the diff. *Source: 258-research-17.*
- **✅ Verified** (source search): two later commits changed the selector's
  default. 588a95422f stops propagating file references through imports; its
  replay keeps 27 of 40 diff-caused reds selected, under labels that differ from
  258-research-17's (inferred, not read: the 13 not selected are the 13
  accounting misses). 08a1329f21 (subject "Sam 2026-11-23") caps a selection
  over 30% of the suite's test files (`--cap-pct`) at the direct tier and leaves
  "transitive importers, the run-everything triggers, the lints that glob a
  directory, and on a service/ main change the engine-booting set" to develop
  CI; its replay keeps 16 of 40 diff-caused reds selected. `--full-closure`
  turns the cap off, and `--service-substrate` is the default
  (`default=True`). *Source: 258-research-18.*
- **✅ Verified** (source search): the push-time check gate.
  `scripts/push_check_gate.py` (nexus-igea9, Sam 2026-11-22) runs nine checks a
  push's diff reaches against the pushed tree and refuses the push on a red:
  file-size, changelog, context-budget, wire-ledger, cloud-leg, pg-cluster,
  plugin-ledger, and two that need a proof of a run, docs-readers and
  write-seam. `nx ci check-hold` refuses a push while develop is red, unless the
  pusher owns the red or pushed a suspect. *Source: 258-research-18.*

### Critical Assumptions

- [ ] CA-1: An accounting red does not hide a product defect, so demoting a
  check to a report loses no product catch. — **Status**: Refuted as a
  statement about the class; holds for each of the five checks of Phases 1 and
  2 (258-research-15). Three checks the census labelled accounting caught
  product defects, so demotion is decided check by check from Phase 0's report,
  never by the class label. — **Method**: Source Search
- [ ] CA-2: The release PR into main and the engine-release workflow both run
  the wire-pairing check, so moving its blocking role there still stops an
  undeclared both-halves change before any user receives it. — **Status**:
  Half false (258-research-16): the release PR runs it; the engine release does
  not, and engines are tagged from develop. Phase 2 therefore adds the check to
  the engine release first, and pairing keeps blocking on develop until then.
  — **Method**: Source Search
- [ ] CA-3: A check that reports instead of failing is still acted on.
  Fowler's warning about forgotten quarantines says it may not be.
  — **Status**: Unverified. Measured from the review records: the 2-week review
  starts with Phase 1's first demotion, and CA-3 holds when, in each of the
  first three reviews, every class-A check whose value or failing-item count
  rose since the previous review, and every test quarantined longer than 14
  days, has a disposition written in that review's record (fixed, accepted,
  alarm line changed by Sam, or put to retirement). — **Method**: Spike (three
  review cycles).
- [ ] CA-4: `scripts/select_affected_tests.py`, run with `--full-closure` and
  with the engine-substrate selection on (the default; a CI run never passes
  `--no-service-substrate`), with `meta_tests.py run` beside it, misses few
  failures that the full run would catch. — **Status**: Holds on the 124-run
  replay at 4c2c7b593e: no diff-caused miss with the engine-substrate selection
  on (258-research-17). The capped default of 08a1329f21 keeps only 16 of 40
  diff-caused reds selected, so CA-4 does not cover it (258-research-18). Phase
  7 step 1 re-runs the replay at the configuration it adopts; the shadow window
  measures it live. — **Method**: Source Search (replay), then Spike (shadow
  window).

## Proposed Solution

### Approach

Treat the test apparatus the way Google's and Beck's definitions treat a single
test: it earns its place by catching product failures, and its cost is counted.
Eight elements, each grounded below.

**(a) Two classes of check.** Every check that is not a product test is either
*user-reachable* (class U: it guards a failure a user can receive, and keeps
blocking at least at the release boundaries, the release PR into main and, for
wire pairing from Phase 2, the engine release) or *accounting* (class A: it
reports as a fitness function in warning mode and is reviewed every 2 weeks; a
class-A check with a numeric value also fails past an explicit alarm line,
which Sam sets per check from Phase 0's report). Every class-A check is
demoted, either by a phase step or by a review batch (Phase 1 step 7). Grounds: the warn mode of
fitness functions (258-research-9); CA-1; the red census (258-research-1). One
test decides a check's class: what it guards. A check that guards a failure a
user can receive is class U; every other check is class A. Phase 0's report can
move a check from A to U when it shows the check caught a product defect, as
three checks the census's name-based label calls accounting did
(258-research-15); it never moves a check from U to A for lack of a catch.
The class of each named check, from strand C's inventory (258-research-5):

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
| Plugin ledger, changelog fragments, ci-evidence, push script | A, process | release bookkeeping; whether the release PR into main runs each ledger check blocking is not verified here, and Phase 4 step 1 checks it |
| `cli_runner_output_invariant`, `tmp_root`, the other lint modules | to classify in Phase 0 | not classified by strand C; a class-A one is demoted in a review batch (Phase 1 step 7) |
| The push-time check gate's nine checks | the class of the CI check each one runs | Phase 0 classes pg-cluster, docs-readers and write-seam (258-research-18) |

The class of the remaining lint modules is written into each module by Phase 0,
not typed into this RDR.

**(b) Retirement is part of the normal process.** Each check carries three
header lines: `owner`, `guards` (the incident or bead it answers) and `class`.
Its red count and catch count are derived, never hand-kept: a report script
reads CI history the way strand B did (failing item per red run, plus the fix
signal) and prints, per check, reds, catches and red hours in a window. A check
with no catch in 60 days is deleted at the next 2-week review by default; the
review keeps one only with a written reason in its record (retirement by default:
Sam, 2026-11-27, T2 `nexus/sam-ruling-rdr258-retire-by-default-and-half-pins-2026-11-27`; N and the cadence: Sam, 2026-11-27). For a class-U check, a catch is a
product catch. For a class-A check it is a guard catch (Sam, 2026-11-27, Q1;
T2 `nexus/sam-ruling-rdr258-gate-answers-2026-11-27`): a red fixed by changing
what the check's `guards:` header names. A check whose reds were all fixed by
ledger or ceiling edits retires after 60 days. A demoted check reports instead
of failing, so a reported failing item fixed by changing what `guards:` names
also counts as a catch. Each check's 60-day clock starts on the day its
demotion commit lands on develop, read from git; the first clocks start with
Phase 1's demotions, and every other class-A check's clock starts with its
phase step or review batch. The 2-week
review applies the rule from Phase 1 step 5 on. Grounds: the 67-to-4 ceiling record and the absence of any merit
retirement since 2026-10-04 (258-research-4); the 3.1x regrowth after a burst
cleanup (258-research-7). No verified source gives a retirement rule; this
element rests on the measured figures.

**(c) Budgets that alert on a trend instead of exact ceilings.** Each ceiling
(storage-touch, DDL/WAL, the 288 file-size exceptions, context budgets, sleep)
becomes a reported value with its trend over recent runs and one alarm line,
which Sam sets from Phase 0's report. Moving the value no longer needs a
commit; crossing the alarm line fails, and the failing message names the
commits since the last report. Grounds: a ceiling that was raised 67 times and
lowered 4 is a counter that every change must increment (258-research-4);
Google's "brittle" and Beck's structure-insensitive property (258-research-8);
warn mode (258-research-9). Ledgers are not budgets: they list changes, not a
count. A class-A ledger check reports a missing entry on develop and blocks
where the ledger is consumed, the release PR into main (Phase 4). The wire
ledger feeds a class-U check and follows Phase 2.

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
job; each quarantine has a 14-day deadline, after which the test is fixed or
deleted (Sam, 2026-11-27), and the report lists every quarantined test with its
age. Grounds: Fowler and Gradle (258-research-10); flakes are 15.1% of red runs
(258-research-1). The existing nightly workflow is the model (258-research-13).

**(f) Test selection on develop pushes.** A develop push runs the tests that
`scripts/select_affected_tests.py --full-closure` selects (the transitive import
closure of the changed files, file references, and the meta and docs rules,
with no size cap), with the engine-substrate selection on, which is the default
(a CI run never passes `--no-service-substrate`), and with `meta_tests.py run`
beside it for the lint and census tests. The full suite and every class-A check
run nightly and on the release PR into main. The capped default of 08a1329f21
(Sam 2026-11-23) is the hand pre-push mode: it leaves transitive importers, the
run-everything triggers, the directory-glob lints and the engine-booting set to
develop CI, and on a selected develop push nothing after it would run them
before the nightly run. Its replay keeps 16 of 40 diff-caused reds selected
(258-research-18), so it is not the develop-CI configuration; Sam decides the
configuration at Phase 7. Grounds: post-submit runs for the broad set
(258-research-11); the existing selector (258-research-13, 258-research-17).
The Machalica result is for a predictive selector, a different method, so its
cost and catch figures do not transfer; it is cited for the shape (select at
push, keep a full backstop) and for its warning that flaky outcomes corrupt
selection, which is why (e) comes first.

**(g) Optional: mutation testing to measure test value.** Mutation testing
changes product code on purpose and asks which tests notice. A diff-scoped run
on review, as Google does, would show which tests catch nothing
(258-research-12). Optional because the verified source gives no usefulness
figure.

**(h) Stop adding a gate per incident.** The review gate's habit of answering a
finding with a new check becomes: fix the behaviour and add a behaviour test of
the product. A new accounting check needs an owner, an incident and, if it has
a numeric value, an alarm line, and it lands in report mode. Phase 1 step 6
writes this into nexus's rules; Phase 8 carries it into the plugin guidance.
Grounds: about half of the lint modules were
born from incidents (258-research-4); Google's "a test for each behavior"
(258-research-8).

### Technical Design

The new code is small and reuses existing patterns.

- **Check header.** Three comment lines at the top of each check module:
  `owner:`, `guards:`, `class: U | A`. The `guards:` line holds the incident
  or bead id and the paths of the guarded thing; the report counts a guard
  catch when a fix touched those paths and not the check's own ceiling,
  ledger or allowlist files. A check without them is listed by the
  report as a finding; no test fails on it (a new gate to police the gates
  would repeat Gap 2).
- **Report script** (`scripts/check_value_report.py`, illustrative name):
  input is a date window; it reads run and job data through `gh api`
  (read-only, one call per page, no polling), or the Phase 0 snapshot for the
  baseline window, and the git history; output is a table per check (reds,
  catches, red hours, class, owner, quarantine age) and the four Day-2 metrics.
  It keeps no state of its own; it recomputes from the record each time. Its
  first run is over the Phase 0 snapshot and must reproduce strand B's totals
  within the Minimum Viable Validation tolerance.
- **Report mode for class A.** Reuse the `--advisory` and `::warning::`
  pattern already in `ci.yml` and `ddl_ratchet.py`. A check reports in one of
  two ways, by kind:
  - a numeric check (a ceiling, a budget, the storage-touch fan-in) prints its
    value and trend to the job summary and fails only past its alarm line;
  - a pass/fail check (a pytest lint test such as the census,
    `docs_reference_rot` or `module_seam`) has no value and no alarm line. It
    lists each failing item in the job summary and in the report, and never
    fails the gate on develop; a class-A ledger check still blocks the release
    PR into main (Phase 4). An item still failing at the next 2-week review is fixed,
    or its check is put to the retirement question at that review.

  The pytest-gate reads whole jobs (its `needs:` list in `ci.yml`), not single
  tests, so a class-A pytest test moves out of the blocking test-lint job into
  an advisory job; Phase 1 step 2 chooses the job layout.
- **Push-time gate.** Each check of `scripts/push_check_gate.py` takes the class
  of the CI check it runs. In the step that demotes a CI check, the matching
  push-gate check stops refusing and prints its value or failing items; a
  numeric one still refuses past its alarm line. Class-U checks keep refusing.
  The wire-ledger check changes in Phase 2, file-size and context-budget in
  Phase 3, changelog, plugin-ledger and cloud-leg in Phase 4; pg-cluster,
  docs-readers and write-seam keep refusing unless Phase 0 classes one A.
  `nx ci check-hold` needs no change: a demoted check no longer turns develop
  red, so it no longer holds pushes (inferred, not read). The push gate is
  Sam's (nexus-igea9, Sam 2026-11-22), so each change to it is marked [Sam].
- **Review record.** Each 2-week review writes one T2 record (for example
  `nexus/check-value-review-<date>`) listing each class-A check whose value or
  failing-item count rose, each test quarantined longer than 14 days, each check
  at 60 days with no catch, and the disposition of each. These records are the
  CA-3 measurement.

```text
// Illustrative: one numeric class-A check result
check: storage_touch   class: A   value: <n>   trend(10 runs): <+k>
alarm_line: <set by Sam from Phase 0's report>   verdict: report   (fails only when value > alarm_line)
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Report mode for class A | `ci.yml` mcpb/constraints warn-on-develop; `ddl_ratchet.py --advisory`; storage-touch report mode | Reuse for numeric checks; pass/fail pytest checks need an advisory job beside test-lint |
| Push-time refusal | `scripts/push_check_gate.py`, `nx ci check-hold` | Extend: each push-gate check follows the class of its CI check; check-hold unchanged |
| Nightly quarantine and full run | `local-service-gate-nightly.yml` | Extend: one more nightly job, same caching and concurrency rules |
| Develop-push selection | `scripts/select_affected_tests.py` (hand pre-push run since 2026-11-23), `select_affected_integration_tests.py`, `select_docs_reader_tests.py` | Reuse: the transitive selector with `--full-closure` and the engine-substrate selection on |
| Red and catch counts | strand B's one-off scripts; its per-run labels (`items.json`, `runlabels.json`) are no longer in any session scratchpad | Replace with one committed script and a committed snapshot of the baseline window (Phase 0 step 1) |
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
  258-research-3); about two lint modules a day keep arriving
  (258-research-4).

**Reason for rejection**: It is the state Sam asked to leave.

### Alternative 2: Another one-off cleanup like 2026-10-03

**Description**: Delete a batch of checks in one ordered burst.

**Pros**:

- Fast, and proven to land (five batches in one day).

**Cons**:

- The lint bucket regrew to 3.1 times its post-cleanup size in seven weeks
  (258-research-7), because the habits that add gates did not change.

**Reason for rejection**: It treats the stock and not the flow. This RDR
removes checks only through (b), and (h) lands in Phase 1, with the first
demotions.

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
  checks of Phases 1 and 2 guard, in order: CI tenant cost; dangling citations
  in docs; tests passing on leaked mode state; a fake `time.sleep` that hung a
  run for about 50 minutes; and an undeclared engine-plus-client wire change
  (258-research-14).
  **Mitigation**: each keeps running and reporting; only its failing role
  changes. Wire pairing stays class U and keeps failing on develop until the
  engine release runs it (Phase 2; CA-2 is half false).
- **Risk**: wire pairing reporting on develop lets a both-halves change sit on
  develop undeclared; nexus-sh9v2 shows what such a change can do once
  deployed. **Mitigation**: pairing keeps blocking on develop until Phase 2
  step 1 adds the pairing check to `engine-service-release.yml`. Only then does
  it report on develop, and it blocks on the release PR into main and in the
  engine release.
- **Risk**: report-only checks are ignored (CA-3; Fowler's "forgotten").
  **Mitigation**: the 2-week review starts with Phase 1's first demotion,
  before any later phase; Sam sets each alarm line before its check is demoted;
  quarantined tests have a 14-day deadline. The review records measure CA-3.
- **Risk**: selection misses a failure (CA-4). **Mitigation**: a shadow window
  runs selection and the full suite side by side before selection gates
  anything; the release PR always runs everything.

### Failure Modes

- A class-A value drifts up without a red: visible in the job summary and the
  report; a numeric check fails once it passes its alarm line.
- A quarantined test is never fixed: the report lists its age; past 14 days it
  is fixed or deleted at review.
- A selection miss: the nightly full run turns red on a commit whose push was
  green; the report counts these as selection misses, which is the CA-4
  measurement.

## Implementation Plan

Items marked **[Sam]** change a pre-declared test criterion, a gate's failing
role, or a file under Sam's rules (AGENTS.md, CLAUDE.md, the plugin's review
guidance, the push gate, the engine-release and release paths). Each needs his
decision before it lands; acceptance of this RDR is not that decision unless he
says so. At accept, the plan is one epic with one bead per phase (Sam,
2026-11-27). Each phase heading names what Sam decides and what an implementer
does without him.

### Prerequisites

- [x] CA-2 checked by source search (258-research-16): half false; Phase 2 adds the engine-release pairing check before pairing leaves develop.
- [x] Sam's answers to the Finalization Gate questions (T2 `nexus/sam-ruling-rdr258-gate-answers-2026-11-27`): N = 60 days; review every 2 weeks; quarantine deadline 14 days; targets as in the Day-2 metrics table; per-check alarm lines set from Phase 0's report, each one to Sam.
- [x] Q1 answered (same record): a guard catch counts for a class-A check.

Phase 0 step 1 waits on no answer from Sam.

### Questions answered by Sam

**Q1: Which catch counts for a class-A check under the 60-day retirement
rule?** Closed 2026-11-27: Sam chose "Guard catch" (T2
`nexus/sam-ruling-rdr258-gate-answers-2026-11-27`). A catch is a red fixed by
changing what the check's `guards:` header names; a check whose reds were all
fixed by ledger or ceiling edits retires after 60 days. Each retirement is
still Sam's decision per check. The options he did not choose were a literal
product catch, which would have retired every class-A check, and red hours
against a bound he would set.

### Minimum Viable Validation

The report script, run over the Phase 0 snapshot of 2026-09-23 to 2026-11-23,
reproduces strand B's totals within a stated tolerance:

- 787 red runs, and the split by workflow (CI 602, Windows rehearsal 85,
  Service CI 62, plugin drift ledger 24, other 14), each within 2%;
- the top seven per-check red-run counts (258-research-2), each within 10% or 3
  runs, whichever is larger;
- the six category counts. Strand B's figures are estimates corrected by a
  60-run hand sample (258-research-1), so the script's automatic counts are
  compared by category, and a category off by more than 10% or 3 runs is
  explained by category (which rule moved which runs) in the Phase 0 record.

A run-by-run reproduction is not possible: strand B's per-run labels are no
longer in any session scratchpad. The script's automatic counts on the
snapshot become the Day-2 baselines, so every later window is measured the
same way. If the automatic accounting share differs from 63.6% by more than
the tolerance, the accounting target goes back to Sam, because his target
("halve it", under 30%) was set against 63.6%.

### Phase 0: Snapshot, report and labels. Sam decides each alarm line from its report; the implementer does every step without him.

**Sam decides**: each class-A alarm line, one check at a time, from step 5's
report; the accounting-share target again only if the MVV moves the baseline.
**Without Sam**: steps 1 to 5. No CI change and no failing role changes.

#### Step 1: Snapshot the baseline window, by 2026-12-15

GitHub keeps workflow logs for 90 days, so the window's oldest logs (develop
runs of 2026-09-23) expire on 2026-12-22 (258-research-18). By 2026-12-15, a
week ahead of that, download the failed-job logs and the run and job data (run
id, workflow, sha, conclusion, start and end times, job names and results) for
every red develop run from 2026-09-23 to 2026-11-23. Keep them as one
compressed archive on a lab host disk, outside any session scratchpad, with
its path and sha256 in a T2 record. This step needs neither the report script
nor any answer from Sam.

#### Step 2: Report script and the MVV

Commit the report script, derived from strand B's method. It extracts the
failing items from the step 1 archive (one row per failing item: run, sha,
workflow, job, venue, failing test or step, fix signal), and that extraction is
committed beside it as a compressed file. Run the MVV on it.

#### Step 3: Hand-check the catch attribution

For each red the report attributes to a fix in `src/` or `service/src/main`
(18 in 258-research-15), read the fix commit and record whether the red caught a
product defect or was a gate red on an in-flight feature commit. A check with a
real product catch moves to class U, whatever its label.

#### Step 4: Check headers and classes

Add `owner`, `guards`, `class` to every check module; class the modules strand
C did not class; confirm the scrub gate's class; class the push gate's
pg-cluster, docs-readers and write-seam checks.

#### Step 5: First report

Run the report over the baseline window and write: the Day-2 baselines (the
automatic counts); for each numeric class-A check, its value, trend and a
proposed alarm line, sent to Sam one check at a time; and the target for the
ledger-and-ceiling commit share, which Sam left for Phase 0 to set.

### Phase 1: Report mode for four class-A checks, the 2-week review, rule (h) and the review batches. Sam decides each demotion and alarm line, each batch and the AGENTS.md rule; the implementer builds the reporting, runs the reviews and proposes the batches without him.

**Sam decides**: each step's demotion, with the alarm line of a numeric check;
each step 7 batch; the AGENTS.md rule in step 6. Retirement needs no
per-check decision: a check is deleted by default at 60 days, and the review
record names any it keeps, with the reason, for Sam to read.
**Without Sam**: the report-mode code, the advisory job layout, the review
records, the cause test in step 3, the step 6 draft, and each batch proposal.

Ordered by measured red runs (258-research-2). `cli_runner_output_invariant`
(27) and `tmp_root` (20) are larger than step 4 but were not examined
(258-research-14); Phase 0 classes them, and a class-A one is demoted in a
review batch (step 7).

#### Step 1: Storage-touch ratchet (68 red runs) [Sam]

Run the fan-in in its existing report mode on develop; fail only past an alarm
line; stop editing `tests/storage_touch_ceiling.toml` per change.

#### Step 2: `docs_reference_rot` (56) [Sam]

Choose the advisory job layout for pass/fail pytest checks (Technical Design).
Report on develop and in the nightly job; fix dangling citations in batches at
review. The test also runs in the `test-docs-readers` job, which pytest-gate
needs, and in the push gate's docs-readers proof, both of which take their
files from `scripts/select_docs_reader_tests.py`. Exclude it from that
selector's output in the same step, so neither path keeps it blocking.

#### Step 3: Mode-declarations census (47) [Sam]

The census keeps running and emits a warning instead of a failure. In the same
step, test whether the leak it guards against (cloud-mode state carried
between tests) can be removed at its cause, the isolation item on Fowler's list
(258-research-10); if it can, the census is put to retirement.

#### Step 4: `module_seam` (26) [Sam]

Report only. A fake that hangs a run still shows up as a red (a timed-out run),
which is an inference, not a measured fact; Phase 0's report checks it.

#### Step 5: The 2-week review starts

The first review runs two weeks after this phase's first demotion lands, and
every two weeks after. Each writes a review record (Technical Design); Sam
reads it. The first three records are the CA-3 measurement. From this step on,
the review applies the retirement rule (b): a check with no guard catch in the
60 days since its demotion is deleted by default, and the review record gives a
written reason for any it keeps (T2 `nexus/sam-ruling-rdr258-retire-by-default-and-half-pins-2026-11-27`).

#### Step 6: No new gate per incident [Sam]

Write rule (h) into the AGENTS.md hot rule on review gates: a finding is
answered by fixing the behaviour and adding a behaviour test; a new accounting
check lands in report mode with an owner, an incident and, if numeric, an
alarm line.

#### Step 7: The review demotes the class-A remainder in batches [Sam per batch]

The remainder is every check Phase 0 classes A that no other step demotes:
not steps 1 to 4 here, not the ceilings of Phase 3, not the ledgers of Phase 4.
From the first review on, the review proposes the next batch from Phase 0's
report, largest baseline red-run count first, and sends it to Sam as one
decision (Sam, 2026-11-27, T2 `nexus/sam-ruling-rdr258-accept-2026-11-27`).
Each batch is a plannable unit of its own: the review record names its checks,
the alarm line of each numeric one, the advisory-job and push-gate changes,
and its demotion lands as one change, which starts each check's 60-day clock.
Batches continue until the report shows that the baseline red runs a
still-blocking class-A check would still turn red put the accounting share
under the 30% target; after that, the rest of the remainder is demoted at the
review's pace. With the 286 other red runs of the baseline window held
constant, that means about 122 accounting red runs per sixty days or fewer,
against 501 (258-research-1; inferred, not read: the other categories do not
move).

### Phase 2: Wire pairing blocks at the release boundaries. Sam decides the engine-release change and the develop demotion; the implementer prepares both without him.

**Sam decides**: step 1 (his engine-release path) and step 2.
**Without Sam**: the workflow step, its kill-control test, and the push-gate
change, prepared for his review.

Wire pairing is class U. Until step 2 lands, it keeps failing on develop and
the push gate's wire-ledger check keeps refusing. The ledger's format does not
change in this phase.

#### Step 1: The engine release runs the pairing check [Sam]

Add a step to `engine-service-release.yml` that runs
`scripts/check_wire_contract_pairing.py --rev-range <previous engine-service-v* tag>..<the commit being tagged>`.
The script's default range is `<newest published v* tag>..HEAD`, which follows
the client's tags, so the engine release passes its own range (inferred, not
read: `v*` does not match `engine-service-v*`). The engine-release skill's tag
step names the new check. A kill-control commit proves the step fails on an
undeclared both-halves change (Test Plan).

#### Step 2: Pairing reports on develop [Sam]

Once step 1 is in place, pairing reports on develop and blocks on the release
PR into main and in the engine release; the push gate's wire-ledger check
follows.

### Phase 3: Ceilings become budgets. Sam decides each alarm line and the file-size hot rule; the implementer builds value, trend and alarm reporting without him.

**Sam decides**: each ceiling's alarm line; the AGENTS.md file-size hot rule
change; the push-gate change.
**Without Sam**: the reporting code and the push-gate change, prepared for his
review.

DDL/WAL, file-size exceptions, context budgets and the sleep ceiling move to
value, trend and alarm line, as in Phase 1 step 1 [Sam]. The push gate's
file-size and context-budget checks follow. The file-size hot rule in
AGENTS.md changes with it.

### Phase 4 (CLOSED, 2026-11-28): Ledgers report on develop and block where they are consumed. Sam decides each demotion and any release-path change; the implementer does step 1 and prepares step 2 without him.

**Sam decides**: any change to the release PR's gate in step 1, and step 2.
**Without Sam**: step 1's source search, and the report-mode code.

This phase covers the class-A ledgers: the plugin ledger
(`conexus/PENDING_RELEASE.md`, 402 commits), changelog fragments, and the
cloud-leg route ledger. The wire ledger is Phase 2's.

#### Step 1: Confirm the consumer gates

Read whether the release PR into main runs each of the three ledger checks as
blocking. `scripts/changelog_fold.py check` runs in `ci.yml` and the plugin
ledger test runs in `plugin-drift-ledger.yml` (258-research-18); whether each
blocks the release PR was not read. Where one does not, add it to the release
PR's gate before step 2 [Sam].

#### Step 2: Report on develop [Sam]

Each of the three reports a missing entry on develop (a push, or a pull request
into develop) and blocks on the release PR into main; the push gate's changelog,
plugin-ledger and cloud-leg checks follow (their `Spec.default_mode` is report,
and `--ledger-report` stays as the on-record request). Fewer ledger-only fix-up
commits are expected, since an entry can be written when the release needs it
(inferred, not read); the ledger-and-ceiling commit share measures it.

Sam's ruling of 2026-11-28 (T2 `nexus/sam-ruling-rdr258-phase4-step2-2026-11-28`)
adds a fourth check, report only: a develop push and CI list each commit that
changes shipped code and has no changelog fragment naming its bead
(`scripts/changelog_coverage.py`; the push gate's `changelog-coverage` check,
`--skip-changelog-coverage '<reason>'` to override it on the record, and a step
of CI's `changes` job). The release PR into main runs the same script in report
mode, in `release-ledger-gate`, while the fragment format check
(`changelog_fold.py check`) blocks there. A run that examined nothing is a
refusal, with two passes that carry a `PUSH_CHECK_NOTE ... examined nothing`
line: a push range of merge commits only (a release back-merge), which the gate
confirms with git, and a range of release-preparation and revert commits only.
The suite pins the rule: only `changelog-coverage` may pass that way, an exit 10
with no vacuous line still refuses on a merge-only range, and a range with a
real commit is examined.

### Phase 5: Flake quarantine. Sam decides the merge-gate change; the implementer builds the marker and the nightly job without him.

**Sam decides**: removing quarantined tests from the merge gate.
**Without Sam**: the marker, the nightly job, and the report's quarantine list.

Add the quarantine marker and the nightly quarantine job; move each test that
the report shows flaky; each quarantine has a 14-day deadline, then the test is
fixed or deleted.

### Phase 6: Behaviour tests. Sam decides each deletion; the implementer rewrites and fixes tests without him.

**Sam decides**: deleting a pin that has no behaviour behind it.
**Without Sam**: rewriting pins as behaviour tests, and fixing stale and
ambient-coupled tests.

Delete at least half of the about 386 repository pins (193 or more) and rewrite
the rest as behaviour tests (T2 `nexus/sam-ruling-rdr258-retire-by-default-and-half-pins-2026-11-27`). The implementer proposes the deletion list,
each entry with the behaviour it pins or why none exists, and Sam approves it in
one batch. Fix the stale and
ambient-coupled tests and the Windows portability failures to own their state.
A diff that touches the Windows set's push filter is tested on native Windows
before its push (AGENTS.md worktree rule 14).

### Phase 7: Selection on develop pushes. Sam decides the configuration and the switch; the implementer runs the replay and the shadow window without him.

**Sam decides**: the selector configuration, and step 3.
**Without Sam**: steps 1 and 2.

Starts after Phase 5, because flaky outcomes corrupt selection.

#### Step 1: Replay at the adopted configuration

Re-run the 124-run replay from the Phase 0 snapshot with
`select_affected_tests.py --full-closure`, the engine-substrate selection on,
and `meta_tests.py run` beside it, at the commit this phase starts from. For
each of the 4 stale-test misses in 258-research-17, say whether the diff
caused it.

#### Step 2: Shadow window

Selection and the full suite run side by side on develop pushes; misses are
counted (CA-4).

#### Step 3: Selection gates develop pushes [Sam]

Selection gates develop pushes, with the full suite nightly and on the release
PR into main.

### Phase 8: The review-gate habit in the plugin guidance. Sam decides the guidance change and the plugin release; the implementer drafts the guidance without him.

**Sam decides**: the guidance change; the plugin release that ships it.
**Without Sam**: the guidance draft.

Retirement is not in this phase: the 2-week review applies it from Phase 1
step 5, and Phase 1 step 6 writes rule (h) into AGENTS.md. This phase carries
rule (h) into the conexus plugin's orchestration review gate: "new check per
finding" becomes "fix the behaviour; add a behaviour test". The plugin guidance ships to every plugin user, so
the AGENTS.md rule "The plugin's process is product; nexus's specifics are
leaks" applies: the new terms (class A, alarm line, the `owner`, `guards` and
`class` headers) are written generically, nexus's names and ids stay in
nexus's own config, and any new hook registration or module-level text
constant is declared in `conexus/hooks/audience.yaml`. The change carries a
changelog fragment and ships in a plugin release, which Sam cuts.

### Phase 9 (optional): Mutation testing pilot. Sam decides whether it runs at all; nothing happens without him.

A diff-scoped mutation run on review for one module, to see whether it finds
tests that catch nothing.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Report script | N/A (one script) | its output | N/A | MVV reproduction | in git |
| Baseline snapshot | the extraction file in git; the raw archive's path in T2 | its T2 record | kept | MVV | git, and the lab host archive |
| Check headers | report lists all | report row | removed with the check | report flags missing headers | in git |
| Quarantine marker | report lists quarantined tests with age | report row | marker removed when fixed | nightly job runs them | in git |
| Review records | one T2 record per review | the record | kept | CA-3 | T2 |

No new store is created: the snapshot is a committed file and an archive, and
the reviews are T2 records.

### New Dependencies

None.

## Test Plan

- **Scenario**: a numeric class-A check below its alarm line regresses —
  **Verify**: the job summary shows the new value and trend, and the merge gate
  stays green.
- **Scenario**: a numeric class-A check passes its alarm line — **Verify**: the
  merge gate fails and names the check and the commits since the last report.
- **Scenario**: a pass/fail class-A check fails on develop — **Verify**: its
  failing items are in the job summary and the report, and the merge gate
  stays green.
- **Scenario**: a push whose tip fails a demoted class-A check below its alarm
  line — **Verify**: the push gate prints the value or the failing items and
  does not refuse.
- **Scenario**: an undeclared both-halves commit on a release PR into main, and
  in an engine release (from Phase 2) — **Verify**: wire pairing fails each (a
  kill-control commit in a test repo, as the existing lint's non-vacuity tests
  do).
- **Scenario**: a quarantined test — **Verify**: it is absent from the merge
  gate's selection and present in the nightly job's.
- **Scenario**: a push that changes one `src/nexus` module — **Verify**: the
  selector picks the tests that import it, and the shadow run records any test
  the full run failed that selection did not pick.
- **Scenario**: the report over the baseline snapshot — **Verify**: it
  reproduces strand B's totals within the MVV tolerance.

## Validation

### Testing Strategy

1. **Scenario**: each phase's change
   **Expected**: the metric the phase names, measured by the report over the
   sixty days after the phase, moves toward its target; a phase that does not
   move its metric is reverted. Phases 1 to 4 are measured by the accounting
   red share and the red hours, Phase 1 also by the lint module count, and
   Phase 4 also by the ledger-and-ceiling commit share; Phase 5 by the flake red
   share; Phase 6 by the stale and ambient-coupled red share; Phase 7 by the
   shadow window's miss count and the runner-hours per develop push; Phase 8 by
   the lint module count. A metric Sam set no target for has the target "better
   than its Phase 0 baseline"; for Phase 7's miss count the target is no
   diff-caused miss that the full run caught, as on the replay
   (258-research-17). Phase 0 measures and changes nothing; Phase 9 is optional
   and has no metric.

### Performance Expectations

Fewer red runs and fewer runner-hours on red follow from the red census if the
demoted checks stop failing (258-research-2). Selection's saving is measured in
the shadow window, not predicted here.

## Finalization Gate

### Day-2 metrics

Each is measured by the report script over a sixty-day window. The baselines
below are strand B's figures; Phase 0 step 5 replaces each with the report's
automatic count on the baseline snapshot. Targets are Sam's (2026-11-27)
except where the table says otherwise.

| Metric | Baseline | Target |
| --- | --- | --- |
| Red share by category (product, accounting, flake/infra, stale/ambient, harness, build) | 2.4% / 63.6% / 15.1% / 10.9% / 7.4% / 0.5%, sample-corrected estimates (258-research-1) | accounting under 30%, reached through Phase 1 steps 1 to 4, Phases 3 and 4, and the review batches of Phase 1 step 7; no target for the other categories |
| CI red time | about 280 h, 19% of the window; accounting 188 h (258-research-1, 258-research-2) | under 140 red hours per sixty days |
| Ledger-and-ceiling commit share | 545 of 8,165 non-merge commits, 7% (258-research-3) | set by Phase 0 step 5 |
| Lint module count | 200 modules, 3,380 tests (258-research-4) | no net growth |

Sam's other answers: N = 60 days; review every 2 weeks; quarantine deadline 14
days; each class-A alarm line is set from Phase 0's report, one check at a
time.

### Contradiction Check

Three tensions are stated in the text. (f) cites Machalica et al. for a
different selector than the one proposed, and says so. CA-2 is half false, so
Phase 2 adds the engine-release pairing check before pairing leaves develop. A
retirement rule keyed on product catches would contradict the class-A
definition; Sam's Q1 answer keys it on guard catches.

### Assumption Verification

CA-1 is refuted as a statement about the class and holds for the five checks of
Phases 1 and 2; Phase 0 step 3 re-checks catch attribution per check. CA-2 is
half false (258-research-16), and Phase 2 step 1 is the remedy. CA-3 is
measured by the first three review records after Phase 1's first demotion.
CA-4 is measured by Phase 7's replay and shadow window.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `gh api` run and job listing | GitHub REST | Source Search (strand B used it for the baseline) |

### Scope Verification

The MVV (the report reproducing strand B's totals) is Phase 0 step 2 and is in
scope; the snapshot it runs on is Phase 0 step 1.

### Cross-Cutting Concerns

- **Versioning**: Phase 8 changes the conexus plugin's orchestration guidance,
  which ships to every plugin user; it needs a changelog fragment and a plugin
  release, and its text follows the process-is-product rule. The other phases
  change CI workflows, scripts and tests.
- **Build tool compatibility**: CI workflow changes follow the repo's CI cost
  rules. Phase 6 diffs that touch the Windows set are tested on native Windows
  before their push.
- **Licensing**: N/A
- **Deployment model**: the release PR into main keeps every class-U check
  blocking; the engine release gains the pairing check in Phase 2 step 1, and
  does not run it today (258-research-16).
- **IDE compatibility**: N/A
- **Incremental adoption**: each phase is reversible by restoring a check's
  failing role. Phases 1 to 4 need Phase 0's alarm lines; Phase 2 step 2 waits
  on step 1; Phase 7 waits on Phase 5; Phase 8 waits on Phase 1 step 6.
- **Secret/credential lifecycle**: the report uses the existing `gh` token
  read-only.
- **Memory management**: N/A

### Proportionality

The document carries one design per gap and the decisions Sam must make; the
per-module classification is left to Phase 0 rather than listed here.

Gate round 1 finding S11 (optional and independent work widens the RDR) is
carried to implementation: Phase 9 runs only if Sam says so, and Phase 7 can be
split into its own RDR at accept if Sam prefers.

## References

- T2 `nexus/test-apparatus-rdr-evidence-2026-11-23` (strands A-D).
- T3 `nexus-engineering-practice` / `sources-test-cost-and-value-2026-11-23`.
- T2 `nexus/test-and-machinery-cleanup-outcome-2026-10-03`.
- T2 `nexus/sam-ruling-rdr258-gate-answers-2026-11-27`.
- T2 `nexus_rdr/258-research-1` through `258-research-18`.
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

- 2026-11-23: critical assumptions checked by source search (258-research-15, -16, -17). CA-1 refuted for the class, holds for the five Phase-1 checks, so demotion is per check; CA-2 half false, so wire pairing stays blocking on develop; CA-4 holds for select_affected_tests.py with --service-substrate.
- 2026-11-27: Gate round 1 — PASSED (0 Critical, 11 Significant, 0 ship-blocker(s)); blob `e2ccd807694a`; critique `nexus_rdr/258-gate-critique-2026-11-27-r1`.
- 2026-11-27: round 1 fixes and Sam's gate answers (258-research-18). Folded in N = 60 days, the 2-week review, the 14-day quarantine and the Day-2 targets. Fixed S1 (Q1 opened), S3, S4, S5, S6, S7, S8, S9, S10; S2 resolved in Technical Design; S11 carried to implementation. Phases renumbered: old Phase 1 steps 1-4 are Phase 1, old step 5 is Phase 2, old Phases 2 to 7 are Phases 3, 5, 6, 7, 8 and 9, and Phase 4 (ledgers) is new.
- 2026-11-27: Gate round 2 — PASSED (0 Critical, 11 Significant, 0 ship-blocker(s)); blob `37f653b4c9e1`; critique `nexus_rdr/258-gate-critique-2026-11-27-r2`.
- 2026-11-27: round 2 fixes (258-research-19). Q1 closed: Sam chose the guard catch. N1: one class test, by what a check guards; a catch can move a check from A to U only. N7: the 2-week review applies retirement from Phase 1 step 5, each check's 60-day clock starts at its demotion, rule (h) moves to Phase 1 step 6, and Phase 8 keeps only the plugin guidance. One-line fixes from N2 (class U blocks at least at the release boundaries; pass/fail checks never fail on develop; (h)'s alarm line only for numeric checks), N3 (docs_reference_rot leaves the docs-readers selector) and N6 (a target for each phase metric). N4, N5, N8 to N11 carried to implementation.
- 2026-11-27: Gate round 3 — PASSED (0 Critical, 14 Significant, 0 ship-blocker(s)); blob `0c28c8be1f16`; critique `nexus_rdr/258-gate-critique-2026-11-27-r3`.
- 2026-11-27: Sam's accept condition (T2 `nexus/sam-ruling-rdr258-accept-2026-11-27`; 258-research-20). Phase 1 step 7: the 2-week review demotes the class-A remainder in batches from Phase 0's report, each batch to Sam. The ruling sets no stop condition; the step 7 batches are what give the under-30% accounting target a path. This disposes of round 3's planning blocker "no phase demotes the class-A remainder". Fix-check one-liners: `guards:` carries the guarded paths, so the report can derive a guard catch; each 60-day clock starts at the demotion commit on develop, and every class-A check now has a demoting step or batch.
- 2026-11-27: Gate round 4 — PASSED (0 Critical, 7 Significant, 0 ship-blocker(s)); blob `f4923e254b8b`; critique `nexus_rdr/258-gate-critique-2026-11-27-r4`.
- 2026-11-27: Accepted by Sam (T2 `nexus/sam-ruling-rdr258-accept-2026-11-27`), on round 4's PASS (`nexus_rdr/258-gate-latest`). Round 3's planning blocker is disposed by Phase 1 step 7 (4cd68e5d09); round 4's 7 Significant residuals are discover-at-implementation and are carried to their phase beads under epic nexus-2jomg.
- 2026-11-27: Amended on Sam's word (T2 `nexus/sam-ruling-rdr258-retire-by-default-and-half-pins-2026-11-27`): rule (b) and Phase 1 step 5 make retirement the default (a class-A check with no guard catch in 60 days is deleted unless the review keeps it with a written reason); Phase 6 deletes at least half of the about 386 repository pins, the list approved by Sam in one batch.
- 2026-11-28: Phase 4 step 2 text amended to match the reviewed code and Sam's ruling of 2026-11-28 (T2 `nexus/sam-ruling-rdr258-phase4-step2-2026-11-28`): the develop listing of missing fragments (report only), the merge-only pass and its suite-pinned rule, `--skip-changelog-coverage`, and the release PR's coverage step in report mode. Wording only.
