# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.3 (nexus-mmvf) — ``nx doc`` CLI under
``NX_STORAGE_MODE=daemon``.

``commands/doc.py`` has one direct T3 site, inside the
``_phase4_catalog_t3_chash`` factory helper (~line 564). Every
``nx doc render`` / ``nx doc cite`` invocation that needs T3 goes
through this helper. The mmvf flip routes the helper's T3 source
through ``mcp_infra.get_t3`` so daemon mode returns the daemon's
``HttpClient``.

RDR-112 6shq.2 (nexus-3gdg) augmentation: the helper's Catalog open
now also routes through the daemon (via ``open_cached`` ->
``open_catalog`` -> ``ExecuteProxy``). The fixture spawns both a T2
daemon and a T3 daemon so the helper resolves without raising
``DaemonNotRunningError`` on the catalog side. ChashIndex remains
out-of-scope for 3gdg (tracked under 6shq.4).
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

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
def t2db(tmp_path: Path) -> T2Database:
    db = T2Database(tmp_path / "memory.db")
    yield db
    db.close()


@pytest.fixture
def live_t2_daemon(t2db: T2Database, config_dir: Path, daemon_env):
    # RDR-112 6shq.2 (nexus-3gdg): doc command's catalog open now
    # routes through the daemon under daemon mode; T2 daemon required.
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        yield daemon
    finally:
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


@pytest.fixture
def initialized_catalog(config_dir: Path):
    """``_phase4_catalog_t3_chash`` opens a catalog at
    ``default_db_path().parent / catalog``. Under the test's
    ``NEXUS_CONFIG_DIR`` override that resolves to
    ``config_dir / catalog``. The factory does NOT auto-create the
    catalog dir; pre-initialize one so the smoke test reaches the
    T3 branch without choking on a missing ``.catalog.db``."""
    from nexus.catalog import Catalog
    cat_dir = config_dir / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    Catalog.init(cat_dir)
    return cat_dir


class TestDocPhase4Factory:
    def test_phase4_factory_resolves_t3_via_daemon(
        self,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
        initialized_catalog: Path,
    ) -> None:
        """``_phase4_catalog_t3_chash`` returns a ``(catalog, t3,
        chash_index)`` trio. Under daemon mode:

        * the ``t3`` slot is a ``T3Database`` whose underlying client
          is the daemon's ``HttpClient`` (mmvf scope)
        * the ``catalog`` slot is a Catalog backed by an
          ``ExecuteProxy`` over ``T2Client.catalog`` (RDR-112 6shq.2
          / nexus-3gdg scope)

        The ``chash_index`` slot still opens ``ChashIndex`` directly
        and is out-of-scope until 6shq.4 (it tracks T2's chash table,
        a separate seam from the catalog flip)."""
        from nexus.commands.doc import _phase4_catalog_t3_chash
        cat, t3, _chash = _phase4_catalog_t3_chash()
        # nexus-mmvf review Minor: chromadb factories return the same
        # Client class; use the client identifier (host:port vs path)
        # as the daemon-vs-persistent discriminator.
        client_id = getattr(t3._client, "_identifier", "") or ""
        assert "/" not in client_id, (
            f"client identifier {client_id!r} looks like a filesystem "
            f"path — the seam likely returned a PersistentClient under "
            f"daemon mode, racing the daemon writer."
        )
        # 3gdg review Suggestion-2: assert the catalog arrived via the
        # daemon proxy. ``_daemon_proxy`` is the Catalog flag set in
        # ``Catalog.__init__`` when ``db=`` is an ExecuteProxy
        # instance. A False here would mean the factory bypassed
        # ``open_cached`` / ``open_catalog`` and constructed a direct
        # CatalogDB — a regression on the 3gdg flip.
        assert getattr(cat, "_daemon_proxy", False), (
            "catalog should be proxy-backed in daemon mode after "
            "RDR-112 6shq.2 / nexus-3gdg; got direct CatalogDB"
        )
        # The helper's T3 read surface must work end-to-end against
        # the daemon (empty list is the correct answer for a fresh
        # daemon with no collections).
        cols = t3.list_collections()
        assert isinstance(cols, list)
