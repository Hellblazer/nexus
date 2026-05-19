# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 6shq.3 (nexus-siy7) — ``nx dt`` Catalog opens under
``NX_STORAGE_MODE=daemon``.

Scope: the siy7 flip swaps ``Catalog(cat_path, ...)`` in
``commands/dt.py:_stamp_dt_uri_on_entry`` (line 152) for
``open_catalog`` so the dt-stamp helper runs against the daemon-owned
catalog when ``NX_STORAGE_MODE=daemon`` is set.

The helper has a contract of "log warning, return False on any miss"
rather than raising, so siy7 wraps the ``open_catalog`` call in
``try/except RuntimeError`` and folds daemon-down into the existing
warning path rather than producing a ClickException. Pre-flip this
site would crash with ``DaemonNotRunningError`` because ``Catalog(...)``
itself does not consult ``NX_STORAGE_MODE``.

These tests pin the helper's behaviour under daemon-mode directly
because ``_stamp_dt_uri_on_entry`` is a private function — the public
``nx dt index`` command depends on DEVONthink AppleScript bindings
that are not available in CI. Direct calls avoid the macOS-only
surface while still exercising the siy7 code path.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

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
def catalog_dir(tmp_path: Path, monkeypatch) -> Path:
    """Initialize a real catalog under tmp_path and route
    ``nexus.config.catalog_path`` at it."""
    from nexus.catalog import Catalog
    cd = tmp_path / "catalog"
    cd.mkdir()
    Catalog.init(cd)
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: cd,
    )
    monkeypatch.setattr(
        "nexus.commands.dt.catalog_path",
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
        from nexus.catalog import reset_cache
        reset_cache()
        _stop_daemon(daemon, loop)


# ── Tests ───────────────────────────────────────────────────────────────────


class TestDtStampUnderDaemon:
    """siy7 site 8 (dt.py:152): ``_stamp_dt_uri_on_entry`` opens the
    catalog via the daemon-aware factory under
    ``NX_STORAGE_MODE=daemon``. The smoke pins both branches of the
    contract: daemon-up returns False on a missing row (graceful), and
    daemon-down absorbs the ``DaemonNotRunningError`` into the helper's
    warning path (returns False without raising).
    """

    def test_stamp_returns_false_for_missing_file_path_under_daemon(
        self,
        live_t2_daemon,
        catalog_dir: Path,
    ) -> None:
        """When the daemon is live but the catalog has no entry for
        ``file_path``, the helper logs ``dt_stamp_no_entry_found`` and
        returns False. This exercises the full siy7 daemon-mode path:
        ``open_catalog`` -> ``ExecuteProxy`` -> daemon-side SELECT
        returns no rows -> graceful False.
        """
        from nexus.commands.dt import _stamp_dt_uri_on_entry

        # Path not in the catalog: helper hits the "no row" branch.
        ret = _stamp_dt_uri_on_entry(
            Path("/tmp/siy7-dt-smoke-missing.pdf"),
            "00000000-0000-0000-0000-000000000000",
        )
        assert ret is False

    def test_stamp_returns_false_when_daemon_down(
        self,
        daemon_env,
        catalog_dir: Path,
    ) -> None:
        """When daemon mode is configured but no daemon is running,
        ``open_catalog`` raises ``DaemonNotRunningError`` (a
        ``RuntimeError`` subclass). The siy7 wrap absorbs it into the
        warning path so the helper returns False without raising —
        consistent with the helper's "log and continue" contract for
        upstream callers iterating over a batch of records.
        """
        from nexus.commands.dt import _stamp_dt_uri_on_entry

        # No daemon fixture -> daemon is down. Pre-siy7 this would
        # raise; post-flip the wrap folds it into the existing warning
        # path.
        ret = _stamp_dt_uri_on_entry(
            Path("/tmp/siy7-dt-smoke-daemon-down.pdf"),
            "00000000-0000-0000-0000-000000000001",
        )
        assert ret is False
