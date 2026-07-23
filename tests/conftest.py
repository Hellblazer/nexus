import logging
import os

import pytest
from pathlib import Path

import chromadb
import structlog
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database


def _enable_t2_test_auto_migrate() -> None:
    """RDR-120 P3b: T2Database.__init__ no longer auto-runs migrations
    in production (the daemon owns ``apply_pending``). The test suite
    has hundreds of direct-open call sites that rely on a freshly-
    migrated schema, so we opt the in-process default ON and also
    set the ``NX_T2_AUTO_MIGRATE`` env var so subprocesses
    (``subprocess.run`` / ``claude -p`` / MCP children) that inherit
    ``os.environ`` but not Python module state get the same default.
    Production code paths (CLI, MCP servers) keep the
    daemon-owns-migration semantic; only the test process tree sees
    the flipped default.
    """
    import os

    from nexus.db import t2 as _t2

    _t2._DEFAULT_RUN_MIGRATIONS = True
    os.environ.setdefault(_t2._RUN_MIGRATIONS_ENV, "1")


def _disable_aspect_worker_autostart() -> None:
    """Stop the aspect-extraction-enqueue hook from lazy-spawning the
    singleton polling worker during the unit suite.

    A ``store_put`` / index / MCP test that touches a supported collection
    fires ``aspect_extraction_enqueue_hook``, which (in production)
    auto-spawns the polling worker. The worker then gets stuck mid
    ``t2_index_write`` poll, so the autouse ``_reset_aspect_worker_singleton``
    teardown's ``stop()`` join waits its full 5s timeout — a fixed ~5s tax on
    every such test (≥140s across the suite). The worker is never asserted on
    by those tests, and leaving it unspawned also removes the leaked-singleton
    hazard (nexus-u0u8a) at its root. Worker-specific tests call
    ``ensure_worker_started()`` directly, which ignores this gate, or
    ``monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "1")`` to exercise the
    hook path. ``setdefault`` so an explicit opt-in set before import wins.
    """
    import os

    os.environ.setdefault("NX_ASPECT_WORKER_AUTOSTART", "0")


_enable_t2_test_auto_migrate()
_disable_aspect_worker_autostart()

# RDR-155 P4b P0a': import at collection start so the engine substrate
# resolves PG binaries against the AMBIENT env (per-test fixtures patch
# HOME/NEXUS_CONFIG_DIR before the lazy first ensure_engine() call).
import tests._engine_substrate  # noqa: E402, F401


def pytest_configure(config):
    """Configure structlog level to match pytest's --log-level.

    Default run: WARNING level — quiet, no clutter.
    Validation run: pytest --log-level=DEBUG — full structlog output to stdout.

    Example:
        uv run pytest                          # quiet (WARNING)
        uv run pytest --log-level=DEBUG        # full debug output
    """
    try:
        level_str = (config.getoption("log_level") or "WARNING").upper()
    except (ValueError, AttributeError):
        level_str = "WARNING"
    level = getattr(logging, level_str, logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


# nexus-nifd: prefixes that the indexer's repo cache uses for
# pytest fixture-named test repos. Files at
# ``~/.config/nexus/<prefix>-*-<repo_hash>.cache`` matching one of
# these are evidence that a test bypassed the autouse
# ``_isolate_config_dir`` fixture (e.g. a subprocess that didn't
# inherit ``NEXUS_CONFIG_DIR``, or a test that explicitly
# ``monkeypatch.delenv("NEXUS_CONFIG_DIR")``). Update this list when
# adding a new fixture-named test repo.
_FIXTURE_CACHE_PREFIXES: tuple[str, ...] = (
    "nexus-rich0",
    "nexus-mini0",
    "code-repo",
    "prose-repo",
    "pdf-repo",
    "stage-b-repo",
    "sentinel-repo",
    "test-repo",
    "nx-shakeout-",
)


def _scan_fixture_cache_files() -> set[Path]:
    """Return the set of *.cache files in the REAL ~/.config/nexus/
    whose basename starts with a fixture-cache prefix. Empty when
    the directory doesn't exist.

    Uses Path.home() rather than ``nexus_config_dir()`` to bypass
    any test-time NEXUS_CONFIG_DIR override; the leak we're guarding
    against is precisely tests that hit the REAL config dir.
    """
    real_config = Path.home() / ".config" / "nexus"
    if not real_config.exists():
        return set()
    return {
        p for p in real_config.glob("*.cache")
        if p.name.startswith(_FIXTURE_CACHE_PREFIXES)
    }


_fixture_cache_baseline: set[Path] = set()


def pytest_sessionstart(session):
    """Snapshot fixture cache files in ~/.config/nexus/ at session
    start so ``pytest_sessionfinish`` can detect leaks introduced
    during the session (nexus-nifd).
    """
    global _fixture_cache_baseline
    _fixture_cache_baseline = _scan_fixture_cache_files()


def pytest_sessionfinish(session, exitstatus):
    """nexus-nifd: fail the session when any new test-fixture cache
    file appears in the REAL ~/.config/nexus/ during the session.

    Background: 2026-05-08 prod shakeout found 1,707 leaked
    test-fixture cache files (~121.5 MB) accumulated over weeks.
    The autouse ``_isolate_config_dir`` fixture (PR #601 / nexus-
    mrmq) prevents future leakage for tests that USE it, but a
    test that bypasses the fixture or spawns a subprocess without
    propagating ``NEXUS_CONFIG_DIR`` could re-introduce the leak
    silently. This guard catches that class.

    Best-effort cleanup: any newly-leaked file is unlinked before
    the failure surfaces so the next run starts from a clean
    baseline. The session is still failed so the offending test
    is visible in CI.
    """
    after = _scan_fixture_cache_files()
    leaked = after - _fixture_cache_baseline
    if not leaked:
        return
    # Surface and clean up.
    leaked_sorted = sorted(leaked)
    for path in leaked_sorted:
        try:
            path.unlink()
        except OSError:
            pass
    names = ", ".join(p.name for p in leaked_sorted[:5])
    suffix = "" if len(leaked_sorted) <= 5 else f" (+{len(leaked_sorted) - 5} more)"
    session.exitstatus = 1
    print(
        f"\n\nFAIL: nexus-nifd cache-leak guard caught "
        f"{len(leaked_sorted)} fixture-cache file(s) leaked into "
        f"~/.config/nexus/: {names}{suffix}\n"
        f"  Cause: a test bypassed the autouse `_isolate_config_dir` "
        f"fixture or spawned a subprocess without inheriting "
        f"NEXUS_CONFIG_DIR.\n"
        f"  Cleanup: leaked files removed; failing the session.\n",
        flush=True,
    )


@pytest.fixture(autouse=True)
def _disable_migration_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-0rwwv: pin the substrate-migration bridge probe OFF for the
    whole suite. ``pending_migration_notice`` (called by interactive
    ``nx upgrade`` and default ``nx doctor``) opens the local Chroma read
    leg — and on a lived-in box the isolated test config reads as SQLITE
    mode while the XDG chroma default resolves to the REAL store (the
    immutable post-migration rollback source), which unit tests must never
    open. Tests of the notice itself opt back in with
    ``monkeypatch.setenv("NX_MIGRATION_NOTICE", "1")`` plus a patched
    ``detect_pending_migration``.
    """
    monkeypatch.setenv("NX_MIGRATION_NOTICE", "0")


@pytest.fixture(autouse=True)
def _restore_structlog_after_test():
    """Save and restore structlog config around every test so any test
    that calls ``structlog.configure(...)`` (directly or via
    ``nexus.logging_setup.configure_logging``) does not leak its
    config to downstream tests.

    Background: tests that swap ``logger_factory`` from the default
    ``PrintLoggerFactory`` to ``LoggerFactory(stdlib)`` reroute every
    structlog event from stderr to stdlib logging. ``capsys``-based
    assertions in unrelated tests then read empty strings while the
    event sits in caplog. The originally-affected test was
    ``test_plan_audit_logs_warning_on_clamp``, which fails when run
    after any test that pollutes structlog. Solving it per-file via
    individual autouse fixtures drifted; a global one is cheap and
    closes the door for new tests too.
    """
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture(autouse=True)
def _isolate_claude_code_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the ambient ``CLAUDE_CODE_SESSION_ID`` for every test.

    nexus-36q84: :func:`nexus.session.resolve_active_session_id` gained a
    new tier that reads ``CLAUDE_CODE_SESSION_ID`` (the harness-provided
    per-process env var Claude Code sets natively) between ``NX_SESSION_ID``
    and the ``current_session`` flat-file fallback. Because the unit suite
    itself typically runs *inside* a live Claude Code session (via the Bash
    tool), the real conversation's ``CLAUDE_CODE_SESSION_ID`` is present in
    ``os.environ`` for every subprocess/test-process — exactly the ambient
    pollution class this fixture family exists to close (see
    ``_isolate_t1_sessions`` above). Without
    this, any test exercising the flat-file or ``None`` fallback tiers of
    ``resolve_active_session_id`` would silently resolve to the real
    session id instead of the fixture/file value it asserts against.

    Tests that want to exercise the new tier explicitly
    ``monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ...)`` inside the test
    body, which overrides this fixture's ``delenv`` (later calls on the
    same ``monkeypatch`` win — same pattern documented on
    ``_isolate_config_dir``).
    """
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture(autouse=True)
def _isolate_t1_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force tests onto the explicit-isolation T1 path.

    RDR-105 P4 (nexus-jnx7) collapsed T1 discovery to a single
    four-branch fail-loud gate. With no env vars and no addr file,
    the constructor raises ``T1ServerNotFoundError``. Tests that
    previously relied on the legacy EphemeralClient fallback opt
    in via ``NX_T1_ISOLATED=1`` Path C; this autouse fixture sets
    it process-wide so the suite gets the process-scoped
    ``InMemoryVectorClient`` singleton by default (RDR-155 P4b
    P0a; session_id metadata filtering provides per-test scoping).
    Tests that need a different mode (env-passdown, addr file,
    fail-loud raise) override the env inside the test.
    """
    monkeypatch.setenv("NX_T1_ISOLATED", "1")


@pytest.fixture(autouse=True)
def _pin_mineru_autostart_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suite-wide MinerU autostart kill-switch (nexus-1qdb9).

    ensure_mineru_running() spawns a REAL mineru-api process on demand;
    an unpatched unit test that wanders into the PDF extractor's server
    check must never do that (2026-07-14: one suite run left four stray
    servers). Lifecycle tests that exercise the spawn path re-enable via
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1") + patched spawn core.
    """
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "0")


