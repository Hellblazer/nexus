# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.1, .1.3): the self-contained ART RDR-084 fixture on the engine
substrate, the containment expectations for the runner, and the baseline emission.

ONE journey test (tests/AGENTS.md: a scenario is a single self-contained function with
function-scoped fixtures; the tenant ceiling forbids a second one) because the fixture costs about
12 s to write and the claims share it. Its three parts are separate helpers so each can change on
its own:

1. ``_assert_fixture_loads``: two RDR-084 documents under two owners, more than 300 code chunks
   reachable through ``implements``/``relates`` links, written into the test tenant only.
2. The containment expectations, one helper per stream:

   * ``_assert_vector_stream_contained`` (nexus-jypmj.1.3): ``nx_answer(scope="rdr")`` on the
     factual-lookup builtin searches ``rdr`` only. Before RDR-232 the plan's own ``corpus: all``
     beat the caller's scope, and the search returned 40 references, all code (T2
     ``nexus/rdr232-golden-baseline``).
   * ``_assert_graph_stream_not_yet_contained`` (PRE-CHANGE until nexus-jypmj.1.5): ``traverse``
     still hits its 300-id cap and every reference it returns is code. The traversal policy bead
     replaces this helper's assertions with the containment assertions; the RDR-084 regression bead
     (nexus-jypmj.1.8) replaces the whole claim.

   A red in a helper after a later Phase 1 change means exactly that: update the expectation the
   change owns, do not weaken the fixture.
3. ``_emit_baseline``: every golden request's plan selection under the matcher, and the seven
   measures with the later-phase ones reported "not measurable before Phase N". It asserts
   non-vacuity only, never a pre-change value, so it keeps emitting. The report it writes
   (``$NX_RDR232_BASELINE_OUT``) measures THIS tree; the pre-change baseline is the T2 record
   ``nexus/rdr232-golden-baseline`` at its recorded sha and is reproduced by checking that sha out,
   where the test's pin was still the pre-change one.

No model call anywhere: plan selection is the matcher over local ONNX embeddings, and the run is cut
before the first operator step (``continuation_cut_at_step``).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from bench.answer_shape_metrics import (
    MEASURE_NAMES,
    NotMeasurable,
    RunObservation,
    compute_measures,
    measures_to_json,
)
from bench.schema import load_golden_set
from tests import _rdr232_golden as g

ART_REQUEST_QID = "G06-synthesis-art-rdr084"

#: Floor on code chunks reachable from the RDRs: the traverse cap is 300, so the fixture must
#: exceed it for the cap to bite (the incident's mix needed more than 300 reachable code ids).
MIN_CODE_CHUNKS = 301


def _assert_fixture_loads(fx) -> None:
    url = os.environ.get("NX_SERVICE_URL", "")
    assert url.startswith(("http://127.0.0.1", "http://localhost")), (
        f"the fixture must write to the hermetic substrate, not a live service: {url!r}")
    assert fx.art_owner != fx.nexus_owner
    assert fx.art_rdr.startswith(fx.art_owner + ".") and fx.nexus_rdr.startswith(fx.nexus_owner + ".")
    assert fx.code_chunk_count >= MIN_CODE_CHUNKS, fx.code_chunk_count
    assert fx.link_count >= MIN_CODE_CHUNKS // g.PIECES_PER_CODE_DOC

    from nexus.catalog.factory import make_catalog_reader

    reader = make_catalog_reader()
    rdr084 = list(reader.find("RDR-084", content_type="rdr", limit=50))
    assert sorted(str(d.tumbler) for d in rdr084) == sorted([fx.art_rdr, fx.nexus_rdr]), (
        "two RDR-084 documents, one per owner, must be the only RDR-084 candidates")
    assert len(reader.get_manifest(fx.art_rdr)) == len(g.ART_RDR_PIECES)


def _assert_vector_stream_contained(fx, run) -> dict[str, int]:
    """nexus-jypmj.1.3: the caller's scope="rdr" narrows the plan's ``corpus: all`` search. Returns
    the vector stream's reference counts by corpus."""
    assert run.cut_applied, "the run must stop before the first operator step (no model call)"
    assert run.resolved_search_corpus == "rdr", (
        f"scope='rdr' must narrow the builtin's corpus 'all' to 'rdr'; got {run.resolved_search_corpus!r}")
    (search_diag,) = [d for d in run.scope_diagnostics if d["tool"] == "search"]
    assert (search_diag["outcome"], search_diag["plan_corpus"], search_diag["resolved_corpus"]) == (
        "narrowed", "all", "rdr"), search_diag
    assert run.search_ids, "non-vacuity: the narrowed search found the fixture's RDR chunks"
    assert run.search_collections and all(c.startswith("rdr__") for c in run.search_collections), (
        f"the vector stream searched outside rdr: {run.search_collections}")
    search_by_corpus = g.count_by_corpus(g.evidence_from_ids(fx, run.search_ids))
    assert search_by_corpus.get("rdr", 0) == len(set(run.search_ids)) > 0 and "code" not in search_by_corpus, (
        f"every vector-stream reference is an rdr chunk: {search_by_corpus}")
    hydrated = g.count_by_corpus(g.evidence_from_contents(fx, run.vector_contents))
    assert hydrated.get("rdr", 0) > 0 and "code" not in hydrated, (
        f"the vector stream hydrated for the reducer holds rdr chunks only: {hydrated}")
    return search_by_corpus


