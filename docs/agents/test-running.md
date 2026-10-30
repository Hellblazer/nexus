# Test running details

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); AGENTS.md keeps only the per-turn rules and links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

`-n auto` is capped automatically to what this machine's SysV shared-memory
budget can support (nexus-6qp25). Each worker boots its own Postgres
substrate, each substrate holds one segment against `kern.sysv.shmmni`, and
that budget is shared with every other cluster on the box — a running gate, a
peer's suite, a cluster orphaned by a killed battery. Past the limit `initdb`
fails and every substrate-backed test errors at SETUP, so the run reports
thousands of setup errors that read as catastrophic breakage rather than as
resource contention. The cap binds only when headroom is genuinely low and
prints the reason when it does; `NX_XDIST_NO_CAP=1` opts out. If you see that
wall of setup errors anyway, look at `ipcs -m` before you look at the diff.

Unit tests use the in-process `InMemoryVectorClient` (`nexus.db.inmemory_vector_store`) + bundled ONNX MiniLM — no API keys or network; engine-substrate tests self-provision a local service (`ensure_engine`/`mint_test_tenant` in `tests/_engine_substrate.py`) or skip. **After any pull/rebase or edit touching `service/` (a comment in a Java file or a changelog XML counts: the gate is mtime-keyed), run `scripts/build-gate-jar.sh`** — the substrate's freshness gate rejects a stale jar, lint-marked tests included (tests marked `needs_stamped_jar` also skip on an unstamped one), and a session start now refuses on it (exit 75, one line naming `scripts/build-gate-jar.sh`; the same preflight refuses when the pinned engine tag's PG bundle is not downloadable, and `NX_TEST_T2_SUBSTRATE=none` skips it). A jar rebuilt DURING a run is a different case: the preflight has already passed, so that run still reports a wall of setup errors that is not a test failure. Never backdate mtimes to get past it; rebuild. **A test gets a tenant only when it asks** (a `t2_tenant`/`t2_service_env` parameter or `@pytest.mark.t2_tenant`); one that reaches storage without it fails at teardown naming itself, `NX_TEST_T2_SUBSTRATE=none` provisions nothing, and the `per-test` hatch is retired (nexus-j23ni, nexus-llo7w; [`tests/AGENTS.md`](../../tests/AGENTS.md) § The tenant is opt-in). Test-authoring directives (scenario journeys, lint bucket, contract-suite patterns, parametrize rules) live in [`tests/AGENTS.md`](../../tests/AGENTS.md).

## Hand runs and develop CI

nexus-clmlb (labrun RDR-001 lever 3, approved by Sam 2026-10-18). In the week of 2026-10-11 to 10-18, 91 hand full-suite runs through `scripts/watched_pytest.py` took about 1,108 GB-h, two thirds of the lab pool's hand memory-hours; 53 of them ran 10+ minutes on 16+ CPUs, mostly repeating develop CI on the same commit, and hand `lab-l` grants are the longest wait agents see (p90 about 17 minutes).

