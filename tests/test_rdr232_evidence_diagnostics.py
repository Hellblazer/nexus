# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.8): deterministic source and scope checks, per-stream and
reducer-input diagnostics.

Tenant-free: the pure builder (``nexus.plans.evidence_diagnostics``), the runner seam that records
the reducer input (``_hydrate_operator_args``, ``plan_run`` with stubbed tools) and the ``nx_answer``
wiring through the same patched seams as ``tests/test_rdr232_caller_scope.py``. The substrate journey
is the golden fixture (``tests/test_rdr232_golden_fixture.py``). Each test names the claim it pins.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.plans.evidence import ScopeConstraint, SourceRef
from nexus.plans.lifecycle_stream import LifecycleCaps, LifecycleRecord, LifecycleStream, SourceLifecycle
from nexus.plans.primary_evidence import (
    DocumentCoverage,
    NamedSourceResolution,
    PrimaryEvidence,
    ResolvedSource,
    SelectedChunk,
)
from nexus.plans.reserved_stream import ReservedItem, ReservedStream, reserved_scope, set_reserved_step
from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args
from tests.test_rdr232_reserved_merge import ids_hydration  # noqa: F401  (a fixture)

RDR_COLL = "rdr__1-1__bge-base-en-v15-768__v1"
CODE_COLL = "code__1-1__bge-base-en-v15-768__v1"
DOC = "1.1.9"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _search_out(texts: list[str], collections: list[str]) -> dict:
    ids = [_h(t) for t in texts]
    return {"ids": ids, "tumblers": [f"1.1.{i}" for i in range(len(ids))], "collections": sorted(set(collections)),
            "chunk_collections": list(collections), "chunk_text_hash": list(ids), "distances": [0.1] * len(ids)}


def _traverse_out(texts: list[str], collections: list[str], **report) -> dict:
    out = _search_out(texts, collections)
    out.pop("distances")
    out["traversal"] = {"policy_applied": True, "policy_failed": False, "possibly_truncated": False,
                        "contained": True, "nodes_walked": 3, "nodes_retained": 2, "dropped_out_of_scope": 0,
                        "dropped_role": 0, "dropped_cap": 0, "widened": None, **report}
    return out


def _chunk(i: int, *, doc: str = DOC, collection: str = RDR_COLL, source: str = "RDR-084") -> SelectedChunk:
    return SelectedChunk(
        source=source, doc_id=doc, position=i, chash=_h(f"PRIMARY TEXT {i}"), collection=collection,
        label=f"Section {i}", label_source="content", text=f"PRIMARY TEXT {i}",
    )


def _resolution(*, status: str = "resolved", doc: str | None = DOC) -> NamedSourceResolution:
    return NamedSourceResolution(sources=(ResolvedSource(
        ref=SourceRef.parse("RDR-084"), status=status, tumbler=doc if status == "resolved" else None,
        title="RDR-084", qualified=True, rdr_number=84 if status == "resolved" else None,
    ),))


def _coverage(*, rows: int, selected: int, partial: bool | None = None, status: str = "ok") -> DocumentCoverage:
    return DocumentCoverage(
        source="RDR-084", doc_id=DOC, title="RDR-084", status=status, manifest_rows=rows, selected_rows=selected,
        partial=(selected < rows) if partial is None else partial,
    )


def _primary(n: int, *, rows: int | None = None, partial: bool | None = None) -> PrimaryEvidence:
    return PrimaryEvidence(
        items=tuple(_chunk(i) for i in range(n)), documents=(_coverage(rows=rows or n, selected=n, partial=partial),))


def _lifecycle(status: str, *, detail: str = "", records: int = 0) -> LifecycleStream:
    recs = tuple(
        LifecycleRecord(source="RDR-084", project="art_rdr", title=t, kind=k, text=f"{k} body", chars_full=9, truncated=False, kept=9)
        for t, k in (("084", "metadata"), ("084-gate-latest", "gate-latest"))[:records])
    return LifecycleStream(
        records=recs, numbers=(84,) * len(recs), reads=3,
        sources=(SourceLifecycle("RDR-084", status, project="art_rdr", rdr_number=84, detail=detail),))  # type: ignore[arg-type]


def _build(**kw):
    from nexus.plans.evidence_diagnostics import build_evidence_diagnostics

    kw.setdefault("steps", [])
    kw.setdefault("tools", [])
    kw.setdefault("scope", ScopeConstraint.parse(""))
    kw.setdefault("scope_diagnostics", [])
    return build_evidence_diagnostics(**kw)


def _recorder(items: list[str], step: int = 4):
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder

    rec = ReducerInputRecorder()
    rec.set_step(step)
    rec.note(items)
    return rec


# ── 1. the runner seam records the reducer input, and changes nothing it hands the operator ──────────────────


def test_ids_hydration_records_the_final_reducer_input(ids_hydration) -> None:  # noqa: F811
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope

    rec = ReducerInputRecorder()
    ids = [f"{0xB000 + i:064x}" for i in range(3)]
    with reducer_input_scope(rec):
        rec.set_step(2)
        _, args = _hydrate_operator_args("rank", {"criterion": "c", "ids": ids, "collections": "rdr"})
    assert list(rec.items or ()) == json.loads(args["items"]) == ["secondary 0", "secondary 1", "secondary 2"]
    assert rec.step == 2


