# tests/AGENTS.md

Test-suite conventions for AI coding agents. The project-root `AGENTS.md` covers global conventions and links this file. Mechanism, measurements and incident history that used to sit here live in the module docstrings named below and in [`docs/agents/test-running.md`](../docs/agents/test-running.md).

## Dev loop

Default local dev loop: `uv run pytest -n auto`. `pytest-xdist` is a `dev`-group dependency, opt-in via `-n`: it is deliberately NOT baked into `addopts`, so CI's `pytest-split` duration-balanced sharding (`.github/workflows/ci.yml`) is untouched. CI passes `-n 4` itself on the self-hosted pool shards and on the lint job, and nothing on hosted shards. Serial (`uv run pytest`, no `-n`) stays available for debugging output interleaving or fixture-isolation issues that parallel workers can mask.

**`-n auto` is capped twice** (`tests/_xdist_cap.py`; `NX_XDIST_NO_CAP=1` opts out of both):

- *Shared memory* (nexus-6qp25). Every worker boots its own Postgres cluster, each holding one SysV segment against `kern.sysv.shmmni`, a budget shared with every other cluster on the box (a gate, a peer's suite, a cluster orphaned by a killed battery). When it runs out, `initdb` dies with `shmget: No space left on device` and EVERY substrate-backed test errors at setup: thousands of errors that look like a catastrophic regression and are contention. Diagnose with `ipcs -m` before suspecting the code. The cap does nothing on a quiet machine.
- *Memory grant* (nexus-vo9qn). Workers <= min(`LAB_MEM_MB`, cgroup `memory.max`) / 3264 MiB (about 3.2 GiB, the measured per-worker peak of a FULL suite). So `-n auto` runs 15 on lab-l and 5 on lab-m, and the clamp prints its reason. Nothing applies without a visible limit (a laptop).

Both caps rewrite xdist's `option.tx` as well as `numprocesses` (`set_worker_count`): setting the count alone prints "clamped" and still spawns every worker (`TestClampsReallyReduceWorkers` runs real xdist). Measurements and the cost on lab-m: [test-running](../docs/agents/test-running.md) § Worker caps.

**Clusters and `TMPDIR` go on `/dev/shm` inside a grant** (nexus-me3gm, nexus-kyujr; `tests/_engine_substrate.py` for the cluster root, `tests/_tmp_root.py` for pytest's basetemp and `TMPDIR`; the docstrings carry the gates, layout and cleanup). Each is decided once per run by the xdist controller and inherited by the workers, so a run never drifts from tmpfs to disk as the grant fills. The summary says where each went (`clusters on tmpfs N, on disk M`; `nexus test tmp: ...`). `NX_TEST_PG_ROOT` and `NX_TEST_TMP_ROOT` take `default` (the tempdir, which is the NVMe write this removes; `NX_TEST_PG_ROOT=default` also keeps pytest's scratch there) or `<dir>` (kept after the run, for inspecting a failure). A run that exits non-zero keeps its engine logs and failed `tmp_path` trees under `<original tempdir>/nexus-test-reports-<uid>/<run>/failed/`. Rules for tests:

- A test that hands a child process its own `TMPDIR` builds the child's env with `tests._tmp_root.child_env_with_tmpdir(os.environ, <dir>)`, never a hand-written copy filtering `NX_TEST_TMP_ORIG`: inside a grant the outer run's `NX_TEST_TMP_ORIG` wins over `TMPDIR`, and a laptop never has the variable, so the filter is the part everyone forgets (a lint in `tests/test_tmp_root.py` rejects the pasted copy). A test that pins "follows `NX_TEST_TMP_ORIG`" or "drops it" sets it to a tmp_path decoy with `monkeypatch`, so reverting the fix fails off a grant too.
- A nested pytest whose test globs its own `TMPDIR` for clusters sets `NX_TEST_PG_ROOT=default`. A test that drives the root decision itself starts from a cleared `NX_TEST_PG_ROOT_SESSION` (`tests/test_engine_substrate_pg_root.py` has the fixture).
- A fixture must not assume hard links to the checkout (tmpfs cannot): the plugin-install fixture clones with `--shared` for this reason.
- `tests/test_tmp_root.py` drives the decision with injected inputs and runs real nested pytests against `NX_TEST_TMP_SHM_DIR`, a directory standing in for `/dev/shm` on any platform.

**If `SubstrateRootExhausted` trips** (nexus-vo9qn). It means this run chose tmpfs
for its clusters and, after 45 s of re-reading (or at once, within 120 s of an
earlier trip in that process), a start still saw under 512 MiB
(the floor) of tmpfs space or cgroup memory headroom. It is a capacity statement,
not a code failure: do not rerun hoping it passes.
- Read the cause: the exception names it (`... MiB free` or `the cgroup has N MiB
  of memory headroom left`); the pytest summary line says `clusters on tmpfs N, on
  disk M`; the `nexus_t2_substrate.cluster_root` and `root_decision_headroom`
  events (info level, so rerun with `--log-level=INFO`) give each start's root and
  the headroom the decision saw.
- Remedies, in order: (1) fewer workers or a larger `lab-run` class: `-n` =
  headroom / 3.2 GiB (a 16 GB grant carries 5), which is also what the memory cap
  computes unless `NX_XDIST_NO_CAP=1` overrode it; (2) `NX_TEST_PG_ROOT=<dir>`
  puts every cluster under a directory you choose (a disk you accept writing to);
  (3) `NX_TEST_PG_ROOT=default` puts every cluster back on the tempdir, which is
  the NVMe writes this mechanism exists to remove (3.9 GB in 4 minutes on lab-l),
  so use it for a one-off, never in a CI wrapper.

The `-m integration` fixtures that boot their own cluster (nexus-942h9) use the same root and conf: `pg_data_tempdir(prefix)` creates the dir through the root chooser, and the fixture appends `integration_pg_conf(pgdata)` to `postgresql.conf`. A new fixture must append the conf (`tests/test_integration_pg_cluster.py` has a census); one that needs durability semantics passes `parent_dir=` to stay on disk, and none does today. Clusters that still write PG to disk are listed, with the owner-record sweep detail, in [test-running](../docs/agents/test-running.md) § Integration clusters (census tracked in nexus-szyun).

**The session start refuses when the engine substrate cannot boot** (`tests/conftest.py` `_preflight_engine_substrate`, verdict from `tests/_substrate_preflight.refusal`, whose docstring has the mechanism). A missing or stale service jar (rebuild with `scripts/build-gate-jar.sh` after any pull/rebase touching `service/`), or a PG bundle for the pinned engine tag that is not downloadable, used to error every substrate-backed test at setup (25,636 errors on one full `-n auto` run, one fact). Now the xdist controller checks once, after the lease gates and before any worker spawns, and exits 75 with one line naming the cause and the remedy. It does not check the release stamp: a plain `./mvnw package` jar is unstamped, CI runs one on purpose, and only tests marked `needs_stamped_jar` skip on it (`tests/db/_service_fixture.py` `unstamped_jar_skip_reason`). `NX_TEST_T2_SUBSTRATE=none` skips it, and so does a nested pytest inside a lease holder. A test that spawns a child pytest and wants the preflight to run must clear `NX_SUITE_LEASE_HELD_BY` and give the child its own `NX_BUILD_LEASE_ROOT` (`tests/test_substrate_preflight.py` does).

**The suite lease queues; the laptop guard refuses** (`tests/conftest.py` `_take_suite_lease`, `_refuse_heavy_laptop_run`; nexus-tvt6u). A run whose `NX_TEST_T2_SUBSTRATE` is not `none` (the engine can boot; a scoped run included) takes the box-wide `suite` lease (`tests/_suite_lease.py`). A second such run on the box WAITS by default, up to `DEFAULT_WAIT_SECONDS` (30 minutes), printing `suite lease: waiting for <holder>; waited N s of M s` at once and then every minute, so `scripts/watched_pytest.py`'s stall clock (any output resets it) does not read a queued run as hung. It starts when the holder exits; if the wait runs out it exits 75 naming the holder. `NX_SUITE_LEASE_WAIT=0` restores the immediate exit 75, `NX_SUITE_LEASE_WAIT=1` is the same as the default, and `NX_SUITE_LEASE_UNGUARDED=1` still runs at once without queueing. Inside a lab-run grant (`LAB_CPUS` or `LAB_MEM_MB` set) on Linux the lease is skipped: lab-run is the queue there (admitting by CPU and memory), and the lease is the fallback for a run with no grant. On macOS a `lab-mac` grant is admission only, so the lease is still taken (`_take_suite_lease(platform=)`; `tests/e2e/macos-guest/host.sh` reads it). On macOS with no grant, an xdist run with more than one worker (after the SysV and memory clamps; an unresolved `-n auto` counts) whose substrate is not `none` exits 75 with `lab-remote -c lab-l -- uv run pytest <your args>`; `NX_LOCAL_HEAVY=1` opts out (one stderr line says so) for a run that needs claude, bd or the laptop's credentials. A serial or `-n 1` run, `NX_TEST_T2_SUBSTRATE=none` and a nested pytest are not refused. A test that spawns a child pytest to see a refusal sets `NX_SUITE_LEASE_WAIT=0` and drops `LAB_CPUS`/`LAB_MEM_MB` from the child env.

`-m lint` selects the O(repo) meta-tests (AST/regex scans of `src/nexus`, `conexus/` agent-skill-command markdown, RDR frontmatter, marker-selection coverage itself) that the default `addopts` (`-m 'not integration and not slow and not lint'`) excludes from the hot loop — they only change when repo *structure* changes, not application behavior, and run once in CI's dedicated `test-lint` job (two hosted groups, `pytest (lint markers, group N/2)`) rather than once per shard. Run them explicitly with `uv run pytest -m lint` when touching `conexus/`, RDR frontmatter, or a storage-boundary/hook-registration invariant those files pin.

`-m integration` is also deselected by default. CI's `pytest (integration, affected)` job runs, on every push and PR, the integration files the change touched or that directly import a `src/nexus` module it changed (`scripts/select_affected_integration_tests.py`), with a per-file non-vacuity check (`scripts/assert_integration_affected_ran.py`). Transitive imports and `service/` changes select nothing by themselves; the nightly local-service gate runs the whole family, the release battery runs it per release, and the CA-3 and write-seam jobs run their own files. A new integration test FILE therefore runs in CI on the push that adds it; a new test inside an existing file runs too, but a skip in it is visible only in the job's `-rs` output, not as a failure. If the job reports that a file skipped every test, give the job the missing prerequisite, or list the file in `ALL_SKIP_ALLOWED` with its reason (a runner limit, or a bead for a test no gate arms).

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

## Live-cloud recall-parity gate (nexus-d9xt2)

`tests/test_search_fanout_recall_parity.py` (`integration` marker, skipped by
default; `uv run pytest -m integration tests/test_search_fanout_recall_parity.py`)
compares the model-grouped batched cross-corpus fan-out against the pre-fix
one-call-per-collection fan-out (loaded standalone from the pre-fix commit's
`src/nexus/search_engine.py`) across 10 real queries spanning all five
corpus specs, against this box's own live cloud collections. Each query's new
top-10 is scored against five runs of the old fan-out (`_reference_score`: the
smaller of the share of new ids the old path ever returned and the share of the
ids it always returned that the new path kept) with a floor of 0.9, or 0.6 for a
corpus whose collection group splits into sub-batches (`_JACCARD_FLOOR_SPLIT`).
Reads only, and self-skips on a named content precondition (this tenant missing
real collections for one of the five corpus specs) rather than passing
vacuously. Five old runs cost one search per collection each: tell the cloud's
operator the UTC window before a run. Run it by hand after any change to
`nexus.search_engine.search_cross_corpus`'s batching, grouping, or per-batch
sizing logic; CI selects it when it changes but it skips there (a hosted runner
has no live cloud config). The scoring function has fast, non-`integration`
coverage in `tests/test_search_fanout_recall_parity_reference_logic.py`, which
runs in the default loop.

## Running an MVV journey against a managed-service tenant (nexus-ckejb)

`tests/test_rdr235_mvv_aspect_queue_tuples.py::test_mvv_journey_on_the_tuple_queue` can run against an EXTERNAL engine in a tenant the operator provisioned, instead of the session engine's minted one (RDR-235 Activation Step 1). The opt-in is `tests/_external_engine.py`, read by `_engine_tenant_env` (the body of `t2_service_env` / `t2_tenant`). It takes all three of: the `external_engine_ok` marker on the test, `NX_TEST_EXTERNAL_ENGINE_URL` and `NX_TEST_EXTERNAL_TENANT_TOKEN`. An unmarked test, or a marked one with one variable, keeps the local minted tenant and logs one `external_engine.variables_ignored` line, so a pair left exported cannot point the suite at production. The marker is carried by exactly two files, the journey module and `tests/test_external_engine_optin.py`; `tests/test_external_engine_marker_allowlist.py` (`-m lint`) fails on any other site, and on an allowlist entry that no longer carries it. Inside the journey module only the journey is marked.

Scenarios 24 (a child process on the PG-direct substrate env) and 25 (1,200 tasks) always run on the local engine, whatever the pair says; they carry no skip. Run the journey with `-k journey`, which deselects them. Running the whole file with the pair set is operator error: the journey goes to the external engine, and 24 still boots a local engine (it needs the gate jar, and the `leases=0 mints=0` line below no longer holds).

Before the run:

- **A tenant nobody uses, on an engine that carries the `aspects` template (E3).** The journey counts every task in the tenant and asserts that up front (available, claimed, dead and consumed rows all zero, and the old queue table empty), so a second run in the same tenant fails at once with "already holds aspect-queue work". A green run leaves, in that tenant: 58 catalog documents (50 journey, 6 old-table, 2 fence), one owner (`test-aspect-fixture-owner`), one collection with no chunks, about 57 `document_aspects` rows, 49 consumed tuple rows plus the one dead-lettered task (document 13) with its claim-log rows, done rows in the old queue table, and a committed `embedding_profile` row for `knowledge`. A red run can also leave claimed, dead or waiting tasks. Only dropping the tenant cleans it; that is yours, and a red run burns the tenant too. The one exception: a 422 at `register_collection` (the first write) leaves only the profile row, so a retry in the same tenant is legal after that specific failure.
- **State the write, and name the tenant.** The suite exempts itself from the production-write guard in-process; the external branch withdraws that for the test and requires `NX_ALLOW_PROD_WRITE="<reason>"` from you (a bare `1`/`true` is refused). The harness never sets it. When the engine names the bearer's tenant (`GET /v1/_whoami`), the reason must contain that tenant name, or the test fails at setup. Announce the UTC window to the engine's operator first.
- **A dev checkout with `uv sync` done** (an editable install). `guard_production_write` returns early when `_dev_checkout_root()` is None (`src/nexus/db/service_endpoint.py`), so under a non-editable install the reason check is silent and a write would go out unchecked. Do not use `lab-remote`: the variables carry a bearer.
- `NX_TEST_T2_SUBSTRATE=none`: the run boots no engine, mints no tenant and needs no jar. (Without it the session start still wants the gate jar and the suite lease for nothing.)
- **The token comes from a file, not the command line.** `export NX_TEST_EXTERNAL_TENANT_TOKEN=...` typed at the prompt leaves it in shell history. Keep `NX_TEST_EXTERNAL_ENGINE_URL` and `NX_TEST_EXTERNAL_TENANT_TOKEN` in a mode-0600 file and source it with `set -a`. `http://` is accepted only for a loopback host; a URL carrying `user:password@` is refused.

```bash
set -a; . ~/secure/mvv-tenant.env; set +a    # two lines: NX_TEST_EXTERNAL_ENGINE_URL=https://api.conexus-nexus.com, NX_TEST_EXTERNAL_TENANT_TOKEN=...
NX_ALLOW_PROD_WRITE="RDR-235 Activation Step 1 (nexus-lnwn1): MVV journey in throwaway tenant <name>, window <UTC>" \
  NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_rdr235_mvv_aspect_queue_tuples.py -k journey -q -p no:cacheprovider
```

The run prints one line at setup, token-free: `external engine: host=... tenant=... release_version=... embedding_mode=... embedding_profile=... posture=NX_LOCAL=<0|1> (from ...) bearer=validated by GET /v1/_whoami` (or `UNVALIDATED` when the engine has no such route, in which case the tenant shows as `external` and the reason is not asked to name it). Green is `1 passed, 2 deselected` and `tenant pool: leases=0 mints=0` (nothing local was leased). A red at setup names its cause: the missing `NX_ALLOW_PROD_WRITE`, a reason that does not name the tenant, an engine that does not answer `GET /version`, a bearer the engine refuses on `GET /v1/_whoami` (401/403) or an unexpected status there, or an engine reporting `embedding_degraded`.

`NX_LOCAL` follows the engine's `embedding_profile` from `GET /version` (`voyage` gives `NX_LOCAL=0`, `bge` gives `NX_LOCAL=1`), and `embedding_mode` only when the engine omits that key, because `register_collection` must send the model the tenant's profile holds and the engine answers 422 otherwise. `embedding_mode` alone (`onnx-local`, `voyage`, `dual`) is the runtime family and does not settle it: a `dual` engine can profile either way. The collection name's model token comes from `effective_embedding_model_for_writes`, so it matches in either posture. In an external run the lease is 10 s instead of 2 s (the fence variant's renew cadence and deadline scale with it), so the journey takes about a minute and a half rather than half a minute.

