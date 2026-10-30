# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 section 2a, the document confinement (nexus-jypmj; Sam, 2026-10-30).

A synthesis or unknown request that names exactly one source confines every secondary retrieval step to the
DOCUMENT: the engine's ``chash_allow`` filter on ``search`` and ``query`` when ``GET /version`` advertises
``vector_search_chash_allow_supported``, the document's address on ``traverse``, and no dispatch at all for a step
whose route takes no filter or on an engine without the flag. Tenant-free: the real runner is driven with a counting
dispatcher, the real ``HttpVectorClient`` and ``search_cross_corpus`` with the route fake of
``tests/test_search_per_collection_route.py``, and ``nx_answer`` with the catalog fake of
``tests/test_rdr232_primary_evidence.py``.

Every behaviour test here fails with its fix reverted; the revert checks are in the bead's hand-back.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import nexus.db.http_vector_client as hvc
from nexus.plans import runner
from nexus.plans.coverage_ledger import CoverageBoundary
from nexus.plans.coverage_repair import (
    MAX_REPAIR_ITEMS,
    REPAIR_RESULTS_PER_QUERY,
    REPAIR_RESULTS_PER_QUERY_UNFILTERED,
)
from nexus.plans.document_confinement import (
    CHASH_ALLOW_MAX,
    CONFINED_TOOLS,
    CONFINEMENT_MARKER,
    DocumentConfinement,
    active_chash_allow,
    active_confinement,
    build_confinement,
    chash_allow_scope,
    confine_step,
    confinement_scope,
    valid_chashes,
)
from nexus.plans.evidence import ScopeConstraint
from nexus.plans.primary_evidence import (
    DEFAULT_CAPS,
    DocumentChashes,
    PrimaryEvidence,
    PrimaryEvidenceCaps,
)
from nexus.plans.reserved_stream import RESERVED_SHARE_NARROWED
from nexus.plans.runner import plan_run
from nexus.plans.single_source import (
    SingleSourceNarrowing,
    derive_single_source_narrowing,
    unavailable_single_source_narrowing,
)
from tests.test_rdr232_coverage_ledger import PLAN as LEDGER_PLAN
from tests.test_rdr232_coverage_ledger import RDR_COLL, _match, _primary, _request, _reserved, _structured
from tests.test_rdr232_coverage_repair import (
    OTHER_REPAIR_TEXT,
    REPAIR_TEXT,
    RepairDispatcher,
    _cov,
)
from tests.test_rdr232_primary_evidence import (
    ART_COL,
    ART_DOC,
    ART_OWNER,
    ART_PATH,
    NEXUS_DOC,
    FakeCatalog,
    FakeT3,
    _answer,
    _coverage,
    _doc,
    _manuscript,
    _one_source_resolution,
    _resolved,
    _select,
    _two_projects,
    RDR084,
)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _coverage_on(monkeypatch):
    """The boundary tests below read the CoverageBoundary ``nx_answer`` hands the runner; the suite pins it off."""
    monkeypatch.setenv("NX_ANSWER_COVERAGE", "1")


@pytest.fixture(autouse=True)
def _tenant_free():
    """These tests are tenant-free: the runner's non-evidentiary hygiene and the owner narrowing's collection listing
    would ask the service, so each answers 'nothing excluded' and 'no owner'."""
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         patch("nexus.plans.owner_narrowing.narrow_args_by_owner", lambda args, owners: (args, None)):
        yield


IN_A, IN_B, OUT = _h("in a"), _h("in b"), _h("a sibling document's chunk")


def _confinement(mode: str = "engine_filter", chashes: tuple[str, ...] = (IN_A, IN_B), rows: int | None = None) -> DocumentConfinement:
    return DocumentConfinement(ART_DOC, ART_COL, mode, chashes, len(chashes) if rows is None else rows)  # type: ignore[arg-type]


# ── the step rules ───────────────────────────────────────────────────────────────────────────────────────


def test_a_search_or_query_step_carries_the_sorted_allow_list_in_engine_filter_mode() -> None:
    c = _confinement(chashes=(IN_B, IN_A))
    for tool, args in (("search", {"query": "q", "corpus": "rdr"}), ("query", {"question": "q", "corpus": "rdr"})):
        d = confine_step(tool, args, c)
        assert d.run and d.args["chash_allow"] == sorted([IN_A, IN_B]) and d.args["corpus"] == "rdr", tool
        assert "chash_allow" not in args, "the caller's args are not mutated"


@pytest.mark.parametrize("tool", ["search", "query", "traverse"])
def test_without_the_engine_flag_no_secondary_step_is_run(tool: str) -> None:
    d = confine_step(tool, {"query": "q"}, _confinement("steps_not_run"))
    assert not d.run and d.skipped == "steps_not_run"


def test_a_document_with_no_chunk_hash_is_never_filtered_it_is_not_run() -> None:
    built = build_confinement(tumbler=ART_DOC, collection=ART_COL, evidence=PrimaryEvidence(), engine_filter=True)
    assert built.mode == "steps_not_run" and built.chashes == (), "nothing to send, so nothing is dispatched"
    assert not confine_step("search", {"query": "q"}, built).run


