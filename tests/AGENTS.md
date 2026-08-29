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
- **`session.items` shrinks under `--splits`/`--group` too, not just `-m lint`**
  (nexus-vdti6, 2026-08-06). CI's real PR-gating `test` job runs
  `pytest tests/ --splits 4 --group N` (pytest-split); its
  `pytest_collection_modifyitems` does `items[:] = group.selected` — the
  identical `session.items`-mutation mechanism `-m`/`-k` deselection uses. A
  session.items-based census left in the default loop (the nexus-8x4le fix
  above) still only ever sees its own shard (~20-25% of the corpus) under
  that real invocation — catching a violation depends on it landing in the
  same shard as the census, not on genuine whole-corpus coverage. Two-part
  fix, both mandatory for any session.items-based census, present or future:
  1. **Structural honesty**: call
     `tests.conftest.partial_session_view_reason(request)` at the top of the
     census and `pytest.skip(reason)` on a non-`None` return — never
     silently scan a proven-partial `session.items`. It fires
     deterministically whenever `--splits`/`--group` is active, plus a
     generic floor (50% of the raw pre-deselection count, tracked by
     `tests/conftest.py`'s `pytest_itemcollected` hook) for any OTHER future
     shrink mechanism. Exempts a developer's own `-k` narrowing — that is a
     normal local convenience run, not shard-blindness under a new name.
  2. **CI coverage**: the `pytest (mode-declarations census)` job
     (`.github/workflows/ci.yml`, job id `test-mode-census`) runs the whole
     `tests/` corpus with `NX_CENSUS_ONLY_JOB=1` and no `--splits`/`--group`,
     so the guard above sees a trustworthy view and the census actually
     executes. That env var makes `tests/conftest.py` SKIP-MARK (not
     deselect) every test outside `_CENSUS_ONLY_ALLOWED_MODULES`, so
     `session.items` stays the full collected corpus while the ~11.7k
     non-census tests never reach fixture setup (skip fires in
     `pytest_runtest_setup` before `item.setup()`) — no engine substrate, PG,
     or service jar needed. Add a new session.items-based census's module
     name to `_CENSUS_ONLY_ALLOWED_MODULES` instead of standing up a second
     dedicated job.

## Engine substrate: jar freshness

The suite's engine substrate enforces jar-vs-source freshness and version
stamping. After **any** pull/rebase that touches `service/`, rebuild with
`scripts/build-gate-jar.sh` (a plain `./mvnw package` jar is unstamped and the
client probe hard-rejects it). Symptom of forgetting: the entire suite errors
instantly at setup (thousands of `E`s in seconds).

## E2E isolation: a sandboxed HOME does NOT isolate a service install

**Only a container isolates the service unit. `HOME` / `NEXUS_CONFIG_DIR` do
not.** The developer box runs Hal's real cloud-mode install beside this
checkout, so a harness that registers the autostart unit collides with
production, not with a sandbox.

- `_activate_cmd` (`src/nexus/daemon/installer.py`) runs
  `launchctl bootstrap gui/$UID <plist>` — the GUI session domain is keyed on
  **uid**, not HOME. Linux: `systemctl --user enable --now`, same shape.
- The label is a hard constant, `_SERVICE_LAUNCHD_LABEL = "com.nexus.service"`
  (`src/nexus/commands/daemon.py`). A swapped HOME only relocates the plist
  FILE; it still loads into the production domain under the production label.

Two things keep the harnesses safe today, neither of them HOME:

1. **The consent gate** — `_decide_autostart` (`src/nexus/commands/init.py`):
   a non-interactive run with no flag DECLINES; a unit is never written without
   an explicit `--yes`. This is why `local-service-gate.sh`'s bare
   `nx init --service` writes nothing.
2. **Explicit `--no-autostart`** at the host-side sites (`fresh-install-mvv.sh`,
   `release-sandbox.sh`).

**Rule when authoring or moving an E2E script:** any `nx init` carrying
`-y`/`--yes` without `--no-autostart` must be **container-executed**. Every
current such site is under `tests/e2e/migration-rehearsal/rehearse_*.sh`. Adding
a host-side one, or making a container script host-runnable, arms a collision
whose blast radius is the developer's live machine, not a red CI job.
Un-linted — tracked on `nexus-d5yu5`, which also records the subtler hazard:
`InstallStatus.ALREADY_PRESENT` reports an existing identical unit as success,
so a harness on that path can poll **production's** lease and pass green while
measuring the wrong service. A differing unit is fail-loud
(`InstallerError` -> `SystemExit(1)`); an identical one is not.

## Host-run harness write-time guard: `nx index`/`nx store`/`nx collection`/`store_put` (nexus-8tnz2)

A distinct hazard from the one above: a harness that indexes/stores real content into a service, rather than registering a system unit. Benchmark/gate debris (13 `code__test-repo-<hex>__voyage-code-3__v1` collections, `docs__hotfix_smoke`, `docs__local_smoketest_336`, `knowledge__val530`, a 2,828-doc `docs__1-2188` with no owner — all zero-catalog-doc T3 orphans) landed in the production tenant because nothing at write time kept a host-run harness from writing into whatever service `NX_SERVICE_URL`/`NX_SERVICE_TOKEN` happened to point at. `tests/test_host_harness_scratch_scope_lint.py` mechanically enforces an exact, per-file allowlist of every `nx index`/`nx store`/`nx collection`/`store_put`/`nexus.mcp.core` site across every tracked **shell (`.sh`) AND Python (`.py`)** file under `tests/e2e/**` and `scripts/**` — the scan originally globbed `.sh` only, which missed real in-process MCP-tool call sites entirely (fix-round CRITICAL 1: `scripts/validate/01-mcp-core.py`, `scripts/spikes/spike_rdr089_delos.py`, the latter DELETED outright — a spike with a live, unsandboxed `nx index pdf` write path and zero non-docstring references anywhere else in the repo). A nexus-8tnz2 census found every one of the 34 existing sites already safe under one of five shapes (READ-ONLY — including every `operator_*`/`nx_answer` MCP tool, which carry `readOnlyHint` or write only a telemetry row, never catalog/T3 debris — CONTAINER-isolated, `NX_LOCAL` + a sandboxed HOME/config dir (one enforced by an in-file guard, `01-mcp-core.py`'s `_refuse_unless_sandboxed()`, for a file that is independently `__main__`-runnable), the throughput bench's MARKER+SNAPSHOT precedent, or PROSE-ONLY), so there is no shared preamble to source — the allowlist IS the enforcement, not an escape hatch from one. A genuinely new site needing the operator's live service has two conforming routes named in the lint's failure directive: self-provision an engine and mint its own tenant (`POST <engine>/v1/tenants/create` under the boot bearer — the `tests/_engine_substrate.py` `mint_test_tenant` precedent), or the throughput bench's marker-scoped-owner + before/after `nx collection list` snapshot + EXIT-time teardown shape. The lint is deliberately dumb (per-line regex, same philosophy as the sibling lint above) — a descriptive echo/label string that merely mentions one of these verbs still counts as a site needing a reviewed, exact-counted entry. The write-time guard is SCOPED to this repo's own tracked harnesses, not a root-cause fix: the actual live debris population's producer is external and unidentified — see `_WRITE_TIME_GUARDS["tombstone-vanished"]` in `reconcile_stale.py` for that honesty note, and `nx catalog reconcile-stale --execute drop-orphan-collections` for the symptom-level sweep.

## Mode defaults (RDR-109 Phase 1)

The suite runs in **local mode by default** — no API keys, ONNX MiniLM embedding function via `nexus.db.minilm_direct.MiniLMDirectEmbeddingFunction` (aliased `DefaultEmbeddingFunction` in test imports, e.g. `tests/conftest.py`). This matches CI (which has no credentials) and reproduces a clean-install developer environment.

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

## Redirecting the config dir in a test: setenv, never setattr

Use `monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))` (or
`patch.dict(os.environ, ...)`). Never `monkeypatch.setattr("nexus.config.
nexus_config_dir", ...)` or `mock.patch` it: any module FIRST-imported while
that patch is live captures the lambda by value (`from nexus.config import
nexus_config_dir`), keeps it after teardown, and poisons every later test in
that xdist worker (PR #1467; the TestGcPurgeMarker ordering failure,
2026-08-20). `tests/test_nexus_config_dir_setattr_lint.py` (`-m lint`)
ratchets the remaining sites (nexus-78blw; src-side by-value imports are
nexus-grg79). Production modules resolve the dir at call time via
`from nexus import config as _config` + `_config.nexus_config_dir()`.

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

## CI's pytest jar is UNSTAMPED — deliberately

ci.yml's pytest-matrix service jar is built plain (`-DskipTests package`), so
`release_version` is BLANK and every cloud-mode probe fail-closes. A dev box's
`scripts/build-gate-jar.sh` STAMPS the version, so a unit test that
accidentally builds a REAL cloud vector client passes locally and fails only
in CI with `ManagedServiceIncompatible` naming the current floor — the floor
is (almost always) innocent; the test is reaching a real client it should be
injecting. Local repro: blank `release.properties` inside the jar (zip
update), run the failing test standalone, restore the stamp with
build-gate-jar. Precedent: release-7.7.0 shard reds, bead nexus-c7l4n,
T2 release-7.7.0-ci-shard-reds-2026-08-14.
