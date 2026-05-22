# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P5.A.1 (nexus-9zmpl): T2 catalog store skeleton tests.

Pins the eighth-domain-store invariants without depending on the
P5.A.2 shim conversion:

- ``CatalogStore`` exists in ``nexus.db.t2`` and is lazy-importable.
- ``T2Database.catalog`` lazy-constructs on first access; never
  materialised when not touched (no .catalog.db file appears).
- Daemon dispatch table includes ``catalog.*`` ops.
- T2Client has a ``client.catalog`` proxy attribute.
- ``catalog.execute`` / ``transaction`` / ``bulk_load_documents`` /
  ``rebuild`` are denylisted at the RPC boundary (their shapes do
  not round-trip framed JSON).
- Surface parity: every public method on the underlying
  ``CatalogDB`` that production code uses round-trips through
  ``CatalogStore``.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class TestModuleSurface:
    def test_catalog_store_lazy_importable_from_t2_package(self) -> None:
        """``nexus.db.t2.CatalogStore`` resolves via PEP 562 __getattr__."""
        from nexus.db.t2 import CatalogStore

        assert CatalogStore.__module__ == "nexus.db.t2.catalog"

    def test_catalog_store_listed_in_all(self) -> None:
        import nexus.db.t2 as t2

        assert "CatalogStore" in t2.__all__


class TestT2DatabaseLazyCatalog:
    def test_catalog_not_materialised_until_accessed(
        self, tmp_path: Path,
    ) -> None:
        """Touching only the seven shared-nexus.db stores must not
        open ``.catalog.db``."""
        from nexus.db.t2 import T2Database

        nexus_db = tmp_path / "memory.db"
        catalog_db = tmp_path / "catalog" / ".catalog.db"
        db = T2Database(nexus_db, catalog_db_path=catalog_db)
        try:
            # Force one of the existing seven stores.
            db.memory.put("p", "t", "content")
            # Catalog file must not exist yet — lazy property
            # never fired.
            assert not catalog_db.exists()
            assert db._catalog is None
        finally:
            db.close()

    def test_catalog_property_materialises_store(
        self, tmp_path: Path,
    ) -> None:
        from nexus.db.t2 import T2Database
        from nexus.db.t2.catalog import CatalogStore

        nexus_db = tmp_path / "memory.db"
        catalog_db = tmp_path / "catalog" / ".catalog.db"
        db = T2Database(nexus_db, catalog_db_path=catalog_db)
        try:
            store = db.catalog
            assert isinstance(store, CatalogStore)
            assert store.path == catalog_db
            # Second access returns the same instance (no double-open).
            assert db.catalog is store
        finally:
            db.close()

    def test_catalog_db_path_parent_auto_created(
        self, tmp_path: Path,
    ) -> None:
        """Daemon-startup path: catalog dir may not exist; the store
        materialises the parent on construction."""
        from nexus.db.t2 import T2Database

        nexus_db = tmp_path / "memory.db"
        catalog_db = tmp_path / "fresh-catalog-dir" / ".catalog.db"
        assert not catalog_db.parent.exists()
        db = T2Database(nexus_db, catalog_db_path=catalog_db)
        try:
            _ = db.catalog
            assert catalog_db.parent.is_dir()
            assert catalog_db.exists()
        finally:
            db.close()


class TestDaemonDispatch:
    def test_catalog_registered_in_store_attrs(self) -> None:
        from nexus.daemon.t2_daemon import _T2_STORE_ATTRS

        assert "catalog" in _T2_STORE_ATTRS
        assert _T2_STORE_ATTRS[-1] == "catalog", (
            "catalog must be the eighth (last) store per RDR-120 P5"
        )

    def test_catalog_rpc_denyops_include_unserialisable_methods(
        self,
    ) -> None:
        from nexus.daemon.t2_daemon import _RPC_DENY_OPS

        for op in (
            "catalog.execute",
            "catalog.transaction",
            "catalog.bulk_load_documents",
            "catalog.rebuild",
        ):
            assert op in _RPC_DENY_OPS, (
                f"{op!r} must be denylisted — its shape does not "
                f"round-trip framed JSON"
            )

    def test_dispatch_table_builds_with_catalog_store(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end: a real T2Database with catalog property
        registered builds a dispatch table that includes catalog ops
        for the methods that DO round-trip JSON.
        """
        from nexus.daemon.t2_daemon import _build_dispatch_table
        from nexus.db.t2 import T2Database

        db = T2Database(
            tmp_path / "memory.db",
            catalog_db_path=tmp_path / "catalog" / ".catalog.db",
        )
        try:
            table = _build_dispatch_table(db)
            # next_document_number / search / descendants ARE valid
            # RPC ops (return JSON-shaped values).
            assert "catalog.next_document_number" in table
            assert "catalog.search" in table
            assert "catalog.descendants" in table
            # Denylisted ops MUST be absent.
            for op in (
                "catalog.execute",
                "catalog.transaction",
                "catalog.bulk_load_documents",
                "catalog.rebuild",
            ):
                assert op not in table, f"{op!r} should be denylisted"
        finally:
            db.close()


class TestT2ClientProxy:
    def test_t2client_has_catalog_proxy(self) -> None:
        from nexus.daemon.t2_client import T2Client

        client = T2Client(skip_handshake=True)
        try:
            assert hasattr(client, "catalog")
            # Method names build the right op via __getattr__.
            method = client.catalog.search
            assert callable(method)
        finally:
            client.close()


class TestCatalogStoreSurfaceParity:
    """Every public ``CatalogDB`` method that production code uses
    must round-trip through ``CatalogStore``.
    """

    @pytest.fixture
    def store(self, tmp_path: Path):
        from nexus.db.t2.catalog import CatalogStore

        s = CatalogStore(tmp_path / "catalog" / ".catalog.db")
        yield s
        s.close()

    def test_next_document_number_round_trip(self, store) -> None:
        # Fresh owner_prefix with no documents — MAX(...) is NULL,
        # so (NULL or 0) + 1 = 1. The method does not allocate the
        # number, just reports what the next free slot is.
        assert store.next_document_number("nexus-test1") == 1
        # Repeat without inserting documents — still 1.
        assert store.next_document_number("nexus-test1") == 1

    def test_search_returns_list(self, store) -> None:
        rows = store.search("nonexistent")
        assert rows == []

    def test_descendants_returns_list(self, store) -> None:
        rows = store.descendants("nexus-zzzz")
        assert rows == []

    def test_execute_returns_cursor(self, store) -> None:
        import sqlite3

        cur = store.execute("SELECT 1")
        assert isinstance(cur, sqlite3.Cursor)
        assert cur.fetchone() == (1,)

    def test_transaction_context_manager(self, store) -> None:
        # Just confirm the context manager protocol works; behaviour
        # parity is locked by the underlying CatalogDB tests.
        with store.transaction():
            store.execute("SELECT 1")

    def test_backfilled_collections_attribute_present(self, store) -> None:
        # Fresh store: no collections to backfill.
        assert store.backfilled_collections == set()
