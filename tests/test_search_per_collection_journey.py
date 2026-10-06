# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-tu8wp.2: the client against the REAL engine's
``POST /v1/vectors/search-per-collection`` (the nexus-tu8wp.1 route, run from
the substrate's service jar).

``tests/test_search_per_collection_route.py`` pins the client's request and
envelope handling against a fake transport. This file is the other half: the
same flows end to end, so a disagreement between the client and what the
engine really returns (a field name, the header, the per-arm SQL's own
results) cannot hide behind a fake that the client was written against.

The collections use one local model token (bge-768) and one corpus class
(code, over-fetch multiplier 2) so the reference is exact: with
``n_results=3`` the pre-batching fan-out asked each collection for
``max(5, 3 * 2) = 6`` rows, which is the ``per_collection_k`` the route is
sent.
"""
from __future__ import annotations

import hashlib

import pytest

import nexus.db.http_vector_client as hvc
from nexus.search_engine import (
    SearchDiagnostics,
    apply_ranking_boosts,
    search_cross_corpus,
)
from tests._catalog_fixture_ops import give_chunks_a_live_owner
from tests._chunk_seed import seed_chunks_direct

_MODEL = "bge-base-en-v15-768"
_BIG = f"code__tu8wp-big__{_MODEL}__v1"        # 40 rows, all close to the query
_SMALL = f"code__tu8wp-small__{_MODEL}__v1"    # 5 rows, farther
_MID = f"code__tu8wp-mid__{_MODEL}__v1"        # 12 rows, in between
_TINY_A = f"code__tu8wp-tiny-a__{_MODEL}__v1"  # 8 rows
_TINY_B = f"code__tu8wp-tiny-b__{_MODEL}__v1"  # 4 rows
_GHOST = f"code__tu8wp-ghost__{_MODEL}__v1"    # never registered

_QUERY = "tuple lease renewal does not change the attempts counter"


def _seed(collection: str, texts: list[str], owner: str) -> list[str]:
    ids = [hashlib.sha256(f"{collection}:{i}".encode()).hexdigest() for i in range(len(texts))]
    metas = [{"chunk_text_hash": c, "title": f"{owner}-{i}.py:1-3"} for i, c in enumerate(ids)]
    seed_chunks_direct(collection, ids=ids, documents=texts, embed=True, metadatas=metas)
    give_chunks_a_live_owner(collection, ids, content_type="code", owner_name=owner)
    return ids


@pytest.fixture
def seeded(t2_service_env):
    """A client bound to a fresh tenant with the fixture collections seeded."""
    client = hvc.HttpVectorClient(tenant=t2_service_env)
    _seed(_BIG, [
        f"tuple_renew extends the claim lease and leaves the attempts counter alone, variant {i}"
        for i in range(40)
    ], "tu8wp-big")
    _seed(_SMALL, [
        f"tomato plants need watering twice a week in summer, garden note {i}" for i in range(5)
    ], "tu8wp-small")
    _seed(_MID, [
        f"a lease is renewed by the holder before it lapses, renewal note {i}" for i in range(12)
    ], "tu8wp-mid")
    _seed(_TINY_A, [
        f"lease renewal and counters in a queue, observation {i}" for i in range(8)
    ], "tu8wp-tiny-a")
    _seed(_TINY_B, [
        f"counter of attempts for a retried delivery, observation {i}" for i in range(4)
    ], "tu8wp-tiny-b")
    return client


def _run(client, cols, n, **kw):
    kw.setdefault("threshold_override", float("inf"))
    kw.setdefault("cluster_by", None)
    return search_cross_corpus(_QUERY, cols, n, client, **kw)


def _signature(results):
    return sorted((r.collection, r.id, round(r.distance, 9)) for r in results)


def _old_client(client):
    """The same client with the per-collection route switched off: every
    search goes through the batched ``/search`` path."""
    old = hvc.HttpVectorClient(tenant=client._tenant)
    old.supports_per_collection_search = False  # type: ignore[misc]
    return old


def test_the_real_engine_answers_the_route_with_the_envelope_the_client_validates(seeded):
    env = seeded.search_per_collection(
        _QUERY, [_BIG, _SMALL, _MID], per_collection_k=6, limit=300,
    )
    assert env is not None, "the engine jar in this checkout carries the route (nexus-tu8wp.1)"
    assert env["per_collection_k"] == 6 and env["limit"] == 300
    by = {e["collection"]: e for e in env["per_collection"]}
    assert set(by) == {_BIG, _SMALL, _MID}
    assert all(e["error"] is None and e["error_kind"] is None for e in by.values())
    assert by[_BIG]["raw_count"] == 6 and by[_SMALL]["raw_count"] == 5
    assert {r["collection"] for r in env["results"]} == {_BIG, _SMALL, _MID}
    assert env["skipped_collections"] == []


def test_the_route_equals_the_prebatching_per_collection_fanout(seeded):
    """The reference the parity gate holds the route to: one call per
    collection at ``per_k = max(5, n * mult)``. The fixture is crowded (the
    big collection holds 40 near rows against 6 requested), so a flat batched
    call could not reproduce it; the route must."""
    cols = [_BIG, _SMALL, _MID]
    old = _old_client(seeded)
    reference = []
    for c in cols:
        reference.extend(_run(old, [c], 3))

    served = _run(seeded, cols, 3)

    assert len({r.collection for r in reference}) == 3, "fixture must exercise every collection"
    assert _signature(served) == _signature(reference)
    # And after the CLI/MCP ranking boosts (hybrid off), the page a user reads.
    page = lambda rs: [(r.collection, r.id) for r in apply_ranking_boosts(rs)[:10]]  # noqa: E731
    assert page(served) == page(reference)


def test_the_route_equals_the_batched_path_at_the_deep_floor_when_nothing_crowds(seeded):
    """Where no collection holds more than its share, the flat batched call
    at the deep floor returns every row, and so does the route."""
    cols = [_SMALL, _TINY_A, _TINY_B]
    old = _old_client(seeded)
    batched = _run(old, cols, 10, deep_candidates=True)
    served = _run(seeded, cols, 10, deep_candidates=True)
    assert len(batched) == 5 + 8 + 4
    assert _signature(served) == _signature(batched)


def test_a_dominant_collection_does_not_starve_a_small_one(seeded):
    """The defect the route exists for. At the lean floor (n=3: a 5-row share
    per collection) the flat batched call fills its 10 slots from the big
    collection alone."""
    cols = [_BIG, _SMALL]
    old = _old_client(seeded)
    flat = _run(old, cols, 3)
    assert not [r for r in flat if r.collection == _SMALL], (
        "precondition: the fixture really crowds the small collection out of the flat call"
    )
    served = _run(seeded, cols, 3)
    assert len([r for r in served if r.collection == _SMALL]) == 5


def test_a_collection_the_engine_skips_is_accounted_for_not_refused(seeded):
    """``X-Nexus-Skipped-Collections`` through the real transport: the engine
    drops the unregistered name from the fan-out, reports it in the header, and
    gives it no ``per_collection`` entry."""
    diags: list[SearchDiagnostics] = []
    results = _run(seeded, [_BIG, _GHOST], 3, diagnostics_out=diags)
    assert {r.collection for r in results} == {_BIG}
    assert diags[0].failed_collections == {}
    assert diags[0].per_collection[_GHOST][:2] == (0, 0)
    assert diags[0].per_collection[_BIG][0] == 6

    env = seeded.search_per_collection(
        _QUERY, [_BIG, _GHOST], per_collection_k=6, limit=300,
    )
    assert env["skipped_collections"] == [_GHOST]
    assert [e["collection"] for e in env["per_collection"]] == [_BIG]


def test_finite_thresholds_reach_the_engine_and_filter_there(seeded):
    cols = [_BIG, _SMALL]
    everything = _run(seeded, cols, 3)
    cutoff = sorted(r.distance for r in everything)[len(everything) // 2]
    diags: list[SearchDiagnostics] = []
    kept = _run(seeded, cols, 3, threshold_override=cutoff, diagnostics_out=diags)
    assert kept and len(kept) < len(everything)
    assert all(r.distance <= cutoff for r in kept)
    dropped = sum(d for (_raw, d, _t, _m) in diags[0].per_collection.values())
    assert dropped == len(everything) - len(kept)
