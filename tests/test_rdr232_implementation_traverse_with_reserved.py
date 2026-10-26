# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.12, from the 1.11 validation): an implementation-role traverse
beside the reserved primary stream, at the ``nx_answer`` level.

The 1.5 Test Plan scenario 5 says the named source (RDR-084) "stays the reserved primary stream" both
when the traversal is widened (no caller scope: the implementation links admit code) and when it
conflicts (``scope="rdr"``: the code expansion is dropped and disclosed). Until now only the traverse
seed staying in its own stream was asserted (``test_rdr232_traversal_policy.py``). This runs the real
``nx_answer`` and the real ``plan_run`` (search, a ``find-implementations`` traverse, summarize) over a
fake catalog, with the named source resolved and its primary chunks read, and checks what the reducer
was handed and what the diagnostics say, in both cases.

Tenant-free: the catalog, the vector search and the operator are fakes (the same seams as
``test_rdr232_evidence_diagnostics.py``); no tenant is minted and the storage-touch ceiling is untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from nexus.mcp import core as mcp_core
from nexus.plans.match import Match
from nexus.plans.primary_evidence import PrimaryEvidence
from nexus.plans.primary_evidence import DocumentCoverage
from tests.test_rdr232_evidence_diagnostics import RDR_COLL, _chunk
from tests.test_rdr232_reserved_merge import _resolution_for_rdr084
from tests.test_rdr232_traversal_policy import CODE, RDR, SEED, _edge, _node

N_PRIMARY = 3
NAMED_DOC = "1.2.3"
N_CODE = 4
_ANSWER = "The decision is X [1]. It was accepted on 2026-10-01 after review of the evidence."


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text_of(tumbler: str) -> str:
    return f"chunk of document {tumbler}"


def _catalog():
    """The seed (an RDR document) with N_CODE code documents linked to it by ``implements``. Every
    manifest row's chash is the hash of its text, so the diagnostics attribute it by content."""
    code = [f"1.2.50.{i}" for i in range(N_CODE)]
    nodes = [_node(SEED, RDR)] + [_node(t, CODE) for t in code]
    edges = [_edge(t, SEED, "implements") for t in code]
    cat = MagicMock()
    reply = {"nodes": nodes, "edges": edges}
    cat.graph.return_value = reply
    cat.graph_many.return_value = reply
    cat.get_manifests.side_effect = lambda tumblers: {
        t: [MagicMock(chash=_h(_text_of(t)))] for t in tumblers}
    return cat


def _match() -> Match:
    plan = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "rdr"}},
        {"tool": "traverse", "args": {"seeds": "$step1.tumblers", "purpose": "find-implementations",
                                      "direction": "in", "depth": 1}},
        {"tool": "summarize", "args": {"ids": "$step2.ids", "collections": "$step2.collections"}},
    ]}
    return Match(
        plan_id=7, name="impl", description="impl", confidence=0.9, dimensions={}, tags="",
        plan_json=json.dumps(plan), required_bindings=["intent"], optional_bindings=[],
        default_bindings={"intent": "q"}, parent_dims=None)


def _run_nx_answer(scope: str) -> tuple[dict, str]:
    """``nx_answer`` with RDR-084 named; returns the envelope and the texts the summarize operator got."""
    handed: list[str] = []   # the one ``content`` string summarize was handed
    texts_by_id: dict[str, str] = {}

    async def stub_search(**kwargs):  # noqa: ARG001
        t = _text_of(SEED)
        texts_by_id[_h(t)] = t
        return {"ids": [_h(t)], "tumblers": [SEED], "collections": [RDR], "chunk_collections": [RDR],
                "chunk_text_hash": [_h(t)], "distances": [0.1]}

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [texts_by_id[i] for i in ids]}

    async def stub_summarize(**kwargs):
        raw = kwargs.get("content", "")
        handed.append(raw if isinstance(raw, str) else "\n".join(raw))
        return {"summary": _ANSWER}

    cat = _catalog()
    for tumbler in [SEED] + [n.tumbler for n in cat.graph.return_value["nodes"][1:]]:
        texts_by_id[_h(_text_of(tumbler))] = _text_of(tumbler)
    # the resolution (_resolution_for_rdr084) names document 1.2.3; the primary chunks belong to it
    evidence = PrimaryEvidence(
        items=tuple(_chunk(i, doc=NAMED_DOC) for i in range(N_PRIMARY)),
        documents=(DocumentCoverage(source="RDR-084", doc_id=NAMED_DOC, title="RDR-084", status="ok",
                                    manifest_rows=N_PRIMARY, selected_rows=N_PRIMARY, partial=False),))
    db_stub = MagicMock(plans=MagicMock())
    db_stub.conn = MagicMock()
    with patch("nexus.plans.matcher.plan_match", return_value=[_match()]), \
         patch("nexus.mcp.core._get_catalog", return_value=cat), \
         patch("nexus.mcp.core._nx_answer_named_sources", return_value=_resolution_for_rdr084()), \
         patch("nexus.plans.primary_evidence.select_primary_evidence", MagicMock(return_value=evidence)), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=lambda c, h: {}), \
         patch("nexus.mcp.core._get_t3", return_value=MagicMock()), \
         patch("nexus.mcp.core._t2_ctx") as ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         patch.object(mcp_core, "search", stub_search), patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_summarize", stub_summarize):
        ctx.return_value.__enter__.return_value = db_stub
        env = asyncio.run(mcp_core.nx_answer(
            question="how is RDR-084 implemented", scope=scope, structured=True,
            bindings={"source": "RDR-084"}))
    assert len(handed) == 1, "summarize ran once"
    return env, handed[0]