def _assert_graph_stream_not_yet_contained(fx, run) -> dict[str, int]:
    """PRE-CHANGE until nexus-jypmj.1.5 (traversal evidence policy): traverse is unbounded by scope,
    so it still returns code only and still hits the 300-id cap."""
    traverse_by_corpus = g.count_by_corpus(g.evidence_from_ids(fx, run.traverse_ids))
    assert traverse_by_corpus.get("code", 0) == len(set(run.traverse_ids)) > 0 and "rdr" not in traverse_by_corpus, (
        f"pre-change premise (RDR-232 Gap 3): the graph stream is code only: {traverse_by_corpus}")
    assert run.traverse_ids_truncated and run.traverse_ids_truncated["total"] > 300, (
        f"traverse never hit its 300-id cap: {run.traverse_ids_truncated}")
    return traverse_by_corpus


def _emit_baseline(fx, run, queries, art, search_by_corpus, traverse_by_corpus) -> None:
    refs = g.evidence_from_ids(fx, run.search_ids + run.traverse_ids)
    refs_by_corpus = g.count_by_corpus(refs)
    reducer_input = g.evidence_from_contents(fx, run.vector_contents + run.graph_contents)
    reducer_by_corpus = g.count_by_corpus(reducer_input)
    print(
        f"RDR232-STREAMS vector={search_by_corpus} graph={traverse_by_corpus} "
        f"refs={len(refs)} by_corpus={refs_by_corpus} traverse_truncated={run.traverse_ids_truncated} "
        f"reducer_input={len(reducer_input)} reducer_by_corpus={reducer_by_corpus} "
        f"search_corpus={run.resolved_search_corpus!r}")
    assert any(i.source == g.ART_REF and i.corpus == "rdr" for i in reducer_input), (
        "an ART RDR-084 chunk reaches the reducer's input once the vector stream is contained")

    library = g.seed_builtin_plans()
    observations: list[RunObservation] = []
    selections: dict[str, dict] = {}
    for q in queries:
        sc, bd = g.request_call_shape(q)
        sel = g.observe_plan_selection(library, q.text, scope=sc, bindings=bd)
        selections[q.qid] = {"selected": sel.selected, "candidates": sel.candidates}
        observations.append(RunObservation(
            qid=q.qid, selected_plan=sel.selected, candidate_plans=sel.candidates,
            evidence=refs if q.qid == ART_REQUEST_QID else None,
        ))
    measures = compute_measures(queries, observations)
    assert tuple(measures) == MEASURE_NAMES

    # The ART request, scored on the reducer-input view as well (what the model is handed).
    reducer_measures = compute_measures(
        [art], [RunObservation(qid=ART_REQUEST_QID, evidence=reducer_input)])

    # Non-vacuity of the measures themselves: they scored the fixture run, not an empty set. No
    # value is pinned here, so a later bead that moves a measure does not turn this red.
    for scored in ("primary_source_coverage", "scope_leakage", "facet_evidence_coverage"):
        assert measures[scored].n == 1 and isinstance(measures[scored].value, float), (scored, measures[scored])
    assert measures["eligible_plan_recall"].n == len(queries), (
        "every golden request must have had its candidate plans observed")
    for later in ("repair_yield_and_cost", "terminal_completeness"):
        assert isinstance(measures[later].value, NotMeasurable) and measures[later].value.phase == 3

    # The one measure expectation this bead owns: the vector stream now reaches the ART RDR, so
    # coverage of the named source is no longer zero and leakage is no longer total. Leakage stays
    # high on purpose: the graph stream is contained by nexus-jypmj.1.5.
    assert measures["primary_source_coverage"].value == 1.0
    assert 0.9 < measures["scope_leakage"].value < 1.0, measures["scope_leakage"]
    assert measures["facet_evidence_coverage"].value > 0.0

    out = os.environ.get("NX_RDR232_BASELINE_OUT")
    if out:
        report = {
            "tree_sha": g.head_sha(),
            "tree_dirty": g.tree_is_dirty(),
            "golden_set_version": 1,
            "golden_requests": len(queries),
            "fixture": {
                "art_owner": fx.art_owner, "nexus_owner": fx.nexus_owner,
                "art_rdr": fx.art_rdr, "nexus_rdr": fx.nexus_rdr,
                "code_chunks": fx.code_chunk_count, "links": fx.link_count,
                "rdr_chunks": len(g.ART_RDR_PIECES) + len(g.NEXUS_RDR_PIECES),
            },
            "art_run": {
                "search_ids": len(run.search_ids), "traverse_ids": len(run.traverse_ids),
                "traverse_truncated": run.traverse_ids_truncated,
                "vector_stream_by_corpus": search_by_corpus,
                "graph_stream_by_corpus": traverse_by_corpus,
                "references_by_corpus": refs_by_corpus,
                "reducer_input": len(reducer_input), "reducer_input_by_corpus": reducer_by_corpus,
                "search_corpus_resolved": run.resolved_search_corpus,
                "scope_diagnostics": run.scope_diagnostics,
            },
            "plan_selection": selections,
            "measures": json.loads(measures_to_json(measures)),
            "art_reducer_input_measures": json.loads(measures_to_json(reducer_measures)),
        }
        Path(out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def test_rdr084_fixture_containment_and_baseline_emission(t2_service_env) -> None:
    queries = load_golden_set(g.GOLDEN_PATH)
    art = next(q for q in queries if q.qid == ART_REQUEST_QID)

    fx = g.build_rdr084_fixture()
    _assert_fixture_loads(fx)

    scope, bindings = g.request_call_shape(art)
    run = asyncio.run(g.run_lookup_retrieval(art.text, scope=scope, bindings=bindings))
    search_by_corpus = _assert_vector_stream_contained(fx, run)
    traverse_by_corpus = _assert_graph_stream_not_yet_contained(fx, run)
    raw_references = len(run.search_ids) + len(run.traverse_ids)
    assert raw_references > 300, f"only {raw_references} chunk references; the graph stream's cap is 300"

    _emit_baseline(fx, run, queries, art, search_by_corpus, traverse_by_corpus)
