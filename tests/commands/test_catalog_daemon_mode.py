# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.4 (nexus-uar6) — ``nx catalog`` CLI under
``NX_STORAGE_MODE=daemon``.

Scope: this file covers the T3-side catalog port. Several ``nx
catalog`` subcommands call ``_make_t3()`` (now daemon-aware via
``mcp_infra.get_t3``) to enumerate or write T3 collections. Without
the daemon-aware seam, the legacy ``make_t3()`` factory opens a
``PersistentClient`` on the same on-disk path the T3 daemon owns —
the chroma writer cannot tolerate two live processes on the same
DuckDB+Parquet store.

RDR-112 6shq.2 (nexus-3gdg) augmentation: now that ``_get_catalog``
also routes through the daemon (via ``open_catalog``), every CLI verb
in this file needs both a live T2 daemon AND a live T3 daemon. The
fixture ``live_t2_and_t3`` spawns both in-thread.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.t2_daemon import T2Daemon
from nexus.db.t2 import T2Database


# ── In-thread T2 daemon harness (matches yfqv/idqd/lj2l pattern) ───────────


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
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def catalog_dir(tmp_path: Path, monkeypatch) -> Path:
    """Initialize a real catalog under tmp_path and route
    ``nexus.config.catalog_path`` at it for the test."""
    from nexus.catalog import Catalog
    cd = tmp_path / "catalog"
    cd.mkdir()
    Catalog.init(cd)
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: cd,
    )
    monkeypatch.setattr(
        "nexus.commands.catalog.catalog_path",
        lambda: cd,
        raising=False,
    )
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
        # RDR-112 6shq.2 (nexus-3gdg): drop the process-singleton
        # T2Client before stopping the daemon so the daemon's
        # server.wait_closed completes within its 5 s timeout. Without
        # this, the orphan client sockets held by the catalog cache
        # block the daemon's accept loop from finishing teardown.
        from nexus.catalog import reset_cache
        reset_cache()
        _stop_daemon(daemon, loop)


@pytest.fixture
def live_t3_daemon(daemon_env, config_dir: Path, local_path: Path):
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── Tests ───────────────────────────────────────────────────────────────────


class TestDaemonDownClickException:
    """3gdg review IMPORTANT-1 regression: ``DaemonNotRunningError`` is
    a ``RuntimeError`` subclass; Click does NOT translate it
    automatically. ``_get_catalog`` and the two direct opens in
    ``setup_cmd`` / ``_run_t3_doc_id_coverage`` must wrap the
    ``open_catalog`` call in ``try/except RuntimeError`` and re-raise
    ``click.ClickException`` so the operator sees a one-line message
    instead of a Python traceback.
    """

    def test_get_catalog_under_daemon_no_daemon_is_click_exception(
        self,
        runner: CliRunner,
        daemon_env,
        catalog_dir: Path,
    ) -> None:
        """``nx catalog list`` under daemon mode with no daemon
        running surfaces a ``ClickException`` (exit code 1, single
        message line), not a Python traceback."""
        result = runner.invoke(main, ["catalog", "list"])
        # ClickException -> exit_code 1 with the message printed to
        # output as a single ``Error: ...`` line. CliRunner translates
        # ClickException to ``SystemExit(1)`` so we can't isinstance-
        # check the exception class; instead we verify the output
        # shape that operators actually see.
        assert result.exit_code == 1, (
            f"daemon-down should exit 1 (ClickException), got "
            f"{result.exit_code}; output: {result.output!r}; "
            f"exc: {result.exception!r}"
        )
        # The output must be a single ``Error: ...`` line, not a
        # multi-line Python traceback. Pre-fix the operator saw the
        # full ``DaemonNotRunningError`` stack dump because Click does
        # not translate ``RuntimeError`` subclasses automatically.
        assert result.output.startswith("Error:"), (
            f"expected 'Error: ...' ClickException line; got: {result.output!r}"
        )
        assert "Traceback" not in result.output, (
            f"daemon-down should NOT surface a Python traceback; got: {result.output!r}"
        )
        # The original DaemonNotRunningError message points operators
        # at ``nx daemon t2 start`` — that hint must survive the
        # translation.
        assert "daemon" in result.output.lower(), result.output


class TestCatalogBackfillCollections:
    def test_backfill_dry_run_under_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
        catalog_dir: Path,
    ) -> None:
        """``nx catalog backfill-collections --dry-run`` calls
        ``_make_t3().list_collections()`` to enumerate T3 collections,
        then reads the catalog ``documents.physical_collection`` column
        to compute the union. Under daemon mode, BOTH the T3 call
        routes through ``mcp_infra.get_t3`` AND the catalog read
        routes through ``open_catalog`` -> ``ExecuteProxy`` (RDR-112
        6shq.2, nexus-3gdg).

        The test does not need any pre-seeded T3 collections; an empty
        list is the correct dry-run output (\"Nothing to backfill\")
        and proves both round-trips work."""
        result = runner.invoke(
            main, ["catalog", "backfill-collections", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        # Either "Nothing to backfill" (empty T3 + empty catalog) or
        # a candidate list — both indicate the round-trip succeeded
        # without racing the daemon.
        assert (
            "Nothing to backfill" in result.output
            or "Would register" in result.output
            or "candidates" in result.output.lower()
        ), result.output
