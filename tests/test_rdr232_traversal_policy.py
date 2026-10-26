# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.5): the traversal evidence policy inside ``traverse``.

The policy runs after the engine walk and BEFORE manifest expansion: out-of-scope documents are
dropped, the rest ranked by link role, then link distance, then tumbler, and capped; manifests are
read for the retained documents only; the secondary chunk budget reserves a share per document;
every returned id carries its collection. A response at the engine's 500-node cap is disclosed as
possibly truncated and counted. With no policy the result is the legacy one.

No test here mints a tenant: ``traverse`` runs against a fake catalog that returns the engine's
reply shape (``{"nodes": [...], "edges": [...]}``), the same pattern ``tests/test_traverse.py``
uses. The one engine-backed claim (the RDR-084 fixture's graph stream) lives in the existing
tenant test ``tests/test_rdr232_golden_fixture.py``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
RDR = "rdr__1-2__bge-base-en-v15-768__v1"
CODE = "code__1-2__bge-base-en-v15-768__v1"
PAPER = "knowledge__papers__bge-base-en-v15-768__v1"
SEED = "1.2.1"


# ── fakes ───────────────────────────────────────────────────────────────────


_TYPE_OF_PREFIX = {"rdr": "rdr", "code": "code", "knowledge": "paper"}


def _node(tumbler: str, collection: str):
    """An engine node: the catalog document row's tumbler, collection and content_type."""
    return SimpleNamespace(
        tumbler=tumbler, physical_collection=collection,
        content_type=_TYPE_OF_PREFIX[collection.split("__")[0]],
    )


def _edge(frm: str, to: str, link_type: str):
    return SimpleNamespace(from_tumbler=frm, to_tumbler=to, link_type=link_type)


_OWNER: dict[str, str] = {}   # chash -> the document whose manifest holds it


def _chash(tumbler: str, j: int) -> str:
    c = f"{tumbler}:{j:03d}".encode().hex().ljust(64, "0")
    _OWNER[c] = tumbler
    return c


def _rows(tumbler: str, n: int):
    return [SimpleNamespace(chash=_chash(tumbler, j)) for j in range(n)]


def _fake_catalog(nodes, edges, per_doc: int | dict = 1):
    """A catalog whose graph is *nodes*/*edges* and whose manifests give each node ``per_doc`` rows."""
    cat = MagicMock()
    reply = {"nodes": nodes, "edges": edges}
    cat.graph.return_value = reply
    cat.graph_many.return_value = reply

    def _manifests(tumblers):
        return {
            t: _rows(t, per_doc[t] if isinstance(per_doc, dict) else per_doc) for t in tumblers
        }

    cat.get_manifests.side_effect = _manifests
    return cat


def _run(cat, **kw):
    from nexus.mcp.core import traverse

    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        return traverse(**kw)


def _policy(scope: str = "rdr", **over) -> dict:
    return {"allowed_collections": scope, **over}


def _owned(prefix: str, n: int, collection: str, link: str = "implements", seed: str = SEED):
    """*n* documents under *prefix*, each linked to *seed*."""
    nodes = [_node(f"{prefix}.{i}", collection) for i in range(n)]
    edges = [_edge(f"{prefix}.{i}", seed, link) for i in range(n)]
    return nodes, edges


# ── ScopeConstraint.admits (the public predicate) ───────────────────────────


def test_admits_follows_the_scope_kind() -> None:
    from nexus.plans.evidence import ScopeConstraint

    parse = ScopeConstraint.parse
    assert parse("").admits(CODE) and parse("all").admits(CODE) and parse(None).admits(RDR)
    rdr = parse("rdr")
    assert rdr.admits(RDR) and not rdr.admits(CODE) and not rdr.admits("rdrx__1-1__m__v1")
    assert not rdr.admits("")
    assert parse(RDR).admits(RDR) and not parse(RDR).admits("rdr__1-3__bge-base-en-v15-768__v1")
    sub = parse("1.2")
    assert sub.kind == "subtree"
    assert sub.admits(CODE, tumbler="1.2.7") and sub.admits(CODE, tumbler="1.2")
    assert not sub.admits(CODE, tumbler="1.20.7") and not sub.admits(CODE, tumbler="1.3.1")
    assert not sub.admits(CODE), "a subtree scope cannot judge a collection alone: it fails closed"


# ── the policy value ────────────────────────────────────────────────────────


def test_policy_wire_round_trip_and_validation() -> None:
    from nexus.plans.traversal_policy import TraversalPolicy, TraversalPolicyError

    p = TraversalPolicy.from_wire({"allowed_collections": "rdr", "max_documents": 7, "require_link_role": "citation"})
    assert (p.allowed_collections.kind, p.allowed_collections.value) == ("corpus_prefix", "rdr")
    assert (p.max_documents, p.require_link_role, p.preserve_seed_documents) == (7, "citation", True)
    assert TraversalPolicy.from_wire(p.to_wire()) == p
    for bad in (
        {"allowed_collections": "rdr", "require_link_role": "gossip"},
        {"allowed_collections": "rdr", "max_documents": 0},
        {"allowed_collections": "rdr", "max_chunks": "many"},
        {"allowed_collections": "rdr", "surprise": 1},
        "rdr",
    ):
        with pytest.raises(TraversalPolicyError):
            TraversalPolicy.from_wire(bad)


# ── scenario 6: no policy, legacy result ────────────────────────────────────


def test_no_policy_result_is_the_legacy_shape_byte_for_byte() -> None:
    nodes = [_node(SEED, RDR), _node("1.2.2", CODE), _node("1.2.3", PAPER)]
    edges = [_edge("1.2.2", SEED, "implements"), _edge("1.2.3", SEED, "cites")]
    out = _run(_fake_catalog(nodes, edges, per_doc=2), seeds=[SEED], link_types=["implements", "cites"])
    assert list(out) == ["tumblers", "ids", "collections"], "the legacy keys, in the legacy order"
    assert out["tumblers"] == [SEED, "1.2.2", "1.2.3"]
    assert out["collections"] == sorted({RDR, CODE, PAPER})
    assert len(out["ids"]) == 6
    assert json.dumps(out)  # serializable as before


def test_a_500_node_response_without_a_policy_is_not_annotated() -> None:
    nodes = [_node(f"1.2.{i}", CODE) for i in range(500)]
    out = _run(_fake_catalog(nodes, []), seeds=["1.2.0"], link_types=["implements"])
    assert "traversal" not in out and "chunk_collections" not in out


# ── scenario 1: more than 300 out-of-scope ids precede in-scope ids ─────────


def test_out_of_scope_ids_before_in_scope_ids_do_not_displace_them() -> None:
    code_nodes, code_edges = _owned("1.2.100", 320, CODE)
    rdr_nodes = [_node(SEED, RDR), _node("1.2.900", RDR), _node("1.2.901", RDR)]
    rdr_edges = [_edge("1.2.900", SEED, "relates"), _edge("1.2.901", SEED, "relates")]
    nodes = code_nodes + rdr_nodes          # the engine walk lists the code documents first
    edges = code_edges + rdr_edges

    legacy = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["implements", "relates"])
    assert legacy["ids_truncated"]["total"] == 323 and len(legacy["ids"]) == 300
    in_scope_ids = {_chash(t, 0) for t in (SEED, "1.2.900", "1.2.901")}
    assert not in_scope_ids & set(legacy["ids"]), (
        "premise: with no policy the cap cut every in-scope id (Gap 3 of RDR-232)")

    cat = _fake_catalog(nodes, edges)
    out = _run(cat, seeds=[SEED], link_types=["implements", "relates"], policy=_policy("rdr"))
    assert set(out["ids"]) == in_scope_ids
    assert out["tumblers"] == [SEED, "1.2.900", "1.2.901"]
    assert out["collections"] == [RDR]
    assert out["chunk_collections"] == [RDR] * len(out["ids"]), "one collection per id, aligned"
    (asked,) = (c.args[0] for c in cat.get_manifests.call_args_list)
    assert sorted(asked) == sorted(out["tumblers"]), "manifests are read for the retained documents only"
    rep = out["traversal"]
    assert (rep["nodes_walked"], rep["nodes_retained"], rep["dropped_out_of_scope"]) == (323, 3, 320)
    assert rep["corpus_counts"] == {"rdr": 3} and rep["dropped_by_corpus"] == {"code": 320}
    assert rep["possibly_truncated"] is False


