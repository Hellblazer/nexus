# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-217 P2.3 (bead nexus-lqo4p.8) — a nexus-side journey over
``POST /v1/vectors/hybrid-search``, against the real engine substrate.

THIS FILE IS THE GAP 2 CLOSURE, not a check on it. The route has had a
production consumer (the cloud deployment) and zero nexus-side callers since
it shipped at RDR-155 P3. The RDR-180 post-mortem recorded the consequence in
terms, at ``docs/rdr/post-mortem/180-content-address-chash-binary-32byte.md``
line 104: "no nexus journey could ever have driven hybrid-in-window, and
BUG-0148's surface was structurally unreachable from our test suite."

BUG-0148 (2026-07-19, engine v0.1.48) is the outage that lesson came from. An
``ALTER TYPE`` rewrote tables and reset planner statistics; the planner moved
sparse text-gate queries onto a budget-bounded HNSW plan; hybrid-search
returned zero rows in production while ``/health``, ``/version``, the smoke
round-trip and the aggregate STEP-6 exit code all stayed green. Every gate
watching was outside the surface that broke. This file puts a gate inside it.

Deliberately in the default suite and NOT integration-marked: a Gap 2 closure
that CI excludes from every PR closes nothing. The cost is the engine
substrate ``pytest -n auto`` already boots.

WHY THERE IS NO 503 TEST HERE, stated rather than skipped. The RDR's Test
Plan lists a fourth assertion — no pgvector backend configured returns 503,
never a silent vector fallback — and it is UNREACHABLE from this substrate by
construction, not merely awkward. ``Main.java:235`` constructs
``PgVectorRepository`` unconditionally and passes it at ``:313``; there is no
env knob, so a real jar boot always has the backend wired. The engine's own
pin builds a SECOND in-process service through a constructor overload the
shipped ``Main`` never calls (``new NexusService(0, TOKEN_A, svcDs)``,
``VectorHybridHttpTest.java:128``), and that test is the coverage of record
for the 503 leg: ``VectorHybridHttpTest.java:189``. A skipping test here
would read as absence of coverage, which would be false. A second reason not
to route it through the client even if a knob appeared: 503 is in
``gateway_backoff._GATEWAY_RETRY_CODES``, so ``_request`` would retry it
through the (2.0, 5.0, 10.0) schedule before raising, costing ~17s per
assertion to observe a status the engine already pins.
"""
from __future__ import annotations

import hashlib

import pytest

import nexus.db.http_vector_client as hvc
from nexus.db.http_vector_client import VectorServiceError
from tests._engine_substrate import ensure_engine, mint_test_tenant

# The substrate's real tier-1 embedding token. A guessed 1024 or 384 width
# disagrees with the registered row's 768 and the upsert 400s
# (tests/_catalog_fixture_ops.py:190-215 root-causes that failure).
_COLLECTION = "code__rdr217-owner__bge-base-en-v15-768__v1"

# The query's tokens appear LITERALLY in the seeded documents. The route gates
# on FTS and trigram before the vector ranks anything, so a purely semantic
# near-match returns zero rows — which is the whole difference between this
# route and /search.
_QUERY = "resolve_active_session_id"


def _seed(db: hvc.HttpVectorClient) -> list[str]:
    ids, docs, metas = [], [], []
    for i in range(6):
        chash = hashlib.sha256(f"rdr217:{i}".encode()).hexdigest()
        ids.append(chash)
        docs.append(
            f"def {_QUERY}_{i}(session):\n"
            f"    # {_QUERY} resolves the leased session id\n"
            f"    return session.id  # probe {i}\n"
        )
        metas.append({
            "chunk_text_hash": chash,
            "title": f"rdr217_probe_{i}.py:1-3",
            "rdr217_probe": "yes",
        })
    db.upsert_chunks_with_embeddings(
        _COLLECTION, ids=ids, documents=docs, embeddings=[], metadatas=metas,
    )
    return ids


def test_hybrid_search_returns_flat_rows_from_the_real_engine(t2_service_env) -> None:
    """Assertion 1: the flat row shape, from the live route.

    Rows carry (id, content, collection, distance, metadata, retention) — the
    same shape /search returns, declared byte-identically by
    ``plain_search_<dim>`` and by both branches behind this route. Seeded
    metadata flattens onto the row top level exactly as /search does, and a
    seeded chunk's ``retention`` is "full".

    Non-vacuity matters more here than usual: an empty list is exactly what
    BUG-0148 returned, and a test that only checked "no exception" would have
    passed straight through that outage.
    """
    tenant = t2_service_env
    db = hvc.HttpVectorClient(tenant=tenant)
    ids = _seed(db)

    rows = db.hybrid_search(_QUERY, [_COLLECTION], n_results=5)

    assert rows, (
        "the lexical gate matched nothing against documents carrying the "
        "query's literal tokens — this is BUG-0148's exact signature"
    )
    assert {r["id"] for r in rows} <= set(ids)
    for r in rows:
        assert set(r) >= {"id", "content", "collection", "distance"}
        assert r["collection"] == _COLLECTION
        assert isinstance(r["distance"], float)
        assert r["content"] and _QUERY in r["content"]
        assert r["retention"] == "full"
        # Flattened, not nested: the seeded key reaches the row top level.
        assert r["rdr217_probe"] == "yes"
    # The route selects NO fusion component — no ts_rank, no trigram score, no
    # RRF score. A score field appearing here would mean the live path changed.
    assert not any(k in rows[0] for k in ("score", "ts_rank", "rrf_score"))


def test_rerank_on_the_hybrid_route_carries_the_same_envelope(t2_service_env) -> None:
    """Assertion 2: envelope parity with /search, matching the engine's own
    ``hybridSearchCarriesTheSameRerankEnvelope``
    (``RerankStageIntegrationTest.java:270``).

    WHAT IS NOT ASSERTED, and why. Not ``rerank_degraded is False``. The
    engine pin runs against a scripted fake Voyage; this substrate runs local
    posture with no Voyage key and reranks with the lazily-initialised
    ms-marco cross-encoder, which ``Main.java:225`` degrades loud per request
    when the model is not yet provisioned. On a box whose ONNX models are not
    ``nx init``-provisioned, ``rerank_degraded`` is legitimately true, so
    pinning it False would make this test a fact about the box rather than
    about the route.

    The actual parity claim is the ENVELOPE SHAPE: ``rerank_meta_out``
    populated with all four keys whichever way the rerank went, and rows
    still flat and same-shaped as the non-rerank call. Both routes share one
    rerank tail server-side, so any divergence here is a client-side one.
    """
    tenant = t2_service_env
    db = hvc.HttpVectorClient(tenant=tenant)
    _seed(db)

    plain = db.hybrid_search(_QUERY, [_COLLECTION], n_results=5)
    meta: dict = {}
    reranked = db.hybrid_search(_QUERY, [_COLLECTION], n_results=5,
                                rerank=True, rerank_top_k=3,
                                rerank_meta_out=meta)

    assert set(meta) >= {"degraded", "error", "model", "retry_after_seconds"}
    assert isinstance(meta["degraded"], bool)
    assert reranked, "the rerank call lost every row the plain call found"
    # Same flat shape either way: the envelope is unwrapped, never handed back.
    assert not isinstance(reranked, dict)
    assert {r["collection"] for r in reranked} == {_COLLECTION}
    for r in reranked:
        assert set(r) >= {"id", "content", "collection", "distance"}
    assert {r["id"] for r in reranked} <= {r["id"] for r in plain}


def test_a_bearer_bound_to_another_tenant_gets_422_not_an_empty_list(
    t2_service_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assertion 3: cross-tenant access RAISES, and the client surfaces it.

    Pinned engine-side already at ``VectorHybridHttpTest.java:170``, so why
    again here: this asserts the CLIENT turns it into an error rather than
    swallowing it into an empty list. An empty list is indistinguishable from
    "the lexical gate matched nothing", which is the failure shape this whole
    RDR exists to close — a wrong answer that looks like a legitimate one.

    Tenant binds to the BEARER server-side; the ``X-Nexus-Tenant`` header is
    ignored. Http clients bake their resolved token per instance, so tenant
    B needs a NEW client built after the env swap; the first client keeps
    working.
    """
    tenant_a = t2_service_env
    db_a = hvc.HttpVectorClient(tenant=tenant_a)
    _seed(db_a)
    assert db_a.hybrid_search(_QUERY, [_COLLECTION], n_results=5), (
        "precondition: tenant A must be able to read its own rows, or the "
        "refusal below would prove nothing"
    )

    _, token_b = mint_test_tenant(ensure_engine())
    monkeypatch.setenv("NX_SERVICE_TOKEN", token_b)
    db_b = hvc.HttpVectorClient(tenant=tenant_a)  # A's collection, B's bearer

    with pytest.raises(VectorServiceError) as exc:
        db_b.hybrid_search(_QUERY, [_COLLECTION], n_results=5)
    assert exc.value.code == 422, exc.value
    assert _COLLECTION in str(exc.value)


