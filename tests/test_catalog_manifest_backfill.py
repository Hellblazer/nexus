# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j43k: document_chunks manifest backfill (RDR-108 Phase 1b).

Tests cover:
  - write_manifest creates expected rows in (doc_id, position) order
  - write_manifest is idempotent (overwrites on re-run)
  - empty manifest (zero chunks) is a no-op rather than an error
  - full backfill verb iterates documents, paginates T3, writes manifest entries
  - taxonomy__centroids collections are skipped by default
  - missing chunk_text_hash raises a structured error (fail loud, not silent skip)
  - quota compliance: col.get page size <= 300
  - nx t3 backfill-manifest CLI: --collection, --dry-run, --limit flags

NOTE: chromadb.EphemeralClient instances share an in-memory backend singleton.
Data seeded in one test is visible to subsequent tests. All T3-touching tests
use _unique_coll() to generate isolated collection names.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main
from nexus.db.t3 import T3Database


# ── Helpers ──────────────────────────────────────────────────────────────────


def _unique_coll(prefix: str = "code") -> str:
    """Return a unique collection name per call.

    chromadb.EphemeralClient instances share an in-memory backend singleton;
    data seeded in one test is visible to subsequent tests unless collection
    names are isolated per call.
    """
    return f"{prefix}__{uuid.uuid4().hex[:12]}"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def t3_db():
    """Real T3Database backed by an ephemeral local Chroma."""
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def catalog(tmp_path):
    """Catalog rooted in tmp_path."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _insert_doc(
    cat: Catalog, tumbler: str, collection: str,
) -> None:
    """Insert a document row directly into the catalog DB for testing."""
    cat._db.execute(  # epsilon-allow: test fixture seeds documents row with caller-pinned tumbler
        "INSERT OR IGNORE INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "code", f"/tmp/{tumbler}.py",
            "", collection, 0, "", "", "{}", 0.0, "", "",
        ),
    )
    cat._db.commit()


def _seed_chunk(
    t3_db: T3Database,
    *,
    collection: str,
    chunk_id: str,
    content: str,
    doc_id: str,
    chunk_index: int,
    chunk_text_hash: str,
    line_start: int | None = None,
    line_end: int | None = None,
    chunk_start_char: int | None = None,
    chunk_end_char: int | None = None,
) -> None:
    """Insert one chunk with the metadata the backfill reads."""
    col = t3_db._client.get_or_create_collection(collection)
    meta: dict[str, Any] = {
        "doc_id": doc_id,
        "chunk_index": chunk_index,
        "chunk_text_hash": chunk_text_hash,
    }
    if line_start is not None:
        meta["line_start"] = line_start
    if line_end is not None:
        meta["line_end"] = line_end
    if chunk_start_char is not None:
        meta["chunk_start_char"] = chunk_start_char
    if chunk_end_char is not None:
        meta["chunk_end_char"] = chunk_end_char
    col.add(
        ids=[chunk_id],
        documents=[content],
        metadatas=[meta],
    )


# ── Unit tests: Catalog.write_manifest ───────────────────────────────────────


class TestWriteManifest:
    """Tests for Catalog.write_manifest(doc_id, chunks) in isolation."""

    def test_write_manifest_creates_rows_in_position_order(self, catalog):
        """write_manifest inserts one row per chunk, ordered by position."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        chunks = [
            {"chash": "b" * 64, "position": 1, "line_start": 10, "line_end": 20,
             "char_start": None, "char_end": None},
            {"chash": "a" * 64, "position": 0, "line_start": 0, "line_end": 9,
             "char_start": None, "char_end": None},
        ]
        catalog.write_manifest("1.1.1", chunks)

        rows = catalog._db.execute(
            "SELECT doc_id, position, chash FROM document_chunks "
            "WHERE doc_id = ? ORDER BY position",
            ("1.1.1",),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == ("1.1.1", 0, "a" * 64)
        assert rows[1] == ("1.1.1", 1, "b" * 64)

    def test_write_manifest_stores_positional_columns(self, catalog):
        """write_manifest persists line_start, line_end, char_start, char_end."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        chunks = [
            {
                "chash": "c" * 64,
                "position": 0,
                "line_start": 5,
                "line_end": 15,
                "char_start": 100,
                "char_end": 200,
            }
        ]
        catalog.write_manifest("1.1.1", chunks)

        row = catalog._db.execute(
            "SELECT line_start, line_end, char_start, char_end FROM document_chunks "
            "WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()
        assert row == (5, 15, 100, 200)

    def test_write_manifest_idempotent(self, catalog):
        """Re-running write_manifest for the same doc_id overwrites in place."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        chunks = [{"chash": "a" * 64, "position": 0, "line_start": None,
                   "line_end": None, "char_start": None, "char_end": None}]
        catalog.write_manifest("1.1.1", chunks)
        catalog.write_manifest("1.1.1", chunks)

        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 1

    def test_write_manifest_idempotent_replaces_on_rerun(self, catalog):
        """Re-run with different chunks replaces the old manifest."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        old_chunks = [{"chash": "a" * 64, "position": 0, "line_start": None,
                       "line_end": None, "char_start": None, "char_end": None}]
        catalog.write_manifest("1.1.1", old_chunks)

        new_chunks = [
            {"chash": "b" * 64, "position": 0, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
            {"chash": "c" * 64, "position": 1, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
        ]
        catalog.write_manifest("1.1.1", new_chunks)

        rows = catalog._db.execute(
            "SELECT chash FROM document_chunks WHERE doc_id = ? ORDER BY position",
            ("1.1.1",),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "b" * 64
        assert rows[1][0] == "c" * 64

    def test_write_manifest_zero_chunks_is_noop(self, catalog):
        """write_manifest with no chunks produces no rows and no error."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        catalog.write_manifest("1.1.1", [])

        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 0

    def test_write_manifest_zero_chunks_idempotent(self, catalog):
        """write_manifest([]) called twice doesn't crash."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        catalog.write_manifest("1.1.1", [])
        catalog.write_manifest("1.1.1", [])

        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 0

    def test_write_manifest_multiple_docs_independent(self, catalog):
        """Manifests for different doc_ids are independent."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _insert_doc(catalog, "1.1.2", coll)

        catalog.write_manifest("1.1.1", [
            {"chash": "a" * 64, "position": 0, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
        ])
        catalog.write_manifest("1.1.2", [
            {"chash": "b" * 64, "position": 0, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
            {"chash": "c" * 64, "position": 1, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
        ])

        count_1 = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?", ("1.1.1",),
        ).fetchone()[0]
        count_2 = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?", ("1.1.2",),
        ).fetchone()[0]
        assert count_1 == 1
        assert count_2 == 2

    def test_write_manifest_clears_old_rows_before_insert(self, catalog):
        """write_manifest deletes prior rows then inserts new ones (atomic)."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        # Write 3 chunks first
        catalog.write_manifest("1.1.1", [
            {"chash": "a" * 64, "position": 0, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
            {"chash": "b" * 64, "position": 1, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
            {"chash": "c" * 64, "position": 2, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
        ])
        # Overwrite with just 1 chunk at position 0
        catalog.write_manifest("1.1.1", [
            {"chash": "d" * 64, "position": 0, "line_start": None,
             "line_end": None, "char_start": None, "char_end": None},
        ])

        rows = catalog._db.execute(
            "SELECT position, chash FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchall()
        # Only the new chunk remains; the old 3 are gone
        assert len(rows) == 1
        assert rows[0] == (0, "d" * 64)


# ── Integration tests: backfill_manifest_for_collection ─────────────────────


class TestBackfillManifestForCollection:
    """Tests for the core backfill function that reads T3 and writes manifests."""

    def test_backfill_writes_chunks_for_known_docs(self, catalog, t3_db):
        """Backfill reads T3 chunk metadata and writes manifest rows."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="hello", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64, line_start=0, line_end=5)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c2-{coll}",
                    content="world", doc_id="1.1.1", chunk_index=1,
                    chunk_text_hash="b" * 64, line_start=6, line_end=10)

        result = backfill_manifest_for_collection(
            catalog, t3_db, coll, dry_run=False
        )
        assert result.chunks_written == 2
        assert result.docs_processed == 1

        rows = catalog._db.execute(
            "SELECT position, chash FROM document_chunks WHERE doc_id = ? "
            "ORDER BY position",
            ("1.1.1",),
        ).fetchall()
        assert rows == [(0, "a" * 64), (1, "b" * 64)]

    def test_backfill_zero_chunk_doc_produces_no_rows(self, catalog, t3_db):
        """A doc registered in the catalog with no T3 chunks gets an empty manifest."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        # No chunks seeded for this doc in this unique collection

        result = backfill_manifest_for_collection(
            catalog, t3_db, coll, dry_run=False
        )
        assert result.docs_processed == 1
        assert result.chunks_written == 0

        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 0

    def test_backfill_skips_taxonomy_centroids(self, catalog, t3_db):
        """taxonomy__centroids collection is skipped (no chunk_text_hash)."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll(prefix="taxonomy")
        # Create centroids collection with a centroid-style chunk (no chunk_text_hash)
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=[f"centroid-{coll}"],
            documents=["centroid content"],
            metadatas=[{"centroid_hash": "abc123", "doc_id": "1.1.1"}],
        )

        result = backfill_manifest_for_collection(
            catalog, t3_db, coll, dry_run=False
        )
        assert result.skipped_taxonomy is True
        assert result.chunks_written == 0

    def test_backfill_raises_on_missing_chunk_text_hash(self, catalog, t3_db):
        """Chunks without chunk_text_hash raise a structured error (fail loud)."""
        from nexus.catalog.manifest_backfill import (
            MissingChunkHashError,
            backfill_manifest_for_collection,
        )

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        # Seed a chunk WITHOUT chunk_text_hash (pre-RDR-053 style)
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=[f"legacy-{coll}"],
            documents=["legacy content"],
            metadatas=[{"doc_id": "1.1.1", "chunk_index": 0}],  # no chunk_text_hash
        )

        with pytest.raises(MissingChunkHashError) as exc_info:
            backfill_manifest_for_collection(
                catalog, t3_db, coll, dry_run=False
            )
        assert f"legacy-{coll}" in str(exc_info.value)
        assert coll in str(exc_info.value)

    def test_backfill_dry_run_does_not_write(self, catalog, t3_db):
        """--dry-run reports but does not write manifest rows."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="hello", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)

        result = backfill_manifest_for_collection(
            catalog, t3_db, coll, dry_run=True
        )
        assert result.chunks_written == 0  # dry_run: counts found but nothing committed

        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks",
        ).fetchone()[0]
        assert count == 0

    def test_backfill_idempotent_on_rerun(self, catalog, t3_db):
        """Running backfill twice produces the same manifest (idempotent)."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="hello", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)

        backfill_manifest_for_collection(catalog, t3_db, coll, dry_run=False)
        backfill_manifest_for_collection(catalog, t3_db, coll, dry_run=False)

        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 1

    def test_backfill_chunks_sorted_by_chunk_index(self, catalog, t3_db):
        """Chunks are ordered by chunk_index ascending in the manifest."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        # Seed in reverse index order to test sorting
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c3-{coll}",
                    content="third", doc_id="1.1.1", chunk_index=2,
                    chunk_text_hash="c" * 64)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="first", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c2-{coll}",
                    content="second", doc_id="1.1.1", chunk_index=1,
                    chunk_text_hash="b" * 64)

        backfill_manifest_for_collection(catalog, t3_db, coll, dry_run=False)

        rows = catalog._db.execute(
            "SELECT position, chash FROM document_chunks WHERE doc_id = ? "
            "ORDER BY position",
            ("1.1.1",),
        ).fetchall()
        assert rows == [(0, "a" * 64), (1, "b" * 64), (2, "c" * 64)]

    def test_backfill_respects_limit(self, catalog, t3_db):
        """--limit N processes at most N documents."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _insert_doc(catalog, "1.1.2", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="first doc", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c2-{coll}",
                    content="second doc", doc_id="1.1.2", chunk_index=0,
                    chunk_text_hash="b" * 64)

        result = backfill_manifest_for_collection(
            catalog, t3_db, coll, dry_run=False, limit=1
        )
        assert result.docs_processed == 1