def test_explicit_inputs_record_after_the_reserved_merge_and_cap() -> None:
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope

    stream = ReservedStream.from_evidence(PrimaryEvidence(items=tuple(_chunk(i) for i in range(5))))
    rec = ReducerInputRecorder()
    inputs = [f"secondary {k}" for k in range(_OPERATOR_MAX_INPUTS + 20)]
    with reducer_input_scope(rec), reserved_scope(stream):
        rec.set_step(4)
        set_reserved_step(4)
        _, args = _hydrate_operator_args("rank", {"criterion": "c", "inputs": inputs})
    sent = json.loads(args["items"])
    assert list(rec.items or ()) == sent and len(sent) == _OPERATOR_MAX_INPUTS
    assert sent[:5] == [i.text for i in stream.items], "the recorded input is what the operator received, reserved first"


def test_extract_explicit_inputs_record() -> None:
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope

    rec = ReducerInputRecorder()
    with reducer_input_scope(rec):
        rec.set_step(2)
        _, args = _hydrate_operator_args("extract", {"inputs": ["a", "", "b"], "fields": "decision"})
    assert list(rec.items or ()) == ["a", "b"], "the operator's evidence, without the empty entries"
    assert args["inputs"] == ["a", "", "b"], "and the operator still receives exactly what it did before"


@pytest.mark.parametrize("shape", ["ids", "inputs"])
def test_recording_changes_nothing_the_operator_receives(shape: str, ids_hydration) -> None:  # noqa: F811
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope

    def args():
        if shape == "ids":
            return {"criterion": "c", "ids": [f"{0xC000 + i:064x}" for i in range(4)], "collections": "rdr"}
        return {"criterion": "c", "inputs": ["x", "y", "z"]}

    _, bare = _hydrate_operator_args("rank", args())
    rec = ReducerInputRecorder()
    with reducer_input_scope(rec):
        rec.set_step(1)
        _, recorded = _hydrate_operator_args("rank", args())
    assert recorded == bare, "byte-identical to a run with no recorder"
    assert rec.items


def test_first_consuming_step_wins_a_rehydration_of_it_replaces_and_later_steps_are_ignored() -> None:
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope

    rec = ReducerInputRecorder()
    with reducer_input_scope(rec):
        rec.set_step(4)
        _hydrate_operator_args("rank", {"criterion": "c", "inputs": ["a", "b"]})
        rec.set_step(4)
        _hydrate_operator_args("rank", {"criterion": "c", "inputs": ["a", "b", "c"]})
        assert list(rec.items or ()) == ["a", "b", "c"], "a re-hydration of the recording step replaces it"
        rec.set_step(5)
        _hydrate_operator_args("generate", {"inputs": ["only the ranked"], "instruction": "i"})
    assert list(rec.items or ()) == ["a", "b", "c"] and rec.step == 4, "a later operator is not the reducer input"


def test_no_active_recorder_is_a_no_op() -> None:
    from nexus.plans.evidence_diagnostics import active_recorder

    assert active_recorder() is None
    _hydrate_operator_args("rank", {"criterion": "c", "inputs": ["a"]})  # must not raise
    assert active_recorder() is None


@pytest.mark.asyncio
async def test_plan_run_marks_the_step_and_resets_the_recorder_per_run() -> None:
    from nexus.mcp import core as mcp_core
    from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope
    from nexus.plans.match import Match
    from nexus.plans.runner import plan_run

    async def stub_search(**kwargs):  # noqa: ARG001
        return {"ids": [_h("hit")], "tumblers": ["1.1"], "distances": [0.1], "collections": [RDR_COLL],
                "chunk_text_hash": [_h("hit")], "chunk_collections": [RDR_COLL]}

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": ["hit" for _ in ids]}

    async def stub_rank(**kwargs):
        return {"ranked": [], "text": "r"}

    plan = {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr"}},
        {"tool": "rank", "args": {"criterion": "c", "inputs": "$step2.contents"}},
    ]}
    match = Match(plan_id=9, name="p", description="p", confidence=1.0, dimensions={}, tags="",
                  plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[],
                  default_bindings={}, parent_dims=None)
    rec = ReducerInputRecorder()
    rec.note(["stale from a previous run"])
    with patch.object(mcp_core, "search", stub_search), patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_rank", stub_rank), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         reducer_input_scope(rec):
        await plan_run(match, {}, bundle_operators=False)
    assert list(rec.items or ()) == ["hit"], "plan_run reset the previous run's input"
    assert rec.step == 2, "recorded at the rank step (0-based index 2)"


# ── 2. provenance: a reserved item carries its stream and collection ─────────────────────────────────────


def test_reserved_items_carry_stream_collection_document_and_source() -> None:
    stream = ReservedStream.from_evidence(
        PrimaryEvidence(items=(_chunk(0),)), extra=_lifecycle("read", records=2).reserved_items())
    assert stream is not None
    t2 = [i for i in stream.items if i.stream == "t2"]
    chunks = [i for i in stream.items if i.stream == "primary"]
    assert len(t2) == 2 and len(chunks) == 1
    assert all(i.token.startswith("primary:t2:") and i.source == "RDR-084" for i in t2), (
        "a lifecycle record is identifiable by its token prefix and its stream field alike")
    assert (chunks[0].collection, chunks[0].doc_id, chunks[0].source) == (RDR_COLL, DOC, "RDR-084")
    assert ReservedItem.record("x", "body").stream == "t2"