def test_alignment_follows_each_ids_own_document_collection() -> None:
    nodes = [_node(SEED, RDR), _node("1.2.2", PAPER), _node("1.2.3", CODE)]
    edges = [_edge("1.2.2", SEED, "cites"), _edge("1.2.3", SEED, "implements")]
    out = _run(_fake_catalog(nodes, edges, per_doc=2), seeds=[SEED], link_types=["cites", "implements"],
               policy=_policy("all"))
    assert len(out["ids"]) == len(out["chunk_collections"]) == 6
    by_doc = {t: c for t, c in ((SEED, RDR), ("1.2.2", PAPER), ("1.2.3", CODE))}
    expect = [by_doc[t] for t in out["tumblers"] for _ in range(2)]
    assert out["chunk_collections"] == expect


# ── scenario 2 / 7: the 500-node cap ────────────────────────────────────────


def test_the_client_cap_equals_the_engine_constant() -> None:
    from nexus.plans.traversal_policy import MAX_GRAPH_NODES

    java = (REPO / "service/src/main/java/dev/nexus/service/db/CatalogRepository.java").read_text()
    (declared,) = re.findall(r"private static final int MAX_GRAPH_NODES = (\d+);", java)
    assert MAX_GRAPH_NODES == int(declared) == 500


@pytest.mark.parametrize("count,capped", [(499, False), (500, True), (501, True)])
def test_a_response_at_the_cap_is_possibly_truncated_never_contained(count: int, capped: bool) -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", RDR) for i in range(count - 1)]
    edges = [_edge(n.tumbler, SEED, "relates") for n in nodes[1:]]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    rep = out["traversal"]
    assert rep["possibly_truncated"] is capped
    assert rep["contained"] is (not capped)
    assert rep["nodes_walked"] == count