## Test-authoring directives

Distilled from the 2026-08 compression arc (design records in T2: `nexus/test-suite-compression-analysis-2026-08-05` and the closure/critique entries linked from it):

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
- **O(repo) meta-tests go in the lint bucket** (`pytestmark = pytest.mark.lint`), which is excluded from the hot loop but PR-gated by CI's `test-lint` job. Expensive shared computation inside one (e.g. an AST scan of `src/nexus`) gets a `functools.cache`d single scan.
- **The lint bucket is only safe for FILESYSTEM-scanned censuses** (`rglob` over `src/`/`tests/`: `test_storage_boundary_lint.py`, `test_no_new_sqlite.py`, `test_private_handle_access_census.py`, etc.), never for a `request.session.items`-scanned one. `-m lint`/`-k` deselection mutates `session.items` in place, so a census reading it sees only the lint-bucket tests, permanently and silently blind to the rest. (Corrected in nexus-8x4le: a session.items census is NOT xdist-unsafe, and reclassifying `test_mode_declarations_are_explicit.py` into the lint bucket made it miss a live violation; its module docstring has the account.) A session.items census that must run once per PR rather than once per shard is a CI-wiring problem, not a marker-reclassification one.
- **`session.items` shrinks under `--splits`/`--group` too, not just `-m lint`** (nexus-vdti6): CI's `pytest tests/ --splits 6 --group N` does `items[:] = group.selected`, so a census left in the default loop only sees its own shard. Both parts are mandatory for any session.items-based census:
  1. **Structural honesty**: call `tests.conftest.partial_session_view_reason(request)` at the top of the census and `pytest.skip(reason)` on a non-`None` return. It fires whenever `--splits`/`--group` is active, plus a generic floor (50% of the raw pre-deselection count) for any other shrink mechanism, and exempts a developer's own `-k` narrowing.
  2. **CI coverage**: the `pytest (mode-declarations census)` job (`.github/workflows/ci.yml`, job id `test-mode-census`) runs the whole `tests/` corpus with `NX_CENSUS_ONLY_JOB=1` and no `--splits`/`--group`. That variable makes `tests/conftest.py` SKIP-MARK (not deselect) every test outside `_CENSUS_ONLY_ALLOWED_MODULES`, so `session.items` stays the full corpus while no engine substrate, PG or jar is needed. Add a new census's module name to `_CENSUS_ONLY_ALLOWED_MODULES` instead of standing up a second dedicated job.