1. **Iterate on a selection.** Run the tests of the modules you changed (a path list, a `-k` expression or a `-m` marker) on chas: `lab-run -c lab-m -- python3 scripts/watched_pytest.py --stall 180 --status <file> -- uv run pytest <paths>`. `watched_pytest.py` prints a one-line hint when its command selects nothing. Name the status file `<something>status<something>.json` at the worktree root, or put fetched output under `lab-out/`: git ignores both, so a commit that builds its paths from `git status` cannot take them along (nexus-0asrr).
2. **One green full run per tree.** A green full run of a tree (the same sha, clean) is not repeated by anyone, whoever ran it: an implementer hands on the sha and the status file, and the reviewer or orchestrator reads them instead of rerunning. Once the commit is on develop, its CI run is the verdict (`gh api repos/Hellblazer/nexus/commits/<sha>/check-runs`): every develop push runs the Python shards and the lint bucket, and a push touching `service/**` also runs Service CI's Java job. An agent that cannot see CI asks the session that pushed instead of rerunning. A review fix round reruns its reds plus the tripwire for its change class; a round that touches only a test or a doc file does not trigger a full run.
3. **A hand full suite is announced with its reason**, in the announcement to the pool owner, carrying three things: the change class it is for and the run that class requires (the full Python suite for a change under `src/nexus/` or `service/`, the full Java suite and integration group for an engine change, the lint bucket for a new test file: this pre-push practice is unchanged, because CI runs only after the push); the tree sha; and that no green full run of that tree exists. "Pre-push" alone is not a reason. The other reasons are a selection CI cannot run (a Linux-only test from a Mac, a host-specific gate) and a flake being bisected. When two sessions stack commits, the last to push owns one full run on the combined tree. The release battery is unaffected.
4. **Where.** Full Python suites run on chas inside a `lab-l` grant through `watched_pytest.py`, with the gate jar built first ([self-hosted-runners.md](self-hosted-runners.md)); `-n auto` inside a grant sees the grant's cores. Engine builds and Java suites go through `scripts/mvnw-leased.sh --lab-run <class>` (or `scripts/build-gate-jar.sh --lab-run <class>` for the gate jar), never wrapped in `lab-run`. Inside a grant the worker count is also clamped to the grant's memory at about 3.2 GiB a worker, a full suite's measured peak (`tests/_xdist_cap.py`): lab-s 1, lab-m 5, lab-6c18g 5, lab-l 15, lab-mac-s 1, lab-mac-m 3.
5. **A refused or queued full run is logged**, per user per host, to `${XDG_STATE_HOME:-$HOME/.local/state}/nexus/lease-events.log` (`NX_LEASE_EVENTS_LOG` for a shared path); fields in [engine-service-release.md](engine-service-release.md) (the paragraph on the `suite` lease).
6. **A second substrate-heavy run queues; on a Mac laptop the conftest enforces `lab-remote`** (nexus-tvt6u). The suite lease no longer refuses a second run by default: it waits up to 30 minutes for the holder, prints the holder and the time waited once a minute (which also keeps `watched_pytest.py`'s stall clock from killing it), then runs; a wait that runs out exits 75 naming the holder. `NX_SUITE_LEASE_WAIT=0` keeps the immediate exit 75. Inside a lab-run grant (`LAB_CPUS`/`LAB_MEM_MB`) on Linux the lease steps aside and lab-run is the only queue; it admits by CPU and memory, so it does not keep two `lab-l` suites apart on chas. A macOS `lab-mac` grant is admission only: the lease is still taken there (`tests/e2e/macos-guest/host.sh` reads it). On macOS with no grant, `tests/conftest.py` `_refuse_heavy_laptop_run` now enforces the global rule that any `-n` run is heavy: an xdist run with more than one effective worker whose `NX_TEST_T2_SUBSTRATE` is not `none` (the engine can boot) exits 75 naming `lab-remote -c lab-l -- uv run pytest <the same args>`. `NX_LOCAL_HEAVY=1` opts out (a stderr line says so) for a run that needs claude, bd or the laptop's credentials; a serial or `-n 1` run and `NX_TEST_T2_SUBSTRATE=none` are not refused. `tests/e2e/release-preflight.sh` reports a pytest leg stopped by the lease as `suite lease held by <holder>`.

labrun measures the change a week after it lands: `kind=hand` full-suite lines in chas and qwent `/var/log/lab-runs.log` whose commit already had a finished develop CI run, and full-suite runs on a sha that already had a green hand full run.

## Test substrate, guard and ratchet mechanics

Moved from `tests/AGENTS.md` on 2026-10-24 (docs tidy, agents guidance) so that file keeps the rules a test author obeys; the text below is the mechanism, measurements and procedures behind them, unchanged where kept; parts that a module docstring already carried were dropped in favour of it. The module docstrings (`tests/_xdist_cap.py`, `tests/_engine_substrate.py`, `tests/_tmp_root.py`, `tests/_tenant_pool.py`, `tests/_storage_touch_guard.py`, `tests/_ddl_counter.py`) are the first source for each.

### Worker caps

**`-n` is also capped to the run's memory grant** (nexus-vo9qn,
`tests/_xdist_cap.py`): workers <= min(`LAB_MEM_MB`, cgroup `memory.max`) / 3264
MiB (about 3.2 GiB). That is the measured per-worker peak of a FULL Python suite
(engine JVM + Postgres cluster + the cluster's tmpfs pages, charged to the
grant): lab-l, 16 workers peaked 47,795 MiB of the 49,152 MiB grant, 1,357 MiB
from exit 137 (2026-10-21, labrun sampler). So `-n auto` runs 15 on lab-l and 5 on lab-m; the clamp prints its
reason. Nothing applies without a
visible limit (a laptop). `NX_XDIST_NO_CAP=1` opts out of both caps. The cost on
lab-m is real: `-n auto` goes from 8 workers to 5 (37% less parallelism, a longer
wall clock), because 8 at 14.5 of 16 GB left no room for the run to stay on tmpfs.
Both caps rewrite xdist's `option.tx` as well as `numprocesses`
(`set_worker_count`): xdist builds `tx` before conftest loads, so setting the
count alone prints "clamped" and still spawns every worker (the SysV cap was inert
this way until nexus-vo9qn; `TestClampsReallyReduceWorkers` runs real xdist).