def test_capped_responses_are_counted_for_the_diagnostics() -> None:
    from nexus.plans.traversal_policy import capped_traversal_count

    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", RDR) for i in range(499)]
    edges = [_edge(n.tumbler, SEED, "relates") for n in nodes[1:]]
    capped = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    small = _run(_fake_catalog(nodes[:3], edges[:2]), seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    legacy = _run(_fake_catalog(nodes[:3], edges[:2]), seeds=[SEED], link_types=["relates"])
    assert capped_traversal_count([capped, small, legacy, {}, "not a dict"]) == 1
    assert capped_traversal_count([]) == 0


def test_the_cap_is_logged_as_an_event_the_window_count_can_read() -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", RDR) for i in range(499)]
    edges = [_edge(n.tumbler, SEED, "relates") for n in nodes[1:]]
    with patch("nexus.mcp.core._log") as log:
        _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    events = [c.args[0] for c in log.warning.call_args_list]
    assert "traverse_graph_node_cap" in events


def test_a_dead_seed_after_the_engine_cut_still_reads_as_possibly_truncated() -> None:
    """The engine slices to 500 nodes, then drops tombstoned seeds. A reply of 499 live nodes plus
    one seed that is missing could have been a 500-node walk: every non-seed node in a reply is
    live, so only a missing seed can hide a cut."""
    dead = "1.2.2"
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", RDR) for i in range(498)]
    edges = [_edge(n.tumbler, SEED, "relates") for n in nodes[1:]]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED, dead], link_types=["relates"], policy=_policy("rdr"))
    rep = out["traversal"]
    assert rep["nodes_walked"] == 499
    assert rep["possibly_truncated"] is True and rep["contained"] is False
    # the same reply without the missing seed is a complete walk
    again = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert again["traversal"]["possibly_truncated"] is False


def test_two_dead_seeds_and_a_short_reply_stay_contained_below_the_cap() -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", RDR) for i in range(10)]
    edges = [_edge(n.tumbler, SEED, "relates") for n in nodes[1:]]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED, "1.2.2", "1.2.3"], link_types=["relates"], policy=_policy("rdr"))
    assert out["traversal"]["possibly_truncated"] is False


