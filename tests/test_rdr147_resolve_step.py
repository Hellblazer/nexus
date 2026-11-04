# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-147 Step 3 (nexus-v9v5r): the runner-native ``resolve`` plan step.

``resolve(tokens, by="title", content_type=None, owner=None)`` looks each distinct key token up by exact
document title (``HttpCatalogClient.find_by_title_exact``) and reports ``{tumblers, ids, collections, missing,
ambiguous}``. A token with no live document is ``missing``; a token that names several is ``ambiguous`` with
every candidate tumbler and is never resolved to one of them. No embedding, no model call, no MCP tool.

These run on the engine substrate: a real catalog, real manifests, real chunk text. The only wrapper is the call
counter in :class:`_Spy`, which delegates to the real client. Only the tests that reach the engine ask for a
tenant (``@engine``); the argument checks run over :class:`_NoCatalog`, which fails on any lookup.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

import nexus.mcp.core as mcp_core
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.plans import runner
from nexus.plans.continuation_envelope import _harvest_hydrated_bundles
from nexus.plans.cost_estimate import OperatorPriceTable, estimate_plan_cost
from nexus.plans.coverage_ledger import _hits
from nexus.plans.document_confinement import (
    CONFINED_TOOLS,
    CONFINEMENT_MARKER,
    DocumentConfinement,
    confine_step,
)
from nexus.plans.match import Match
from nexus.plans.resolve_step import resolve_keys
from nexus.plans.runner import plan_run
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct

#: A test that reaches the engine asks for a tenant on the test itself (tests/AGENTS.md § The tenant is opt-in);
#: the checks that raise before any lookup and the constant pins mint none.
engine = pytest.mark.t2_tenant

COLLECTION = "knowledge__rdr147resolve__bge-base-en-v15-768__v1"


#: What ``resolve_keys`` answers for no tokens: the step-output keys a later ``$stepN.<key>`` may name.
EMPTY_RESULT: dict[str, Any] = {
    "tumblers": [], "ids": [], "collections": [], "chunk_collections": [], "chunk_tumblers": [],
    "by_token": {}, "missing": [], "ambiguous": {},
}


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _match(plan: dict[str, Any]) -> Match:
    return Match(
        plan_id=1, name="default", description="t", confidence=0.9,
        dimensions={}, tags="", plan_json=json.dumps(plan),
        required_bindings=[], optional_bindings=[], default_bindings={}, parent_dims=None,
    )


class _Spy:
    """The real catalog client with a per-method call log on the two lookups ``resolve_keys`` makes."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.title_calls: list[str] = []
        self.manifest_calls: list[list[str]] = []

    def find_by_title_exact(self, title: str, **kw: Any) -> Any:
        self.title_calls.append(title)
        return self._real.find_by_title_exact(title, **kw)

    def get_manifests(self, doc_ids: list[str]) -> Any:
        self.manifest_calls.append(list(doc_ids))
        return self._real.get_manifests(doc_ids)


class _NoCatalog:
    """A catalog that fails on any lookup: for the checks that must raise or answer before they ask it anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def find_by_title_exact(self, title: str, **kw: Any) -> Any:
        self.calls.append(title)
        raise AssertionError(f"unexpected catalog lookup of {title!r}")

    def get_manifests(self, doc_ids: list[str]) -> Any:
        self.calls.append(repr(doc_ids))
        raise AssertionError(f"unexpected manifest fetch for {doc_ids!r}")


@pytest.fixture
def cat() -> ActiveCatalog:
    return ActiveCatalog()


@pytest.fixture
def client():
    with HttpCatalogClient() as c:
        yield c


def _doc(cat: ActiveCatalog, owner_name: str, title: str, texts: list[str], *,
         content_type: str = "knowledge") -> str:
    """Register *title* under its own owner with one chunk per text (real chunk rows, real manifest)."""
    owner = cat.register_owner(owner_name, "curator")
    chashes = [_chash(t) for t in texts]
    seed_chunks_direct(COLLECTION, chashes, texts)
    tumbler = str(cat.register(
        owner, title, content_type=content_type, physical_collection=COLLECTION, chunk_count=len(texts),
    ))
    cat.write_manifest(
        tumbler, [{"chash": c, "position": i} for i, c in enumerate(chashes)], collection=COLLECTION,
    )
    return tumbler


# ── the core function ───────────────────────────────────────────────────────────────────────────


