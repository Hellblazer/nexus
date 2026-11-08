# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 Step 2.4 (bead nexus-rfvyy.7.9): the ``nx_answer`` runner, its operator bundle and ``nx_tidy``
refuse sensitive documents, and say so.

The scenario is the Test Plan's canary: a corpus with collections of TWO embedding models (so the cross-corpus
fan-out pool runs) and one sensitive canary document. The canary text must be absent from every operator prompt
and from the final answer, on the chash route (a search hit hydrated by chunk id), on the tumbler route
(document ids hydrated through the catalog manifest) and in a fused bundle, and the refusal must leave a skip
line on the run record (``StepRecord.sensitive_skips``) naming the tumbler and the surface.

The engine double here does NOT filter: it returns the canary chunk like any other, the way a stale engine or a
double would. That is on purpose. The engine's own ``exclude`` filter is the first line (pinned in
``tests/db/test_vector_read_sensitivity_argument.py`` and the engine tests); what this file pins is the runner's
own catalog refusal, so the test goes red when that refusal is deleted instead of passing on the engine's say-so.
``test_the_fixture_leaks_the_canary_when_the_posture_is_include`` is the control: the same fixture, read as
``include``, hands the canary over.
"""
from __future__ import annotations

import json

import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, ManifestRow
from nexus.doc_sensitivity import SensitivityUnknown, flagged_owners_of_chashes
from nexus.mcp import core
from nexus.plans.match import Match
from nexus.plans.runner import _hydrate_tumbler_ids, plan_run
from nexus.sensitivity import llm_bound
from tests.test_search_per_collection_route import _BGE, _MINI, _FakeEngine

pytestmark = pytest.mark.sensitivity_reads  # the real read_sensitivity(): the suite pins it to include otherwise

CANARY = "CANARY-5e1d-do-not-send"
COL_BGE = f"knowledge__sens__{_BGE}__v1"
COL_MINI = f"knowledge__sens__{_MINI}__v1"
P1, P2, CAN = "a1" * 32, "b2" * 32, "c3" * 32  # chunk hashes: plain (bge), plain (minilm), canary (bge)
T_PLAIN1, T_PLAIN2, T_SECRET = "1.1.1", "1.1.2", "1.1.3"
ROWS = {
    P1: {"content": "plain text one about gardens"},
    P2: {"content": "plain text two about orchards"},
    CAN: {"content": f"the secret memo says {CANARY}"},
}


def _entry(tumbler: str, title: str, collection: str, *, sensitive: bool = False) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=title, author="", year=0, content_type="prose", file_path="",
        corpus="", physical_collection=collection, chunk_count=1, head_hash="", indexed_at="2026-11-01T00:00:00+00:00",
        meta={}, sensitive=sensitive,
    )


class _Catalog:
    """The catalog reads the hydration and the document gate make, over three documents, one of them sensitive."""

    def __init__(self, *, unreadable: bool = False) -> None:
        docs = [
            (T_PLAIN1, "Gardens", COL_BGE, P1, False),
            (T_PLAIN2, "Orchards", COL_MINI, P2, False),
            (T_SECRET, "Secret memo", COL_BGE, CAN, True),
        ]
        self.entries = {t: _entry(t, title, col, sensitive=s) for t, title, col, _c, s in docs}
        self.manifests = {t: [ManifestRow(position=0, chash=c, collection=col)] for t, _ti, col, c, _s in docs}
        self.owner = {c: t for t, _ti, _col, c, _s in docs}
        self.unreadable = unreadable

    def _check(self) -> None:
        if self.unreadable:
            raise RuntimeError("catalog down")

    def get_manifests(self, doc_ids):
        return {d: self.manifests.get(d, []) for d in doc_ids}

    def resolve_many(self, doc_ids):
        self._check()
        return {d: self.entries[d] for d in doc_ids if d in self.entries}

    def resolve(self, tumbler, *, follow_alias=True):
        return self.entries.get(str(tumbler))

    def by_doc_id(self, doc_id):
        return self.entries.get(doc_id)

    def docs_for_chashes(self, chashes):
        self._check()
        return {c: [self.owner[c]] for c in chashes if c in self.owner}

    def chash_positions(self, chashes):
        return [
            {"chash": c, "doc_id": self.owner[c], "position": 0, "chunk_count": 1, "collection": self.manifests[self.owner[c]][0].collection}
            for c in chashes if c in self.owner
        ]


class _Rows:
    """The collection handle ``store_get_many`` reads chunk text through; it records each read's posture."""

    seen: list[str] = []

    def get(self, ids=None, *, sensitivity, **_kw):
        type(self).seen.append(sensitivity)
        found = [i for i in (ids or []) if i in ROWS]
        return {"ids": found, "documents": [ROWS[i]["content"] for i in found], "metadatas": [{} for _ in found]}