# ── scenario 3: a named RDR under scope=rdr admits no code ──────────────────


@pytest.mark.parametrize("link", ["implements", "relates", "implements-heuristic"])
def test_scope_rdr_admits_no_code_through_implements_or_relates(link: str) -> None:
    code_nodes, code_edges = _owned("1.2.50", 12, CODE, link=link)
    nodes = [_node(SEED, RDR)] + code_nodes
    out = _run(_fake_catalog(nodes, code_edges), seeds=[SEED], link_types=[link], policy=_policy("rdr"))
    assert out["tumblers"] == [SEED]
    assert set(out["chunk_collections"]) == {RDR}
    assert out["traversal"]["dropped_out_of_scope"] == 12


# ── ranking, the document cap, seeds, the chunk budget ──────────────────────


def test_documents_rank_by_role_then_distance_then_tumbler() -> None:
    nodes = [_node(t, RDR) for t in (SEED, "1.2.9", "1.2.4", "1.2.7", "1.2.5", "1.2.6", "1.2.8")]
    edges = [
        _edge("1.2.9", SEED, "relates"),                 # related, distance 1
        _edge("1.2.4", SEED, "cites"),                   # citation, distance 1
        _edge("1.2.7", SEED, "implements"),              # implementation, distance 1
        _edge("1.2.5", "1.2.4", "cites"),                # citation, distance 2
        _edge("1.2.6", SEED, "cites"),                   # citation, distance 1 (ties 1.2.4 on role+distance)
        _edge("1.2.8", SEED, "relates"),                 # related, distance 1 (ties 1.2.9)
    ]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["cites", "implements", "relates"],
               depth=2, direction="in", policy=_policy("all", max_documents=10))
    assert out["tumblers"] == [SEED, "1.2.4", "1.2.6", "1.2.5", "1.2.7", "1.2.8", "1.2.9"]


def test_max_documents_caps_secondary_documents_and_never_evicts_a_seed() -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", PAPER) for i in range(8)]
    edges = [_edge(f"1.2.{i + 10}", SEED, "cites") for i in range(8)]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["cites"],
               policy=_policy("all", max_documents=3))
    assert out["tumblers"] == [SEED, "1.2.10", "1.2.11", "1.2.12"]
    assert out["traversal"]["dropped_cap"] == 5


def test_without_seed_preservation_a_seed_is_an_ordinary_candidate_that_ranks_first() -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", PAPER) for i in range(4)]
    edges = [_edge(f"1.2.{i + 10}", SEED, "cites") for i in range(4)]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["cites"],
               policy=_policy("all", max_documents=2, preserve_seed_documents=False))
    assert out["tumblers"] == [SEED, "1.2.10"], "the seed ranks first and spends one of the two slots"
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["cites"],
               policy=_policy("paper_nothing", max_documents=2, preserve_seed_documents=False))
    assert out["tumblers"] == [], "an out-of-scope seed is dropped either way: the scope wins"


def test_chunk_budget_reserves_a_share_per_document() -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.{i + 10}", PAPER) for i in range(3)]
    edges = [_edge(f"1.2.{i + 10}", SEED, "cites") for i in range(3)]
    out = _run(_fake_catalog(nodes, edges, per_doc=200), seeds=[SEED], link_types=["cites"],
               policy=_policy("all", max_chunks=40, max_documents=10))
    assert len(out["ids"]) == 40
    assert out["ids_truncated"] == {"total": 800, "kept": 40}
    per_doc = {t: 0 for t in out["tumblers"]}
    for cid in out["ids"]:
        per_doc[_OWNER[cid]] += 1
    assert min(per_doc.values()) >= 10, f"every document keeps its reservation (40 // 4): {per_doc}"
    assert out["chunk_collections"] == [
        RDR if i < per_doc[SEED] else PAPER for i in range(40)
    ], "ids are grouped by document in rank order"