# ── 3. the builder: per-stream corpus counts ──────────────────────────────────────────────────────────────


def test_stream_counts_by_collection_for_the_vector_and_graph_streams() -> None:
    steps = [
        _search_out(["v0", "v1", "v2"], [RDR_COLL, RDR_COLL, CODE_COLL]),
        _traverse_out(["g0", "g1"], [RDR_COLL, RDR_COLL]),
        {"contents": ["v0"]},
    ]
    d = _build(steps=steps, tools=["search", "traverse", "store_get_many"], scope=ScopeConstraint.parse("rdr"),
               resolved_args=[{"step_index": 0, "tool": "search", "corpus": "rdr", "query": "q"}])
    v, g = d["streams"]["vector"], d["streams"]["graph"]
    assert (v["references"], v["by_collection"]) == (3, {RDR_COLL: 2, CODE_COLL: 1})
    assert (v["in_scope"], v["out_of_scope"]) == (2, 1)
    assert v["steps"] == [{"step_index": 0, "tool": "search", "corpus": "rdr"}]
    assert (g["references"], g["by_collection"], g["out_of_scope"]) == (2, {RDR_COLL: 2}, 0)
    assert d["streams"]["primary"]["references"] == 0 and d["streams"]["t2"]["references"] == 0
    json.dumps(d)  # the envelope is JSON


def test_a_chash_in_two_steps_of_one_stream_counts_once() -> None:
    steps = [_search_out(["a", "b"], [RDR_COLL] * 2), _search_out(["b", "c"], [RDR_COLL] * 2)]
    d = _build(steps=steps, tools=["search", "search"])
    assert d["streams"]["vector"]["references"] == 3


def test_an_output_without_per_chunk_collections_is_unattributed_not_guessed() -> None:
    out = {"ids": [_h("a"), _h("b")], "collections": [RDR_COLL, CODE_COLL]}
    d = _build(steps=[out], tools=["search"], scope=ScopeConstraint.parse("rdr"))
    assert d["streams"]["vector"]["by_collection"] == {"": 2}, "two candidate collections, no alignment: unattributed"
    assert d["streams"]["vector"]["in_scope"] is None and d["streams"]["vector"]["out_of_scope"] is None


def test_primary_and_t2_streams_count_what_was_read() -> None:
    stream = ReservedStream.from_evidence(_primary(3), extra=_lifecycle("read", records=2).reserved_items())
    d = _build(reserved=stream, primary=_primary(3), lifecycle=_lifecycle("read", records=2))
    assert d["streams"]["primary"]["references"] == 3
    assert d["streams"]["primary"]["by_collection"] == {RDR_COLL: 3}
    assert d["streams"]["t2"] == {"references": 2, "by_project": {"art_rdr": 2}}


# ── 4. the reducer input: counts by stream and collection ──────────────────────────────────────────────────


def test_reducer_input_counts_by_stream_and_collection_with_provenance_for_every_kind_of_item() -> None:
    primary = _primary(2)
    life = _lifecycle("read", records=2)
    stream = ReservedStream.from_evidence(primary, extra=life.reserved_items())
    vec_texts, graph_texts = ["vector one", "vector two"], ["graph one"]
    steps = [_search_out(vec_texts, [RDR_COLL, CODE_COLL]), _traverse_out(graph_texts, [RDR_COLL])]
    items = [i.text for i in stream.items] + ["[source: Doc · " + RDR_COLL + "]\nvector one", "vector two", "graph one",
                                               "a manifest-joined document the index cannot attribute"]
    d = _build(steps=steps, tools=["search", "traverse"], scope=ScopeConstraint.parse("rdr"),
               reserved=stream, primary=primary, lifecycle=life, recorder=_recorder(items))
    r = d["reducer_input"]
    assert r["items"] == 8 and r["step"] == 4
    assert r["by_stream"] == {"t2": 2, "primary": 2, "vector": 2, "graph": 1, "unattributed": 1}
    assert r["by_stream_collection"]["vector"] == {RDR_COLL: 1, CODE_COLL: 1}, "the source note is not part of the hash"
    assert r["by_stream_collection"]["primary"] == {RDR_COLL: 2}
    assert r["by_collection"] == {RDR_COLL: 4, CODE_COLL: 1}
    assert (r["in_scope"], r["out_of_scope"], r["unattributed"]) == (4, 1, 1)


def test_no_evidence_consuming_operator_means_no_reducer_input_and_says_so() -> None:
    d = _build(steps=[_search_out(["a"], [RDR_COLL])], tools=["search"])
    assert d["reducer_input"] is None


# ── 5. the deterministic source check ──────────────────────────────────────────────────────────────────────


def _source_state(*, primary, stream, recorder, resolution=None, lifecycle=None) -> dict:
    d = _build(named_sources=resolution or _resolution(), primary=primary, reserved=stream, recorder=recorder,
               lifecycle=lifecycle)
    return d["source_check"]


def _reached(primary: PrimaryEvidence, n: int):
    stream = ReservedStream.from_evidence(primary)
    return stream, _recorder([i.text for i in stream.items][:n] + ["secondary"])