def test_the_text_gate_is_live_and_an_empty_result_is_the_gate_not_a_dead_route(
    t2_service_env,
) -> None:
    """The positive control, and the reason it exists rather than a bare
    "rows came back" assertion.

    An empty list from this route has two possible causes that look identical
    from the outside: the text gate legitimately matched nothing, or the route
    is broken and returns nothing whatever it is asked. BUG-0148 was the
    second wearing the first's clothes. A test that cannot tell them apart is
    not evidence about either.

    So this drives the SAME corpus twice with the same query: ``hybrid_search``
    returns zero rows because the query shares no literal token with any
    seeded document, while ``search`` returns rows from that very corpus. The
    corpus is demonstrably reachable, so the zero is attributable to the gate.

    That difference IS locked invariant 1 — the hybrid route is not a superset
    of /search, a row with no text signal never appears however close its
    vector, and zero text candidates returns an empty list with no silent
    vector fallback. This is the client-side observation of it, which is why
    the route can never be a silent default for ``nx search``.
    """
    tenant = t2_service_env
    db = hvc.HttpVectorClient(tenant=tenant)
    _seed(db)
    # Prose sharing no token with the seeded code: semantically near, lexically
    # disjoint. That is precisely the population the gate excludes.
    semantic_only = "figure out which conversation is presently leased"

    gated = db.hybrid_search(semantic_only, [_COLLECTION], n_results=5)
    vector = db.search(semantic_only, [_COLLECTION], n_results=5)

    assert vector, (
        "control failed: the vector route found nothing either, so this test "
        "proves nothing about the text gate — the corpus or the tenant is wrong"
    )
    assert gated == [], (
        "the text gate admitted a row with no lexical signal; the route would "
        f"then be a superset of /search, contradicting invariant 1: {gated}"
    )