def test_a_document_under_its_reservation_leaves_the_rest_to_the_others() -> None:
    nodes = [_node(SEED, RDR), _node("1.2.10", PAPER), _node("1.2.11", PAPER)]
    edges = [_edge("1.2.10", SEED, "cites"), _edge("1.2.11", SEED, "cites")]
    out = _run(_fake_catalog(nodes, edges, per_doc={SEED: 2, "1.2.10": 2, "1.2.11": 50}), seeds=[SEED],
               link_types=["cites"], policy=_policy("all", max_chunks=30))
    assert len(out["ids"]) == 30
    assert sum(1 for c in out["chunk_collections"] if c == RDR) == 2


# ── scenario 4: bounded graph expansion still returns the linked evidence ───


def test_which_paper_reported_value_x_still_gets_its_linked_evidence() -> None:
    nodes = [_node(SEED, PAPER)] + [_node(f"1.2.{i + 10}", PAPER) for i in range(5)]
    edges = [_edge(f"1.2.{i + 10}", SEED, "cites") for i in range(5)]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], purpose="reference-chain", direction="in",
               policy=_policy("knowledge", max_documents=3))
    assert out["tumblers"] == [SEED, "1.2.10", "1.2.11", "1.2.12"]
    assert out["ids"] and set(out["chunk_collections"]) == {PAPER}


# ── scenario 5: widening only without a scope; a prefix scope conflicts ─────


def test_without_a_scope_code_expansion_is_admitted_and_recorded_as_widened() -> None:
    code_nodes, code_edges = _owned("1.2.50", 4, CODE)
    nodes = [_node(SEED, RDR)] + code_nodes
    out = _run(_fake_catalog(nodes, code_edges), seeds=[SEED], purpose="find-implementations", direction="in",
               policy=_policy("", require_link_role="implementation"))
    assert out["tumblers"][0] == SEED and len(out["tumblers"]) == 5
    rep = out["traversal"]
    assert rep["scope_conflict"] is None
    assert rep["widened"]["corpus_counts"] == {"rdr": 1, "code": 4}
    assert "implementation" in rep["widened"]["reason"] and "no caller scope" in rep["widened"]["reason"]


def test_with_scope_rdr_a_current_code_expansion_is_a_conflict_not_a_widening() -> None:
    code_nodes, code_edges = _owned("1.2.50", 4, CODE)
    nodes = [_node(SEED, RDR)] + code_nodes
    out = _run(_fake_catalog(nodes, code_edges), seeds=[SEED], purpose="find-implementations", direction="in",
               policy=_policy("rdr", require_link_role="implementation"))
    assert out["tumblers"] == [SEED], "the seed stays in its own stream; no code is admitted"
    rep = out["traversal"]
    assert rep["widened"] is None
    assert rep["scope_conflict"] == {
        "caller_scope": "rdr", "scope_kind": "corpus_prefix", "link_role": "implementation",
        "dropped_by_corpus": {"code": 4},
    }


def test_an_unscoped_expansion_that_stays_in_the_seed_corpus_is_not_widened() -> None:
    nodes = [_node(SEED, RDR), _node("1.2.2", RDR)]
    out = _run(_fake_catalog(nodes, [_edge("1.2.2", SEED, "relates")]), seeds=[SEED], link_types=["relates"],
               policy=_policy(""))
    assert out["traversal"]["widened"] is None


def test_require_link_role_admits_only_documents_reached_by_that_role() -> None:
    nodes = [_node(SEED, RDR), _node("1.2.2", RDR), _node("1.2.3", RDR), _node("1.2.4", RDR)]
    edges = [_edge("1.2.2", SEED, "cites"), _edge("1.2.3", SEED, "relates"), _edge("1.2.4", SEED, "implements")]
    cat = lambda: _fake_catalog(nodes, edges)  # noqa: E731
    cites = _run(cat(), seeds=[SEED], link_types=["cites", "relates", "implements"],
                 policy=_policy("", require_link_role="citation"))
    assert cites["tumblers"] == [SEED, "1.2.2"] and cites["traversal"]["dropped_role"] == 2
    anything = _run(cat(), seeds=[SEED], link_types=["cites", "relates", "implements"], policy=_policy(""))
    assert anything["tumblers"] == [SEED, "1.2.2", "1.2.4", "1.2.3"] and anything["traversal"]["dropped_role"] == 0
    related = _run(cat(), seeds=[SEED], link_types=["cites", "relates", "implements"],
                   policy=_policy("", require_link_role="related"))
    assert related["tumblers"] == [SEED, "1.2.3"]