@pytest.mark.parametrize("tool", ["search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped"])
def test_a_route_with_no_chunk_filter_is_not_run_even_when_the_engine_has_the_flag(tool: str) -> None:
    d = confine_step(tool, {"query": "q"}, _confinement())
    assert not d.run and d.skipped == "route_has_no_chash_filter"


@pytest.mark.parametrize("param", ["author", "content_type", "follow_links", "subtree"])
def test_a_query_step_that_takes_the_catalog_route_is_not_run(param: str) -> None:
    d = confine_step("query", {"question": "q", param: "x"}, _confinement())
    assert not d.run and d.skipped == "catalog_route_has_no_chash_filter"


def test_a_traverse_step_runs_in_engine_filter_mode_and_every_other_tool_is_untouched() -> None:
    c = _confinement()
    assert confine_step("traverse", {"seeds": ["1.2.4686"]}, c).run
    assert confine_step("generate", {"context": "x"}, c).args == {"context": "x"}
    assert confine_step("search", {"query": "q"}, None).args == {"query": "q"}
    assert CONFINED_TOOLS == {
        "search", "query", "search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped",
        "traverse", "store_get_many",
    }


def test_store_get_many_ids_are_cut_to_the_document_unless_its_set_is_a_sample() -> None:
    c = _confinement()
    args = {"ids": [IN_A, OUT, IN_B, ART_DOC, NEXUS_DOC], "collections": "rdr"}
    assert confine_step("store_get_many", args, c).args["ids"] == [IN_A, IN_B, ART_DOC]
    assert confine_step("store_get_many", args, _confinement("steps_not_run")).args["ids"] == [IN_A, IN_B, ART_DOC]
    sampled = _confinement(rows=3000)
    assert sampled.sampled and confine_step("store_get_many", args, sampled).args["ids"] == args["ids"]


def test_valid_chashes_drops_malformed_and_repeated_hashes() -> None:
    kept, bad = valid_chashes([IN_A, IN_A.upper(), "short", "", None, IN_B])
    assert kept == (IN_A, IN_B) and bad == 3


# ── the primary read holds the document's full manifest set ──────────────────────────────────────────────


def test_the_primary_read_keeps_every_manifest_hash_not_just_the_chunks_it_selected() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _manuscript(27, 5, 800))  # 135 chunks, the 60,000-character cap selects about 70
    _res, primary = _select(cat, t3, [RDR084], one_source=True, narrowed=True)
    held = primary.chashes_of(ART_DOC)
    assert held is not None and held.manifest_rows == 135 and len(held.chashes) == 135
    assert primary.selected_items < 135, "non-vacuity: the selection is a sample, the set is not"
    assert {i.chash for i in primary.items} <= set(held.chashes)
    assert primary.chashes_of("9.9.9") is None


def test_a_manifest_past_the_row_cap_gives_a_sampled_set_that_says_so() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _manuscript(10, 10, 20))  # 100 rows
    caps = PrimaryEvidenceCaps(max_manifest_rows_read=40)
    _res, primary = _select(cat, t3, [RDR084], caps=caps, one_source=True, narrowed=True)
    held = primary.chashes_of(ART_DOC)
    assert held is not None and len(held.chashes) == 40 and held.manifest_rows == 100
    built = build_confinement(tumbler=ART_DOC, collection=ART_COL, evidence=primary, engine_filter=True)
    assert built.sampled is True and built.mode == "engine_filter" and built.to_wire()["sampled"] is True


def test_the_wire_cap_is_the_engines() -> None:
    assert CHASH_ALLOW_MAX == 3000 == DEFAULT_CAPS.max_manifest_rows_read


# ── the one-source verdict ───────────────────────────────────────────────────────────────────────────────


def _inferred(res, *, foreign: int, fallback: str = "", inferred: bool = True):
    return replace(res, sources=(replace(res.sources[0], inferred=inferred, foreign=foreign, fallback=fallback),))


def test_an_inferred_source_narrows_only_when_no_other_project_carries_the_number() -> None:
    res, cov = _one_source_resolution(), _coverage("Synthesize RDR-084")
    alone = derive_single_source_narrowing(cov, _inferred(res, foreign=0), "")
    assert alone is not None and alone.applied and alone.confinement == "steps_not_run" and alone.step_scope == ART_COL
    shared = derive_single_source_narrowing(cov, _inferred(res, foreign=2), "rdr")
    assert shared is not None and not shared.applied
    assert "inferred" in shared.reason and "2 document(s) in other projects carry the same number" in shared.reason
    assert (shared.effective_scope, shared.confinement, shared.to_wire()["document"]) == ("rdr", None, "")
    assert "retrieval was not narrowed to it" in shared.disclosure()
    # an explicit owner is not an inference: a foreign document does not stop it
    explicit = derive_single_source_narrowing(cov, _inferred(res, foreign=2, inferred=False), "")
    assert explicit is not None and explicit.applied


def test_a_source_resolved_by_elimination_narrows() -> None:
    res, cov = _one_source_resolution(), _coverage("Synthesize RDR-084")
    fb = derive_single_source_narrowing(cov, _inferred(res, foreign=0, inferred=False, fallback="the active project holds no RDR-084"), "")
    assert fb is not None and fb.applied


