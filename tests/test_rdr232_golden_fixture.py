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

   nexus-jypmj.1.8 made the fixture stop saturating: distractor RDRs take slots from the named source
   in a full vector stream (40 of more than 100 RDR chunks), 62 secondary documents with three link
   roles exceed the traversal document cap, and the RDR-084 regression runs ``nx_answer`` itself
   (``_assert_production_wiring_reaches_the_reducer``) for the lookup plan, the taxonomy-domain builtin
   and the ``single_query`` fast path. Primary and facet coverage over the streams' ids are
   REFERENCE-LEVEL (the graph stream carries a seed's whole manifest); the reducer input is what the
   model is handed, counted by ``evidence_diagnostics``, and its leakage is the figure that matters.
3. ``_assert_named_source_and_primary_evidence`` (nexus-jypmj.1.4): the named-source resolver and
   the section-diverse primary selection against the real catalog and T3.
4. ``_emit_baseline``: every golden request's plan selection under the matcher, and the seven
   measures with the later-phase ones reported "not measurable before Phase N". It asserts
   non-vacuity only (the measures scored the run, the later-phase ones are reported as such),
   never a value a Phase 1 change owns, so it keeps emitting whatever the tree does. The report it
   writes (``$NX_RDR232_BASELINE_OUT``) measures THIS tree; the pre-change baseline is the T2 record
   ``nexus/rdr232-golden-baseline`` at its recorded sha. The plan-side measures are taken twice
   (nexus-jypmj.1.14): over the builtins-only library, which is a FLOOR (the incident request selects
   ``research:default`` there and scores as the right shape), and again after
   ``_measure_with_grown_473_clone`` adds a grown clone of plan 473, which is what the incident
   selected. The report carries ``BASELINE_CAVEATS`` and so must any quotation of it:
   ``eligible_plan_recall`` is a blacklist reading, and the evidence-side measures score n=1.
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
    BASELINE_CAVEATS,
    MEASURE_NAMES,
    NotMeasurable,
    RunObservation,
    compute_measures,
    measures_to_json,
)
from bench.schema import GOLDEN_SET_VERSION, load_golden_set
from tests import _rdr232_golden as g

ART_REQUEST_QID = "G06-synthesis-art-rdr084"

#: Floor on code chunks reachable from the RDRs: the traverse cap is 300, so the fixture must
#: exceed it for the cap to bite (the incident's mix needed more than 300 reachable code ids).
MIN_CODE_CHUNKS = 301

#: The builtin lookup plan's first operator (``rank``, 0-based): where the reserved stream merges.
LOOKUP_RANK_STEP = 4

#: The builtin lookup plan's ``limit`` binding, the vector stream's size.
SEARCH_LIMIT = 40


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
    # nexus-jypmj.1.8: the stream is FULL, not saturated by construction. The fixture holds far more
    # RDR chunks than the search limit and the distractor RDRs (near-copies of the request's wording)
    # take slots from the named source: the vector stream seeds the walk with the ART RDR but does not
    # hold all of it, and a wrong-number RDR holds some of the 40.
    rdr_chunks = sum(1 for v in fx.chunks.values() if v[1] == "rdr")
    assert rdr_chunks > 2 * SEARCH_LIMIT, (rdr_chunks, "the RDR chunks must exceed the search limit by design")
    vector = set(run.search_ids)
    assert len(vector) == SEARCH_LIMIT, f"the vector stream is at the search limit, not under it: {len(vector)}"
    art_in_vector = sum(1 for c in vector if fx.chunks[c][0] == g.ART_REF)
    distractors_in_vector = sum(1 for c in vector if fx.chunks[c][0].startswith("RDR-1") and fx.chunks[c][0].endswith("@ART"))
    assert 0 < art_in_vector < len(g.ART_RDR_PIECES), (
        f"the ART RDR must seed the walk (>0) without the vector stream holding all of it: {art_in_vector}")
    assert distractors_in_vector > 0, "the distractor RDRs take slots from the named source"
    assert not any(c in vector for c in fx.secondary_role_of_chash), "the secondary documents are reached by the walk only"
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
    # the code documents the walk reached are those linked to a seeded RDR; each is dropped before its
    # manifest is read. The ART RDR is always a seed (asserted by the vector helper); the Nexus RDR is
    # a seed only if one of its chunks won a slot among the 40.
    nexus_seeded = any(fx.chunks[c][0] == g.NEXUS_REF for c in set(run.search_ids))
    reached_code = g.ART_CODE_DOCS + (g.NEXUS_CODE_DOCS if nexus_seeded else 0)
    assert rep["dropped_out_of_scope"] == reached_code and rep["dropped_by_corpus"] == {"code": reached_code}, rep
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
    return primary, art_resolution