def test_a_scoped_expansion_is_never_recorded_as_widened() -> None:
    """A catalog-subtree scope admits a different content type than the seeds' (code under 1.2),
    but the caller gave a scope, so nothing was widened by the policy."""
    code_nodes, code_edges = _owned("1.2.50", 3, CODE)
    nodes = [_node(SEED, RDR)] + code_nodes
    out = _run(_fake_catalog(nodes, code_edges), seeds=[SEED], link_types=["implements"], policy=_policy("1.2"))
    assert len(out["tumblers"]) == 4, "the subtree scope admits the code documents"
    assert out["traversal"]["widened"] is None
    unscoped = _run(_fake_catalog(nodes, code_edges), seeds=[SEED], link_types=["implements"], policy=_policy(""))
    assert unscoped["traversal"]["widened"] is not None


@pytest.mark.parametrize("role", ["factual", "citation", "related"])
def test_a_scope_conflict_is_only_for_an_implementation_role_step(role: str) -> None:
    code_nodes, code_edges = _owned("1.2.50", 4, CODE, link="cites")
    nodes = [_node(SEED, RDR)] + code_nodes
    out = _run(_fake_catalog(nodes, code_edges), seeds=[SEED], link_types=["cites"],
               policy=_policy("rdr", require_link_role=role))
    assert out["traversal"]["dropped_out_of_scope"] == 4
    assert out["traversal"]["scope_conflict"] is None


# ── a policy failure keeps the rest and omits the graph stream ──────────────


@pytest.mark.parametrize("bad", [{"allowed_collections": "rdr", "require_link_role": "gossip"}, "rdr", {"x": 1}])
def test_an_invalid_policy_omits_the_graph_stream_and_never_walks_unrestricted(bad) -> None:
    cat = _fake_catalog([_node(SEED, RDR), _node("1.2.2", CODE)], [_edge("1.2.2", SEED, "implements")])
    from nexus.mcp._tool_failure import is_failure

    out = _run(cat, seeds=[SEED], link_types=["implements"], policy=bad)
    assert out["tumblers"] == [] and out["ids"] == [] and out["collections"] == []
    assert out["traversal"]["policy_failed"] is True and "traverse: traversal policy failed" in out["warning"]
    assert not is_failure(out), "the primary and vector streams continue: this is a warning, not a failed tool"
    assert not cat.graph.called and not cat.graph_many.called and not cat.get_manifests.called


def test_a_failure_inside_the_policy_after_the_walk_also_omits_the_graph_stream() -> None:
    nodes = [_node(SEED, RDR), _node("1.2.2", CODE)]
    cat = _fake_catalog(nodes, [_edge("1.2.2", SEED, "implements")])
    with patch("nexus.plans.traversal_policy.select_documents", side_effect=RuntimeError("boom")):
        out = _run(cat, seeds=[SEED], link_types=["implements"], policy=_policy("rdr"))
    assert out["tumblers"] == [] and out["ids"] == []
    assert out["traversal"]["policy_failed"] is True and "RuntimeError: boom" in out["warning"]
    assert not cat.get_manifests.called, "no unrestricted manifest expansion after a policy failure"


# ── the runner derives and passes the policy (item 9) ───────────────────────


def _plan_with_traverse(**args) -> dict:
    return {"steps": [
        {"tool": "search", "args": {"query": "x", "corpus": "all"}},
        {"tool": "traverse", "args": {"seeds": "$step1.tumblers", **args}},
    ]}


