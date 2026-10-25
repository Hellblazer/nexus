# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.1): the self-contained ART RDR-084 fixture on the engine
substrate, the non-vacuity assertion for the unmodified runner, and the pre-change baseline.

ONE journey test (tests/AGENTS.md: a scenario is a single self-contained function with
function-scoped fixtures) because the fixture costs about 12 s to write and the three claims share
it:

1. The fixture loads: two RDR-084 documents under two owners, more than 300 code chunks reachable
   through ``implements``/``relates`` links, written into the test tenant only (no live corpus).
2. NON-VACUITY: the UNMODIFIED ``plan_run`` running the factual-lookup builtin with the recorded
   call shape (``scope="rdr"``, ``content_type`` binding) gathers a code-dominated evidence set:
   its search ignores the caller scope (``corpus: all``), traverse hits its 300-id cap, and no ART
   RDR-084 chunk reaches the reducer's input. This is the 2026-10-23 incident's mix (340
   references, 339 code, 1 RDR).
3. The baseline: every golden request's plan selection under the unmodified matcher, and the seven
   measures with the later-phase ones reported "not measurable before Phase N". Written to
   ``$NX_RDR232_BASELINE_OUT`` when set (that is how the T2 baseline is produced).

PRE-CHANGE CHARACTERIZATION. Claim 2 pins what the code does BEFORE RDR-232 Phase 1. The Phase 1
containment beads (nexus-jypmj.1.2 .. .1.8) change that behavior on purpose; the RDR-084 regression
bead (nexus-jypmj.1.8) replaces claim 2's assertions with the containment assertions. A red here
after a Phase 1 change means exactly that: update the characterization, do not weaken the fixture.

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


def test_rdr084_fixture_non_vacuity_and_pre_change_baseline(t2_service_env) -> None:
    queries = load_golden_set(g.GOLDEN_PATH)
    art = next(q for q in queries if q.qid == ART_REQUEST_QID)

    # ── 1. the fixture loads, in the test tenant only ───────────────────────────────────────
    url = os.environ.get("NX_SERVICE_URL", "")
    assert url.startswith(("http://127.0.0.1", "http://localhost")), (
        f"the fixture must write to the hermetic substrate, not a live service: {url!r}")
    fx = g.build_rdr084_fixture()
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

    # ── 2. NON-VACUITY: the unmodified runner's evidence set is code-dominated ──────────────
    scope, bindings = g.request_call_shape(art)
    run = asyncio.run(g.run_lookup_retrieval(art.text, scope=scope, bindings=bindings))
    assert run.cut_applied, "the run must stop before the first operator step (no model call)"

    refs = g.evidence_from_ids(fx, run.search_ids + run.traverse_ids)
    refs_by_corpus = g.count_by_corpus(refs)
    reducer_input = g.evidence_from_contents(fx, run.vector_contents + run.graph_contents)
    reducer_by_corpus = g.count_by_corpus(reducer_input)
    print(
        f"RDR232-NONVACUITY refs={len(refs)} by_corpus={refs_by_corpus} "
        f"search={len(run.search_ids)} traverse={len(run.traverse_ids)} "
        f"traverse_truncated={run.traverse_ids_truncated} reducer_input={len(reducer_input)} "
        f"reducer_by_corpus={reducer_by_corpus} search_corpus={run.resolved_search_corpus!r}")

    code, rdr = refs_by_corpus.get("code", 0), refs_by_corpus.get("rdr", 0)
    assert code + rdr == len(refs), f"every reference is attributable to the fixture: {refs_by_corpus}"
    raw_references = len(run.search_ids) + len(run.traverse_ids)
    assert raw_references > 300, f"only {raw_references} chunk references; the incident had 340"
    assert code > 0.95 * len(refs), f"code is not the bulk of the evidence: {refs_by_corpus}"
    assert rdr == 0 or code >= 20 * rdr, f"evidence set is not code-dominated: code={code} rdr={rdr}"
    assert run.traverse_ids_truncated and run.traverse_ids_truncated["total"] > 300, (
        f"traverse never hit its 300-id cap: {run.traverse_ids_truncated}")
    assert run.resolved_search_corpus == "all", (
        "pre-change premise (RDR-232 Gap 2): the plan's own corpus pin beats scope='rdr'; "
        f"got {run.resolved_search_corpus!r}")
    assert reducer_by_corpus.get("rdr", 0) == 0, (
        f"no ART RDR-084 chunk reaches the reducer input before RDR-232: {reducer_by_corpus}")

    # ── 3. the baseline ─────────────────────────────────────────────────────────────────────
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

    # Non-vacuity of the measures themselves: they scored the fixture run, not an empty set.
    assert measures["primary_source_coverage"].n == 1
    assert measures["primary_source_coverage"].value == 0.0
    assert measures["scope_leakage"].n >= 1 and measures["scope_leakage"].value > 0.9
    assert measures["facet_evidence_coverage"].value == 0.0
    assert measures["eligible_plan_recall"].n == len(queries), (
        "every golden request must have had its candidate plans observed")
    for later in ("repair_yield_and_cost", "terminal_completeness"):
        assert isinstance(measures[later].value, NotMeasurable) and measures[later].value.phase == 3

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
                "references_by_corpus": refs_by_corpus,
                "reducer_input": len(reducer_input), "reducer_input_by_corpus": reducer_by_corpus,
                "search_corpus_resolved": run.resolved_search_corpus,
            },
            "plan_selection": selections,
            "measures": json.loads(measures_to_json(measures)),
            "art_reducer_input_measures": json.loads(measures_to_json(reducer_measures)),
        }
        Path(out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