## The tenant is opt-in (nexus-j23ni, 2026-10)

A test gets a minted tenant (and the engine behind it) only when it asks: a
`t2_tenant` / `t2_service_env` parameter, or `@pytest.mark.t2_tenant` on the test,
its class or its module (`pytestmark = pytest.mark.t2_tenant`). A test that asks for
nothing mints nothing: `NX_SERVICE_URL`/`NX_SERVICE_TOKEN` are unset, `NX_LOCAL=1`,
and no T1 session exists. Each tenant is about 112 DDL statements, so an unneeded one
is the cost this removes.

**A test that reaches storage without a tenant fails at teardown, naming itself**
(`tests/_storage_touch_guard.py`, fail mode by default), even when the code under test
catches the error and carries on. Fix it with the mark, or with
`@pytest.mark.expects_unresolvable_endpoint` when raising `ServiceEndpointUnresolvableError`
is the point (a resolver-subject test; it is reported stale when it never does). Put the
mark on the TEST, not only on a fixture parameter: `def test_x(db, t2_service_env)` still
touches storage in `db` before the tenant exists, and only the mark makes the autouse pin run first.
A thread or child that touches storage after its test finished prints a yellow late-touch
warning naming that test and does not change the exit status of a scoped run (whether it lands is
thread timing); a run that set `NX_STORAGE_TOUCH_SWEEP=1` (below) fails on it. Join it inside the test.
A thread-pool worker (`ThreadPoolExecutor`, `multiprocessing.pool.ThreadPool`) outlives the test that
created it, so its touch is charged to the test that was running, never to the creator. A pool worker
that touches while no test is running is a late touch, owned by the test that created the worker
(`<no test running>` when it was created outside any test); it is never dropped.