@pytest.fixture
def t2_service_env(request: pytest.FixtureRequest,
                   monkeypatch: pytest.MonkeyPatch) -> str:
    """Engine-backed T2 substrate env for one test (RDR-155 P4b P0a', D-A).

    Boots the session-scoped hermetic PG + service JAR on first use
    (tests/_engine_substrate.py, memoized) and points this test's env at
    it with a freshly MINTED tenant + tenant-bound token — the engine
    binds tenant to the BEARER server-side (AuthFilter Decision 1; the
    X-Nexus-Tenant header is ignored), so per-test isolation is a
    per-test token. Tests never share or clean up state. Returns the
    tenant name.

    Opt-in during the incremental migration; replaces the sqlite pin
    (set AFTER _pin_storage_backend_sqlite — later setenv wins) and
    becomes the suite default when the pin flips at the end of P0a'.
    """
    from tests._engine_substrate import ensure_engine, mint_test_tenant

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.setenv("NX_SERVICE_URL", state["base_url"])
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    return tenant


@pytest.fixture(autouse=True)
def _pin_storage_backend_sqlite(request: pytest.FixtureRequest,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the unit suite to the SQLite storage backend (RDR-152 nexus-fjwxh).

    FLIP MECHANISM (RDR-155 P4b P0a'): ``NX_TEST_T2_SUBSTRATE=engine``
    routes this autouse pin to the engine-backed substrate instead —
    every test gets the session PG+JAR with a freshly minted tenant
    (exactly what the ``t2_service_env`` opt-in fixture provides). This
    is both the flip dry-run switch (run any subset against the engine
    without editing files) and, when the migration completes, the
    default this fixture body becomes.

    ``storage_backend_for`` defaults to ``service`` since the T2 cutover, so a
    bare ``T2Database(path)`` would construct the Http* stores and try to reach
    the nexus-service — which unit tests neither run nor want. Pinning sqlite
    here keeps the ~116 T2Database-constructing unit tests deterministic and
    independent of ambient service/lease state (a dev box with the supervisor
    running would otherwise auto-discover a real lease mid-unit-test).

    Tests that exercise the resolver itself (``test_storage_mode.py``) carry
    their own ``_clean_storage_env`` autouse fixture that ``delenv``s the
    backend vars AFTER this one, so they still observe the true default. Any
    test that wants service mode sets ``NX_STORAGE_BACKEND[_<store>]`` itself,
    which overrides this pin (later ``setenv`` wins).
    """
    if os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine":
        request.getfixturevalue("t2_service_env")
        return
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")


@pytest.fixture(autouse=True)
def _reset_lease_resolution_history() -> None:
    """Reset ``service_endpoint``'s process-wide "ever resolved a lease"
    signal before AND after every test (nexus-7dsgp, critic round 1
    CRITICAL fix).

    The flag is deliberately process-lifetime in production (see its
    docstring), but a unit-test SESSION is one process shared across
    thousands of tests — without this reset, any earlier test that
    successfully calls ``discover_lease()`` (there are many, e.g. every
    ``_publish_lease``-based test in test_service_endpoint_discovery.py)
    would leave the flag ``True`` for the rest of the run, silently
    making a LATER, unrelated test's construction-time resolution
    failure retry-with-wait (a REAL 12s stall with no fake clock
    injected at most construction-time call sites) instead of the fast
    fail-loud that test actually expects — order-dependent pollution of
    exactly the kind nexus-1091 caught for the T3 side of this bead.
    """
    from nexus.db import service_endpoint

    service_endpoint.reset_lease_resolution_history_for_tests()
    yield
    service_endpoint.reset_lease_resolution_history_for_tests()


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect NEXUS_CONFIG_DIR so child processes write under tmp_path.

    nexus-mrmq: integration tests that dispatch ``claude -p`` subprocesses
    (the operator dispatch path, plan-runner, nx_answer equivalence
    suite) inherit the parent's ``os.environ``. Without this fixture
    the child resolves ``nexus_config_dir()`` to the user's real
    ``~/.config/nexus/`` and writes ``current_session`` /
    ``t1_addr.<claude_pid>`` files there. Reproduced 2026-05-08 during
    4.27.1 shakeout: a transient ``claude_dispatch -p`` subprocess
    rewrote the live MCP's session file and unlinked its addr file
    mid-session.

    Setting ``NEXUS_CONFIG_DIR`` here is read at call time inside
    ``nexus.config.nexus_config_dir()`` and propagates to children
    via ``os.environ`` inheritance, so every spawned subprocess
    (regardless of operator-dispatch mode) writes its config files
    under the per-test tmp dir.

    Tests that need to assert the default path (``Path.home() /
    .config / nexus``) explicitly ``monkeypatch.delenv`` first; that
    still works because this fixture's ``monkeypatch.setenv`` is
    overridden by any later test-local ``setenv`` / ``delenv`` call.

    Path layout mirrors the natural ``~/.config/nexus`` relative
    layout (``tmp_path/.config/nexus``) so per-test fixtures that
    set ``HOME=tmp_path`` and write into ``tmp_path/.config/nexus/``
    (e.g. ``test_scratch_cmd.fake_home``) land at the same path
    ``read_claude_session_id`` resolves to via ``NEXUS_CONFIG_DIR``.

    The directory itself is *not* pre-created — write helpers
    (``write_claude_session_id``, the T1 lease registry, etc.) all do
    ``parents=True, exist_ok=True`` themselves, and tests that
    explicitly call ``mkdir(parents=True)`` without ``exist_ok``
    on the same path would otherwise hit ``FileExistsError``.
    """
    config_dir = tmp_path / ".config" / "nexus"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture(autouse=True)
def _isolate_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect NEXUS_CATALOG_PATH so tests never pollute the real user catalog.

    Without this, integration tests that trigger _catalog_hook() (via index_repo,
    index_markdown, or similar) register documents in the user's live catalog at
    ~/.config/nexus/. Before this fixture landed (RDR-060, 2026-04-08), 64
    orphan ``int-cce-*`` curator owners accumulated from
    ``test_cce_query_retrieves_cce_indexed_markdown`` alone.

    The fixture works because catalog write paths guard on
    ``Catalog.is_initialized(cat_path)`` — the tmp path is never initialised,
    so hooks return early. See ``tests/test_catalog_isolation.py`` for the
    regression tests that lock this behaviour in (nexus-dqr3 / nexus-b34f).
    """
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "test-catalog"))


@pytest.fixture(autouse=True)
def _reset_aspect_worker_singleton() -> None:
    """Reset the module-level aspect_worker singleton around every test.

    nexus-u0u8a: ``aspect_extraction_enqueue_hook`` lazy-spawns a singleton
    daemon-thread worker via ``ensure_worker_started()`` for any
    supported-collection (knowledge__/rdr__/docs__) document hook. Only
    ``test_aspect_worker.py`` / ``test_aspect_drain_protocol.py`` reset it,
    so any OTHER test that fires such a hook leaks the singleton. The leaked
    worker keeps polling ``t2_index_write`` (degraded fallback to
    ``T2Database(default_db_path())``), and when a later test patches
    ``default_db_path`` to its own tmp db the worker claims + ``mark_done``s
    rows out from under that test — the exact mechanism behind the
    ``test_collection_rename`` aspect-cascade canary (debugger verdict
    2026-05-28: 95% repro). Resetting before AND after each test confines a
    spawned worker to its own test so it can never poll a sibling's db.
    """
    from nexus.aspect_worker import reset_worker_for_tests
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


@pytest.fixture(autouse=True)
def _reap_spawned_daemons(tmp_path: Path):
    """nexus-scoo5: reap any T2/T3 daemon a test spawned under its own
    isolated tmp ``NEXUS_CONFIG_DIR``.

    A test that drives a real ``nx upgrade`` (non-``--auto``) reaches
    ``upgrade._cycle_daemon_to_current()``, which shells out to ``nx daemon
    t2 ensure-running`` and spawns a *detached* ``nx daemon t2 start`` bound
    to the per-test config dir. ``subprocess.run`` returns once the daemon
    is up, so the process outlives the test body and lingers as an orphan
    after pytest GCs the tmp dir (observed: three orphan daemons on
    ``garbage-*/test_force0/.config/nexus/memory.db``).

    The autouse ``_isolate_config_dir`` fixture sets ``NEXUS_CONFIG_DIR`` to
    ``tmp_path / ".config" / "nexus"``, so a spawned daemon's discovery file
    lands there. This teardown is the process-level analog of the
    ``pytest_sessionfinish`` cache-file leak guard: it is scoped strictly to
    that per-test tmp path (and double-guarded by a cmdline check in
    ``reap_tmp_daemons``), so it can never signal the user's real daemon,
    whose discovery file lives under ``~/.config/nexus``.

    Best-effort; never raises. Tests that suppress the spawn at source
    (patching ``_cycle_daemon_to_current``) make this a no-op.
    """
    yield
    from tests._daemon_leak_guard import reap_tmp_daemons

    # Scoped to the autouse ``_isolate_config_dir`` default
    # (``tmp_path/.config/nexus``) only. A full-suite sweep confirmed this is
    # leak-free: the ``tests/daemon`` lifecycle tests that spawn real daemons
    # under the ``--config-dir str(tmp_path)`` root form self-clean. Scanning
    # the tmp_path root too would reach the fake discovery files those tests
    # pre-seed (with mocked ``subprocess.run`` / ``os.kill``) and trip their
    # "must not spawn" guards at teardown — cost with no proven benefit.
    try:
        reap_tmp_daemons(tmp_path / ".config" / "nexus")
    except BaseException:  # noqa: BLE001 — teardown guard must never fail a test
        pass


def set_credentials(monkeypatch) -> None:
    """Set required T3/Voyage credential env vars for tests that call _has_credentials().

    Shared helper used by test_doc_indexer.py and test_pdf_subsystem.py to avoid
    duplicating the same four setenv calls across both files.
    """
    monkeypatch.setenv("VOYAGE_API_KEY", "vk_test")
    monkeypatch.setenv("CHROMA_API_KEY", "ck_test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "db")


# RDR-109 Phase 1: cloud-mode opt-in fixture.
#
# Default test mode is local (no API keys, ONNX MiniLM EF). Tests that
# assert cloud-mode behavior — voyage-context-3 / voyage-code-3 embedder
# names, _has_credentials() gated paths, CloudClient routing — opt in via
# this fixture (or class-level
# ``pytestmark = pytest.mark.usefixtures("cloud_mode")``).
#
# The lint test ``test_mode_declarations_are_explicit`` enforces that any
# test function whose source contains ``voyage-(context|code)-3`` either
# depends on ``cloud_mode`` or is listed in ``_MODE_LINT_EXCLUDE`` below.
@pytest.fixture
def cloud_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate cloud mode: set Voyage/Chroma credentials and force
    ``nexus.config.is_local_mode`` to return False.

    Callers that do ``from nexus.config import is_local_mode`` inside a
    function body (the established pattern in this codebase — see all
    callsites under ``src/nexus/``) pick up the patch on next call.
    """
    set_credentials(monkeypatch)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)


# Tests whose source matches the voyage-token regex but legitimately do
# NOT need cloud_mode. Two granularities:
#   * ``_MODE_LINT_EXCLUDE_FILES`` — every test in the file is exempt.
#     Use for files whose tests are uniformly schema / name-shape /
#     canonical-set tests where the voyage token is a label, not a
#     behavior assertion.
#   * ``_MODE_LINT_EXCLUDE_NODEIDS`` — individual ``file.py::test_x``
#     entries for mixed files.
#
# Exclusion reasons fall into:
#   - "canonical-set": tests of ``corpus.canonical_embedding_model``
#     or ``CollectionName`` schema constants; the token is the schema's
#     canonical embedder name, not a behavior assertion.
#   - "string-literal-as-name": the test builds a conformant collection
#     name string and asserts on the *name shape* (RDR-103
#     ``<content_type>__<owner>__<model>__v<n>``), not on the embedder
#     that actually ran. The name is canonical regardless of mode.
#   - "parametrize-label": the voyage token appears only in a
#     ``pytest.mark.parametrize`` data tuple or test id.
#   - "docstring-or-comment": the voyage token appears only in a
#     docstring or comment, not in executable code.
#   - "mode-self-test": the test asserts local-mode behavior itself
#     (``test_local_mode.py``); cloud_mode would invert what it tests.
#
# Files that primarily exercise cloud-mode behavior (real Voyage calls,
# CloudClient routing, ``_has_credentials()`` gated paths) do NOT appear
# here; they declare ``pytestmark = pytest.mark.usefixtures("cloud_mode")``
# at module scope instead. See ``docs/contributing.md`` and
# ``tests/AGENTS.md``.
_MODE_LINT_EXCLUDE_FILES: frozenset[str] = frozenset({
    # Cloud-behavior files — Phase 1 ships the lint mechanism with these
    # excluded; subsequent PRs promote each to module-level
    # ``pytestmark = pytest.mark.usefixtures("cloud_mode")``. Promotion is
    # per-file so each can be validated against the suite independently.
    # The lint test itself contains the regex.
    "test_mode_declarations_are_explicit.py",
    # RDR-109 Phase 2 dispatch tests intentionally name voyage tokens
    # to exercise the (mode, name) matrix. Voyage names here are the
    # subject under test, not assertions of cloud-mode behavior.
    "test_rdr_109_phase2_dispatch.py",
    # Local-daemon client-side embedding tests: voyage tokens are
    # collection-NAME fixtures / Fix-4 display-dispatch subjects; every
    # test pins mode explicitly via T3Database(local_mode=...), not the
    # ambient cloud_mode fixture.
    "test_local_daemon_client_embed.py",
    # RDR-159 P0 detection classifier: voyage tokens are collection-NAME
    # fixtures driving the support matrix; every test pins deployment mode
    # explicitly via the ``voyage_key_present`` argument, never the ambient
    # cloud_mode fixture (the classifier is a pure deployment-mode function).
    "test_detection.py",
    # RDR-166 nexus-hxry2 vector-ETL: voyage tokens are collection-NAME segments
    # driving same-model passthrough vs cross-model routing (_is_same_model_
    # passthrough / _migrate_one). The ETL never embeds — the server does — so
    # these tests are deployment-mode-agnostic; they pin behavior via explicit
    # target_name / collection names, never the ambient cloud_mode fixture.
    "test_vector_etl.py",
    # RDR-159 P1d pre-gate + P1c quiesce: voyage tokens are collection-NAME /
    # wired-model-set fixtures driving the support gate and the count-mismatch
    # attribution message; mode is pinned explicitly via the injected
    # WiredModelSource / ``voyage_key_present`` argument, never the ambient
    # cloud_mode fixture.
    "test_pregate.py",
    "test_quiesce.py",
    # RDR-169 G5 bridge address-field tests: voyage tokens appear only as
    # collection-NAME fixtures (knowledge__test__voyage-context-3__v1) in fully
    # mocked HttpVectorClient / _ServiceCollectionStub unit tests. The server
    # embeds, not these tests — they assert additive /v1 response shape and the
    # include_source_uri opt-in, never cloud-mode embedding behavior.
    "test_bridge_address_fields.py",
    # nexus-8o9pm voyage-capability gate: voyage tokens are collection-NAME
    # fixtures (footprint detection) and embedding_models inside a FAKE /version
    # response body; the gate is a pure data/HTTP predicate that never embeds, so
    # there is no ambient cloud_mode behavior to assert.
    "test_guided_upgrade_voyage_capability.py",
    # nexus-3l6gz multi-model combined-query grouping: voyage tokens are
    # collection-NAME fixtures driving _group_collections_by_model against a
    # fully-fake model-aware T3 stub — no embedder ever runs (the fake resolves
    # "embedding" by parsing the collection name), so the tests are
    # deployment-mode-agnostic; nothing asserts cloud-mode behavior.
    "test_combined_query_multimodel_bug.py",
    # RDR-001 managed-endpoint probe: voyage tokens appear only as
    # embedding_models inside a FAKE /version response body (injected
    # http_get) — the managed service's reported models, not cloud-mode
    # behavior. The test touches no credentials and pins nothing on
    # is_local_mode; the probe targets the unauthenticated /version handshake.
    "test_managed_endpoint.py",
    # nexus-vgq89 burn-down (2026-07-15): the Phase 1 "ships excluded,
    # subsequent PRs promote each" batch above (test_mode_declarations_are_
    # explicit.py .. test_managed_endpoint.py) predates this comment and is
    # left as-is. The 19 files below were that batch's un-promoted remainder
    # (conftest.py:530-548 in the pre-burn-down revision) — the promotion
    # was promised but never done. Each is now resolved one of two ways:
    # actually promoted (cloud_mode fixture added to the genuinely
    # cloud-behavior tests; file removed from this set — see
    # test_collection_cmd.py / test_doc_indexer.py / test_indexer_e2e.py /
    # test_integration.py / test_pdf_e2e.py / test_voyage_retry.py, now
    # living only as individual ``_MODE_LINT_EXCLUDE_NODEIDS`` entries
    # below plus their cloud_mode promotions), or kept here with an
    # honest per-file rationale (the remainder, below) matching the
    # documented-rationale pattern already used throughout the rest of
    # this set.
    #
    # Substantive-critic correction (2026-07-15, same-day follow-up): the
    # first pass of this burn-down wrote rationale for 5 files
    # (test_index_cmd.py, test_index_pdf_batch.py, test_index_rdr_cmd.py,
    # test_indexer.py, test_mcp_server.py) WITHOUT checking each file's
    # HEADER first — all 5 already carry a pre-existing module-level
    # ``pytestmark = pytest.mark.usefixtures("cloud_mode")`` (RDR-109
    # Phase 2), which means every test in them already satisfies the
    # lint's fixturenames check regardless of this exclusion set; the
    # "no is_local_mode() branch under test" rationale written for them
    # was simply false. All 5 are removed here as free wins. The
    # correction sweep also caught 9 MORE files with the same pre-existing
    # module mark sitting unnecessarily in the older "Schema /
    # canonical-set" block further below (test_catalog_cli.py,
    # test_catalog_collection_for.py, test_catalog_consolidation.py,
    # test_collection_name_migration.py, test_commands_dt.py,
    # test_corpus.py, test_indexer_conformant_names.py, test_rdr_hook.py,
    # test_registry.py) — removed there for the same reason. The sweep
    # also surfaced one rationale that was substantively wrong despite no
    # module mark: test_indexer_e2e.py's ``_pin_fake_voyage_key`` autouse
    # fixture makes its embedding-model assertions genuinely
    # credential-routing-dependent, not literal test data — promoted to
    # ``cloud_mode`` alongside the other 4 promotions above instead of
    # staying here.
    #
    # "chunker-param" class: the voyage token is passed as an explicit
    # ``target_model`` / collection-name string argument to a pure chunking
    # or CLI-normalization function (``_pdf_chunks``, ``_markdown_chunks``,
    # ``_collections_from_registry_info``-style name synthesis); no embedder
    # ever runs and no ``is_local_mode()`` branch is exercised by the
    # assertions. Equivalent to the "string-literal-as-name" class used
    # elsewhere in this set. No autouse fixture in this file's header sets
    # Voyage/Chroma credentials. Caveat:
    # ``test_staleness_check_uses_content_hash_when_catalog_absent`` DOES
    # call ``set_credentials(monkeypatch)`` directly in its body — but its
    # assertions (``where == {"content_hash": expected_hash}``, ``result
    # == 0``) only check the staleness query's WHERE clause and short-
    # circuit outcome, never the stored/resolved ``embedding_model`` value
    # (the mocked existing metadata's ``"voyage-context-3"`` is unused
    # test-fixture noise, per the test's own docstring: staleness falls
    # back to content_hash "which uniquely identifies an unchanged file
    # just as well as the legacy source_path key"). cloud_mode would be a
    # no-op declaration here too.
    "test_catalog_path.py",
    # "retry-mechanics" class: ``target_model`` is an opaque literal passed
    # into ``_index_code_file`` against a mocked collection/Voyage client;
    # the test proves retry-on-connect-error behavior, which does not
    # depend on deployment mode. No module-level fixtures/marks in this
    # file's header.
    "test_chroma_retry.py",
    # Whole-file "nxexp export/import format" class: every flagged test
    # constructs or reads a ``.nxexp`` header/record by hand (or via
    # ``export_collection``/``import_collection`` against a local
    # ``ephemeral_db``); ``embedding_model`` is header/record metadata being
    # validated, compared, or round-tripped — never an actual embedder
    # invocation. No test in this file reads ``is_local_mode()``. The two
    # flagged tests that DO call ``monkeypatch.setenv`` for Chroma/Voyage
    # credentials (``TestImportFlagsCLI`` and one other) do so only to
    # route the CLI's ``_t3()`` handle through the mocked/ephemeral db
    # argument, not to select an embedder — no header-level autouse
    # fixture is involved.
    "test_exporter.py",
    # "chunker-param" class (same as test_catalog_path.py): both flagged
    # tests call ``_pdf_chunks(..., target_model="voyage-context-3", ...)``
    # directly with a mocked ``PDFExtractor``/``PDFChunker`` — the model is
    # an opaque label passed through to chunk metadata, not something an
    # embedder produced. No module-level fixtures/marks in this file's
    # header.
    "test_pdf_chunks_no_silent_zero.py",
    # Same class, same header-verified absence of credential fixtures.
    "test_pdf_extractor.py",
    # Same class. This file's one autouse fixture (``_legacy_vector_backend``)
    # only pins ``NX_STORAGE_BACKEND_VECTORS=local`` (a vector-STORAGE-backend
    # axis, Chroma-direct vs service) — orthogonal to embedder mode. The
    # module docstring states outright: "prove that the pipeline stitches
    # together correctly without requiring API keys or network access."
    "test_pdf_subsystem.py",
    # nexus-vgq89 correction (2026-07-15, code-review-expert delta):
    # test_pdf_e2e.py's 4 flagged tests are NOT here. First-draft
    # rationale claimed "cloud_mode would be actively misleading" on the
    # theory that the module has no credentials and embeds purely
    # locally — WRONG. Every flagged test does
    # ``patch("nexus.config.get_credential", side_effect=lambda k:
    # "test-key")``, which makes ``is_local_mode()`` (it calls
    # ``get_credential("chroma_api_key")`` / ``get_credential(
    # "voyage_api_key")``) resolve to CLOUD unconditionally — so
    # ``effective_embedding_model_for_writes`` genuinely takes the cloud
    # branch and synthesizes the ``voyage-context-3`` collection-name
    # segment for real, not as a hardcoded label. The ACTUAL embedding
    # is separately forced local via a distinct
    # ``_embed_with_fallback`` override — two independent axes, and the
    # naming axis is the one this lint cares about. Promoted to
    # ``cloud_mode`` (replacing the fragile incidental get_credential
    # side-effect with the explicit, robust fixture — no behavior
    # change, since ``cloud_mode`` patches ``is_local_mode`` directly
    # and the embed override is untouched).
    #
    # "chunker-param / mocked-embed" class: all three flagged tests pass
    # ``target_model="voyage-context-3"`` directly into ``chunker_loop`` /
    # ``pipeline_index_pdf`` with ``_embed_with_fallback`` fully mocked
    # (return value hardcoded); no real embedder call, no
    # ``is_local_mode()`` branch under test. ``test_embed_fn_none_
    # resolves_credentials`` patches ``nexus.config.get_credential``
    # directly (not the ambient env) and only asserts the fallback got
    # CALLED, never which model it resolved to — the credential-resolution
    # WIRING is under test, not cloud-mode embedding behavior.
    "test_pipeline_stages.py",
    # Whole-file "mocked-store / collection-name" class: every flagged
    # test drives a mocked ``mock_store`` or a faked-transport
    # ``real_http_vector_client`` and asserts on the RDR-103-normalized
    # collection name a CLI flag was translated to — never a real embedder
    # call. Two flagged tests depend (via ``mock_store``) on the file's
    # ``env_creds`` fixture, which sets Chroma/Voyage credentials so
    # ``mock_store`` specs as ``HttpVectorClient`` rather than a local
    # ``T3Database`` — but the assertion under test is
    # ``t3_collection_name``'s auto-promotion, a pure function of the
    # collection-name PREFIX (``voyage_model_for_collection`` in
    # src/nexus/corpus.py never calls ``is_local_mode()``), so the
    # env_creds-driven handle TYPE is irrelevant to what's asserted.
    "test_store_cmd.py",
    # Schema / canonical-set / collection-name shape — mode-independent.
    #
    # nexus-vgq89 correction sweep (2026-07-15): test_catalog_cli.py,
    # test_catalog_collection_for.py, and test_catalog_consolidation.py
    # (previously listed between test_catalog_backfill_collections.py and
    # test_catalog_db.py) were removed here as free wins — each already
    # carries a pre-existing module-level ``pytestmark = pytest.mark.
    # usefixtures("cloud_mode")`` (RDR-109 Phase 2), making the blanket
    # file exclusion redundant. See the correction note above
    # test_catalog_path.py for the full sweep methodology.
    "test_backfill_hash.py",
    "test_catalog_backfill_collections.py",
    "test_catalog_collection_name.py",
    "test_catalog_collections.py",
    "test_catalog_collections_rebuild.py",
    "test_catalog_concurrent_writer_lock.py",
    "test_catalog_db.py",
    # RDR-152 catalog SQLite->Postgres ETL: voyage tokens are collection-NAME
    # fixtures being migrated as data (owner/collection/document rows), never
    # assertions of cloud-mode embedding behaviour. The whole file is mode-agnostic.
    "test_catalog_etl.py",
    "test_catalog_doctor_collections_drift.py",
    # RDR-103 / nexus-j9ey + b03o advisor: voyage tokens appear in
    # synthetic collection names being asserted against, not as
    # cloud-mode behaviour under test.
    "test_catalog_doctor_name_vs_embed_dim.py",
    "test_upgrade_name_vs_embed_dim_advisory.py",
    "test_catalog_incremental_rebuild.py",
    "test_catalog_manifest_backfill.py",
    "test_catalog_migrate_fallback.py",
    "test_catalog_papers_curator_isolation.py",
    "test_catalog_rename_collection.py",
    "test_catalog_spans_chunk_char.py",
    "test_checkpoint.py",
    "test_collection_gc.py",
    # nexus-vgq89 correction sweep: test_collection_name_migration.py
    # removed here (same free-win reason as above — pre-existing module
    # cloud_mode mark).
    # RDR-137 P1.5a: voyage tokens appear in synthetic conformant
    # collection names used as backfill fixtures (e.g.
    # ``code__nexus-1-1__voyage-code-3__v1``). Tests exercise pure
    # SQLite + string parsing; no Voyage call is ever made.
    "test_collections_owner_backfill.py",
    # RDR-137 P2a (nexus-tts0d.4): same voyage-token-in-fixture pattern
    # — the catalog-backed reader tests register synthetic conformant
    # collection names and read them back; no Voyage call.
    "test_repos_reader.py",
    # RDR-137 P4.3 (nexus-tts0d.17): same pattern — knowledge__ /
    # docs__ collection names used as fixtures for the catalog
    # writer+reader cycle; no Voyage call.
    "test_index_corpus_knowledge_e2e.py",
    # RDR-137 followup CRITICAL-3/4/5 (nexus-43qgm.3-5): voyage tokens
    # appear in synthetic conformant collection names used as
    # adapter-test fixtures; no Voyage call is ever made.
    "test_rdr137_followup_critical_345.py",
    # RDR-137 followup SIG-6/8/11 (nexus-43qgm.6,8,11): same pattern
    # — voyage tokens in synthetic collection-name fixtures for the
    # OQ-5 deterministic-ordering and catalog-missing observability
    # tests; no Voyage call.
    "test_rdr137_followup_reader_sigs.py",
    # RDR-137 followup SIG-10/13/14/17 (nexus-43qgm.10,13,14,17):
    # voyage tokens in adapter / context / collection synthetic
    # fixtures; no Voyage call.
    "test_rdr137_followup_batch_sigs.py",
    # RDR-137 followup IMP-18..27 (nexus-43qgm.18-27): voyage tokens
    # in list_sibling_collections + adapter fixtures; no Voyage call.
    "test_rdr137_followup_p2_batch.py",
    # RDR-137 P3.5 (nexus-tts0d.10): same pattern — phantom
    # docs__1-2188 in the regression fixture for nexus-9iw41.
    "test_context_catalog_cutover.py",
    # nexus-vgq89 correction sweep: test_commands_dt.py, test_corpus.py,
    # and test_indexer_conformant_names.py removed here (same free-win
    # reason — pre-existing module cloud_mode mark).
    "test_doc_indexer_hash_sync.py",
    "test_doctor_cmd.py",
    "test_doctor_integrity.py",
    "test_doctor_search.py",
    "test_indexer_duplicate_content.py",
    "test_indexer_modules.py",
    "test_indexer_utils_repo.py",
    "test_memory.py",
    "test_metadata_consistency.py",
    "test_metadata_extraction_source.py",  # RDR-139 Layer D: pure schema unit
    "test_metadata_schema.py",
    # RDR-139 Phase 2/3 (Layers C/D/E): pure metadata-schema / CLI-routing /
    # T2-store unit tests; the voyage-context-3 literal is an incidental
    # placeholder embedding_model / collection-name segment, not cloud-mode
    # behavior.
    "test_dt_content_layer_d.py",
    "test_dt_mcp_fallback.py",
    "test_document_highlights.py",
    "test_dt_highlights_layer_e.py",
    "test_dt_capture_cmd.py",
    "test_migrations_rdr108_phase1c.py",
    "test_plan_run.py",
    # nexus-vgq89 correction sweep: test_rdr_hook.py (tests/hooks/) and
    # test_registry.py removed here (same free-win reason — pre-existing
    # module cloud_mode mark).
    "test_source_uri_home_key.py",
    "test_store_enrich_doc_id.py",
    "test_store_put_cli_parity.py",
    "test_t3_strict_collection_naming.py",
    "test_t3.py",
    "test_tuning_config.py",
    # Mode-self-tests — these assert local-mode behavior; cloud_mode
    # would invert what they test.
    "test_local_mode.py",
    # nexus-duoak.3 bench teardown-scope: voyage tokens appear only in
    # synthetic collection-name fixtures (REAL / after lists) exercising pure
    # set-difference logic in bench_tumblers/plan_teardown; no Voyage call is
    # ever made and no embedder mode is asserted.
    "test_teardown_scope.py",
})

_MODE_LINT_EXCLUDE_NODEIDS: frozenset[str] = frozenset({
    # Reserved for individual mixed-file exclusions. Format:
    # "tests/test_file.py::test_func"  (no parametrize suffix).
    #
    # RDR-185 ladder — reason: "string-literal-as-name". Every one of these
    # builds a conformant RDR-103 collection NAME (or a
    # CollectionClassification carrying the name's model SEGMENT) and asserts
    # on planning/rollback/re-id behaviour keyed off that segment. None calls
    # a Voyage embedder: the rung tests inject fakes for every collaborator,
    # and the local bge-768 path is what actually runs. cloud_mode would
    # change nothing they assert.
    #
    # The mislabel pair is the sharpest case FOR the exclusion: their whole
    # subject is a name whose voyage token LIES (a pre-RDR-109 collection
    # named voyage-context-3 whose stored vectors measure as local bge-768,
    # bead nexus-j5diu). Opting them into cloud_mode would assert the
    # opposite of their point.
    #
    # The six P2 entries (test_rollback_via_map, test_substrate_leg) were
    # already offending before P4 and went unnoticed because this arc ran
    # narrow, path-scoped selections — this lint only fires when the full
    # session is collected, so `pytest tests/upgrade/` alone never sees it.
    # nexus-r5f3c — reason: "string-literal-as-config-value". The test's
    # subject is the SUPERVISOR's env-plumbing gate: a legacy config with
    # local.embed_model="voyage-context-3" must still plumb the credential
    # chain (the mirror of the bge-blocks-plumb case). Popen is mocked; no
    # embedder or cloud call exists. cloud_mode would change nothing.
    "tests/daemon/test_storage_service_daemon.py::TestSpawnServiceVoyageKeyPlumbing::test_voyage_configured_model_still_plumbs",
    "tests/upgrade/test_rollback_via_map.py::test_cross_model_rollback_deletes_from_recorded_target",
    "tests/upgrade/test_rollback_via_map.py::test_cross_model_conformant_ids_roll_back_via_target_names",
    "tests/upgrade/test_substrate_leg.py::test_execute_cross_model_leg_targets_remapped_collection",
    "tests/upgrade/test_substrate_leg.py::test_reid_only_leg_passes_through_stored_vectors",
    "tests/upgrade/test_substrate_leg.py::test_mis_provenanced_vector_falls_back_to_reembed",
    "tests/upgrade/test_substrate_leg.py::test_pure_reembed_leg_rolls_back_via_plan_target_names",
    "tests/upgrade/test_substrate_rung.py::test_measured_768_mislabel_is_planned_without_a_voyage_key",
    "tests/upgrade/test_substrate_rung.py::test_genuine_voyage_without_a_key_is_still_the_credential_case",
    "tests/upgrade/test_gap4_two_mechanisms.py::test_rung_convergence_is_re_derived_live_never_cached",
    #
    # REAL keyed integration tests (-m integration, @requires_voyage_key):
    # these derive cloud mode from GENUINE credentials — the cloud_mode
    # fixture would OVERWRITE the real VOYAGE_API_KEY with the "vk_test"
    # fake and break them against the live API (caught by the local-service
    # gate during the 6.10.1 release: voyageai AuthenticationError; the
    # default-marker full suite deselects -m integration, so only the gate
    # runs these). Their mode declaration is the requires-key gating itself.
    "tests/test_integration.py::test_voyage_code3_index_and_query",
    "tests/test_integration.py::test_cce_query_retrieves_cce_indexed_markdown",
    "tests/test_integration.py::test_t3_put_embedding_model_in_search_metadata",
    #
    # nexus-pebfx.2: Java-SOURCE-PARSING parity tests — they regex the
    # EmbedderRouter/embedder .java files for RDR-103 model tokens and
    # cross-check Python _MODEL_DIMS. The voyage tokens are registry
    # labels being compared, not embedder behavior; no embedder runs and
    # no mode-dependent code path is exercised ("canonical-set" class).
    "tests/migration/test_vector_etl.py::TestEmbedderModeParityJava::test_cloud_mode_dispatch_tokens_are_known_models",
    # nexus-e0w01 / nexus-gednd (2026-07-13): "string-literal-as-name" class —
    # the voyage token appears only inside RDR-103-conformant collection-NAME
    # strings; the frecency test pins the service path via
    # NX_STORAGE_BACKEND_VECTORS + a mocked HttpVectorClient (no embedder
    # runs), and the tripwire tests mock get_t3/compute_assignments entirely.
    "tests/test_frecency_service_mode.py::TestFrecencyRdrCollection::test_rdr_collection_included_in_frecency_update",
    "tests/test_taxonomy_hook_tripwire.py::test_local_path_failure_records_hook_failures_row",
    "tests/test_taxonomy_hook_tripwire.py::test_tripwire_persist_failure_never_propagates",
    "tests/migration/test_vector_etl.py::TestEmbedderModeParityJava::test_embedder_model_tokens_match_java_overrides",
    #
    # #1060: pure collection-NAME validation (length/charset) — references a
    # legacy voyage-named collection as realistic input but makes no cloud-mode
    # embedder assertion, so the cloud_mode fixture is not applicable.
    "tests/test_issue_1060_collection_name_overflow.py::test_short_known_voyage_name_passes",
    #
    # nexus-h8rf6.3: shape-conformance regression — a REAL HttpCatalogClient
    # (faked transport) flows through build_staleness_cache; the voyage token
    # appears only inside a conformant collection-name string used as data
    # ("string-literal-as-name" class). No embedder runs; no mode-dependent
    # path is exercised.
    "tests/catalog/test_docs_for_chashes_shape_conformance.py::TestBuildStalenessCacheConsumesRealHttpClient::test_no_raise_with_real_http_catalog_client",
    #
    # nexus-h8rf6 wave (expire/update_source_path/collection_metadata ports +
    # the 49523e16 live-content regression): all "string-literal-as-name" —
    # a REAL HttpVectorClient/HttpCatalogClient over a FAKED transport, with
    # the voyage token appearing only inside conformant collection-name
    # strings used as opaque data (or, for collection_metadata, asserting the
    # NAME-derived model parse). No embedder runs; no mode-dependent path.
    "tests/catalog/test_docs_for_chashes_live_content.py::TestBuildStalenessCacheLiveContent::test_nonzero_docs_after_index_like_write",
    "tests/test_http_vector_client_parity.py::TestExpire::test_expire_deletes_only_expired_knowledge_rows",
    "tests/test_http_vector_client_parity.py::TestExpire::test_expire_no_knowledge_collections_returns_zero",
    "tests/test_http_vector_client_parity.py::TestUpdateSourcePath::test_rewrites_matching_rows_and_returns_count",
    "tests/test_http_vector_client_parity.py::TestCollectionMetadata::test_returns_t3_parity_keys",
    #
    # RDR-159 P4 (nexus-ue6g7.24): the guided-upgrade driver's two-leg test
    # uses a conformant voyage-named collection STRING to assert the composite
    # read client routes it to the cloud leg + that distinct dims (384, 1024)
    # are extracted. The engine is fully mocked; no embedder runs and no
    # mode-dependent path executes ("string-literal-as-name" class).
    # (renamed test_two_leg_composes_collections_and_dims -> _reopens_both_legs_
    # for_landing in the RDR-180 land-then-transform rewrite, nexus-jxizy.10.7 —
    # same fully-mocked engine, same string-literal-as-name rationale.)
    "tests/migration/test_driver.py::test_two_leg_reopens_both_legs_for_landing",
    #
    # nexus-gc2ze + nexus-c9xr2/u37lw wave (2026-07-04): all
    # "string-literal-as-name" — a REAL HttpCatalogClient/HttpVectorClient
    # over a FAKED transport; the voyage token appears only inside
    # conformant collection-name strings used as opaque identifiers (the
    # u37lw guard tests additionally assert the NAME-derived model parse,
    # same rationale as collection_metadata above). No embedder runs; no
    # mode-dependent path executes.
    "tests/catalog/test_http_catalog_client.py::TestResolveChunk::test_resolve_chunk_returns_full_dict",
    "tests/test_service_mode_cli_real_client.py::test_collection_reembed_dry_run_service_mode_real_client",
    "tests/test_service_mode_cli_real_client.py::test_collection_reembed_cross_model_rejected_service_mode",
    "tests/test_service_mode_cli_real_client.py::test_collection_reembed_same_model_uses_verbatim_passthrough",
    #
    # nexus-gilf2: the cross-model remap-target test asserts the driver derives
    # voyage target NAMES (voyage-code-3 / voyage-context-3) in cloud mode. Mode
    # is pinned explicitly by patching ``voyage_key_available`` (via the
    # ``voyage_key=True`` engine patch), not the ambient cloud_mode fixture —
    # the target resolver is a pure deployment-mode function, same rationale as
    # the ``test_detection.py`` file exclusion ("string-literal-as-name" class).
    "tests/migration/test_driver.py::test_cross_model_target_is_voyage_in_cloud_mode",
    #
    # RDR-152 nexus-gmiaf.22 (Seam B): asserts service-mode skips the embed
    # fallback. Voyage tokens appear only as realistic collection-NAME /
    # prepared-chunk-metadata fixtures (real docs collections ARE
    # voyage-context-3); the test never calls Voyage — service mode embeds
    # server-side — so it makes no cloud-mode embedder assertion and the
    # cloud_mode fixture is not applicable.
    "tests/test_indexer_seam_b_cutover.py::test_index_pdf_incremental_service_mode_skips_embed_fallback",
    #
    # RDR-152 nexus-qnp5s: catalog consumer migration tests. Voyage tokens
    # appear only as realistic collection-NAME fixtures in collections_by_owner
    # assertions (real collections ARE voyage-named); these test the catalog
    # public-API methods, not cloud-mode embedder behavior, so cloud_mode is
    # not applicable.
    "tests/test_catalog_consumer_service_mode.py::TestSQLiteCatalogNewMethods::test_collections_by_owner_filters",
    "tests/test_catalog_consumer_service_mode.py::TestHttpCatalogClientNewMethods::test_collections_by_owner",
    #
    # RDR-152 nexus-enehl: frecency metadata-update service client test. The
    # voyage token is a realistic collection-NAME fixture for the update-chunks
    # HTTP request body; the test asserts the request is POSTed to the
    # /update-metadata endpoint, not any cloud-mode embedder behavior.
    "tests/db/test_http_vector_client.py::TestUpdateChunks::test_posts_to_update_metadata_endpoint",
    #
    # nexus-f0r8p.3 (RDR-181): force_re_embed forwarding tests in the batch-flush
    # closure. The voyage tokens are collection-NAME fixtures (code__repo__voyage-code-3__v1
    # etc.); the tests assert the force_re_embed kwarg is forwarded/omitted
    # correctly on the flush call, not any cloud-mode embedder behavior.
    "tests/test_indexer_seam_b_cutover.py::test_run_index_batch_flush_forwards_force_re_embed",
    "tests/test_indexer_seam_b_cutover.py::test_run_index_batch_flush_force_false_omits_force_re_embed",
    #
    # nexus-5b9v0: the target-name collision guard tests build
    # CollectionClassification/message fixtures naming real conformant
    # collections (code__1-3__voyage-code-3__v1 etc.) to assert the pre-flight
    # collision detector fires and its message names the colliding sources
    # correctly. The voyage tokens are collection-NAME/model-label DATA the
    # guard reasons about structurally (classify_collections is fully mocked);
    # no embedder runs and no mode-dependent path executes
    # ("string-literal-as-name" class, same rationale as test_driver.py's
    # existing exclusions above).
    # (renamed ..._blocked_before_sequence -> ..._before_land_then_transform in
    # the RDR-180 rewrite, nexus-jxizy.10.7 — voyage_key pinned False, engine
    # fully mocked.)
    "tests/migration/test_driver.py::test_target_name_collision_blocked_before_land_then_transform",
    "tests/migration/test_driver.py::test_target_name_collision_between_two_remapped_collections",
    "tests/migration/test_driver.py::test_target_name_no_collision_when_targets_distinct",
    "tests/migration/test_driver.py::test_target_name_collision_three_way",
    "tests/migration/test_driver.py::test_target_name_collision_message_carries_classification_metadata",
    "tests/migration/test_driver.py::test_target_name_collision_message_flags_likely_stale_source",
    "tests/commands/test_migrate_cost_guardrail.py::TestRunMigrationCollisionGuard::test_target_name_collision_renders_as_click_exception",
    #
    # nexus-p9vqa / nexus-772h2 (nx migration-audit + dual-world false-clean
    # regression): both build CollisionAuditReport / CollectionClassification
    # fixtures and conformant collection-NAME strings (code__1-3__voyage-code-3__v1)
    # as the audit's opaque input data. classify_collections / read+vector
    # clients are fully monkeypatched — no embedder runs and no mode-dependent
    # path executes ("string-literal-as-name" class, same rationale as the
    # test_driver.py collision exclusions above).
    "tests/migration/test_collision_audit.py::test_false_clean_regression_merge_only_visible_in_no_key_world",
    "tests/test_migration_audit_cmd.py::test_json_output_is_machine_readable",
    #
    # nexus-te885.8.1 (pg-source reconcile leg for verify-fill): builds a
    # mocked /v1/vectors/collections response using conformant collection-
    # NAME strings (code__nexus-1-1__voyage-code-3__v1,
    # knowledge__nexus-1-1__voyage-context-3__v1) purely as PgReadClient
    # list_collections() parsing test data. No embedder runs and no
    # mode-dependent path executes ("string-literal-as-name" class, same
    # rationale as the test_driver.py collision exclusions above).
    "tests/migration/test_pg_read.py::TestListCollections::test_returns_name_objects",
    #
    # nexus-vgq89 burn-down (2026-07-15): test_collection_cmd.py promoted
    # out of the whole-file grandfathered exclusion above. Three of its
    # eight flagged tests are genuine cloud-embedder behavior (re-embed via
    # Voyage) and now carry the ``cloud_mode`` fixture directly; the
    # remaining five below are "string-literal-as-name" /
    # collection-name-DATA: ``_collections_from_registry_info`` and
    # ``run_collection_postprocessing`` tests build registry-info dicts
    # with conformant collection-name strings and fully mock
    # ``_discover_taxonomy``/``make_t3`` — no embedder runs. Note
    # ``test_collections_from_registry_info_filters_excluded`` and
    # ``..._prefers_conformant_code_collection`` do exercise
    # ``is_local_mode()`` indirectly (via ``taxonomy.local_exclude_
    # collections``), but neither test's actual assertions depend on which
    # branch fires — both only assert the always-unfiltered docs__/rdr__
    # names are present, never a code__ presence/absence — so cloud_mode
    # would be a no-op declaration, not a real promotion.
    "tests/test_collection_cmd.py::test_collections_from_registry_info_filters_excluded",
    "tests/test_collection_cmd.py::test_collections_from_registry_info_prefers_conformant_code_collection",
    "tests/test_collection_cmd.py::test_collections_from_registry_info_dedupes",
    "tests/test_collection_cmd.py::test_run_collection_postprocessing_does_not_pass_alias_through",
    # ``test_info_shows_embedding_model``: parametrized over
    # (collection_name, expected_model) pairs against a mocked ``mock_db``;
    # asserts the ``info`` command's display parses the model out of the
    # collection NAME, never a real embedder call.
    "tests/test_collection_cmd.py::test_info_shows_embedding_model",
    #
    # nexus-vgq89 burn-down (2026-07-15): test_doc_indexer.py promoted out
    # of the whole-file grandfathered exclusion above; 32 of its 36
    # flagged tests genuinely exercise cloud-embedder behavior (the
    # ``_embed_with_fallback``/CCE family, and the credential-gated
    # staleness/force/incremental-checkpoint family whose target_model
    # resolution depends on ``is_local_mode()``) and now carry the
    # ``cloud_mode`` fixture directly. The four below do not:
    # ``test_index_md_falls_back_to_local_embedder_when_no_credentials``
    # and ``test_make_local_embed_fn_returns_consistent_model_name`` are
    # mode-self-tests — they explicitly delete/never-set credentials to
    # prove the LOCAL fallback path; ``cloud_mode`` would invert what they
    # test (same "mode-self-test" class as test_local_mode.py above).
    "tests/test_doc_indexer.py::test_index_md_falls_back_to_local_embedder_when_no_credentials",
    "tests/test_doc_indexer.py::test_make_local_embed_fn_returns_consistent_model_name",
    # ``TestSectionTypeInPipeline``'s two tests call ``_markdown_chunks(md,
    # "abc123", "voyage-context-3", ...)`` directly — the model is an
    # opaque label argument to a pure chunking/section-classification
    # function; no embedder runs ("string-literal-as-name" / "chunker-param"
    # class, same as test_catalog_path.py above).
    "tests/test_doc_indexer.py::TestSectionTypeInPipeline::test_markdown_chunks_has_section_type",
    "tests/test_doc_indexer.py::TestSectionTypeInPipeline::test_markdown_chunks_section_classified",
})


@pytest.fixture
def db(tmp_path: Path) -> T2Database:
    """Provide a T2Database backed by a temporary SQLite file."""
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


def make_vector_test_client():
    """THE test vector substrate (RDR-155 P4b P0a): a fresh
    ``InMemoryVectorClient`` with the real MiniLM default EF.

    The single replacement idiom for inline ``chromadb.EphemeralClient()``
    test constructions — semantics pinned differentially against the
    chroma oracle by ``tests/test_vector_substrate_contract.py``. Real
    per-instance isolation (no SharedSystemClient shared-state gotcha).
    The EF choice is centralised here so the P0b test-EF decision edits
    ONE line; the chromadb import dies with the dependency at P3.
    """
    from nexus.db.inmemory_vector_store import InMemoryVectorClient

    return InMemoryVectorClient(
        default_embedding_function=DefaultEmbeddingFunction()
    )


@pytest.fixture
def local_t3() -> T3Database:
    """T3Database backed by a fresh InMemoryVectorClient and DefaultEmbeddingFunction.

    Each test gets a fresh, isolated database — no API keys required.
    DefaultEmbeddingFunction uses the bundled ONNX MiniLM-L6-v2 model,
    so semantic similarity works correctly without Voyage AI.
    """
    return T3Database(
        _client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction()
    )


# ── PDF fixture generators ─────────────────────────────────────────────────

_PAGE_TOPICS = [
    "Apple orchards produce fruit in autumn harvests.",
    "Database transactions ensure ACID consistency in storage systems.",
    "Network protocols define communication rules between distributed nodes.",
]


def _make_simple_pdf(path: Path) -> None:
    """1-page TrueType PDF with embedded metadata."""
    import pymupdf  # lazy

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 100),
        "Hello World. This is a test document for PDF ingest.",
        fontsize=12,
    )
    doc.set_metadata({
        "title": "Test Document",
        "author": "Test Author",
        "subject": "PDF Ingest Testing",
        "keywords": "test, pdf, nexus",
        "creationDate": "D:20260301000000",
    })
    doc.save(str(path))
    doc.close()


