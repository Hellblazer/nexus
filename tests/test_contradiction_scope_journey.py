# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search latency (nexus-92q1p follow-up): the contradiction-scope flow against the REAL engine.

``tests/test_contradiction_scope.py`` pins the client against a fake transport. The scoped check
reads ``source_agent`` off each returned row's metadata, so this file closes the one gap a fake
cannot: that the engine's ``search-per-collection`` route AND the batched ``/search`` leg both carry
the stored ``source_agent`` on the row (the engine flattens a chunk's stored metadata into the row),
and that the flag comes out of real vectors, with ``get-embeddings`` asked for the mixed-agent
collection only.
"""
from __future__ import annotations

import hashlib

import pytest

import nexus.db.http_vector_client as hvc
from nexus.search_engine import search_cross_corpus
from tests._catalog_fixture_ops import give_chunks_a_live_owner
from tests._chunk_seed import seed_chunks_direct

_MODEL = "bge-base-en-v15-768"
_NOTES = f"knowledge__scope-journey__{_MODEL}__v1"
_CODE = f"code__scope-journey__{_MODEL}__v1"

_QUERY = "the tuple lease renewal leaves the attempts counter alone"
_TEXT = "tuple_renew extends the claim lease and leaves the attempts counter alone"


def _seed(collection: str, content_type: str, agents: list[str], owner: str) -> list[str]:
    ids = [hashlib.sha256(f"{collection}:{i}".encode()).hexdigest() for i in range(len(agents))]
    metas = [
        {"chunk_text_hash": c, "title": f"{owner}-{i}", "source_agent": a}
        for i, (c, a) in enumerate(zip(ids, agents))
    ]
    seed_chunks_direct(
        collection, ids=ids, documents=[f"{_TEXT}, variant {i}" for i in range(len(agents))],
        embed=True, metadatas=metas,
    )
    give_chunks_a_live_owner(collection, ids, content_type=content_type, owner_name=owner)
    return ids


@pytest.fixture
def seeded(t2_service_env, monkeypatch):
    client = hvc.HttpVectorClient(tenant=t2_service_env)
    note_ids = _seed(_NOTES, "knowledge", ["agent-x", "agent-y", "agent-x"], "scope-notes")
    _seed(_CODE, "code", ["nexus-indexer"] * 3, "scope-code")
    fetched: list[tuple[str, list[str]]] = []
    real = client.get_embeddings

    def _spy(collection, ids, *a, **kw):
        fetched.append((collection, sorted(ids)))
        return real(collection, ids, *a, **kw)

    monkeypatch.setattr(client, "get_embeddings", _spy)
    return client, note_ids, fetched


@pytest.mark.parametrize("route", ["1", "0"], ids=["per-collection-route", "batched-search"])
def test_the_rows_carry_source_agent_and_only_the_mixed_collection_is_fetched(seeded, monkeypatch, route):
    client, note_ids, fetched = seeded
    monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, route)

    results = search_cross_corpus(
        _QUERY, [_NOTES, _CODE], 6, client, threshold_override=float("inf"), cluster_by=None,
    )

    by_col: dict[str, list] = {}
    for r in results:
        by_col.setdefault(r.collection, []).append(r)
    assert {c: len(v) for c, v in by_col.items()} == {_NOTES: 3, _CODE: 3}
    assert {r.metadata.get("source_agent") for r in by_col[_NOTES]} == {"agent-x", "agent-y"}
    assert {r.metadata.get("source_agent") for r in by_col[_CODE]} == {"nexus-indexer"}

    assert fetched == [(_NOTES, sorted(note_ids))], "vectors for the mixed-agent collection only"
    assert all(r.metadata.get("_contradiction_flag") for r in by_col[_NOTES]), "near-identical notes, two agents"
    assert not any(r.metadata.get("_contradiction_flag") for r in by_col[_CODE])