Tests marked `integration`, `slow` or `lint` keep a tenant per test: the default selection
deselects them, so no census has said which of them touch storage.

- `NX_TEST_T2_SUBSTRATE=none` provisions nothing. The `per-test` escape hatch (older spelling
  `engine`: a tenant for every test, the guard off) shipped in 7.83.0 and is retired
  (nexus-llo7w); either value now errors every test at setup, naming the variable.
- `NX_STORAGE_TOUCH_GUARD_MODE=report` prints what would fail without failing it. `NX_STORAGE_TOUCH_SWEEP=1` adds the whole-run shape checks and marks the report as a whole-run one for the ratchet below; set it on a full-suite run, not a scoped one. A run that pytest-split is splitting, or that collects at least `SWEEP_AUTO_FLOOR` tests, asks for them by itself, but an automatic run is advisory (yellow warnings, exit status unchanged). Only an explicit `NX_STORAGE_TOUCH_SWEEP=1` run fails on them, late touches included, and the CI shards export it (nexus-ey246), so on CI each shard gates. **Downgrade trigger:** the first shard red on a late touch that passes on a rerun with no code change moves late touches back to a warning (`_vacuity` in the guard, one line); the shape, children-seen and guard-error checks keep gating. The floors, the shape checks and the advisory semantics: `tests/_storage_touch_guard.py` and [test-running](../docs/agents/test-running.md) § Storage-touch sweep.
- **The saving is ratcheted over the whole run** (nexus-zv8i7): the NUMBER of tests that minted a
  tenant, summed over the `nx-storage-touch-report.json` of every shard, must not exceed
  `provisioned_tests` + `tolerance_tests` (an absolute count, at most 50) in
  `tests/storage_touch_ceiling.toml`. Check it with `python -m tests._storage_touch_fan_in
  <dir holding the reports> --suite python_unit --shards 6` (`--suite docs_readers --shards 1` for that
  leg). The module is standard library only, because the CI gate runner has no pytest; a test holds
  that. It exits 1 over the ceiling, when the directory does not hold exactly `--shards` reports (a
  missing shard would under-count and pass), on a report from a non-sweep run, and on a repeated run id. A session never gates on its share: it sees only its file neighbourhood of the suite, so
  `provisioned_per_test` in the summary line and the per-report shares in the fan-in output are advice.
  Ceilings are reduce-only (the fan-in prints when one can be lowered); `"measure"` fails the fan-in and
  prints the number to commit. pytest-gate runs the fan-in over the six shards' reports, and the
  `python_unit` ceiling is armed (measured on CI, `measured_on` in the ceiling file). The CI shards
  export `NX_STORAGE_TOUCH_SWEEP=1` (nexus-ey246), so each shard gates on the whole-run checks, late
  touches included; the docs-readers leg does not export it. Marks only get added, so when the count rises, drop marks a test
  no longer needs before raising the ceiling.
  Before pushing, `uv run python scripts/tenant_delta.py` prints how many tests that mint a tenant your
  branch adds against its merge base with origin/develop, per changed module (nexus-g37hn; the marker,
  or `t2_tenant`/`t2_service_env` in the fixture closure, counted at collection). The push script runs
  it as a `PUSH_TENANT_HINT` line on stderr when the pushed range changes a test module; it never
  refuses (`NX_PUSH_TENANT_CHECK=0` skips it).