def test_a_caller_scope_that_is_this_documents_address_is_kept_not_widened_to_the_collection() -> None:
    res, cov = _one_source_resolution(), _coverage("Synthesize RDR-084")
    kept = derive_single_source_narrowing(cov, res, ART_DOC)
    assert kept is not None and kept.applied and kept.step_scope == ART_DOC and kept.effective_scope == ART_DOC
    wider = derive_single_source_narrowing(cov, res, ART_OWNER)  # a subtree that merely holds it is replaced
    assert wider is not None and wider.step_scope == ART_COL and wider.effective_scope == ART_DOC
    other = derive_single_source_narrowing(cov, res, NEXUS_DOC)  # another document's address stays not-applied
    assert other is not None and not other.applied and other.effective_scope == NEXUS_DOC


def test_a_fault_in_the_derivation_is_an_unapplied_verdict_not_none() -> None:
    cov = _coverage("Synthesize RDR-084")
    failed = unavailable_single_source_narrowing(cov, "rdr")
    assert failed is not None and failed.applied is False and failed.source == "RDR-084"
    assert "could not be computed" in failed.reason and failed.effective_scope == "rdr"
    assert unavailable_single_source_narrowing(_coverage("What is the status of RDR-084?"), "") is None, (
        "a request the rule does not concern has no verdict to fail"
    )


def test_the_disclosure_names_the_document_the_confinement_the_primary_read_and_the_repair() -> None:
    res, cov = _one_source_resolution(), _coverage("Synthesize RDR-084")
    n = derive_single_source_narrowing(cov, res, "")
    assert n is not None
    primary = replace(
        PrimaryEvidence(), documents=(MagicMock(doc_id=ART_DOC, manifest_rows=58, selected_rows=57),),
    )
    engine = n.with_primary(primary, _confinement(chashes=tuple(_h(str(i)) for i in range(58)), rows=58))
    text = engine.disclosure()
    assert f"reads only that document ({ART_DOC})" in text and "other documents were not read" in text
    assert "confined to it by the engine (search and query by a filter of 58 chunk hashes; traversal by the document's address)" in text
    assert "a step on a route that takes no such filter was not run" in text
    assert "The primary read holds 57 of its 58 chunks" in text
    assert f"a coverage repair may add up to {MAX_REPAIR_ITEMS} more chunks of the same document" in text
    assert engine.to_wire()["confinement"] == "engine_filter" and engine.to_wire()["allow_chunks"] == 58
    sampled = n.with_primary(primary, _confinement(chashes=(IN_A,), rows=58)).disclosure()
    assert "a filter of 1 chunk hashes, a sample of its 58 manifest rows" in sampled
    off = n.with_primary(primary, _confinement("steps_not_run")).disclosure()
    assert "does not apply the document filter, so no secondary retrieval step (search, query, traverse) was run" in off
    assert "the other documents were not read" not in off and "by the engine" not in off


# ── the runner: a counting dispatcher over a real plan_run ───────────────────────────────────────────────


