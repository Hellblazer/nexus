# tests/AGENTS.md

Test-suite conventions for AI coding agents. `CLAUDE.md` is not a symlink here; the project-root `AGENTS.md` covers global conventions.

## Dev loop (test-suite-compression P0, nexus-test-cleanup 2026-08-05)

Default local dev loop: `uv run pytest -n auto`. `pytest-xdist` is a `dev`-group dependency, opt-in via `-n` — it is deliberately NOT baked into `addopts`, so CI's `pytest-split` duration-balanced sharding (matrix parallelism across runners, see `.github/workflows/ci.yml`) is untouched; `-n auto` is a second, independent, local-only lever. Serial fallback (`uv run pytest`, no `-n`) remains available for debugging output-interleaving or fixture-isolation issues that parallel workers can mask.

**`-n` is capped to this machine's shared-memory headroom** (nexus-6qp25,
`tests/_xdist_cap.py`). Every worker is a separate process, and
`_engine_substrate.ensure_engine` boots once per process, so N workers means N
Postgres clusters, each holding one segment against `kern.sysv.shmmni`. The
budget is machine-wide: a concurrent gate, a peer session's suite, or a
cluster orphaned by a battery killed mid-run all spend from the same pool.
When it runs out `initdb` dies with `shmget: No space left on device` and
EVERY substrate-backed test errors at setup — thousands of setup errors that
look like a catastrophic regression and are contention. Measured on a 16-core
box: one cluster is one 56-byte segment, the full suite at `-n 16` peaks at 20
of 32 and passes, so the cap does nothing on a quiet machine and binds only
when something else has already taken the budget. `NX_XDIST_NO_CAP=1` opts
out. Diagnose with `ipcs -m` before suspecting the code.

**`-n` is also capped to the run's memory grant** (nexus-vo9qn,
`tests/_xdist_cap.py`): workers <= min(`LAB_MEM_MB`, cgroup `memory.max`) / 2.5
GiB. The 2.5 GiB is the measured per-worker peak (engine JVM + Postgres cluster
+ the cluster's tmpfs pages, charged to the grant): lab-l, 48 GB, 16 workers
peaked 39.4 GB; lab-m, 16 GB, `-n 8` peaked 14.5 GB. So `-n auto` keeps 16 on
lab-l and runs 6 on lab-m; the clamp prints its reason. Nothing applies without a
visible limit (a laptop). `NX_XDIST_NO_CAP=1` opts out of both caps. The cost on
lab-m is real: `-n auto` goes from 8 workers to 6 (25% less parallelism, a longer
wall clock), because 8 at 14.5 of 16 GB left no room for the run to stay on tmpfs.
Both caps rewrite xdist's `option.tx` as well as `numprocesses`
(`set_worker_count`): xdist builds `tx` before conftest loads, so setting the
count alone prints "clamped" and still spawns every worker (the SysV cap was inert
this way until nexus-vo9qn; `TestClampsReallyReduceWorkers` runs real xdist).

**Where substrate clusters live** (nexus-me3gm, nexus-vo9qn): on Linux with
`/dev/shm` mounted tmpfs, writable and with 3 GiB free (and 3 GiB of cgroup memory
headroom), each worker's Postgres cluster is created under
`/dev/shm/nexus-test-pg-<uid>` with `pg_wal` capped at 64 MB and dynamic shared
memory kept inside the cluster dir; otherwise (macOS, no tmpfs, low room) it stays
under the tempdir, and a Linux fallback warns with its reason. **The choice is made
once per run, never per cluster**: the xdist controller takes it before it spawns
workers (a serial run, at its first cluster) and persists it in
`NX_TEST_PG_ROOT_SESSION`, which every worker inherits. A run that chose tmpfs
therefore never drifts to ext4 as the grant's memory fills (tmpfs pages are charged
to it; that drift wrote 3.9 GB to the NVMe in 4 minutes on lab-l). A cluster start
that finds under 512 MiB of tmpfs space or cgroup headroom (droppable page cache
counted as room) waits up to 45 s for it to recover, then raises
`SubstrateRootExhausted` (see "If `SubstrateRootExhausted` trips" below). The start
decision uses the same page-cache-credited headroom, so a grant warmed by a build or
`uv sync` still picks tmpfs; `nexus_t2_substrate.root_decision_headroom` logs the raw
and the credited figure. Every cluster start
logs `nexus_t2_substrate.cluster_root` (root, `on=tmpfs|disk`, and the refusal reason;
a disk start with a reason is a warning), and the pytest summary ends with
`clusters on tmpfs N, on disk M`. `NX_TEST_PG_ROOT=<dir>` picks a root,
`NX_TEST_PG_ROOT=default` forces the tempdir for new clusters. The stray-cluster
sweep and `scripts/sweep-test-substrates.sh` always look in the tmpfs root too,
so a crash-orphaned cluster in RAM is reaped. A nested pytest whose test globs
its own `TMPDIR` for clusters must set `NX_TEST_PG_ROOT=default`. A test that
drives the decision itself starts from a cleared `NX_TEST_PG_ROOT_SESSION`
(`tests/test_engine_substrate_pg_root.py` has the fixture).

