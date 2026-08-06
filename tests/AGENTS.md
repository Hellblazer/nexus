# tests/AGENTS.md

Test-suite conventions for AI coding agents. `CLAUDE.md` is not a symlink here; the project-root `AGENTS.md` covers global conventions.

## Dev loop (test-suite-compression P0, nexus-test-cleanup 2026-08-05)

Default local dev loop: `uv run pytest -n auto`. `pytest-xdist` is a `dev`-group dependency, opt-in via `-n` — it is deliberately NOT baked into `addopts`, so CI's `pytest-split` duration-balanced sharding (matrix parallelism across runners, see `.github/workflows/ci.yml`) is untouched; `-n auto` is a second, independent, local-only lever. Serial fallback (`uv run pytest`, no `-n`) remains available for debugging output-interleaving or fixture-isolation issues that parallel workers can mask.

`-m lint` selects the O(repo) meta-tests (AST/regex scans of `src/nexus`, `conexus/` agent-skill-command markdown, RDR frontmatter, marker-selection coverage itself) that the default `addopts` (`-m 'not integration and not slow and not lint'`) excludes from the hot loop — they only change when repo *structure* changes, not application behavior, and run once in CI's dedicated `pytest (lint markers)` job rather than once per shard. Run them explicitly with `uv run pytest -m lint` when touching `conexus/`, RDR frontmatter, or a storage-boundary/hook-registration invariant those files pin.

## Scenario journey layer (test-suite-compression P2, 2026-08-05)

`tests/test_scenario_journeys.py` holds `scenario`-marked journey tests that run
**in the default loop** against the real per-process engine substrate (the same
PG+jar `_pin_t2_substrate` already boots for the whole suite — no fakes, no
extra boot cost, ~1s per journey). They exist to prove system truths end to
end: store put → searchable; index → searchable with the RDR-108 catalog
manifest join intact; corpus routing. `tests/test_indexer_e2e.py` (integration
marker) remains the nightly depth version — do not fold it.

Rules when touching this layer:

- **Each journey is ONE self-contained test function with function-scoped
  fixtures.** CI's `pytest-split` shards and the local `-n auto` loop both
  partition by test id; an order-dependent class would be split across
  workers/shards and break.
- **Never assert overall `nx doctor` health** in a journey — doctor is an
  ambient-environment probe (real `shutil.which`, subprocess, TCP). Assert
  only storage-derived facts (collection census).
- **The non-vacuity guard** (`tests/conftest.py`, scenario-guard section):
  when N `scenario` tests are selected in a session, more than
  `NX_SCENARIO_SKIP_BUDGET` (default 0) skips fails the run — enforced on the
  xdist **controller** via `terminalreporter.stats` aggregation (a worker-local
  `session.exitstatus` mutation is discarded by the controller; that was a
  real shipped-then-caught bug). Zero-selected sessions are inert by design,
  so whole-suite removal is pinned separately by
  `tests/test_scenario_wiring_lint.py` (lint bucket, per-PR): addopts may
  never exclude `scenario`, and the journeys file must keep ≥4 marked tests.
  Retiring a journey deliberately = lower that lint's floor in the same
  commit, with rationale.

## Test-authoring directives (compression arc, 2026-08-05)

Distilled from P0–P3 (design records in T2:
`nexus/test-suite-compression-analysis-2026-08-05` and the closure/critique
entries linked from it):

- **Scenario-merge vs parametrize:** checks that share one journey/state
  progression belong in one scenario test (multi-assert, with messages).
  Independent inputs/signals get a parametrize table so each keeps its own
  failure id — never chain independent signals into sequential asserts (an
  early failure hides the rest).
- **Sort every set/frozenset fed to `parametrize`.** Per-process hash
  randomization gives xdist workers different collection orders and the run
  aborts (`sorted(VALID_STORE_NAMES)` precedent).
- **Cross-store contract behavior goes in the case-binding suites**, not new
  per-store copies: `tests/db/test_http_store_selfheal.py` (SelfHealCase),
  `tests/db/test_t2_store_crud_contract.py` (CrudCase/AuthCase),
  `test_t2_store_config_contract.py`. A store whose shape genuinely diverges
  (chash composite key, telemetry append-log) stays out of the template with
  a docstring saying why — don't force-fit.
- **Regression pins keep their bead/incident name and rationale visible**,
  whether standalone or folded into a scenario. Never silently generalize one.
