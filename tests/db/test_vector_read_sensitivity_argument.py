# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 Step 2.3 (bead nexus-rfvyy.7.8): every client read that returns chunk text takes a
keyword-only ``sensitivity`` argument with no default.

The argument is the client half of the engine's ``sensitivity=exclude`` read filter (nexus-rfvyy.7.3 and
.7.4). It has no default so that a call site written without a decision fails with ``TypeError`` at call
time instead of silently reading sensitive chunks. ``exclude`` sends ``"sensitivity": "exclude"`` in the
body; ``include`` sends nothing, which is the request the client made before the argument existed. An
``exclude`` read first reads the engine's ``vector_sensitivity_exclude_supported`` flag from ``GET
/version`` and raises, before any request is sent, when it is not JSON ``true``.

The probe follows Sam's ruling on nexus-rfvyy.3: only a real ``/version`` answer (true or false) is
cached; a failed or timed-out probe is not, so it is retried on the next read and the outage costs
exactly the reads made during it.

The five groups, as the bead lists them:

1. a read without ``sensitivity`` raises ``TypeError``;
2. the wire: ``exclude`` sends the key, ``include`` sends none;
3. the probe: a real false, an absent key and a failed probe each refuse an ``exclude`` read, an
   ``include`` read always works, and no request goes out on a refused read; plus the caching pair;
4. under ``llm_bound`` an MCP ``search`` over two embedding-model groups sends ``exclude`` from every pool
   task (and the test proves the pool ran);
5. no MCP read tool exposes a ``sensitivity`` parameter.

A sixth, structural test scans ``src/nexus`` for a collection-handle ``.get`` call without the keyword,
so a caller this change missed is a test failure and not a ``TypeError`` in production.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
import threading

import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.db.inmemory_vector_store import InMemoryVectorClient
from nexus.db.t3 import T3Database
from nexus.mcp import core
from nexus.sensitivity import llm_bound

from tests.test_search_per_collection_route import (  # type: ignore[import-not-found]
    _BGE,
    _MINI,
    _ROUTE,
    _FakeEngine,
    _cols,
    _rows,
)

_VERSION_OK = {"vector_sensitivity_exclude_supported": True}


class _Sent(Exception):
    """Raised by the fake ``_post`` after it recorded the request: the test needs the request, not a response."""


class _NoRequest(AssertionError):
    pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hvc.HttpVectorClient, "__init__", lambda self: None)
    c = hvc.HttpVectorClient()
    c._tenant = "t"
    return c


class _Wire:
    """Replaces the module's ``_post`` and ``_get``: records every request, answers ``/version`` from ``version``."""

    def __init__(self, monkeypatch, version=_VERSION_OK):
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.version = version  # a dict, or an Exception to raise, or a list of those consumed in order
        monkeypatch.setattr(hvc, "_post", self._post)
        monkeypatch.setattr(hvc, "_get", self._get)

    def _post(self, path, body, tenant=None, **kw):
        self.posts.append({"path": path, "body": body})
        raise _Sent(path)

    def _get(self, path, *, tenant="default", timeout=30):
        self.gets.append(path)
        assert path == "/version", path
        v = self.version.pop(0) if isinstance(self.version, list) else self.version
        if isinstance(v, Exception):
            raise v
        return v


# Every read on the client that returns chunk text, as (id, callable taking (client, **sensitivity_kwargs)).
def _stub(c):
    return hvc._ServiceCollectionStub("c", "t", sensitivity_gate=c._require_sensitivity_exclude)


CLIENT_READS = [
    ("search", lambda c, **kw: c.search("q", ["c"], **kw)),
    ("hybrid_search", lambda c, **kw: c.hybrid_search("q", ["c"], **kw)),
    ("search_per_collection",
     lambda c, **kw: c.search_per_collection("q", ["c"], per_collection_k=1, limit=1, **kw)),
    ("search_metadata_scoped", lambda c, **kw: c.search_metadata_scoped("q", ["c"], **kw)),
    ("search_topic_scoped", lambda c, **kw: c.search_topic_scoped("q", "topic", "c", **kw)),
    ("search_graph_hop", lambda c, **kw: c.search_graph_hop("q", ["1.1.1"], ["c"], **kw)),
    ("search_aspect_scoped", lambda c, **kw: c.search_aspect_scoped("q", ["c"], **kw)),
    ("get_by_id", lambda c, **kw: c.get_by_id("c", "id", **kw)),
    ("chunk_rows", lambda c, **kw: c.chunk_rows("c", ["h"], **kw)),
    ("stub_get_ids", lambda c, **kw: _stub(c).get(ids=["a"], **kw)),
    ("stub_get_where", lambda c, **kw: _stub(c).get(where={"k": "v"}, limit=5, **kw)),
]
_IDS = [name for name, _ in CLIENT_READS]


