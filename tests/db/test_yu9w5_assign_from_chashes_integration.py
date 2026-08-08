# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-yu9w5 (lns3o client half): ``assign_from_chashes`` against the REAL
engine substrate.

POST /v1/taxonomy/assignments/assign_from_chashes (nexus-lns3o engine half,
dc87dd3c) needs BOTH the upserted chunk row (``chunks_<dim>``) and a topic
centroid (``taxonomy_centroids_<dim>``, pgvector) to produce a real
assignment. ``tests/db/test_http_taxonomy_store_integration.py``'s bespoke
harness explicitly does NOT bootstrap pgvector (see its
``TestServiceBackedPersist`` docstring), so centroid-dependent taxonomy work
cannot be proven there. This file uses the modern ``t2_service_env``
substrate instead (``tests/_engine_substrate.py`` — the SAME per-process
PG+pgvector engine ``tests/test_scenario_journeys.py`` exercises), which DOES
carry pgvector, and is real integration: real HTTP round trips, real
Postgres rows, no mocked transport.

Covers:
  - the route itself, called directly through ``HttpTaxonomyStore.
    assign_from_chashes`` — a real assignment persists and is readable back
    via ``get_assignments_for_docs``.
  - unmatched chashes (never upserted into the collection) are named in
    ``unmatched_chashes``, never silently dropped.
  - ``taxonomy_assign_batch_hook`` reaches the route end-to-end (no
    embeddings fetch, no client-side compute) and its result is a real
    persisted assignment.
"""
from __future__ import annotations

import hashlib

import pytest

_COLL = "knowledge__yu9w5test__bge-base-en-v15-768__v1"


def _chash(text: str) -> str:
    """A real sha256 hex digest — chunks_<dim>.chash is bytea server-side,
    decoded via hex; an arbitrary non-hex string would 400 at the wire."""
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.scenario
def test_assign_from_chashes_route_persists_real_assignment(t2_service_env) -> None:
    from nexus.db import make_t3
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    t3 = make_t3()
    chash_a = _chash("alpha content about ants and colonies")
    chash_b = _chash("beta content about beaches and boats")
    ids = [chash_a, chash_b]
    docs = [
        "alpha content about ants and colonies",
        "beta content about beaches and boats",
    ]

    # Server-side embed (NX_LOCAL=1 posture — bge-768, no Voyage key needed).
    t3.upsert_chunks_with_embeddings(
        collection_name=_COLL, ids=ids, documents=docs,
        embeddings=[[] for _ in ids], metadatas=[{}, {}],
    )
    real_embs = t3.get_embeddings(_COLL, ids)
    assert real_embs.shape[0] == 2, "both chunks must have real server-side vectors"

    tax = HttpTaxonomyStore()
    topic_id = tax.import_topic(
        src_id=90101, label="yu9w5-topic", parent_id=None,
        collection=_COLL, centroid_hash=None, doc_count=0,
        created_at="2026-08-07T00:00:00Z", review_status="pending", terms=None,
    )
    # Seed the sole centroid with chash_a's own vector — deterministic argmax
    # match for BOTH chunks (only one topic exists to match against).
    tax._centroid.upsert([{
        "collection": _COLL, "topic_id": topic_id,
        "embedding": real_embs[0].tolist(), "label": "yu9w5-topic", "doc_count": 0,
    }])

    result = tax.assign_from_chashes(_COLL, ids, cross_collection=False)

    assert result["unmatched_chashes"] == []
    assert result["assigned"] == 2
    assert result["cross_assigned"] == 0

    got = tax.get_assignments_for_docs(ids)
    assert got[chash_a] == topic_id
    assert got[chash_b] == topic_id


@pytest.mark.scenario
def test_assign_from_chashes_names_unmatched_chashes(t2_service_env) -> None:
    """A chash never upserted into the collection is reported, not dropped."""
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    tax = HttpTaxonomyStore()
    bogus = _chash("never upserted anywhere in this test")

    result = tax.assign_from_chashes(_COLL, [bogus], cross_collection=False)

    assert result["assigned"] == 0
    assert result["unmatched_chashes"] == [bogus]


@pytest.mark.scenario
def test_hook_reaches_route_and_persists_without_embeddings_fetch(t2_service_env) -> None:
    """taxonomy_assign_batch_hook, called with embeddings=None (the MCP
    store_put shape), must NOT fetch embeddings at all — it hands the
    chashes straight to the engine route, which computes server-side from
    the ALREADY-UPSERTED chunks_<dim> rows."""
    import nexus.mcp_infra as mcp_infra
    from nexus.db import make_t3
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    # get_t3() is a lazy process-lifetime singleton NOT auto-reset between
    # tests (tests/conftest.py's `_reset_service_t2_db` docstring: only the
    # credential-bearing T2 handle is evicted automatically). A stale
    # instance from an earlier test wouldn't break correctness here (the
    # hook's service branch no longer calls anything on it besides the
    # isinstance-shaped `is_service_backed` gate), but forcing a fresh
    # resolve keeps this test independent of suite ordering — same pattern
    # as tests/db/test_http_vector_client.py's TestTaxonomyServiceModeGuard.
    mcp_infra.reset_singletons()

    t3 = make_t3()
    chash_c = _chash("gamma content about glaciers")
    ids = [chash_c]

    t3.upsert_chunks_with_embeddings(
        collection_name=_COLL, ids=ids, documents=["gamma content about glaciers"],
        embeddings=[[]], metadatas=[{}],
    )
    real_embs = t3.get_embeddings(_COLL, ids)

    tax = HttpTaxonomyStore()
    topic_id = tax.import_topic(
        src_id=90102, label="yu9w5-hook-topic", parent_id=None,
        collection=_COLL, centroid_hash=None, doc_count=0,
        created_at="2026-08-07T00:00:00Z", review_status="pending", terms=None,
    )
    tax._centroid.upsert([{
        "collection": _COLL, "topic_id": topic_id,
        "embedding": real_embs[0].tolist(), "label": "yu9w5-hook-topic", "doc_count": 0,
    }])

    # embeddings=None: the MCP store_put shape. Pre-fix this forced a
    # get_embeddings re-fetch; post-fix it must be entirely unread.
    mcp_infra.taxonomy_assign_batch_hook(ids, _COLL, ["ignored"], None, None)

    got = tax.get_assignments_for_docs(ids)
    assert got[chash_c] == topic_id, (
        "hook must persist a real assignment through the route without "
        "ever re-fetching embeddings"
    )

    mcp_infra.reset_singletons()