def _drive(plan: dict, bindings: dict) -> list[tuple[str, dict]]:
    import asyncio

    from nexus.plans.match import Match
    from nexus.plans.runner import plan_run

    seen: list[tuple[str, dict]] = []

    def dispatcher(tool: str, args: dict) -> dict:
        seen.append((tool, dict(args)))
        if tool == "search":
            return {"ids": ["a"], "tumblers": [SEED], "collections": [RDR], "distances": [0.1]}
        return {"tumblers": [], "ids": [], "collections": []}

    match = Match(
        plan_id=1, name="t", description="t", confidence=0.9, dimensions={}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[], default_bindings={},
        parent_dims=None,
    )
    asyncio.run(plan_run(match, bindings, dispatcher=dispatcher))
    return seen


def test_the_runner_gives_every_traverse_step_a_policy_from_the_caller_scope() -> None:
    seen = _drive(_plan_with_traverse(purpose="factual-evidence", depth=1), {"_nx_scope": "rdr"})
    (policy,) = [a["policy"] for t, a in seen if t == "traverse"]
    assert policy["allowed_collections"] == "rdr" and policy["require_link_role"] == "factual"
    assert policy["preserve_seed_documents"] is True


def test_the_runner_policy_without_a_scope_has_no_binding_document_cap() -> None:
    from nexus.plans.traversal_policy import DEFAULT_MAX_DOCUMENTS, MAX_GRAPH_NODES

    for bindings in ({}, {"_nx_scope": "all"}, {"_nx_scope": ""}):
        seen = _drive(_plan_with_traverse(purpose="factual-evidence"), bindings)
        (policy,) = [a["policy"] for t, a in seen if t == "traverse"]
        assert policy["allowed_collections"] in ("", "all")
        assert policy["max_documents"] >= MAX_GRAPH_NODES > DEFAULT_MAX_DOCUMENTS, bindings
        assert policy["max_chunks"] > 0


def test_an_unscoped_runner_traverse_does_not_drop_documents_to_a_cap() -> None:
    """120 linked documents, no caller scope: all of them survive the policy (the engine's own
    500-node cap is the only bound). An uncalibrated 50-document cut would silently change
    what every unscoped traverse builtin returns (RDR-232 2a: unscoped is unchanged)."""
    nodes, edges = _owned("1.2.50", 120, RDR, link="relates")
    nodes = [_node(SEED, RDR)] + nodes
    seen = _drive(_plan_with_traverse(purpose="factual-evidence"), {})
    (policy,) = [a["policy"] for t, a in seen if t == "traverse"]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["relates"], policy=policy)
    assert len(out["tumblers"]) == 121
    assert out["traversal"]["dropped_cap"] == 0 and out["traversal"]["nodes_retained"] == 121


def test_an_unscoped_runner_traverse_still_ranks_by_role() -> None:
    nodes = [_node(t, RDR) for t in (SEED, "1.2.4", "1.2.9")]
    edges = [_edge("1.2.4", SEED, "relates"), _edge("1.2.9", SEED, "cites")]
    seen = _drive(_plan_with_traverse(purpose="factual-evidence"), {})
    (policy,) = [a["policy"] for t, a in seen if t == "traverse"]
    out = _run(_fake_catalog(nodes, edges), seeds=[SEED], link_types=["cites", "relates"], policy=policy)
    assert out["tumblers"] == [SEED, "1.2.9", "1.2.4"], "citation outranks related despite the later tumbler"


def test_a_scoped_runner_traverse_keeps_the_default_document_cap() -> None:
    from nexus.plans.traversal_policy import DEFAULT_MAX_DOCUMENTS

    seen = _drive(_plan_with_traverse(purpose="factual-evidence"), {"_nx_scope": "rdr"})
    (policy,) = [a["policy"] for t, a in seen if t == "traverse"]
    assert policy["max_documents"] == DEFAULT_MAX_DOCUMENTS


def test_the_runner_names_the_implementation_role_for_an_implementation_only_step() -> None:
    seen = _drive(_plan_with_traverse(purpose="find-implementations"), {"_nx_scope": "rdr"})
    (policy,) = [a["policy"] for t, a in seen if t == "traverse"]
    assert policy["require_link_role"] == "implementation"