def test_source_covered_when_every_selected_chunk_reached_the_reducer() -> None:
    p = _primary(4)
    stream, rec = _reached(p, 4)
    sc = _source_state(primary=p, stream=stream, recorder=rec)
    assert sc["state"] == "covered"
    (s,) = sc["sources"]
    assert (s["state"], s["selected"], s["reached_reducer"], s["manifest_rows"]) == ("covered", 4, 4, 4)


def test_source_partial_when_the_document_is_over_budget() -> None:
    p = _primary(4, rows=11)  # 11 chunks in the document, the budget kept 4
    stream, rec = _reached(p, 4)
    sc = _source_state(primary=p, stream=stream, recorder=rec)
    assert sc["state"] == "partial" and "4 of 11" in sc["sources"][0]["detail"]


def test_source_partial_when_only_some_selected_chunks_reached_the_reducer() -> None:
    p = _primary(4)
    stream, rec = _reached(p, 2)
    sc = _source_state(primary=p, stream=stream, recorder=rec)
    assert (sc["state"], sc["sources"][0]["reached_reducer"]) == ("partial", 2)


def test_source_missing_when_the_primary_is_absent_from_the_reducer() -> None:
    p = _primary(3)
    stream = ReservedStream.from_evidence(p)
    sc = _source_state(primary=p, stream=stream, recorder=_recorder(["only secondary evidence"]))
    assert sc["state"] == "missing" and sc["sources"][0]["reached_reducer"] == 0


@pytest.mark.parametrize(
    ("resolution", "why"),
    [
        pytest.param(_resolution(status="unresolved"), "unresolved", id="unresolved"),
        pytest.param(_resolution(status="candidates"), "candidates", id="ambiguous"),
    ],
)
def test_source_missing_when_it_did_not_resolve(resolution, why) -> None:
    sc = _source_state(primary=None, stream=None, recorder=_recorder(["x"]), resolution=resolution)
    assert sc["state"] == "missing" and why in sc["sources"][0]["detail"]


def test_source_missing_when_nothing_was_read_for_it() -> None:
    """A resolved source with no primary evidence (the single_query fast path, a failed read)."""
    sc = _source_state(primary=None, stream=None, recorder=None)
    assert sc["state"] == "missing" and "no primary evidence" in sc["sources"][0]["detail"]


def test_source_missing_when_the_stream_was_never_merged_into_a_reduction() -> None:
    p = _primary(3)
    stream = ReservedStream.from_evidence(p)  # merged stays False
    sc = _source_state(primary=p, stream=stream, recorder=None)
    assert sc["state"] == "missing" and sc["reducer_input_recorded"] is False


def test_no_named_source_means_not_applicable() -> None:
    assert _build()["source_check"] == {"state": "not_applicable", "sources": [], "reducer_input_recorded": False,
                                         "unresolved": []}


# ── 6. lifecycle statuses are facet unresolved, not silent absence ────────────────────────────────────────


@pytest.mark.parametrize("status", ["no_records", "project_undetermined", "read_capped", "read_failed", "over_cap"])
def test_a_lifecycle_that_delivered_nothing_is_unresolved(status: str) -> None:
    p = _primary(2)
    stream = ReservedStream.from_evidence(p)
    sc = _source_state(primary=p, stream=stream, recorder=_recorder([i.text for i in stream.items]),
                       lifecycle=_lifecycle(status, detail="because"))
    (s,) = sc["sources"]
    assert s["lifecycle"] == {"state": "unresolved", "status": status, "detail": "because", "delivered": 0, "reached_reducer": 0}
    assert sc["unresolved"] == [{"source": "RDR-084", "facet": "lifecycle", "status": status}]


@pytest.mark.parametrize("status", ["not_rdr", "duplicate", "not_resolved"])
def test_a_lifecycle_that_does_not_apply_is_not_applicable(status: str) -> None:
    p = _primary(2)
    stream = ReservedStream.from_evidence(p)
    sc = _source_state(primary=p, stream=stream, recorder=_recorder([i.text for i in stream.items]),
                       lifecycle=_lifecycle(status))
    assert sc["sources"][0]["lifecycle"]["state"] == "not_applicable" and sc["unresolved"] == []


def test_a_lifecycle_read_with_both_records_is_resolved_and_one_absent_is_partial() -> None:
    p = _primary(2)
    full = ReservedStream.from_evidence(p, extra=_lifecycle("read", records=2).reserved_items())
    ok = _source_state(primary=p, stream=full, recorder=_recorder([i.text for i in full.items]),
                       lifecycle=_lifecycle("read", records=2))
    assert ok["sources"][0]["lifecycle"]["state"] == "resolved" and ok["sources"][0]["lifecycle"]["reached_reducer"] == 2
    one = ReservedStream.from_evidence(p, extra=_lifecycle("read", records=1).reserved_items())
    part = _source_state(primary=p, stream=one, recorder=_recorder([i.text for i in one.items]),
                         lifecycle=_lifecycle("read", detail="no gate record", records=1))
    assert part["sources"][0]["lifecycle"]["state"] == "partial"
    assert part["unresolved"] == [{"source": "RDR-084", "facet": "lifecycle", "status": "read"}]


