# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (critic S5): a search that may return sensitive rows does not ask an engine to
rerank unless the engine reports ``rerank_sensitive_exclusion_supported``.

An engine without the key reranks every candidate through Voyage, sensitive rows included. So with
``sensitivity="include"`` (the session may return sensitive rows) the client sends no ``rerank`` and the degrade
is reported through ``rerank_meta_out``; with ``"exclude"`` the engine never holds a sensitive row, so rerank
stands. The probe follows ``vector_sensitivity_exclude_supported``: only a real answer is cached, a failed one is
retried on the next read.
"""
from __future__ import annotations

import pytest

import nexus.db.http_vector_client as hvc
from tests.db.test_vector_read_sensitivity_argument import _Sent, _Wire, client  # noqa: F401 — client is a fixture

_KEY = "rerank_sensitive_exclusion_supported"
_BOTH = {"vector_sensitivity_exclude_supported": True, _KEY: True}
_NO_RERANK_KEY = {"vector_sensitivity_exclude_supported": True}

#: (id, call taking (client, rerank_meta_out, **kw)) for the three routes that carry ``rerank``.
RERANK_READS = [
    ("search", lambda c, m, **kw: c.search("q", ["c"], rerank=True, rerank_meta_out=m, **kw)),
    ("hybrid_search", lambda c, m, **kw: c.hybrid_search("q", ["c"], rerank=True, rerank_meta_out=m, **kw)),
    ("search_per_collection",
     lambda c, m, **kw: c.search_per_collection(
         "q", ["c"], per_collection_k=1, limit=1, rerank=True, rerank_meta_out=m, **kw)),
]
_IDS = [n for n, _ in RERANK_READS]
_CALLS = [c for _, c in RERANK_READS]


@pytest.mark.parametrize("call", _CALLS, ids=_IDS)
def test_include_with_a_key_less_engine_sends_no_rerank_and_says_so(client, monkeypatch, call):  # noqa: F811
    wire = _Wire(monkeypatch, version=_NO_RERANK_KEY)
    meta: dict = {}
    with pytest.raises(_Sent):
        call(client, meta, sensitivity="include")
    assert "rerank" not in wire.posts[0]["body"], "no Voyage rerank is requested of an engine that cannot withhold"
    assert meta["degraded"] is True and "rerank_sensitive_exclusion_supported" in meta["error"]


@pytest.mark.parametrize("call", _CALLS, ids=_IDS)
def test_include_with_the_key_sends_rerank(client, monkeypatch, call):  # noqa: F811
    wire = _Wire(monkeypatch, version=_BOTH)
    meta: dict = {}
    with pytest.raises(_Sent):
        call(client, meta, sensitivity="include")
    assert wire.posts[0]["body"]["rerank"] is True and meta == {}


@pytest.mark.parametrize("call", _CALLS, ids=_IDS)
def test_exclude_keeps_rerank_whatever_the_engine_says_about_the_key(client, monkeypatch, call):  # noqa: F811
    """No sensitive row survives an exclude read, so none can reach the reranker; the key is not asked."""
    wire = _Wire(monkeypatch, version=_NO_RERANK_KEY)
    meta: dict = {}
    with pytest.raises(_Sent):
        call(client, meta, sensitivity="exclude")
    assert wire.posts[0]["body"]["rerank"] is True
    assert wire.posts[0]["body"]["sensitivity"] == "exclude"
    assert meta == {}


@pytest.mark.parametrize("version", [
    pytest.param({}, id="absent"),
    pytest.param({_KEY: False}, id="false"),
    pytest.param({_KEY: "true"}, id="string-true"),
    pytest.param({_KEY: 1}, id="truthy"),
    pytest.param(hvc.VectorServiceError("boom", code=503), id="probe-fails"),
    pytest.param(TimeoutError("slow"), id="probe-times-out"),
    pytest.param(None, id="not-an-object"),
])
def test_only_a_json_true_counts(client, monkeypatch, version):  # noqa: F811
    wire = _Wire(monkeypatch, version=version)
    meta: dict = {}
    with pytest.raises(_Sent):
        client.search("q", ["c"], rerank=True, rerank_meta_out=meta, sensitivity="include")
    assert "rerank" not in wire.posts[0]["body"] and meta["degraded"] is True


def test_a_search_that_did_not_ask_for_rerank_never_probes(client, monkeypatch):  # noqa: F811
    wire = _Wire(monkeypatch, version=_NO_RERANK_KEY)
    with pytest.raises(_Sent):
        client.search("q", ["c"], sensitivity="include")
    assert wire.gets == [] and "rerank" not in wire.posts[0]["body"]


def test_a_real_answer_is_cached_and_a_failed_probe_is_not(client, monkeypatch):  # noqa: F811
    wire = _Wire(monkeypatch, version=[TimeoutError("slow"), {}, ])
    now = [1000.0]
    monkeypatch.setattr(hvc, "_monotonic", lambda: now[0])
    for _ in range(2):  # first probe fails (not cached), second is a real false (cached)
        with pytest.raises(_Sent):
            client.search("q", ["c"], rerank=True, sensitivity="include")
    with pytest.raises(_Sent):
        client.search("q", ["c"], rerank=True, sensitivity="include")
    assert wire.gets == ["/version", "/version"], "the failure was retried; the real false was then held"
    wire.version = [_BOTH]
    now[0] += client._RERANK_SENSITIVE_TTL_S + 1
    with pytest.raises(_Sent):
        client.search("q", ["c"], rerank=True, sensitivity="include")
    assert wire.posts[-1]["body"]["rerank"] is True, "after the TTL the engine is asked again"
