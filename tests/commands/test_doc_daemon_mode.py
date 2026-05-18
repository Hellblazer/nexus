# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.3 (nexus-mmvf) — ``nx doc`` CLI under
``NX_STORAGE_MODE=daemon``.

``commands/doc.py`` has one direct T3 site, inside the
``_phase4_catalog_t3_chash`` factory helper (~line 564). Every
``nx doc render`` / ``nx doc cite`` invocation that needs T3 goes
through this helper. The mmvf flip routes the helper's T3 source
through ``mcp_infra.get_t3`` so daemon mode returns the daemon's
``HttpClient``.

The helper also constructs a Catalog (``open_cached``) and a
``ChashIndex`` directly off ``default_db_path()``. Those two direct
opens race the daemon under daemon mode and are tracked separately
under nexus-6shq (the broader Catalog -> T2Client.catalog refactor);
mmvf flips only the T3 source here.
"""
from __future__ import annotations

from pathlib import Path

import pytest


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
        live_t3_daemon,
        reset_t3_singleton,
        initialized_catalog: Path,
    ) -> None:
        """``_phase4_catalog_t3_chash`` returns a ``(catalog, t3,
        chash_index)`` trio. Under daemon mode the ``t3`` slot must be
        a ``T3Database`` whose underlying client is the daemon's
        ``HttpClient``. The catalog + chash_index slots are out-of-
        scope for mmvf (tracked under nexus-6shq); we only assert on
        the T3 client class."""
        from nexus.commands.doc import _phase4_catalog_t3_chash
        _cat, t3, _chash = _phase4_catalog_t3_chash()
        # nexus-mmvf review Minor: chromadb factories return the same
        # Client class; use the client identifier (host:port vs path)
        # as the daemon-vs-persistent discriminator.
        client_id = getattr(t3._client, "_identifier", "") or ""
        assert "/" not in client_id, (
            f"client identifier {client_id!r} looks like a filesystem "
            f"path — the seam likely returned a PersistentClient under "
            f"daemon mode, racing the daemon writer."
        )
        # The helper's T3 read surface must work end-to-end against
        # the daemon (empty list is the correct answer for a fresh
        # daemon with no collections).
        cols = t3.list_collections()
        assert isinstance(cols, list)