class TestBackfillPagination:
    """Verify the backfill paginates T3 at <=300 records per page."""

    def test_page_size_never_exceeds_300(self, catalog, t3_db):
        """col.get is called with limit <= 300."""
        from nexus.catalog.manifest_backfill import backfill_manifest_for_collection

        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="x", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)

        calls: list[dict] = []
        col = t3_db._client.get_or_create_collection(coll)
        original_col_get = col.get

        def _tracking_get(**kwargs):
            calls.append(kwargs)
            return original_col_get(**kwargs)

        with (
            patch.object(col, "get", side_effect=_tracking_get),
            patch.object(t3_db._client, "get_collection", return_value=col),
        ):
            backfill_manifest_for_collection(
                catalog, t3_db, coll, dry_run=False
            )

        assert calls, "col.get was never called"
        for call in calls:
            limit = call.get("limit", None)
            if limit is not None:
                assert limit <= 300, (
                    f"col.get called with limit={limit} > 300 (quota violation)"
                )


# ── CLI tests: nx t3 backfill-manifest ───────────────────────────────────────


class TestBackfillManifestCLI:
    """Tests for ``nx t3 backfill-manifest`` CLI command."""

    def test_cli_dry_run_reports_no_write(self, catalog, t3_db, runner):
        """--dry-run prints a report but writes nothing."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="hello", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)

        with (
            patch("nexus.commands.t3._make_catalog", return_value=catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower() or "would" in result.output.lower()
        # No rows written
        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks"
        ).fetchone()[0]
        assert count == 0

    def test_cli_no_dry_run_writes_manifest(self, catalog, t3_db, runner):
        """Without --dry-run, the CLI writes manifest rows."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="hello", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)

        with (
            patch("nexus.commands.t3._make_catalog", return_value=catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output
        count = catalog._db.execute(
            "SELECT COUNT(*) FROM document_chunks"
        ).fetchone()[0]
        assert count == 1

    def test_cli_all_collections_no_filter(self, catalog, t3_db, runner):
        """Without --collection, the CLI runs across all known collections."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        _seed_chunk(t3_db, collection=coll, chunk_id=f"c1-{coll}",
                    content="hello", doc_id="1.1.1", chunk_index=0,
                    chunk_text_hash="a" * 64)
        # Register collection so catalog knows about it
        catalog._db.execute(  # epsilon-allow: test fixture seeds a collections row directly; Catalog.register_collection requires a registered owner which is heavyweight setup for this CLI coverage test
            "INSERT OR IGNORE INTO collections (name, content_type, owner_id, "
            "embedding_model, legacy_grandfathered, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (coll, "code", "1.1", "voyage-code-3", 1, "2026-01-01T00:00:00Z"),
        )
        catalog._db.commit()

        with (
            patch("nexus.commands.t3._make_catalog", return_value=catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output

    def test_cli_taxonomy_skipped_in_output(self, catalog, t3_db, runner):
        """taxonomy__* collections are reported as skipped in CLI output."""
        coll = _unique_coll(prefix="taxonomy")
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=[f"centroid-{coll}"],
            documents=["centroid"],
            metadatas=[{"centroid_hash": "abc", "doc_id": "1.1.1"}],
        )
        catalog._db.execute(  # epsilon-allow: test fixture seeds a collections row directly for taxonomy carve-out CLI test; register_collection requires heavyweight owner setup
            "INSERT OR IGNORE INTO collections (name, content_type, owner_id, "
            "embedding_model, legacy_grandfathered, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (coll, "taxonomy", "", "", 1, "2026-01-01T00:00:00Z"),
        )
        catalog._db.commit()

        with (
            patch("nexus.commands.t3._make_catalog", return_value=catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "skip" in result.output.lower()

    def test_cli_missing_hash_exits_nonzero(self, catalog, t3_db, runner):
        """Missing chunk_text_hash causes the CLI to exit with non-zero status."""
        coll = _unique_coll()
        _insert_doc(catalog, "1.1.1", coll)
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=[f"bad-{coll}"],
            documents=["content"],
            metadatas=[{"doc_id": "1.1.1", "chunk_index": 0}],  # no chunk_text_hash
        )

        with (
            patch("nexus.commands.t3._make_catalog", return_value=catalog),
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
        ):
            result = runner.invoke(
                main,
                ["t3", "backfill-manifest", "--collection", coll, "--no-dry-run"],
            )
        assert result.exit_code != 0