- **Count-reduction claims need assertion-level evidence.** The arc's measured
  lesson: shape-similar tests usually encode distinct semantics; consolidation
  under a strict no-coverage-loss constraint yields ~1–2% collected-count
  reduction, not 40–50%. An honest "left alone" verdict beats a forced merge.
- **O(repo) meta-tests go in the lint bucket** (`pytestmark = pytest.mark.lint`),
  which is excluded from the hot loop but PR-gated by CI's `test-lint` job.
  Expensive shared computation inside one (e.g. an AST scan of `src/nexus`)
  gets a `functools.cache`d single scan.
- **The lint bucket is only safe for FILESYSTEM-scanned censuses** (`rglob`
  over `src/`/`tests/` — `test_storage_boundary_lint.py`,
  `test_no_new_sqlite.py`, `test_private_handle_access_census.py`, etc.),
  never for a `request.session.items`-scanned one. `-m lint`/`-k` deselection
  mutates `session.items` in place, so a census reading it sees only the
  ~800 lint-bucket tests, not the ~11.7k-test default corpus — permanently
  blind to anything outside the bucket, silently, with no error. (Corrected
  2026-08-05, nexus-8x4le: `test_mode_declarations_are_explicit.py` was
  wrongly reclassified under the "whole-repo census tests are xdist-unsafe
  by nature" reading of this rule; it isn't xdist-unsafe — a serial run and
  an `-n 2` run of a full default collection produced identical
  `session.items` — and the reclassification caused it to miss a real,
  live violation for as long as it stayed lint-marked. See that file's
  module docstring for the full account.) If a session.items-based census
  genuinely needs to run once per PR rather than once per shard, that is a
  CI-wiring problem, not a marker-reclassification one.

## Engine substrate: jar freshness

The suite's engine substrate enforces jar-vs-source freshness and version
stamping. After **any** pull/rebase that touches `service/`, rebuild with
`scripts/build-gate-jar.sh` (a plain `./mvnw package` jar is unstamped and the
client probe hard-rejects it). Symptom of forgetting: the entire suite errors
instantly at setup (thousands of `E`s in seconds).

## Mode defaults (RDR-109 Phase 1)

The suite runs in **local mode by default** — no API keys, ONNX MiniLM embedding function via `chromadb.DefaultEmbeddingFunction`. This matches CI (which has no credentials) and reproduces a clean-install developer environment.

Tests that exercise **cloud-mode behavior** (real Voyage calls, `CloudClient` routing, `_has_credentials()`-gated code paths, `voyage-context-3` / `voyage-code-3` embedder assertions) **opt in** by depending on the `cloud_mode` fixture:

```python
def test_something(cloud_mode, ...):
    ...
```

Or at module / class scope:

```python
import pytest

pytestmark = pytest.mark.usefixtures("cloud_mode")
```

The `cloud_mode` fixture lives in `tests/conftest.py`. It sets `CHROMA_API_KEY`, `VOYAGE_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE` to test sentinels and monkeypatches `nexus.config.is_local_mode` to return `False`.

## Lint guard

`tests/test_mode_declarations_are_explicit.py` enforces the convention. For every collected test whose source contains the regex `voyage-(context|code)-3`, it requires one of:

- The test depends on `cloud_mode` (directly, via class `pytestmark`, or via module `pytestmark`).
- The test's file is in `_MODE_LINT_EXCLUDE_FILES` (uniformly mode-independent).
- The test's nodeid is in `_MODE_LINT_EXCLUDE_NODEIDS` (per-test exclusion for mixed files).

Exclusion categories are documented in `tests/conftest.py` above each set.

## When you add a new test that references voyage embedder names

Decide:

1. **Is the voyage token a schema-canonical name** (e.g. `corpus.canonical_embedding_model("code")`, a collection-name parse / render round-trip, RDR-103 four-segment shape)? → mode-independent. Add the test's file to `_MODE_LINT_EXCLUDE_FILES` (if the whole file is schema-only) or its nodeid to `_MODE_LINT_EXCLUDE_NODEIDS`.
2. **Does the test actually exercise cloud-mode behavior** (real Voyage embedder, `_has_credentials()` gated path, `CloudClient` routing)? → add `cloud_mode` to the test's fixture list, or `pytestmark = pytest.mark.usefixtures("cloud_mode")` at module scope.

If the lint fails on a CI run after your edit, the failure message lists the offending nodeids and the two options above.