def test_a_plan_declared_policy_is_not_trusted_over_the_callers_scope() -> None:
    loose = {"allowed_collections": "all", "max_documents": 9999}
    seen = _drive(_plan_with_traverse(purpose="factual-evidence", policy=loose), {"_nx_scope": "rdr"})
    (policy,) = [a["policy"] for t, a in seen if t == "traverse"]
    assert policy["allowed_collections"] == "rdr" and policy["max_documents"] < 9999


def test_a_plan_declared_policy_may_tighten_never_widen() -> None:
    from nexus.plans.traversal_policy import DEFAULT_MAX_DOCUMENTS, derive_policy

    step = {"purpose": "factual-evidence"}
    base = derive_policy(step, "rdr")
    assert base["max_documents"] == DEFAULT_MAX_DOCUMENTS and base["require_link_role"] == "factual"

    tight = derive_policy({**step, "policy": {"max_documents": 5, "max_chunks": 40, "require_link_role": "citation"}}, "rdr")
    assert (tight["max_documents"], tight["max_chunks"], tight["require_link_role"]) == (5, 40, "citation")
    assert tight["allowed_collections"] == "rdr", "the scope is never the plan's to change"

    wide = derive_policy({**step, "policy": {"max_documents": 9999, "max_chunks": 99999, "allowed_collections": "all"}}, "rdr")
    assert wide == base

    # the derived role is already specific: a different declared role cannot widen or swap it
    impl = derive_policy({"purpose": "find-implementations", "policy": {"require_link_role": "citation"}}, "rdr")
    assert impl["require_link_role"] == "implementation"
    # junk is ignored, never raised and never loosening
    junk = derive_policy({**step, "policy": {"max_documents": True, "max_chunks": -3, "require_link_role": "gossip"}}, "rdr")
    assert junk == base
    assert derive_policy({**step, "policy": "all"}, "rdr") == base


def test_a_plan_declared_cap_tightens_an_unscoped_run_too() -> None:
    from nexus.plans.traversal_policy import derive_policy

    out = derive_policy({"purpose": "factual-evidence", "policy": {"max_documents": 7}}, None)
    assert out["max_documents"] == 7 and out["allowed_collections"] == ""


# ── derive_policy input shapes ──────────────────────────────────────────────


def test_derive_policy_does_not_split_a_bare_string_link_type_into_characters() -> None:
    from nexus.plans.traversal_policy import derive_policy

    assert derive_policy({"link_types": "implements"}, "rdr")["require_link_role"] == "implementation"
    assert derive_policy({"link_types": ["cites"]}, "rdr")["require_link_role"] == "citation"
    assert derive_policy({"link_types": ("implements",)}, "rdr")["require_link_role"] == "implementation"


def test_a_list_scope_is_never_stringified_into_a_prefix_that_admits_nothing() -> None:
    from nexus.plans.traversal_policy import TraversalPolicy, TraversalPolicyError, derive_policy

    assert derive_policy({"purpose": "factual-evidence"}, ["rdr"])["allowed_collections"] == "rdr"
    assert derive_policy({"purpose": "factual-evidence"}, [])["allowed_collections"] == ""
    many = derive_policy({"purpose": "factual-evidence"}, ["rdr", "docs"])
    assert many["allowed_collections"] == ["rdr", "docs"], "kept as given, so the policy parse refuses it"
    with pytest.raises(TraversalPolicyError):
        TraversalPolicy.from_wire(many)
    with pytest.raises(TraversalPolicyError):
        TraversalPolicy.from_wire({"allowed_collections": ["rdr"]})
    # a scope the policy cannot express omits the graph stream with a warning, as any policy failure does
    cat = _fake_catalog([_node(SEED, RDR), _node("1.2.2", RDR)], [_edge("1.2.2", SEED, "relates")])
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=many)
    assert out["tumblers"] == [] and out["traversal"]["policy_failed"] is True


def test_other_tools_are_untouched_by_the_policy_hook() -> None:
    seen = _drive(_plan_with_traverse(purpose="factual-evidence"), {"_nx_scope": "rdr"})
    (search_args,) = [a for t, a in seen if t == "search"]
    assert "policy" not in search_args