@engine
def test_a_unique_an_absent_and_a_shared_token(cat: ActiveCatalog, client: HttpCatalogClient) -> None:
    only = _doc(cat, "o-a", "CUST-0001", ["customer one, page one", "customer one, page two"])
    a = _doc(cat, "o-b", "CUST-SHARED", ["shared title, copy a"])
    b = _doc(cat, "o-c", "CUST-SHARED", ["shared title, copy b"])

    out = resolve_keys(client, ["CUST-0001", "CUST-ABSENT", "CUST-SHARED"])

    assert out["tumblers"] == [only]
    assert out["ids"] == [_chash("customer one, page one"), _chash("customer one, page two")]
    assert out["collections"] == [COLLECTION, COLLECTION], "collections line up one-to-one with ids"
    assert out["chunk_collections"] == out["collections"]
    assert out["chunk_tumblers"] == [only, only], "chunk_tumblers line up one-to-one with ids"
    assert out["by_token"] == {
        "CUST-0001": {"tumbler": only, "ids": out["ids"], "collections": [COLLECTION, COLLECTION]},
    }, "only a resolved token has a by_token entry"
    assert out["missing"] == ["CUST-ABSENT"]
    assert out["ambiguous"] == {"CUST-SHARED": sorted([a, b])}
    assert a not in out["tumblers"] and b not in out["tumblers"], "an ambiguous token resolves to neither"
    assert _chash("shared title, copy a") not in out["ids"]


@engine
def test_a_trashed_document_is_missing(cat: ActiveCatalog, client: HttpCatalogClient) -> None:
    gone = _doc(cat, "o-a", "CUST-0002", ["customer two"])
    assert cat.delete_document(gone)

    out = resolve_keys(client, ["CUST-0002"])

    assert out["missing"] == ["CUST-0002"]
    assert out["tumblers"] == [] and out["ids"] == [] and out["ambiguous"] == {}


@engine
def test_trashing_one_of_two_copies_leaves_the_token_resolved(cat: ActiveCatalog, client: HttpCatalogClient) -> None:
    keep = _doc(cat, "o-a", "CUST-0003", ["kept copy"])
    drop = _doc(cat, "o-b", "CUST-0003", ["trashed copy"])
    assert resolve_keys(client, ["CUST-0003"])["ambiguous"] == {"CUST-0003": sorted([keep, drop])}
    assert cat.delete_document(drop)

    out = resolve_keys(client, ["CUST-0003"])

    assert out["tumblers"] == [keep] and out["ambiguous"] == {} and out["missing"] == []


@engine
def test_a_repeated_token_is_looked_up_once(cat: ActiveCatalog, client: HttpCatalogClient) -> None:
    only = _doc(cat, "o-a", "CUST-0004", ["customer four"])
    other = _doc(cat, "o-b", "CUST-0005", ["customer five"])
    spy = _Spy(client)

    out = resolve_keys(spy, ["CUST-0004", "CUST-0005", "CUST-0004", "CUST-0004"])

    assert spy.title_calls == ["CUST-0004", "CUST-0005"], "one lookup per distinct token, first-seen order"
    assert out["tumblers"] == [only, other]
    assert len(spy.manifest_calls) == 1, "the manifests of every resolved document come in one batch"


@engine
def test_a_chunk_shared_by_two_resolved_documents_is_listed_once(cat: ActiveCatalog, client: HttpCatalogClient) -> None:
    """Identical chunk text is one T3 row (RDR-108); two documents whose manifests list it give the plan one id."""
    a = _doc(cat, "o-a", "CUST-0007", ["boilerplate footer", "only in seven"])
    b = _doc(cat, "o-b", "CUST-0008", ["boilerplate footer", "only in eight"])

    out = resolve_keys(client, ["CUST-0007", "CUST-0008"])

    assert out["tumblers"] == [a, b]
    assert out["ids"] == [_chash("boilerplate footer"), _chash("only in seven"), _chash("only in eight")]
    assert len(out["collections"]) == len(out["ids"])
    assert out["chunk_tumblers"] == [a, a, b], "the shared chunk is credited to the document that contributed it first"


