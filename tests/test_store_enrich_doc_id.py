# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""WITH TEETH: nx store put must write the catalog tumbler as ``doc_id``
into T3 chunk metadata at write-time; nx enrich must preserve doc_id
round-trip (RDR-101 Phase 3 PR δ Stage B.4).

Two independent CLI write paths that don't go through ``index_repository``:

1. ``nx store put`` writes a single T3 chunk via ``T3Database.put()``.
   Pre-Stage-B.4 the catalog hook ran AFTER the T3 write, so chunk
   metadata never carried the catalog tumbler.

2. ``nx enrich bib`` re-writes existing chunk metadata with bib_*
   fields via ``col.update(metadatas=...)``. The contract here is
   pass-through: doc_id present pre-enrich must be present post-enrich.

Reverting either fix breaks the corresponding test deterministically.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


def _no_op_post_store(*args, **kwargs):
    """Disable post-store hook chains (chash, taxonomy, aspect-extraction).
    The Stage B.4 contract is just about the T3 chunk's doc_id metadata,
    not about side-effect hook chains; the hooks rely on T2 schema state
    that isn't initialised in this test fixture.
    """
    pass


def test_store_put_cli_writes_catalog_doc_id_into_t3_chunk_metadata(
    local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx store put`` CLI command must populate ``doc_id`` in the T3
    chunk's metadata, matching the catalog tumbler created by the store
    hook (Stage B.4 contract).
    """
    from click.testing import CliRunner
    from nexus.commands.store import store

    # Create a temp file to feed to ``nx store put``
    (catalog_env.parent / "finding.md").write_text(
        "# Finding: nexus-doc-id-pin\n\nT3 chunks must carry catalog tumbler.",
        encoding="utf-8",
    )

    with patch("nexus.commands.store._t3", return_value=local_t3), \
         patch("nexus.mcp_infra.fire_store_chains", side_effect=_no_op_post_store), \
         patch("nexus.mcp_infra.fire_post_store_hooks", side_effect=_no_op_post_store), \
         patch("nexus.mcp_infra.fire_post_store_batch_hooks", side_effect=_no_op_post_store), \
         patch("nexus.mcp_infra.fire_post_document_hooks", side_effect=_no_op_post_store):
        runner = CliRunner()
        result = runner.invoke(store, [
            "put",
            str(catalog_env.parent / "finding.md"),
            "--collection", "knowledge",
            "--title", "finding-doc-id-pin",
            "--tags", "rdr-101,test",
        ], catch_exceptions=False)

    assert result.exit_code == 0, f"store put failed: {result.output}"
    assert "Stored:" in result.output

    # Catalog should now have an entry for the stored doc.
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    rows = cat._db.execute(
        "SELECT tumbler FROM documents WHERE title = 'finding-doc-id-pin'"
    ).fetchall()
    assert rows, "expected catalog entry for the stored doc"
    expected_doc_id = rows[0][0]

    # Find the actual stored collection (resolved from "knowledge" via
    # t3_collection_name → "knowledge__knowledge" in the default routing).
    cols = local_t3._client.list_collections()
    stored_col = None
    for c in cols:
        if c.name.startswith("knowledge"):
            stored_col = c
            break
    assert stored_col is not None, "expected a knowledge__ collection"

    chunk_result = stored_col.get(include=["metadatas"])
    assert chunk_result["ids"], "expected at least one chunk in knowledge collection"

    matching_metas = [
        m for m in chunk_result["metadatas"]
        if m.get("title") == "finding-doc-id-pin"
    ]
    assert matching_metas, "expected a chunk with title='finding-doc-id-pin'"

    for m in matching_metas:
        assert m.get("doc_id") == expected_doc_id, (
            f"chunk for finding-doc-id-pin carries doc_id={m.get('doc_id')!r}, "
            f"expected {expected_doc_id!r} (catalog tumbler)"
        )


def test_store_put_doc_id_absent_when_catalog_uninitialized(
    local_t3: T3Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no catalog exists, store put must still succeed and emit a
    chunk WITHOUT doc_id (schema drops empty doc_id at the funnel).
    """
    from click.testing import CliRunner
    from nexus.commands.store import store

    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
    (tmp_path / "finding-nocat.md").write_text(
        "# Finding without catalog backing\n\nstore put no-catalog path.",
        encoding="utf-8",
    )

    with patch("nexus.commands.store._t3", return_value=local_t3), \
         patch("nexus.mcp_infra.fire_store_chains", side_effect=_no_op_post_store), \
         patch("nexus.mcp_infra.fire_post_store_hooks", side_effect=_no_op_post_store), \
         patch("nexus.mcp_infra.fire_post_store_batch_hooks", side_effect=_no_op_post_store), \
         patch("nexus.mcp_infra.fire_post_document_hooks", side_effect=_no_op_post_store):
        runner = CliRunner()
        result = runner.invoke(store, [
            "put",
            str(tmp_path / "finding-nocat.md"),
            "--collection", "knowledge",
            "--title", "finding-no-catalog",
            "--tags", "test",
        ], catch_exceptions=False)
    assert result.exit_code == 0, f"store put failed: {result.output}"

    cols = local_t3._client.list_collections()
    stored_col = None
    for c in cols:
        if c.name.startswith("knowledge"):
            stored_col = c
            break
    assert stored_col is not None
    chunk_result = stored_col.get(include=["metadatas"])
    assert chunk_result["ids"]

    for m in chunk_result["metadatas"]:
        if m.get("title") == "finding-no-catalog":
            assert "doc_id" not in m, (
                "doc_id must be dropped (normalize Step 4c) when no catalog "
                "entry exists; saw doc_id=%r" % m.get("doc_id")
            )


def test_enrich_preserves_doc_id_round_trip(
    local_t3: T3Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx enrich bib`` re-writes chunk metadata with bib_* fields. If a
    chunk already carries doc_id (from a Stage B.1 / B.2 / B.3 ingest),
    the enrich update must NOT drop it — the catalog cross-reference is
    load-bearing for the ζ cutover.
    """
    from click.testing import CliRunner
    from nexus.commands.enrich import enrich

    # Set up: a fake docs__ collection with a pre-Stage-B.4 chunk that
    # already carries doc_id (simulating a Stage B.1 / B.2 / B.3 ingest).
    coll_name = "docs__test-enrich-roundtrip"
    col = local_t3.get_or_create_collection(coll_name)
    chunk_meta = {
        "content_type": "prose",
        "source_path": "/tmp/fake.md",
        "title": "Round-Trip Test Title",
        "chunk_index": 0,
        "chunk_count": 1,
        "chunk_text_hash": "deadbeef",
        "content_hash": "deadbeef",
        "chunk_start_char": 0,
        "chunk_end_char": 50,
        "indexed_at": "2026-05-01T00:00:00+00:00",
        "embedding_model": "voyage-context-3",
        "store_type": "prose",
        "corpus": coll_name,
        "tags": "test",
        "category": "prose",
        "frecency_score": 0.0,
        "doc_id": "1.42.7",  # pre-existing catalog tumbler
        "ttl_days": 0,
    }
    col.add(
        ids=["chunk-1"],
        documents=["Round-trip preservation matters."],
        metadatas=[chunk_meta],
    )

    # Stub the bib backend resolver so the test runs offline.
    def fake_resolve(title, *args, **kwargs):
        return {
            "year": 2024,
            "venue": "Test Journal",
            "authors": "Doe, Jane",
            "citation_count": 42,
            "semantic_scholar_id": "test-ssid",
            "_resolved_via": "title",
        }

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.commands.enrich._resolve_bib_for_title", side_effect=fake_resolve), \
         patch("nexus.commands.enrich._catalog_enrich_hook"):
        runner = CliRunner()
        result = runner.invoke(enrich, [
            "bib", coll_name,
            "--source", "s2",
            "--delay", "0",
        ], catch_exceptions=False)

    assert result.exit_code == 0, f"enrich bib failed: {result.output}"

    # Re-read the chunk and verify doc_id survived the col.update merge.
    after = col.get(ids=["chunk-1"], include=["metadatas"])
    assert after["ids"] == ["chunk-1"]
    after_meta = after["metadatas"][0]
    assert after_meta.get("bib_year") == 2024, (
        f"enrich should have written bib_year=2024; got {after_meta.get('bib_year')!r}"
    )
    assert after_meta.get("doc_id") == "1.42.7", (
        f"doc_id must round-trip through enrich; got {after_meta.get('doc_id')!r}"
    )
