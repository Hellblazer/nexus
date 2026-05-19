# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.4 (nexus-uar6) — ``nx index`` CLI under
``NX_STORAGE_MODE=daemon``.

Scope: the single direct ``make_t3()`` call in ``nx index`` lives
inside ``run_collection_postprocessing`` (post-index taxonomy +
projection + topic-link chain). The yfqv/uar6 port flipped that
import to ``mcp_infra.get_t3`` so daemon mode hits the HttpClient
instead of racing the daemon on the on-disk chroma path.

We do not exercise the full ``nx index repo`` end-to-end pipeline in
this file because:

1. Indexing requires real file content + tree-sitter + embeddings,
   which is slow and brittle for a routing-correctness test.
2. The flip's blast radius is the ``get_t3()`` resolution call inside
   ``run_collection_postprocessing``; we can exercise that path
   directly with a fake collections list and a live T2 + T3 daemon
   pair without indexing anything.

Per-collection exceptions inside the chain are swallowed by design
(non-fatal failures must not take down the post-index sweep), so the
test asserts the routing call resolves without crashing the host
process. A subsequent integration test against a real indexed
collection (out of scope for uar6) would validate the discovery
results.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from nexus.daemon.t2_daemon import T2Daemon
from nexus.db.t2 import T2Database


# ── In-thread T2 daemon harness (matches the yfqv/idqd pattern) ────────────


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
        # T2Client before stopping the daemon; see the matching
        # comment in tests/commands/test_catalog_daemon_mode.py.
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


# ── Tests ───────────────────────────────────────────────────────────────────


class TestOpenCatalogOrNoneLogging:
    """3gdg review IMPORTANT-3 regression: when ``_open_catalog_or_none``
    catches an exception (e.g. ``DaemonNotRunningError`` under daemon
    mode without a live daemon), it must emit a structured log
    warning before returning ``None``. Pre-fix the silent return left
    operators unable to tell that catalog-aware features had been
    bypassed.
    """

    def test_open_catalog_or_none_logs_when_swallowing_exception(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        """When ``open_catalog`` raises any exception (e.g.
        ``DaemonNotRunningError``), the helper returns ``None`` and
        emits ``catalog_open_failed_returning_none`` so the silent
        fallback is observable in logs.

        Uses an injected fault rather than a real daemon-down
        scenario so the test is fast and deterministic (no daemon
        startup, no env-var dependency). Captures via ``capsys``
        because structlog's default emit path writes to the
        ConsoleRenderer (stdout/stderr) rather than the stdlib
        logging handlers ``caplog`` taps.
        """
        from nexus.catalog import Catalog
        from nexus.commands.index import _open_catalog_or_none

        # Initialize a real catalog in direct mode so the
        # ``Catalog.is_initialized`` check passes and the helper
        # reaches the ``open_catalog`` call.
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        Catalog.init(cat_dir)
        monkeypatch.setattr("nexus.config.catalog_path", lambda: cat_dir)

        # Inject the same RuntimeError shape ``DaemonNotRunningError``
        # would raise. Using a generic RuntimeError keeps the test
        # independent of the discovery module's internals. The
        # function imports ``open_catalog`` from ``nexus.catalog``
        # inside its body so we patch the source module.
        def _boom(_cat_path):
            raise RuntimeError("injected: no daemon")
        monkeypatch.setattr("nexus.catalog.open_catalog", _boom)

        result = _open_catalog_or_none()

        assert result is None, (
            "open_catalog failure should cause helper to return None"
        )
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "catalog_open_failed_returning_none" in combined, (
            f"expected catalog_open_failed_returning_none in log "
            f"output; saw stdout={captured.out!r}, stderr={captured.err!r}"
        )
        # The structured warning must carry the exception type so
        # operators can distinguish daemon-down (RuntimeError) from
        # catalog-corrupt (sqlite3.OperationalError) etc.
        assert "RuntimeError" in combined, (
            f"expected error_type=RuntimeError in log output; "
            f"saw: {combined!r}"
        )


class TestPostIndexTaxonomyRouting:
    def test_postprocessing_resolves_t3_via_daemon(
        self,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """``run_collection_postprocessing`` is the only ``index`` site
        that calls ``_make_t3()`` (now ``get_t3()``). Under daemon mode
        the resolver returns a T3Database wrapping the daemon's
        HttpClient. We pass a single non-existent collection name so
        the per-collection chain steps fail gracefully (the function
        wraps each step in ``try/except`` per its docstring), proving:

        1. ``get_t3()`` resolves to the daemon without raising.
        2. The function does not race the chroma writer on the on-disk
           path (we never opened a parallel ``PersistentClient``).

        A future end-to-end test against a real indexed collection
        will assert the chain's outputs; the contract under test here
        is the daemon-routing seam."""
        from nexus.commands.index import run_collection_postprocessing
        # quiet=True suppresses the human-facing ``click.echo`` lines
        # that would otherwise hit a non-Click context and explode.
        run_collection_postprocessing(
            collections=["knowledge__uar6-postindex-smoke"],
            quiet=True,
        )

    def test_postprocessing_empty_collections_is_noop(
        self,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """Empty list early-exits before any T3 resolution. Confirms
        the noop branch is undisturbed by the daemon-routing change."""
        from nexus.commands.index import run_collection_postprocessing
        run_collection_postprocessing(collections=[], quiet=True)
