# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""WITH TEETH: MCP ``store_put`` must write the catalog tumbler as
``doc_id`` into T3 chunk metadata at write-time (RDR-101 Phase 3 PR δ
Stage B.5).

Mirrors B.4's CLI-side ``nx store put`` test for the MCP-side
``store_put`` tool (mcp/core.py). MCP ``store_put`` is the hot path —
Claude subagents call it for findings, research notes, and decision
artefacts. Pre-Stage-B.5 the catalog hook ran AFTER the T3 write, so
chunks landed without doc_id back-ref.

Reverting the wiring (resolver -> ctx.catalog_doc_id ->
``T3Database.put(catalog_doc_id=...)``) breaks the test deterministically.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
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


def _no_op(*args, **kwargs):
    pass


@pytest.fixture
def inject_local_t3(local_t3: T3Database):
    """Inject ``local_t3`` into the mcp_infra ``_t3_instance`` singleton
    AND patch the local-name binding in ``mcp.core``. Both layers are
    necessary because:
      - Other tests (e.g. ``test_mcp_server.py::t3``) leak a different
        injected T3 into the singleton across test boundaries; without
        ``_inject_t3(local_t3)`` here, the global lookup serves the
        leaked instance.
      - ``mcp.core`` imports ``get_t3 as _get_t3`` at module-load time,
        so patching the local name is the per-test belt to the global
        suspenders.
    """
    from nexus.mcp_infra import inject_t3
    inject_t3(local_t3)
    yield local_t3
    # Reset singleton after the test so we don't leak into the next.
    inject_t3(None)


def test_mcp_store_put_writes_catalog_doc_id_into_t3_chunk_metadata(
    inject_local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP ``store_put`` must populate ``doc_id`` in the T3 chunk's
    metadata, matching the catalog tumbler created by the store hook.
    """
    from nexus.mcp.core import store_put
    local_t3 = inject_local_t3

    # Patch the process-local HookRegistry methods so the test focuses on
    # the doc_id stamping contract rather than running real hook chains.
    with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        result = store_put(
            content="# MCP finding: nexus-mcp-doc-id\n\nSubagents need catalog backref.",
            collection="knowledge",
            title="mcp-finding-doc-id",
            tags="rdr-101,mcp,test",
        )

    assert "Stored" in result, f"store_put failed: {result}"

    # Extract the stored collection name from the result message
    # ("Stored: <chunk_id>  →  <collection>"). ChromaDB's EphemeralClient
    # shares process-wide state, so other tests in the suite may have
    # populated unrelated knowledge__ collections; we must scope to the
    # exact collection this MCP invocation wrote to.
    stored_col_name = result.split("->")[-1].strip()

    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    rows = cat._db.execute(
        "SELECT tumbler FROM documents WHERE title = 'mcp-finding-doc-id'"
    ).fetchall()
    assert rows, "expected catalog entry for the mcp-stored doc"
    expected_doc_id = rows[0][0]

    stored_col = local_t3._client.get_collection(stored_col_name)
    chunk_result = stored_col.get(include=["metadatas"])
    matching_metas = [
        m for m in chunk_result["metadatas"]
        if m.get("title") == "mcp-finding-doc-id"
    ]
    assert matching_metas, "expected a chunk with title='mcp-finding-doc-id'"

    # RDR-108 Phase 3: MCP-stored chunks no longer carry ``doc_id`` —
    # the catalog manifest is authoritative. The Document's existence
    # in the catalog (asserted above) is the contract Phase 3 locks in.
    for m in matching_metas:
        assert "doc_id" not in m, (
            f"Phase 3: MCP-stored chunk metadata must not carry doc_id; "
            f"got {m!r}"
        )
    assert expected_doc_id, "expected catalog tumbler for the mcp-stored doc"


def test_mcp_store_put_forwards_catalog_tumbler_as_fire_document_doc_id(
    inject_local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-172 / nexus-pyn35 (closes nexus-ov0sw): MCP ``store_put`` must
    forward the catalog tumbler (``catalog_doc_id``) as ``fire_document``'s
    ``doc_id`` kwarg — the value the aspect-queue row stamps and the
    RDR-156 FK ``aspect_extraction_queue.doc_id -> catalog_documents``
    checks. Pre-fix it forwarded the ``t3.put`` chunk natural-id
    (``sha256(content)[:32]``, never a tumbler), which 500s the service
    enqueue while the best-effort hook swallows it — silent, total loss of
    RDR-089 aspects in service mode.
    """
    import hashlib

    from nexus.mcp.core import store_put
    local_t3 = inject_local_t3

    content = "# Paper: BFT consensus\n\nIntroduces a new approach to consensus."
    captured: dict[str, str] = {}

    def _capture_fire_document(
        source_path: str, collection: str, doc_content: str,
        *, doc_id: str = "", **_kw,
    ) -> None:
        captured["source_path"] = source_path
        captured["doc_id"] = doc_id

    with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document",
               side_effect=_capture_fire_document), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        result = store_put(
            content=content,
            collection="knowledge",
            title="pyn35-tumbler-forward",
            tags="rdr-172,test",
        )
    assert "Stored" in result, f"store_put failed: {result}"

    # The catalog tumbler the store hook minted for this doc.
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    rows = cat._db.execute(
        "SELECT tumbler FROM documents WHERE title = 'pyn35-tumbler-forward'"
    ).fetchall()
    assert rows, "expected a catalog entry for the mcp-stored doc"
    expected_tumbler = rows[0][0]
    assert expected_tumbler, "expected a non-empty catalog tumbler"

    # The t3.put chunk natural-id — the value the pre-fix code (wrongly)
    # forwarded; it can never satisfy the doc_id -> catalog_documents FK.
    chunk_id = hashlib.sha256(content.encode()).hexdigest()[:32]

    assert captured.get("doc_id") == expected_tumbler, (
        "store_put must forward the catalog tumbler as fire_document's "
        f"doc_id kwarg; got {captured.get('doc_id')!r}, expected the tumbler "
        f"{expected_tumbler!r}"
    )
    assert captured["doc_id"] != chunk_id, (
        "regression guard (nexus-ov0sw): forwarding the t3.put chunk "
        "natural-id as doc_id violates the RDR-156 FK and 500s the enqueue"
    )