def test_a_lifecycle_record_that_did_not_reach_the_reducer_is_partial() -> None:
    p = _primary(2)
    stream = ReservedStream.from_evidence(p, extra=_lifecycle("read", records=2).reserved_items())
    items = [i.text for i in stream.items if i.stream == "primary"]  # both records lost between stream and reducer
    sc = _source_state(primary=p, stream=stream, recorder=_recorder(items), lifecycle=_lifecycle("read", records=2))
    lc = sc["sources"][0]["lifecycle"]
    assert (lc["state"], lc["delivered"], lc["reached_reducer"]) == ("partial", 2, 0)


# ── 7. the deterministic scope check ───────────────────────────────────────────────────────────────────────


def _scope_check(scope: str, steps, tools, *, scope_diagnostics=(), recorder=None, reserved=None, primary=None):
    return _build(steps=steps, tools=tools, scope=ScopeConstraint.parse(scope), scope_diagnostics=list(scope_diagnostics),
                  recorder=recorder, reserved=reserved, primary=primary)["scope_check"]


def test_scope_satisfied_when_every_stream_and_the_reducer_input_is_inside() -> None:
    sc = _scope_check("rdr", [_search_out(["a", "b"], [RDR_COLL] * 2)], ["search"],
                      recorder=_recorder(["a", "b"]))
    assert (sc["state"], sc["scope"], sc["out_of_scope"]) == ("satisfied", "rdr", 0)


def test_scope_violated_when_an_out_of_scope_collection_reaches_the_reducer_without_a_reason() -> None:
    sc = _scope_check("rdr", [_search_out(["a", "leak"], [RDR_COLL, CODE_COLL])], ["search"],
                      recorder=_recorder(["a", "leak"]))
    assert sc["state"] == "violated"
    assert sc["reducer_out_of_scope"] == 1 and sc["streams_out_of_scope"] == {"vector": 1}
    assert sc["violations"] == [{"stream": "vector", "collection": CODE_COLL, "in_reducer": 1}]


def test_scope_violated_by_a_stream_reference_even_if_the_reducer_dropped_it() -> None:
    sc = _scope_check("rdr", [_search_out(["a", "leak"], [RDR_COLL, CODE_COLL])], ["search"],
                      recorder=_recorder(["a"]))
    assert sc["state"] == "violated" and sc["reducer_out_of_scope"] == 0 and sc["streams_out_of_scope"] == {"vector": 1}


def test_scope_conflict_when_a_step_was_skipped_for_it() -> None:
    skipped = {"step_index": 0, "tool": "search", "plan_corpus": "code", "caller_scope": "rdr",
               "scope_kind": "corpus_prefix", "outcome": "conflict", "resolved_corpus": None}
    steps = [{"ids": [], "scope_conflict": {"step_index": 0}}, _search_out(["a"], [RDR_COLL])]
    sc = _scope_check("rdr", steps, ["search", "search"], scope_diagnostics=[skipped], recorder=_recorder(["a"]))
    assert sc["state"] == "conflict" and sc["conflict_steps"] == [0]


def test_scope_widened_when_a_named_source_sits_outside_the_scope_by_request() -> None:
    p = PrimaryEvidence(items=(_chunk(0, collection=CODE_COLL),), documents=(_coverage(rows=1, selected=1),))
    stream = ReservedStream.from_evidence(p)
    sc = _scope_check("rdr", [], [], recorder=_recorder([i.text for i in stream.items]), reserved=stream, primary=p)
    assert sc["state"] == "widened"
    assert sc["recorded_reasons"] == [{"stream": "primary", "reason": "named_source", "collection": CODE_COLL, "count": 1}]
    assert sc["reducer_out_of_scope"] == 1, "counted, with its reason, not hidden"


def test_scope_widened_by_a_traversal_that_recorded_its_reason_when_no_scope_was_given() -> None:
    out = _traverse_out(["g"], [CODE_COLL], widened={"reason": "no caller scope: implementation links admitted", "corpus_counts": {"code": 1}})
    sc = _scope_check("", [out], ["traverse"])
    assert sc["state"] == "not_applicable", "no explicit scope, so nothing to satisfy"
    assert sc["recorded_reasons"] and sc["recorded_reasons"][0]["reason"] == "traversal_widened"


def test_scope_not_applicable_without_a_scope_and_not_evaluated_for_a_subtree() -> None:
    assert _scope_check("", [_search_out(["a"], [RDR_COLL])], ["search"])["state"] == "not_applicable"
    sub = _scope_check("1.2", [_search_out(["a"], [RDR_COLL])], ["search"])
    assert sub["state"] == "not_evaluated" and "subtree" in sub["detail"]


def test_unattributed_reducer_items_are_counted_and_flagged_not_assumed_inside() -> None:
    sc = _scope_check("rdr", [_search_out(["a"], [RDR_COLL])], ["search"], recorder=_recorder(["a", "mystery"]))
    assert sc["state"] == "satisfied" and sc["reducer_unattributed"] == 1


# ── 8. skipped steps, traversals, capped traversals ────────────────────────────────────────────────────────


