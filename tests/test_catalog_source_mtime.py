# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8luh — ``catalog.documents`` carries ``source_mtime`` at index time.

Three surfaces are under test:

  1. ``DocumentRecord`` + ``CatalogEntry`` dataclass round-trip.
  2. ``CatalogDB`` schema — fresh install has the column; ALTER-on-open
     migration adds it to pre-8luh databases without data loss.
  3. ``Catalog.register`` / ``Catalog.update`` / ``Catalog.by_file_path`` /
     ``Catalog.resolve`` / ``Catalog.by_doc_id`` / ``Catalog.delete_document``
     round-trip source_mtime through JSONL + SQLite.

Adds a column-exists smoke test so downstream consumers (RDR-087 Phase
3.4 stale_source_ratio) can assume the schema is present.

CATALOG SUBSTRATE (nexus-i711w Stage 2). Surface 3 — the CRUD round-trip —
is caller-facing protocol that ``HttpCatalogClient`` implements in full
(``register`` takes ``source_mtime`` explicitly; ``update`` /
``by_file_path`` / ``resolve`` / ``by_doc_id`` all exist), so those tests
seed and read through :class:`tests._catalog_fixture_ops.ActiveCatalog` and
now exercise whichever catalog is live. Surface 2 (``CatalogDB``'s SQLite
DDL + ALTER-on-open) is DIE and stays pinned; ``nexus.catalog.catalog_db``
is imported inside those bodies so the file still COLLECTS after the
deletion. Surface 1 is substrate-neutral (``nexus.catalog.tumbler``
dataclasses, not retiring), with one caveat noted at
``test_jsonl_roundtrip_preserves_mtime``.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from nexus.catalog.tumbler import DocumentRecord, read_documents
from tests._catalog_fixture_ops import ActiveCatalog


# ── Dataclass round-trip ────────────────────────────────────────────────────


class TestDocumentRecord:
    def test_default_mtime_is_zero(self) -> None:
        rec = DocumentRecord(
            tumbler="1.1.1", title="t", author="", year=0,
            content_type="paper", file_path="x.pdf",
            corpus="", physical_collection="knowledge__x",
            chunk_count=1, head_hash="", indexed_at="",
        )
        assert rec.source_mtime == 0.0

    def test_accepts_explicit_mtime(self) -> None:
        rec = DocumentRecord(
            tumbler="1.1.1", title="t", author="", year=0,
            content_type="paper", file_path="x.pdf",
            corpus="", physical_collection="knowledge__x",
            chunk_count=1, head_hash="", indexed_at="",
            source_mtime=1_700_000_000.5,
        )
        assert rec.source_mtime == 1_700_000_000.5

    def test_jsonl_roundtrip_preserves_mtime(self, tmp_path: Path) -> None:
        """nexus-i711w: the ``documents.jsonl`` wire format this asserts on is
        a LOCAL-catalog artifact, so this test's meaning retires with the
        local catalog even though ``read_documents`` itself lives in
        ``nexus.catalog.tumbler`` (not on the deletion list) and the test
        touches no ``Catalog`` object. Left exactly as-is; it collects and
        passes regardless of substrate.
        """
        path = tmp_path / "documents.jsonl"
        rec = DocumentRecord(
            tumbler="1.1.1", title="t", author="", year=0,
            content_type="paper", file_path="x.pdf",
            corpus="", physical_collection="knowledge__x",
            chunk_count=1, head_hash="", indexed_at="",
            source_mtime=1_700_000_000.5,
        )
        with path.open("w") as f:
            f.write(json.dumps(rec.__dict__) + "\n")
        loaded = read_documents(path)
        assert loaded["1.1.1"].source_mtime == 1_700_000_000.5


