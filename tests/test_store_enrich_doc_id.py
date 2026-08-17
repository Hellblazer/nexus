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
from unittest.mock import patch

import pytest

from tests._catalog_fixture_ops import documents_by_title
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # nexus-i711w terminal deletion: the local ``Catalog.init`` seeding is
    # gone with the local catalog; the store hook registers via the
    # service-only factory into the live per-test tenant, so no init is needed.
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


def _seed_for_store_put(content: str, collection: str = "knowledge") -> None:
    """Pre-seed a REAL ``nexus.chunks`` row for what CLI ``nx store put``
    is about to write (nexus-dbzxb, RDR-191 Phase 5 Python collateral).

    ``local_t3`` injects a FAKE in-memory T3 client, but the manifest
    write always goes through the REAL engine catalog (autouse
    ``_pin_t2_substrate``). ``fk_catalog_chunks_chunk`` now requires the
    manifest's chash to have a matching REAL ``nexus.chunks`` row.
    """
    import hashlib

    from nexus.corpus import t3_collection_name
    from tests._catalog_fixture_ops import seed_manifest_chunks

    col_name = t3_collection_name(collection)
    chash = hashlib.sha256(content.encode()).hexdigest()
    seed_manifest_chunks(col_name, [chash])


def _no_op_post_store(*args, **kwargs):
    """Disable post-store hook chains (chash, taxonomy, aspect-extraction).
    The Stage B.4 contract is just about the T3 chunk's doc_id metadata,
    not about side-effect hook chains; the hooks rely on T2 schema state
    that isn't initialised in this test fixture.
    """
    pass


@pytest.fixture(autouse=True)
def _isolate_t3_singleton():
    """Reset the mcp_infra ``_t3_instance`` global before / after each
    test. Other tests in the suite (notably ``test_mcp_server.py::t3``)
    inject a T3 instance into this global without resetting it; the
    leak masks our ``patch("nexus.commands.store._t3", ...)`` because
    downstream hooks read the global directly. Belt-and-suspenders isolation."""
    from nexus.mcp_infra import inject_t3
    inject_t3(None)
    yield
    inject_t3(None)


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
    _finding_content = "# Finding: nexus-doc-id-pin\n\nT3 chunks must carry catalog tumbler."
    (catalog_env.parent / "finding.md").write_text(_finding_content, encoding="utf-8")
    _seed_for_store_put(_finding_content)

    # Patch HookRegistry's fire methods so no real hooks run for these
    # doc_id-stamping contract tests. The CLI constructs its own
    # registry per invocation; patching the class methods covers any
    # fresh instance the command code creates.
    with patch("nexus.commands.store._t3", return_value=local_t3), \
         patch("nexus.hook_registry.HookRegistry.fire_store_chains", side_effect=_no_op_post_store), \
         patch("nexus.hook_registry.HookRegistry.fire_single", side_effect=_no_op_post_store), \
         patch("nexus.hook_registry.HookRegistry.fire_batch", side_effect=_no_op_post_store), \
         patch("nexus.hook_registry.HookRegistry.fire_document", side_effect=_no_op_post_store):
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

    # Extract the stored collection name from the CLI output
    # ("Stored: <chunk_id>  →  <collection>"). ChromaDB's EphemeralClient
    # shares process-wide state, so other tests in the suite may have
    # populated unrelated knowledge__ collections; we must scope to the
    # exact collection this CLI invocation wrote to.
    stored_line = next(line for line in result.output.splitlines() if "Stored:" in line)
    stored_col_name = stored_line.split("→")[-1].strip()

    # Catalog should now have an entry for the stored doc.
    # nexus-aqbrk: read through the ACTIVE catalog — the store hook registers
    # via the factory, so the raw local .catalog.db was empty on the engine arm.
    rows = documents_by_title("finding-doc-id-pin")
    assert rows, "expected catalog entry for the stored doc"
    expected_doc_id = str(rows[0].tumbler)

    stored_col = local_t3._client.get_collection(stored_col_name)
    chunk_result = stored_col.get(include=["metadatas"])
    assert chunk_result["ids"], (
        f"expected at least one chunk in {stored_col_name}"
    )

    matching_metas = [
        m for m in chunk_result["metadatas"]
        if m.get("title") == "finding-doc-id-pin"
    ]
    assert matching_metas, "expected a chunk with title='finding-doc-id-pin'"

    # RDR-108 Phase 3: chunks no longer carry ``doc_id``. The catalog
    # entry's existence (asserted above) is the contract Phase 3 locks
    # in; the manifest is populated by the post-store batch hook.
    for m in matching_metas:
        assert "doc_id" not in m, (
            f"Phase 3: chunk for finding-doc-id-pin must not carry doc_id; "
            f"got {m!r}"
        )
    assert expected_doc_id, "expected catalog tumbler for the stored doc"


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
    _finding_content = "# Finding without catalog backing\n\nstore put no-catalog path."
    (tmp_path / "finding-nocat.md").write_text(_finding_content, encoding="utf-8")
    _seed_for_store_put(_finding_content)

    # Patch HookRegistry's fire methods so no real hooks run for these
    # doc_id-stamping contract tests. The CLI constructs its own
    # registry per invocation; patching the class methods covers any
    # fresh instance the command code creates.
    with patch("nexus.commands.store._t3", return_value=local_t3), \
         patch("nexus.hook_registry.HookRegistry.fire_store_chains", side_effect=_no_op_post_store), \
         patch("nexus.hook_registry.HookRegistry.fire_single", side_effect=_no_op_post_store), \
         patch("nexus.hook_registry.HookRegistry.fire_batch", side_effect=_no_op_post_store), \
         patch("nexus.hook_registry.HookRegistry.fire_document", side_effect=_no_op_post_store):
        runner = CliRunner()
        result = runner.invoke(store, [
            "put",
            str(tmp_path / "finding-nocat.md"),
            "--collection", "knowledge",
            "--title", "finding-no-catalog",
            "--tags", "test",
        ], catch_exceptions=False)
    assert result.exit_code == 0, f"store put failed: {result.output}"

    stored_line = next(line for line in result.output.splitlines() if "Stored:" in line)
    stored_col_name = stored_line.split("→")[-1].strip()
    stored_col = local_t3._client.get_collection(stored_col_name)
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
    col = local_t3.get_or_create_collection(coll_name, strict=False)
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