def test_skipped_steps_traversals_and_capped_traversals_feed_the_diagnostics() -> None:
    skipped_out = {"ids": [], "tumblers": [], "scope_conflict": {
        "step_index": 0, "tool": "search", "plan_corpus": "code", "caller_scope": "rdr", "scope_kind": "corpus_prefix"}}
    capped = _traverse_out(["g0"], [RDR_COLL], possibly_truncated=True, contained=False, nodes_walked=500,
                           dropped_out_of_scope=120, dropped_cap=4)
    steps = [skipped_out, capped, _traverse_out(["g1"], [RDR_COLL]), {"text": "x"}]
    d = _build(steps=steps, tools=["search", "traverse", "traverse", "rank"], scope=ScopeConstraint.parse("rdr"))
    assert d["skipped_steps"] == [{"step_index": 0, "tool": "search", "reason": "scope_conflict",
                                   "plan_corpus": "code", "caller_scope": "rdr"}]
    assert d["capped_traversals"] == 1
    t = d["traversals"]
    assert [x["step_index"] for x in t] == [1, 2] and t[0]["possibly_truncated"] is True
    assert (t[0]["dropped_out_of_scope"], t[0]["dropped_cap"], t[0]["nodes_walked"]) == (120, 4, 500)
    assert d["streams"]["graph"]["references"] == 2, "a skipped step contributes no references"


def test_reserved_summary_carries_the_runner_report() -> None:
    stream = ReservedStream.from_evidence(_primary(3))
    stream.over_share_dropped, stream.secondary_cut, stream.capped, stream.deduped_secondary = 2, 5, True, 1
    stream.last_observed = {"step": 5, "missing": 1, "of": 3}
    r = _build(reserved=stream)["reserved"]
    assert r == {"items": 3, "over_share_dropped": 2, "deduped_secondary": 1, "secondary_cut": 5, "capped": True,
                 "merged": False, "dropped_before_terminal": {"step": 5, "missing": 1, "of": 3}}


def test_without_a_stream_the_reserved_summary_is_none() -> None:
    assert _build()["reserved"] is None


# ── 9. logs: ids and counts, never question text ───────────────────────────────────────────────────────────


def test_the_log_event_is_flat_ids_and_counts_without_any_text() -> None:
    from nexus.plans.evidence_diagnostics import log_evidence_diagnostics

    p = _primary(2)
    stream = ReservedStream.from_evidence(p, extra=_lifecycle("no_records", detail="private note").reserved_items())
    d = _build(named_sources=_resolution(), primary=p, reserved=stream, recorder=_recorder([i.text for i in stream.items]),
               lifecycle=_lifecycle("no_records", detail="private note"), scope=ScopeConstraint.parse("rdr"),
               steps=[_search_out(["a"], [RDR_COLL])], tools=["search"])
    with patch("nexus.plans.evidence_diagnostics._log") as lg:
        log_evidence_diagnostics(d, plan_id=7)
    ((name,), event) = lg.info.call_args
    assert name == "nx_answer_evidence_diagnostics"
    blob = json.dumps(event, default=str)
    assert event["plan_id"] == 7 and event["reducer_items"] == 2 and event["vector_refs"] == 1
    assert event["source_state"] == "covered" and event["scope_state"] == "satisfied"
    assert event["lifecycle_unresolved"] == 1
    for text in ("private note", "PRIMARY TEXT", "RDR-084", "body"):
        assert text not in blob, f"{text!r} leaked into the log event"


# ── 10. nx_answer wiring ───────────────────────────────────────────────────────────────────────────────────

from tests.test_rdr232_caller_scope import (  # noqa: E402
    _MULTI_PLAN,
    _drive_nx_answer,
    _plan_run_result,
    _single_query_match,
)


def test_the_plan_path_envelope_carries_evidence_diagnostics() -> None:
    steps = [_search_out(["a", "b"], [RDR_COLL, RDR_COLL]), _search_out(["c"], [RDR_COLL]),
             {"text": "The decision is X [1]. It was accepted on 2026-10-01 after review of the evidence."}]
    env, _ = _drive_nx_answer(_MULTI_PLAN, scope="rdr", structured=True, plan_run_result=_plan_run_result(steps, []))
    d = env["evidence_diagnostics"]
    assert d["streams"]["vector"]["references"] == 3 and d["streams"]["vector"]["by_collection"] == {RDR_COLL: 3}
    assert d["scope_check"]["state"] == "satisfied" and d["source_check"]["state"] == "not_applicable"
    assert d["reducer_input"] is None
    json.dumps(env["evidence_diagnostics"])


def test_a_skipped_step_has_no_step_record_and_the_diagnostics_count_it() -> None:
    """Decision: a step skipped for a scope conflict has no StepRecord or telemetry row (it dispatched
    nothing and cost nothing; a row would change what the run table persists). The diagnostics count it, so
    the envelope's step records and its planned-minus-skipped steps can be reconciled."""
    from tests.test_rdr232_caller_scope import CONFLICT_DIAG, RAN_DIAG

    steps = [{"ids": [], "tumblers": [], "scope_conflict": {"step_index": 0, "tool": "search", "plan_corpus": "code",
                                                              "caller_scope": "rdr", "scope_kind": "corpus_prefix"}},
             _search_out(["c1"], [RDR_COLL]),
             {"text": "The decision is X [1]. It was accepted on 2026-10-01 after review of the evidence."}]
    env, _ = _drive_nx_answer(_MULTI_PLAN, scope="rdr", structured=True,
                              plan_run_result=_plan_run_result(steps, [CONFLICT_DIAG, RAN_DIAG]))
    d = env["evidence_diagnostics"]
    assert [s["step_index"] for s in d["skipped_steps"]] == [0]
    assert env["steps"] == [], "no StepRecord was synthesised for the skipped step"
    assert d["scope_check"]["state"] == "conflict"