Measured on a 16-core box: one substrate cluster is one 56-byte SysV segment, and the full suite at `-n 16` peaks at 20 of 32 segments and passes, so the shared-memory cap does nothing on a quiet machine and binds only when something else has already taken the budget.

### Where substrate clusters live

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
`SubstrateRootExhausted`; a start within 120 s of such a raise in the same process
looks once and raises at once instead of waiting again (nexus-gsjzh) (see "If `SubstrateRootExhausted` trips" in `tests/AGENTS.md`). The start
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

### Integration clusters

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

### Storage-touch sweep

- `NX_STORAGE_TOUCH_GUARD_MODE=report` prints what would fail without failing it.
  `NX_STORAGE_TOUCH_SWEEP=1` adds the whole-run shape checks (some test provisioned, some clean,
  some touch event, and Python children that were spawned reached the shim: `children_seen` must be
  non-zero when `children_spawned` is) and marks the report as a whole-run one for the ratchet (the "saving is ratcheted" rule in `tests/AGENTS.md`);
  set it on a full-suite run, not a scoped one. A run that pytest-split is splitting (`--splits`: a CI
  shard, whatever its size) or that collects at least `SWEEP_AUTO_FLOOR` (4000,
  `tests/_storage_touch_guard.py`) tests asks them by itself; the docs-readers leg (1561, not split)
  and any one-directory run do not; `NX_STORAGE_TOUCH_SWEEP_FLOOR=<n>` moves the floor. **An automatic
  run is advisory**: whatever the shape checks or the late-touch check find is a yellow warning with
  the test names (and a `warnings` entry in the report), never a `vacuity` entry, so it cannot change
  the exit status; its report still says `sweep: true` so the fan-in accepts it. Only an explicit
  `NX_STORAGE_TOUCH_SWEEP=1` run fails on them, late touch included. **The CI shards export it**
  (nexus-ey246), so on CI each shard gates; a split run outside CI is advisory. The late touches seen
  before (5 on a hellmini full run, 2026-10-19; 1 per CI run until 2026-10-20 15:40Z) had deterministic
  causes, since fixed. **Downgrade trigger:** the first shard red on a late touch that passes on a rerun
  with no code change moves late touches back to a warning (`_vacuity` in the guard, one line); the
  shape, children-seen and guard-error checks keep gating. A scoped run whose Python children never
  reached the shim prints a yellow warning instead of a verdict.

### Tenant pool

By default `t2_service_env` / `t2_tenant` lease one lazily minted tenant per xdist worker (`tests/_tenant_pool.py`)
instead of minting and dropping one per test. While the pool is on, the substrate engine boots with
`NX_TEST_TENANT_RESET_ROUTE=1`, `NX_SEARCH_PCI=0` and `NX_REAPER_ENABLED=false`; the reaper's first pass (60 s after
boot) writes `nexus.gc_audit` rows per tenant, and one landing between a lease's wipe and its census failed the census.
`NX_TEST_TENANT_POOL=0` turns the pool off, and the engine is then exactly the one develop boots; unset and `1` are on,
and any other value is on too. Measured on 13 storage-touching files at `-n 4` (767 tests): DDL 41,968 -> 576, WAL
268 MB -> 15 MB, 373 leases on 4 mints, 0 retired. The default-flip A/B on chas (3+3 interleaved full runs, 16
workers): wall off 1125/1097/1091 s vs on 1109/1084/1095 s, suite `io_wbytes` 817 MB -> 9.4 MB, DDL 574,525 ->
30,287, test WAL 6.6 GB -> 0.5 GB. A test that needs per-collection index builds or the engine reaper to run belongs
under `@pytest.mark.fresh_tenant` plus its own engine, or under `NX_TEST_TENANT_POOL=0`; none does today.