# ── Schema ──────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestCatalogDBSchema:
    """nexus-i711w: DIE. The subject is ``CatalogDB``'s SQLite ``documents``
    DDL and its ALTER-on-open migration; ``PRAGMA table_info`` has no
    service-mode expression (the engine schema is Liquibase-managed, covered
    by the Java suite). ``nexus.catalog.catalog_db`` is imported inside each
    body so the FILE still collects; the class retires with
    ``nexus/catalog/catalog_db.py``.
    """

    def test_fresh_install_has_column(self, tmp_path: Path) -> None:
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / "cat.db")
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(documents)").fetchall()]
        assert "source_mtime" in cols

    def test_migration_adds_column_to_pre_8luh_db(self, tmp_path: Path) -> None:
        """ALTER-on-open must patch a pre-migration catalog.db that has
        every column except source_mtime. Verifies the try/SELECT guard
        actually runs the ALTER instead of silently skipping it."""
        import sqlite3

        from nexus.catalog.catalog_db import CatalogDB

        db_path = tmp_path / "cat.db"
        conn = sqlite3.connect(str(db_path))
        # Legacy schema — no source_mtime column.
        conn.execute("""
            CREATE TABLE documents (
                tumbler TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                author TEXT,
                year INTEGER,
                content_type TEXT,
                file_path TEXT,
                corpus TEXT,
                physical_collection TEXT,
                chunk_count INTEGER,
                head_hash TEXT,
                indexed_at TEXT,
                metadata JSON
            )
        """)
        conn.execute(
            "INSERT INTO documents (tumbler, title, author, year, content_type, "
            "file_path, corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata) VALUES ('1.1.1', 'legacy', '', 0, 'paper', "
            "'legacy.pdf', '', 'knowledge__legacy', 1, '', '', '{}')"
        )
        conn.commit()
        conn.close()

        # Re-open via CatalogDB — migration should kick in.
        db = CatalogDB(db_path)
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(documents)").fetchall()]
        assert "source_mtime" in cols
        # Legacy data survives, and pre-existing rows get the default 0.
        row = db._conn.execute(
            "SELECT title, source_mtime FROM documents WHERE tumbler = ?",
            ("1.1.1",),
        ).fetchone()
        assert row == ("legacy", 0.0)


# ── Catalog CRUD ────────────────────────────────────────────────────────────