- A guard test that asserts a nested session or child process FAILED goes through
  `assert_session_failed_for(result, "<reason>")` / `assert_exit_failed_for(code, output, "<reason>")`
  (`tests/test_storage_touch_guard.py`), never a bare `ret != 0` or `== 1`: under the default
  `sweep=True` a body with no provisioned test fails on 'vacuous sweep: provisioned=0' whatever the rule
  under test did (nexus-j002k). A blank reason is refused (`"" in out` is always true). A lint in that
  file enforces it over the four guard test files: an equality to any exit code but 0 and the usage
  error 4, truthiness, `!=`, ordering and `in` are flagged, however the status is spelled (`.ret`,
  `.returncode`, a name bound to one, `bool()` / `int()` of one, inside a nested boolean expression).
- **Seeding the marks from a census:** run the whole suite as `NX_STORAGE_TOUCH_GUARD_MODE=report
  NX_STORAGE_TOUCH_SWEEP=1 NX_STORAGE_TOUCH_REPORT_DIR=<dir>`, then
  `uv run python scripts/annotate_t2_tenant.py <dir>` (a dry run) and `--apply`. It marks a module
  when 90% of its otherwise-tenantless tests need one, a class when all of them do, else the test,
  and lists failures that left no storage trace (a test reading `NX_SERVICE_TOKEN` straight from the
  environment fails with a `KeyError` the guard cannot see).

## The per-worker tenant pool (nexus-fpwj1, `NX_TEST_TENANT_POOL`, default ON since nexus-7zloy)

By default `t2_service_env` / `t2_tenant` lease one lazily minted tenant per xdist worker (`tests/_tenant_pool.py`; its docstring has the four-step lease protocol, the census and the retire rules) instead of minting and dropping one per test. While the pool is on, the substrate engine boots with `NX_TEST_TENANT_RESET_ROUTE=1`, `NX_SEARCH_PCI=0` and `NX_REAPER_ENABLED=false`. `NX_TEST_TENANT_POOL=0` turns the pool off, and the engine is then exactly the one develop boots; unset and any other value are on. A test that needs per-collection index builds or the engine reaper to run belongs under `@pytest.mark.fresh_tenant` plus its own engine, or under `NX_TEST_TENANT_POOL=0`; none does today. One lease at a time: re-entry raises `TenantLeaseBusyError`. The A/B numbers and the lease steps in prose: [test-running](../docs/agents/test-running.md) § Tenant pool.

- **Opt out with `@pytest.mark.fresh_tenant`** a test that mints or drops tenants, rotates or revokes the tenant's own token, or needs a tenant whose partition leaves have never held data. The tenant it gets is the old per-test mint.
- A writer that commits between one test's teardown and the next test's setup is caught by the lease-start census. A writer that fires DURING the next test's body is NOT detected: the next teardown wipes its rows before the census, so it shows up, if at all, only as an unexplained failure in that test. The A/B found one such writer, the engine reaper (now off in the pool's engine); fix the writer, never the census.
- The summary ends with `tenant pool: leases=N mints=M retired=R fresh_tenant=K workers=W ...`. A pool tenant that is not empty at the end, or tenant tests that took no lease, fail the run. `tests/test_tenant_pool.py` drives each behaviour against the real engine, each with a control that skips the eviction (or the lease-start census) and must show the stale state. Its engine-backed tests skip under `NX_TEST_TENANT_POOL=0`; there `tests/test_tenant_pool_end_to_end.py` runs the file in a child with the pool on (must run at least one test, pass all, skip none) and asserts the engine posture of the flag unset and `0`.

## Bytes written and DDL per run (nexus-sz651)

