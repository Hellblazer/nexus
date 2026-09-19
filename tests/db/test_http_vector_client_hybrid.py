# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 P2.1 (bead nexus-lqo4p.6) — ``HttpVectorClient.hybrid_search``.

The first client caller of ``POST /v1/vectors/hybrid-search``. The route has
existed since RDR-155 P3 and its only consumer was the cloud deployment, so
nothing in this repository could reach it (BUG-0148's coverage gap).

These tests drive the method into existence: the route it posts to, the body
it builds, the rows it returns verbatim, and — the part a body-only copy of
``search()`` would drop silently — the rerank envelope arriving from the
shared server-side tail. The exhaustive wire pin (seven fields and no more)
and the three side effects in isolation belong to bead .7, which extends this
file and adds the route-label sibling in
``tests/db/test_http_vector_client.py``.

Every test here fakes the MODULE-GLOBAL ``_post``, which is also the seam
bead .10's planted BUG-0148 fixture injects through. An implementation that
reached the network any other way would leave these tests passing over
behaviour they no longer reach.
"""
from __future__ import annotations

import pytest

from nexus import rate_brake
from nexus.db import http_vector_client as hvc


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hvc.HttpVectorClient, "__init__", lambda self: None)
    c = hvc.HttpVectorClient()
    c._tenant = "t"
    return c


def _patch_post(monkeypatch, response, captured):
    def fake_post(path, body, tenant=None):
        captured.append({"path": path, "body": body, "tenant": tenant})
        return response
    monkeypatch.setattr(hvc, "_post", fake_post)


ROWS = [
    {"id": "a", "content": "resolve_active_session_id", "distance": 0.31,
     "collection": "code__nexus-1-1", "metadata": {}, "retention": "full"},
]


def test_posts_to_the_hybrid_route_and_returns_rows_verbatim(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    rows = client.hybrid_search("resolve_active_session_id",
                                ["code__nexus-1-1"], n_results=5)

    assert captured[0]["path"] == "/v1/vectors/hybrid-search"
    assert captured[0]["tenant"] == "t"
    assert captured[0]["body"] == {
        "query": "resolve_active_session_id",
        "collections": ["code__nexus-1-1"],
        "n_results": 5,
    }
    # Verbatim: the route returns the same flat row shape /search returns and
    # this method adds nothing to it. In particular there is no fusion score
    # to add — the live route selects cosine distance only.
    assert rows == ROWS


def test_optional_fields_are_omitted_when_not_requested(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"])

    body = captured[0]["body"]
    for absent in ("where", "include_source_uri", "rerank", "rerank_top_k"):
        assert absent not in body, f"{absent} must be omitted when not requested"


def test_where_and_source_uri_reach_the_body(client, monkeypatch):
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"],
                         where={"lang": "python"}, include_source_uri=True)

    body = captured[0]["body"]
    assert body["where"] == {"lang": "python"}
    assert body["include_source_uri"] is True


def test_rerank_envelope_unpacked_exactly_as_search_unpacks_it(client, monkeypatch):
    """Both routes share ONE rerank tail server-side
    (``VectorHandler#sendSearchResult``), so the envelope this method receives
    is byte-identical to ``search()``'s.

    WHAT THIS DOES AND DOES NOT PIN, corrected at P2.2 after the P2.1 review
    caught the original wording claiming more than it delivers. Both call
    sites now run the same ``_unpack_rerank_envelope`` function object, so
    there is no copy left to drift and this equality is trivially true for
    any bug INSIDE that function. The unpacking's own correctness — the
    retry-after clamp, the stale-engine branch, the missing-flag branch — is
    pinned by ``tests/db/test_http_vector_client_rerank.py``, not here.

    What this assertion really guards is a FUTURE RE-FORK: someone inlining
    the handling back into one call site and not the other. That is a real
    risk and worth a test, but it is narrower than "a copy that drifts",
    which is what this docstring used to say.
    """
    envelope = {
        "results": [{"id": "a", "content": "x", "distance": 0.2,
                     "collection": "code__nexus-1-1", "rerank_score": 0.9}],
        "rerank_degraded": False,
        "rerank_model": "rerank-2.5",
    }

    _patch_post(monkeypatch, envelope, [])
    hybrid_meta: dict = {}
    hybrid_rows = client.hybrid_search("q", ["code__nexus-1-1"], rerank=True,
                                       rerank_top_k=3, rerank_meta_out=hybrid_meta)

    _patch_post(monkeypatch, envelope, [])
    search_meta: dict = {}
    search_rows = client.search("q", ["code__nexus-1-1"], rerank=True,
                                rerank_top_k=3, rerank_meta_out=search_meta)

    assert hybrid_rows == search_rows
    assert hybrid_meta == search_meta
    assert hybrid_meta["degraded"] is False
    assert hybrid_meta["model"] == "rerank-2.5"


def test_stale_engine_bare_array_reports_the_same_degrade(client, monkeypatch):
    """An engine predating the fused stage ignores ``rerank`` and returns a
    bare array. One-engine doctrine: report the degrade with the convergence
    remedy, never refuse, never stay silent.
    """
    _patch_post(monkeypatch, ROWS, [])
    meta: dict = {}
    rows = client.hybrid_search("q", ["code__nexus-1-1"], rerank=True,
                                rerank_meta_out=meta)

    assert rows == ROWS
    assert meta["degraded"] is True
    assert meta["stale_engine"] is True
    assert "nx upgrade" in meta["error"]


def test_rerank_top_k_is_only_sent_alongside_rerank(client, monkeypatch):
    """``rerank_top_k`` without ``rerank: true`` is a 400 from the engine
    (``sendSearchResult``: "set both or neither"). The client never builds
    that body — the field is set inside the rerank branch only.
    """
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"], rerank_top_k=3)

    assert "rerank_top_k" not in captured[0]["body"]
    assert "rerank" not in captured[0]["body"]


# ── P2.2 (bead nexus-lqo4p.7): the wire body, and the side effects a
# body-only test cannot see ──────────────────────────────────────────────────
#
# "And no more" is the load-bearing half. cluster_by, threshold and structured
# are client-local and never reach the wire, so a test asserting only that the
# seven fields are PRESENT would pass while one of those three leaked onto the
# request. These assert the exact key set.


def test_minimal_call_sends_exactly_three_keys_on_the_hybrid_route(client, monkeypatch):
    """Exact key set, not a subset — and the path asserted alongside it.

    A deliberate strengthening of the precedent at
    ``tests/db/test_http_vector_client_rerank.py``'s
    ``test_no_rerank_request_body_and_return_unchanged``, which asserts only
    ``"rerank" not in body``. A subset assertion cannot see a leaked field,
    and a copy of ``search()`` that forgot to change the path is exactly what
    it misses — hence both halves here.
    """
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"])

    assert captured[0]["path"] == "/v1/vectors/hybrid-search"
    assert set(captured[0]["body"]) == {"query", "collections", "n_results"}


def test_maximal_call_sends_exactly_the_seven_wire_fields(client, monkeypatch):
    """Every optional set at once: seven keys, no eighth.

    The set is identical to /search's, and the engine reads all seven —
    five in ``VectorHandler#handleHybridSearch``, and ``rerank`` plus
    ``rerank_top_k`` off the same body in the shared ``sendSearchResult``
    tail. There is no eighth field to send.
    """
    rate_brake.reset_brake()
    captured: list[dict] = []
    _patch_post(monkeypatch, ROWS, captured)

    client.hybrid_search("q", ["code__nexus-1-1"], n_results=5,
                         where={"lang": "python"}, include_source_uri=True,
                         rerank=True, rerank_top_k=3)

    assert set(captured[0]["body"]) == {
        "query", "collections", "n_results", "where",
        "include_source_uri", "rerank", "rerank_top_k",
    }
    rate_brake.reset_brake()


def test_client_local_parameters_are_unreachable_from_this_method(client, monkeypatch):
    """``cluster_by``, ``threshold`` and ``structured`` are post-retrieval
    processing that never reaches the wire, so they are absent from the
    signature rather than accepted and ignored. Passing one is a TypeError,
    which is what makes "never reaches the wire" true by construction here
    rather than by a body assertion that could go stale.
    """
    _patch_post(monkeypatch, ROWS, [])

    for kwarg in ("cluster_by", "threshold", "structured"):
        with pytest.raises(TypeError):
            client.hybrid_search("q", ["code__nexus-1-1"], **{kwarg: "x"})


def test_the_brake_is_consulted_before_the_post_not_after(client, monkeypatch):
    """Ordering, not presence. A boolean "wait was called" cannot tell
    before from after, and after is the defect: a brake consulted once the
    request is already away paces nothing. Both fakes record into one
    ordering list so the sequence itself is the assertion.
    """
    rate_brake.reset_brake()
    order: list[str] = []

    def fake_post(path, body, tenant=None):
        order.append("post")
        return ROWS

    class _RecordingBrake:
        def wait(self):
            order.append("wait")
            return 0.0

    monkeypatch.setattr(hvc, "_post", fake_post)
    monkeypatch.setattr(rate_brake, "get_brake", lambda: _RecordingBrake())

    client.hybrid_search("q", ["code__nexus-1-1"], rerank=True)

    assert order == ["wait", "post"]
    rate_brake.reset_brake()


def test_a_rate_limited_degrade_trips_the_shared_brake_with_the_reported_value(
    client, monkeypatch,
):
    """The response-side half of the brake wiring: the engine reports a
    structured ``rerank_retry_after_seconds`` only when the degrade cause was
    Voyage rate-limiting the reranker, and that value feeds the process-wide
    brake so every OTHER writer paces itself. This call never retries — the
    server already served a 200.
    """
    _patch_post(monkeypatch, {
        "results": [{"id": "a", "content": "x", "distance": 0.2,
                     "collection": "code__nexus-1-1"}],
        "rerank_degraded": True,
        "rerank_error": "Voyage AI is rate limiting the reranker (retry after ~7s): boom",
        "rerank_retry_after_seconds": 7,
    }, [])
    rate_brake.reset_brake()

    meta: dict = {}
    rows = client.hybrid_search("q", ["code__nexus-1-1"], rerank=True,
                                rerank_meta_out=meta)

    assert meta["degraded"] is True
    assert meta["retry_after_seconds"] == 7
    assert rows and "rerank_score" not in rows[0]
    brake = rate_brake.get_brake()
    assert brake._resume_at > brake._clock()  # noqa: SLF001 — asserting the trip actually landed
    rate_brake.reset_brake()
