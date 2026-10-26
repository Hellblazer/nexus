# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.18): an explicit owner narrows the secondary streams.

An explicit ``owner``/``project`` binding used to select only the named primary source: the vector
stream still read every project's ``rdr`` collections, so another project's same-number RDR reached the
reducer. The rule under test (``nexus.plans.owner_narrowing``): the vector stream keeps only collections
of a project-owned content type (``code``/``docs``/``rdr``) whose owner segment matches an owner, a step
left with no collection is skipped and disclosed (the section 2a conflict path), the traversal policy
applies the same rule to the collections of the walked documents (other collections pass), a collection
whose owner cannot be read is kept and disclosed, and the ``single_query`` fast path applies the same
filter. A catalog-subtree ``scope`` is not an owner binding and narrows nothing.

No test here mints a tenant: the collection listing and the catalog rows are the two seams
(``expand_corpus`` and ``attribute_collection``), replaced by a small fake world; the runner is driven
through ``plan_run`` with a recording dispatcher; ``traverse`` runs against a fake catalog. The one
engine-backed claim (the golden fixture's foreign-owner count) lives in the existing tenant test
``tests/test_rdr232_golden_fixture.py``.
"""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.plans import owner_narrowing as on
from nexus.plans.match import Match
from nexus.plans.runner import plan_run

MODEL = "bge-base-en-v15-768"
RDR_ART = f"rdr__1-1__{MODEL}__v1"
RDR_NEXUS = f"rdr__1-2__{MODEL}__v1"
CODE_ART = f"code__1-1__{MODEL}__v1"
CODE_NEXUS = f"code__1-2__{MODEL}__v1"
CODE_TEN = f"code__1-10__{MODEL}__v1"
DOCS_NEXUS = f"docs__1-2__{MODEL}__v1"
KNOW = f"knowledge__papers__{MODEL}__v1"
PAPER = f"paper__1-2__{MODEL}__v1"

#: name -> (content_type, owner_id): what the catalog rows say.
WORLD: dict[str, tuple[str, str]] = {
    RDR_ART: ("rdr", "1-1"), RDR_NEXUS: ("rdr", "1-2"),
    CODE_ART: ("code", "1-1"), CODE_NEXUS: ("code", "1-2"), CODE_TEN: ("code", "1-10"),
    DOCS_NEXUS: ("docs", "1-2"), KNOW: ("knowledge", "papers"),
    PAPER: ("paper", "1-2"),
}

RETRIEVAL = frozenset({
    "search", "query", "search_metadata_scoped", "search_topic_scoped",
    "search_graph_hop", "search_aspect_scoped",
})


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """Replace the two seams with the fake world; ``calls`` records every corpus expanded."""
    calls: list[str] = []

    def expand(corpus: str) -> list[str]:
        calls.append(corpus)
        out: list[str] = []
        for entry in (e.strip() for e in corpus.split(",") if e.strip()):
            for name in WORLD:
                if entry == "all" or name == entry or name.startswith(entry + "__"):
                    out.append(name)
        return list(dict.fromkeys(out))

    monkeypatch.setattr(on, "expand_corpus", expand)
    monkeypatch.setattr(on, "attribute_collection", lambda name: WORLD.get(name))
    return calls


# ── the pure rule ───────────────────────────────────────────────────────────


def test_foreign_project_collections_are_removed_and_other_types_pass() -> None:
    args = {"query": "q", "corpus": "knowledge,code,docs,rdr,paper"}
    out, n = on.narrow_args_by_owner(args, "1.1")
    assert n is not None and n.status == "narrowed"
    assert out["corpus"] == f"{KNOW},{CODE_ART},{RDR_ART},{PAPER}"
    assert set(n.removed) == {CODE_NEXUS, CODE_TEN, DOCS_NEXUS, RDR_NEXUS}
    assert set(n.kept) == {KNOW, CODE_ART, RDR_ART, PAPER}, "knowledge and paper pass through unchanged"
    assert args["corpus"] == "knowledge,code,docs,rdr,paper", "the input args are not mutated"


def test_an_owner_matching_collection_set_leaves_the_args_untouched() -> None:
    args = {"query": "q", "corpus": "rdr,knowledge"}
    out, n = on.narrow_args_by_owner(args, "1.1,1.2")
    assert out is args and n.status == "within_scope" and n.removed == ()
    assert n.corpus == "rdr,knowledge", "a within-scope step keeps the corpus string it had"


def test_a_step_with_no_collection_left_is_a_conflict_and_never_unscoped() -> None:
    args = {"query": "q", "corpus": "docs,code__1-2"}
    out, n = on.narrow_args_by_owner(args, "1.1")
    assert out is args and n.conflict and n.corpus is None and n.kept == ()
    assert set(n.removed) == {DOCS_NEXUS, CODE_NEXUS}
    assert "owned by 1.1" in n.detail


def test_owner_one_one_does_not_keep_owner_one_ten() -> None:
    # `1-10` starts with `1-1`: the owner segment is matched whole, never as a prefix.
    out, n = on.narrow_args_by_owner({"corpus": "code"}, "1.1")
    assert out["corpus"] == CODE_ART and set(n.removed) == {CODE_NEXUS, CODE_TEN}
    only_ten, n10 = on.narrow_args_by_owner({"corpus": "code"}, "1.10")
    assert only_ten["corpus"] == CODE_TEN and set(n10.removed) == {CODE_ART, CODE_NEXUS}


def test_a_corpus_that_resolves_to_nothing_is_a_conflict() -> None:
    _, n = on.narrow_args_by_owner({"corpus": "nosuch"}, "1.1")
    assert n.conflict and n.removed == () and "no collection" in n.detail


def test_a_corpus_that_cannot_be_expanded_is_a_conflict_not_an_unscoped_run(monkeypatch) -> None:
    def boom(corpus: str) -> list[str]:
        raise RuntimeError("t3 unreachable")

    monkeypatch.setattr(on, "expand_corpus", boom)
    out, n = on.narrow_args_by_owner({"corpus": "rdr"}, "1.1")
    assert n.conflict and n.corpus is None and "RuntimeError" in n.detail
    assert out == {"corpus": "rdr"}


def test_a_step_with_no_corpus_reads_the_tool_default(world) -> None:
    out, n = on.narrow_args_by_owner({"query": "q"}, "1.1")
    assert world == [on.TOOL_DEFAULT_CORPUS]
    assert n.status == "narrowed" and out["corpus"] == f"{KNOW},{CODE_ART}"
    assert n.plan_corpus is None


@pytest.mark.parametrize("pin", [{"collection": CODE_NEXUS}, {"collections": [CODE_NEXUS, RDR_NEXUS]}])
def test_a_collection_pinned_step_keeps_its_pin_and_is_reported(pin, world) -> None:
    args = {"query": "q", **pin}
    out, n = on.narrow_args_by_owner(args, "1.1")
    assert out is args and n.status == "unscoped_collection_pin" and world == []


@pytest.mark.parametrize("binding", ["", None, "garbage", "1", [], "x,y"])
def test_no_usable_owner_is_no_narrowing(binding, world) -> None:
    args = {"corpus": "rdr"}
    assert on.narrow_args_by_owner(args, binding) == (args, None)
    assert world == []


def test_the_owner_segment_is_the_first_two_tumbler_segments_joined() -> None:
    assert on.owner_segments(["1.2", "1.2.4686", "3.10"]) == {"1-2", "3-10"}
    assert on.owner_tumblers("1.2.4686, 3.10 ,bad,1") == ("1.2", "3.10")


def test_a_collection_with_no_attribution_passes_and_is_disclosed(monkeypatch) -> None:
    monkeypatch.setattr(on, "attribute_collection", lambda name: None)
    _, n = on.narrow_args_by_owner({"corpus": "rdr"}, "1.1")
    assert n.status == "within_scope" and n.removed == ()
    assert set(n.unattributed) == {RDR_ART, RDR_NEXUS}
    assert set(n.to_diagnostic(step_index=0, tool="search")["unattributed"]) == {RDR_ART, RDR_NEXUS}


LEGACY_REPO_DOCS = "docs__nexus-571b8edd"
LEGACY_DEFAULT_DOCS = "docs__default"
LEGACY_CODE = "code__nexus-571b8edd"


def _legacy_world(monkeypatch) -> None:
    """The fake world plus old two-segment names, whose rows (or name fallback) give a non-tumbler owner id."""
    legacy = {
        LEGACY_REPO_DOCS: ("docs", "nexus-571b8edd"), LEGACY_DEFAULT_DOCS: ("docs", "default"),
        LEGACY_CODE: ("code", "nexus-571b8edd"),
    }
    world = {**WORLD, **legacy}
    monkeypatch.setattr(on, "attribute_collection", lambda name: world.get(name))
    monkeypatch.setattr(on, "expand_corpus", lambda corpus: [
        n for n in world if any(n == e or n.startswith(e + "__") for e in corpus.split(","))])


def test_a_legacy_named_project_collection_is_kept_and_disclosed_not_dropped_as_foreign(monkeypatch) -> None:
    _legacy_world(monkeypatch)
    out, n = on.narrow_args_by_owner({"corpus": "docs,rdr,code"}, "1.1")
    assert n.status == "narrowed"
    assert set(n.removed) == {DOCS_NEXUS, RDR_NEXUS, CODE_NEXUS, CODE_TEN}, "conformant foreign owners still go"
    assert set(n.unattributed) == {LEGACY_REPO_DOCS, LEGACY_DEFAULT_DOCS, LEGACY_CODE}
    assert set(out["corpus"].split(",")) == {RDR_ART, CODE_ART, *n.unattributed}
    assert set(n.to_diagnostic(step_index=0, tool="search")["unattributed"]) == set(n.unattributed)
    # only legacy names in the corpus: kept (within scope), not a conflict, and disclosed
    out2, n2 = on.narrow_args_by_owner({"corpus": LEGACY_REPO_DOCS}, "1.1")
    assert n2.status == "within_scope" and n2.unattributed == (LEGACY_REPO_DOCS,) and out2 == {"corpus": LEGACY_REPO_DOCS}


def test_the_name_grammar_fallback_reads_a_conformant_owner_and_leaves_a_legacy_name_unattributed(monkeypatch) -> None:
    # No monkeypatched world here: the real attribute_collection with no cached catalog row.
    monkeypatch.undo()
    import nexus.mcp_infra as infra

    monkeypatch.setattr(infra, "get_collection_row", lambda name, **kw: None)
    assert on.collection_kind(RDR_ART) == "project_owned"
    assert on.collection_kind(KNOW) == "other"
    assert on.collection_kind(LEGACY_REPO_DOCS) == "unattributed"
    assert on.collection_kind(LEGACY_DEFAULT_DOCS) == "unattributed"
    assert on.collection_kind("docs__") == "unattributed", "a name that fails the grammar has no owner to read"


def test_the_diagnostic_has_the_scope_check_shape_plus_the_owners() -> None:
    _, n = on.narrow_args_by_owner({"corpus": "rdr,code"}, "1.1")
    d = n.to_diagnostic(step_index=3, tool="search")
    assert d == {
        "step_index": 3, "tool": "search", "plan_corpus": "rdr,code", "caller_scope": "1-1",
        "scope_kind": "owner", "outcome": "narrowed", "resolved_corpus": f"{RDR_ART},{CODE_ART}",
        "owners": ["1.1"], "kept_collections": 2, "removed_collections": [RDR_NEXUS, CODE_NEXUS, CODE_TEN],
        "unattributed": [], "detail": "",
    }


# ── which request narrows ───────────────────────────────────────────────────


def _request(bindings=None, scope: str = "", lookup=None):
    from nexus.plans.evidence import OwnerNameError, build_evidence_request

    def default_lookup(name: str) -> str:
        if name == "ART":
            return "1.1"
        raise OwnerNameError(f"{name} names no owner")

    return build_evidence_request(bindings, scope=scope, name_lookup=lookup or default_lookup)


@pytest.mark.parametrize(
    ("bindings", "scope", "owners"),
    [
        pytest.param({"owner": "1.1", "source": "RDR-084"}, "rdr", ("1.1",), id="owner-binding"),
        pytest.param({"project": "ART", "source": "RDR-084"}, "rdr", ("1.1",), id="project-name-resolved"),
        pytest.param({"source": "RDR-084"}, "1.1", (), id="subtree-scope-alone-is-the-source-qualifier-only"),
        pytest.param({"source": "RDR-084"}, "1.1.4686", (), id="document-subtree-alone-narrows-nothing"),
        pytest.param({"owner": "1.1.4686"}, "", ("1.1",), id="document-tumbler-owner-addresses-its-owner"),
        pytest.param({"owner": "1.1"}, "1.1.9", ("1.1",), id="agreeing-qualifiers-are-one-owner"),
        pytest.param({"source": "RDR-084"}, "rdr", (), id="corpus-scope-alone-is-never-a-project"),
        pytest.param({"source": "RDR-084"}, "", (), id="no-qualifier"),
        pytest.param({"owner": "1.1"}, "1.2", (), id="conflicting-qualifiers-pick-nothing"),
        pytest.param({"project": "NoSuch"}, "", (), id="unresolved-name-picks-nothing"),
        pytest.param(None, "", (), id="no-bindings"),
    ],
)
def test_only_an_explicit_resolved_qualifier_narrows(bindings, scope, owners) -> None:
    assert on.request_owners(_request(bindings, scope)) == owners


def test_an_inferred_origin_never_counts() -> None:
    inferred = SimpleNamespace(owner=SimpleNamespace(
        status="resolved", tumbler="1.9", contributions=(("active_project", "1.9"),)))
    assert on.request_owners(inferred) == ()
    mixed = SimpleNamespace(owner=SimpleNamespace(
        status="resolved", tumbler="1.9", contributions=(("owner", "1.1"), ("active_project", "1.9"))))
    assert on.request_owners(mixed) == ("1.1",)


def test_the_binding_value_round_trips() -> None:
    assert on.owners_binding_value(["1.1", "1.1", "1.2"]) == "1.1,1.2"
    assert on.owners_binding_value([]) == ""
    assert on.owner_tumblers(on.owners_binding_value(["1.1", "1.2"])) == ("1.1", "1.2")


# ── the runner ──────────────────────────────────────────────────────────────


def _match(plan: dict) -> Match:
    return Match(
        plan_id=7, name="fixture", description="fixture", confidence=0.9, dimensions={"verb": "test"},
        tags="", plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool in RETRIEVAL:
            return {
                "ids": ["c1"], "tumblers": ["1.1.1"], "distances": [0.1], "collections": [RDR_ART],
                "chunk_collections": [RDR_ART], "chunk_text_hash": ["h1"], "contents": ["body"],
            }
        if tool == "traverse":
            if not args.get("seeds"):
                return {"ids": [], "tumblers": [], "collections": []}
            return {"ids": ["g1"], "tumblers": ["1.1.2"], "collections": [CODE_ART]}
        if tool == "store_get_many":
            return {"contents": ["c"] if args.get("ids") else [], "missing": []}
        return {"text": f"{tool}(stub)"}

    def corpora(self) -> list[tuple[str, str]]:
        return [(t, a.get("corpus", "")) for t, a in self.calls if t in RETRIEVAL]


def _run(plan: dict, bindings: dict, *, cut: int | None = None):
    rec = _Recorder()
    result = asyncio.run(plan_run(
        _match(plan), bindings, dispatcher=rec, continuation_cut_at_step=cut, bundle_operators=False,
    ))
    return result, rec


PLAN = {"steps": [
    {"tool": "search", "args": {"query": "$intent", "corpus": "all"}},
    {"tool": "query", "args": {"question": "$intent", "corpus": "rdr,knowledge"}},
    {"tool": "search", "args": {"query": "$intent", "corpus": "docs"}},
    {"tool": "search", "args": {"query": "$intent", "collection": CODE_NEXUS}},
    {"tool": "traverse", "args": {"seeds": ["$step1.tumblers", "$step3.tumblers"], "depth": 1}},
    {"tool": "store_get_many", "args": {
        "ids": ["$step1.ids", "$step2.ids", "$step3.ids"],
        "collections": ["$step1.collections", "$step2.collections", "$step3.collections"],
    }},
    {"tool": "summarize", "args": {"inputs": "$step6.contents"}},
]}


def test_the_runner_narrows_every_corpus_step_skips_the_empty_one_and_reports_the_pin() -> None:
    result, rec = _run(PLAN, {"intent": "q", "_nx_owners": "1.1"}, cut=6)

    kept_all = f"{RDR_ART},{CODE_ART},{KNOW},{PAPER}"
    assert rec.corpora() == [("search", kept_all), ("query", f"{RDR_ART},{KNOW}"), ("search", "")]
    # the docs-only step (only the foreign docs collection) is skipped, never dispatched
    assert [t for t, _ in rec.calls[:3]] == ["search", "query", "search"]
    assert rec.calls[2][1].get("collection") == CODE_NEXUS, "the pinned step ran as written"

    by_step = {d["step_index"]: d for d in result.scope_diagnostics}
    assert set(by_step) == {0, 1, 2, 3}
    assert by_step[0]["outcome"] == "narrowed" and by_step[0]["scope_kind"] == "owner"
    assert set(by_step[0]["removed_collections"]) == {CODE_NEXUS, CODE_TEN, DOCS_NEXUS, RDR_NEXUS}
    assert by_step[1]["outcome"] == "narrowed" and by_step[1]["resolved_corpus"] == f"{RDR_ART},{KNOW}"
    assert by_step[2]["outcome"] == "conflict" and by_step[2]["removed_collections"] == [DOCS_NEXUS]
    assert by_step[3]["outcome"] == "unscoped_collection_pin"
    assert all(d["owners"] == ["1.1"] and d["caller_scope"] == "1-1" for d in result.scope_diagnostics)

    skipped = result.steps[2]
    assert skipped["ids"] == [] and skipped["contents"] == []
    marker = skipped["scope_conflict"]
    assert (marker["scope_kind"], marker["caller_scope"], marker["plan_corpus"]) == ("owner", "1-1", "docs")
    assert marker["owners"] == ["1.1"]
    assert not any("scope_conflict" in result.steps[i] for i in (0, 1, 3))
    assert dict(rec.calls)["traverse"]["seeds"] == ["1.1.1"], "the skipped step fed the graph nothing"
    resolved = {a["step_index"]: a for a in result.resolved_step_args}
    assert resolved[0]["corpus"] == kept_all and resolved[2].get("skipped") is True


def test_without_owners_the_runner_resolves_exactly_what_it_did_before(world) -> None:
    result, rec = _run(PLAN, {"intent": "q"}, cut=6)
    assert rec.corpora()[:2] == [("search", "all"), ("query", "rdr,knowledge")]
    assert result.scope_diagnostics == [] and world == []


def test_owner_narrowing_runs_after_the_section_2a_scope_narrowing(world) -> None:
    result, rec = _run(PLAN, {"intent": "q", "_nx_scope": "rdr", "_nx_owners": "1.1"}, cut=6)
    # `all` -> `rdr` (2a), then `rdr` -> the one ART rdr collection (owner); `docs` conflicts at 2a first
    assert rec.corpora()[0] == ("search", RDR_ART)
    assert "all" not in world and world[0] == "rdr"
    by_step = {(d["step_index"], d["scope_kind"]): d["outcome"] for d in result.scope_diagnostics}
    assert by_step[(0, "corpus_prefix")] == "narrowed" and by_step[(0, "owner")] == "narrowed"
    assert by_step[(2, "corpus_prefix")] == "conflict" and (2, "owner") not in by_step, (
        "a step the scope skipped never reaches the owner stage")


def test_every_retrieval_step_skipped_by_the_owner_is_empty_retrieval() -> None:
    from nexus.mcp.core import _nx_answer_empty_retrieval_outcome, _nx_answer_is_empty_retrieval

    plan = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "docs"}},
        {"tool": "traverse", "args": {"seeds": ["$step1.tumblers"]}},
        {"tool": "store_get_many", "args": {"ids": ["$step1.ids"], "collections": ["$step1.collections"]}},
        {"tool": "summarize", "args": {"inputs": "$step3.contents"}},
    ]}
    result, rec = _run(plan, {"intent": "q", "_nx_owners": "1.1"}, cut=3)
    assert rec.corpora() == []
    assert [d["outcome"] for d in result.scope_diagnostics] == ["conflict"]
    assert dict(rec.calls)["traverse"]["seeds"] == []
    assert _nx_answer_is_empty_retrieval(result.steps) is True
    assert _nx_answer_empty_retrieval_outcome(result.steps[:1]) is None, "the caller's error: no outcome counted"


def test_the_traverse_step_gets_the_owners_in_its_policy() -> None:
    _, rec = _run(PLAN, {"intent": "q", "_nx_owners": "1.1"}, cut=6)
    policy = dict(rec.calls)["traverse"]["policy"]
    assert policy["allowed_owners"] == ["1.1"]
    _, bare = _run(PLAN, {"intent": "q"}, cut=6)
    assert "allowed_owners" not in dict(bare.calls)["traverse"]["policy"]


def test_the_runner_expands_the_corpus_off_the_event_loop(monkeypatch) -> None:
    # expand_corpus can be a cold, blocking T3 listing: it must not run on the loop's own thread.
    seen: list[int] = []
    real = on.expand_corpus

    def recording(corpus: str) -> list[str]:
        seen.append(threading.get_ident())
        return real(corpus)

    monkeypatch.setattr(on, "expand_corpus", recording)
    _run(PLAN, {"intent": "q", "_nx_owners": "1.1"}, cut=6)
    assert seen and all(t != threading.get_ident() for t in seen), "the loop thread did the blocking listing"


def test_a_plan_declared_policy_cannot_remove_the_owners() -> None:
    plan = {"steps": [{"tool": "traverse", "args": {
        "seeds": ["1.1.1"], "policy": {"allowed_owners": [], "max_documents": 3}}}]}
    _, rec = _run(plan, {"intent": "q", "_nx_owners": "1.1"})
    policy = rec.calls[0][1]["policy"]
    assert policy["allowed_owners"] == ["1.1"] and policy["max_documents"] == 3


# ── the traversal policy ────────────────────────────────────────────────────

RDR_T = RDR_ART
SEED = "1.1.1"


def _node(tumbler: str, collection: str):
    return SimpleNamespace(
        tumbler=tumbler, physical_collection=collection,
        content_type={"rdr": "rdr", "code": "code", "knowledge": "paper", "paper": "paper", "docs": "prose"}[
            collection.split("__")[0]],
    )


def _edge(frm: str, to: str, link_type: str):
    return SimpleNamespace(from_tumbler=frm, to_tumbler=to, link_type=link_type)


def _traverse(nodes, edges, policy: dict, seeds=(SEED,)):
    from nexus.mcp.core import traverse

    cat = MagicMock()
    reply = {"nodes": nodes, "edges": edges}
    cat.graph.return_value = reply
    cat.graph_many.return_value = reply
    cat.get_manifests.side_effect = lambda ts: {
        t: [SimpleNamespace(chash=f"{t}:{j}".encode().hex().ljust(64, "0")) for j in range(2)] for t in ts
    }
    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        return traverse(seeds=list(seeds), link_types=["implements", "cites"], policy=policy)


def _graph():
    nodes = [
        _node(SEED, RDR_ART),
        _node("1.1.7", RDR_ART), _node("1.1.8", CODE_ART),
        _node("1.2.5", RDR_NEXUS), _node("1.2.6", CODE_NEXUS),
        _node("1.9.1", KNOW), _node("1.2.9", PAPER),
        _node("1.10.2", RDR_ART),
    ]
    edges = [_edge(t.tumbler, SEED, "implements") for t in nodes[1:]]
    return nodes, edges


def test_the_traversal_drops_foreign_project_documents_and_keeps_the_rest() -> None:
    nodes, edges = _graph()
    out = _traverse(nodes, edges, {"allowed_collections": "", "allowed_owners": ["1.1"]})
    assert out["tumblers"] == [SEED, "1.1.7", "1.1.8", "1.2.9", "1.9.1"], (
        "a foreign rdr or code document and 1.10 (a string prefix of 1.1) are out; a paper and a knowledge "
        "document are kept, as the vector stream keeps them")
    rep = out["traversal"]
    assert rep["dropped_out_of_owner"] == 3 and rep["owners"] == ["1.1"] and rep["owner_unattributed"] == []
    assert rep["nodes_retained"] == 5 and rep["nodes_walked"] == 8
    assert "1.2.5" not in out["tumblers"] and "1.2.6" not in out["tumblers"]


def test_a_document_of_a_docs_collection_is_project_owned_whatever_its_catalog_content_type() -> None:
    # The kind comes from the collection row, never from the document's own type ("prose" for a docs doc).
    nodes = [_node(SEED, RDR_ART), _node("1.2.4", DOCS_NEXUS), _node("1.1.4", f"docs__1-1__{MODEL}__v1")]
    world = {f"docs__1-1__{MODEL}__v1": ("docs", "1-1")}
    with patch.object(on, "attribute_collection", lambda n: {**WORLD, **world}.get(n)):
        out = _traverse(nodes, [_edge(n.tumbler, SEED, "cites") for n in nodes[1:]],
                        {"allowed_collections": "", "allowed_owners": ["1.1"]})
    assert out["tumblers"] == [SEED, "1.1.4"] and out["traversal"]["dropped_out_of_owner"] == 1


def test_a_collection_whose_owner_cannot_be_read_passes_the_graph_and_is_reported(monkeypatch) -> None:
    legacy = "docs__nexus-571b8edd"
    world = {**WORLD, legacy: ("docs", "nexus-571b8edd")}
    monkeypatch.setattr(on, "attribute_collection", lambda n: world.get(n))
    nodes = [_node(SEED, RDR_ART), _node("1.1.5", legacy), _node("1.2.5", RDR_NEXUS)]
    out = _traverse(nodes, [_edge(n.tumbler, SEED, "cites") for n in nodes[1:]],
                    {"allowed_collections": "", "allowed_owners": ["1.1"]})
    assert out["tumblers"] == [SEED, "1.1.5"]
    assert out["traversal"]["owner_unattributed"] == [legacy] and out["traversal"]["dropped_out_of_owner"] == 1


def test_select_documents_with_owners_and_no_collection_kinds_fails_closed() -> None:
    from nexus.plans.traversal_policy import TraversalPolicy, select_documents

    policy = TraversalPolicy.from_wire({"allowed_collections": "", "allowed_owners": ["1.1"]})
    nodes, edges = _graph()
    with pytest.raises(ValueError, match="collection_kinds"):
        select_documents(nodes, edges, [SEED], policy)


def test_a_seed_stays_in_its_own_stream_whatever_its_owner() -> None:
    nodes, edges = _graph()
    out = _traverse(nodes, edges, {"allowed_collections": "", "allowed_owners": ["1.1"]}, seeds=(SEED, "1.2.5"))
    assert out["tumblers"] == [SEED, "1.2.5", "1.1.7", "1.1.8", "1.2.9", "1.9.1"], (
        "a seed is never evicted by the owner filter")
    out2 = _traverse(
        nodes, edges,
        {"allowed_collections": "", "allowed_owners": ["1.1"], "preserve_seed_documents": False},
        seeds=(SEED, "1.2.5"),
    )
    assert out2["tumblers"] == [SEED, "1.1.7", "1.1.8", "1.2.9", "1.9.1"], (
        "with seeds not preserved a seed is an ordinary candidate")


def test_no_owners_is_the_policy_it_was() -> None:
    nodes, edges = _graph()
    out = _traverse(nodes, edges, {"allowed_collections": ""})
    assert {"1.2.5", "1.2.6", "1.9.1", "1.10.2"} <= set(out["tumblers"])
    assert out["traversal"]["dropped_out_of_owner"] == 0 and out["traversal"]["owners"] == []
    assert out["traversal"]["owner_unattributed"] == []


def test_the_policy_wire_validates_allowed_owners() -> None:
    from nexus.plans.traversal_policy import TraversalPolicy, TraversalPolicyError

    p = TraversalPolicy.from_wire({"allowed_collections": "rdr", "allowed_owners": ["1.1", "1.2"]})
    assert p.allowed_owners == ("1.1", "1.2") and p.to_wire()["allowed_owners"] == ["1.1", "1.2"]
    assert "allowed_owners" not in TraversalPolicy.from_wire({"allowed_collections": "rdr"}).to_wire()
    for bad in ("1.1", ["1"], ["x.y"], [1.1], [""], 7):
        with pytest.raises(TraversalPolicyError):
            TraversalPolicy.from_wire({"allowed_collections": "rdr", "allowed_owners": bad})


def test_derive_policy_carries_the_owners() -> None:
    from nexus.plans.traversal_policy import derive_policy

    assert derive_policy({"link_types": ["cites"]}, "rdr", owners="1.1,1.2")["allowed_owners"] == ["1.1", "1.2"]
    assert "allowed_owners" not in derive_policy({"link_types": ["cites"]}, "rdr")
    assert "allowed_owners" not in derive_policy({"link_types": ["cites"]}, "rdr", owners="bad")


# ── the disclosure ──────────────────────────────────────────────────────────


def _owner_diag(step: int, outcome: str, removed: list[str], plan: str = "rdr") -> dict:
    return {
        "step_index": step, "tool": "search", "plan_corpus": plan, "caller_scope": "1-1",
        "scope_kind": "owner", "outcome": outcome, "resolved_corpus": None if outcome == "conflict" else RDR_ART,
        "owners": ["1.1"], "kept_collections": 0 if outcome == "conflict" else 1,
        "removed_collections": removed, "unattributed": [], "detail": "",
    }


def test_the_conflict_notice_names_the_owner_reason() -> None:
    from nexus.plans.evidence import scope_conflict_clauses, scope_conflict_notice

    _, removed = on.narrow_args_by_owner({"corpus": "docs"}, "1.1")
    diags = [_owner_diag(0, "narrowed", [RDR_NEXUS]), removed.to_diagnostic(step_index=1, tool="search")]
    (clause,) = scope_conflict_clauses(diags)
    assert "step 2" in clause and "'docs'" in clause and "no collection of 'docs' is owned by 1.1" in clause
    assert "owner 1.1" in clause and "1-1" not in clause, "the owner is named by tumbler, not by collection segment"
    assert "outside the caller scope" not in clause
    assert scope_conflict_notice(diags).startswith("[scope conflict:")


def test_the_conflict_notice_for_a_failed_expansion_does_not_claim_the_corpus_holds_no_owner_collection(
        monkeypatch) -> None:
    from nexus.plans.evidence import scope_conflict_clauses

    def boom(corpus: str) -> list[str]:
        raise RuntimeError("t3 unreachable")

    monkeypatch.setattr(on, "expand_corpus", boom)
    _, n = on.narrow_args_by_owner({"corpus": "rdr"}, "1.1")
    (clause,) = scope_conflict_clauses([n.to_diagnostic(step_index=0, tool="search")])
    assert "could not be expanded to collections (RuntimeError)" in clause, clause
    assert "owned by" not in clause and "holds no collection" not in clause, clause
    assert "owner 1.1" in clause and "1-1" not in clause and "skipped" in clause


def test_the_conflict_notice_for_an_empty_listing_does_not_claim_the_corpus_holds_no_owner_collection() -> None:
    from nexus.plans.evidence import scope_conflict_clauses

    _, n = on.narrow_args_by_owner({"corpus": "nosuch"}, "1.1")
    (clause,) = scope_conflict_clauses([n.to_diagnostic(step_index=0, tool="query")])
    assert "'nosuch' resolves to no collection" in clause, clause
    assert "owned by" not in clause and "holds no collection" not in clause, clause
    assert "owner 1.1" in clause and "1-1" not in clause


def test_evidence_diagnostics_report_the_owners_removals_and_skipped_steps() -> None:
    from nexus.plans.evidence import ScopeConstraint
    from nexus.plans.evidence_diagnostics import build_evidence_diagnostics

    diags = [_owner_diag(0, "narrowed", [RDR_NEXUS, CODE_NEXUS]), _owner_diag(1, "conflict", [DOCS_NEXUS], plan="docs")]
    steps = [
        {"ids": ["a"], "tumblers": ["1.1.1"], "chunk_collections": [RDR_ART], "collections": [RDR_ART]},
        {"ids": [], "tumblers": [], "scope_conflict": {
            "step_index": 1, "tool": "search", "plan_corpus": "docs", "caller_scope": "1-1",
            "scope_kind": "owner", "owners": ["1.1"]}},
    ]
    d = build_evidence_diagnostics(
        steps=steps, tools=["search", "search"], scope=ScopeConstraint.parse("rdr"),
        scope_diagnostics=diags, reducer_expected=False)
    sc = d["scope_check"]
    assert sc["conflict_steps"] == [1] and sc["state"] == "conflict"
    on_ = sc["owner_narrowing"]
    assert on_["owners"] == ["1.1"]
    assert on_["removed_total"] == 3
    assert on_["steps"] == [
        {"step_index": 0, "tool": "search", "outcome": "narrowed", "kept": 1, "removed": [RDR_NEXUS, CODE_NEXUS],
         "unattributed": []},
        {"step_index": 1, "tool": "search", "outcome": "conflict", "kept": 0, "removed": [DOCS_NEXUS],
         "unattributed": []},
    ]
    assert on_["skipped_steps"] == [1] and on_["unattributed"] == []
    assert on_["applied"] is False and on_["not_applied"] is None, "no resolution was passed: nothing to say"
    assert [s["step_index"] for s in d["skipped_steps"]] == [1]
    # no owner entry: the key is present and empty, so a reader never has to test for absence
    bare = build_evidence_diagnostics(
        steps=steps[:1], tools=["search"], scope=ScopeConstraint.parse("rdr"), reducer_expected=False)
    assert bare["scope_check"]["owner_narrowing"] == {
        "applied": False, "not_applied": None, "owners": [], "removed_total": 0, "steps": [],
        "skipped_steps": [], "unattributed": []}


@pytest.mark.parametrize(
    ("bindings", "scope", "applied", "reason"),
    [
        pytest.param({"owner": "1.1"}, "rdr", True, None, id="explicit-owner-applied"),
        pytest.param({"owner": "1.1"}, "1.2", False, "conflict", id="owner-against-another-subtree"),
        pytest.param({"owner": "1.1", "project": "1.2"}, "", False, "conflict", id="owner-against-project"),
        pytest.param({"project": "NoSuch"}, "", False, "unresolved", id="unresolved-project-name"),
        pytest.param({"source": "RDR-084"}, "1.1", False, None, id="subtree-scope-only-asked-for-no-narrowing"),
        pytest.param(None, "", False, None, id="no-qualifier"),
    ],
)
def test_the_diagnostics_say_plainly_when_the_owner_was_not_applied_and_why(bindings, scope, applied, reason) -> None:
    from nexus.plans.evidence import ScopeConstraint
    from nexus.plans.evidence_diagnostics import build_evidence_diagnostics

    request = _request(bindings, scope)
    d = build_evidence_diagnostics(
        steps=[], tools=[], scope=ScopeConstraint.parse(""), reducer_expected=False,
        owner_resolution=request.owner.to_wire())
    wire = d["scope_check"]["owner_narrowing"]
    assert wire["applied"] is applied
    if reason is None:
        assert wire["not_applied"] is None
    else:
        assert wire["not_applied"]["reason"] == reason
        assert "owner narrowing was not applied" in wire["not_applied"]["detail"]
        assert request.owner.detail in wire["not_applied"]["detail"], "the resolution's own explanation rides along"


def test_the_diagnostics_list_the_unattributed_collections_of_steps_and_traversals(monkeypatch) -> None:
    from nexus.plans.evidence import ScopeConstraint
    from nexus.plans.evidence_diagnostics import build_evidence_diagnostics

    _legacy_world(monkeypatch)
    _, n = on.narrow_args_by_owner({"corpus": "docs"}, "1.1")
    nodes = [_node(SEED, RDR_ART), _node("1.1.5", LEGACY_CODE)]
    out = _traverse(nodes, [_edge("1.1.5", SEED, "cites")], {"allowed_collections": "", "allowed_owners": ["1.1"]})
    d = build_evidence_diagnostics(
        steps=[{"ids": [], "tumblers": []}, out], tools=["search", "traverse"], scope=ScopeConstraint.parse(""),
        scope_diagnostics=[n.to_diagnostic(step_index=0, tool="search")], reducer_expected=False)
    wire = d["scope_check"]["owner_narrowing"]
    assert set(wire["unattributed"]) == {LEGACY_REPO_DOCS, LEGACY_DEFAULT_DOCS, LEGACY_CODE}
    assert set(wire["steps"][0]["unattributed"]) == {LEGACY_REPO_DOCS, LEGACY_DEFAULT_DOCS}
    assert d["traversals"][0]["owner_unattributed"] == [LEGACY_CODE]


def test_the_traversal_summary_carries_the_owner_drops() -> None:
    from nexus.plans.evidence import ScopeConstraint
    from nexus.plans.evidence_diagnostics import build_evidence_diagnostics

    nodes, edges = _graph()
    out = _traverse(nodes, edges, {"allowed_collections": "", "allowed_owners": ["1.1"]})
    d = build_evidence_diagnostics(
        steps=[out], tools=["traverse"], scope=ScopeConstraint.parse(""), reducer_expected=False)
    (summary,) = d["traversals"]
    assert summary["dropped_out_of_owner"] == 3


# ── nx_answer ───────────────────────────────────────────────────────────────


def test_nx_answer_sets_the_owners_binding_only_for_an_explicit_resolved_request() -> None:
    from nexus.mcp.core import _nx_answer_owner_binding

    assert _nx_answer_owner_binding(_request({"owner": "1.1", "source": "RDR-084"}, "rdr")) == {"_nx_owners": "1.1"}
    assert _nx_answer_owner_binding(_request({"source": "RDR-084"}, "rdr")) == {}
    assert _nx_answer_owner_binding(_request({"owner": "1.1"}, "1.2")) == {}, "a conflicting qualifier narrows nothing"
    assert _nx_answer_owner_binding(_request(None, "1.1")) == {}, "a catalog-subtree scope is not an owner binding"
    assert _nx_answer_owner_binding(_request({"source": "RDR-084"}, "1.1.4686")) == {}


def test_a_caller_cannot_inject_the_owners_binding() -> None:
    from nexus.mcp.core import _nx_answer_caller_bindings

    run, available = _nx_answer_caller_bindings(question="q", scope="", bindings={"_nx_owners": "1.9"})
    assert "_nx_owners" not in run and "_nx_owners" not in available
    run2, _ = _nx_answer_caller_bindings(question="q", scope="", bindings={"_nx_scope": "code__other"})
    assert "_nx_scope" not in run2, "the sibling internal binding is nx_answer's own too"


def _drive(match, *, scope: str = "", bindings=None, structured: bool = False, plan_run_mock=None):
    from tests.test_rdr232_caller_scope import _drive_nx_answer

    if plan_run_mock is None:
        return _drive_nx_answer(match, scope=scope, bindings=bindings, structured=structured)
    with patch("nexus.plans.runner.plan_run", plan_run_mock):
        return _drive_nx_answer(match, scope=scope, bindings=bindings, structured=structured)


def test_the_owners_binding_reaches_plan_run_for_an_explicit_owner_and_only_then() -> None:
    from unittest.mock import AsyncMock

    from tests.test_rdr232_caller_scope import MULTI_STEP_PLAN, _match as _cs_match, _plan_run_result

    match = _cs_match(MULTI_STEP_PLAN, default_bindings={"intent": "q"})
    steps = [{"ids": ["c1"], "tumblers": ["1.1.1"]}, {"text": "The decision is X [1] after review of the evidence."}]

    def bindings_seen(call_bindings, scope="rdr") -> dict:
        mock = AsyncMock(return_value=_plan_run_result(list(steps), []))
        _drive(match, scope=scope, bindings=call_bindings, plan_run_mock=mock)
        return dict(mock.await_args.args[1])

    assert bindings_seen({"owner": "1.1"})["_nx_owners"] == "1.1"
    assert bindings_seen({"owner": "1.1", "_nx_owners": "1.9"})["_nx_owners"] == "1.1", "an injected value never wins"
    for call_bindings, scope in (
        (None, "rdr"), (None, "1.1"), ({"owner": "1.1"}, "1.2"), ({"project": "NoSuch"}, ""),
        ({"_nx_owners": "1.9"}, ""),
    ):
        assert "_nx_owners" not in bindings_seen(call_bindings, scope=scope), (call_bindings, scope)


@pytest.mark.parametrize(
    ("plan_corpus", "scope", "queried"),
    [
        pytest.param("knowledge,code,docs,rdr", "rdr", [RDR_ART], id="scope-then-owner"),
        pytest.param("knowledge,code,docs,rdr", "", [f"{KNOW},{CODE_ART},{RDR_ART}"], id="owner-alone-keeps-knowledge"),
        pytest.param(None, "", [f"{KNOW},{CODE_ART},{RDR_ART}"], id="default-corpus-is-narrowed-too"),
        pytest.param("docs", "", [], id="only-foreign-collections-skips-the-query"),
    ],
)
def test_single_query_fast_path_applies_the_owner(plan_corpus, scope, queried) -> None:
    from tests.test_rdr232_caller_scope import _single_query_match

    out, calls = _drive(_single_query_match(plan_corpus), scope=scope, bindings={"owner": "1.1"})
    assert [c["corpus"] for c in calls] == queried
    if not queried:
        assert "No matching evidence" in out and "owned by 1.1" in out and "skipped" in out


def test_single_query_owner_conflict_envelope_discloses_the_skip() -> None:
    from tests.test_rdr232_caller_scope import _single_query_match

    out, calls = _drive(_single_query_match("docs"), bindings={"owner": "1.1"}, structured=True)
    assert calls == [] and out["answer_shape"] == "no_evidence"
    (diag,) = out["scope_diagnostics"]
    assert (diag["scope_kind"], diag["outcome"], diag["removed_collections"]) == ("owner", "conflict", [DOCS_NEXUS])
    d = out["evidence_diagnostics"]
    assert d["skipped_steps"][0]["reason"] == "scope_conflict"
    assert d["scope_check"]["owner_narrowing"]["skipped_steps"] == [0]


def test_a_subtree_scope_alone_narrows_nothing(world) -> None:
    from tests.test_rdr232_caller_scope import _single_query_match

    out, calls = _drive(_single_query_match("knowledge,code,docs,rdr"), scope="1.1", structured=True)
    assert world == [], "no corpus expansion is called for a catalog-subtree scope"
    assert all(d["scope_kind"] != "owner" for d in out["scope_diagnostics"]), out["scope_diagnostics"]
    assert [c["corpus"] for c in calls] == ["knowledge,code,docs,rdr"], "the corpus is the plan's own"
    on_ = out["evidence_diagnostics"]["scope_check"]["owner_narrowing"]
    assert on_["owners"] == [] and on_["steps"] == [] and on_["applied"] is False and on_["not_applied"] is None


def test_an_owner_binding_against_another_subtree_runs_unnarrowed_and_says_so(world) -> None:
    from tests.test_rdr232_caller_scope import _single_query_match

    out, calls = _drive(
        _single_query_match("knowledge,code,docs,rdr"), scope="1.2", bindings={"owner": "1.1"}, structured=True)
    assert world == [], "a conflicting qualifier expands nothing"
    assert all(d["scope_kind"] != "owner" for d in out["scope_diagnostics"])
    on_ = out["evidence_diagnostics"]["scope_check"]["owner_narrowing"]
    assert on_["applied"] is False and on_["not_applied"]["reason"] == "conflict", on_
    assert "owner narrowing was not applied" in on_["not_applied"]["detail"]
    assert out["evidence_request"]["owner"]["status"] == "conflict"


def test_single_query_expands_the_corpus_off_the_event_loop(monkeypatch) -> None:
    from tests.test_rdr232_caller_scope import _single_query_match

    seen: list[int] = []
    real = on.expand_corpus
    monkeypatch.setattr(on, "expand_corpus", lambda corpus: (seen.append(threading.get_ident()), real(corpus))[1])
    _drive(_single_query_match("knowledge,code,docs,rdr"), bindings={"owner": "1.1"})
    assert seen and all(t != threading.get_ident() for t in seen)


def test_single_query_without_an_explicit_owner_is_untouched(world) -> None:
    from tests.test_rdr232_caller_scope import _single_query_match

    out, calls = _drive(_single_query_match("knowledge,code,docs,rdr"), scope="rdr", structured=True)
    assert [c["corpus"] for c in calls] == ["rdr"] and world == []
    assert all(d["scope_kind"] != "owner" for d in out["scope_diagnostics"])