def _primary_texts() -> list[str]:
    return [f"PRIMARY TEXT {i}" for i in range(N_PRIMARY)]


def _assert_rdr084_is_still_the_reserved_primary_stream(env: dict, handed: str) -> dict:
    """The claims both cases share: the primary chunks lead the reducer input, all of them, from the
    named source's collection, and the diagnostics call the source covered."""
    assert env["answer_shape"] == "answered", env["final_text"][:300]
    assert env["reserved_stream"]["merged"] is True and env["reserved_stream"]["reserved_items"] == N_PRIMARY
    at = [handed.find(t) for t in _primary_texts()]
    assert min(at) >= 0 and at == sorted(at), "the primary chunks lead the summarize input, in order"
    assert all(handed.count(t) == 1 for t in _primary_texts()), "the traverse did not re-add the primary chunks"
    assert handed.find(_text_of(SEED)) > at[-1], "the traverse seed's own chunk follows the primary chunks"
    d = env["evidence_diagnostics"]
    r = d["reducer_input"]
    assert r["by_stream"]["primary"] == N_PRIMARY and r["unattributed"] == 0, r
    assert r["by_stream_collection"]["primary"] == {RDR_COLL: N_PRIMARY}
    assert d["source_check"]["state"] == "covered", d["source_check"]
    assert d["streams"]["primary"]["references"] == N_PRIMARY
    (trav,) = d["traversals"]
    assert d["streams"]["graph"]["references"] > 0, "the traversal contributed to the graph stream, beside the primary one"
    return d | {"_traversal": trav}


def test_widened_traversal_admits_code_and_rdr084_stays_the_reserved_primary_stream() -> None:
    env, handed = _run_nx_answer(scope="")
    d = _assert_rdr084_is_still_the_reserved_primary_stream(env, handed)
    trav = d["_traversal"]
    assert trav["widened"] is True and trav["scope_conflict"] is None, trav
    (reason,) = [x for x in d["scope_check"]["recorded_reasons"] if x["reason"] == "traversal_widened"]
    assert "implementation" in reason["detail"] and "no caller scope" in reason["detail"], reason
    code_texts = [_text_of(f"1.2.50.{i}") for i in range(N_CODE)]
    assert all(t in handed for t in code_texts), "no caller scope: the implementing code reaches the reducer"
    assert d["scope_check"]["state"] == "not_applicable"
    r = d["reducer_input"]
    # the seed's own chunk is the vector stream's (first stream in plan order); the graph stream adds the code
    assert r["by_stream"] == {"primary": N_PRIMARY, "vector": 1, "graph": N_CODE}, r
    assert r["by_stream_collection"]["graph"] == {CODE: N_CODE}, r


def test_conflicting_traversal_drops_code_and_rdr084_stays_the_reserved_primary_stream() -> None:
    env, handed = _run_nx_answer(scope="rdr")
    d = _assert_rdr084_is_still_the_reserved_primary_stream(env, handed)
    trav = d["_traversal"]
    assert trav["widened"] is False
    conflict = {"caller_scope": "rdr", "scope_kind": "corpus_prefix", "link_role": "implementation",
                "dropped_by_corpus": {"code": N_CODE}}
    assert trav["scope_conflict"] == conflict, trav
    assert d["scope_check"]["conflict_traversals"] == [{"step_index": 1, **conflict}]
    assert all(_text_of(f"1.2.50.{i}") not in handed for i in range(N_CODE)), "scope rdr: no code reaches the reducer"
    assert d["scope_check"]["state"] == "conflict", d["scope_check"]
    r = d["reducer_input"]
    assert r["out_of_scope"] == 0 and r["by_stream"] == {"primary": N_PRIMARY, "vector": 1}, r
    assert not any(c.startswith("code__") for cols in r["by_stream_collection"].values() for c in cols), r
    assert d["source_check"]["state"] == "covered"