class TestCatalogRegisterStoresMtime:
    """PORTED (nexus-i711w Stage 2): ``register`` / ``update`` /
    ``by_file_path`` / ``resolve`` / ``by_doc_id`` are all implemented on both
    substrates, and ``HttpCatalogClient.register`` takes ``source_mtime``
    explicitly (http_catalog_client.py:781-825), so these now round-trip
    ``source_mtime`` through whichever catalog is live rather than through a
    private local one.
    """

    def _seed(self, tmp_path: Path) -> Any:
        """The ACTIVE catalog plus a unique file path for this test.

        Was ``Catalog(tmp_path/"catalog", ...)``; ``tmp_path`` is no longer
        used because there is no local artifact to root. Returns
        ``(catalog, owner_name, file_path)``: ``register`` is idempotent by
        ``file_path`` WITHIN an owner on both substrates, so a per-test slug
        keeps each test's single document genuinely its own.
        """
        slug = uuid.uuid4().hex[:8]
        return ActiveCatalog(), f"mtime-{slug}", f"a-{slug}.pdf"

    def _seed_local(self, tmp_path: Path) -> Any:
        """A real LOCAL ``Catalog``, for the ONE test in this class whose
        lookup verb means something different on the two substrates
        (``test_by_doc_id_returns_mtime``). Same return shape as ``_seed``.
        """
        from nexus.catalog.catalog import Catalog

        slug = uuid.uuid4().hex[:8]
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        return cat, f"mtime-{slug}", f"a-{slug}.pdf"

    def test_register_without_mtime_defaults_to_zero(self, tmp_path: Path) -> None:
        cat, owner_name, fp = self._seed(tmp_path)
        owner = cat.register_owner(owner_name, "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper",
            file_path=fp, physical_collection="knowledge__x",
        )
        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.source_mtime == 0.0

    def test_register_preserves_explicit_mtime(self, tmp_path: Path) -> None:
        cat, owner_name, fp = self._seed(tmp_path)
        owner = cat.register_owner(owner_name, "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper",
            file_path=fp, physical_collection="knowledge__x",
            source_mtime=1_700_000_000.25,
        )
        entry = cat.resolve(tumbler)
        assert entry is not None
        # Exact equality retained (not approx): the claim is that the value is
        # PRESERVED, and a substrate that rounded or truncated it must fail.
        assert entry.source_mtime == 1_700_000_000.25

    def test_by_file_path_returns_mtime(self, tmp_path: Path) -> None:
        cat, owner_name, fp = self._seed(tmp_path)
        owner = cat.register_owner(owner_name, "corpus")
        cat.register(
            owner, title="doc", content_type="paper",
            file_path=fp, physical_collection="knowledge__x",
            source_mtime=123.5,
        )
        entry = cat.by_file_path(owner, fp)
        assert entry is not None
        assert entry.source_mtime == 123.5

    def test_update_can_bump_mtime(self, tmp_path: Path) -> None:
        """Callers re-indexing a file must be able to bump stored mtime
        to the latest file.stat().st_mtime without wiping the rest of
        the record."""
        cat, owner_name, fp = self._seed(tmp_path)
        owner = cat.register_owner(owner_name, "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper",
            file_path=fp, physical_collection="knowledge__x",
            source_mtime=100.0,
        )
        cat.update(tumbler, source_mtime=200.5)
        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.source_mtime == 200.5
        # Non-vacuous partial-update check: a substrate whose ``update``
        # rewrote the whole row from the supplied fields would blank the
        # title, so this is what distinguishes "bumped" from "replaced".
        assert entry.title == "doc"  # other fields preserved

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_by_doc_id_returns_mtime(self, tmp_path: Path) -> None:
        """⚠️ PORT-BLOCKED, PINNED. Found by attempting the port and MEASURED,
        not predicted: with ``ActiveCatalog`` this fails ``assert None is not
        None``.

        ``by_doc_id`` means two DIFFERENT things on the two substrates:

          - local (``catalog_docs.py``:791-799) —
            ``WHERE json_extract(metadata,'$.doc_id') = ?``, i.e. the T3
            doc_id a caller stashed in ``meta``;
          - service (``http_catalog_client.py``:1075-1076) — a bare
            ``return self.resolve(doc_id)``, i.e. a TUMBLER lookup.

        So a document registered with ``meta={"doc_id": "doc-abc-42"}`` is
        findable by that key locally and invisible service-side. Under
        RDR-108 the tumbler IS the doc identity, so the service reading may
        well be the intended one — but the two implementations share a name
        and a signature while answering different questions, and one of them
        silently returns ``None``. Not resolved here (a src question);
        recorded so the divergence is not lost with this test.

        The other four tests in this class DID port and run against the live
        catalog.
        """
        cat, owner_name, fp = self._seed_local(tmp_path)
        doc_id = f"doc-abc-{uuid.uuid4().hex[:8]}"
        owner = cat.register_owner(owner_name, "corpus")
        cat.register(
            owner, title="doc", content_type="paper",
            file_path=fp, physical_collection="knowledge__x",
            meta={"doc_id": doc_id},
            source_mtime=55.5,
        )
        entry = cat.by_doc_id(doc_id)
        assert entry is not None
        assert entry.source_mtime == 55.5


# ── Indexing-side plumbing smoke test ───────────────────────────────────────


class TestIndexSiteCapturesMtime:
    def test_real_file_mtime_propagates_via_catalog_hook(self, tmp_path: Path) -> None:
        """The indexer calls register() with file.stat().st_mtime; end-to-end
        we verify the catalog stores the real mtime for a file we created.

        PORTED (nexus-i711w): seeds through the ACTIVE catalog, which is what
        the indexer's own hook resolves, so the round-trip this asserts is the
        one production performs.
        """
        cat = ActiveCatalog()

        # Mimic what indexer.py:291 now does: call cat.register with
        # file.stat().st_mtime.
        real_file = tmp_path / "sample.md"
        real_file.write_text("hello")
        # Set a known mtime so the round-trip is deterministic.
        os_mtime = real_file.stat().st_mtime
        owner = cat.register_owner(f"mtime-index-{uuid.uuid4().hex[:8]}", "corpus")
        tumbler = cat.register(
            owner, title="sample", content_type="prose",
            file_path=str(real_file), physical_collection="docs__x",
            source_mtime=os_mtime,
        )
        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.source_mtime == pytest.approx(os_mtime, abs=1e-3)
