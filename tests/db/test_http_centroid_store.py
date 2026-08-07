# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpCentroidStore (RDR-156 bead nexus-t1hnc.5).

Uses an httpx.MockTransport fake of the /v1/taxonomy/centroids/* endpoints to
verify: correct HTTP calls, AssignResult mapping, chroma-envelope shape adaptation
(ids/embeddings/metadatas), and the Phase-1-gate error-translation contract
(HTTP 400 -> [] / None; transport/5xx -> raise).

Cross-language end-to-end is the integration test (-m integration).
"""
from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nexus.db.t2.taxonomy_compute import AssignResult
from nexus.db.t2.http_centroid_store import HttpCentroidStore

TOKEN = "fake-centroid-token"


# ── In-memory fake centroid service ────────────────────────────────────────────


class _FakeCentroidService:
    """Minimal in-memory implementation of the centroid endpoints."""

    def __init__(self) -> None:
        # (collection, topic_id) -> {embedding, label, doc_count}
        self.rows: dict[tuple[str, int], dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path.replace("/v1/taxonomy/centroids", "")
        qs = parse_qs(urlparse(str(request.url)).query)
        body = json.loads(request.content) if request.content else {}

        if request.method == "POST" and path == "/upsert":
            for r in body["records"]:
                self.rows[(r["collection"], int(r["topic_id"]))] = {
                    "embedding": r["embedding"],
                    "label": r.get("label"),
                    "doc_count": r.get("doc_count"),
                }
            return _json(200, {"ok": True, "count": len(body["records"])})

        if request.method == "POST" and path == "/query":
            emb = body["embedding"]
            # Simulated sentinels (O2/M1/S3 contract tests).
            if body["collection"] == "dimmismatch":
                # Real service dim-mismatch message contains "taxonomy_centroids".
                return _json(400, {"error": f"query embedding is {len(emb)}-dim — "
                                            "no taxonomy_centroids_X table"})
            if body["collection"] == "badrequest":
                # A non-dimension 400 (caller bug) — must re-raise, not swallow.
                return _json(400, {"error": "nResults must be >= 1"})
            if body["collection"] == "boom":
                return _json(500, {"error": "internal"})
            cross = body.get("cross_collection", False)
            hits = []
            for (coll, tid), rec in self.rows.items():
                if cross and coll == body["collection"]:
                    continue
                if not cross and coll != body["collection"]:
                    continue
                hits.append((tid, _cosine_sim(emb, rec["embedding"])))
            hits.sort(key=lambda h: h[1], reverse=True)
            hits = hits[: body.get("n_results", 1)]
            return _json(200, [{"topic_id": t, "similarity": s} for t, s in hits])

        if request.method == "GET" and path == "/count":
            coll = qs.get("collection", [None])[0]
            n = sum(1 for (c, _t) in self.rows if coll is None or c == coll)
            return _json(200, {"count": n})

        if request.method == "GET" and path == "/dimension":
            if not self.rows:
                return _json(200, {"dimension": -1})
            dim = len(next(iter(self.rows.values()))["embedding"])
            return _json(200, {"dimension": dim})

        if request.method == "GET" and path in ("/by_collection", "/foreign"):
            coll = qs["collection"][0]
            out = []
            for (c, tid), rec in sorted(self.rows.items()):
                match = (c == coll) if path == "/by_collection" else (c != coll)
                if match:
                    out.append({
                        "topic_id": tid, "embedding": rec["embedding"],
                        "label": rec["label"], "collection": c,
                        "doc_count": rec["doc_count"],
                    })
            return _json(200, out)

        if request.method == "POST" and path == "/delete":
            coll, tids = body["collection"], set(body["topic_ids"])
            removed = [k for k in self.rows if k[0] == coll and k[1] in tids]
            for k in removed:
                del self.rows[k]
            return _json(200, {"deleted": len(removed)})

        if request.method == "POST" and path == "/purge":
            coll = body["collection"]
            removed = [k for k in self.rows if k[0] == coll]
            for k in removed:
                del self.rows[k]
            return _json(200, {"deleted": len(removed)})

        return _json(404, {"error": "not found"})


def _json(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@pytest.fixture
def store() -> HttpCentroidStore:
    fake = _FakeCentroidService()
    s = HttpCentroidStore(
        base_url="http://svc",
        _token=TOKEN,
        _transport=httpx.MockTransport(fake.handler),
    )
    # Expose the fake for assertions that need server-side state.
    s._fake = fake  # type: ignore[attr-defined]
    yield s
    s.close()


# ── Tests ───────────────────────────────────────────────────────────────────────


def test_upsert_count_dimension(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "knowledge__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "x", "doc_count": 5},
        {"collection": "knowledge__a", "topic_id": 2, "embedding": [0.0, 1.0], "label": "y", "doc_count": 3},
    ])
    assert store.count("knowledge__a") == 2
    assert store.count() == 2
    assert store.dimension() == 2


def test_upsert_empty_is_noop(store: HttpCentroidStore) -> None:
    store.upsert([])
    assert store.count() == 0
    assert store.dimension() == -1


def test_nearest_returns_assignresult(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__n", "topic_id": 10, "embedding": [1.0, 0.0], "label": "near", "doc_count": 1},
        {"collection": "k__n", "topic_id": 20, "embedding": [0.0, 1.0], "label": "far", "doc_count": 1},
    ])
    hit = store.nearest([1.0, 0.0], "k__n")
    assert isinstance(hit, AssignResult)
    assert hit.topic_id == 10
    assert hit.similarity == pytest.approx(1.0, abs=1e-6)


def test_ann_query_ordering(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__o", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "k__o", "topic_id": 2, "embedding": [0.6, 0.8], "label": "b", "doc_count": 1},
    ])
    hits = store.ann_query([1.0, 0.0], "k__o", n_results=2)
    assert [h.topic_id for h in hits] == [1, 2]
    assert hits[0].similarity > hits[1].similarity


def test_cross_collection_excludes_source(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "k__b", "topic_id": 2, "embedding": [1.0, 0.0], "label": "b", "doc_count": 1},
    ])
    hits = store.ann_query([1.0, 0.0], "k__a", cross_collection=True, n_results=5)
    assert [h.topic_id for h in hits] == [2]


def test_get_by_collection_envelope_shape(store: HttpCentroidStore) -> None:
    # nullable label/doc_count must survive as None in metadatas.
    store.upsert([
        {"collection": "k__e", "topic_id": 7, "embedding": [0.6, 0.8], "label": None, "doc_count": None},
    ])
    env = store.get_by_collection("k__e")
    assert env["ids"] == ["k__e:7"]
    assert env["embeddings"] == [[0.6, 0.8]]
    assert env["metadatas"] == [
        {"topic_id": 7, "label": None, "collection": "k__e", "doc_count": None}
    ]


def test_get_foreign_excludes_given(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__fa", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "d__fb", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
    ])
    env = store.get_foreign("k__fa")
    assert env["ids"] == ["d__fb:2"]
    assert {m["collection"] for m in env["metadatas"]} == {"d__fb"}


def test_delete_and_purge(store: HttpCentroidStore) -> None:
    store.upsert([
        {"collection": "k__d", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        {"collection": "k__d", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
    ])
    assert store.delete_ids("k__d", [1]) == 1
    assert store.count("k__d") == 1
    assert store.purge("k__d") == 1
    assert store.count("k__d") == 0


def test_delete_empty_is_noop(store: HttpCentroidStore) -> None:
    assert store.delete_ids("k__d", []) == 0


def test_nearest_none_on_empty_collection(store: HttpCentroidStore) -> None:
    # Collection has no centroids yet (service 200 []), the first-run case before
    # discover_topics runs — nearest() -> None, NOT an error (M2).
    assert store.ann_query([1.0, 0.0], "k__empty") == []
    assert store.nearest([1.0, 0.0], "k__empty") is None


def test_o2_dim_mismatch_400_maps_to_none(store: HttpCentroidStore) -> None:
    # Dimension-mismatch 400 -> ann_query [] -> nearest None (oracle best-effort).
    assert store.ann_query([1.0, 0.0], "dimmismatch") == []
    assert store.nearest([1.0, 0.0], "dimmismatch") is None


def test_non_dimension_400_reraises_not_swallowed(store: HttpCentroidStore) -> None:
    # A non-dim 400 (caller bug) must surface, never silently empty (M1/S3 fail-loud).
    with pytest.raises(httpx.HTTPStatusError):
        store.ann_query([1.0, 0.0], "badrequest")


def test_o2_server_error_propagates_not_swallowed(store: HttpCentroidStore) -> None:
    # 5xx is RAISED, not silently None (fail-loud divergence from the oracle swallow).
    with pytest.raises(httpx.HTTPStatusError):
        store.nearest([1.0, 0.0], "boom")


# ── nexus-2mb6n: per-store-instance centroid cache ─────────────────────────────


def _counting_store() -> tuple[HttpCentroidStore, _FakeCentroidService, dict[str, int]]:
    """A store wired to a fake service that also counts by_collection/foreign
    hits — the call-count spy the fetch-once test needs."""
    fake = _FakeCentroidService()
    counts = {"by_collection": 0, "foreign": 0}
    orig_handler = fake.handler

    def counting_handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path.replace("/v1/taxonomy/centroids", "")
        if request.method == "GET" and path == "/by_collection":
            counts["by_collection"] += 1
        elif request.method == "GET" and path == "/foreign":
            counts["foreign"] += 1
        return orig_handler(request)

    s = HttpCentroidStore(
        base_url="http://svc",
        _token=TOKEN,
        _transport=httpx.MockTransport(counting_handler),
    )
    return s, fake, counts


def test_get_by_collection_fetched_once_across_repeated_calls() -> None:
    store, _fake, counts = _counting_store()
    try:
        store.upsert([
            {"collection": "k__cache", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        ])
        first = store.get_by_collection("k__cache")
        second = store.get_by_collection("k__cache")
        third = store.get_by_collection("k__cache")
        assert first == second == third
        assert counts["by_collection"] == 1, (
            f"expected exactly one network fetch, got {counts['by_collection']}"
        )
    finally:
        store.close()


def test_get_foreign_fetched_once_across_repeated_calls() -> None:
    store, _fake, counts = _counting_store()
    try:
        store.upsert([
            {"collection": "k__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
            {"collection": "k__b", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
        ])
        store.get_foreign("k__a")
        store.get_foreign("k__a")
        assert counts["foreign"] == 1

    finally:
        store.close()


def test_same_and_foreign_are_independently_cached() -> None:
    """(collection, 'same') and (collection, 'foreign') are distinct cache
    keys — fetching one must not satisfy the other from cache."""
    store, _fake, counts = _counting_store()
    try:
        store.upsert([
            {"collection": "k__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
            {"collection": "k__b", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
        ])
        store.get_by_collection("k__a")
        store.get_foreign("k__a")
        assert counts["by_collection"] == 1
        assert counts["foreign"] == 1
    finally:
        store.close()


def test_cache_invalidated_on_upsert() -> None:
    store, _fake, counts = _counting_store()
    try:
        store.upsert([
            {"collection": "k__inv", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        ])
        first = store.get_by_collection("k__inv")
        assert len(first["ids"]) == 1

        store.upsert([
            {"collection": "k__inv", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
        ])
        second = store.get_by_collection("k__inv")
        assert len(second["ids"]) == 2, "stale cache: upsert must invalidate"
        assert counts["by_collection"] == 2, "the post-mutation read must be a real fetch, not cached"
    finally:
        store.close()


def test_cache_invalidated_on_delete_ids() -> None:
    store, _fake, counts = _counting_store()
    try:
        store.upsert([
            {"collection": "k__del", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
            {"collection": "k__del", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
        ])
        store.get_by_collection("k__del")
        store.delete_ids("k__del", [1])
        after = store.get_by_collection("k__del")
        assert len(after["ids"]) == 1
        assert counts["by_collection"] == 2
    finally:
        store.close()


def test_cache_invalidated_on_purge() -> None:
    store, _fake, counts = _counting_store()
    try:
        store.upsert([
            {"collection": "k__purge", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        ])
        store.get_by_collection("k__purge")
        store.purge("k__purge")
        after = store.get_by_collection("k__purge")
        assert after["ids"] == []
        assert counts["by_collection"] == 2
    finally:
        store.close()


def test_mutation_in_one_collection_invalidates_others_foreign_cache() -> None:
    """A centroid change in collection A must invalidate B's cached
    get_foreign (which includes A's rows) — narrower per-collection
    invalidation would leave B's cross-collection view stale."""
    store, _fake, counts = _counting_store()
    try:
        store.upsert([
            {"collection": "k__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        ])
        before = store.get_foreign("k__b")
        assert before["ids"] == ["k__a:1"]

        store.upsert([
            {"collection": "k__a", "topic_id": 2, "embedding": [0.0, 1.0], "label": "a2", "doc_count": 1},
        ])
        after = store.get_foreign("k__b")
        assert {i.split(":")[1] for i in after["ids"]} == {"1", "2"}
        assert counts["foreign"] == 2
    finally:
        store.close()


def test_cache_failure_never_masks_as_silent_empty() -> None:
    """A fetch failure on a cold key must raise, never get cached as an
    empty/None result that later reads would silently trust."""
    fake = _FakeCentroidService()
    store = HttpCentroidStore(
        base_url="http://svc", _token=TOKEN,
        _transport=httpx.MockTransport(fake.handler),
    )
    try:
        # "boom" is the fake service's 500-sentinel collection for /query;
        # /by_collection has no such sentinel, so drive a raise via a
        # monkeypatched _get instead — the failure path under test is
        # "fetch() raises", regardless of cause.
        def _boom(*_a, **_kw):
            raise httpx.HTTPStatusError("boom", request=None, response=httpx.Response(500))

        import unittest.mock as mock
        with mock.patch.object(store, "_get", side_effect=_boom):
            with pytest.raises(httpx.HTTPStatusError):
                store.get_by_collection("k__fail")
        # Nothing cached — the NEXT call (now unpatched) does a real fetch.
        store.upsert([
            {"collection": "k__fail", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        ])
        env = store.get_by_collection("k__fail")
        assert len(env["ids"]) == 1
    finally:
        store.close()


def test_concurrent_same_key_reads_are_thread_safe_and_single_fetch() -> None:
    """Two threads requesting the SAME (collection, kind) concurrently must
    both see correct data, and only ONE underlying HTTP fetch should occur
    — the flush_concurrency=3 indexer path hits this exact race on every
    cold collection."""
    import threading
    import time as _time

    fake = _FakeCentroidService()
    counts = {"n": 0}
    orig_handler = fake.handler
    started = threading.Event()
    release = threading.Event()

    def slow_counting_handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path.replace("/v1/taxonomy/centroids", "")
        if request.method == "GET" and path == "/by_collection":
            counts["n"] += 1
            started.set()
            # Hold the response just long enough for a second thread to
            # queue up on the store's lock behind this fetch.
            release.wait(timeout=2.0)
        return orig_handler(request)

    store = HttpCentroidStore(
        base_url="http://svc", _token=TOKEN,
        _transport=httpx.MockTransport(slow_counting_handler),
    )
    try:
        store.upsert([
            {"collection": "k__thread", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
        ])

        results: list[dict] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(store.get_by_collection("k__thread"))
            except BaseException as exc:  # noqa: BLE001 — surfaced via errors list
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        started.wait(timeout=2.0)
        t2.start()
        _time.sleep(0.05)  # let t2 queue up behind the lock
        release.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        assert not errors, errors
        assert len(results) == 2
        assert results[0] == results[1]
        assert counts["n"] == 1, f"expected a single fetch under concurrency, got {counts['n']}"
    finally:
        store.close()


def test_cache_hit_for_one_key_never_blocks_behind_another_keys_slow_fetch() -> None:
    """nexus-2mb6n review round 2 (reviewer Important-1): the original
    single process-wide lock was held across the FULL fetch (including
    the mixin's gateway-retry envelope — worst case ~90s), so a slow miss
    on one key blocked a cache HIT on an already-warm, unrelated key. Per-
    key locking fixes this: warm key A, block key B's fetch mid-flight in
    another thread, and confirm A's hit still returns fast."""
    import threading
    import time as _time

    fake = _FakeCentroidService()
    b_started = threading.Event()
    b_release = threading.Event()
    orig_handler = fake.handler

    def gated_handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path.replace("/v1/taxonomy/centroids", "")
        qs = parse_qs(urlparse(str(request.url)).query)
        if (request.method == "GET" and path == "/by_collection"
                and qs.get("collection") == ["k__b"]):
            b_started.set()
            b_release.wait(timeout=2.0)
        return orig_handler(request)

    store = HttpCentroidStore(
        base_url="http://svc", _token=TOKEN,
        _transport=httpx.MockTransport(gated_handler),
    )
    try:
        store.upsert([
            {"collection": "k__a", "topic_id": 1, "embedding": [1.0, 0.0], "label": "a", "doc_count": 1},
            {"collection": "k__b", "topic_id": 2, "embedding": [0.0, 1.0], "label": "b", "doc_count": 1},
        ])
        # Warm key A BEFORE key B's slow fetch starts — a genuine cache hit.
        a_warm = store.get_by_collection("k__a")
        assert a_warm["ids"] == ["k__a:1"]

        b_result: list = []

        def fetch_b() -> None:
            b_result.append(store.get_by_collection("k__b"))

        t_b = threading.Thread(target=fetch_b)
        t_b.start()
        assert b_started.wait(timeout=2.0), "B's fetch never started"

        # A's hit must return quickly even though B's fetch is currently
        # blocked mid-flight — the whole point of per-key locking.
        a_t0 = _time.monotonic()
        a_hit = store.get_by_collection("k__a")
        a_elapsed = _time.monotonic() - a_t0
        assert a_elapsed < 0.5, (
            f"A's cache hit took {a_elapsed}s — blocked behind B's slow fetch "
            "(per-key locking regression)"
        )
        assert a_hit == a_warm

        b_release.set()
        t_b.join(timeout=2.0)
        assert b_result and b_result[0]["ids"] == ["k__b:2"]
    finally:
        store.close()


def test_mid_fetch_invalidation_does_not_poison_cache_epoch_guard() -> None:
    """nexus-2mb6n review round 2 RECURRENCE (critic-reproduced
    empirically): the per-key-locking redesign (finding 2) opened a new
    same-process race the coarser single-lock predecessor didn't have —
    a reader's fetch computes its response reflecting PRE-mutation state,
    a writer's upsert/invalidate fires on another thread WHILE that
    fetch is still in flight (client-side), and the reader would then
    cache its now-stale result AFTER the writer's clear, undoing the
    invalidation.

    The fetch-epoch guard closes this: the reader records the cache
    epoch before starting its fetch and refuses to write the cache if
    the epoch moved while the fetch was in flight. Post-settle, the
    cache must NOT contain the pre-mutation snapshot, and the next read
    must fetch fresh (real, post-mutation) data.
    """
    import threading
    import time as _time

    fake = _FakeCentroidService()
    reader_response_captured = threading.Event()
    writer_mutation_done = threading.Event()
    orig_handler = fake.handler

    def gated_handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path.replace("/v1/taxonomy/centroids", "")
        if request.method == "GET" and path == "/by_collection":
            # Compute the response NOW, against CURRENT (pre-mutation)
            # server state — this is the reader's HTTP round trip having
            # already captured its snapshot. Only the RETURN to the
            # client-side caller is delayed until after the writer's
            # mutation lands, simulating "stale data in flight" rather
            # than a request that hasn't reached the server yet.
            response = orig_handler(request)
            reader_response_captured.set()
            writer_mutation_done.wait(timeout=2.0)
            return response
        return orig_handler(request)

    store = HttpCentroidStore(
        base_url="http://svc", _token=TOKEN,
        _transport=httpx.MockTransport(gated_handler),
    )
    try:
        store.upsert([
            {"collection": "k__race", "topic_id": 1, "embedding": [1.0, 0.0], "label": "pre", "doc_count": 1},
        ])

        reader_result: list = []

        def reader() -> None:
            reader_result.append(store.get_by_collection("k__race"))

        t_reader = threading.Thread(target=reader)
        t_reader.start()
        assert reader_response_captured.wait(timeout=2.0), "reader's fetch never captured a response"

        # Writer mutates the SAME store instance while the reader's
        # fetch is still in flight (blocked, response captured but not
        # yet returned) — this is exactly the critic's reproduction.
        store.upsert([
            {"collection": "k__race", "topic_id": 2, "embedding": [0.0, 1.0], "label": "post", "doc_count": 1},
        ])
        writer_mutation_done.set()
        t_reader.join(timeout=2.0)

        # The reader's OWN captured result legitimately reflects
        # pre-mutation state (1 centroid) — that was live when its fetch
        # captured its snapshot. That part is correct and expected.
        assert len(reader_result[0]["ids"]) == 1

        # The assertion that matters: the cache must NOT have been
        # poisoned with that pre-mutation snapshot. A fresh read must
        # reflect the mutation (2 centroids) — proving the epoch guard
        # skipped the cache-store on the reader's stale-relative-to-epoch
        # fetch.
        after = store.get_by_collection("k__race")
        assert len(after["ids"]) == 2, (
            f"cache poisoned with pre-mutation data: got {after['ids']!r}"
        )
    finally:
        store.close()