# ── 1. a read without the argument is a TypeError ────────────────────────────


@pytest.mark.parametrize("call", [c for _, c in CLIENT_READS], ids=_IDS)
def test_client_read_without_sensitivity_is_a_type_error_before_any_request(client, monkeypatch, call):
    wire = _Wire(monkeypatch)
    with pytest.raises(TypeError, match="sensitivity"):
        call(client)
    assert wire.posts == [] and wire.gets == [], "the TypeError comes at call time, before any request"


@pytest.mark.parametrize("bad", [None, "", "EXCLUDE", "none", True])
def test_a_value_outside_the_literal_is_refused_not_read_as_include(client, monkeypatch, bad):
    wire = _Wire(monkeypatch)
    with pytest.raises(ValueError, match="sensitivity"):
        client.search("q", ["c"], sensitivity=bad)
    assert wire.posts == []


def test_t3_facade_and_the_in_memory_double_have_the_same_default_less_keyword(tmp_path):
    mem = InMemoryVectorClient()
    col = mem.get_or_create_collection("c")
    with pytest.raises(TypeError, match="sensitivity"):
        col.get(ids=["a"])
    db = T3Database(_client=mem, local_mode=True, local_path=str(tmp_path))
    with pytest.raises(TypeError, match="sensitivity"):
        db.search("q", ["c"])
    with pytest.raises(TypeError, match="sensitivity"):
        db.get_by_id("c", "a")


def test_the_in_memory_double_serves_both_values_the_same(tmp_path):
    """The double has no catalog, so no chunk of it is sensitive: ``exclude`` and ``include`` read alike.
    Pinned so a later change to either value is a decision, not an accident."""
    mem = InMemoryVectorClient()
    col = mem.get_or_create_collection("c")
    col.upsert(ids=["a"], documents=["text"], metadatas=[{"k": "v"}], embeddings=[[0.1, 0.2]])
    assert col.get(ids=["a"], sensitivity="exclude") == col.get(ids=["a"], sensitivity="include")
    db = T3Database(_client=mem, local_mode=True, local_path=str(tmp_path))
    assert db.get_by_id("c", "a", sensitivity="exclude") == db.get_by_id("c", "a", sensitivity="include")
    assert db.get_by_id("c", "a", sensitivity="include")["content"] == "text"


def test_search_cross_corpus_and_the_batched_get_without_sensitivity_are_type_errors(client, monkeypatch):
    _Wire(monkeypatch)
    with pytest.raises(TypeError, match="sensitivity"):
        se.search_cross_corpus("q", ["knowledge__a__minilm-l6-v2-384__v1"], 5, client)
    with pytest.raises(TypeError, match="sensitivity"):
        core._batched_get_by_ids(client, "c", ["a"])


# ── 2. the wire ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("call", [c for _, c in CLIENT_READS], ids=_IDS)
def test_exclude_sends_the_key_and_include_sends_none(client, monkeypatch, call):
    wire = _Wire(monkeypatch)
    with pytest.raises(_Sent):
        call(client, sensitivity="exclude")
    assert wire.posts[-1]["body"]["sensitivity"] == "exclude"

    wire = _Wire(monkeypatch)
    with pytest.raises(_Sent):
        call(client, sensitivity="include")
    assert "sensitivity" not in wire.posts[-1]["body"], "include is the request the client made before the argument"
    assert wire.gets == [], "an include read never asks the engine what it supports"


def test_every_page_of_a_paged_stub_get_carries_exclude(client, monkeypatch):
    sent: list[dict] = []

    def fake_post(path, body, tenant=None, **kw):
        sent.append(body)
        return {"ids": [], "documents": [], "metadatas": [], "count": 0}

    monkeypatch.setattr(hvc, "_post", fake_post)
    monkeypatch.setattr(hvc, "_get", lambda path, **kw: _VERSION_OK)
    ids = [f"h{i}" for i in range(700)]
    _stub(client).get(ids=ids, sensitivity="exclude")
    assert len(sent) == 3 and all(b["sensitivity"] == "exclude" for b in sent)