@engine
def test_by_token_keeps_each_tokens_own_chunks_when_two_documents_share_one(
    cat: ActiveCatalog, client: HttpCatalogClient,
) -> None:
    """Two single-chunk documents whose chunk text is identical are one T3 row (RDR-108). ``ids`` lists it once, but
    the repair that joins a record to the anchor that named it needs each token's own chunks."""
    a = _doc(cat, "o-a", "CUST-0013", ["identical text in both"])
    b = _doc(cat, "o-b", "CUST-0014", ["identical text in both"])
    shared = _chash("identical text in both")

    out = resolve_keys(client, ["CUST-0013", "CUST-0014"])

    assert out["ids"] == [shared], "the cross-document dedup is unchanged"
    assert out["chunk_tumblers"] == [a]
    assert out["by_token"] == {
        "CUST-0013": {"tumbler": a, "ids": [shared], "collections": [COLLECTION]},
        "CUST-0014": {"tumbler": b, "ids": [shared], "collections": [COLLECTION]},
    }, "the second token still reports the chunk the dedup took out of ids"


@engine
def test_the_title_match_is_case_exact(cat: ActiveCatalog, client: HttpCatalogClient) -> None:
    _doc(cat, "o-a", "CUST-0018", ["customer eighteen"])

    out = resolve_keys(client, ["cust-0018"])

    assert out["missing"] == ["cust-0018"] and out["tumblers"] == []


@engine
def test_owner_and_content_type_narrow_an_ambiguous_token(cat: ActiveCatalog, client: HttpCatalogClient) -> None:
    note = _doc(cat, "o-a", "CUST-0006", ["the note"], content_type="knowledge")
    paper = _doc(cat, "o-b", "CUST-0006", ["the paper"], content_type="paper")
    assert set(resolve_keys(client, ["CUST-0006"])["ambiguous"]["CUST-0006"]) == {note, paper}

    by_type = resolve_keys(client, ["CUST-0006"], content_type="paper")
    assert by_type["tumblers"] == [paper] and by_type["ambiguous"] == {}

    owner_prefix = note.rsplit(".", 1)[0]
    by_owner = resolve_keys(client, ["CUST-0006"], owner=owner_prefix)
    assert by_owner["tumblers"] == [note] and by_owner["ambiguous"] == {}


@pytest.mark.parametrize("by", ["chash", "tumbler", "", "TITLE"])
def test_any_other_by_value_raises(by: str) -> None:
    stub = _NoCatalog()
    with pytest.raises(ValueError, match="by"):
        resolve_keys(stub, ["CUST-0001"], by=by)
    assert stub.calls == [], "refused before any lookup"


@pytest.mark.parametrize("tokens", ["CUST-0001", ["CUST-0001", None], ["CUST-0001", 7], 7, None])
def test_tokens_that_are_not_a_list_of_strings_raise(tokens: Any) -> None:
    stub = _NoCatalog()
    with pytest.raises((TypeError, ValueError)):
        resolve_keys(stub, tokens)
    assert stub.calls == [], "refused before any lookup"


def test_a_blank_token_is_missing_without_a_catalog_call() -> None:
    """``find_by_title_exact("")`` falls back to a scan of every document, so a blank token never reaches it."""
    stub = _NoCatalog()

    out = resolve_keys(stub, ["", "   ", "\t", ""])

    assert stub.calls == []
    assert out["missing"] == ["", "   ", "\t"], "each distinct blank token is reported, in first-seen order"
    assert out["tumblers"] == [] and out["ids"] == [] and out["ambiguous"] == {} and out["by_token"] == {}


@engine
def test_a_blank_token_beside_a_real_one_leaves_the_real_one_resolved(
    cat: ActiveCatalog, client: HttpCatalogClient,
) -> None:
    only = _doc(cat, "o-a", "CUST-0012", ["customer twelve"])
    spy = _Spy(client)

    out = resolve_keys(spy, ["CUST-0012", " "])

    assert spy.title_calls == ["CUST-0012"]
    assert out["tumblers"] == [only] and out["missing"] == [" "]


def test_no_tokens_resolve_to_nothing_without_a_call() -> None:
    stub = _NoCatalog()
    out = resolve_keys(stub, [])
    assert out == EMPTY_RESULT
    assert stub.calls == []


# ── the plan step ───────────────────────────────────────────────────────────────────────────────


