# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.1, .1.3): the self-contained ART RDR-084 fixture on the engine
substrate, the containment expectations for the runner, and the baseline emission.

ONE journey test (tests/AGENTS.md: a scenario is a single self-contained function with
function-scoped fixtures; the tenant ceiling forbids a second one) because the fixture costs about
12 s to write and the claims share it. Its parts are separate helpers so each can change on its own:

1. ``_assert_fixture_loads``: two RDR-084 documents under two owners, more than 300 code chunks
   reachable through ``implements``/``relates`` links, written into the test tenant only.
2. The containment expectations, one helper per stream:

   * ``_assert_vector_stream_contained`` (nexus-jypmj.1.3): ``nx_answer(scope="rdr")`` on the
     factual-lookup builtin searches ``rdr`` only. Before RDR-232 the plan's own ``corpus: all``
     beat the caller's scope, and the search returned 40 references, all code (T2
     ``nexus/rdr232-golden-baseline``).
   * ``_assert_graph_stream_contained`` (nexus-jypmj.1.5): the runner gives ``traverse`` a policy
     from the caller's scope, so the 115 out-of-scope code documents are dropped before manifest
     expansion and the graph stream holds ``rdr`` chunks only. Before RDR-232 it hit the 300-id
     cap with every reference code. The RDR-084 regression bead (nexus-jypmj.1.8) replaces the
     whole claim.

   A red in a helper after a later Phase 1 change means exactly that: update the expectation the
   change owns, do not weaken the fixture.
3. ``_assert_named_source_and_primary_evidence`` (nexus-jypmj.1.4): the named-source resolver and
   the section-diverse primary selection against the real catalog and T3.
4. ``_emit_baseline``: every golden request's plan selection under the matcher, and the seven
   measures with the later-phase ones reported "not measurable before Phase N". It asserts
   non-vacuity only (the measures scored the run, the later-phase ones are reported as such),
   never a value a Phase 1 change owns, so it keeps emitting whatever the tree does. The report it
   writes (``$NX_RDR232_BASELINE_OUT``) measures THIS tree; the pre-change baseline is the T2 record
   ``nexus/rdr232-golden-baseline`` at its recorded sha.
5. ``_assert_measures_follow_containment``: what the containment changes did to the measures,
   asserted after the report is written. The graph-stream bead (nexus-jypmj.1.5) edited this
   helper and flipped the graph-stream claim, never the emission.

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

#: The builtin lookup plan's first operator (``rank``, 0-based): where the reserved stream merges.
LOOKUP_RANK_STEP = 4


def _assert_fixture_loads(fx):
    """Asserts the fixture is in the hermetic tenant and returns the catalog reader."""
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
    return reader


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
    reducer_input = g.evidence_from_contents(fx, run.vector_contents + run.graph_contents)
    assert any(i.source == g.ART_REF and i.corpus == "rdr" for i in reducer_input), (
        "an ART RDR-084 chunk reaches the reducer's input once the vector stream is contained")
    return search_by_corpus


def _assert_graph_stream_contained(fx, run) -> dict[str, int]:
    """nexus-jypmj.1.5: ``traverse`` runs under the caller's scope. Out-of-scope documents are
    dropped before their manifests are read, so the 300-id cap no longer cuts in-scope evidence,
    every id carries its collection, and the walk (115 code documents, far under the engine's
    500-node cap) is reported as contained. Returns the graph stream's reference counts by
    corpus."""
    traverse_by_corpus = g.count_by_corpus(g.evidence_from_ids(fx, run.traverse_ids))
    assert traverse_by_corpus.get("rdr", 0) == len(set(run.traverse_ids)) > 0 and "code" not in traverse_by_corpus, (
        f"the graph stream holds rdr chunks only once traverse is contained: {traverse_by_corpus}")
    assert run.traverse_ids_truncated is None, run.traverse_ids_truncated
    rep = run.traverse_report
    assert rep["policy_applied"] and not rep["policy_failed"], rep
    assert rep["dropped_out_of_scope"] == len(fx.code_docs) and rep["dropped_by_corpus"] == {"code": len(fx.code_docs)}, rep
    assert rep["corpus_counts"].get("rdr", 0) >= 1 and "code" not in rep["corpus_counts"], rep
    assert rep["possibly_truncated"] is False and rep["contained"] is True and rep["nodes_walked"] < 500, rep
    assert len(run.traverse_chunk_collections) == len(run.traverse_ids), "one collection per id"
    for chash, collection in zip(run.traverse_ids, run.traverse_chunk_collections, strict=True):
        assert collection.startswith("rdr__"), (chash, collection)
        assert fx.chunks[chash][1] == "rdr"
    graph_hydrated = g.count_by_corpus(g.evidence_from_contents(fx, run.graph_contents))
    assert graph_hydrated.get("rdr", 0) > 0 and "code" not in graph_hydrated, (
        f"the graph stream hydrated for the reducer holds rdr chunks only: {graph_hydrated}")
    return traverse_by_corpus


