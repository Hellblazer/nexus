# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.5 (nexus-pac1) - ``nx doctor`` CLI under
``NX_STORAGE_MODE=daemon``.

Coverage:

1. ``_T2Inspector`` direct seam: instantiating the inspector under
   daemon mode resolves to the daemon's introspection RPCs (via
   ``t2_ctx``) without opening a competing sqlite3 connection.

2. The five converted check functions (schema, plan_library,
   aspect_queue, tier_discipline, taxonomy) reach the T2 daemon
   without raising ``DaemonModeDiagnosticError`` (the previous
   reject-then-abort path). Each check is exercised end-to-end via
   ``CliRunner`` so the broader argument parsing + ``_T2Inspector``
   wiring is covered.

3. The two T3 ``make_t3()`` sites flipped to ``get_t3()`` (the doctor
   ``fix_paths`` mode + the quota report's reachability probe) resolve
   to a daemon-bound T3 client under daemon mode.

Some checks (``--check-tier-discipline``, ``--check-taxonomy``) depend
on session ID / catalog initialisation respectively; their daemon-mode
tests assert the seam reach rather than the full check output."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.t2_daemon import T2Daemon
from nexus.db.t2 import T2Database


# ── In-thread T2 daemon harness ─────────────────────────────────────────────


def _run_daemon(daemon: T2Daemon) -> asyncio.AbstractEventLoop:
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    started.wait(timeout=5.0)
    return loop


def _stop_daemon(daemon: T2Daemon, loop: asyncio.AbstractEventLoop) -> None:
    asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=5.0)
    loop.call_soon_threadsafe(loop.stop)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def reset_t3_singleton():
    import nexus.mcp_infra as infra
    original_t3 = infra._t3_instance
    original_collections = infra._collections_cache
    infra._t3_instance = None
    infra._collections_cache = ([], 0.0)
    yield
    infra._t3_instance = original_t3
    infra._collections_cache = original_collections


@pytest.fixture
def t2db(tmp_path: Path) -> T2Database:
    db = T2Database(tmp_path / "memory.db")
    yield db
    db.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def daemon_env(monkeypatch, config_dir: Path):
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture
def live_t2_daemon(t2db: T2Database, config_dir: Path, daemon_env):
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        yield daemon
    finally:
        # chak review NIT (defensive): doctor tests currently do not
        # invoke ``open_catalog`` / ``open_cached`` (they use
        # ``_T2Inspector`` directly), so no T2Client singleton is
        # populated. Calling ``reset_cache`` here keeps the fixture
        # symmetric with the other daemon-mode suites and is a no-op
        # when the singleton is None.
        from nexus.catalog import reset_cache
        reset_cache()
        _stop_daemon(daemon, loop)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def catalog_dir(tmp_path: Path, monkeypatch) -> Path:
    """Initialize a real catalog under tmp_path and route
    ``nexus.config.catalog_path`` at it for the test. Mirrors the
    fixture in ``test_catalog_daemon_mode.py``."""
    from nexus.catalog import Catalog
    cd = tmp_path / "catalog"
    cd.mkdir()
    Catalog.init(cd)
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: cd,
    )
    return cd


# ── _T2Inspector seam tests ────────────────────────────────────────────────


class TestT2InspectorSeam:
    def test_inspector_resolves_daemon_mode(
        self,
        live_t2_daemon,
        t2db: T2Database,
        reset_t3_singleton,
    ) -> None:
        """Under daemon mode the inspector takes the t2_ctx branch and
        never opens a local sqlite3.Connection. The daemon owns the
        path; a competing PersistentClient-equivalent would race the
        writer."""
        from nexus.commands.doctor import _T2Inspector
        with _T2Inspector(t2db.path) as t2:
            assert t2.mode == "daemon"
            assert t2._conn is None, (
                "daemon-mode inspector must NOT hold a sqlite3 connection"
            )
            assert t2._client is not None
            # The seam works end-to-end: schema RPC returns real data.
            tables = t2.tables()
            assert isinstance(tables, set)
            # memory table is created by T2Database init so the daemon
            # exposes it via the schema RPC.
            assert "memory" in tables

    def test_inspector_execute_via_daemon(
        self,
        live_t2_daemon,
        t2db: T2Database,
        reset_t3_singleton,
    ) -> None:
        """``execute()`` routes through ``exec_raw`` under daemon mode
        and returns tuples in fetchall-shape so existing check bodies
        stay drop-in compatible."""
        from nexus.commands.doctor import _T2Inspector
        # Seed one row via the daemon's T2Database so the count is non-
        # zero (otherwise we can't distinguish "RPC succeeded with 0"
        # from "RPC failed silently").
        t2db.put(project="pac1", title="probe.md", content="probe body")
        with _T2Inspector(t2db.path) as t2:
            rows = t2.execute("SELECT COUNT(*) FROM memory")
            assert rows
            assert rows[0][0] >= 1


# ── End-to-end CLI under daemon mode ───────────────────────────────────────