def test_a_stub_with_no_gate_refuses_exclude_and_still_serves_include(monkeypatch):
    wire = _Wire(monkeypatch)
    bare = hvc._ServiceCollectionStub("c", "t")
    with pytest.raises(hvc.SensitivityExcludeUnavailable):
        bare.get(ids=["a"], sensitivity="exclude")
    assert wire.posts == []
    with pytest.raises(_Sent):
        bare.get(ids=["a"], sensitivity="include")


def test_the_collection_handles_a_client_returns_carry_its_gate(client, monkeypatch):
    monkeypatch.setattr(client, "list_collections", lambda strict=False, routing=False: [{"name": "c"}])
    wire = _Wire(monkeypatch, version={})
    for handle in (client.get_collection("c"), client.get_or_create_collection("c")):
        with pytest.raises(hvc.SensitivityExcludeUnavailable):
            handle.get(ids=["a"], sensitivity="exclude")
    assert wire.posts == []


# ── 3. the probe ─────────────────────────────────────────────────────────────

_REFUSING_VERSIONS = [
    pytest.param({}, id="flag-absent"),
    pytest.param({"vector_sensitivity_exclude_supported": False}, id="flag-false"),
    pytest.param({"vector_sensitivity_exclude_supported": "true"}, id="flag-not-json-true"),
    pytest.param({"vector_sensitivity_exclude_supported": 1}, id="flag-truthy-not-true"),
    pytest.param(hvc.VectorServiceError("boom", code=503), id="probe-service-error"),
    pytest.param(TimeoutError("slow"), id="probe-timeout"),
    pytest.param(None, id="probe-answer-not-an-object"),
]


@pytest.mark.parametrize("version", _REFUSING_VERSIONS)
@pytest.mark.parametrize("call", [c for _, c in CLIENT_READS], ids=_IDS)
def test_an_exclude_read_refuses_when_the_engine_cannot_be_shown_to_filter(client, monkeypatch, call, version):
    wire = _Wire(monkeypatch, version=version)
    with pytest.raises(hvc.VectorServiceError) as err:
        call(client, sensitivity="exclude")
    assert "exclude" in str(err.value)
    assert wire.posts == [], "no request, filtered or not, goes out on a refused exclude read"


@pytest.mark.parametrize("version", _REFUSING_VERSIONS)
@pytest.mark.parametrize("call", [c for _, c in CLIENT_READS], ids=_IDS)
def test_an_include_read_works_whatever_the_probe_would_say(client, monkeypatch, call, version):
    wire = _Wire(monkeypatch, version=version)
    with pytest.raises(_Sent):  # the fake _post stops it: the request was sent
        call(client, sensitivity="include")
    assert len(wire.posts) == 1 and wire.gets == []


def test_a_real_false_is_cached_for_the_ttl_and_a_reread_after_it_sees_a_new_answer(client, monkeypatch):
    wire = _Wire(monkeypatch, version=[{}, {"vector_sensitivity_exclude_supported": True}])
    now = [1000.0]
    monkeypatch.setattr(hvc, "_monotonic", lambda: now[0])
    for _ in range(3):
        with pytest.raises(hvc.SensitivityExcludeUnavailable):
            client.search("q", ["c"], sensitivity="exclude")
    assert wire.gets == ["/version"], "a real false answer is cached: the three reads made one request"
    assert wire.posts == []
    now[0] += client._SENSITIVITY_EXCLUDE_TTL_S + 1
    with pytest.raises(_Sent):
        client.search("q", ["c"], sensitivity="exclude")
    assert len(wire.gets) == 2, "after the TTL the engine is asked again"


def test_a_real_true_is_cached_for_the_ttl(client, monkeypatch):
    wire = _Wire(monkeypatch)
    for _ in range(3):
        with pytest.raises(_Sent):
            client.search("q", ["c"], sensitivity="exclude")
    assert wire.gets == ["/version"] and len(wire.posts) == 3


def test_a_failed_probe_is_not_cached_so_the_next_read_goes_through(client, monkeypatch):
    """Sam's ruling on nexus-rfvyy.3: a failure followed by a success makes the second read go through."""
    wire = _Wire(monkeypatch, version=[TimeoutError("slow"), _VERSION_OK])
    with pytest.raises(hvc.VectorServiceError):
        client.search("q", ["c"], sensitivity="exclude")
    assert wire.posts == []
    with pytest.raises(_Sent):
        client.search("q", ["c"], sensitivity="exclude")
    assert wire.gets == ["/version", "/version"], "the failure was retried, not remembered"
    assert len(wire.posts) == 1