def _assert_named_source_and_primary_evidence(fx, reader):
    """nexus-jypmj.1.4: the named-source resolver and primary evidence, real catalog + T3."""
    from dataclasses import replace as _replace

    from nexus.mcp_infra import get_t3
    from nexus.plans.evidence import OwnerResolution, SourceRef
    from nexus.plans.primary_evidence import (
        DEFAULT_CAPS,
        resolve_named_sources,
        select_primary_evidence,
        t3_chunk_fetcher,
    )

    rdr084_ref = SourceRef.parse("RDR-084")
    (both,) = resolve_named_sources([rdr084_ref], OwnerResolution("none"), cat=reader).sources
    assert both.status == "candidates" and both.tumbler is None, (
        "the incident's call has no qualifier and two RDR-084 documents: it must not pick one")
    assert {c.tumbler for c in both.candidates} == {fx.art_rdr, fx.nexus_rdr}
    art_resolution = resolve_named_sources(
        [rdr084_ref], OwnerResolution("resolved", fx.art_owner, (("project", fx.art_owner),)), cat=reader)
    assert [s.tumbler for s in art_resolution.sources] == [fx.art_rdr]
    caps = _replace(DEFAULT_CAPS, max_selected_items=6)
    primary = select_primary_evidence(
        art_resolution, cat=reader, fetch_chunks=t3_chunk_fetcher(get_t3()), caps=caps)
    (cov,) = primary.documents
    assert cov.status == "ok" and cov.manifest_rows == len(g.ART_RDR_PIECES) and cov.partial is True
    assert primary.selected_items == 6 and {i.doc_id for i in primary.items} == {fx.art_rdr}
    assert all(fx.chunks[i.chash][0] == g.ART_REF for i in primary.items), "every chunk is ART RDR-084's own"
    labelled = {i.label for i in primary.items if i.label}
    for kind in ("Locked Architecture", "Evidence", "Implementation State", "Open by Design"):
        assert any(kind in label for label in labelled), f"no {kind!r} section selected: {sorted(labelled)}"
    sources_of_labels = {i.label_source for i in primary.items}
    print(
        f"RDR232-PRIMARY rows={cov.manifest_rows} hydrated={cov.rows_hydrated} selected={cov.selected_rows} "
        f"labels_from_manifest={cov.labels_from_manifest} labels_from_content={cov.labels_from_content} "
        f"label_sources={sorted(sources_of_labels)} catalog_calls={art_resolution.catalog_calls}")
    return primary