class CountingDispatcher:
    """Records every call; answers every retrieval tool with an empty structured shape and a generate."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool == "generate":
            return {"text": "the answer"}
        if tool == "store_get_many":
            return {"contents": [f"text of {i}" for i in args.get("ids", [])]}
        return _structured([], RDR_COLL) | ({"traversal": {"policy_applied": True}} if tool == "traverse" else {})

    def of(self, tool: str) -> list[dict]:
        return [a for t, a in self.calls if t == tool]

    @property
    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]


PLAN_ALL = {"steps": [
    {"tool": "search", "args": {"query": "q", "corpus": ART_COL}},
    {"tool": "query", "args": {"question": "q", "corpus": ART_COL}},
    {"tool": "search", "args": {"query": "another", "corpus": ART_COL, "limit": 5}},
    {"tool": "query", "args": {"question": "q", "corpus": ART_COL, "content_type": "rdr"}},
    {"tool": "search_metadata_scoped", "args": {"query": "q", "corpus": ART_COL}},
    {"tool": "search_topic_scoped", "args": {"query": "q", "topic": "t", "corpus": ART_COL}},
    {"tool": "search_graph_hop", "args": {"query": "q", "seeds": [ART_DOC], "corpus": ART_COL}},
    {"tool": "search_aspect_scoped", "args": {"query": "q", "corpus": ART_COL}},
    {"tool": "traverse", "args": {"seeds": [ART_DOC], "link_types": ["cites"]}},
    {"tool": "store_get_many", "args": {"ids": [IN_A, OUT, IN_B], "collections": ART_COL}},
    {"tool": "generate", "args": {"context": "$step10.contents", "question": "q"}},
]}
BINDINGS = {"_nx_scope": ART_COL}
RETRIEVAL = ["search", "query", "search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped", "traverse"]


async def _run_plan(c: DocumentConfinement | None, *, bindings: dict | None = None):
    d = CountingDispatcher()
    result = await plan_run(
        _match(PLAN_ALL), BINDINGS if bindings is None else bindings, dispatcher=d, bundle_operators=False,
        document_confinement=c,
    )
    return d, result


@pytest.mark.asyncio
async def test_with_the_engine_flag_every_search_and_query_step_sends_the_allow_list_and_nothing_else_leaks() -> None:
    d, result = await _run_plan(_confinement())
    allow = sorted([IN_A, IN_B])
    sent = d.of("search") + d.of("query")
    assert len(sent) == 3, "two searches and the one plain query: the three steps the engine can confine"
    assert all(a["chash_allow"] == allow for a in sent), "every secondary vector call carries the filter"
    assert {a.get("question") or a.get("query") for a in sent} == {"q", "another"}
    for tool in ("search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped"):
        assert d.of(tool) == [], f"{tool} has no chunk filter on its route, so it is not dispatched"
    assert d.tools.count("query") == 1, "the catalog-routed query step is not dispatched"
    (walk,) = d.of("traverse")
    assert walk["policy"]["allowed_collections"] == ART_DOC and "chash_allow" not in walk, "the walk is under the document's address"
    (hydrate,) = d.of("store_get_many")
    assert hydrate["ids"] == [IN_A, IN_B], "an id outside the document is cut"
    outcomes = {(x["tool"], x["outcome"]) for x in result.scope_diagnostics if x.get("scope_kind") == "document"}
    assert outcomes == {
        ("search", "document_filtered"), ("query", "document_filtered"), ("query", "document_confined"),
        ("search_metadata_scoped", "document_confined"), ("search_topic_scoped", "document_confined"),
        ("search_graph_hop", "document_confined"), ("search_aspect_scoped", "document_confined"),
        ("traverse", "document_scoped"),
    }
    skipped = result.steps[4]  # the metadata step
    assert skipped[CONFINEMENT_MARKER]["reason"] == "route_has_no_chash_filter" and skipped["ids"] == [], (
        "an empty structured shape, so a later $stepN reference resolves"
    )
    assert result.steps[-1]["text"] == "the answer"


@pytest.mark.asyncio
async def test_without_the_engine_flag_no_secondary_retrieval_step_is_dispatched() -> None:
    d, result = await _run_plan(_confinement("steps_not_run"))
    assert [t for t in d.tools if t in RETRIEVAL] == [], d.tools
    assert d.tools == ["store_get_many", "generate"], "the hydration (cut to the document) and the reduction still run"
    assert d.of("store_get_many")[0]["ids"] == [IN_A, IN_B]
    markers = [s[CONFINEMENT_MARKER] for s in result.steps if CONFINEMENT_MARKER in s]
    assert len(markers) == 9, "every retrieval step but the hydration left the marker"
    assert {m["mode"] for m in markers} == {"steps_not_run"}
    reasons = sorted(m["reason"] for m in markers)
    # two searches, the plain query, the catalog-routed query and the traversal are not run for lack of the flag; the
    # four combined-query routes have no filter whatever the engine says
    assert reasons.count("steps_not_run") == 5 and reasons.count("route_has_no_chash_filter") == 4, reasons


@pytest.mark.asyncio
async def test_a_run_that_is_not_confined_dispatches_everything_and_sends_no_filter() -> None:
    # the non-vacuity control for the two tests above: the same plan, no confinement
    d, result = await _run_plan(None)
    for tool in RETRIEVAL:
        assert d.of(tool), f"{tool} is dispatched when the run is not confined"
    assert all("chash_allow" not in a for _t, a in d.calls)
    assert d.of("store_get_many")[0]["ids"] == [IN_A, OUT, IN_B]
    assert not [x for x in result.scope_diagnostics if x.get("scope_kind") == "document"]


@pytest.mark.asyncio
async def test_the_runner_applies_the_narrowing_bindings_to_every_step_and_the_traversal_policy() -> None:
    # The seam, run through the runner (it was hand-built bindings before): the run's _nx_scope reaches every
    # corpus-taking step, a step pinned to another corpus is skipped and never run unscoped, a traversal carries the
    # collection and the owner in its policy. No confinement here: this is the defense-in-depth layer.
    plan = {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "query", "args": {"question": "q"}},
        {"tool": "search", "args": {"query": "q", "corpus": "knowledge,code"}},
        {"tool": "search", "args": {"query": "q", "corpus": "all"}},
        {"tool": "traverse", "args": {"seeds": [ART_DOC], "link_types": ["cites"]}},
        {"tool": "generate", "args": {"context": "x", "question": "q"}},
    ]}
    d = CountingDispatcher()
    result = await plan_run(
        _match(plan), {"_nx_scope": ART_COL, "_nx_owners": ART_OWNER}, dispatcher=d, bundle_operators=False,
    )
    corpora = [a.get("corpus") for a in d.of("search")] + [a.get("corpus") for a in d.of("query")]
    assert corpora == [ART_COL, ART_COL, ART_COL], "rdr, none and all each read the collection"
    assert len(d.of("search")) == 2, "the step on other collections was not dispatched"
    conflict = [x for x in result.scope_diagnostics if x["outcome"] == "conflict"]
    assert [x["step_index"] for x in conflict] == [2]
    (walk,) = d.of("traverse")
    assert walk["policy"]["allowed_collections"] == ART_COL and walk["policy"]["allowed_owners"] == [ART_OWNER]


def test_a_traversal_policy_for_a_confined_run_is_the_documents_address() -> None:
    with confinement_scope(_confinement()):
        out = runner._apply_traversal_policy_to_args("traverse", {"link_types": ["cites"]}, bindings={"_nx_scope": ART_COL})
    assert out["policy"]["allowed_collections"] == ART_DOC
    out = runner._apply_traversal_policy_to_args("traverse", {"link_types": ["cites"]}, bindings={"_nx_scope": ART_COL})
    assert out["policy"]["allowed_collections"] == ART_COL, "no confinement, the run's own scope"
    assert active_confinement() is None


# ── the dispatcher turns the arg into the context value the engine layer reads ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search", "query"])
async def test_the_default_dispatcher_hands_the_allow_list_to_the_tool_as_context_not_as_a_parameter(tool: str) -> None:
    from nexus.mcp import core

    seen: dict[str, Any] = {}

    def fake(*, query: str = "", question: str = "", structured: bool = False) -> dict:
        seen["kwargs"] = {"query": query, "question": question, "structured": structured}
        seen["allow"] = active_chash_allow()
        return {"ids": [], "tumblers": [], "distances": [], "collections": []}

    with patch.object(core, tool, fake):
        await runner._default_dispatcher(tool, {"query": "q", "question": "q", "chash_allow": [IN_A, IN_B]})
    assert seen["allow"] == (IN_A, IN_B)
    assert "chash_allow" not in seen["kwargs"] and seen["kwargs"]["structured"] is True
    assert active_chash_allow() is None, "the context value does not outlive the call"
    with patch.object(core, tool, fake):
        await runner._default_dispatcher(tool, {"query": "q", "question": "q"})
    assert seen["allow"] is None, "a step with no filter reads none"


@pytest.mark.asyncio
async def test_the_allow_list_reaches_a_tool_run_on_the_retrieval_executor() -> None:
    from nexus.mcp import core
    from nexus import call_deadline

    seen: list = []
    token = call_deadline.set_deadline(10**12)
    try:
        with patch.object(core, "search", lambda *, query="", structured=False: seen.append(active_chash_allow()) or {
            "ids": [], "tumblers": [], "distances": [], "collections": []}):
            await runner._default_dispatcher("search", {"query": "q", "chash_allow": [IN_A]})
    finally:
        call_deadline.reset(token)
    assert seen == [(IN_A,)], "the deadline path copies the context into the worker thread"


# ── the engine layer: search_cross_corpus and the client send the key on every route ────────────────────────


@pytest.fixture
def engine(monkeypatch):
    import nexus.search_engine as se
    from tests.test_search_per_collection_route import _BGE, _FakeEngine, _cols, _rows

    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)
    cols = _cols("rdr", _BGE, 1)
    return _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols}), cols


def test_search_cross_corpus_sends_chash_allow_on_the_per_collection_route(engine) -> None:
    from nexus.search_engine import search_cross_corpus

    fake, cols = engine
    with chash_allow_scope([IN_A, IN_B]):
        search_cross_corpus("q", cols, 5, fake.client, threshold_override=float("inf"), cluster_by=None)
    (call,) = fake.route_calls()
    assert call["chash_allow"] == [IN_A, IN_B]


def test_search_cross_corpus_sends_chash_allow_on_the_flat_search_and_the_hybrid_leg(engine, monkeypatch) -> None:
    from nexus.search_engine import search_cross_corpus

    fake, cols = engine
    fake.route = False  # an engine without the per-collection route: the batched /search, and /hybrid-search for lexical
    with chash_allow_scope([IN_A]):
        search_cross_corpus("q", cols, 5, fake.client, threshold_override=float("inf"), cluster_by=None, lexical=True)
    bodies = {p: b for p, b in fake.calls if p in ("/v1/vectors/search", "/v1/vectors/hybrid-search")}
    assert set(bodies) == {"/v1/vectors/search", "/v1/vectors/hybrid-search"}, bodies.keys()
    assert all(b["chash_allow"] == [IN_A] for b in bodies.values())


def test_the_reranked_flat_and_hybrid_legs_send_chash_allow_too(engine) -> None:
    from nexus.search_engine import search_cross_corpus

    fake, cols = engine
    fake.route = False
    with chash_allow_scope([IN_A, IN_B]):
        search_cross_corpus(
            "q", cols, 5, fake.client, threshold_override=float("inf"), cluster_by=None, lexical=True, rerank=True,
        )
    bodies = [b for p, b in fake.calls if p in ("/v1/vectors/search", "/v1/vectors/hybrid-search")]
    assert len(bodies) == 2 and all(b.get("rerank") is True and b["chash_allow"] == [IN_A, IN_B] for b in bodies), bodies


def test_search_cross_corpus_sends_no_key_without_a_scope(engine) -> None:
    from nexus.search_engine import search_cross_corpus

    fake, cols = engine
    search_cross_corpus("q", cols, 5, fake.client, threshold_override=float("inf"), cluster_by=None)
    assert "chash_allow" not in fake.route_calls()[0]


def test_an_empty_allow_list_is_sent_as_an_empty_array_which_the_engine_answers_with_no_rows(engine) -> None:
    from nexus.search_engine import search_cross_corpus

    fake, cols = engine
    with chash_allow_scope([]):
        search_cross_corpus("q", cols, 5, fake.client, threshold_override=float("inf"), cluster_by=None)
    assert fake.route_calls()[0]["chash_allow"] == []


def test_the_client_capability_is_a_json_true_cached_and_never_raises(monkeypatch) -> None:
    client = hvc.HttpVectorClient()
    answers: list[Any] = []
    monkeypatch.setattr(hvc, "_get", lambda path, **kw: answers.pop(0) if not isinstance(answers[0], Exception) else (_ for _ in ()).throw(answers.pop(0)))
    for body, want in (
        ({"vector_search_chash_allow_supported": True}, True), ({"vector_search_chash_allow_supported": "true"}, False),
        ({}, False), ("not a dict", False), (RuntimeError("edge trimmed"), False),
    ):
        client._chash_allow_flag = None  # a fresh client for each reading
        answers.append(body)
        assert client.supports_chash_allow() is want, body
    client._chash_allow_flag = None
    answers.append({"vector_search_chash_allow_supported": True})
    assert client.supports_chash_allow() is True
    assert client.supports_chash_allow() is True, "cached: the second reading made no request"
    assert answers == []


# ── the coverage repair ───────────────────────────────────────────────────────────────────────────────────


class DocRepair(RepairDispatcher):
    """Repair hits whose catalog tumbler names a SIBLING document (the last writer of a shared chunk)."""


def _boundary(c: DocumentConfinement | None) -> CoverageBoundary:
    return CoverageBoundary(
        request=_request(3), primary=_primary(), scope=ScopeConstraint.parse(RDR_COLL), confinement=c,
    )


async def _repair(d: DocRepair, c: DocumentConfinement | None):
    return _cov(await plan_run(
        _match(LEDGER_PLAN), {}, dispatcher=d,
        bundle_operators=False, coverage_boundary=_boundary(c), reserved_stream=_reserved(),
    ))


@pytest.mark.asyncio
async def test_a_repair_query_carries_the_allow_list_and_keeps_a_shared_chunk_whose_tumbler_names_a_sibling() -> None:
    allow = (_h(REPAIR_TEXT),)
    d = DocRepair(hydrates=True)
    cov = await _repair(d, _confinement(chashes=allow))
    searches = d.repair_searches()
    assert searches and all(a["chash_allow"] == list(allow) and a["limit"] == REPAIR_RESULTS_PER_QUERY for a in searches)
    # the stub's hits carry tumblers 1.1.N (siblings of 1.2.4686): the chunk is in the document by its hash
    assert cov.repair.items_merged == 1 and cov.repair.outcome == "completed"


@pytest.mark.asyncio
async def test_a_repair_hit_outside_the_documents_hashes_is_dropped_even_with_the_documents_own_tumbler() -> None:
    class Sibling(DocRepair):
        async def __call__(self, tool: str, args: dict) -> dict:
            out = await super().__call__(tool, args)
            if tool in ("search", "query") and self.is_repair_search(args):
                return out | {"tumblers": [ART_DOC] * len(out["ids"])}  # the metadata says this document ...
            return out

    d = Sibling(hydrates=True)
    cov = await _repair(d, _confinement("steps_not_run", chashes=(_h("something else"),)))
    assert cov.repair.items_merged == 0 and cov.repair.outcome == "no_new_evidence", "... the manifest says it is not"
    assert REPAIR_TEXT not in d.args_of("generate")[0]["context"]


@pytest.mark.asyncio
async def test_without_the_engine_flag_the_repair_asks_a_wide_page_sends_no_key_and_still_filters_by_hash() -> None:
    d = DocRepair(repair_hits=[REPAIR_TEXT, OTHER_REPAIR_TEXT], hydrates=True)
    cov = await _repair(d, _confinement("steps_not_run", chashes=(_h(REPAIR_TEXT),)))
    searches = d.repair_searches()
    assert searches and all("chash_allow" not in a for a in searches)
    assert all(a["limit"] == REPAIR_RESULTS_PER_QUERY_UNFILTERED == 40 for a in searches)
    assert cov.repair.items_found == 1 and cov.repair.items_merged == 1, "the sibling's chunk is found by the page and dropped"
    context = d.args_of("generate")[0]["context"]
    assert REPAIR_TEXT in context and OTHER_REPAIR_TEXT not in context


@pytest.mark.asyncio
async def test_a_repair_hit_the_primary_read_already_holds_is_not_added_again() -> None:
    held = _primary().items[0].chash
    d = DocRepair(repair_hits=[_primary().items[0].text], hydrates=True)
    d.known = {held: _primary().items[0].text}
    cov = await _repair(d, _confinement(chashes=(held,)))
    assert cov.repair.items_merged == 0, "held chunks are dropped before the fetch"


@pytest.mark.asyncio
async def test_a_run_with_no_confinement_repairs_exactly_as_before() -> None:
    d = DocRepair(hydrates=True)
    cov = await _repair(d, None)
    searches = d.repair_searches()
    assert all("chash_allow" not in a and a["limit"] == REPAIR_RESULTS_PER_QUERY for a in searches)
    assert cov.repair.items_merged == 1


# ── nx_answer: the wiring ─────────────────────────────────────────────────────────────────────────────────


async def _narrowed(chash_supported: bool, *, scope: str = "", question: str = "Synthesize RDR-084", **kw: Any):
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    runs: list = []
    misses: list = []
    env = await _answer(
        cat, bindings={"source": "RDR-084", "owner": ART_OWNER}, structured=True, t3=t3, plan_run_calls=runs,
        plan_miss_calls=misses, chash_supported=chash_supported, question=question, scope=scope, **kw,
    )
    return env, runs[0], misses[0], cat


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_nx_answer_builds_the_confinement_from_the_manifest_and_the_engines_capability(supported: bool) -> None:
    env, run, _miss, cat = await _narrowed(supported)
    c = run.call_args.kwargs["document_confinement"]
    held = [r.chash for r in cat.manifests[ART_DOC]]
    assert c.tumbler == ART_DOC and c.collection == ART_COL and set(c.chashes) == set(held) and c.manifest_rows == len(held)
    assert c.mode == ("engine_filter" if supported else "steps_not_run")
    narrowing = env["single_source_narrowing"]
    assert narrowing["confinement"] == c.mode and narrowing["document"] == ART_DOC == narrowing["effective_scope"]
    assert (narrowing["read_chunks"], narrowing["manifest_chunks"]) == (1, 1)
    assert narrowing["allow_chunks"] == (len(held) if supported else 0)
    assert ("by the engine" in env["final_text"]) is supported
    assert ("no secondary retrieval step" in env["final_text"]) is (not supported)


@pytest.mark.asyncio
async def test_nx_answer_hands_the_runner_the_collection_and_the_boundary_the_same_confinement() -> None:
    env, run, _miss, _cat = await _narrowed(True)
    assert run.call_args.args[1]["_nx_scope"] == ART_COL and run.call_args.args[1]["_nx_owners"] == ART_OWNER
    boundary = run.call_args.kwargs["coverage_boundary"]
    assert boundary.scope.value == ART_COL, "the scope the steps ran under, not the caller's and not the tumbler"
    assert boundary.confinement is run.call_args.kwargs["document_confinement"]
    assert env["single_source_narrowing"]["caller_scope"] == ""


@pytest.mark.asyncio
async def test_nx_answer_keeps_a_caller_scope_that_is_the_documents_own_address() -> None:
    env, run, _miss, _cat = await _narrowed(True, scope=ART_DOC)
    narrowing = env["single_source_narrowing"]
    assert narrowing["applied"] is True and narrowing["caller_scope"] == ART_DOC == narrowing["effective_scope"]
    assert run.call_args.args[1]["_nx_scope"] == ART_DOC, "round 3 widened this to the collection"
    assert run.call_args.kwargs["coverage_boundary"].scope.value == ART_DOC


@pytest.mark.asyncio
async def test_the_evidence_diagnostics_are_built_under_the_scope_the_steps_ran_under() -> None:
    from nexus.plans import evidence_diagnostics as ed

    seen: list = []
    real = ed.build_evidence_diagnostics

    def spy(**kw: Any) -> Any:
        seen.append(kw["scope"])
        return real(**kw)

    with patch.object(ed, "build_evidence_diagnostics", spy):
        env, _run, _miss, _cat = await _narrowed(True, scope="rdr")
    assert seen and all(s.value == ART_COL for s in seen), [s.value for s in seen]
    assert env["single_source_narrowing"]["effective_scope"] == ART_DOC and env["single_source_narrowing"]["caller_scope"] == "rdr"


@pytest.mark.asyncio
async def test_the_inline_planner_is_handed_the_callers_scope_not_the_collection() -> None:
    # nexus-4xmpv code review: a plan the planner grows must not pin the next question to this document's collection
    for scope in ("", "rdr"):
        env, _run, miss, _cat = await _narrowed(True, scope=scope)
        assert env["single_source_narrowing"]["applied"] is True
        assert miss.await_args.kwargs["scope"] == scope, scope


@pytest.mark.asyncio
async def test_a_single_query_plan_does_not_take_the_fast_path_when_the_run_is_confined() -> None:
    single = [{"tool": "query", "args": {"question": "$intent", "corpus": "rdr"}}]
    queries: list = []
    env, run, _miss, _cat = await _narrowed(True, plan_steps=single, query_calls=queries)
    queries_mock = queries[0]
    assert queries_mock.call_count == 0, "the fast path calls query() outside the runner, which would read every project"
    assert run.await_count == 1 and run.call_args.kwargs["document_confinement"].mode == "engine_filter"
    assert run.call_args.kwargs["reserved_stream"] is not None, "the primary read is reserved, as for any narrowed run"
    assert env["single_source_narrowing"]["applied"] is True

    # control: a lookup naming the same source is not narrowed, so the same plan takes the fast path
    queries.clear()
    env, run, _miss, _cat = await _narrowed(True, plan_steps=single, query_calls=queries, question="What is the status of RDR-084?")
    assert env["single_source_narrowing"] is None
    assert queries[0].call_count == 1 and run.await_count == 0, "non-vacuity: the single_query plan takes the fast path"


@pytest.mark.asyncio
async def test_a_fault_in_the_narrowing_is_reported_as_an_unapplied_verdict() -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("derive broke")

    with patch("nexus.plans.single_source.derive_single_source_narrowing", boom):
        env, run, _miss, _cat = await _narrowed(True)
    narrowing = env["single_source_narrowing"]
    assert narrowing is not None and narrowing["applied"] is False and "could not be computed" in narrowing["reason"]
    assert "document_confinement" not in run.call_args.kwargs and "_nx_scope" not in run.call_args.args[1]


@pytest.mark.asyncio
async def test_a_lookup_and_a_not_narrowed_run_carry_no_confinement() -> None:
    env, run, _miss, _cat = await _narrowed(True, question="What is the status of RDR-084?")
    assert env["single_source_narrowing"] is None and "document_confinement" not in run.call_args.kwargs
    assert run.call_args.kwargs["coverage_boundary"].confinement is None


# ── the end to end counting test: nx_answer, the real runner, a counting dispatcher ────────────────────────


async def _end_to_end(chash_supported: bool, question: str, plan_steps: list[dict]):
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    d = CountingDispatcher()
    from nexus.mcp.core import nx_answer
    from nexus.plans.match import Match

    match = Match(
        plan_id=0, name="ad-hoc", description="q", confidence=None, dimensions={}, tags="ad-hoc",
        plan_json=json.dumps({"steps": plan_steps}), required_bindings=["intent"], optional_bindings=[],
        default_bindings={"intent": "q"}, parent_dims=None,
    )
    db_stub = MagicMock()
    db_stub.telemetry._supports_nx_answer_run_complete.return_value = False
    db_stub.memory.get.return_value = None
    with patch("nexus.plans.matcher.plan_match", return_value=[]), \
         patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
         patch("nexus.plans.runner._default_dispatcher", d), \
         patch("nexus.mcp.core._nx_answer_chash_allow_supported", AsyncMock(return_value=chash_supported)), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp.core._get_catalog", return_value=cat), \
         patch("nexus.mcp.core._get_t3", return_value=MagicMock()), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=t3.fetch), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        env = await nx_answer(
            question=question, bindings={"source": "RDR-084", "owner": ART_OWNER}, structured=True, scope="",
        )
    return d, env, cat


E2E_PLAN = [
    {"tool": "search", "args": {"query": "$intent", "corpus": "rdr"}},
    {"tool": "query", "args": {"question": "$intent", "corpus": "rdr"}},
    {"tool": "search_graph_hop", "args": {"query": "$intent", "seeds": ["1.2.4686"], "corpus": "rdr"}},
    {"tool": "traverse", "args": {"seeds": ["1.2.4686"], "link_types": ["cites"]}},
    {"tool": "generate", "args": {"context": "$step1.contents", "question": "$intent"}},
]


@pytest.mark.asyncio
async def test_end_to_end_a_confined_run_sends_chash_allow_on_every_secondary_vector_call_when_the_flag_is_present() -> None:
    d, env, cat = await _end_to_end(True, "Synthesize RDR-084", E2E_PLAN)
    held = sorted(r.chash for r in cat.manifests[ART_DOC])
    vector = d.of("search") + d.of("query")
    assert len(vector) == 2 and all(a["chash_allow"] == held for a in vector), d.calls
    assert d.of("search_graph_hop") == []
    assert all(a["corpus"] == ART_COL for a in vector), "the collection narrowing is still the defense in depth"
    assert d.of("traverse")[0]["policy"]["allowed_collections"] == ART_DOC
    assert env["single_source_narrowing"]["confinement"] == "engine_filter"


@pytest.mark.asyncio
async def test_end_to_end_a_confined_run_dispatches_no_secondary_step_when_the_flag_is_absent() -> None:
    d, env, _cat = await _end_to_end(False, "Synthesize RDR-084", E2E_PLAN)
    assert [t for t in d.tools if t in RETRIEVAL] == [], d.tools
    assert "generate" in d.tools, "the reduction still runs, over the primary read"
    assert env["single_source_narrowing"]["confinement"] == "steps_not_run"
    outcomes = {x["outcome"] for x in env["scope_diagnostics"] if x.get("scope_kind") == "document"}
    assert outcomes == {"document_confined"}


@pytest.mark.asyncio
async def test_end_to_end_a_run_that_is_not_narrowed_dispatches_every_step_unfiltered() -> None:
    d, env, _cat = await _end_to_end(True, "What is the status of RDR-084?", E2E_PLAN)
    assert env["single_source_narrowing"] is None
    for tool in ("search", "query", "search_graph_hop", "traverse"):
        assert d.of(tool), f"non-vacuity: {tool} is dispatched when the run is not narrowed"
    assert all("chash_allow" not in a for _t, a in d.calls)


# ── the arithmetic ────────────────────────────────────────────────────────────────────────────────────────


def test_the_narrowed_share_and_the_repair_supplement_fill_the_reducers_slots_exactly() -> None:
    from nexus.plans.lifecycle_stream import LifecycleCaps

    assert RESERVED_SHARE_NARROWED == 88 and DEFAULT_CAPS.max_narrowed_source_items == 82
    assert DEFAULT_CAPS.max_narrowed_source_items + LifecycleCaps().max_records == RESERVED_SHARE_NARROWED
    assert RESERVED_SHARE_NARROWED + MAX_REPAIR_ITEMS == runner._OPERATOR_MAX_INPUTS == 100