def _assert_t2_lifecycle_stream(fx, reader) -> None:
    """nexus-jypmj.1.7 (A7): the T2 lifecycle stream against the real catalog and the real T2 client,
    and a real second tenant. The two RDR-084 documents have T2 records under the same titles in two
    projects (`<owner name>_rdr`) with different content, so a read of the wrong project shows in the
    text; a code document is a non-RDR source; the second tenant holds a decoy under the SAME project
    name and owns none of the first tenant's catalog."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    from nexus.mcp_infra import t2_index_write
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
        # the shared transport nx_answer itself reads through (core._nx_answer_lifecycle_stream)
        return res, read_lifecycle_stream(res, cat=cat, t2_get=lambda p, t, timeout: t2_index_write(
            lambda db: db.memory.get(project=p, title=t, timeout=timeout, retry_read_timeout=False)))

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
                t2_get=lambda p, t, timeout: memory_b.get(project=p, title=t, timeout=timeout, retry_read_timeout=False))
            assert res_b.sources[0].status == "unresolved", res_b.sources[0]
            assert b_stream.records == () and b_stream.reads == 0
            assert b_stream.sources[0].status == "not_resolved"
            forced = res_b.__class__(sources=(res.sources[0],))  # tenant A's resolved source, handed to tenant B
            _, b_forced = life(
                [], OwnerResolution("none"), cat=cat_b, resolution=forced,
                t2_get=lambda p, t, timeout: memory_b.get(project=p, title=t, timeout=timeout, retry_read_timeout=False))
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
    return art


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


def _assert_traversal_policy_exercised(fx, run) -> dict[str, int]:
    """nexus-jypmj.1.8 (1.5 review, item 3): the document cap, the role ranking and ``dropped_cap`` run
    on the substrate. The ART RDR has more secondary in-scope documents than the cap (50), linked with
    three roles; the cap keeps every citation, every implementation and only the related ones that fit."""
    from nexus.plans.traversal_policy import DEFAULT_MAX_DOCUMENTS

    rep = run.traverse_report
    assert g.SECONDARY_DOCS > DEFAULT_MAX_DOCUMENTS
    assert rep["dropped_cap"] == g.SECONDARY_DOCS - DEFAULT_MAX_DOCUMENTS, rep
    assert rep["dropped_role"] == 0, "the factual-evidence step admits every role"
    kept = {}
    for chash in run.traverse_ids:
        role = fx.secondary_role_of_chash.get(chash)
        if role:
            kept[role] = kept.get(role, 0) + 1
    cites, impls, relates = (n for _link, n in g.SECONDARY_ROLE_DOCS)
    assert kept == {"citation": cites, "implementation": impls, "related": DEFAULT_MAX_DOCUMENTS - cites - impls}, (
        f"role ranking decides the cap: citation, then implementation, then related: {kept}")
    assert len(set(run.traverse_ids)) <= 300
    return kept


def _assert_oversized_input_keeps_the_reserved_stream(fx, art, primary, resolution, life) -> dict:
    """nexus-jypmj.1.8 (1.5/1.6 review): an input larger than ``_OPERATOR_MAX_INPUTS`` through the
    explicit-inputs shape, on real search and hydration. Every reserved item survives the positional cap
    and leads the input, only secondary items are cut, and the diagnostics count the input by stream and
    collection with every item attributed (a retrieved chunk's id is the hash of its text)."""
    import json

    from nexus.mcp import core as mcp_core
    from nexus.plans.evidence import ScopeConstraint
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder, build_evidence_diagnostics, reducer_input_scope
    from nexus.plans.reserved_stream import ReservedStream, reserved_scope, set_reserved_step, strip_source_note
    from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args

    # nexus-jypmj.1.18: the production owner narrowing (real collection listing and catalog rows). Without it
    # the other project's same-number RDR is in the search; with the ART owner it is not.
    from nexus.plans.owner_narrowing import narrow_args_by_owner

    unscoped = mcp_core.search(query=art.text, corpus="rdr", limit=g.OVERSIZED_SEARCH_LIMIT, threshold=2.0, structured=True)
    foreign_unscoped = sum(1 for c in unscoped["chunk_collections"] if c == fx.nexus_rdr_collection)
    assert foreign_unscoped > 0, "non-vacuity: without the owner the other project's RDR-084 is in the vector stream"
    narrowed, narrowing = narrow_args_by_owner({"query": art.text, "corpus": "rdr"}, fx.art_owner)
    assert narrowing is not None and narrowing.status == "narrowed", narrowing
    assert narrowing.removed == (fx.nexus_rdr_collection,) and narrowed["corpus"] == fx.art_rdr_collection, narrowing
    hits = mcp_core.search(query=art.text, corpus=narrowed["corpus"], limit=g.OVERSIZED_SEARCH_LIMIT, threshold=2.0, structured=True)
    got = mcp_core.store_get_many(ids=hits["ids"], collections=hits["collections"], structured=True)
    contents = [c for c in got["contents"] if c]
    assert len(hits["ids"]) > _OPERATOR_MAX_INPUTS, "the fixture holds more RDR chunks than the operator cap"
    assert len(contents) > _OPERATOR_MAX_INPUTS, "the hydrated secondary input alone exceeds the operator cap"
    stream = ReservedStream.from_evidence(primary, extra=life.reserved_items())
    rec = ReducerInputRecorder()
    with reducer_input_scope(rec), reserved_scope(stream):
        rec.set_step(LOOKUP_RANK_STEP)
        set_reserved_step(LOOKUP_RANK_STEP)
        _, args = _hydrate_operator_args("rank", {"criterion": "c", "inputs": list(contents)})
    after = json.loads(args["items"])
    reserved = [i.text for i in stream.items]
    assert len(after) == _OPERATOR_MAX_INPUTS and after[:len(reserved)] == reserved, "the cap cut secondary items only"
    report = stream.report()
    assert report["capped"] is True and report["secondary_cut"] > 0 and report["over_share_dropped"] == 0, report

    diag = build_evidence_diagnostics(
        steps=[hits, got], tools=["search", "store_get_many"], scope=ScopeConstraint.parse("rdr"),
        recorder=rec, reserved=stream, primary=primary, lifecycle=life, named_sources=resolution)
    r = diag["reducer_input"]
    secondary = after[len(reserved):]
    assert r["items"] == _OPERATOR_MAX_INPUTS and r["step"] == LOOKUP_RANK_STEP
    assert r["by_stream"] == {"t2": len(life.records), "primary": primary.selected_items, "vector": len(secondary)}, r
    assert r["unattributed"] == 0, "every retrieved item is found again by the hash of its text"
    assert (r["out_of_scope"], diag["scope_check"]["state"]) == (0, "satisfied"), diag["scope_check"]
    # wrong-source contamination (nexus-jypmj.1.8 found it, nexus-jypmj.1.18 closes it): the other project's
    # RDR-084 is in scope and lexically close, so it reached the reducer through the VECTOR stream. With the
    # explicit ART owner it does not: no foreign-owner item in the reducer input, and the production counts
    # equal the oracle's.
    oracle_wrong = sum(1 for t in secondary if fx.chunks.get(g.chash_of(strip_source_note(t)), ("",))[0] == g.NEXUS_REF)
    assert oracle_wrong == 0
    assert r["by_stream_collection"]["vector"].get(fx.nexus_rdr_collection, 0) == oracle_wrong == 0
    assert set(r["by_stream_collection"]["vector"]) == {fx.art_rdr_collection}, r["by_stream_collection"]
    # the primary budget here is six chunks of a twelve-chunk document: partial, and the lifecycle facet resolved
    assert diag["source_check"]["state"] == "partial" and diag["source_check"]["unresolved"] == []
    assert not any(fx.chunks.get(g.chash_of(strip_source_note(t)), ("", ""))[1] == "code" for t in after)
    print(
        f"RDR232-OVERSIZED hits={len(hits['ids'])} hydrated={len(contents)} reducer_input={r['items']} "
        f"by_stream={r['by_stream']} secondary_cut={report['secondary_cut']} "
        f"wrong_source_unscoped_hits={foreign_unscoped} wrong_source_in_reducer={oracle_wrong} "
        f"unattributed={r['unattributed']} "
        f"by_collection={ {c: n for c, n in r['by_collection'].items()} }")
    return diag


def _nx_answer_envelope(art, scope, bindings, *, strategy: tuple[str, str] | None = None) -> dict:
    """``nx_answer`` itself on the substrate (the real ``_nx_answer_named_sources``,
    ``_nx_answer_lifecycle_stream``, ``_nx_answer_primary_stream`` and ``plan_run``), stopped before the
    first operator by the continuation handoff; the model is patched to fail loudly. *strategy*
    ``(verb, strategy)`` pins the library plan the matcher offers (the matcher itself, run over the
    seeded builtins, picks it; ``None`` lets ``nx_answer`` choose)."""
    from unittest.mock import AsyncMock, patch

    from nexus.mcp.core import nx_answer

    lib = g.seed_builtin_plans()
    no_model = AsyncMock(side_effect=AssertionError("a model call: the journey must stop before the first operator"))
    patches = [
        patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True),
        patch("nexus.operators.dispatch.claude_dispatch", no_model),
    ]
    if strategy is not None:
        match = g.library_match(lib, art.text, scope=scope, bindings=bindings,
                                dimensions={"verb": strategy[0], "strategy": strategy[1]})
        patches.append(patch("nexus.plans.matcher.plan_match", return_value=[match]))
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        env = asyncio.run(nx_answer(
            question=art.text, scope=scope, bindings=bindings, min_confidence=0.0,
            continuation=True, structured=True))
    no_model.assert_not_awaited()
    return env


