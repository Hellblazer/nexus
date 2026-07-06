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
from nexus.catalog.tumbler import Tumbler
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


def patch_reconcile(t3_db, catalog, *, writer=None):
    from unittest.mock import patch as _patch
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(_patch("nexus.db.make_t3", return_value=t3_db))
    stack.enter_context(_patch("nexus.commands.catalog._get_catalog", return_value=catalog))
    stack.enter_context(
        _patch("nexus.commands.catalog._get_catalog_writer", return_value=writer or catalog)
    )
    return stack


class _UnsyncedManifestWriter:
    """Wraps a real ``Catalog`` but mimics
    ``HttpCatalogClient.atomic_manifest_replace``'s contract: writes the
    manifest rows WITHOUT resyncing ``documents.chunk_count`` unless
    ``new_chunk_count=`` is explicitly passed (see
    ``src/nexus/catalog/http_catalog_client.py``).

    The local-mode ``Catalog.atomic_manifest_replace`` re-derives
    ``chunk_count`` from the post-write row count inside the same SQL
    transaction, so the GH #1371 follow-up bug (reconcile never converges)
    cannot reproduce against a bare local ``Catalog`` — it is a real
    behavioral asymmetry between the two backends. This double isolates
    exactly that one asymmetry so the command-level convergence contract
    can be exercised with a real Catalog + real chromadb, without standing
    up an HTTP transport for the Java engine.
    """

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def atomic_manifest_replace(self, doc_id, chunks, *, new_collection=None, new_chunk_count=None):
        self._catalog.write_manifest(doc_id, chunks)
        if new_chunk_count is not None:
            self._catalog.update(Tumbler.parse(doc_id), chunk_count=new_chunk_count)

    def resync_chunk_count_cache(self, doc_id):
        self._catalog.resync_chunk_count_cache(doc_id)

    def close(self):
        pass


def test_reconcile_converges_after_one_pass_service_mode_shaped(t3_db, catalog, runner):
    """GH #1371 follow-up: live-verified defect where ``nx catalog
    reconcile`` reported "Reconciled 36 document(s)" on TWO consecutive
    runs against the deployed cloud catalog, and a dry-run after that
    still reported the same 36. Root cause: ``atomic_manifest_replace``'s
    HTTP/service-mode path only resyncs ``documents.chunk_count`` when
    ``new_chunk_count=`` is passed, which the reconcile call site never
    did — so the gap detector (``len(manifest) < chunk_count``) re-flagged
    the same documents forever.

    Fixture covers both shapes from the live incident: a document whose
    manifest positions collapse onto fewer distinct T3 rows (duplicate
    chunk text, RDR-108) and a document with a stale-high chunk_count
    whose manifest was entirely empty (pure indexing-hook-failure gap).
    """
    # (a) Duplicate-text collapse: claims 3 chunks, only 2 distinct T3 rows.
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=3, content_hash="dup1",
    )
    _seed_chunks(t3_db, "code__delos", "dup1", 2)

    # (b) Stale-high chunk_count, no dedup involved: claims 5, T3 has 5.
    _seed_doc(
        catalog, tumbler="1.1.2", collection="code__delos",
        chunk_count=5, content_hash="stale1",
    )
    _seed_chunks(t3_db, "code__delos", "stale1", 5)

    writer = _UnsyncedManifestWriter(catalog)

    with patch_reconcile(t3_db, catalog, writer=writer):
        first = runner.invoke(main, ["catalog", "reconcile"])
    assert first.exit_code == 0, first.output
    assert "Reconciled 2 document(s)" in first.output
    assert "chunk_count corrected 3 -> 2" in first.output
    assert "0 could not be matched" in first.output

    # Manifests + chunk_count both converged after one pass.
    assert len(catalog.get_manifest("1.1.1")) == 2
    assert len(catalog.get_manifest("1.1.2")) == 5
    assert catalog.resolve(Tumbler.parse("1.1.1")).chunk_count == 2
    assert catalog.resolve(Tumbler.parse("1.1.2")).chunk_count == 5

    # The bug: without the resync_chunk_count_cache fix, chunk_count stays
    # stale on this writer shape and the SAME documents get re-flagged.
    with patch_reconcile(t3_db, catalog, writer=writer):
        second = runner.invoke(main, ["catalog", "reconcile"])
    assert second.exit_code == 0, second.output
    assert "Reconciled 0 document(s); 0 could not be matched" in second.output

    with patch_reconcile(t3_db, catalog, writer=writer):
        third = runner.invoke(main, ["catalog", "reconcile", "--dry-run"])
    assert third.exit_code == 0, third.output
    assert "Would reconcile 0 document(s); 0 could not be matched" in third.output