A lease ends with four steps, in this order: (1) one psql transaction deletes the tenant's rows from every tenant-keyed
relation (derived from the catalog at call time; token rows included; DML only, no DDL, no `TRUNCATE`); (2) `POST
/v1/_test/tenant-reset` evicts every JVM cache keyed by the tenant (nexus-0bl77; the route answers 503 while per-collection index builds, which
are DDL, are on, and the engine's default for an unset `NX_SEARCH_PCI` is on, hence the pool-on flags above); (3) a
census proves zero rows in at least 40 relations including a required set, and zero `pci_` indexes. It runs AFTER the
wipe in the same teardown, so it proves the wipe completed and the tenant was quiet through the teardown window; it
cannot see rows a test left before the wipe, because the wipe deletes them; (4) `POST /v1/tenants/create` for the same name
re-issues a bearer, so the old one answers 401 and the Python `(url, token)`-keyed slots rebuild by construction. ANY
failure of a step (a leak verdict, an httpx error, a psql timeout, a bad re-issue reply) retires the tenant (counted; the
next lease mints a fresh one) and errors the test whose teardown failed, naming the cause; a body's own exception is
never replaced. A lease also STARTS with a census, before the test body: a row there was committed after the previous
lease's teardown census, and the NEW test's setup errors naming the previous lease's test as the likely leaker, then the
tenant is retired. One lease at a time is enforced: re-entry raises `TenantLeaseBusyError`. Python caches keyed by
collection name (`search_engine._poisoned_collections`, `aspect_extractor._ENGINE_ASPECTS_ENABLED_CACHE`, the vector
client singleton and lease cache) are cleared on every lease.

### DDL counter, Java side

The primary gated signal is the **DDL count per test** (exact, the same on every host, and the lever that
explains the writes); the WAL bytes are a churn proxy for disk thrashing, not a disk meter. Every run with the engine substrate prints
`ddl: migration=<n> tests=<n> per_test=<x> wal: migration=<size> tests=<size> per_test=<size>
[io_wbytes=<size>] | <n> uncounted cluster start sites (static tree census) + <m> product-provisioning commands (sh_nx_init, counted apart)` in the terminal summary and writes `nx-ddl-report.json`
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
line off Docker's log, the container waits on a `pg_isready -h 127.0.0.1` TCP probe (60 s a start attempt).
`PgServerLogTest` pins all three. A `docker exec` that never returns (seen once on colima, cause not
established) is abandoned after 60 s and retried, 3 tries; each abandon is a WARN
(`event=server_log_read_abandoned`) naming `owner` (the class that BOOTED the container; `unknown` for a
handle) and `reader` (the test class on the calling thread; `unknown` for a read from the JVM-exit hook, whose thread has no test frame). That bounds the caller. What becomes of the stuck
exec depends on where it hangs, and which one happened on colima is not established: if it is parked in
docker-java's `awaitCompletion` waiting for its stream, `Future.cancel(true)` wakes it and its `finally`
closes the stream; if the hang is inside the synchronous execCreate/inspect HTTP call, the reader thread stays
blocked until the JVM exits. A start whose wait times out logs the last 50 lines of the log file at ERROR
(Testcontainers' own dump is Docker's log and lacks them). That tail is readable only while the container is
still up (a slow initdb, a hung probe): a postmaster that died takes PID 1, the tmpfs and the file with it, and
the line then reads `(server log unreadable: ...)`. The JVM-exit hook reads the still-live containers under
one 15 s budget, inside surefire's 30 s `forkedProcessExitTimeoutInSeconds` (not set in `service/pom.xml`) so
the `log_errors` mark of an abandoned read is flushed before the fork is halted; a read slower than 15 s is
cut and counted as a log error, so the ratchet refuses that run (conservative, by design). A test must not
call `getLogs()` on a Postgres container; `PgContainerTmpfsGateTest` fails the build if one does.


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

### DDL counter A/B

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
45/46 off; dockerd 18/16 on vs 27/27 off (on is lower: Docker's json log shrinks from 4.2 MB to 30 KB, sized every tick; the sampler now sizes an unreadable log through `docker logs` every 30 s, so set `NX_IO_SAMPLER_LOGS_EVERY=1` to compare arms on log bytes);
systemd-journald 662/436 on vs 690/407 off, host noise that does not follow the counter, written against
about 2 MB of journal entries (ssh login and veth/network churn, present at idle), so journald's own write
amplification dominates. The lab owner's nvme0n1 sampler gave 0.84 / 0.66 / 0.51 / 0.52 GB for on / off /
off / on, so the on arms sit at both ends of the off spread. That refutes the hypothesis the bead opened on,
that copying the probe script into each container was the writer: a run starts only 17-18 containers, their
sum is 64-72 KiB, and containerd writes the same on and off. The writer was journald itself; the lab owner
moved it to RAM on 2026-10-19 (Storage=volatile), so a later A/B should show a near-zero journald row. The
switch stays so that any later change to the counter can be A/B'd at the same sha. Diagnosing a stall: `BOUNDS THAT EXPIRED` in the report lists the docker calls and the loop that
were cut off.

### DDL counter, uncounted clusters and Java buckets

Clusters other than the substrate's are not counted. `tests/ddl_uncounted_clusters.toml` lists the
`pg_ctl_start` / `initdb` / `docker_run` (postgres image) sites a static scan of the Python in `tests/`
and `scripts/` finds, the `pg_ctl start` / `initdb` / `docker run` commands in `tests/e2e/**/*.sh` (`sh_*` kinds,
keyed by file and kind with a per-file count), and the jOOQ code generator. The `nx init` commands in those scripts
are product provisioning, not a cluster a test started: they sit in their own `[provisioning]` table and print as a
separate `+ <m> product-provisioning commands` figure, never folded into the start-site number. Not scanned, so the
list is not every start site: shell outside `tests/e2e`, among it files that do start clusters
(`scripts/liquibase_bundle_smoke.sh`, `service/native-smoke.sh`, `tests/containers/fanout.sh`, `tests/cc-validation/*`),
e2e clusters started by anything but a literal `nx init`, and a `docker run` whose argv or image is not a literal
(`tests/_ddl_cluster_sites.py` lists the limits).
The terminal line prints the total as `<n> uncounted cluster start sites (static tree census)`: a
census of call sites, not clusters (one Python cluster usually counts twice: its `<module>::initdb` and its
`pg_instance::pg_ctl_start`), the same on every run whichever tests ran, unweighted by size.
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

### Seeding chunks, Java side

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
`nx collection re-embed` answer to a chunk that loses its owner mid-run.

### Host-run harness write guard

The hazard in `tests/AGENTS.md` § Host-run harness write-time guard: a harness that indexes/stores real content into a service, rather than registering a system unit. Benchmark/gate debris (13 `code__test-repo-<hex>__voyage-code-3__v1` collections, `docs__hotfix_smoke`, `docs__local_smoketest_336`, `knowledge__val530`, a 2,828-doc `docs__1-2188` with no owner — all zero-catalog-doc T3 orphans) landed in the production tenant because nothing at write time kept a host-run harness from writing into whatever service `NX_SERVICE_URL`/`NX_SERVICE_TOKEN` happened to point at. `tests/test_host_harness_scratch_scope_lint.py` mechanically enforces an exact, per-file allowlist of every `nx index`/`nx store`/`nx collection`/`store_put`/`nexus.mcp.core` site across every tracked **shell (`.sh`) AND Python (`.py`)** file under `tests/e2e/**` and `scripts/**` — the scan originally globbed `.sh` only, which missed real in-process MCP-tool call sites entirely (fix-round CRITICAL 1: `scripts/validate/01-mcp-core.py`, `spike_rdr089_delos.py` (under the former `scripts/spikes`), the latter DELETED outright — a spike with a live, unsandboxed `nx index pdf` write path and zero non-docstring references anywhere else in the repo). A nexus-8tnz2 census found every one of the 34 existing sites already safe under one of five shapes (READ-ONLY — including every `operator_*`/`nx_answer` MCP tool, which carry `readOnlyHint` or write only a telemetry row, never catalog/T3 debris — CONTAINER-isolated, `NX_LOCAL` + a sandboxed HOME/config dir (one enforced by an in-file guard, `01-mcp-core.py`'s `_refuse_unless_sandboxed()`, for a file that is independently `__main__`-runnable), the throughput bench's MARKER+SNAPSHOT precedent, or PROSE-ONLY), so there is no shared preamble to source — the allowlist IS the enforcement, not an escape hatch from one. A genuinely new site needing the operator's live service has two conforming routes named in the lint's failure directive: self-provision an engine and mint its own tenant (`POST <engine>/v1/tenants/create` under the boot bearer — the `tests/_engine_substrate.py` `mint_test_tenant` precedent), or the throughput bench's marker-scoped-owner + before/after `nx collection list` snapshot + EXIT-time teardown shape. The lint is deliberately dumb (per-line regex, same philosophy as the sibling lint above) — a descriptive echo/label string that merely mentions one of these verbs still counts as a site needing a reviewed, exact-counted entry. The write-time guard is SCOPED to this repo's own tracked harnesses, not a root-cause fix: the actual live debris population's producer is external and unidentified — see `_WRITE_TIME_GUARDS["tombstone-vanished"]` in `reconcile_stale.py` for that honesty note, and `nx catalog reconcile-stale --execute drop-orphan-collections` for the symptom-level sweep.
