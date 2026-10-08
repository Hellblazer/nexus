# SPDX-License-Identifier: AGPL-3.0-or-later
"""A collection's first chunk write makes it visible to this process's collection cache at once.

The engine lists only collections that physically hold a chunk (``GET /v1/vectors/stats``). A new
collection is registered (which drops ``mcp_infra``'s collection cache) and the combined writer
then reads the cache for its per-request chunk cap BEFORE the chunks land, so the cache is
refilled with a listing that does not name the collection. Nothing dropped the cache after the
write, so for up to ``_COLLECTIONS_CACHE_TTL`` (60 s) every in-process reader (corpus resolution,
search fan-out, ``get_collection_names``) did not see the collection that had just been written.

Found by ``tests/integration/test_rdr_196_p2c_retrieval_bench.py``: once the local engine embedded
faster (61e0e12f7), its 15 in-process ``nx index rdr`` runs finished inside the 60 s window and the
bench read an empty collection list after a successful index.
"""
from __future__ import annotations

import nexus.mcp_infra as mcp_infra
from nexus.db.http_vector_client import HttpVectorClient
from nexus.doc_indexer import index_markdown

_COLLECTION = "docs__cachefirstwrite__bge-base-en-v15-768__v1"

_BODY = "\n\n".join(
    f"## Section {i}\n\n" + " ".join(f"cache first-write paragraph {i} sentence {j}." for j in range(30))
    for i in range(3)
)


def test_a_collection_written_for_the_first_time_is_listed_at_once(t2_service_env, tmp_path):
    md = tmp_path / "cache-first-write.md"
    md.write_text(f"# cache first write\n\n{_BODY}\n")
    client = HttpVectorClient(tenant=t2_service_env)

    mcp_infra.invalidate_collections_cache()
    assert _COLLECTION not in mcp_infra.get_collection_names(), "control: nothing written yet"

    count = index_markdown(md, corpus="cachefirstwrite", t3=client, collection_name=_COLLECTION)
    assert count > 0, "control: the document was written"

    assert _COLLECTION in mcp_infra.get_collection_names(), (
        "a collection this process just wrote is missing from its collection cache: the cache was "
        "refilled between the collection's registration and its first chunk write, and nothing "
        "dropped it after the write"
    )
    assert mcp_infra.get_collection_row(_COLLECTION) is not None
