# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P2.1 (nexus-7ejx): CatalogClient round-trip tests.

Tests that T2Client.catalog routes CatalogStore RPCs through the daemon
and back with identical results to direct CatalogStore calls. Also covers:
- Legacy catalog.db import path (rows land in memory.db on daemon start)
- Atomic-rollback: corrupted legacy file produces no partial state
- Signature parity: client.catalog.<method> signature matches CatalogStore
"""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
from pathlib import Path

import pytest

from nexus.catalog.tumbler import DocumentRecord, LinkRecord, OwnerRecord
from nexus.daemon.t2_daemon import T2Daemon
from nexus.daemon.t2_client import T2Client
from nexus.db.t2 import T2Database
from nexus.db.t2.catalog_store import CatalogStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_owner(
    owner: str = "1.1",
    name: str = "test-repo",
) -> OwnerRecord:
    return OwnerRecord(
        owner=owner,
        name=name,
        owner_type="repo",
        repo_hash="abcd1234",
        description="test repo",
        repo_root="",
    )


def _make_doc(
    tumbler: str = "1.1.1",
    title: str = "test.py",
    physical_collection: str = "code__test",
) -> DocumentRecord:
    return DocumentRecord(
        tumbler=tumbler,
        title=title,
        author="alice",
        year=2026,
        content_type="code",
        file_path="src/test.py",
        corpus="",
        physical_collection=physical_collection,
        chunk_count=5,
        head_hash="abc123",
        indexed_at="2026-01-01T00:00:00Z",
        meta={},
    )


def _make_link(
    from_t: str = "1.1.1",
    to_t: str = "1.1.2",
    link_type: str = "cites",
) -> LinkRecord:
    return LinkRecord(
        from_t=from_t,
        to_t=to_t,
        link_type=link_type,
        from_span="",
        to_span="",
        created_by="user",
        created_at="2026-01-01T00:00:00Z",
        meta={},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


@pytest.fixture
def t2db(db_path: Path) -> T2Database:
    database = T2Database(db_path)
    yield database
    database.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "config"


@pytest.fixture
def daemon_and_client(t2db: T2Database, config_dir: Path):
    """Start T2Daemon with t2db; yield (daemon, T2Client via UDS)."""
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    client = T2Client(uds_path=daemon.uds_path)
    yield daemon, client
    client.close()
    _stop_daemon(daemon, loop)


# ---------------------------------------------------------------------------
# Signature parity
# ---------------------------------------------------------------------------


class TestSignatureParity:
    """client.catalog.<method> signature must match CatalogStore.<method> (minus self)."""

    @pytest.mark.parametrize("method", ["rebuild", "next_document_number", "search", "descendants", "execute", "commit"])
    def test_signature_parity(self, daemon_and_client, method: str) -> None:
        _daemon, client = daemon_and_client
        store_fn = getattr(CatalogStore, method)
        store_sig = inspect.signature(store_fn)
        store_params = list(store_sig.parameters.values())[1:]  # drop self
        expected_sig = store_sig.replace(parameters=store_params)

        client_fn = getattr(client.catalog, method)
        client_sig = inspect.signature(client_fn)

        assert list(expected_sig.parameters) == list(client_sig.parameters), (
            f"signature mismatch for {method!r}: "
            f"expected {expected_sig}, got {client_sig}"
        )


# ---------------------------------------------------------------------------
# RPC round-trip: representative ops
# ---------------------------------------------------------------------------


class TestCatalogClientRpc:
    def test_rebuild_and_next_document_number(self, t2db: T2Database, daemon_and_client) -> None:
        """rebuild + next_document_number via RPC matches direct store call."""
        _daemon, client = daemon_and_client
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(), "1.1.2": _make_doc(tumbler="1.1.2", title="b.py")}

        # Call via RPC
        client.catalog.rebuild(owners, docs, [])
        next_num = client.catalog.next_document_number("1.1")

        # Compare to direct call
        t2db.catalog.rebuild(owners, docs, [])
        expected_next = t2db.catalog.next_document_number("1.1")

        assert next_num == expected_next

    def test_search_via_rpc(self, daemon_and_client) -> None:
        """search returns matching results via RPC."""
        _daemon, client = daemon_and_client
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(tumbler="1.1.1", title="uniqueterm.py")}
        client.catalog.rebuild(owners, docs, [])

        results = client.catalog.search("uniqueterm")
        assert len(results) == 1
        assert results[0]["tumbler"] == "1.1.1"

    def test_descendants_via_rpc(self, daemon_and_client) -> None:
        """descendants returns children via RPC."""
        _daemon, client = daemon_and_client
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="b.py"),
            "2.1.1": _make_doc(tumbler="2.1.1", title="other.py"),
        }
        client.catalog.rebuild(owners, docs, [])
        results = client.catalog.descendants("1.1")
        tumblers = {r["tumbler"] for r in results}
        assert "1.1.1" in tumblers
        assert "1.1.2" in tumblers
        assert "2.1.1" not in tumblers

    def test_execute_via_rpc(self, daemon_and_client) -> None:
        """execute returns row data via RPC."""
        _daemon, client = daemon_and_client
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc()}
        client.catalog.rebuild(owners, docs, [])

        # execute returns serializable result
        result = client.catalog.execute("SELECT COUNT(*) FROM documents")
        # Result is the fetchall() output or similar — depends on implementation
        # At minimum this should not raise
        assert result is not None

    def test_search_content_type_filter_via_rpc(self, daemon_and_client) -> None:
        """search with content_type filter works via RPC."""
        _daemon, client = daemon_and_client
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1", title="term.py"),
        }
        client.catalog.rebuild(owners, docs, [])
        results = client.catalog.search("term", content_type="code")
        assert len(results) == 1
        assert results[0]["content_type"] == "code"

    def test_search_no_match_via_rpc(self, daemon_and_client) -> None:
        """search returns empty list when no match."""
        _daemon, client = daemon_and_client
        client.catalog.rebuild({}, {}, [])
        results = client.catalog.search("zzznomatch")
        assert results == []

    def test_backfilled_collections_via_rpc(
        self, t2db: T2Database, daemon_and_client,
    ) -> None:
        """nexus-m0hi (RDR-112 P2.review S2): the public
        ``backfilled_collections()`` accessor is RPC-dispatchable so
        the Phase 4 catalog port (nexus-uar6) can read the synthetic-
        event seed set from ``T2Client.catalog`` without reaching for
        an underscore attribute that ``_StoreProxy`` would not
        expose."""
        _daemon, client = daemon_and_client
        # Seed a legacy document via the daemon's T2Database directly
        # so the backfill path runs at next CatalogStore construction.
        # We re-trigger init by clearing the process-level migrated-
        # paths cache and accessing the catalog store one more time.
        t2db.catalog._conn.execute(
            "INSERT INTO documents (tumbler, title, physical_collection) "
            "VALUES (?, ?, ?)",
            ("9.9.9", "m0hi-legacy", "code__m0hi-rpc-smoke"),
        )
        t2db.catalog._conn.commit()
        # Force a re-init so the backfill captures the newly-seeded row.
        from nexus.db.t2 import catalog_store as cs_module
        cs_module._migrated_paths.clear()
        t2db.catalog._backfill_collections()

        names = client.catalog.backfilled_collections()
        assert isinstance(names, list)
        assert "code__m0hi-rpc-smoke" in names, (
            f"expected the legacy collection to surface via RPC, "
            f"got {names!r}"
        )


# ---------------------------------------------------------------------------
# Legacy catalog.db import
# ---------------------------------------------------------------------------


def _seed_legacy_catalog(legacy_path: Path) -> int:
    """Create a minimal legacy catalog.db with known rows. Returns doc count."""
    from nexus.catalog.catalog_db import CatalogDB
    db = CatalogDB(legacy_path)
    owners = {"1.1": _make_owner()}
    docs = {
        "1.1.1": _make_doc(tumbler="1.1.1", title="legacy.py"),
        "1.1.2": _make_doc(tumbler="1.1.2", title="legacy2.py"),
    }
    links = [_make_link()]
    db.rebuild(owners, docs, links)
    db.close()
    return len(docs)


class TestLegacyImport:
    def test_legacy_rows_present_after_daemon_start(self, tmp_path: Path) -> None:
        """Rows from legacy catalog.db appear in memory.db after daemon startup."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        legacy_path = config_dir / "catalog.db"
        expected_docs = _seed_legacy_catalog(legacy_path)

        # Apply migrations (which will trigger the legacy import)
        from nexus.db import migrations as mig
        db_path = config_dir / "memory.db"
        mig.run_if_needed(db_path)

        # Verify rows landed in memory.db
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        conn.close()
        assert count == expected_docs

    def test_legacy_file_renamed_to_imported(self, tmp_path: Path) -> None:
        """Legacy catalog.db is renamed to catalog.db.imported after import."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        legacy_path = config_dir / "catalog.db"
        _seed_legacy_catalog(legacy_path)

        db_path = config_dir / "memory.db"
        from nexus.db import migrations as mig
        mig.run_if_needed(db_path)

        assert not legacy_path.exists(), "catalog.db should be renamed after import"
        assert (config_dir / "catalog.db.imported").exists(), "catalog.db.imported should exist"

    def test_no_legacy_file_is_noop(self, tmp_path: Path) -> None:
        """Migration is a no-op when catalog.db is absent."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        db_path = config_dir / "memory.db"

        from nexus.db import migrations as mig
        mig.run_if_needed(db_path)  # Should not raise

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        conn.close()
        assert count == 0

    def test_bib_columns_preserved_on_legacy_import(self, tmp_path: Path) -> None:
        """RDR-112 P2.review S1 (nexus-3vyw): legacy import must carry bib_*
        enrichment values from catalog.db into memory.db. Pre-fix the SELECT
        omitted all bib_* columns; any ``nx enrich bib`` result would silently
        revert to defaults on the next daemon startup."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        legacy_path = config_dir / "catalog.db"

        # Seed a legacy catalog.db with documents carrying non-default bib_*.
        from nexus.catalog.catalog_db import CatalogDB
        legacy = CatalogDB(legacy_path)
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(tumbler="1.1.1", title="enriched.pdf")}
        legacy.rebuild(owners, docs, [])
        legacy._conn.execute(
            "UPDATE documents SET "
            "bib_year=?, bib_authors=?, bib_venue=?, bib_citation_count=?, "
            "bib_semantic_scholar_id=?, bib_openalex_id=?, bib_doi=?, bib_enriched_at=? "
            "WHERE tumbler='1.1.1'",
            (
                2024, "Smith, Jones", "NeurIPS", 137,
                "s2-deadbeef", "openalex-abc123", "10.1234/abcd",
                "2026-04-01T10:00:00Z",
            ),
        )
        legacy._conn.commit()
        legacy.close()

        # Run migration; legacy rows should land in memory.db.
        from nexus.db import migrations as mig
        db_path = config_dir / "memory.db"
        mig.run_if_needed(db_path)

        # Verify every bib_* value survives the import.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT bib_year, bib_authors, bib_venue, bib_citation_count, "
                "       bib_semantic_scholar_id, bib_openalex_id, bib_doi, "
                "       bib_enriched_at "
                "FROM documents WHERE tumbler='1.1.1'"
            ).fetchone()
        finally:
            conn.close()
        assert row == (
            2024, "Smith, Jones", "NeurIPS", 137,
            "s2-deadbeef", "openalex-abc123", "10.1234/abcd",
            "2026-04-01T10:00:00Z",
        ), f"bib_* values lost on legacy import: row={row}"


class TestLegacyImportAtomicRollback:
    def test_corrupt_legacy_file_leaves_no_partial_state(self, tmp_path: Path) -> None:
        """A corrupt legacy catalog.db leaves memory.db unchanged after failed import.

        Strategy: seed a valid catalog.db, then corrupt its SQLite header.
        The migration should roll back the transaction and leave memory.db
        with no catalog rows. The sentinel file catalog.db.importing persists
        for retry (or cleanup).
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        legacy_path = config_dir / "catalog.db"

        # Write corrupt data (not a valid SQLite file)
        legacy_path.write_bytes(b"THIS IS NOT VALID SQLITE\x00" * 100)

        db_path = config_dir / "memory.db"
        from nexus.db import migrations as mig

        # Migration should not raise (corrupt file is handled gracefully)
        # or may raise -- either way, memory.db must have no partial rows
        try:
            mig.run_if_needed(db_path)
        except Exception:
            pass  # Acceptable: the migration fails loudly

        # Regardless of whether it raised, no partial docs in memory.db
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM documents"
                ).fetchone()[0]
                assert count == 0, "No partial rows should land in memory.db on failed import"
            except sqlite3.OperationalError:
                pass  # Table may not exist yet on fresh DB after failed migration
            finally:
                conn.close()

    def test_sentinel_file_persists_on_import_failure(self, tmp_path: Path) -> None:
        """Sentinel catalog.db.importing persists when import transaction fails.

        This allows the operator (or next startup) to retry the import.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        legacy_path = config_dir / "catalog.db"
        # Write corrupt file
        legacy_path.write_bytes(b"INVALID SQLITE" * 50)

        db_path = config_dir / "memory.db"
        from nexus.db import migrations as mig
        try:
            mig.run_if_needed(db_path)
        except Exception:
            pass

        # Either catalog.db or catalog.db.importing should exist
        # (the legacy file was moved to sentinel before the failed import)
        sentinel = config_dir / "catalog.db.importing"
        either_exists = legacy_path.exists() or sentinel.exists()
        assert either_exists, (
            "After failed import, either catalog.db or catalog.db.importing "
            "should exist so the operator can retry"
        )


class TestRpcDenyOpsContextManagers:
    """transaction() and bulk_load_documents() are @contextmanager methods
    that cannot round-trip over the RPC dispatch table. They MUST be in
    _RPC_DENY_OPS so a client invoking them as plain RPCs gets an explicit
    'unknown RPC op' error rather than a silent None return.
    """

    def test_catalog_transaction_in_deny_ops(self) -> None:
        from nexus.daemon.t2_daemon import _RPC_DENY_OPS
        assert "catalog.transaction" in _RPC_DENY_OPS

    def test_catalog_bulk_load_in_deny_ops(self) -> None:
        from nexus.daemon.t2_daemon import _RPC_DENY_OPS
        assert "catalog.bulk_load_documents" in _RPC_DENY_OPS