def test_two_failed_probes_in_a_row_are_two_requests(client, monkeypatch):
    wire = _Wire(monkeypatch, version=[TimeoutError("a"), TimeoutError("b")])
    for _ in range(2):
        with pytest.raises(hvc.VectorServiceError):
            client.search("q", ["c"], sensitivity="exclude")
    assert wire.gets == ["/version", "/version"]


def test_get_by_id_does_not_read_a_refused_exclude_as_no_such_chunk(client, monkeypatch):
    """``get_by_id`` turns a service error into ``None``; a refusal must not become that quiet miss."""
    _Wire(monkeypatch, version={})
    with pytest.raises(hvc.SensitivityExcludeUnavailable):
        client.get_by_id("c", "id", sensitivity="exclude")


def test_the_engine_decides_what_a_read_returns_the_client_adds_no_clean_inference(client, monkeypatch):
    """Phase 1 engine review: ``sensitive=false`` is not proof of clean under a mis-scoped tenant. The client
    passes rows through verbatim, hits and ``sensitives`` alike, and neither reads nor drops on them."""
    rows = [{"id": "a", "content": "x", "distance": 0.1, "collection": "c", "sensitive": False},
            {"id": "b", "content": "y", "distance": 0.2, "collection": "c", "sensitive": True}]
    monkeypatch.setattr(hvc, "_get", lambda path, **kw: _VERSION_OK)
    monkeypatch.setattr(hvc, "_post", lambda path, body, tenant=None, **kw: list(rows))
    assert client.search("q", ["c"], sensitivity="exclude") == rows
    env = {"ids": ["a", "b"], "documents": ["x", "y"], "metadatas": [{}, {}], "sensitives": [False, True], "count": 2}
    monkeypatch.setattr(hvc, "_post", lambda path, body, tenant=None, **kw: dict(env))
    out = _stub(client).get(ids=["a", "b"], sensitivity="exclude")
    assert out["ids"] == ["a", "b"], "a row the engine returned is a row the client returns"


# ── 4. the pool ──────────────────────────────────────────────────────────────


class _VersionedEngine(_FakeEngine):
    """The route fake of tests/test_search_per_collection_route.py, plus ``GET /version`` and the thread of each route request."""

    def __init__(self, monkeypatch, data):
        super().__init__(monkeypatch, data)
        self.route_threads: list[int] = []
        monkeypatch.setattr(hvc, "_request", self._request_with_version)

    def _request_with_version(self, method, path, *, tenant, timeout, body):
        if path == "/version":
            self.calls.append((path, body))
            return dict(_VERSION_OK)
        if method == "GET" and path.startswith("/v1/vectors/stats"):
            return []  # the collection-routing listing the MCP search prefetches
        if path == _ROUTE:
            self.route_threads.append(threading.get_ident())
        return self._request(method, path, tenant=tenant, timeout=timeout, body=body)


@pytest.mark.sensitivity_reads
@pytest.mark.t2_tenant
def test_under_llm_bound_an_mcp_search_sends_exclude_from_every_pool_task(monkeypatch):
    monkeypatch.delenv("NX_SENSITIVITY_POSTURE", raising=False)  # the suite's blanket: this test owns its posture
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, "1")  # opt-in route (nexus-tu8wp.3)
    bge, mini = _cols("docs", _BGE, 2), _cols("docs", _MINI, 2)
    engine = _VersionedEngine(monkeypatch, {c: _rows("r", 4, 0.2) for c in bge + mini})
    monkeypatch.setattr(core, "_get_t3", lambda: engine.client)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **kw: bge + mini)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)

    def run(posture_scope):
        engine.calls.clear()
        engine.route_threads.clear()
        core._page_cache_invalidate()
        try:
            with posture_scope:
                core.search(query="anything", corpus="knowledge", threshold=float("inf"))
        finally:
            core._page_cache_invalidate()
        return engine.route_calls()

    bound = run(llm_bound("nx_answer_operators"))
    assert len(bound) == 2, "the pool ran: one route request per embedding-model group"
    assert {tuple(b["collections"]) for b in bound} == {tuple(bge), tuple(mini)}
    assert [b.get("sensitivity") for b in bound] == ["exclude", "exclude"]
    assert any(t != threading.get_ident() for t in engine.route_threads), "the route requests ran on pool threads"

    default = run(contextlib.nullcontext())
    assert len(default) == 2
    assert all("sensitivity" not in b for b in default), "outside the scope the request is what it always was"

    # A child of a scoped parent has no ContextVar, only the environment the parent exported.
    monkeypatch.setenv("NX_SENSITIVITY_POSTURE", "llm_bound")
    from_env = run(contextlib.nullcontext())
    assert [b.get("sensitivity") for b in from_env] == ["exclude", "exclude"]


