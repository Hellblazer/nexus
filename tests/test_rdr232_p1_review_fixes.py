# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 review fixes for nexus-jypmj.1.3 / .1.4 (T2 ``nexus/rdr232-p1-1.3-1.4-review-
2026-10-25`` and ``-critique-``).

No test here mints a tenant; the ``nx_answer`` wiring runs through the same patched seams as
``tests/test_rdr232_caller_scope.py``.

* A run whose retrieval steps all conflicted with the CALLER'S scope is the caller's error, not a
  plan failure: the run is recorded, no outcome counter moves.
* ``_scope_conflict_output`` carries every key a structured retrieval result carries, so a grown
  plan's ``$stepN.<key>`` on a skipped step resolves empty.
* The ``[scope conflict:`` line follows a leading ``[non-answer:`` / ``[budget exhausted`` marker.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.plans.match import Match
from nexus.plans.runner import PlanRunStepRefError, _scope_conflict_output, plan_run
from tests.test_rdr232_caller_scope import (
    ART_RDR_COLLECTION,
    CONFLICT_DIAG,
    RAN_DIAG,
    _MULTI_PLAN,
    _Recorder,
    _drive_nx_answer,
    _match,
    _plan_run_result,
    _single_query_match,
)


def _outcomes(db: MagicMock) -> list[bool]:
    """Every outcome the run counted, however it reached the store: the separate
    ``increment_run_outcome`` bump or the composite ``record_nx_answer_run_complete`` write."""
    counted = [c.kwargs["success"] for c in db.plans.increment_run_outcome.call_args_list]
    counted += [c.kwargs["success"] for c in db.telemetry.record_nx_answer_run_complete.call_args_list]
    return counted


def _run_recorded(db: MagicMock) -> bool:
    return bool(db.telemetry.record_nx_answer_run.called or db.telemetry.record_nx_answer_run_complete.called)


def _db() -> MagicMock:
    return MagicMock(plans=MagicMock())


# ── the choke point: success=None is "no verdict" ───────────────────────────


@pytest.mark.parametrize("composite", [False, True], ids=["degraded-path", "composite-path"])
def test_record_complete_without_a_verdict_records_the_run_and_counts_no_outcome(
    composite: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus.mcp.core as core
    from nexus.mcp.core import _nx_answer_record_complete

    db = _db()
    monkeypatch.setattr(core, "_t2_index_write", lambda fn, **_kw: fn(db))
    _nx_answer_record_complete(
        db, question="q", plan_id=7, matched_confidence=0.9, step_count=1, final_text="t",
        step_records=[], duration_ms=5, trace=True, success=None,
        composite_supported_at_start=composite, early_bump_fired=not composite,
    )
    assert db.telemetry.record_nx_answer_run.call_count == 1, "the run record is still written"
    assert db.telemetry.record_nx_answer_run_complete.call_count == 0, "the composite write carries a verdict"
    assert db.plans.increment_run_outcome.call_count == 0
    # use_count: bumped once per run. On the composite route nothing bumped it early.
    assert db.plans.increment_run_started.call_count == (1 if composite else 0)


def test_record_complete_with_a_verdict_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.mcp.core as core
    from nexus.mcp.core import _nx_answer_record_complete

    db = _db()
    monkeypatch.setattr(core, "_t2_index_write", lambda fn, **_kw: fn(db))
    _nx_answer_record_complete(
        db, question="q", plan_id=7, matched_confidence=0.9, step_count=1, final_text="t",
        step_records=[], duration_ms=5, trace=True, success=False,
        composite_supported_at_start=True, early_bump_fired=False,
    )
    assert [c.kwargs["success"] for c in db.telemetry.record_nx_answer_run_complete.call_args_list] == [False]


# ── the plan path's empty-retrieval verdict ─────────────────────────────────

_CONFLICT_STEP = {"ids": [], "tumblers": [], "scope_conflict": {"step_index": 0}}


def test_empty_retrieval_outcome_helper() -> None:
    from nexus.mcp.core import _nx_answer_empty_retrieval_outcome

    other_conflict = {**_CONFLICT_STEP, "scope_conflict": {"step_index": 1}}
    assert _nx_answer_empty_retrieval_outcome([_CONFLICT_STEP, other_conflict, {"text": "s"}]) is None
    # one retrieval step ran and found nothing: that is the plan's miss, a failure
    assert _nx_answer_empty_retrieval_outcome([_CONFLICT_STEP, {"ids": [], "tumblers": []}]) is False
    assert _nx_answer_empty_retrieval_outcome([{"ids": [], "tumblers": []}]) is False
    assert _nx_answer_empty_retrieval_outcome([]) is False


def test_plan_path_run_whose_retrieval_steps_all_conflicted_counts_no_outcome() -> None:
    db = _db()
    steps = [
        _CONFLICT_STEP, {**_CONFLICT_STEP, "scope_conflict": {"step_index": 1}}, {"text": "summary of nothing"},
    ]
    both = [CONFLICT_DIAG, {**CONFLICT_DIAG, "step_index": 1, "plan_corpus": "docs"}]
    env, _ = _drive_nx_answer(
        _MULTI_PLAN, scope="rdr", structured=True, plan_run_result=_plan_run_result(steps, both), db=db)
    assert env["answer_shape"] == "no_evidence"
    assert _run_recorded(db), "the run is still recorded; its text says what happened"
    assert _outcomes(db) == [], "a caller's scope conflict is not a plan failure"


def test_plan_path_genuine_zero_evidence_is_still_a_failure() -> None:
    db = _db()
    steps = [_CONFLICT_STEP, {"ids": [], "tumblers": []}, {"text": "summary of nothing"}]
    env, _ = _drive_nx_answer(
        _MULTI_PLAN, scope="rdr", structured=True,
        plan_run_result=_plan_run_result(steps, [CONFLICT_DIAG, RAN_DIAG]), db=db)
    assert env["answer_shape"] == "no_evidence"
    assert _outcomes(db) == [False], "a step that ran and found nothing is the plan's miss (nexus-yg49g)"


# ── the fast path ───────────────────────────────────────────────────────────


def test_fast_path_scope_conflict_counts_no_outcome() -> None:
    db = _db()
    out, calls = _drive_nx_answer(_single_query_match("code"), scope="rdr", structured=True, db=db)
    assert calls == [] and out["answer_shape"] == "no_evidence"
    assert _run_recorded(db)
    assert _outcomes(db) == []


def test_fast_path_that_answers_still_counts_its_outcome() -> None:
    db = _db()
    _drive_nx_answer(_single_query_match("knowledge,code,docs,rdr"), scope="rdr", structured=True, db=db)
    assert _outcomes(db) == [True]


# ── _scope_conflict_output carries every key a structured retrieval returns ─


def _narrowing():
    from nexus.plans.evidence import ScopeConstraint, narrow_corpus

    return narrow_corpus("code", ScopeConstraint.parse("rdr"))


def test_scope_conflict_output_carries_every_structured_retrieval_key() -> None:
    out = _scope_conflict_output(_narrowing(), step_index=0, tool="search")
    for key in ("ids", "tumblers", "distances", "hybrid_scores", "collections", "chunk_collections",
                "chunk_text_hash", "also_in", "also_in_ids", "contents"):
        assert out[key] == [], key
    assert out["scope_conflict"]["plan_corpus"] == "code"


def test_a_reference_to_a_skipped_steps_hybrid_scores_resolves_empty() -> None:
    plan = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "code"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.hybrid_scores"}},
        {"tool": "summarize", "args": {"inputs": ["$step1.also_in", "$step1.also_in_ids"]}},
    ]}
    rec = _Recorder()
    try:
        result = asyncio.run(plan_run(
            _match(plan), {"intent": "q", "_nx_scope": "rdr"}, dispatcher=rec,
            continuation_cut_at_step=2, bundle_operators=False,
        ))
    except PlanRunStepRefError as exc:  # pragma: no cover - the failure this test exists to catch
        pytest.fail(f"$stepN.<key> into a skipped step must resolve empty: {exc}")
    assert result.steps[0]["scope_conflict"]["tool"] == "search"
    assert dict(rec.calls)["store_get_many"]["ids"] == []


# ── the scope notice follows a leading marker ───────────────────────────────

_HYDRATION_DUMP = '{"contents": ["a", "b"], "missing": []}'


def test_scope_conflict_line_follows_a_leading_non_answer_marker() -> None:
    steps = [_CONFLICT_STEP, {"ids": ["c1"], "tumblers": ["1.1.1"], "chunk_collections": [ART_RDR_COLLECTION]},
             {"text": _HYDRATION_DUMP}]
    res = _plan_run_result(steps, [CONFLICT_DIAG, RAN_DIAG])
    text, _ = _drive_nx_answer(_MULTI_PLAN, scope="rdr", plan_run_result=res)
    lines = text.splitlines()
    assert lines[0].startswith("[non-answer:"), text
    assert any(line.startswith("[scope conflict:") for line in lines[1:]), text
    assert text.count("[scope conflict:") == 1


def test_scope_conflict_line_follows_a_leading_budget_exhausted_marker() -> None:
    steps = [_CONFLICT_STEP, {"ids": ["c1"], "tumblers": ["1.1.1"], "chunk_collections": [ART_RDR_COLLECTION]}]
    res = _plan_run_result(steps, [CONFLICT_DIAG, RAN_DIAG])
    res.budget_exhausted_at_step = 2
    res.budget_exhausted_kind = "time"
    res.total_planned_steps = 3
    text, _ = _drive_nx_answer(_MULTI_PLAN, scope="rdr", plan_run_result=res)
    assert text.startswith("[budget exhausted"), text
    assert "[scope conflict:" in text.split("\n", 1)[1], text


# ── named-source disclosures (nexus-jypmj.1.4 review) ───────────────────────


def _resolution(*sources):
    from nexus.plans.primary_evidence import NamedSourceResolution

    return NamedSourceResolution(sources=tuple(sources))


def _resolved_source(*, qualified: bool):
    from nexus.plans.evidence import SourceRef
    from nexus.plans.primary_evidence import ResolvedSource

    return ResolvedSource(
        ref=SourceRef.parse("RDR-084"), status="resolved", tumbler="1.2.3", title="RDR-084",
        qualified=qualified,
    )


def _drive_with_sources(resolution, *, bindings, match=None, structured=False, scope=""):
    from unittest.mock import patch

    with patch("nexus.mcp.core._nx_answer_named_sources", return_value=resolution):
        return _drive_nx_answer(
            match or _single_query_match("knowledge,code,docs,rdr"),
            scope=scope, structured=structured, bindings=bindings)


def test_a_lone_unqualified_resolution_gets_a_one_line_notice() -> None:
    lines = _resolution(_resolved_source(qualified=False)).disclosures()
    assert len(lines) == 1
    assert "RDR-084" in lines[0] and "1.2.3" in lines[0] and "not owner-qualified" in lines[0]


def test_a_qualified_resolution_has_no_notice() -> None:
    assert _resolution(_resolved_source(qualified=True)).disclosures() == []


def test_the_fast_path_notice_does_not_claim_the_document_was_used_as_evidence() -> None:
    res = _resolution(_resolved_source(qualified=False))
    (line,) = res.disclosures(primary_stream=False)
    assert "no primary evidence stream" in line and "not owner-qualified" in line
    text, _ = _drive_with_sources(res, bindings={"source": "RDR-084"})
    assert "not owner-qualified" in text and "no primary evidence stream" in text
    env, _ = _drive_with_sources(res, bindings={"source": "RDR-084"}, structured=True)
    assert env["named_sources"]["sources"][0]["tumbler"] == "1.2.3", "the envelope keeps named_sources"


def test_an_owner_qualifier_failure_is_disclosed_once_in_text_mode() -> None:
    from nexus.plans.evidence import SourceRef
    from nexus.plans.primary_evidence import ResolvedSource

    failure = "owner qualifier conflict: owner 1.2 and project 1.3 name different projects"
    res = _resolution(ResolvedSource(ref=SourceRef.parse("RDR-084"), status="unresolved", detail=failure))
    text, _ = _drive_with_sources(res, bindings={"source": "RDR-084", "owner": "1.2", "project": "1.3"})
    assert text.count("owner qualifier") == 1, text
    assert "[evidence request: owner qualifier conflict" in text

    # a source unresolved for a reason of its own is still named
    other = _resolution(ResolvedSource(ref=SourceRef.parse("RDR-084"), status="unresolved", detail="nothing fits"))
    text, _ = _drive_with_sources(other, bindings={"source": "RDR-084"})
    assert "named source RDR-084 unresolved: nothing fits" in text


def test_the_structured_envelope_still_carries_the_owner_failure_per_source() -> None:
    from nexus.plans.evidence import SourceRef
    from nexus.plans.primary_evidence import ResolvedSource

    failure = "owner qualifier conflict: owner 1.2 and project 1.3 name different projects"
    res = _resolution(ResolvedSource(ref=SourceRef.parse("RDR-084"), status="unresolved", detail=failure))
    env, _ = _drive_with_sources(
        res, bindings={"source": "RDR-084", "owner": "1.2", "project": "1.3"}, structured=True)
    assert env["named_sources"]["sources"][0]["detail"] == failure
    assert env["evidence_request"]["owner"]["status"] == "conflict"