class _World:
    def __init__(self, engine: _FakeEngine, catalog: _Catalog) -> None:
        self.engine, self.catalog = engine, catalog
        self.prompts: list[str] = []
        _Rows.seen = []

    @property
    def read_postures(self) -> list[str]:
        return list(_Rows.seen)


@pytest.fixture
def world(monkeypatch):
    # No tenant: the client is a bare transport over the engine double.
    monkeypatch.setattr(hvc.HttpVectorClient, "__init__", lambda self, **kw: setattr(self, "_tenant", "t"))
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)
    engine = _FakeEngine(monkeypatch, {COL_BGE: [(P1, 0.2), (CAN, 0.21)], COL_MINI: [(P2, 0.22)]})
    real = engine._request

    def request(method, path, *, tenant, timeout, body):
        if path == "/version":
            return {"vector_sensitivity_exclude_supported": True}
        if method == "GET" and path.startswith("/v1/vectors/stats"):
            return []
        return real(method, path, tenant=tenant, timeout=timeout, body=body)

    monkeypatch.setattr(hvc, "_request", request)
    monkeypatch.setattr(engine.client, "get_or_create_collection", lambda _name: _Rows())
    monkeypatch.setattr(engine.client, "_resolve_collection_rows", lambda names: {})  # no catalog row lookup: no tenant
    monkeypatch.setattr(core, "_get_t3", lambda: engine.client)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **kw: [COL_BGE, COL_MINI])
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    monkeypatch.setattr(core, "_search_telemetry_sink", lambda: None)  # the telemetry write is a T2 write: no tenant here

    def no_session():
        raise RuntimeError("no T1 session in this test")  # search's relevance trace is best-effort and writes T1

    monkeypatch.setattr(core, "_get_t1", no_session)
    monkeypatch.setattr(core, "_read_scope", lambda t3, cand: [cand] if cand in (COL_BGE, COL_MINI) else [COL_BGE, COL_MINI])
    catalog = _Catalog()
    for target in ("nexus.mcp_infra.get_catalog", "nexus.mcp.core._get_catalog"):
        monkeypatch.setattr(target, lambda: catalog)
    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", lambda: catalog)
    w = _World(engine, catalog)

    async def fake_dispatch(prompt, schema, **_kw):
        w.prompts.append(prompt)
        return {"summary": prompt, "text": prompt, "citations": []}

    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", fake_dispatch)
    core._page_cache_invalidate()
    yield w
    core._page_cache_invalidate()


def _match(plan: dict) -> Match:
    return Match(
        plan_id=1, name="default", description="test", confidence=0.9, dimensions={"verb": "test"}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[], default_bindings={}, parent_dims=None,
    )


async def _run(plan: dict, *, bundle: bool = False):
    with llm_bound("nx_answer_operators"):  # what nx_answer's own @bound_to does
        return await plan_run(_match(plan), {}, bundle_operators=bundle)


def _skips(result) -> list[dict[str, str]]:
    return [line for rec in result.step_records for line in rec.sensitive_skips]


def _assert_canary_absent(world: _World, result) -> None:
    assert world.prompts, "an operator prompt was built: the check below is not vacuous"
    assert not [p for p in world.prompts if CANARY in p], "the canary reached an operator prompt"
    assert CANARY not in json.dumps(result.steps, default=str), "the canary reached the answer"
    assert CANARY not in json.dumps(result.final, default=str)


_SEARCH = {"tool": "search", "args": {"query": "gardens", "corpus": "knowledge", "limit": 10, "threshold": float("inf")}}


@pytest.mark.asyncio
async def test_a_search_hit_on_the_canary_is_refused_on_the_chash_route(world):
    plan = {"steps": [_SEARCH, {"tool": "summarize", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}}]}

    result = await _run(plan)

    route_calls = world.engine.route_calls()
    assert len(route_calls) == 2, "the pool ran: one route request per embedding-model group"
    assert {tuple(c["collections"]) for c in route_calls} == {(COL_BGE,), (COL_MINI,)}
    assert CAN in result.steps[0]["ids"], "the engine double returned the canary chunk: only the runner's refusal stands between it and the prompt"
    _assert_canary_absent(world, result)
    assert "plain text one" in world.prompts[0] and "plain text two" in world.prompts[0], "the plain chunks were kept"
    assert world.read_postures and set(world.read_postures) == {"exclude"}, "the chunk read took the posture as an argument"
    assert {"tumbler": T_SECRET, "surface": "nx_answer_operators", "route": "chash", "reason": "sensitive", "chash": CAN} in _skips(result)


@pytest.mark.asyncio
async def test_document_ids_on_the_tumbler_route_refuse_the_sensitive_document(world):
    plan = {"steps": [{"tool": "summarize", "args": {"ids": [T_PLAIN1, T_SECRET, T_PLAIN2]}}]}

    result = await _run(plan)

    _assert_canary_absent(world, result)
    assert "plain text one" in world.prompts[0] and "plain text two" in world.prompts[0]
    assert _skips(result) == [{"tumbler": T_SECRET, "surface": "nx_answer_operators", "route": "tumbler", "reason": "sensitive"}]
    assert set(world.read_postures) == {"exclude"}


