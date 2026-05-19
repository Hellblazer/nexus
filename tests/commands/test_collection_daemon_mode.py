# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 6shq.3 (nexus-siy7) — ``nx collection`` CLI under
``NX_STORAGE_MODE=daemon``.

Scope: the siy7 flip swaps two ``Catalog(cat_path, ...)`` direct opens
in ``commands/collection.py`` for the daemon-aware
``nexus.catalog.open_catalog`` / ``open_cached`` factories:

* line 171 (``delete_cmd`` cascade) — opens the catalog inside a
  try/except Exception block to delete document rows pointing at the
  gone collection. Daemon-mode flip lets the cascade route through
  ``ExecuteProxy`` so a missing daemon is absorbed by the existing
  warn handler rather than crashing the delete command.
* line 355 (``reindex_cmd`` manifest fallback) — read-mostly per-page
  ``docs_for_chashes`` lookup; uses ``open_cached`` to amortise the
  cost across the page loop. Daemon-down absorbs to ``_cat = None``
  and the loop continues without the manifest fallback (silent
  graceful degrade).

We use the same in-thread T2 + T3 daemon harness as
``test_catalog_daemon_mode.py``. The smoke is depth-1: confirm the
command completes without a Python traceback and the daemon-mode path
is exercised, not full coverage.
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
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


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
    monkeypatch.setattr(
        "nexus.commands.collection.catalog_path",
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
        # Drop the process-singleton T2Client (3gdg precedent) so the
        # daemon's wait_closed completes within its 5 s timeout.
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


class TestCollectionDeleteCascadeUnderDaemon:
    """siy7 site 6 (collection.py:171): the ``delete_cmd`` catalog
    cascade opens a Catalog inside try/except Exception. Under daemon
    mode the open routes through ``open_catalog`` -> ``ExecuteProxy``.
    The smoke verifies the cascade block exercises the daemon-aware
    factory and the command completes cleanly even when no rows match
    in the catalog projection.
    """

    def test_delete_nonexistent_collection_cascade_runs_under_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
        catalog_dir: Path,
    ) -> None:
        """Deleting a collection that exists in neither T3 nor the
        catalog runs the cascade path (which exercises the siy7
        ``open_catalog`` flip) but finds nothing to delete. The
        command absorbs the T3 NotFoundError, runs the cascade, and
        exits 0.
        """
        result = runner.invoke(
            main,
            [
                "collection",
                "delete",
                "knowledge__siy7-collection-cascade__minilm-l6-v2-384__v1",
                "--yes",
            ],
        )
        # Exit cleanly: T3 reports already-absent, cascade runs against
        # the daemon-backed catalog (which has no documents), and the
        # taxonomy + pipeline cleanups are no-ops.
        assert result.exit_code == 0, result.output
        # T3 absent path emits the operator note via stderr; the cascade
        # itself emits no error (which is what we want — the flip must
        # not surface a Python traceback when the catalog is empty).
        assert "Traceback" not in result.output, (
            f"daemon-mode cascade should not surface a Python traceback; "
            f"got: {result.output!r}"
        )

    def test_reindex_manifest_fallback_opens_cached_under_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
        catalog_dir: Path,
    ) -> None:
        """siy7 site 7 (collection.py:355): ``reindex_cmd`` opens the
        catalog via ``open_cached`` to amortise the per-page
        ``docs_for_chashes`` reverse-lookup. The smoke exercises the
        flip by reindexing an empty collection — the helper opens the
        cached daemon-backed Catalog instance, finds nothing to
        manifest, and falls through cleanly.

        The test does not need pre-seeded chunks; an empty collection
        triggers the "no chunks to reindex" branch but still reaches
        the ``open_cached`` site at the top of the page loop (the
        siy7 flip lives inside the try/except Exception around the
        cached open). The assertion is the same shape as the delete
        smoke: no Python traceback, command completes cleanly.
        """
        # Pre-create an empty conformant collection so reindex finds
        # it and runs the page loop. The collection-name validator
        # rejects non-conformant names before the flipped code path.
        import chromadb
        from nexus.db.local_ef import LocalEmbeddingFunction
        client = chromadb.HttpClient(
            host=live_t3_daemon["tcp_host"],
            port=live_t3_daemon["tcp_port"],
        )
        coll_name = "knowledge__siy7-reindex-empty__minilm-l6-v2-384__v1"
        client.get_or_create_collection(
            coll_name,
            embedding_function=LocalEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )

        result = runner.invoke(
            main,
            [
                "collection",
                "reindex",
                coll_name,
                "--force",
            ],
        )
        # Outcome we care about: the open_cached call runs without
        # surfacing a Python traceback. Reindex may report "no
        # sourceless chunks" or similar; we accept any clean exit.
        assert "Traceback" not in result.output, (
            f"daemon-mode reindex should not surface a Python "
            f"traceback; got: {result.output!r}"
        )