The primary gated signal is the **DDL count per test** (exact, the same on every host, and the lever that explains the writes); WAL bytes are a churn proxy for disk thrashing, not a disk meter. Every run with the engine substrate prints a `ddl: migration=<n> tests=<n> per_test=<x> wal: ...` line in the terminal summary and writes `nx-ddl-report.json` (under `NX_DDL_REPORT_DIR`, else the controller's basetemp). **WAL is a relative, lower-bound churn proxy, not what a disk-backed cluster would write, and on Linux it lands on tmpfs and never reaches the NVMe** (`fsync=off`, no full-page images; the Python substrate also `wal_level=minimal`, so Python and Java WAL are not comparable). `io_wbytes` is the job cgroup's write counter over the pytest window where readable: informational, not ratcheted. **Not measured**: the job workspace and checkout, `target/`, `.venv`, jar builds, journald, anything before pytest starts, and the docker-run and e2e shell clusters. The labrun NVMe sampler is the source of truth for bytes written to disk. `tests/_ddl_counter.py` explains the DDL signals and the counting rule; the Java twin is `DdlLogParser`/`DdlCounts`, and its server-log and container mechanics are in [test-running](../docs/agents/test-running.md) § DDL counter, Java side. A Java test must not call `getLogs()` on a Postgres container (`PgContainerTmpfsGateTest` fails the build if one does).

The ceilings are in `tests/ddl_ceiling.toml` and are INTENSIVE numbers, so how the work is split does
not move them: bytes and DDL per test and the migration's bytes and DDL per booted worker (Python); the
migration's bytes and DDL per fork plus the totals of everything else (Java).
**The Python gate is the fan-in over the whole run** (nexus-ekp7w, nexus-2rhif):
`scripts/ddl_ratchet.py fan-in --suite python_unit --shards N [--advisory] --report <every shard's report>` sums the
shards and judges those four numbers over the sum, so it does not flap with which tests the split gave a
shard, with `-n` (a pool shard runs 4 workers, a hosted one 1) or with a shard whose workers never booted
the substrate (it boots lazily; such a shard still writes its report, with `workers_booted: 0`).
`check --suite python_unit --report <one shard's report> --advisory` prints the same comparison for one
shard and always exits 0. In `ci.yml` each 3.12 `test` shard exports `NX_DDL_REPORT_DIR`, runs the advisory
check and uploads the report (kept 7 days); `pytest-gate` downloads all of them and runs the fan-in after
its verdict step. The Java steps of
`service-ci.yml` pass `-Dnexus.ddl.run=<run id>-<suite>` to mvn and the same id to `check --run`, so the
persistent pool workspace's older `service/target/ddl-counts` files are never read; they are `--advisory`
exactly while their ceilings are still `"measure"` (pending nexus-0ty8z's DDL cut), and
`tests/scripts/test_ddl_ratchet_wiring.py` fails the commit that sets them and leaves the flag on.
**The Python fan-in is armed** (2026-10-20, nexus-ekp7w): its ceilings come from three Linux CI fan-ins on
the pool with the tenant pool on (the run ids are in `tests/ddl_ceiling.toml`), `[python_unit] measured_on`
is the marker, and the wiring lint requires the fan-in to enforce and none of its steps (nor each shard's
advisory check and report upload) to be `continue-on-error` while the marker is set. One exception by
runner: when the shards ran hosted (a fork PR, or `NX_CI_POOL=off`) the fan-in passes `--bytes-advisory`,
which prints a WAL breach without failing, because WAL per test is host-dependent and the ceilings were
measured on the pool; the DDL metrics are exact counts and gate everywhere. The flag's condition is the
`test` job's `runs-on` condition, and the lint keeps them equal. Disarming (dropping the marker) needs the
advisory wiring back in the same commit.
**Once armed, a commit that adds a Liquibase changeset bumps `migration_ddl_per_worker` in the same commit**
(its tolerance is 0, because the migration's DDL count is the changelog's own; the failure message says
the same). Raising any ceiling needs the reason in that commit's message.
Bytes carry a tolerance band (`bytes_tolerance`), WAL varying a little; a `"measure"` ceiling makes the check
exit 11 and print the numbers to commit. Java runs are told apart by `-Dnexus.ddl.suite=main|integration`
(the pom sets it; CI passes it) and the run id (`-Dnexus.ddl.run`, default the Maven build timestamp).
Exit codes: 0 ok, 10 above a ceiling, 11 a ceiling unset, 12 not comparable (argparse is 2, a crash 1).
12 includes a measured 0 on a booted cluster (the counter saw nothing) and `workers_lost > 0`: an xdist
worker killed before its `sessionfinish` leaves no file, so the controller counts the node going down
without a report and the report says so, where file recovery covers only a worker that wrote its file and
did not hand it over.

The counter turns fully off for a same-sha A/B (nexus-3cwsw): `-Dnexus.ddl.count=false` for Java, `NX_DDL_COUNT=0` for pytest (default on; any other value fails loud). Off adds none of the counter's settings to a container or cluster and writes no count file or report, so a ratchet run has nothing to read from an off run, by design. `DdlCountSwitchTest` and `test_ddl_counter.py` pin the off recipe. A Java test that asserts on a counter-only surface (the collector file, the probe, marks, WAL reads, the parser on a live log) starts with `assumeTrue(DdlCounts.enabled(), ...)` so an off run is green with those skipped; `tests/test_java_counter_tests_assume_lint.py` (lint) fails when such a `@Test` lacks it. The scan is textual and keyed on a list of surface names in that file: extend the list when a new counter-only surface appears. The same-sha A/B procedure and its 2026-10-19 result: [test-running](../docs/agents/test-running.md) § DDL counter A/B.

Clusters other than the substrate's are not counted. `tests/ddl_uncounted_clusters.toml` lists the cluster start sites a static scan finds, and `tests/test_ddl_uncounted_clusters_lint.py` keeps the list equal to the scan, so a new start site must be read through the counter or listed on purpose. The terminal line's `<n> uncounted cluster start sites` is a census of call sites, not clusters; `tests/_ddl_cluster_sites.py` lists what the scan does not cover.

Which Java test classes may build their own schema is a checked table (nexus-0ty8z): a schema-only class takes a clone of the migrated template (`PgContainerHelper.start()`, or `startMigratedByOwner()` for production's ownership shape). A class that calls `startEmptyDatabase`, `startDedicated` or `newContainer` is listed in `service/src/test/resources/db-start-classification.tsv` with a kind and a reason, and `DbStartClassificationGateTest` fails on an unlisted caller or a stale row. The Java report's buckets and the first-`applyProductSchema`-is-a-no-op edge: [test-running](../docs/agents/test-running.md) § DDL counter, uncounted clusters and Java buckets.

## Seeding a chunk that has no owner (RDR-223 Phase 3)

The engine refuses a write to `/v1/vectors/upsert-chunks` whose chashes have no live
manifest row in the collection (`/store-put` and `/upsert-reference-only` are retired
and answer 410 Gone). A test that needs such a chunk (an orphan on purpose: the RDR-192
census, reaper and gc tests, the `nx store import` tests; or the chunk that must
exist before its manifest row because `fk_catalog_chunks_chunk` wants it first)
builds it with `tests/_chunk_seed.py::seed_chunks_direct`, never with
`upsert_chunks`, `upsert_chunks_with_embeddings` or `put`. It INSERTs into
`nexus.chunks` as `nexus_svc` with the tenant GUC set (FORCE RLS applies), after
registering the collection through the client's own registration path. The
embedding column follows the collection's REGISTERED model and a vector of
another width is refused; duplicate ids collapse first-wins and a repeat write
merges metadata, as the route does (`tests/test_chunk_seed.py` pins that parity
against the route while the route still accepts ownerless writes). Default
vector is zero; pass `embed=True` when the test searches, clusters or exports
the chunk, or `embeddings=` for exact vectors. `seed_manifest_chunks` is built on
it; `give_chunks_a_live_owner` then supplies the manifest row. A test that only
needs SOME authenticated chunk write to keep working (auth, retry, transport)
seeds and owns the chunk first and makes the write under test an owned rewrite.
A subprocess that seeds gets the parent's Postgres from `substrate_env()`; `ensure_engine()` in a fresh
interpreter would boot a second one. A test with its own hermetic Postgres
(`tests/db/` gates that spawn a container or a private `initdb`) builds the same
statement with `chunks_insert_sql` and runs it as that database's superuser.
Java engine tests use `PgContainerHelper.insertChunks` (typed jOOQ), a looser twin and NOT the same contract (it runs as the connection's role, so RLS does not apply, and it leaves an existing `(tenant, collection, chash)` alone); a Java test that re-seeds a chash must not rely on the conflict behavior, and `PgContainerHelper.insertOwnedChunks` is the pre-own form. The full contrast and the route-subject tests: [test-running](../docs/agents/test-running.md) § Seeding chunks, Java side. A test whose SUBJECT is the route itself (deadline mapping, embed errors, the `{"upserted": N}` envelope) cannot move; it asserts the refusal or pre-owns its chashes instead.

A test that needs the engine
in log-only mode sets `NX_OWNERLESS_WRITE_MODE=log-only` in the engine's environment; the substrate
pins `enforce` itself (an unset or blank value is log-only on the engine) unless the caller's environment sets a non-blank
one: a blank `NX_OWNERLESS_WRITE_MODE=` counts as unset there, as it does at the local launcher (the raw engine
parses blank as log-only), so `tests/_engine_substrate.py` goes through `_pin_ownerless_write_mode`, not `setdefault`.

## A fresh test host: what it needs, and what the suite provisions itself

The suite provisions its own PostgreSQL bundle and the engine's ONNX models
on first use, each once per box under a cross-process lock: the PG bundle
under `$NX_TEST_SUBSTRATE_CACHE/<tag>/` (the HOME fence pins it to the real
cache; else `$XDG_CACHE_HOME/nexus-test-substrate`, else `~/.cache/...`), the models under the engine's onnx_models root
(`$NX_ONNX_MODEL_DIR`, else `~/.cache/nexus/onnx_models`). A box that is
offline on its first run gets one exit-75 line at session start naming the
pinned engine tag, not a wall of setup errors (see the substrate preflight
above). Port shards scale with the xdist worker count, so `-n auto` fits any
core count (nexus-wvyvn).

What the host itself must supply:

- A JDK and a Docker socket for `scripts/build-gate-jar.sh` (jOOQ codegen runs
  Testcontainers). Under colima, export both `DOCKER_HOST` and
  `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`; without them the jar build fails in
  seconds and every substrate test then errors at setup.
- `shellcheck`, bash 5 and GNU coreutils for the shell lints and the e2e
  scripts (macOS ships bash 3.2 and BSD coreutils).
- A git identity (`user.name`, `user.email`): tests that commit in a temp repo
  fail without one.
- A Python 3.10 or newer for the host side of the e2e harness (nexus-u67ow). A
  bare `python3` is whatever the box ships first on PATH, and on a Mac with no
  Homebrew python that is `/usr/bin/python3`, 3.9.6, where
  a harness module that used `match` failed there. The harness does not
  call `python3`: `tests/e2e/lib/python.sh` resolves one interpreter, checks its
  `sys.version_info`, exports it as `E2E_PYTHON`, and refuses with exit 2 naming
  every candidate it tried when none qualifies. `NX_E2E_PYTHON=<interpreter>`
  picks one explicitly (it binds: if that interpreter is too old the run
  refuses rather than falling back to another); otherwise the resolver tries an
  inherited `E2E_PYTHON`, `python3`, `python3.14` down to `python3.10`, then
  `uv python find`. A script that reads `tomllib` asks for 3.11 at the point it
  reads. New host-side e2e scripts source `tests/e2e/lib/python.sh`, call
  `e2e_python_resolve`, and use `"$E2E_PYTHON"`;
  `tests/scripts/test_e2e_python_resolver.py` fails on a bare `python3` call.
  Scripts that run inside a container image keep the image's own python3.

## E2E isolation: a sandboxed HOME does NOT isolate a service install

**Only a container isolates the service unit. `HOME` / `NEXUS_CONFIG_DIR` do
not.** The developer box runs Sam's real cloud-mode install beside this
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
2. **Explicit `--no-autostart`** at the host-side sites (`fresh-install-mvv.sh`).

**Rule when authoring or moving an E2E script:** any `nx init` carrying
`-y`/`--yes` without `--no-autostart` must be **container-executed**. Every
current such site is under `tests/e2e/migration-rehearsal/rehearse_*.sh`. Adding
a host-side one, or making a container script host-runnable, arms a collision
whose blast radius is the developer's live machine, not a red CI job.
Linted by `tests/test_nx_init_autostart_collision_lint.py` (nexus-d5yu5), which also records the subtler hazard:
`InstallStatus.ALREADY_PRESENT` reports an existing identical unit as success,
so a harness on that path can poll **production's** lease and pass green while
measuring the wrong service. A differing unit is fail-loud
(`InstallerError` -> `SystemExit(1)`); an identical one is not.

## Host-run harness write-time guard: `nx index`/`nx store`/`nx collection`/`store_put` (nexus-8tnz2)

A distinct hazard from the one above: a harness that indexes or stores real content into a service, rather than registering a system unit, writes into whatever service `NX_SERVICE_URL`/`NX_SERVICE_TOKEN` point at (benchmark debris reached the production tenant this way). `tests/test_host_harness_scratch_scope_lint.py` enforces an exact, per-file allowlist of every `nx index`/`nx store`/`nx collection`/`store_put`/`nexus.mcp.core` site in every tracked shell AND Python file under `tests/e2e/**` and `scripts/**`; the allowlist IS the enforcement, there is no shared preamble to source. A genuinely new site that needs the operator's live service has two conforming routes, named in the lint's failure directive: self-provision an engine and mint its own tenant (`POST <engine>/v1/tenants/create` under the boot bearer, the `mint_test_tenant` precedent in `tests/_engine_substrate.py`), or the throughput bench's marker-scoped-owner plus before/after `nx collection list` snapshot plus EXIT-time teardown. The lint is deliberately dumb (per-line regex): a descriptive string that merely mentions one of these verbs still counts as a site. The census of the 34 sites that justified the five safe shapes, and the debris list: [test-running](../docs/agents/test-running.md) § Host-run harness write guard.

## Mode defaults (RDR-109 Phase 1)

The suite runs in **local mode by default** — no API keys, ONNX MiniLM embedding function via `nexus.db.minilm_direct.MiniLMDirectEmbeddingFunction` (aliased `DefaultEmbeddingFunction` in test imports, e.g. `tests/conftest.py`). This matches CI (which has no credentials) and reproduces a clean-install developer environment.

Tests that exercise **cloud-mode behavior** (real Voyage calls, managed-service routing, `_has_credentials()`-gated code paths, `voyage-context-3` / `voyage-code-3` embedder assertions) **opt in** by depending on the `cloud_mode` fixture:

```python
def test_something(cloud_mode, ...):
    ...
```

Or at module / class scope:

```python
import pytest

pytestmark = pytest.mark.usefixtures("cloud_mode")
```

The `cloud_mode` fixture lives in `tests/conftest.py`. It sets `NX_LOCAL=0` and `VOYAGE_API_KEY` to test sentinels and monkeypatches `nexus.config.is_local_mode` to return `False`.

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

## Faking a stdlib function: patch the module's binding, never the stdlib module

`patch("nexus.x.subprocess.run")`, `monkeypatch.setattr(mod.os, "kill", f)` and
`patch("asyncio.create_subprocess_exec")` all replace the attribute on the one
stdlib module the whole worker shares, so every thread and fixture teardown
sees the fake while it is live (a fake `time.sleep` hung a substrate teardown's
`subprocess.run` for 50 minutes, nexus-hkafl). Use `tests/_module_seam.py`:
`patch_in("nexus.x", "subprocess.run", ...)` (context manager, decorator,
`start()`), `setattr_in(monkeypatch, mod, "os.kill", f)`, `module_proxy` /
`module_time` / `patch_time`. Name every module the code under test reaches
the function through, as a tuple. A module that does `from subprocess import
run` is patched as `mod.run`. `tests/test_module_seam.py` (`-m lint`) rejects
the global forms; a site that must stay process-wide goes in its `ALLOWED`
with the reason.

## Lint guard

`tests/test_mode_declarations_are_explicit.py` enforces the convention. For every collected test whose source contains the regex `voyage-(context|code)-3`, it requires one of:

- The test depends on `cloud_mode` (directly, via class `pytestmark`, or via module `pytestmark`).
- The test's file is in `_MODE_LINT_EXCLUDE_FILES` (uniformly mode-independent).
- The test's nodeid is in `_MODE_LINT_EXCLUDE_NODEIDS` (per-test exclusion for mixed files).

Exclusion categories are documented in `tests/conftest.py` above each set.

## When you add a new test that references voyage embedder names

Decide:

1. **Is the voyage token a schema-canonical name** (e.g. `corpus.canonical_embedding_model("code")`, a collection-name parse / render round-trip, RDR-103 four-segment shape)? → mode-independent. Add the test's file to `_MODE_LINT_EXCLUDE_FILES` (if the whole file is schema-only) or its nodeid to `_MODE_LINT_EXCLUDE_NODEIDS`.
2. **Does the test actually exercise cloud-mode behavior** (real Voyage embedder, `_has_credentials()` gated path, managed-service routing)? → add `cloud_mode` to the test's fixture list, or `pytestmark = pytest.mark.usefixtures("cloud_mode")` at module scope.

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