def test_the_fast_path_envelope_carries_the_vector_stream_counts() -> None:
    env, _ = _drive_nx_answer(_single_query_match("knowledge,code,docs,rdr"), scope="rdr", structured=True)
    d = env["evidence_diagnostics"]
    assert d["streams"]["vector"]["references"] == 1 and d["streams"]["vector"]["out_of_scope"] == 0
    assert d["reducer_input"] is None and d["scope_check"]["state"] == "satisfied"


def test_the_fast_path_conflict_reports_a_conflict_and_no_references() -> None:
    env, calls = _drive_nx_answer(_single_query_match("code"), scope="rdr", structured=True)
    assert calls == []
    d = env["evidence_diagnostics"]
    assert d["scope_check"]["state"] == "conflict" and d["streams"]["vector"]["references"] == 0
    assert [s["step_index"] for s in d["skipped_steps"]] == [0]


def _drive_with_run(run, *, question: str = "which documents discuss widgets", trace: bool = True, bindings=None):
    """nx_answer on the multi-step plan with ``plan_run`` replaced by *run*; returns the envelope."""
    import asyncio

    import nexus.mcp.core as core

    db_stub = MagicMock(plans=MagicMock())
    db_stub.conn = MagicMock()
    with patch("nexus.plans.matcher.plan_match", return_value=[_MULTI_PLAN]), \
         patch("nexus.mcp.core._get_catalog", return_value=None), \
         patch("nexus.mcp.core._t2_ctx") as ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
         patch("nexus.plans.runner.plan_run", run):
        ctx.return_value.__enter__.return_value = db_stub
        return asyncio.run(core.nx_answer(question=question, scope="rdr", structured=True, trace=trace, bindings=bindings))


def test_a_run_that_failed_before_any_result_has_the_key_and_no_diagnostics() -> None:
    env = _drive_with_run(AsyncMock(side_effect=RuntimeError("boom")))
    assert env["answer_shape"] == "error" and env["evidence_diagnostics"] is None


def test_trace_false_logs_ids_and_counts_never_the_question() -> None:
    private = "what did Alice Wonderland say about the secret merger"
    steps = [_search_out(["a"], [RDR_COLL]), {"text": "The decision is X [1]. It was accepted on 2026-10-01 after review."}]
    with patch("nexus.plans.evidence_diagnostics._log") as lg:
        _drive_with_run(AsyncMock(return_value=_plan_run_result(steps, [])), question=private, trace=False,
                        bindings={"source": "RDR-084"})
    events = [c for c in lg.info.call_args_list if c.args and c.args[0] == "nx_answer_evidence_diagnostics"]
    assert len(events) == 1
    blob = json.dumps(events[0].kwargs, default=str)
    assert "Wonderland" not in blob and "secret merger" not in blob and "RDR-084" not in blob


@pytest.mark.asyncio
async def test_a_headless_nx_answer_run_records_the_reducer_input_the_operator_was_handed() -> None:
    """The real ``plan_run`` under ``nx_answer`` (no continuation): the recorder ``nx_answer`` scopes
    around the run sees the summarize step's hydrated input, and the envelope counts it by stream."""
    from nexus.mcp import core as mcp_core
    from nexus.mcp.core import nx_answer

    text = "the secondary hit"

    async def stub_search(**kwargs):  # noqa: ARG001
        return _search_out([text], [RDR_COLL])

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [text for _ in ids]}

    async def stub_summarize(**kwargs):  # noqa: ARG001
        return {"summary": "The decision is X [1]. It was accepted on 2026-10-01 after review of the evidence."}

    match = _MULTI_PLAN.__class__(
        plan_id=7, name="p", description="p", confidence=0.9, dimensions={}, tags="",
        plan_json=json.dumps({"steps": [
            {"tool": "search", "args": {"query": "$intent", "corpus": "rdr"}},
            {"tool": "summarize", "args": {"ids": "$step1.ids", "collections": "rdr"}},
        ]}), required_bindings=["intent"], optional_bindings=[], default_bindings={"intent": "q"}, parent_dims=None)
    db_stub = MagicMock(plans=MagicMock())
    db_stub.conn = MagicMock()
    with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
         patch("nexus.mcp.core._t2_ctx") as ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         patch.object(mcp_core, "search", stub_search), patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_summarize", stub_summarize):
        ctx.return_value.__enter__.return_value = db_stub
        env = await nx_answer(question="q", scope="rdr", structured=True)
    r = env["evidence_diagnostics"]["reducer_input"]
    assert r is not None, env["final_text"]
    assert (r["step"], r["items"], r["by_stream"], r["unattributed"]) == (1, 1, {"vector": 1}, 0), r
    assert r["by_stream_collection"] == {"vector": {RDR_COLL: 1}} and r["out_of_scope"] == 0


# ── 11. the empty-retrieval guard counts the merged reserved stream as evidence (follow-up 4) ───────────────