def _assert_production_wiring_reaches_the_reducer(
    fx, art, scope, bindings, primary, resolution, *, strategy: tuple[str, str] | None, expect_dropped_cap: int,
) -> dict:
    """nexus-jypmj.1.8 (1.7 critique, item 1): ``nx_answer`` on the substrate, stopped before the first
    operator. The reduction request carries the ART gate record and every ART RDR section, none of the
    Nexus project's records, and the envelope's diagnostics describe it."""
    import json

    env = _nx_answer_envelope(art, scope, bindings, strategy=strategy)
    assert env["continuation"] is not None, env["final_text"][:400]
    assert env["plan_id"] > 0, "a library plan"
    request = json.dumps(env["continuation"])

    def present(text: str) -> bool:
        # the items travel as a JSON string inside the prompt, so a newline is escaped (twice, once more
        # in `request`): look for the text's last line, which is a paragraph of its own and unique
        return json.dumps(text.strip().split("\n")[-1])[1:-1] in request

    gate = g.ART_T2_RECORDS["084-gate-latest"]
    assert present(gate) and present(g.ART_T2_RECORDS["084"]), "the ART lifecycle records are in the reduction request"
    assert not any(present(v) for v in g.NEXUS_T2_RECORDS.values()), "no Nexus project record reaches an ART request"
    art_pieces_in_request = [(f, t) for f, t in g.ART_RDR_PIECES if present(t)]
    n_pieces = len(g.ART_RDR_PIECES)
    assert len(art_pieces_in_request) == n_pieces, (
        "every section of the named source is in the reduction request (the document fits the primary budget)")

    d = env["evidence_diagnostics"]
    r = d["reducer_input"]
    assert r is not None and r["step"] == LOOKUP_RANK_STEP
    assert r["by_stream"]["t2"] == 2 and r["by_stream"]["primary"] == n_pieces, r
    assert r["unattributed"] == 0 and r["out_of_scope"] == 0, r
    assert set(r["by_stream_collection"]["primary"]) == {fx.art_rdr_collection}
    assert all(c.startswith("rdr__") for s in ("vector", "graph") for c in r["by_stream_collection"].get(s, {})), r
    sc = d["source_check"]
    (src,) = sc["sources"]
    assert sc["state"] == "covered" and src["reached_reducer"] == n_pieces == src["selected"], sc
    assert src["lifecycle"]["state"] == "resolved" and src["lifecycle"]["reached_reducer"] == 2, src
    assert d["scope_check"]["state"] == "satisfied" and d["scope_check"]["violations"] == [], d["scope_check"]
    assert d["streams"]["vector"]["out_of_scope"] == 0 and d["streams"]["graph"]["out_of_scope"] == 0
    # nexus-jypmj.1.18: scope `rdr` reaches the step first, then the explicit ART owner narrows `rdr` to the
    # ART owner's collection; the other project's rdr collection is removed from every vector step.
    assert all(st["corpus"] == fx.art_rdr_collection for st in d["streams"]["vector"]["steps"]), d["streams"]["vector"]["steps"]
    on_ = d["scope_check"]["owner_narrowing"]
    assert on_["owners"] == [fx.art_owner] and on_["skipped_steps"] == [], on_
    assert on_["steps"] and all(s["removed"] == [fx.nexus_rdr_collection] and s["outcome"] == "narrowed" for s in on_["steps"]), on_
    assert {s["step_index"] for s in on_["steps"]} == {st["step_index"] for st in d["streams"]["vector"]["steps"]}
    assert not any(
        fx.nexus_rdr_collection in r["by_stream_collection"].get(s, {}) for s in ("vector", "graph", "primary")
    ), "no foreign-owner item reaches the reducer input"
    assert d["streams"]["primary"]["references"] == n_pieces and d["streams"]["t2"]["references"] == 2
    assert d["capped_traversals"] == 0 and d["skipped_steps"] == []
    (trav,) = d["traversals"]
    assert trav["dropped_cap"] == expect_dropped_cap and trav["possibly_truncated"] is False, trav
    assert env["reserved_stream"]["merged"] is True and env["reserved_stream"]["merged_at_step"] == LOOKUP_RANK_STEP
    covered_facets = {f for f, _t in art_pieces_in_request if f}
    label = strategy[1] if strategy else "natural-selection"
    print(
        f"RDR232-PRODUCTION[{label}] plan_id={env['plan_id']} reducer_items={r['items']} by_stream={r['by_stream']} "
        f"vector_refs={d['streams']['vector']['references']} graph_refs={d['streams']['graph']['references']} "
        f"vector_by_collection={d['streams']['vector']['by_collection']} "
        f"graph_by_collection={d['streams']['graph']['by_collection']} "
        f"reducer_by_collection={r['by_collection']} "
        f"source_state={sc['state']} lifecycle={src['lifecycle']['state']} scope_state={d['scope_check']['state']} "
        f"capped_traversals={d['capped_traversals']} traversal_dropped_cap={trav['dropped_cap']} "
        f"facets_in_request={sorted(covered_facets)}")
    return {"envelope": env, "present": present, "pieces": art_pieces_in_request}