def _assert_t2_lifecycle_stream(fx, reader) -> None:
    """nexus-jypmj.1.7 (A7): the T2 lifecycle stream against the real catalog and the real T2 client,
    and a real second tenant. The two RDR-084 documents have T2 records under the same titles in two
    projects (`<owner name>_rdr`) with different content, so a read of the wrong project shows in the
    text; a code document is a non-RDR source; the second tenant holds a decoy under the SAME project
    name and owns none of the first tenant's catalog."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    from nexus.mcp_infra import t2_ctx
    from nexus.plans.evidence import OwnerResolution, SourceRef
    from nexus.plans.lifecycle_stream import read_lifecycle_stream
    from nexus.plans.primary_evidence import resolve_named_sources
    from nexus.plans.reserved_stream import ReservedStream
    from tests._engine_substrate import ensure_engine, minted_test_tenant

    def owner(tumbler):
        return OwnerResolution("resolved", tumbler, (("project", tumbler),))

    def life(refs, qualifier, cat=reader, t2_get=None, resolution=None):
        res = resolution or resolve_named_sources([SourceRef.parse(r) for r in refs], qualifier, cat=cat)
        if t2_get is not None:
            return res, read_lifecycle_stream(res, cat=cat, t2_get=t2_get)
        with t2_ctx() as db:
            return res, read_lifecycle_stream(res, cat=cat, t2_get=lambda p, t: db.get(project=p, title=t))

    # 1. the ART source reads the ART project, and only it
    res, art = life(["RDR-084"], owner(fx.art_owner))
    assert res.sources[0].rdr_number == 84
    assert [(r.kind, r.project, r.title, r.text) for r in art.records] == [
        ("metadata", g.ART_T2_PROJECT, "084", g.ART_T2_RECORDS["084"].strip()),
        ("gate-latest", g.ART_T2_PROJECT, "084-gate-latest", g.ART_T2_RECORDS["084-gate-latest"].strip())]
    assert art.reads <= 8 and art.sources[0].status == "read"
    assert not any("Nexus plan-hygiene" in r.text for r in art.records), "no Nexus record reaches an ART RDR"
    # 2. the Nexus source reads the Nexus project
    _, nexus = life(["RDR-084"], owner(fx.nexus_owner))
    assert {r.project for r in nexus.records} == {g.NEXUS_T2_PROJECT}
    assert [r.text for r in nexus.records] == [v.strip() for v in g.NEXUS_T2_RECORDS.values()]
    # 3. no qualifier, two projects: ambiguous, no stream, and the envelope row says why
    _, ambiguous = life(["RDR-084"], OwnerResolution("none"))
    assert ambiguous.records == () and ambiguous.reads == 0
    assert ambiguous.sources[0].status == "not_resolved" and "candidates" in ambiguous.sources[0].detail
    # 4. a code document named by its tumbler is a non-RDR source: no stream, no T2 read
    code_res, code = life([fx.code_docs[0]], OwnerResolution("none"))
    assert code_res.sources[0].status == "resolved" and code_res.sources[0].rdr_number is None
    assert code.records == () and code.reads == 0 and code.sources[0].status == "not_rdr"
    # 5. the same RDR named by its tumbler resolves to an RDR and reads the same records
    tumbler_res, by_tumbler = life([fx.art_rdr], OwnerResolution("none"))
    assert tumbler_res.sources[0].rdr_number == 84
    assert [r.text for r in by_tumbler.records] == [r.text for r in art.records]
    # 6. the records lead the reserved share and the share cap evicts chunks, never them
    stream = ReservedStream.from_evidence(None, extra=art.reserved_items())
    assert stream is not None and [i.raw for i in stream.items] == [r.text for r in art.records]

    # 7. a second tenant: it holds a decoy record under the first tenant's PROJECT NAME, owns none of
    # the first tenant's catalog, and gets no stream for the first tenant's source.
    state = ensure_engine()
    with minted_test_tenant(state) as (_tenant_b, token_b):
        memory_b = HttpMemoryStore(base_url=state["base_url"], _token=token_b)
        cat_b = HttpCatalogClient(base_url=state["base_url"], _token=token_b)
        try:
            memory_b.put(project=g.ART_T2_PROJECT, title="084", content="TENANT B DECOY", tags="rdr")
            # the first tenant still reads its own record, not the decoy under the same coordinates
            _, again = life(["RDR-084"], owner(fx.art_owner))
            assert [r.text for r in again.records] == [r.text for r in art.records]
            # the second tenant cannot reach the first tenant's source: its catalog has no such owner,
            # and a hand-built resolved source (bypassing the resolver) still finds no owner row
            res_b, b_stream = life(
                ["RDR-084"], owner(fx.art_owner), cat=cat_b,
                t2_get=lambda p, t: memory_b.get(project=p, title=t))
            assert res_b.sources[0].status == "unresolved", res_b.sources[0]
            assert b_stream.records == () and b_stream.reads == 0
            assert b_stream.sources[0].status == "not_resolved"
            forced = res_b.__class__(sources=(res.sources[0],))  # tenant A's resolved source, handed to tenant B
            _, b_forced = life(
                [], OwnerResolution("none"), cat=cat_b, resolution=forced,
                t2_get=lambda p, t: memory_b.get(project=p, title=t))
            assert b_forced.records == () and b_forced.reads == 0, "no owner row, so no T2 read at all"
            assert b_forced.sources[0].status == "project_undetermined"
            # and with the project forced, tenant B's T2 returns its own decoy, never tenant A's record
            assert memory_b.get(project=g.ART_T2_PROJECT, title="084-gate-latest") is None
            assert memory_b.get(project=g.NEXUS_T2_PROJECT, title="084") is None
        finally:
            cat_b.close()
            memory_b.close()
    print(
        f"RDR232-LIFECYCLE art_records={len(art.records)} art_reads={art.reads} art_chars={art.chars} "
        f"nexus_records={len(nexus.records)} ambiguous_reads={ambiguous.reads} code_reads={code.reads} "
        f"tenant_b_reads={b_forced.reads} tenant_b_records={len(b_forced.records)}")


def _assert_primary_stream_reaches_the_reducer(fx, run, primary) -> None:
    """nexus-jypmj.1.6: the reserved primary stream is merged ahead of the plan's own inputs at the
    first operator (the builtin's ``rank``, explicit ``inputs`` of both hydrated streams). Every
    primary chunk reaches the reducer's input, first; the positional cap keeps them and cuts
    secondary items only. Run through the runner's real hydration with the run's real contents."""
    import json

    from nexus.plans.reserved_stream import ReservedStream, reserved_scope, set_reserved_step
    from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args

    secondary = run.vector_contents + run.graph_contents
    _, plain = _hydrate_operator_args("rank", {"criterion": "c", "inputs": list(secondary)})
    before = json.loads(plain["items"])
    assert before == [c for c in secondary if c], "no stream: the explicit inputs are untouched"

    stream = ReservedStream.from_evidence(primary)
    assert stream is not None and len(stream.items) == primary.selected_items
    with reserved_scope(stream):
        set_reserved_step(LOOKUP_RANK_STEP)
        _, merged = _hydrate_operator_args("rank", {"criterion": "c", "inputs": list(secondary)})
    after = json.loads(merged["items"])
    reserved = [i.text for i in stream.items]
    assert after[:len(reserved)] == reserved, "the primary chunks lead the reducer's input, in order"
    assert len(after) <= _OPERATOR_MAX_INPUTS
    def art_chunks(contents):
        return [i for i in g.evidence_from_contents(fx, contents) if i.source == g.ART_REF and i.corpus == "rdr"]

    in_before = art_chunks(before)
    in_after = art_chunks([i.raw for i in stream.items] + after[len(reserved):])
    assert len(in_after) >= len(reserved) and len(in_after) >= len(in_before), (
        "the primary chunks are in the reducer's input and the merge removed no ART chunk")
    report = stream.report()
    print(
        f"RDR232-MERGE secondary_before={len(before)} reducer_input_after={len(after)} "
        f"reserved={report['reserved_items']} deduped_secondary={report['deduped_secondary']} "
        f"secondary_cut={report['secondary_cut']} capped={report['capped']} "
        f"art_rdr_chunks_in_reducer_input_before={len(in_before)} after={len(in_after)}")


def _emit_baseline(fx, run, queries, art, search_by_corpus, traverse_by_corpus) -> dict:
    """Emit the report and return the measures. Asserts non-vacuity only."""
    refs = g.evidence_from_ids(fx, run.search_ids + run.traverse_ids)
    refs_by_corpus = g.count_by_corpus(refs)
    reducer_input = g.evidence_from_contents(fx, run.vector_contents + run.graph_contents)
    reducer_by_corpus = g.count_by_corpus(reducer_input)
    print(
        f"RDR232-STREAMS vector={search_by_corpus} graph={traverse_by_corpus} "
        f"refs={len(refs)} by_corpus={refs_by_corpus} traverse_truncated={run.traverse_ids_truncated} "
        f"reducer_input={len(reducer_input)} reducer_by_corpus={reducer_by_corpus} "
        f"search_corpus={run.resolved_search_corpus!r}")

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
    return measures


def _assert_measures_follow_containment(measures: dict) -> None:
    """What the stream containment did to the measures: the vector stream (nexus-jypmj.1.3) reaches
    the ART RDR, so coverage of the named source is no longer zero, and with the graph stream
    contained too (nexus-jypmj.1.5) no reference leaves the ``rdr`` scope, so leakage is zero.
    Before either, leakage was total."""
    assert measures["primary_source_coverage"].value == 1.0
    assert measures["scope_leakage"].value == 0.0, measures["scope_leakage"]
    assert measures["facet_evidence_coverage"].value > 0.0


def test_rdr084_fixture_containment_and_baseline_emission(t2_service_env) -> None:
    queries = load_golden_set(g.GOLDEN_PATH)
    art = next(q for q in queries if q.qid == ART_REQUEST_QID)

    fx = g.build_rdr084_fixture()
    reader = _assert_fixture_loads(fx)

    scope, bindings = g.request_call_shape(art)
    run = asyncio.run(g.run_lookup_retrieval(art.text, scope=scope, bindings=bindings))
    search_by_corpus = _assert_vector_stream_contained(fx, run)
    primary = _assert_named_source_and_primary_evidence(fx, reader)
    _assert_t2_lifecycle_stream(fx, reader)
    _assert_primary_stream_reaches_the_reducer(fx, run, primary)
    traverse_by_corpus = _assert_graph_stream_contained(fx, run)

    measures = _emit_baseline(fx, run, queries, art, search_by_corpus, traverse_by_corpus)
    _assert_measures_follow_containment(measures)
