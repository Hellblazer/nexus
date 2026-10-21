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

Unit tests use the in-process `InMemoryVectorClient` (`nexus.db.inmemory_vector_store`) + bundled ONNX MiniLM — no API keys or network; engine-substrate tests self-provision a local service (`ensure_engine`/`mint_test_tenant` in `tests/_engine_substrate.py`) or skip. **After any pull/rebase or edit touching `service/` (a comment in a Java file or a changelog XML counts: the gate is mtime-keyed), run `scripts/build-gate-jar.sh`** — the substrate's freshness gate rejects a stale/unstamped jar, lint-marked tests included, and a session start now refuses on it (exit 75, one line naming `scripts/build-gate-jar.sh`; the same preflight refuses when the pinned engine tag's PG bundle is not downloadable, and `NX_TEST_T2_SUBSTRATE=none` skips it). A jar rebuilt DURING a run is a different case: the preflight has already passed, so that run still reports a wall of setup errors that is not a test failure. Never backdate mtimes to get past it; rebuild. **A test gets a tenant only when it asks** (a `t2_tenant`/`t2_service_env` parameter or `@pytest.mark.t2_tenant`); one that reaches storage without it fails at teardown naming itself, and `NX_TEST_T2_SUBSTRATE=per-test` restores a tenant for every test (nexus-j23ni; [`tests/AGENTS.md`](../../tests/AGENTS.md) § The tenant is opt-in). Test-authoring directives (scenario journeys, lint bucket, contract-suite patterns, parametrize rules) live in [`tests/AGENTS.md`](../../tests/AGENTS.md).

## Hand runs and develop CI

nexus-clmlb (labrun RDR-001 lever 3, approved by Sam 2026-10-18). In the week of 2026-10-11 to 10-18, 91 hand full-suite runs through `scripts/watched_pytest.py` took about 1,108 GB-h, two thirds of the lab pool's hand memory-hours; 53 of them ran 10+ minutes on 16+ CPUs, mostly repeating develop CI on the same commit, and hand `lab-l` grants are the longest wait agents see (p90 about 17 minutes).

1. **Iterate on a selection.** Run the tests of the modules you changed, a path list or a `-k` expression, through `lab-run` on chas (`lab-run -c lab-m -- uv run pytest <paths>`). `scripts/watched_pytest.py` prints a one-line hint when its command selects nothing.
2. **After a push, read CI.** Develop CI runs the whole Python suite, the lint bucket and the Java suite on every push; `gh api repos/Hellblazer/nexus/commits/<sha>/check-runs` is the verdict for that commit. A hand rerun of the same suite on the same commit adds nothing.
3. **A hand full suite has a stated reason**, said where the run is announced: a pre-push run the change class requires (the full Python suite for a change under `src/nexus/` or `service/`, the full Java suite for an engine change, the lint bucket for a new test file: the pre-push practice is unchanged, because CI runs only after the push), a selection CI cannot run (a Linux-only test from a Mac, a host-specific gate), or a flake being bisected. The release battery is unaffected.

labrun measures the change a week after it lands: `kind=hand` full-suite lines in chas and qwent `/var/log/lab-runs.log` whose commit already had a finished develop CI run.