@pytest.mark.asyncio
async def test_an_explicit_store_get_many_step_on_tumblers_reports_the_refused_document_as_missing(world):
    plan = {"steps": [{"tool": "store_get_many", "args": {"ids": [T_PLAIN1, T_SECRET]}}]}

    result = await _run(plan)

    out = result.steps[0]
    assert out["missing"] == [T_SECRET]
    assert CANARY not in json.dumps(out)
    assert "plain text one" in out["contents"][0] and out["contents"][1] == ""


@pytest.mark.asyncio
async def test_a_fused_bundle_hydrates_through_the_same_refusal(world):
    plan = {"steps": [
        {"tool": "summarize", "args": {"ids": [T_PLAIN1, T_SECRET]}},
        {"tool": "generate", "args": {"template": "memo", "context": "$step1.summary"}},
    ]}

    result = await _run(plan, bundle=True)

    assert [r.source for r in result.step_records] == ["bundle"], "the two operators ran as ONE fused dispatch"
    _assert_canary_absent(world, result)
    assert [(s["tumbler"], s["route"]) for s in _skips(result)] == [(T_SECRET, "tumbler")]
    assert result.step_records[0].sensitive_skips, "the skip line is on the bundle's own record"


@pytest.mark.asyncio
async def test_a_catalog_that_cannot_say_withholds_every_document(world):
    world.catalog.unreadable = True
    plan = {"steps": [{"tool": "summarize", "args": {"ids": [T_PLAIN1, T_SECRET]}}]}

    result = await _run(plan)

    assert not world.prompts or not [p for p in world.prompts if "plain text one" in p or CANARY in p], (
        "a gate that cannot tell sends nothing"
    )
    reasons = {s["reason"] for s in _skips(result)}
    assert reasons == {"sensitivity unknown: withheld"}
    assert {s["tumbler"] for s in _skips(result)} == {T_PLAIN1, T_SECRET}


def test_the_fixture_leaks_the_canary_when_the_posture_is_include(world):
    """The control: with the refusal not asked for, this very fixture hands the canary to the caller."""
    out = _hydrate_tumbler_ids([T_PLAIN1, T_SECRET], sensitivity="include")
    assert CANARY in out["contents"][1]
    assert out["missing"] == []

    refused = _hydrate_tumbler_ids([T_PLAIN1, T_SECRET], sensitivity="exclude")
    assert CANARY not in json.dumps(refused)
    assert refused["missing"] == [T_SECRET]


def test_hydration_takes_the_posture_as_a_required_keyword():
    with pytest.raises(TypeError, match="sensitivity"):
        _hydrate_tumbler_ids([T_PLAIN1])  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_nx_tidy_prefetch_withholds_the_canary_and_the_summary_counts_it(world):
    out = await core.nx_tidy(topic="gardens", collection="knowledge")

    assert world.prompts, "tidy built a prompt"
    assert not [p for p in world.prompts if CANARY in p], "the canary reached tidy's prompt"
    assert "plain text one" in world.prompts[0]
    assert CANARY not in out
    assert "Sensitive entries withheld from this pass: 1" in out
    assert set(world.read_postures) == {"exclude"}, "the prefetch's hydration took the posture as an argument"


def test_tidy_prefetch_reports_zero_withheld_under_include(world):
    block, n, _reason, _genuine, withheld = core._tidy_prefetch("gardens", "knowledge", sensitivity="include")
    assert withheld == 0
    assert CANARY in block, "the control: under include the prefetch keeps every entry"
    assert n == 3


def test_the_tidy_prefetch_with_every_match_sensitive_says_so(world, monkeypatch):
    monkeypatch.setattr(core, "_search_render", lambda **_k: {"ids": [CAN], "chunk_collections": [COL_BGE]})

    block, n, reason, genuine, withheld = core._tidy_prefetch("secret", "knowledge", sensitivity="exclude")

    assert (block, n, withheld) == ("", 0, 1)
    assert "withheld as sensitive" in reason and genuine is True


def test_flagged_owners_of_chashes_names_the_sensitive_owner_and_honours_a_given_owner_map(world):
    assert flagged_owners_of_chashes([P1, P2, CAN]) == {CAN: [T_SECRET]}
    # A caller that already holds the owner rows passes them: the catalog is not asked for them again.
    assert flagged_owners_of_chashes([P1], owners={P1: [T_SECRET]}) == {P1: [T_SECRET]}
    assert flagged_owners_of_chashes([]) == {}
    world.catalog.unreadable = True
    with pytest.raises(SensitivityUnknown):
        flagged_owners_of_chashes([CAN])
