"""RDR-192 Step 5 amendment (nexus-wbfpw.10): the manifest backfill's reverse
lookup reads stored rows whether or not they have a live owner.

A reverse candidate is by definition a chunk no manifest owns yet, and since
Step 5 every content read hides such a chunk. ``_forward_hints_for_chashes``
and ``_fetch_chunk_by_id`` therefore ask the engine for stored rows
(``include_non_live``). Real engine substrate, because the property lives in
the engine's read filter.
"""
from __future__ import annotations

import hashlib

# Not integration-marked (nexus-wbfpw.38): the substrate provisions itself,
# and CI's default selection must run this RDR-192 pin.

_COLLECTION = "knowledge__wbfpw10-backfill__bge-base-en-v15-768__v1"


def _unowned_chunk(client, content: str, meta: dict) -> str:
    chash = hashlib.sha256(content.encode()).hexdigest()
    client.upsert_chunks(_COLLECTION, [chash], [content], metadatas=[meta])
    assert chash in client.existing_ids(_COLLECTION, [chash])
    return chash


def test_reverse_lookup_sees_an_unowned_chunk(t2_service_env):
    import nexus.db.http_vector_client as hvc
    from nexus.catalog.manifest_backfill import (
        _fetch_chunk_by_id,
        _forward_hints_for_chashes,
    )

    client = hvc.HttpVectorClient(tenant=t2_service_env)
    content = "wbfpw10 reverse candidate"
    chash = _unowned_chunk(
        client, content,
        {"catalog_doc_id": "1.2.3", "chunk_text_hash": hashlib.sha256(content.encode()).hexdigest()},
    )
    col = client.get_collection(_COLLECTION)

    # Control: a live read cannot see the chunk, so the assertion below is
    # about the physical read and not about a chunk that happens to be live.
    assert col.get(ids=[chash], include=["metadatas"])["ids"] == []

    hints, raw = _forward_hints_for_chashes(col, [chash])
    assert hints == {chash: "1.2.3"}
    assert raw[chash]["catalog_doc_id"] == "1.2.3"

    # The single-chash reader takes the same physical path when it has no
    # cached metadata.
    row = _fetch_chunk_by_id(col, chash, "1.2.3", _COLLECTION)
    assert row is not None and row["chash"] == chash