def _make_multipage_pdf(path: Path) -> None:
    """3-page TrueType PDF with semantically distinct content per page.

    Each page uses insert_textbox to fill a text rectangle (~2000 chars).
    This ensures:
    - PDFChunker(chunk_chars=100) produces multiple chunks (AC-U9/U10).
    - PDFChunker with the default 1500-char limit produces at least one
      dedicated chunk per page for reliable page attribution in E2E tests (AC-E2).
    """
    import pymupdf  # lazy

    doc = pymupdf.open()
    rect = pymupdf.Rect(72, 72, 523, 750)
    for topic in _PAGE_TOPICS:
        page = doc.new_page()
        text = f"{topic} " * 30
        page.insert_textbox(rect, text.strip(), fontsize=12)
    doc.set_metadata({"title": "Multipage Test", "author": "Test Author"})
    doc.save(str(path))
    doc.close()


def _make_type3_pdf(path: Path) -> None:
    """Generate a minimal valid PDF with a Type3 font as raw bytes.

    A ~600-byte hand-crafted PDF:
    - Object 3 (page) resources reference font object 5 as /F1
    - Object 5 is a Type3 font with a single glyph 'A' defined via CharProcs
    - Object 6 is the CharProcs stream for 'A' (d0 + filled box)
    - Object 4 is the page content stream (draws 'A' using /F1)

    Docling handles Type3 fonts via its own text extraction layer.
    get_text() on a Type3 glyph returns '' or 'A' depending on pymupdf
    version — used by pymupdf_normalized fallback if Docling fails.
    """
    glyph_stream = b"100 0 d0\n0 0 100 100 re f\n"
    content_stream = b"BT /F1 12 Tf 100 700 Td (A) Tj ET\n"

    obj_bodies = [
        # 1: catalog
        b"<</Type/Catalog/Pages 2 0 R>>",
        # 2: pages tree
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        # 3: page — resources point at font object 5
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        # 4: content stream
        b"<</Length " + str(len(content_stream)).encode() + b">>"
        b"\nstream\n" + content_stream + b"endstream",
        # 5: Type3 font dictionary; CharProcs references object 6
        b"<</Type/Font/Subtype/Type3"
        b"/FontBBox[0 0 100 100]"
        b"/FontMatrix[0.01 0 0 0.01 0 0]"
        b"/FirstChar 65/LastChar 65/Widths[100]"
        b"/CharProcs<</A 6 0 R>>"
        b"/Encoding<</Type/Encoding/Differences[65/A]>>>>",
        # 6: glyph procedure stream for 'A'
        b"<</Length " + str(len(glyph_stream)).encode() + b">>"
        b"\nstream\n" + glyph_stream + b"endstream",
    ]

    header = b"%PDF-1.4\n"
    body_parts: list[bytes] = []
    offsets: list[int] = []
    pos = len(header)
    for i, body in enumerate(obj_bodies, start=1):
        obj_bytes = f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        offsets.append(pos)
        pos += len(obj_bytes)
        body_parts.append(obj_bytes)

    body = b"".join(body_parts)
    xref_pos = len(header) + len(body)
    n = len(obj_bodies) + 1  # includes free entry 0
    xref = b"xref\n" + f"0 {n}\n".encode()
    xref += b"0000000000 65535 f\r\n"
    for offset in offsets:
        xref += f"{offset:010d} 00000 n\r\n".encode()
    trailer = (
        b"trailer\n<</Size " + str(n).encode() + b"/Root 1 0 R>>\n"
        b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    )
    path.write_bytes(header + body + xref + trailer)