def test_the_posture_reaches_a_pool_worker_that_did_not_inherit_the_context(monkeypatch):
    """A7: a worker thread does not inherit the ContextVar. The value is a call argument, so a bare
    ``threading.Thread`` worker, which gets no context copy, still sends it."""
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, "1")  # reads route_calls(): the route is opt-in (nexus-tu8wp.3)
    cols = _cols("docs", _BGE, 2)
    engine = _VersionedEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
    seen: list = []

    def worker():
        se.search_cross_corpus("q", cols, 5, engine.client, threshold_override=float("inf"),
                               cluster_by=None, sensitivity="exclude")
        seen.append(True)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen == [True]
    assert [b.get("sensitivity") for b in engine.route_calls()] == ["exclude"]


# ── 5. the MCP schemas ───────────────────────────────────────────────────────

_READ_TOOLS = ["search", "query", "store_get", "store_get_many", "search_metadata_scoped", "search_topic_scoped",
               "search_graph_hop", "search_aspect_scoped"]


def test_no_mcp_read_tool_schema_has_a_sensitivity_property():
    tools = {t.name: t for t in asyncio.run(core.mcp.list_tools())}
    for name in _READ_TOOLS:
        assert name in tools, f"{name} is no longer a registered tool: update this list"
    for name, tool in tools.items():
        assert "sensitivity" not in (tool.input_schema.get("properties") or {}), (
            f"MCP tool {name} exposes a sensitivity parameter: a model could pass include"
        )


# ── 6. no caller is left behind ──────────────────────────────────────────────

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "nexus"
_GET_HINT_KWARGS = {"ids", "where", "include", "limit", "offset", "include_source_uri", "include_non_live"}
_CLIENT_READ_NAMES = {
    "search_per_collection", "search_metadata_scoped", "search_topic_scoped", "search_graph_hop",
    "search_aspect_scoped", "hybrid_search", "chunk_rows", "search_cross_corpus", "_batched_get_by_ids",
}
#: T2 stores whose own ``get``/``search`` share a name with the vector reads. Their receivers are not collection handles.
_NOT_VECTOR_RECEIVERS = ("scratch", "memory", "plans", "taxonomy", "telemetry", "tuples", "t1", "t2")


def _call_name(node: ast.Call) -> tuple[str | None, str]:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr, ast.unparse(f.value)
    if isinstance(f, ast.Name):
        return f.id, ""
    return None, ""


def test_every_vector_read_in_src_names_its_sensitivity():
    """The structural pin for "every caller is updated in this change": a collection-handle ``.get`` (recognised
    by the keyword arguments only that call takes), a named client read, ``get_by_id`` and ``t3.search`` each
    pass ``sensitivity=``. Definitions are exempt; so is the double inside its own module."""
    missing: list[str] = []
    examined = 0
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name, recv = _call_name(node)
            kws = {k.arg for k in node.keywords if k.arg}
            passes_get = any(isinstance(a, ast.Attribute) and a.attr == "get" for a in node.args) and (
                bool(kws & _GET_HINT_KWARGS) or any(k.arg is None for k in node.keywords)
            )  # _vector_with_retry(col.get, ids=...): the .get is handed over, not called here
            is_read = (
                passes_get
                or (name == "get" and bool(kws & _GET_HINT_KWARGS))
                or (name in _CLIENT_READ_NAMES)
                or (name == "get_by_id" and not any(s in recv for s in _NOT_VECTOR_RECEIVERS[:6]))
                or (name == "search" and recv.split(".")[-1] in {"t3", "db", "client", "self._client"} and "n_results" in kws)
            )
            if not is_read:
                continue
            examined += 1
            forwards_all = any(isinstance(a, ast.Starred) for a in node.args) and any(k.arg is None for k in node.keywords)
            if "sensitivity" not in kws and not forwards_all:  # f(*args, **kwargs): a wrapper, its caller names it
                missing.append(f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}: {recv}.{name}(...)")
    assert examined > 60, f"the scan examined only {examined} reads: it has gone blind"
    assert not missing, "vector reads without sensitivity=:\n" + "\n".join(missing)
