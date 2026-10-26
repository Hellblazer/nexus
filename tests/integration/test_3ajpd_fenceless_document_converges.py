# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ajpd convergence, on the real engine: a document whose fence is NULL (indexed
before the fence existed) is stale, is indexed on its next run, and is skipped on the run
after that.

Integration-marked, unlike the RDR-192 pins in
``tests/test_0ntxj_failed_index_run_leaves_no_hidden_chunks.py`` (where this test began):
that file is on the RDR-192 no-marker list (``tests/test_rdr192_pins_not_integration_marked_lint.py``),
and this scenario is not an RDR-192 pin. It runs through the ``integration (affected)`` CI job
when ``nexus.doc_indexer`` (or anything else it imports) changes
(``scripts/select_affected_integration_tests.py``). It self-provisions its engine through
``t2_service_env`` like the other files here.
"""
from __future__ import annotations

import pytest

from nexus.db.http_vector_client import HttpVectorClient
from tests._chunk_seed import seed_chunks_direct

pytestmark = [pytest.mark.integration]

_COLLECTION = "docs__3ajpd-fenceless__bge-base-en-v15-768__v1"

_BODY = "\n\n".join(
    f"## Section {i}\n\n" + " ".join(f"3ajpd fenceless paragraph {i} sentence {j}." for j in range(40))
    for i in range(6)
)


def _doc_for(path) -> object:
    from tests._catalog_fixture_ops import ActiveCatalog

    matches = [
        d for d in ActiveCatalog().all_documents()
        if (d.file_path or "").endswith(path.name)
    ]
    assert len(matches) == 1, matches
    return matches[0]


def test_a_fence_less_document_is_stale_then_indexed_then_skipped(t2_service_env, tmp_path):
    """nexus-3ajpd convergence, on the real engine. A document indexed before the fence existed
    (registered, chunks stored under its file hash, manifest rows, NULL fence) used to skip on the
    chunk row's ``content_hash``. A NULL fence is now stale: the next run indexes it once, its
    completion stamps the fence with the file hash, and the run after that skips it. Each half is
    pinned elsewhere; this is the join."""
    from datetime import UTC, datetime

    from nexus.catalog.factory import make_catalog_writer
    from nexus.corpus import index_model_for_collection
    from nexus.doc_indexer import (
        _markdown_chunks,
        _register_or_lookup_doc_id,
        _sha256,
        index_markdown,
    )

    md = tmp_path / "3ajpd-fenceless.md"
    md.write_text(f"# 3ajpd fenceless\n\n{_BODY}\n")
    client = HttpVectorClient(tenant=t2_service_env)
    path = md.resolve()
    doc_id = _register_or_lookup_doc_id(
        path, "3ajpd", content_type="prose", physical_collection=_COLLECTION)
    assert doc_id
    content_hash = _sha256(path)
    prepared = _markdown_chunks(
        path, content_hash, index_model_for_collection(_COLLECTION),
        datetime.now(UTC).isoformat(), "3ajpd", doc_id=doc_id)
    assert prepared
    seed_chunks_direct(
        _COLLECTION, [p[0] for p in prepared], [p[1] for p in prepared],
        [p[2] for p in prepared], embed=True)
    cat = make_catalog_writer()
    try:
        cat.write_manifest(
            doc_id, [{"chash": p[0], "position": i} for i, p in enumerate(prepared)],
            collection=_COLLECTION)
    finally:
        close = getattr(cat, "close", None)
        if close is not None:
            close()

    # Controls: the legacy shape the old gate skipped on. A chunk row carries the file's hash and
    # the document has no fence at all.
    col = client.get_collection(_COLLECTION)
    rows = col.get(ids=[prepared[0][0]], include=["metadatas"])["metadatas"]
    assert rows and rows[0].get("content_hash") == content_hash
    before = _doc_for(md)
    assert before.index_state is None and not before.index_content_hash, before

    first = index_markdown(md, corpus="3ajpd", t3=client, collection_name=_COLLECTION)
    assert first > 0, "a fence-less document is indexed, never skipped on a chunk row's hash"
    done = _doc_for(md)
    assert done.index_state == "complete" and done.index_content_hash == content_hash, done

    second = index_markdown(md, corpus="3ajpd", t3=client, collection_name=_COLLECTION)
    assert second == 0, "the completed fence makes the next run skip it"
    again = _doc_for(md)
    assert again.index_state == "complete" and again.index_content_hash == content_hash