_EMPTY_STEPS = [{"ids": [], "tumblers": [], "collections": []}, {"ids": [], "tumblers": []},
                {"text": "The decision is X [1]. It was accepted on 2026-10-01 after review of the evidence."}]


def _drive_with_stream(*, merged: bool, items: int = 2):
    """nx_answer with every retrieval step empty and a reserved stream that the (mocked) runner did or did not merge."""
    from tests.test_rdr232_reserved_merge import _resolution_for_rdr084

    stream_holder: dict = {}

    async def run(match, bindings, **kwargs):  # noqa: ARG001
        stream = kwargs.get("reserved_stream")
        stream_holder["stream"] = stream
        if stream is not None and merged:
            stream.mark_merged()
        return _plan_run_result([dict(s) for s in _EMPTY_STEPS], [])

    evidence = PrimaryEvidence(items=tuple(_chunk(i) for i in range(items)), documents=(_coverage(rows=items, selected=items),))
    with patch("nexus.mcp.core._nx_answer_named_sources", return_value=_resolution_for_rdr084()), \
         patch("nexus.plans.runner.plan_run", AsyncMock(side_effect=run)), \
         patch("nexus.plans.primary_evidence.select_primary_evidence", MagicMock(return_value=evidence)), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=lambda c, h: {}), \
         patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
        env, _ = _drive_nx_answer(_MULTI_PLAN, scope="", structured=True, bindings={"source": "RDR-084"})
    return env, stream_holder["stream"]


def test_a_primary_only_run_keeps_its_answer_instead_of_the_no_match_notice() -> None:
    env, stream = _drive_with_stream(merged=True)
    assert stream is not None and stream.merged
    assert env["answer_shape"] == "answered", env["final_text"]
    assert "No matching evidence found" not in env["final_text"]
    assert "The decision is X" in env["final_text"]


def test_an_unmerged_reserved_stream_is_not_evidence_the_reducer_saw() -> None:
    env, stream = _drive_with_stream(merged=False)
    assert stream is not None and not stream.merged
    assert env["answer_shape"] == "no_evidence", "primary evidence nobody merged still leaves the reducer empty"


def test_the_guard_helper_counts_a_merged_stream_with_items_only() -> None:
    from nexus.mcp.core import _nx_answer_reserved_stream_is_evidence

    empty = ReservedStream.from_evidence(PrimaryEvidence(items=(_chunk(0),)))
    assert _nx_answer_reserved_stream_is_evidence(None) is False
    assert _nx_answer_reserved_stream_is_evidence(empty) is False, "not merged"
    empty.mark_merged()
    assert _nx_answer_reserved_stream_is_evidence(empty) is True


def test_the_continuation_handoff_guard_counts_the_active_reserved_stream_too() -> None:
    from nexus.plans.continuation_envelope import _prefix_produced_zero_evidence

    steps = [{"ids": [], "tumblers": []}]
    assert _prefix_produced_zero_evidence(steps, 1) == "no_items"
    stream = ReservedStream.from_evidence(PrimaryEvidence(items=(_chunk(0),)))
    with reserved_scope(stream):
        assert _prefix_produced_zero_evidence(steps, 1) is None, "primary evidence is evidence"
        assert _prefix_produced_zero_evidence(steps, 1, [("summarize", {"content": ""})]) == "items_present_all_empty", (
            "the payload check still catches an operator that was handed nothing")


# ── 12. golden sources: how a golden request's required_sources become nx_answer bindings (follow-up 2) ────────


def _golden(qid: str):
    from bench.schema import load_golden_set
    from tests import _rdr232_golden as g

    return g, next(q for q in load_golden_set(g.GOLDEN_PATH) if q.qid.startswith(qid))


def test_a_project_qualified_golden_source_is_a_source_binding_plus_an_owner_or_project() -> None:
    g, q = _golden("G06")
    assert q.required_sources == ["RDR-084@ART"]
    with_owner, label_only = g.golden_source_bindings(q, {"ART": "1.61"})
    assert (with_owner, label_only) == ({"source": "RDR-084", "owner": "1.61"}, [])
    no_owner, _ = g.golden_source_bindings(q)
    assert no_owner == {"source": "RDR-084", "project": "ART"}, "an unmapped project is passed on, never guessed"


def test_a_bare_golden_source_has_no_qualifier_and_so_can_resolve_to_candidates() -> None:
    """G01 names RDR-049 with no project. Where sibling files share the number a bare id resolves to
    candidates; whether the golden label should carry a qualifier is nexus-xho6c's decision."""
    g, q = _golden("G01")
    assert g.golden_source_bindings(q, {"ART": "1.61"}) == ({"source": "RDR-049"}, [])


def test_a_paper_title_source_is_scored_by_label_only() -> None:
    g, q = _golden("G05")
    assert q.required_sources == ["HybridRAG"]
    assert g.golden_source_bindings(q) == ({}, ["HybridRAG"])


def test_several_golden_sources_are_a_source_list() -> None:
    from bench.schema import load_golden_set
    from tests import _rdr232_golden as g

    multi = [x for x in load_golden_set(g.GOLDEN_PATH) if len(x.required_sources) > 1]
    assert multi, "the golden set has multi-source requests"
    bindings, label_only = g.golden_source_bindings(multi[0])
    assert isinstance(bindings["source"], list) and len(bindings["source"]) == len(multi[0].required_sources)
    assert label_only == []
