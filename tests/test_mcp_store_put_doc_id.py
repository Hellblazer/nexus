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

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, documents_by_title
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
    # nexus-i711w: the local Catalog.init that used to run here died with
    # the local catalog; the store hook registers into the live service
    # catalog. The env pin keeps any legacy path resolution isolated.
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir



def _is_chash(value: str) -> bool:
    """True for a 64-hex T3 chunk id — the key type that must NOT reach the
    auto-linker (nexus-5axey)."""
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())

def _no_op(*args, **kwargs):
    pass


def _seed_for_store_put(content: str, collection: str = "knowledge") -> None:
    """Pre-seed a REAL ``nexus.chunks`` row for what MCP ``store_put`` is
    about to write (nexus-dbzxb, RDR-191 Phase 5 Python collateral).

    ``local_t3``/``inject_local_t3`` inject a FAKE in-memory T3 client
    (this file's whole design — see the module docstring), but the
    manifest write always goes through the REAL engine catalog (autouse
    ``_pin_t2_substrate``). ``fk_catalog_chunks_chunk`` now requires the
    manifest's chash to have a matching REAL ``nexus.chunks`` row, which
    the fake T3 client can never provide. Computes the exact
    ``(collection, chash)`` production will use via the same derivation
    production code uses, then seeds a real stub chunk.
    """
    import hashlib

    from nexus.corpus import t3_collection_name
    from tests._catalog_fixture_ops import seed_manifest_chunks

    col_name = t3_collection_name(collection)
    chash = hashlib.sha256(content.encode()).hexdigest()
    seed_manifest_chunks(col_name, [chash])


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

    _seed_for_store_put(
        "# MCP finding: nexus-mcp-doc-id\n\nSubagents need catalog backref.",
    )
    # Patch the process-local HookRegistry methods so the test focuses on
    # the doc_id stamping contract rather than running real hook chains.
    with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0) as auto_link_mock:
        result = store_put(
            content="# MCP finding: nexus-mcp-doc-id\n\nSubagents need catalog backref.",
            collection="knowledge",
            title="mcp-finding-doc-id",
            tags="rdr-101,mcp,test",
        )

    assert "Stored" in result, f"store_put failed: {result}"

    # nexus-5axey class A3: store_put must hand the auto-linker the catalog
    # TUMBLER, not the T3 chash. The predecessor passed `doc_id` (the chash),
    # which by_doc_id tumbler-resolves in service mode — so it matched nothing
    # and EVERY agent-stored document got zero links, with a DEBUG line as the
    # only signal. This pins the CALL SITE: the helper's own tests exercise it
    # directly and would not catch the wrong variable being threaded here.
    assert auto_link_mock.call_count == 1, "store_put must invoke the auto-linker once"
    passed_id = auto_link_mock.call_args[0][0]
    assert passed_id and not _is_chash(passed_id), (
        f"store_put passed {passed_id!r} to the auto-linker — that is a 64-hex "
        "chash, not a catalog tumbler (nexus-5axey A3)"
    )

    # Extract the stored collection name from the result message
    # ("Stored: <chunk_id>  →  <collection>"). ChromaDB's EphemeralClient
    # shares process-wide state, so other tests in the suite may have
    # populated unrelated knowledge__ collections; we must scope to the
    # exact collection this MCP invocation wrote to.
    stored_col_name = result.split("->")[-1].strip()

    rows = documents_by_title("mcp-finding-doc-id")
    assert rows, "expected catalog entry for the mcp-stored doc"
    expected_doc_id = str(rows[0].tumbler)

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
    _seed_for_store_put(content)
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
    rows = documents_by_title("pyn35-tumbler-forward")
    assert rows, "expected a catalog entry for the mcp-stored doc"
    expected_tumbler = str(rows[0].tumbler)
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


