---
name: test-authoring
description: Use when writing, restructuring, or consolidating tests in the nexus repo, or when choosing how to run its suite — dev-loop selection (-n auto / -m lint / integration / scenario journeys), scenario-vs-parametrize decisions, contract-suite placement, and the guard/lint invariants that protect the fast loop
effort: low
---

# Test Authoring (nexus)

Reference skill — no agent dispatch. Canonical doc surfaces: `tests/AGENTS.md`
(authoring directives) and root `AGENTS.md` Quick start; durable rationale in
T2 `nexus/directive-test-suite-architecture`. This skill is the routing card.

## Running the suite — pick the right layer

| Need | Command | Notes |
|------|---------|-------|
| Default dev loop | `uv run pytest -n auto` | ~2min for 11.6k tests. THE loop. |
| Debugging interleaving/isolation | `uv run pytest` (serial) | ~14min; only when parallelism masks the signal |
| Repo-structure invariants | `uv run pytest -m lint` | O(repo) meta-tests; PR-gated by CI `test-lint`; run when touching `conexus/`, RDR frontmatter, storage-boundary/hook invariants |
| Real-substrate depth | `uv run pytest -m integration` | Nightly local-service gate territory |
| System truths, in-process | `scenario` journeys (in default loop) | `tests/test_scenario_journeys.py` |

After ANY pull/rebase touching `service/`: `scripts/build-gate-jar.sh` first —
a stale/unstamped jar makes the entire suite error at setup in seconds.
Sharing a box with another instance's suite run: stagger, and cap at `-n 8`.

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
6. **O(repo) census tests** are `pytest.mark.lint` (they're also xdist-unsafe
   by nature). Expensive shared scans get `functools.cache`.
7. **Consolidation proposals need assertion-level evidence.** Measured lesson
   (5 independent implementers, 2026-08-05 arc): no-coverage-loss
   consolidation yields ~1–2% collected-count reduction — structural
   similarity is not redundancy. An honest "left alone" beats a forced merge.
   Speed comes from parallelism, lint reclassification, and fixture fixes.