def test_mcp_store_put_forwards_blank_doc_id_when_no_catalog(
    inject_local_t3: T3Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-172 / nexus-pyn35: when no catalog tumbler was minted, store_put
    forwards ``doc_id=''`` — the blank sentinel the service NULL-coerces
    (``nullIfBlank``), which satisfies the FK and still extracts from the
    queued content. It must NOT fall back to the chunk natural-id.
    """
    import hashlib

    from nexus.mcp.core import store_put
    local_t3 = inject_local_t3

    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))

    content = "# No-catalog finding\n\nNo tumbler should be minted here."
    captured: dict[str, str] = {}

    def _capture_fire_document(
        source_path: str, collection: str, doc_content: str,
        *, doc_id: str = "", **_kw,
    ) -> None:
        captured["doc_id"] = doc_id

    with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document",
               side_effect=_capture_fire_document), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        result = store_put(
            content=content,
            collection="knowledge",
            title="pyn35-no-catalog",
            tags="test",
        )
    assert "Stored" in result, f"store_put failed: {result}"

    chunk_id = hashlib.sha256(content.encode()).hexdigest()[:32]
    assert captured.get("doc_id") == "", (
        "no-catalog path must forward an empty doc_id (the blank->NULL "
        f"sentinel), got {captured.get('doc_id')!r}"
    )
    assert captured.get("doc_id") != chunk_id, (
        "must not fall back to the chunk natural-id when no tumbler exists"
    )


def test_mcp_store_put_doc_id_absent_when_catalog_uninitialized(
    inject_local_t3: T3Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no catalog exists, MCP store_put still succeeds and emits a
    chunk WITHOUT doc_id (schema drops empty doc_id at the funnel)."""
    from nexus.mcp.core import store_put
    local_t3 = inject_local_t3

    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))

    with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        result = store_put(
            content="# MCP finding without catalog\n\nNo-catalog path test.",
            collection="knowledge",
            title="mcp-finding-no-catalog",
            tags="test",
        )
    assert "Stored" in result, f"store_put failed: {result}"

    stored_col_name = result.split("->")[-1].strip()
    stored_col = local_t3._client.get_collection(stored_col_name)
    chunk_result = stored_col.get(include=["metadatas"])

    for m in chunk_result["metadatas"]:
        if m.get("title") == "mcp-finding-no-catalog":
            assert "doc_id" not in m, (
                "doc_id must be dropped (normalize Step 4c) when no catalog "
                "entry exists; saw doc_id=%r" % m.get("doc_id")
            )