def _assert_single_query_fast_path_is_contained(fx, art, scope) -> None:
    """nexus-jypmj.1.8 / the Phase 1 proof: the ``single_query`` fast path with ``scope="rdr"`` queries
    ``rdr`` only, on the substrate, and its envelope counts the one vector stream."""
    env = _nx_answer_envelope(art, scope, {"content_type": "rdr"}, strategy=("research", "corpus-coverage-check"))
    assert env["continuation"] is None and env["step_count"] == 1, "the single_query fast path, not a plan run"
    d = env["evidence_diagnostics"]
    v = d["streams"]["vector"]
    assert v["references"] > 0 and set(v["by_collection"]) <= {fx.art_rdr_collection, fx.nexus_rdr_collection}, v
    assert v["out_of_scope"] == 0 and d["reducer_input"] is None
    assert [x["outcome"] for x in env["scope_diagnostics"]] == ["narrowed"]
    print(f"RDR232-FASTPATH vector_refs={v['references']} by_collection={v['by_collection']} scope_state={d['scope_check']['state']}")

    # nexus-jypmj.2.4: the call as filed names RDR-084 in its text and binds no source. Its text source now
    # reaches the real resolver: with no reliable session project (this process is not an ART session) both
    # documents are candidates and nothing is picked ...
    (named,) = env["named_sources"]["sources"]
    assert named["origin"] == "question" and named["status"] == "candidates" and named["tumbler"] is None, named
    assert {c["tumbler"] for c in named["candidates"]} == {fx.art_rdr, fx.nexus_rdr}
    assert env["evidence_request"]["owner"]["status"] == "none" and env["primary_evidence"] is None
    # ... and with the session's project ART (the seam of the tier replaced, the catalog real) it resolves
    # to ART's document, inferred, still without narrowing any stream by owner.
    from unittest.mock import patch

    from nexus.plans.active_project import ActiveProject

    with patch("nexus.plans.active_project.detect_active_project",
               return_value=ActiveProject(True, owner=fx.art_owner, name=g.ART_PROJECT)):
        env_art = _nx_answer_envelope(art, scope, {"content_type": "rdr"}, strategy=("research", "corpus-coverage-check"))
    (named_art,) = env_art["named_sources"]["sources"]
    assert (named_art["status"], named_art["tumbler"], named_art["inferred"]) == ("resolved", fx.art_rdr, True), named_art
    assert env_art["scope_diagnostics"] == env["scope_diagnostics"], "an inferred project narrows no stream"
    print(f"RDR232-FASTPATH-TEXT-SOURCE no_project={named['status']} art_session={named_art['status']}:{named_art['tumbler']}")

    # nexus-jypmj.1.18: the same call with the explicit ART owner queries the ART collection only.
    env = _nx_answer_envelope(
        art, scope, {"content_type": "rdr", "owner": fx.art_owner}, strategy=("research", "corpus-coverage-check"))
    assert env["continuation"] is None and env["step_count"] == 1
    d = env["evidence_diagnostics"]
    v = d["streams"]["vector"]
    assert v["references"] > 0 and set(v["by_collection"]) == {fx.art_rdr_collection}, v
    assert [(x["scope_kind"], x["outcome"]) for x in env["scope_diagnostics"]] == [
        ("corpus_prefix", "narrowed"), ("owner", "narrowed")], env["scope_diagnostics"]
    assert d["scope_check"]["owner_narrowing"]["steps"][0]["removed"] == [fx.nexus_rdr_collection]
    print(f"RDR232-FASTPATH-OWNER vector_refs={v['references']} by_collection={v['by_collection']}")


