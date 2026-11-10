---
name: test-authoring
description: Use when writing, restructuring, or consolidating tests in the nexus repo, or choosing how to run its suite — dev-loop layer selection, scenario-vs-parametrize calls, contract-suite placement, and the guard/lint invariants protecting the fast loop.
effort: low
---

# Test Authoring (nexus)

Reference skill — no agent dispatch. Canonical doc surfaces: `tests/AGENTS.md`
(authoring directives) and root `AGENTS.md` Quick start; durable rationale in
T2 `nexus/directive-test-suite-architecture`. This skill is the routing card.

## Running the suite — pick the right layer

| Need | Command | Notes |
|------|---------|-------|
| Agent hand run | `lab-run -c lab-m -- python3 scripts/watched_pytest.py --stall 180 --status <f> -- uv run pytest <paths, -k or -m>` | Targeted, on chas. No tree gets two green full runs; once pushed, develop CI is the verdict; a hand full suite is announced with its change class, tree sha and "no green full run exists" (AGENTS.md rule 13). Engine: `scripts/mvnw-leased.sh --lab-run`. |
| Full Python suite | `uv run pytest -n auto` | Auto-capped to the box's shared-memory budget and, in a lab-run grant, to the grant's memory (AGENTS.md). The pre-push run a `src/nexus/` or `service/` change requires, once per tree (rule 13). |
| Debugging interleaving/isolation | `uv run pytest` (serial) | Only when parallelism masks the signal |
| Repo-structure invariants | `uv run pytest -m lint` | O(repo) meta-tests; PR-gated by CI `test-lint`; run when touching `conexus/`, RDR frontmatter, storage-boundary/hook invariants |
| Real-substrate depth | `uv run pytest -m integration` | Nightly local-service gate territory |
| System truths, in-process | `scenario` journeys (in default loop) | `tests/test_scenario_journeys.py` |

After ANY pull/rebase touching `service/`: `scripts/build-gate-jar.sh` first;
session start refuses (exit 75) on a missing or stale jar.
One full suite per box at a time (AGENTS.md rule 5).

What a diff needs before a push is the `pre-push-review` skill's subject; the
fixed commands for it come from preflight (`nx run preflight`).

## Authoring decisions (the rules that keep the loop fast and honest)

1. **Scenario-merge vs parametrize.** One journey/state progression → ONE
   scenario test, multi-assert with messages. Independent inputs/signals →
   parametrize table (each keeps its own failure id). NEVER chain independent
   signals into sequential asserts.
2. **Sort every set/frozenset fed to `parametrize`.** Hash randomization gives
   xdist workers different collection orders; the run aborts.
3. **Cross-store T2 contract behavior** goes in the case-binding suites
   (`tests/db/test_http_store_selfheal.py` SelfHealCase,
   `tests/db/test_t2_store_crud_contract.py` CrudCase/AuthCase,
   `test_t2_store_config_contract.py`) — never a new per-store copy. A store
   whose shape genuinely diverges stays out, with a docstring saying why.
4. **New system-truth coverage** (X → Y must hold end-to-end) goes in
   `tests/test_scenario_journeys.py`: one self-contained function-scoped test,
   `scenario`-marked, real substrate, ~1s budget. Never assert overall
   `nx doctor` health (ambient probe) — only storage-derived facts. The
   non-vacuity guard + `test_scenario_wiring_lint.py` protect the layer;
   retiring a journey means lowering that lint's floor in the same commit.
5. **Regression pins** keep their bead/incident name and rationale visible,
   standalone or folded. Never silently generalized.
6. **O(repo) census tests** are `pytest.mark.lint`. Expensive shared scans get
   `functools.cache`. A census that reads `session.items` is the exception
   (tests/AGENTS.md, Test-authoring directives).
7. **Consolidation proposals need assertion-level evidence.** Measured lesson
   (5 independent implementers, 2026-08-05 arc): no-coverage-loss
   consolidation yields ~1–2% collected-count reduction — structural
   similarity is not redundancy. An honest "left alone" beats a forced merge.
   Speed comes from parallelism, lint reclassification, and fixture fixes.

## Shell tests under CI's checkout

Source for this and the sections below: T2
`nexus/qo7ie.36-census-feedback_test_runs_before_push`.

A shell test newly wired into pytest runs on CI's depth-1 checkout, which has no
tags. Anything in it that walks `v*` tags needs an override there
(`harness_lock_test.sh` needed one, b6027cb2e).

- Before such a test lands, run it in a depth-1 clone with no tags.

## Timing pins

Three develop reds in one day were timing pins that read "no wait" or "reclaimed
at once" as a small wall-clock bound; each behaviour was right and a loaded shard
stretched a step by seconds (2026-10-30).

- Prove "no wait" against a visibly large wait (100 polls of 1 s) and bound the
  result at a tenth of it. A bound of one or two seconds on a whole-second clock
  cannot tell "no wait" from "slow".
- Wait on the sampler's own tick counter, not a sleep.
- When one test runs two phases that each have a retry ladder, set the earlier
  phase's ladder far beyond the later phase's wait so the two cannot overlap.
- When a paired local run fails once, capture the failing test's name before
  rerunning.

## Fresh-install leg

A change that alters what a virgin box's doctor sees (a default flip, a retired
path, a ratio with no minimum count) needs `tests/e2e/fresh-install-mvv.sh`.

- Run it for such a change: only that leg starts from nothing, and a green full
  run and a green lint bucket do not reach it. One change left `nx doctor` red on
  a fresh box with one live and one dead record, and only this leg saw it
  (nexus-lnwn1).

## Module marks and the tenant ceiling

- Read the `pytestmark` of an existing file before you add a test to it: the test
  inherits the module's marks. A module `pytestmark = pytest.mark.t2_tenant` makes
  every test in it mint a tenant, and one new test moved the ceiling by one and
  turned develop's pytest-gate red (nexus-egv6q).
- Put a tenant-free pin in a tenant-free module.
- Describe a verb in a tenant-ceiling comment without spelling its name: a comment
  that names a verb trips that verb's own repo-wide name scan.