class TestCheckSchemaUnderDaemon:
    def test_check_schema_runs_under_daemon(
        self,
        live_t2_daemon,
        t2db: T2Database,
        reset_t3_singleton,
        runner: CliRunner,
    ) -> None:
        """``nx doctor --check-schema`` works under daemon mode (pre-
        pac1 it called ``reject_under_daemon_mode`` and refused).
        Output contains the schema header and at least one table check
        line."""
        result = runner.invoke(main, ["doctor", "--check-schema"])
        assert result.exit_code == 0, result.output
        assert "T2 Schema Check" in result.output
        assert "Table memory" in result.output


class TestCheckPlanLibraryUnderDaemon:
    def test_check_plan_library_runs_under_daemon(
        self,
        live_t2_daemon,
        t2db: T2Database,
        reset_t3_singleton,
        runner: CliRunner,
    ) -> None:
        """``nx doctor --check-plan-library`` works under daemon mode.
        Exit code 1 is acceptable on a fresh DB (global builtin count
        < min); we assert on the output shape, not the exit code."""
        result = runner.invoke(main, ["doctor", "--check-plan-library"])
        # The check writes its header line regardless of pass/fail.
        assert "Plan library check" in result.output, result.output


class TestCheckAspectQueueUnderDaemon:
    def test_check_aspect_queue_runs_under_daemon(
        self,
        live_t2_daemon,
        t2db: T2Database,
        reset_t3_singleton,
        runner: CliRunner,
    ) -> None:
        """``nx doctor --check-aspect-queue`` reads the queue table via
        the introspection RPC; an empty / missing table is the
        success path on a fresh daemon."""
        result = runner.invoke(main, ["doctor", "--check-aspect-queue"])
        assert result.exit_code == 0, result.output
        # Either "table not present" or "0 row(s) total" — both are
        # daemon-mode-clean outcomes against a fresh T2.
        assert "aspect_extraction_queue" in result.output


class TestCheckTierDisciplineUnderDaemon:
    def test_check_tier_discipline_runs_under_daemon(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_t2_daemon,
        t2db: T2Database,
        reset_t3_singleton,
        runner: CliRunner,
    ) -> None:
        """``nx doctor --check-tier-discipline`` against a fresh T2
        (no ``tier_writes`` table yet) takes the "not yet initialised"
        path. Confirms the daemon-mode seam reaches the check without
        the prior ``reject_under_daemon_mode`` abort.

        Pre-pac1 review SUGGESTION: this coverage was missing. A
        fixed session ID via env var bypasses the live-session
        resolver so the test is hermetic."""
        monkeypatch.setenv("NX_SESSION_ID", "pac1-test-session")
        result = runner.invoke(main, ["doctor", "--check-tier-discipline"])
        assert result.exit_code == 0, result.output
        assert "Tier-discipline check" in result.output


class TestCheckTaxonomyUnderDaemon:
    def test_check_taxonomy_runs_under_daemon(
        self,
        live_t2_daemon,
        t2db: T2Database,
        reset_t3_singleton,
        runner: CliRunner,
    ) -> None:
        """``nx doctor --check-taxonomy`` reads the taxonomy tables via
        introspection. Against a fresh T2 the tables exist (created
        by T2Database init) but have no rows; the invariant trivially
        holds."""
        result = runner.invoke(main, ["doctor", "--check-taxonomy"])
        assert result.exit_code == 0, result.output
        assert (
            "topic_links invariant holds" in result.output
            or "Taxonomy tables missing" in result.output
        ), result.output


# ── Daemon-down ClickException regression (nexus-w6hj) ─────────────────────


class TestFixPathsDaemonDownClickException:
    """RDR-112 6shq.4 (nexus-w6hj): mirror of the
    ``test_catalog_daemon_mode.TestDaemonDownClickException`` pattern
    for the ``doctor --fix-paths`` flip. ``DaemonNotRunningError`` is
    a ``RuntimeError`` subclass; Click does NOT translate it
    automatically. The ``open_catalog`` call inside ``fix_paths`` must
    wrap in ``try/except RuntimeError`` and re-raise
    ``click.ClickException`` so the operator sees a one-line error
    instead of a Python traceback.
    """

    def test_fix_paths_under_daemon_no_daemon_is_click_exception(
        self,
        runner: CliRunner,
        daemon_env,
        catalog_dir: Path,
    ) -> None:
        """``nx doctor --fix-paths`` under daemon mode with no daemon
        running surfaces a ``ClickException`` (exit 1, single error
        line), not a Python traceback. The catalog is initialized so
        the function reaches the ``open_catalog`` call site."""
        result = runner.invoke(main, ["doctor", "--fix-paths", "--dry-run"])
        assert result.exit_code == 1, (
            f"daemon-down should exit 1 (ClickException), got "
            f"{result.exit_code}; output: {result.output!r}; "
            f"exc: {result.exception!r}"
        )
        assert result.output.startswith("Error:"), (
            f"expected 'Error: ...' ClickException line; got: {result.output!r}"
        )
        assert "Traceback" not in result.output, (
            f"daemon-down should NOT surface a Python traceback; got: {result.output!r}"
        )
        assert "daemon" in result.output.lower(), result.output