@engine
@pytest.mark.asyncio
async def test_resolve_then_store_get_many_on_its_ids_returns_the_record_text(cat: ActiveCatalog) -> None:
    text = "CUST-0010 is a member since 2019; renewal due in March."
    _doc(cat, "o-a", "CUST-0010", [text])
    plan = {"steps": [
        # a step-level scope.topic is forwarded to every step by the runner; resolve takes no topic and must not fail on it
        {"tool": "resolve", "args": {"tokens": ["CUST-0010", "CUST-NOPE"]}, "scope": {"topic": "customers"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
    ]}

    result = await plan_run(_match(plan), {})

    first, second = result.steps[0], result.steps[1]
    assert first["ids"] == [_chash(text)] and first["missing"] == ["CUST-NOPE"]
    assert second["contents"] == [text]
    assert second["missing"] == []


@engine
@pytest.mark.asyncio
async def test_a_resolve_step_dispatches_no_operator_and_no_mcp_tool(
    cat: ActiveCatalog, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher answers it itself: ``nexus.mcp.core`` has no ``resolve`` and no model is called."""
    _doc(cat, "o-a", "CUST-0011", ["customer eleven"])
    assert not hasattr(mcp_core, "resolve"), "resolve is not an MCP tool (RDR-147: no MCP surface)"
    seen: list[str] = []
    real = runner._hydrate_operator_args

    def spy(tool: str, args: dict[str, Any]) -> Any:
        seen.append(tool)
        return real(tool, args)

    monkeypatch.setattr(runner, "_hydrate_operator_args", spy)
    out = await runner._default_dispatcher("resolve", {"tokens": ["CUST-0011"]})

    assert len(out["tumblers"]) == 1 and out["missing"] == []
    assert seen == [], "no operator hydration, tiering or MCP lookup for a resolve step"


# ── the guards that let it through ──────────────────────────────────────────────────────────────


def test_the_scope_topic_the_runner_injects_is_not_forwarded_to_a_resolve_step() -> None:
    scope = {"topic": "customers"}
    args = {"tokens": ["CUST-0001"]}
    assert runner._apply_scope_to_args("search", scope, {"query": "q"}, bindings={}, step_outputs=[])["topic"] == "customers"
    out = runner._apply_scope_to_args("resolve", scope, args, bindings={}, step_outputs=[])
    assert out == args, "resolve takes no topic: the injected key is dropped"
    # a topic the plan author wrote on the step is a mistake and still reaches the dispatcher, which refuses it
    authored = runner._apply_scope_to_args("resolve", scope, {**args, "topic": "mine"}, bindings={}, step_outputs=[])
    assert authored["topic"] == "mine"


@pytest.mark.asyncio
async def test_a_resolve_step_with_an_unknown_argument_raises() -> None:
    with pytest.raises(ValueError, match=r"unexpected argument.*topic"):
        await runner._default_dispatcher("resolve", {"tokens": ["CUST-0001"], "topic": "mine"})


@pytest.mark.asyncio
async def test_a_resolve_step_without_tokens_raises() -> None:
    with pytest.raises(ValueError, match="'tokens' argument is required"):
        await runner._default_dispatcher("resolve", {"by": "title"})


def test_resolve_is_a_non_embedding_tool() -> None:
    assert "resolve" in runner._NON_EMBEDDING_TOOLS
    assert "traverse" in runner._NON_EMBEDDING_TOOLS


def test_the_embedding_domain_check_lets_resolve_through() -> None:
    # A collection arg of the wrong model for the declared domain trips the guard on a retrieval tool ...
    wrong = {"collections": "knowledge__x__voyage-context-3__v1"}
    with pytest.raises(runner.PlanRunEmbeddingDomainError):
        runner._check_embedding_domain(0, "search", {"taxonomy_domain": "code"}, wrong)
    # ... and not on resolve, which has no embedding space.
    runner._check_embedding_domain(0, "resolve", {"taxonomy_domain": "code"}, wrong)


def test_a_domain_scope_does_not_inject_a_corpus_into_a_resolve_step() -> None:
    args = {"tokens": ["CUST-0001"]}
    scope = {"taxonomy_domain": "code"}
    assert "corpus" in runner._apply_scope_to_args("search", scope, {"query": "q"}, bindings={}, step_outputs=[])
    assert "corpus" not in runner._apply_scope_to_args("resolve", scope, args, bindings={}, step_outputs=[])


def test_resolve_is_not_a_structured_retrieval_tool() -> None:
    # Adding it there would make the dispatcher inject structured=True and break the confinement census.
    assert "resolve" not in runner._RETRIEVAL_TOOLS


def test_the_cost_estimate_prices_resolve_as_a_known_zero_cost_step() -> None:
    est = estimate_plan_cost(json.dumps({"steps": [{"tool": "resolve", "args": {"tokens": ["a"]}}]}),
                             OperatorPriceTable({}))
    assert est.usd == 0.0 and est.ms == 0, est
    assert "resolve=retrieval(zero-cost)" in est.basis


# ── document confinement: the decision, pinned ──────────────────────────────────────────────────

_IN = "ab" * 32


def _confinement(mode: str = "engine_filter") -> DocumentConfinement:
    return DocumentConfinement("1.2.3", "knowledge__doc__bge-base-en-v15-768__v1", mode, (_IN,), 1)  # type: ignore[arg-type]


def test_resolve_is_a_confined_tool_and_is_not_run_in_a_confined_run() -> None:
    """A title lookup reaches every document in the tenant, so a run confined to one document cannot run it:
    the tumblers and chunk hashes it returns would be other documents', and an operator step reads ``ids``
    without going through the confined ``store_get_many`` cut. Disclosed as a skipped step, never silent."""
    assert "resolve" in CONFINED_TOOLS
    for mode in ("engine_filter", "steps_not_run"):
        d = confine_step("resolve", {"tokens": ["CUST-0001"]}, _confinement(mode))
        assert not d.run and d.skipped == "resolve_reaches_outside_document"


@pytest.mark.asyncio
async def test_a_confined_run_does_not_dispatch_a_resolve_step() -> None:
    calls: list[str] = []

    async def dispatcher(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(tool)
        return {"tumblers": ["9.9.9"], "ids": ["x"], "collections": ["c"], "missing": [], "ambiguous": {}}

    plan = {"steps": [{"tool": "resolve", "args": {"tokens": ["CUST-0001"]}}]}
    result = await plan_run(
        _match(plan), {}, dispatcher=dispatcher, bundle_operators=False, document_confinement=_confinement(),
    )

    assert calls == [], "the confined run never dispatched the resolve step"
    out = result.steps[0]
    assert out["ids"] == [] and out["tumblers"] == []
    assert out[CONFINEMENT_MARKER]["reason"] == "resolve_reaches_outside_document"
    assert set(EMPTY_RESULT) <= set(out), "every key a later $stepN.<key> may read from a resolve step is there"
    assert out["missing"] == [] and out["ambiguous"] == {} and out["by_token"] == {}


@pytest.mark.asyncio
async def test_a_step_reading_missing_from_a_skipped_resolve_step_neither_raises_nor_dispatches() -> None:
    calls: list[str] = []

    async def dispatcher(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(tool)
        return {}

    plan = {"steps": [
        {"tool": "resolve", "args": {"tokens": ["CUST-0001"]}},
        {"tool": "search", "args": {"query": "q", "corpus": "knowledge", "note": "$step1.missing"}},
    ]}
    result = await plan_run(
        _match(plan), {}, dispatcher=dispatcher, bundle_operators=False,
        document_confinement=_confinement("steps_not_run"),
    )

    assert calls == [], "neither step was dispatched"
    assert len(result.steps) == 2


# ── readers pair each id with its own document (chunk_tumblers, not per-document tumblers) ──

_RESOLVE_SHAPED = {
    "tumblers": ["1.1.1", "1.1.2"],          # one per resolved DOCUMENT
    "ids": ["c1", "c2", "c3"],
    "collections": ["k", "k", "k"],
    "chunk_collections": ["k", "k", "k"],
    "chunk_tumblers": ["1.1.1", "1.1.1", "1.1.2"],
    "missing": [], "ambiguous": {}, "by_token": {},
}


def test_the_continuation_envelope_pairs_each_id_with_the_document_that_contributed_it():
    (bundle,) = _harvest_hydrated_bundles([_RESOLVE_SHAPED], None)
    assert [it["tumbler"] for it in bundle["items"]] == ["1.1.1", "1.1.1", "1.1.2"]


def test_the_repair_hits_pair_each_id_with_the_document_that_contributed_it():
    assert [h.tumbler for h in _hits(_RESOLVE_SHAPED)] == ["1.1.1", "1.1.1", "1.1.2"]


def test_a_check_steps_evidence_source_pairs_each_id_with_the_document_that_contributed_it():
    resolved = {"ids": ["c1", "c2", "c3"]}
    out = runner._with_evidence_source(
        "operator_check", {"ids": "$step1.ids"}, resolved, [_RESOLVE_SHAPED],
    )
    assert out[runner._EVIDENCE_SOURCE_KEY]["tumblers"] == ["1.1.1", "1.1.1", "1.1.2"]
