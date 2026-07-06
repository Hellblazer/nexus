# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1371: ``nx catalog reconcile`` repairs document_chunks manifest gaps
left by a persistently-failed manifest_write_batch_hook.

A gap is: ``documents.chunk_count > 0`` but the document_chunks manifest
has fewer rows than chunk_count (including zero). The command rebuilds the
manifest from T3 chunk metadata, matching a document's chunks by the
whole-file ``content_hash`` stamped in ``documents.metadata`` and in every
chunk's T3 metadata (RDR-108 Phase 3 dropped doc_id/chunk_index from chunk
metadata, but content_hash + the char/line spans survive and are enough
to reconstruct both identity and order).
"""
from __future__ import annotations

import json

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main
from nexus.db.t3 import T3Database


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def t3_db():
    db = T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )
    for raw in list(db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            db._client.delete_collection(name)
        except Exception:
            pass
    return db


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _seed_doc(
    catalog: Catalog, *, tumbler: str, collection: str, chunk_count: int,
    content_hash: str, file_path: str = "",
) -> None:
    meta = json.dumps({"content_hash": content_hash}) if content_hash else "{}"
    catalog._db.execute(  # epsilon-allow: fixture seeds a documents row with caller-pinned tumbler; Catalog.register mints its own owner-prefixed tumbler
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "code", file_path or f"/tmp/{tumbler}.py",
            "", collection, chunk_count, "", "", meta, 0.0, "", "",
        ),
    )
    catalog._db.commit()


def _seed_chunks(t3_db: T3Database, collection: str, content_hash: str, n: int) -> list[str]:
    """Add n chunks sharing content_hash, with distinct char spans so
    ordering can be reconstructed, and return the chunk ids in file order."""
    col = t3_db._client.get_or_create_collection(collection)
    ids = [f"{content_hash}{i:02d}" for i in range(n)]
    metadatas = [
        {
            "content_hash": content_hash,
            "chunk_text_hash": ids[i],
            "chunk_start_char": i * 100,
            "chunk_end_char": (i + 1) * 100,
            "embedding_model": "voyage-code-3",
        }
        for i in range(n)
    ]
    # Insert in REVERSE order to prove the command sorts by span, not by
    # T3 insertion / return order.
    col.add(
        ids=list(reversed(ids)),
        documents=[f"chunk {i}" for i in reversed(range(n))],
        metadatas=list(reversed(metadatas)),
    )
    return ids


def test_reconcile_rebuilds_gapped_manifest(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=3, content_hash="abc123",
    )
    _seed_chunks(t3_db, "code__delos", "abc123", 3)

    assert catalog.get_manifest("1.1.1") == []

    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 1 document(s); 0 could not be matched" in result.output

    rows = catalog.get_manifest("1.1.1")
    assert len(rows) == 3
    assert [r.position for r in rows] == [0, 1, 2]
    assert [r.chash for r in rows] == ["abc12300", "abc12301", "abc12302"]


def test_reconcile_dry_run_reports_without_writing(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=3, content_hash="abc123",
    )
    _seed_chunks(t3_db, "code__delos", "abc123", 3)

    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would reconcile 1 document(s); 0 could not be matched" in result.output
    assert catalog.get_manifest("1.1.1") == []


def test_reconcile_reports_unmatched_when_no_content_hash(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=2, content_hash="",
    )
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 1 could not be matched" in result.output
    assert "1.1.1" in result.output


def test_reconcile_reports_unmatched_when_no_t3_chunks_found(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=2, content_hash="nomatch",
    )
    # Seed unrelated chunks under a different content_hash.
    _seed_chunks(t3_db, "code__delos", "other999", 2)
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 1 could not be matched" in result.output


def test_reconcile_skips_documents_already_complete(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=2, content_hash="complete1",
    )
    catalog.write_manifest("1.1.1", [
        {"chash": "complete100", "position": 0},
        {"chash": "complete101", "position": 1},
    ])
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 0 could not be matched" in result.output


def test_reconcile_no_gapped_documents_reports_zero(t3_db, catalog, runner):
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 0 could not be matched" in result.output


def patch_reconcile(t3_db, catalog):
    from unittest.mock import patch as _patch
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(_patch("nexus.db.make_t3", return_value=t3_db))
    stack.enter_context(_patch("nexus.commands.catalog._get_catalog", return_value=catalog))
    stack.enter_context(_patch("nexus.commands.catalog._get_catalog_writer", return_value=catalog))
    return stack