def _assert_library_holds_builtins_and_clone(library) -> None:
    """Seeding the clone must not have overwritten a builtin row (``plans.query`` is the seed loader's
    reconcile key): the library still holds every builtin-tagged row, plus exactly one grown clone."""
    rows = library.list_plans(limit=500, include_disabled=True)
    tag_sets = [{t.strip() for t in str(r.get("tags") or "").split(",")} for r in rows]
    builtin = sum(1 for t in tag_sets if "builtin-template" in t)
    grown = [r for r, t in zip(rows, tag_sets) if "grown" in t]
    expected = len(list(g.BUILTIN_PLANS.glob("*.yml")))
    assert expected >= 10, f"builtin template census came back with {expected}"
    assert builtin == expected, f"the library holds {builtin} builtin-tagged rows, the templates are {expected}"
    assert len(grown) == 1 and grown[0].get("name") == g.GROWN_473_STRATEGY, grown


def _measure_with_grown_473_clone(library, queries, art, floor_observations, floor_selections) -> dict:
    """nexus-jypmj.1.14: the plan-side measures again with a grown clone of plan 473 in the library.

    The builtins-only library selects ``research:default`` for the incident request (the matcher over
    the local embeddings), so that baseline is a FLOOR: it scored the request as the right shape. The
    incident selected plan 473. The clone is a grown plan with 473's body whose originating question is
    the incident request, so the matcher's verbatim-repeat rule selects it for G06 (confidence 1.0) and
    only for G06: this reproduces the selection, it does not measure how 473 matched live. The label
    ``grown:hybrid-factual-lookup-473`` names it in ``unacceptable_plans``.

    The +1 admission is forced by construction (one request, exact-text repeat), not a measured rate.
    The clone stands for one Phase 2 exit clause, "unknown-grown plans cannot bypass the gate"; the
    clause "473 is ineligible" (a builtin-tagged row declaring lookup) is NOT measured here, and 473's
    builtin origin is inferred (RDR-232 line 1606, T2 nexus_rdr/189-research-11), not read live.

    Asserted relations, not values: the clone changes the selection of the incident request and of no
    other, every other request keeps both its selection and its candidate set (so recall cannot have
    moved through them), it is that request's wrong-shape admission, and nothing else moves. The numbers are emitted,
    and the Phase 2 gate (nexus-jypmj.2) is what turns this pin.
    """
    from nexus.mcp_infra import reset_plan_cache_for_tests

    g.seed_grown_473_clone(library, origin_question=art.text)
    _assert_library_holds_builtins_and_clone(library)
    reset_plan_cache_for_tests()  # the matcher's cosine cache holds the library as of its last refresh
    observations: list[RunObservation] = []
    selections: dict[str, dict] = {}
    for q in queries:
        sc, bd = g.request_call_shape(q)
        sel = g.observe_plan_selection(library, q.text, scope=sc, bindings=bd)
        selections[q.qid] = {"selected": sel.selected, "candidates": sel.candidates, "confidences": sel.confidences}
        observations.append(RunObservation(qid=q.qid, selected_plan=sel.selected, candidate_plans=sel.candidates))
    with_clone = compute_measures(queries, observations)
    floor = compute_measures(queries, floor_observations)

    assert floor_selections[ART_REQUEST_QID]["selected"] != g.GROWN_473_KEY
    assert selections[ART_REQUEST_QID]["selected"] == g.GROWN_473_KEY, selections[ART_REQUEST_QID]
    changed, candidates_moved = g.selection_drift(floor_selections, selections)
    assert changed == [ART_REQUEST_QID], f"the clone must change the incident request's selection only: {changed}"
    # recall parity: every other request keeps its candidate set, not only its selection
    assert set(candidates_moved) <= {ART_REQUEST_QID}, (
        f"the clone changed the candidate set of requests other than the incident one: {candidates_moved}")
    f, c = floor["wrong_shape_admission_rate"], with_clone["wrong_shape_admission_rate"]
    assert (c.n, c.detail["selected_unacceptable"]) == (f.n, f.detail["selected_unacceptable"] + 1), (f, c)
    assert with_clone["eligible_plan_recall"].n == floor["eligible_plan_recall"].n == len(queries)
    # recall parity, value side: the incident request keeps research:default as an unlabeled candidate,
    # so the clone adds an admission without moving recall (a relation; the Phase 2 gate turns it)
    assert with_clone["eligible_plan_recall"].value == floor["eligible_plan_recall"].value, (
        floor["eligible_plan_recall"], with_clone["eligible_plan_recall"])
    print(
        f"RDR232-GROWN473 label={g.GROWN_473_KEY} g06_floor_selected={floor_selections[ART_REQUEST_QID]['selected']} "
        f"g06_with_clone_selected={selections[ART_REQUEST_QID]['selected']} "
        f"g06_confidences={selections[ART_REQUEST_QID]['confidences']} "
        f"wrong_shape floor={f.value} ({f.detail['selected_unacceptable']}/{f.n}) "
        f"with_clone={c.value} ({c.detail['selected_unacceptable']}/{c.n}) "
        f"eligible_plan_recall floor={floor['eligible_plan_recall'].value} "
        f"with_clone={with_clone['eligible_plan_recall'].value}")
    eligibility = _measure_eligibility(library, queries, selections)
    return {"selections": selections, "measures": with_clone, "floor": floor, "eligibility": eligibility}


class _LegacyBuiltins:
    """The plan library as it was before nexus-jypmj.2.1: builtin rows without their declaration."""

    def __init__(self, library) -> None:
        self._lib = library

    def __getattr__(self, name):
        return getattr(self._lib, name)

    @staticmethod
    def _strip(row):
        if not row or "builtin-template" not in str(row.get("tags", "")):
            return row
        import json as _json

        plan = _json.loads(row["plan_json"])
        plan.pop("answer_kind", None)
        return {**row, "plan_json": _json.dumps(plan)}

    def get_plan(self, plan_id):
        return self._strip(self._lib.get_plan(plan_id))

    def search_plans(self, *args, **kwargs):
        return [self._strip(r) for r in self._lib.search_plans(*args, **kwargs)]


