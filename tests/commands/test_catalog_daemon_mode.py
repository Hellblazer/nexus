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

Out of scope (filed as a follow-up): the higher-level ``Catalog``
class still wraps a local ``CatalogDB`` opened against
``.catalog.db``. The Phase 4 spec calls for those reads/writes to
route through ``T2Client.catalog`` (a ``_StoreProxy`` over the eighth
T2 domain store, ``CatalogStore``). That refactor would move
``Catalog`` methods (``find``, ``resolve``, ``get_manifest``, …) onto
``CatalogStore`` so the daemon dispatches them; it is substrate work
beyond the strict yfqv-shaped scope and tracked separately.

We exercise the T3 routing via ``nx catalog backfill-collections
--dry-run`` (read-only path that lists T3 collections through
``_make_t3``) and verify the daemon-mode invocation does not race the
chroma writer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main


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


class TestCatalogBackfillCollections:
    def test_backfill_dry_run_under_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        catalog_dir: Path,
    ) -> None:
        """``nx catalog backfill-collections --dry-run`` calls
        ``_make_t3().list_collections()`` to enumerate T3 collections,
        then reads the catalog ``documents.physical_collection`` column
        to compute the union. Under daemon mode, the T3 call routes
        through ``mcp_infra.get_t3`` to the live daemon.

        The test does not need any pre-seeded T3 collections; an empty
        list is the correct dry-run output (\"Nothing to backfill\")
        and proves the T3 round-trip works."""
        result = runner.invoke(
            main, ["catalog", "backfill-collections", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        # Either "Nothing to backfill" (empty T3 + empty catalog) or
        # a candidate list — both indicate the T3 round-trip succeeded
        # without racing the daemon.
        assert (
            "Nothing to backfill" in result.output
            or "Would register" in result.output
            or "candidates" in result.output.lower()
        ), result.output