# test_mcp_store_put_forwards_blank_doc_id_when_no_catalog was REMOVED in
# nexus-i711w Stage 2 sub-stage C-store. Its premise was an ABSENT catalog —
# it pointed NEXUS_CATALOG_PATH at an uninitialised dir so the store hook
# minted nothing and returned ('', False). That state cannot exist once the
# local catalog is gone: make_catalog_reader() always returns a
# _SharedServiceCatalogHandle, so the hook registers and returns a real
# tumbler. Unsatisfiable rather than wrong, and its own docstring said so
# while it was still pinned.
#
# The behaviour it guarded is NOT lost. The blank-doc_id forwarding contract
# is still pinned below by ..._when_catalog_hook_raises (same assertion, a
# reachable trigger), and the service-side counterpart it named lives in
# tests/test_e9ru2_catalog_gate_sweep.py::
# test_register_or_lookup_fresh_box_no_local_catalog_created.
def test_mcp_store_put_forwards_blank_doc_id_when_catalog_hook_raises(
    inject_local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-172 / nexus-pyn35: when ``catalog_store_hook`` RAISES (the
    best-effort boundary catch in store_put), ``catalog_doc_id`` stays the
    pre-initialized '' and store_put forwards ``doc_id=''`` — never the
    chunk natural-id, never a crash. Distinct from the no-catalog path
    (LOW-2 / substantive-critic Significant: the swallow must degrade to
    the blank sentinel, which the service NULL-coerces).
    """
    import hashlib

    from nexus.mcp.core import store_put
    local_t3 = inject_local_t3

    content = "# Hook-raises finding\n\nThe catalog hook will blow up."
    captured: dict[str, str] = {}

    def _capture_fire_document(
        source_path: str, collection: str, doc_content: str,
        *, doc_id: str = "", **_kw,
    ) -> None:
        captured["doc_id"] = doc_id

    with patch("nexus.catalog.store_hook.catalog_store_hook_tracked",
               side_effect=RuntimeError("catalog boom")), \
         patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document",
               side_effect=_capture_fire_document), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        result = store_put(
            content=content,
            collection="knowledge",
            title="pyn35-hook-raises",
            tags="test",
        )
    assert "Stored" in result, (
        f"store_put must not crash when catalog_store_hook raises: {result}"
    )

    chunk_id = hashlib.sha256(content.encode()).hexdigest()[:32]
    assert captured.get("doc_id") == "", (
        "swallowed catalog_store_hook exception must degrade to doc_id='' "
        f"(blank->NULL sentinel), got {captured.get('doc_id')!r}"
    )
    assert captured.get("doc_id") != chunk_id, (
        "must not fall back to the chunk natural-id on hook failure"
    )


def test_mcp_store_put_ghost_reconciliation_and_manifest_linkage(
    inject_local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GH #1370 Defect 4 end-to-end: MCP ``store_put`` against a
    pre-existing GHOST catalog entry (chunk_count=0, same title) must
    (a) reuse the ghost's tumbler instead of minting a duplicate
    document (Defect 4a), and (b) leave that tumbler with real
    ``document_chunks`` manifest linkage / chunk_count > 0 (Defect 4b).

    Runs the REAL batch hook chain (only ``fire_document`` is stubbed,
    to keep aspect-extraction enqueue out of scope) so both
    ``catalog_store_hook``'s title-reconciliation and
    ``manifest_write_batch_hook`` are actually exercised — not mocked
    away like the other tests in this module.
    """
    from nexus.mcp.core import store_put
    local_t3 = inject_local_t3

    cat = ActiveCatalog()
    owner = cat.register_owner("knowledge", "curator")
    ghost = cat.register(
        owner, "ghost-reconcile-e2e", content_type="knowledge",
        physical_collection="knowledge__stale",
        meta={"doc_id": "stale-legacy-doc-id"},
    )
    assert cat.resolve(ghost).chunk_count == 0, "fixture must be a ghost"

    _seed_for_store_put("# Real content for the ghost\n\nFinally has a body.")
    with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        result = store_put(
            content="# Real content for the ghost\n\nFinally has a body.",
            collection="knowledge",
            title="ghost-reconcile-e2e",
        )
    assert "Stored" in result, f"store_put failed: {result}"

    assert len(documents_by_title("ghost-reconcile-e2e")) == 1, (
        "the ghost must be reconciled, not duplicated"
    )

    cat2 = ActiveCatalog()
    entry = cat2.resolve(ghost)
    assert entry is not None
    assert entry.meta.get("doc_id") != "stale-legacy-doc-id", (
        "the ghost's doc_id must be repointed at the new content"
    )
    assert entry.chunk_count >= 1, (
        "manifest_write_batch_hook must populate chunk_count on the "
        "reused tumbler (pre-fix: MCP store_put never wrote manifest "
        "linkage because metadatas was None)"
    )

    manifest_rows = cat2.get_manifest(str(ghost))
    assert manifest_rows, (
        "expected document_chunks manifest rows for the reused tumbler"
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

    _seed_for_store_put("# MCP finding without catalog\n\nNo-catalog path test.")
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