**Where pytest's basetemp and `TMPDIR` live** (nexus-kyujr, `tests/_tmp_root.py`). One
CI-shaped shard wrote 442 MiB to chas's NVMe and 227 MB of that was `tmp_path` data
under `~/tmp/pytest-of-<user>`, which pytest keeps for the last three runs. Inside a
lab-run grant (`LAB_CPUS` set; a CI job on the pool has it too) on Linux, with `/dev/shm`
a writable tmpfs **not mounted `noexec`**, 3 GiB free and 3 GiB of cgroup headroom (the
cluster root's floors), the controller creates
`/dev/shm/nexus-tmp-<uid>/r<random>/{t,p,owner.json}` (0700, a sibling of
`nexus-test-pg-<uid>`) before it spawns workers, runs one real script from it, and sets
`TMPDIR=<run>/t` and `PYTEST_DEBUG_TEMPROOT=<run>/p`. `tempfile`, `tmp_path`, every child
process, the fenced home and the cluster-log file follow; workers and nested pytest runs
find `NX_TEST_TMP_SESSION` and decide nothing. The engine JVM follows too: the JDK ignores
`TMPDIR` on Linux, so every substrate engine launch gets `-Djava.io.tmpdir=<TMPDIR>`
(`with_substrate_sizing` in `tests/db/_service_fixture.py`; netty and JNA extract natives
there on each boot). That is safe for the engine's uid probe, which reads `/proc/self/status`
first and only elsewhere makes one file in that directory. The original tempdir stays in
`NX_TEST_TMP_ORIG` and is what four things use: the boot-slot directory (a per-run slot
directory would split the cross-run boot cap), the stray-cluster sweep's disk root, a crashing
JVM's hs_err report (`-XX:ErrorFile`, `jvm_error_file_arg`: evidence must outlive the run dir),
and any cluster whose root decided on DISK (`pg_cluster_parent()` is None means the original
tempdir, never the run's tmpfs). The start-of-run daemon drain also scans the original
tempdir's `pytest-of-<user>`, so what a disk-mode or killed run stranded there is still reaped.
What does NOT follow: the ONNX native cache (`NX_ORT_NATIVE_DIR`, pinned to the real home on
purpose). The summary ends with one line,
`nexus test tmp: basetemp and TMPDIR on tmpfs <run>, N MiB at the end of the run, removed at
exit; nx-* reports are copied to <dir>`, or `... on disk <tempdir> because: <reason>`
(not inside a grant, not Linux, noexec, low room, ...; a Linux grant that could not use
tmpfs also warns). Settings: `NX_TEST_TMP_ROOT=default` keeps the old behaviour;
`NX_TEST_TMP_ROOT=<dir>` uses `<dir>/{t,p}` and keeps it after the run (inspecting a
failure); `NX_TEST_PG_ROOT=default` also keeps the tempdir; an explicit `--basetemp` wins for
the basetemp. **The run dir is removed at the end of the run** (`pytest_unconfigure`, after
the terminal summary), not retained: pytest's own keep-3 retention only ever sees the one
numbered dir inside the private run dir, so it never keeps a previous run on tmpfs. The DDL
counter and the storage-touch guard write their `nx-*` reports into the controller basetemp
by default, after the session finishes, so just before the removal the basetemp's top-level
`nx-*` files are copied to `<original tempdir>/nexus-test-reports-<uid>/<run>` (newest three
kept); `NX_DDL_REPORT_DIR` / `NX_STORAGE_TOUCH_REPORT_DIR` still put them where you choose, and the
DDL and storage-touch summary lines print the copy's path, not the tmpfs one. **A run that exits
non-zero also keeps its failure evidence** in `<report home>/failed/`: the per-test engine logs
(`nexus-svc-*`; the session engine's `engine.log` is removed with its cluster and is not kept), the `tmp_path` trees of the failed tests (matched by name from the failed node
ids, controller and xdist workers), up to 256 MiB, and `SUMMARY.txt` naming what was kept and what
the cap skipped; `NX_KEEP_FENCED_HOME=1` also copies the fenced home there, uncapped. The summary
line says so for a failed run. A passing run keeps only the reports. SIGKILL or SIGTERM of the
controller keeps nothing (the next run sweeps its dir). A
killed run leaves one `r*` dir, which the next grant run removes at its start: the owner record
(pid plus `/proc` start time) says the controller is gone, or a record-less dir is older than
10 minutes; the dir's `p` tree is first handed to the same scoped daemon reaper the
start-of-run drain uses, and nothing outside the per-uid root or owned by another user is
touched. A fixture must not assume hard links to the checkout (tmpfs cannot): the
plugin-install fixture clones with `--shared` for this reason (a plain clone copied the 1.3 GB
object store per worker). `tests/test_tmp_root.py` drives the decision with injected inputs and
runs real nested pytests (serial, xdist, SIGKILL) against `NX_TEST_TMP_SHM_DIR`, a directory
standing in for `/dev/shm` that is treated as exec-capable tmpfs on any platform.

**If `SubstrateRootExhausted` trips** (nexus-vo9qn). It means this run chose tmpfs
for its clusters and, after 45 s of re-reading, a start still saw under 512 MiB
(the floor) of tmpfs space or cgroup memory headroom. It is a capacity statement,
not a code failure: do not rerun hoping it passes.
- Read the cause: the exception names it (`... MiB free` or `the cgroup has N MiB
  of memory headroom left`); the pytest summary line says `clusters on tmpfs N, on
  disk M`; the `nexus_t2_substrate.cluster_root` and `root_decision_headroom`
  events (info level, so rerun with `--log-level=INFO`) give each start's root and
  the headroom the decision saw.
- Remedies, in order: (1) fewer workers or a larger `lab-run` class: `-n` =
  headroom / 2.5 GiB (a 16 GB grant carries 6), which is also what the memory cap
  computes unless `NX_XDIST_NO_CAP=1` overrode it; (2) `NX_TEST_PG_ROOT=<dir>`
  puts every cluster under a directory you choose (a disk you accept writing to);
  (3) `NX_TEST_PG_ROOT=default` puts every cluster back on the tempdir, which is
  the NVMe writes this mechanism exists to remove (3.9 GB in 4 minutes on lab-l),
  so use it for a one-off, never in a CI wrapper.

The `-m integration` fixtures that boot their own cluster (nexus-942h9) use the
same root and conf: `pg_data_tempdir(prefix)` creates the dir through the root
chooser and records it in `<root>/nexus_integration_owner_<pid>.json` (with the
engine JVM, via `spawn_service`), and the fixture appends
`integration_pg_conf(pgdata)` to `postgresql.conf` (fsync, synchronous_commit and
full_page_writes off; on tmpfs also the 64 MB WAL cap and mmap DSM). The sweep
reaps a record whose owner is dead, and runs once before a process's first
integration cluster. A new fixture must append the conf
(`tests/test_integration_pg_cluster.py` has a census). A fixture that needs
durability semantics passes `parent_dir=` to stay on disk; none does today.

Out of scope of that routing (still write PG to disk; census and fix tracked in
nexus-szyun): the `tmp_path_factory` initdb creators `tests/db/test_svc_monitor_role_live.py`,
`tests/db/test_nexus_diag_role.py` and `tests/daemon/test_storage_service_daemon_pg_monitor_backfill.py`,
and the two `docker run postgres` tests `tests/db/test_write_seam_gate_integration.py` and
`tests/db/test_http_combined_query_integration.py`. The cluster prefix may reach 26 chars
(`nexus_recovery_inttest_pg_`); `integration_cluster_dir` refuses a path whose
socket would pass `sun_path`.

**The session start refuses when the engine substrate cannot boot** (`tests/conftest.py`
`_preflight_engine_substrate`, verdict from `tests/_substrate_preflight.refusal`).
A missing or stale service jar, or a PG bundle for the pinned engine tag that is
not downloadable (the tag is not published yet, as on a release branch whose
engine is still building), used to error every substrate-backed test at setup:
25,636 errors on one full `-n auto` run, one fact. Now the xdist controller checks
once, after the lease gates and before any worker spawns, and exits 75 with one
line naming the cause and the remedy (`scripts/build-gate-jar.sh`, or wait for
the engine release). It asks the substrate's own functions
(`jar_freshness_skip_reason`, `_engine_substrate._pg_bin`), so there is no second
copy of the freshness rule, and it provisions the PG bundle itself when it is not
cached, so workers find it. `NX_TEST_T2_SUBSTRATE=none` skips it; a nested pytest
inside a lease holder skips it; a bug in the preflight is logged and the run goes
on. A test that spawns a child pytest and wants the preflight to run must clear
`NX_SUITE_LEASE_HELD_BY` and give the child its own `NX_BUILD_LEASE_ROOT`
(`tests/test_substrate_preflight.py` does).

`-m lint` selects the O(repo) meta-tests (AST/regex scans of `src/nexus`, `conexus/` agent-skill-command markdown, RDR frontmatter, marker-selection coverage itself) that the default `addopts` (`-m 'not integration and not slow and not lint'`) excludes from the hot loop — they only change when repo *structure* changes, not application behavior, and run once in CI's dedicated `pytest (lint markers)` job rather than once per shard. Run them explicitly with `uv run pytest -m lint` when touching `conexus/`, RDR frontmatter, or a storage-boundary/hook-registration invariant those files pin.

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
default — `uv run pytest -m integration tests/test_search_fanout_recall_parity.py`)
compares the model-grouped batched cross-corpus fan-out against the pre-fix
one-call-per-collection fan-out (loaded standalone via `git show <pre-fix
SHA>:src/nexus/search_engine.py`) across 10 real queries spanning all five
corpus specs, asserting top-10 id-set Jaccard overlap ≥ 0.9 per query against
this box's own live cloud collections. Reads only — `search_cross_corpus`
never writes — and self-skips on a named content precondition (this tenant
missing real collections for one of the five corpus specs) rather than
passing vacuously. Run it by hand after any change to
`nexus.search_engine.search_cross_corpus`'s batching, grouping, or per-batch
sizing logic. CI's affected-integration job selects it when it changes, but it
skips there (a hosted runner has no live cloud config), so in practice it runs
only by hand.

The bounded-retry decision that separates a real regression from measured
ANN/embedding-index jitter (`_is_confirmed_regression`) is a pure function
with its own fast, non-`integration` unit coverage in
`tests/test_search_fanout_recall_parity_retry_logic.py` — run in the default
loop, so a revert of the "a second failure fails" contract (accepting
whichever of two measurements came back last, win or lose) is caught without
needing live-cloud credentials.

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
  `pytest tests/ --splits 6 --group N` (pytest-split); its
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
warning naming that test and does not change the exit status, so nothing gates it: join it inside
the test.

Tests marked `integration`, `slow` or `lint` keep a tenant per test: the default selection
deselects them, so no census has said which of them touch storage.

- `NX_TEST_T2_SUBSTRATE=per-test` (older spelling `engine`) is the escape hatch: a tenant
  for every test, the guard off. Kept for one release; `none` still provisions nothing.
- `NX_STORAGE_TOUCH_GUARD_MODE=report` prints what would fail without failing it.
  `NX_STORAGE_TOUCH_SWEEP=1` adds the whole-run shape checks (some test provisioned, some clean,
  some touch event) and marks the report as a whole-run one for the ratchet below; set it on a
  full-suite run, not a scoped one.
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
  prints the number to commit. **No CI leg runs the fan-in yet, and both ceilings are still `"measure"`**:
  a full chas run with `NX_STORAGE_TOUCH_SWEEP=1` supplies the number, and exporting SWEEP on the
  shard legs is nexus-0ite2 item 3. Marks only get added, so when the count rises, drop marks a test
  no longer needs before raising the ceiling.
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

## The per-worker tenant pool (nexus-fpwj1, `NX_TEST_TENANT_POOL`, default OFF)

`NX_TEST_TENANT_POOL=1` makes `t2_service_env` / `t2_tenant` lease one lazily minted tenant per xdist worker
(`tests/_tenant_pool.py`) instead of minting and dropping one per test. Measured on 13 storage-touching files at `-n 4`
(767 tests): DDL 41,968 -> 576, WAL 268 MB -> 15 MB, 373 leases on 4 mints, 0 retired. The flag is exactly `1`; unset
and `0` are off, and with it off nothing about the tenant fixtures changes.

A lease ends with four steps, in this order: (1) one psql transaction deletes the tenant's rows from every tenant-keyed
relation (derived from the catalog at call time; token rows included; DML only, no DDL, no `TRUNCATE`); (2) `POST
/v1/_test/tenant-reset` evicts every JVM cache keyed by the tenant (nexus-0bl77; the substrate engine runs with
`NX_TEST_TENANT_RESET_ROUTE=1` and `NX_SEARCH_PCI=0`, because the route answers 503 while per-collection index builds,
which are DDL, are on, and the engine's default for an unset `NX_SEARCH_PCI` is on); (3) a census proves zero rows in at
least 40 relations including a required set, and zero `pci_` indexes; (4) `POST /v1/tenants/create` for the same name
re-issues a bearer, so the old one answers 401 and the Python `(url, token)`-keyed slots rebuild by construction. A step
that fails ERRORS the test that leaked, naming the relations, and retires the tenant (counted). Python caches keyed by
collection name (`search_engine._poisoned_collections`, `aspect_extractor._ENGINE_ASPECTS_ENABLED_CACHE`, the vector
client singleton and lease cache) are cleared on every lease.

- **Opt out with `@pytest.mark.fresh_tenant`** a test that mints or drops tenants, rotates or revokes the tenant's own
  token, or needs a tenant whose partition leaves have never held data. The tenant it gets is the old per-test mint.
- A test that leaks rows after its last assertion (a background thread, a daemon) shows up as the next lease's census
  error or the end-of-session census; fix the writer, never the census.
- The summary ends with `tenant pool: leases=N mints=M retired=R fresh_tenant=K workers=W`; a pool tenant that is not
  empty at the end, or tenant tests that took no lease, fail the run. `tests/test_tenant_pool.py` drives each behaviour
  against the real engine, each with a control that skips the eviction and must show the stale state;
  `tests/test_tenant_pool_end_to_end.py` runs the real conftest on and off.
- Flipping the default is nexus-7zloy (the A/B full run on chas), not part of the pool.

## Bytes written and DDL per run (nexus-sz651)

The primary gated signal is the **DDL count per test** (exact, the same on every host, and the lever that
explains the writes); the WAL bytes are a churn proxy for disk thrashing, not a disk meter. Every run with the engine substrate prints
`ddl: migration=<n> tests=<n> per_test=<x> wal: migration=<size> tests=<size> per_test=<size>
[io_wbytes=<size>] | <n> uncounted cluster start sites (static tree census)` in the terminal summary and writes `nx-ddl-report.json`
(under `NX_DDL_REPORT_DIR`, else the controller's basetemp; a worker's own file is under
`<basetemp>/popen-gwN`). WAL bytes are `pg_current_wal_insert_lsn()` differences between the probe's
`start`, `ready` and `end` marks (insert, not the write pointer `pg_current_wal_lsn()`, which lags
by megabytes under `synchronous_commit=off`), so `migration` is the engine's boot and `tests` is
everything the tests caused. **WAL is a relative, lower-bound churn proxy, not what a disk-backed
cluster would write, and on Linux it lands on tmpfs and never reaches the NVMe**: the clusters run `fsync=off` and `full_page_writes=off` (no full-page images),
the Python substrate also `wal_level=minimal` (WAL-skipped bulk operations log nothing), so Python
and Java WAL are not comparable with each other. Clusters sit on tmpfs on Linux with headroom, else
the tempdir. `io_wbytes` is the job cgroup's `io.stat` `wbytes` over the pytest window where
readable (Linux, cgroup v2); it is informational, not ratcheted (the fan-in sums it over the shards and
prints it), and means something only when the run owns its cgroup. **Not measured**: the job workspace and checkout, `target/`, `.venv`, jar
builds, journald, anything before pytest starts, and the docker-run and e2e shell clusters. The
labrun NVMe sampler is the source of truth for bytes written to disk. There is no per-test WAL figure
in Python (an LSN read at every test boundary is a round trip per test); the per-test number is the
tests' bytes over the test count. `tests/_ddl_counter.py` explains the DDL signals
(`log_statement=ddl` for client statements, an event trigger for the DDL a function runs through
`EXECUTE`, which is every tenant mint and drop) and the counting rule. The probe's own install is
the `boot` bucket and is never counted. The Java twin is `DdlLogParser`/`DdlCounts` (per-fork files
in `service/target/ddl-counts/`, named by suite and run; per class bytes where the class is known).
The Java counter's DDL log never goes through Docker's log driver (json-file logs sit under
`/var/lib/docker` on the host disk: with the log on stderr the main suite wrote 1.63 GB against 0.48 GB
without the counter). The engine test containers run `logging_collector=on` and write one unrotated
file, `PgContainerHelper.SERVER_LOG_FILE` (`<PGDATA>/nx-log/postgresql.log`, on the data directory's
tmpfs), and everything that reads the server log (`DdlCounts`, the `auto_explain` readers) calls
`PgContainerHelper.serverLog(container)`, which `cat`s that file; `container.getLogs()` is Docker's log and
holds only the postmaster's first lines. Because the collector also takes the "ready to accept connections"
line off Docker's log, the container waits on a `pg_isready` TCP probe. `PgServerLogTest` pins all three.

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
**The Python fan-in is `--advisory` too until it is armed**: the python ceilings
were estimated or measured off CI (`wal_bytes_per_test` is a cross-OS estimate; the same tests wrote 1.8x
more WAL on chas than on the laptop), so an enforced fan-in could turn develop red on a number nobody
measured there, after the whole shard matrix, with no way to re-run it green. `[python_unit] measured_on`
in `tests/ddl_ceiling.toml` is the marker; the wiring lint keeps `--advisory` (and `continue-on-error` on
the fan-in's checkout, download and run, and on each shard's advisory check and report upload) exactly
while it is absent. **To arm it, in one commit:** paste the
first Linux fan-in line (the `pytest-gate` log of a develop run) into nexus-ekp7w, set the four ceilings from
it, add `measured_on = "<ci run url or sha>"`, and drop `--advisory` and the three `continue-on-error`
lines from the fan-in steps in `ci.yml`, plus the `continue-on-error` line on the two per-shard steps in
the `test` job. The fan-in prints the whole comparison either way.
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

The counter turns fully off for a same-sha A/B (nexus-3cwsw): `-Dnexus.ddl.count=false` for Java,
`NX_DDL_COUNT=0` for pytest (default on; any other value fails loud). Off adds none of the counter's
settings to a container or cluster (no `log_statement`, collector, probe, event trigger, marks, WAL
reads), uses the library's default ready wait, and writes no count file or report; on Java the server
log is then Docker's log again, so `PgContainerHelper.serverLog` falls back to `getLogs()`.
`DdlCountSwitchTest` pins the off recipe (command line, probe copy, ready wait), an off container's boot,
the empty-database probe, and that an off run leaves `DdlCounts`' live set and totals exactly as they were
after `register`, `noteDatabase`, `noteRelease`, `record` and `flush` (`stateForTest` reads them back; each
guard was removed once and the test failed). Two things carry no guard of their own and are not pinned
separately: `markEnd` (only `record` reaches it) and `FailFastPostgreSQLContainer.start()` (its `register`
call checks the switch). `test_ddl_counter.py` pins the Python half (an AST check that every probe use in
the substrate sits under `ddl_count_enabled()`). A ratchet run has nothing to read from an off run, by design.
A Java test that asserts on a counter-only surface (the collector file, the probe, marks, WAL reads, the
parser on a live log) starts with `assumeTrue(DdlCounts.enabled(), ...)` so an off run is green with those
skipped; `tests/test_java_counter_tests_assume_lint.py` (lint) fails when such a `@Test` lacks it. The scan is
textual and keyed on a list of surface names in that file, so a test that reaches the counter through a name
not on the list is not caught: extend the list when a new counter-only surface appears.

**Same-sha A/B (nexus-3cwsw).** Is the counter what writes to the host disk? Measure it with the sampler,
`scripts/diag/container_io_sampler.sh` (no root; its header states what the numbers are not). Run on chas
when the pool is quiet: the daemon and device deltas are host-wide, so a CI job running beside the arm
lands in them. Take an idle window first (`scripts/diag/container_io_sampler.sh -i 5 -- sleep 600`), then
the arms in the order on, off, off, on (two off runs give the noise floor). Each arm gets its own run id,
exported, because the container filter is the run-id label and an unset id would match every nexus test
container with an empty id (the sampler refuses an empty filter value):

```bash
# arm "on"; for "off" use a different id and -Dnexus.ddl.count=false
export NX_TEST_RUN_ID=3cwsw-on-$(date +%s)
scripts/diag/container_io_sampler.sh -i 1 -f "label=dev.nexus.test.run=$NX_TEST_RUN_ID" -- \
  scripts/mvnw-leased.sh --lab-run lab-xl -Dnexus.ddl.count=true test
# the second suite, same arm and id scheme: add -Dgroups=integration
```

The sampler exits 3 (and warns) when `-f` matched no container, so a wrong id cannot pass as a measurement.
An off run writes no count files, so do not run the ratchet on it; an on run's files in
`service/target/ddl-counts/` persist into the next arm unless the directory is cleared or the ratchet gets
`--run`. What to read in the report: `containers seen` (a lower bound; `docker events --filter
label=dev.nexus.test.run=$NX_TEST_RUN_ID --filter event=start` counts every start); the daemon rows
(dockerd, containerd, systemd-journal) and the json-log total, each on minus off, against the idle window
and the off/off spread; the device row and `unattributed = device - attributed`, because cgroup `wbytes`
omit the filesystem's journal and metadata and writeback charged to no sampled cgroup, so the sampled sum
can sit far below the device; the `docker (driver, logging driver, debug, cgroup version)` line, which must
match across arms. Measured on chas (2026-10-19, Java main, two arms each): the counter costs nothing
measurable. Per-container sum 64-72 KiB in every arm, 17-18 containers a run. containerd 49/47 MiB on vs
45/46 off; dockerd 18/16 on vs 27/27 off (on is lower: Docker's json log shrinks from 4.2 MB to 30 KB);
systemd-journald 662/436 on vs 690/407 off, host noise that does not follow the counter, written against
about 2 MB of journal entries (ssh login and veth/network churn, present at idle), so journald's own write
amplification dominates. The lab owner's nvme0n1 sampler gave 0.84 / 0.66 / 0.51 / 0.52 GB for on / off /
off / on, so the on arms sit at both ends of the off spread. That refutes the hypothesis the bead opened on,
that copying the probe script into each container was the writer: a run starts only 17-18 containers, their
sum is 64-72 KiB, and containerd writes the same on and off. The writer was journald itself; the lab owner
moved it to RAM on 2026-10-19 (Storage=volatile), so a later A/B should show a near-zero journald row. The
switch stays so that any later change to the counter can be A/B'd at the same sha. Diagnosing a stall: `BOUNDS THAT EXPIRED` in the report lists the docker calls and the loop that
were cut off.

Clusters other than the substrate's are not counted. `tests/ddl_uncounted_clusters.toml` lists the
`pg_ctl_start` / `initdb` sites a static scan of `tests/` and `scripts/` finds (plus the jOOQ code
generator); docker-run and e2e shell clusters are not scanned, so the list is not every start site.
The terminal line prints the total as `<n> uncounted cluster start sites (static tree census)`: a
census of call sites, the same on every run whichever tests ran, unweighted by size.
`tests/test_ddl_uncounted_clusters_lint.py` keeps the list equal to the scan, so a new start site
must be read through the counter or listed on purpose.

The Java report has five buckets: `boot` (before the container's `start` mark; the probe's install),
`shared_migration` (the per-fork template), `shared_tests` (clones, the tests' own DDL, and each fork's owner-template build, about 2.3k statements, attributed to the first class in that fork that takes an owner clone),
`shared_empty` (everything logged against a database from `startEmptyDatabase`, the
migration-process classes' own walks; nexus-7xfh7) and `dedicated` (a container of its own). A
`template0` database has no event trigger, so `acquireEmptyDatabase` installs the probe into each one
inside a `DO` block, which the count does not see.

Which Java test classes may build their own schema is a checked table, not a habit
(nexus-0ty8z). A schema-only class takes a clone of the migrated template: `PgContainerHelper.start()`,
or `startMigratedByOwner()` for production's ownership shape (a non-superuser owner of every relation,
cloned from a second template built once per fork). A class that calls `startEmptyDatabase`,
`startDedicated` or `newContainer` is listed in
`service/src/test/resources/db-start-classification.tsv` with a kind and a reason, and
`DbStartClassificationGateTest` fails on an unlisted caller, a stale row or a kind that does not fit the
call (a direct call or a method reference). The gate checks that a row exists and that its kind is in the
vocabulary allowed for that call; a kind is a label and a reason is free text, neither is checked against what
the class does, so the table forces a decision and a place to review it, not a proof of it. The first
`PgContainerHelper.applyProductSchema` on a clone made by `start()` is a no-op, because the template already
holds the walk, its runAlways grants and the `nexus_test` objects; a second call, an empty database, an
owner-template clone or `-Dnexus.test.pg.shared=false` runs the changelog. The skip is "first call per clone",
not "nothing touched yet": a class that changes the clone and then calls `applyProductSchema` to heal it is
skipped on that call. `PristineCloneSkipTest` pins the premise (a pristine clone's catalog equals the same
clone after a real second apply) and the edges.

## Seeding a chunk that has no owner (RDR-223 Phase 3)

The engine refuses a write to `/v1/vectors/upsert-chunks`, `/store-put` and
`/upsert-reference-only` whose chashes have no live manifest row in the
collection. A test that needs such a chunk (an orphan on purpose: the RDR-192
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
Java engine tests use `PgContainerHelper.insertChunks` (typed jOOQ), which is a
looser twin, NOT the same contract: it runs as the connection's role (the
superuser in the existing callers, so RLS does not apply), leaves an existing
`(tenant, collection, chash)` alone (`ON CONFLICT DO NOTHING`, no text/vector
replace, no metadata merge, no `retention` reset, no `last_written_at` restamp),
and picks the embedding column from the vector's width rather than the
collection's registered model. A Java test that re-seeds a chash must not rely on
the conflict behavior. A test whose SUBJECT
is the route itself (deadline mapping, embed errors, the `{"upserted": N}`
envelope) cannot move; it asserts the refusal or pre-owns its chashes instead.
`PgContainerHelper.insertOwnedChunks(ctx, tenant, collection, dim, chashes...)` is the
pre-own form: a zero vector of the collection's width, a live owner, and the test then
POSTs with `force_re_embed` when it needs the embedder reached (an owned chash with a
stored vector skips it otherwise). nexus-z0o2p.24 (P3.2) landed the refusal and settled the route-subject tests:
`OwnerlessWriteRefusalTest` pins the refusal (both chunk-write routes, the `force_re_embed` and
supplied-vector branches, the order of the three 4xx checks, log-only mode, the `/v1/status` counters);
`VectorHandlerUpsertReferenceOnlyTest` now asserts the retired route answers 410, and the engine has no writer of
reference-only rows at all (nexus-z0o2p.36 deleted `upsertReferenceOnlyChunk`; `ReferenceOnlyWriterAbsentScanTest` pins
its absence from main): a test that needs one builds it with `PgContainerHelper.insertReferenceOnlyChunk`;
the parity tests in `tests/test_chunk_seed.py` compare the route's CONFLICT write against the helper
(the route refuses a first write); and `tests/test_z0o2p24_reembed_concurrent_supersede.py` pins the
`nx collection re-embed` answer to a chunk that loses its owner mid-run. A test that needs the engine
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
below). Port shards scale with the xdist worker count, so `-n auto` fits any
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
2. **Explicit `--no-autostart`** at the host-side sites (`fresh-install-mvv.sh`).

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