@pytest.fixture(scope="session")
def pdf_fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate all PDF test fixtures once per test session."""
    d = tmp_path_factory.mktemp("pdf_fixtures")
    _make_simple_pdf(d / "simple.pdf")
    _make_multipage_pdf(d / "multipage.pdf")
    _make_type3_pdf(d / "type3_font.pdf")
    return d


@pytest.fixture(scope="session")
def simple_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "simple.pdf"


@pytest.fixture(scope="session")
def multipage_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "multipage.pdf"


@pytest.fixture(scope="session")
def type3_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "type3_font.pdf"


# ── RDR-157 P3.4: synthetic PG bundle factory (bead nexus-vwvv5.13) ─────────────


@pytest.fixture
def make_pg_bundle_txz():
    """Factory building a synthetic ``nexus-pg-*.txz`` for bundle-extract tests.

    Mirrors the real P3.1 artifact shape: a ``bundle/`` root containing
    ``bin/{initdb,pg_ctl,psql,createdb}`` (stub executables), ``include/``,
    ``lib/``, ``share/``, and the ``.build_prefix`` relocation marker that
    ``scripts/build_pg_bundle.sh`` stamps. Single source of truth so a layout
    change (e.g. a new required binary) is a one-site edit.
    """
    import tarfile

    def _factory(tmp: Path, name: str = "nexus-pg-test.txz", *, with_build_prefix: bool = True) -> Path:
        staging = tmp / f"_stage_{name}"
        bundle = staging / "bundle"
        bin_dir = bundle / "bin"
        bin_dir.mkdir(parents=True)
        for b in ("initdb", "pg_ctl", "psql", "createdb"):
            f = bin_dir / b
            f.write_text("#!/bin/sh\nexit 0\n")
            f.chmod(0o755)
        for sub in ("include", "lib", "share"):
            (bundle / sub).mkdir()
        if with_build_prefix:
            (bundle / ".build_prefix").write_text("/build/prefix/nexus-pg\n")
        archive = tmp / name
        with tarfile.open(archive, "w:xz") as tf:
            tf.add(bundle, arcname="bundle")
        return archive

    return _factory


# ── docling model availability (nexus-c7gnx) ─────────────────────────────────
#
# The docling PDF extractor loads its layout + TableFormer models from the
# HuggingFace cache; when they are absent (offline, cold cache) docling raises
# LocalEntryNotFoundError and the extractor SILENTLY falls back to PyMuPDF
# (extraction_method='pymupdf_normalized'). Tests that assert
# extraction_method=='docling' then fail with a confusing assertion rather than
# a clear "models unavailable" signal. CI pre-fetches the models and HARD-FAILS
# if it cannot (see .github/workflows/ci.yml), so in CI the models are always
# present and these guards never skip. The skip only fires on a local run with a
# cold HF cache — turning a baffling fallback-assertion failure into a clean skip.


@pytest.fixture(scope="session")
def docling_available(tmp_path_factory: pytest.TempPathFactory) -> bool:
    """True iff docling actually performs the extraction (models present).

    Faithful probe: docling loads models lazily at convert() time, so we run a
    real extraction on a tiny generated PDF and check the SAME signal the tests
    assert (extraction_method == 'docling'). A cold/offline model cache makes the
    extractor fall back to PyMuPDF, which this detects as unavailable.

    Known limitation: the probe CANNOT distinguish "models unavailable"
    (environmental, skipping is correct) from "docling regressed in CODE so the
    extractor fell back" (a real bug) — both surface as extraction_method !=
    'docling'. This is acceptable because CI does NOT rely on the skip: the
    pre-fetch step (scripts/ci_warm_docling.py) runs this same probe and
    HARD-FAILS the job, so a docling code regression goes CI-red at pre-fetch.
    The skip is a local-developer convenience only; see require_docling.
    """
    try:
        import pymupdf

        probe = tmp_path_factory.mktemp("docling-probe") / "probe.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "docling availability probe")
        doc.save(str(probe))
        doc.close()

        from nexus.pdf_extractor import PDFExtractor

        result = PDFExtractor().extract(probe)
        return result.metadata.get("extraction_method") == "docling"
    except Exception:
        return False


@pytest.fixture
def require_docling(docling_available: bool) -> None:
    """Skip the requesting test when docling did not perform the extraction.

    Composes with the CI pre-fetch hard-fail: in CI the models are guaranteed
    present so this never skips; locally it skips cleanly instead of failing on
    the silent PyMuPDF fallback.
    """
    if not docling_available:
        pytest.skip(
            "docling did not perform the extraction. Locally this almost always "
            "means a cold/offline HuggingFace model cache; it can ALSO indicate a "
            "docling regression. CI does not rely on this skip — its pre-fetch step "
            "runs the same probe and HARD-FAILS (red), which is what distinguishes a "
            "genuine regression from a missing local cache. This skip only fires on "
            "a local run."
        )