def _measure_eligibility(library, queries, with_clone_selections) -> dict:
    """nexus-jypmj.2.3: the answer-kind eligibility filter over the golden set, gate off (shadow) and on.

    Each request's kind is derived by the deterministic first pass of ``RequestCoverage`` (no model call),
    the filter is built for that kind, and the unmodified call shape of ``observe_plan_selection`` is made
    twice over the same library (builtins plus the grown clone of plan 473): with a shadow filter (the
    shipped setting) and with an enforcing one. Asserted relations, not values: the shadow run selects
    exactly what the unfiltered run did for every request (selection does not move), the enforcing run
    never selects a plan the filter refused, and the incident request no longer selects the grown clone.
    The numbers are emitted; nexus-jypmj.2.8 sets thresholds from them before the gate is enabled.

    Limits: IN-SAMPLE. The request kinds are the derivation's own first pass, and that derivation was
    written with this set in view. Wrong-shape 0 is therefore a FLOOR, not a rate: the kinds the gate acts on
    and the kinds the labels say are both read off the same compatibility matrix, so agreement is near-certain
    by construction, and the recall figures carry the same bias (the out-of-sample misses seen in the 2.2
    review were all ``lookup``, the kind the gate admits most broadly). Read the printed figures as "does not
    regress on these 24", never as an error rate; a held-out set is nexus-jypmj.2.8's. The library is the 12
    builtins plus one grown clone, and only the incident request reaches the clone. A builtin seeded before nexus-jypmj.2.1 and not reseeded
    declares nothing and would read as an untrusted unknown plan; the library here was seeded by this
    build, so its builtins carry their declarations.
    """
    from nexus.plans import eligibility as el
    from nexus.plans.request_coverage import derive_request_coverage

    shadow_obs: list[RunObservation] = []
    enforced_obs: list[RunObservation] = []
    rows: list[dict] = []
    for q in queries:
        sc, bd = g.request_call_shape(q)
        cov = derive_request_coverage(q.text, scope=sc)
        shadow = el.EligibilityFilter(cov.answer_kind, cov.kind_decision, enforce=False)
        sel_off = g.observe_plan_selection(library, q.text, scope=sc, bindings=bd, eligibility=shadow)
        gate = el.EligibilityFilter(cov.answer_kind, cov.kind_decision, enforce=True)
        sel_on = g.observe_plan_selection(library, q.text, scope=sc, bindings=bd, eligibility=gate)
        base = with_clone_selections[q.qid]
        assert (sel_off.selected, sel_off.candidates) == (base["selected"], base["candidates"]), (
            q.qid, "a shadow filter moved the selection", sel_off, base)
        refused = set(gate.excluded_plan_ids)
        assert not (refused & set(sel_on.plan_ids)), (q.qid, "the enforcing run offered a refused plan", refused)
        refused_labels = {key for pid, key in zip(sel_off.plan_ids, sel_off.candidates) if pid in refused}
        shadow_obs.append(RunObservation(qid=q.qid, selected_plan=sel_off.selected, candidate_plans=sel_off.candidates))
        enforced_obs.append(RunObservation(qid=q.qid, selected_plan=sel_on.selected, candidate_plans=sel_on.candidates))
        rows.append({
            "qid": q.qid.split("-")[0], "golden_kind": q.answer_kind, "derived_kind": cov.answer_kind,
            "decision": cov.kind_decision, "selected_off": sel_off.selected, "selected_on": sel_on.selected,
            "would_exclude": shadow.excluded_plan_ids, "branches_off": shadow.to_wire()["branches"],
            "excluded_total": shadow.excluded_total, "refused_labels": refused_labels,
        })
    off = compute_measures(queries, shadow_obs)
    on = compute_measures(queries, enforced_obs)
    # The same enforcing run over the library as it stands BEFORE `nx plan reseed`: builtins seeded before
    # nexus-jypmj.2.1 carry no declaration, so with the gate on each reads as an untrusted unknown plan.
    legacy_view = _LegacyBuiltins(library)
    legacy_sel: dict[str, str | None] = {}
    legacy_reasons: dict[str, int] = {}
    for q in queries:
        sc, bd = g.request_call_shape(q)
        cov = derive_request_coverage(q.text, scope=sc)
        gate = el.EligibilityFilter(cov.answer_kind, cov.kind_decision, enforce=True)
        legacy_sel[q.qid.split("-")[0]] = g.observe_plan_selection(
            legacy_view, q.text, scope=sc, bindings=bd, eligibility=gate).selected
        for branch in gate.to_wire()["branches"].values():
            for reason, n in branch["reasons"].items():
                legacy_reasons[reason] = legacy_reasons.get(reason, 0) + n
    legacy_on = {r["qid"]: r["selected_on"] for r in rows}
    print(
        f"RDR232-ELIGIBILITY-LEGACY gate_on_after_reseed_vs_before: miss={sum(1 for v in legacy_on.values() if v is None)}"
        f"->{sum(1 for v in legacy_sel.values() if v is None)} "
        f"selection_differs={[k for k in legacy_sel if legacy_sel[k] != legacy_on[k]]} reasons_before_reseed={legacy_reasons}")
    assert legacy_reasons.get("undeclared_builtin", 0) > 0, "the legacy view must have exercised the reason it reports"
    g06 = next(r for r in rows if r["qid"] == "G06")
    assert g06["selected_off"] == g.GROWN_473_KEY, g06
    assert g06["selected_on"] != g.GROWN_473_KEY, g06
    assert g06["excluded_total"] >= 1 and g06["derived_kind"] == "synthesis", g06
    changed = [r["qid"] for r in rows if r["selected_off"] != r["selected_on"]]
    # selection moves only through the filter: every changed request had a plan selected with the gate off, and
    # that plan's label is among the labels the enforcing run refused for that request
    for r in rows:
        if r["qid"] in changed:
            assert r["selected_off"] is not None, r
            assert r["selected_off"] in r["refused_labels"], (r["qid"], "the old plan was not refused", r)
    # in-sample tripwires (see Limits): the gate may not make the wrong-shape rate worse or lose recall here
    assert on["wrong_shape_admission_rate"].value <= off["wrong_shape_admission_rate"].value, (on, off)
    assert on["eligible_plan_recall"].value >= off["eligible_plan_recall"].value, (on, off)
    print(
        "RDR232-ELIGIBILITY-LIMIT in-sample: request kinds are the first pass fitted to these 24 requests; "
        "wrong_shape 0 is a floor, recall is biased the same way; the figures below are not rates"
    )
    print(
        f"RDR232-ELIGIBILITY in_sample(wrong_shape_0_is_a_floor,not_rates) requests={len(rows)} selection_changed={changed} "
        f"wrong_shape off={off['wrong_shape_admission_rate'].value} on={on['wrong_shape_admission_rate'].value} "
        f"eligible_plan_recall off={off['eligible_plan_recall'].value} on={on['eligible_plan_recall'].value} "
        f"would_exclude_any={sum(1 for r in rows if r['excluded_total'])} "
        f"miss_off={sum(1 for r in rows if r['selected_off'] is None)} "
        f"miss_on={sum(1 for r in rows if r['selected_on'] is None)}")
    for r in rows:
        print(
            f"RDR232-ELIGIBILITY[{r['qid']}] golden={r['golden_kind']} derived={r['derived_kind']}/{r['decision']} "
            f"off={r['selected_off']} on={r['selected_on']} would_exclude={r['would_exclude']} "
            f"excluded_by_branch={ {b: c['excluded'] for b, c in r['branches_off'].items()} }")
    return {"rows": rows, "off": off, "on": on}


