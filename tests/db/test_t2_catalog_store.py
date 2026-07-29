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


# NO TestDaemonDispatch / TestT2ClientProxy: they pinned the CatalogStore's
# registration in the T2 daemon's `_T2_STORE_ATTRS`, its `_RPC_DENY_OPS`
# entries (methods whose shapes cannot round-trip framed JSON), the dispatch
# table composition, and the `client.catalog` proxy. All four are properties of
# the daemon RPC boundary, which is deleted (nexus-i711w Stage 2 sub-stage B).
#
# The store itself SURVIVES to sub-stage A, and its own invariants — lazy
# import, lazy materialisation, parent-dir creation, and the read/write surface
# parity below — are unaffected and still covered above and below.


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
