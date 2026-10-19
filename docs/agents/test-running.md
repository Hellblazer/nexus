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