def _emit_baseline(fx, run, queries, art, search_by_corpus, traverse_by_corpus, production) -> dict:
    """Emit the report and return the measures. Asserts non-vacuity only.

    ``measures`` are REFERENCE-LEVEL: they score the ids the vector and graph streams returned, so
    primary and facet coverage there say whether the named source's sections were retrieved at all,
    not whether the reducer was handed them (the graph stream carries a seed document's whole
    manifest, so they stay high whatever the reducer sees). ``art_reducer_input_measures`` scores the
    hydrated secondary input the run cut at the first operator and ``production_reducer_measures`` the
    reduction request of the production journey (secondary streams plus the reserved stream); the
    reducer-input leakage is the figure that says what the model was handed."""
    refs = g.evidence_from_ids(fx, run.search_ids + run.traverse_ids)
    refs_by_corpus = g.count_by_corpus(refs)
    reducer_input = g.evidence_from_contents(fx, run.vector_contents + run.graph_contents)
    reducer_by_corpus = g.count_by_corpus(reducer_input)
    print(
        f"RDR232-STREAMS vector={search_by_corpus} graph={traverse_by_corpus} "
        f"refs={len(refs)} by_corpus={refs_by_corpus} traverse_truncated={run.traverse_ids_truncated} "
        f"reducer_input={len(reducer_input)} reducer_by_corpus={reducer_by_corpus} "
        f"search_corpus={run.resolved_search_corpus!r}")

    from nexus.mcp_infra import reset_plan_cache_for_tests

    library = g.seed_builtin_plans()
    # The plan cache is process-global and refills from the library only every 90 s: start from none,
    # so the floor does not depend on what an earlier test left in it, and leave none behind.
    reset_plan_cache_for_tests()
    try:
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
        grown = _measure_with_grown_473_clone(library, queries, art, observations, selections)
    finally:
        reset_plan_cache_for_tests()

    # The ART request, scored on the reducer-input view as well (what the model is handed): once on the
    # secondary streams alone (the hydrated contents of the cut run), once on the production request,
    # recovered from the pieces of the oracle that are in the reduction request's text.
    reducer_measures = compute_measures(
        [art], [RunObservation(qid=ART_REQUEST_QID, evidence=reducer_input)])
    production_measures = compute_measures(
        [art], [RunObservation(qid=ART_REQUEST_QID, evidence=[
            fx.evidence_item(g.chash_of(t)) for _f, t in production["pieces"]])])
    prod_reducer = production["envelope"]["evidence_diagnostics"]["reducer_input"]

    # Non-vacuity of the measures themselves: they scored the fixture run, not an empty set. No
    # value is pinned here, so a later bead that moves a measure does not turn this red.
    for scored in ("primary_source_coverage", "scope_leakage", "facet_evidence_coverage"):
        assert measures[scored].n == 1 and isinstance(measures[scored].value, float), (scored, measures[scored])
    assert measures["eligible_plan_recall"].n == len(queries), (
        "every golden request must have had its candidate plans observed")
    for later in ("repair_yield_and_cost", "terminal_completeness"):
        assert isinstance(measures[later].value, NotMeasurable) and measures[later].value.phase == 3

    plan_side = g.plan_side_report(measures, grown["measures"])
    wrong_shape = plan_side["with_grown_473_clone"]["measures"]["wrong_shape_admission_rate"]
    assert wrong_shape["n"] == len(queries) and plan_side["with_grown_473_clone"]["caveat"]
    assert BASELINE_CAVEATS and plan_side["builtins_only"]["reading"]
    out = os.environ.get("NX_RDR232_BASELINE_OUT")
    if out:
        # Built only when a report is wanted: head_sha and tree_is_dirty run the VCS binary with
        # check=True, and a run with no usable repository must not fail the test.
        report = {
            "tree_sha": g.head_sha(),
            "tree_dirty": g.tree_is_dirty(),
            "golden_set_version": GOLDEN_SET_VERSION,
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
            # The builtins-only selections and measures are the FLOOR: the library holds the 12 builtins,
            # so the incident request selects research:default and scores as the right shape. The
            # grown-clone entries add plan 473's stand-in (nexus-jypmj.1.14), selected for G06 by
            # verbatim repeat: forced by construction, see its caveat.
            "plan_selection": selections,
            "plan_selection_with_grown_473_clone": grown["selections"],
            "grown_473_label_key": g.GROWN_473_KEY,
            "plan_side_measures": plan_side,
            "measures": g.measures_report(measures),
            "measures_level": "reference (ids returned by the vector and graph streams)",
            "art_reducer_input_measures": json.loads(measures_to_json(reducer_measures)),
            "production_reducer_measures": json.loads(measures_to_json(production_measures)),
            "production_reducer_leakage": {
                "out_of_scope": prod_reducer["out_of_scope"], "in_scope": prod_reducer["in_scope"],
                "unattributed": prod_reducer["unattributed"],
            },
            "production_evidence_diagnostics": production["envelope"]["evidence_diagnostics"],
            # State these wherever a number from this report is quoted.
            "caveats": list(BASELINE_CAVEATS),
        }
        Path(out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return measures, reducer_measures, production_measures


def _assert_measures_follow_containment(measures: dict, secondary_only: dict, production_measures: dict, production: dict) -> None:
    """What the stream containment did to the measures: the vector stream (nexus-jypmj.1.3) reaches
    the ART RDR, so coverage of the named source is no longer zero, and with the graph stream
    contained too (nexus-jypmj.1.5) no reference leaves the ``rdr`` scope, so leakage is zero.
    Before either, leakage was total."""
    assert measures["primary_source_coverage"].value == 1.0
    assert measures["scope_leakage"].value == 0.0, measures["scope_leakage"]
    assert measures["facet_evidence_coverage"].value > 0.0
    # nexus-jypmj.1.8: those are reference-level and the graph stream's seed manifests keep them high.
    # What the model is handed is the reducer input: zero leakage in the production request, and the
    # named source's facets reach it through the reserved stream, which the hydrated secondary input
    # alone (six vector hits, four graph hits) did not deliver.
    r = production["envelope"]["evidence_diagnostics"]["reducer_input"]
    assert r["out_of_scope"] == 0 and r["in_scope"] > 0, r
    assert production_measures["primary_source_coverage"].value == 1.0
    assert production_measures["facet_evidence_coverage"].value == 1.0
    sec_facets = secondary_only["facet_evidence_coverage"].value
    assert sec_facets < production_measures["facet_evidence_coverage"].value, (
        f"the reserved stream adds facets the secondary streams did not hand the reducer: {sec_facets}")
    print(f"RDR232-COVERAGE reference_primary={measures['primary_source_coverage'].value} "
          f"reference_facet={measures['facet_evidence_coverage'].value} reference_leakage={measures['scope_leakage'].value} "
          f"secondary_reducer_facet={sec_facets} secondary_reducer_primary={secondary_only['primary_source_coverage'].value} "
          f"secondary_reducer_leakage={secondary_only['scope_leakage'].value} "
          f"production_facet={production_measures['facet_evidence_coverage'].value} "
          f"production_primary={production_measures['primary_source_coverage'].value} "
          f"production_leakage_out_of_scope={r['out_of_scope']} of_in_scope={r['in_scope']}")


def test_rdr084_fixture_containment_and_baseline_emission(t2_service_env) -> None:
    queries = load_golden_set(g.GOLDEN_PATH)
    art = next(q for q in queries if q.qid == ART_REQUEST_QID)

    fx = g.build_rdr084_fixture()
    reader = _assert_fixture_loads(fx)

    scope, bindings = g.request_call_shape(art)
    run = asyncio.run(g.run_lookup_retrieval(art.text, scope=scope, bindings=bindings))
    search_by_corpus = _assert_vector_stream_contained(fx, run)
    primary, resolution = _assert_named_source_and_primary_evidence(fx, reader)
    life = _assert_t2_lifecycle_stream(fx, reader)
    _assert_primary_stream_reaches_the_reducer(fx, run, primary)
    traverse_by_corpus = _assert_graph_stream_contained(fx, run)
    kept_roles = _assert_traversal_policy_exercised(fx, run)
    oversized = _assert_oversized_input_keeps_the_reserved_stream(fx, art, primary, resolution, life)

    # The production journey: the golden request's own call shape, its required source mapped through
    # SourceRef.parse_labeled to a source binding plus the owner of its project label.
    source_bindings, label_only = g.golden_source_bindings(art, {g.ART_PROJECT: fx.art_owner})
    assert source_bindings == {"source": "RDR-084", "owner": fx.art_owner} and label_only == []
    call_bindings = {**(bindings or {}), **source_bindings}
    # plan 473's shape (the builtin lookup plan), the walk reaching the secondary documents ...
    production = _assert_production_wiring_reaches_the_reducer(
        fx, art, scope, call_bindings, primary, resolution, strategy=("lookup", "hybrid-factual-lookup"),
        expect_dropped_cap=g.SECONDARY_DOCS - 50)
    # ... a taxonomy-domain builtin, which is what the matcher picks for this request (two searches
    # pinned `corpus: all`, one with a prose and one with a code domain, both narrowed to rdr) ...
    _assert_production_wiring_reaches_the_reducer(
        fx, art, scope, call_bindings, primary, resolution, strategy=None, expect_dropped_cap=0)
    # ... and the single_query fast path.
    _assert_single_query_fast_path_is_contained(fx, art, scope)

    measures, secondary_only, production_measures = _emit_baseline(
        fx, run, queries, art, search_by_corpus, traverse_by_corpus, production)
    _assert_measures_follow_containment(measures, secondary_only, production_measures, production)
    print(f"RDR232-PHASE1 kept_secondary_roles={kept_roles} oversized_wrong_source_in_reducer="
          f"{oversized['reducer_input']['by_stream_collection']['vector'].get(fx.nexus_rdr_collection, 0)}")
