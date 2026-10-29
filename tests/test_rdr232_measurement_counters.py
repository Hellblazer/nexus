# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10): the counters the run summary persists.

Each test builds a run through the real ``plan_run`` and coverage boundary (the model call stubbed at the
dispatcher, as in ``tests/test_rdr232_coverage_ledger.py``) whose ledger produces ONE class, and pins the value
the version-1 run summary then carries. The classes are the ones the Phase 3 critique (S-B, S-C) named: the
reply class of each assessment call (per stream, per call), the failed outcome split into an unusable reply and
a raised call, conflicted facets counted apart from covered, unassessed facets by cause, and whether the run
entered repair. Version 1 is kept: every key here is additive, and no existing key changed meaning.
"""
from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import patch

import pytest

from nexus.plans import coverage_ledger
from nexus.plans.coverage_ledger import (
    REPLY_CLASSES,
    UNASSESSED_CAUSES,
    AssessmentCall,
    AssessmentReport,
    BoundaryView,
    CoverageBoundary,
    CoverageLedger,
    CoverageResult,
)
from nexus.plans.run_summary import (
    RUN_SUMMARY_MAX_BYTES,
    RUN_SUMMARY_VERSION,
    RunSummaryInputs,
    build_run_summary,
    canonical_json,
    fit_summary,
)
from tests.test_rdr232_coverage_ledger import (
    Dispatcher,
    _cov,
    _request,
    _reserved,
    _tag_lines,
    _view,
    returns_no_records,
    supports_first_item,
    supports_nothing,
)
from tests.test_rdr232_coverage_repair import RepairDispatcher, _run


@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    """The runner's non-evidentiary hygiene asks the catalog; these tests are tenant-free."""
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


def _summary(cov: CoverageResult, *, request: Any = None, trace: bool = True) -> dict[str, Any]:
    return build_run_summary(
        RunSummaryInputs(request=request), outcome="ok", plan_id=7, step_count=5, duration_ms=100, cost_usd=None,
        trace=trace, coverage=cov, terminal_reached=True,
    )


def _classes(**counts: int) -> dict[str, int]:
    assert set(counts) <= set(REPLY_CLASSES)
    return {c: counts.get(c, 0) for c in REPLY_CLASSES}


def _causes(**counts: int) -> dict[str, int]:
    assert set(counts) <= set(UNASSESSED_CAUSES)
    return {c: counts.get(c, 0) for c in UNASSESSED_CAUSES}


def untagged(items: str, instruction: str) -> dict:  # noqa: ARG001
    """A reply with a record per item and no facet tag: every record is rejected."""
    return {"ok": True, "evidence": [{"item_id": i["id"], "quote": "a quote with no tag", "role": "supports"} for i in json.loads(items)]}


def contradicts_only(items: str, instruction: str) -> dict:
    first = json.loads(items)[0]["id"]
    return {"ok": False, "evidence": [
        {"item_id": first, "quote": f"[{t}] the opposite", "role": "contradicts"} for t in _tag_lines(instruction)
    ]}


def raises(items: str, instruction: str) -> BaseException:  # noqa: ARG001
    return RuntimeError("check backend down")


def conflicted(items: str, instruction: str) -> dict:
    """The first item supports every listed facet; a second item, where there is one, contradicts f1."""
    payload = json.loads(items)
    recs = [{"item_id": payload[0]["id"], "quote": f"[{t}] supports", "role": "supports"} for t in _tag_lines(instruction)]
    if len(payload) > 1:
        recs.append({"item_id": payload[1]["id"], "quote": "[f1] the opposite", "role": "contradicts"})
    return {"ok": True, "evidence": recs}


# ── the reply class of each assessment call ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("check", "classes", "accepted", "unusable", "raised"),
    [
        (supports_first_item, _classes(support=2), 2, 0, 0),
        (supports_nothing, _classes(neutral_or_contradict=2), 2, 0, 0),
        (contradicts_only, _classes(neutral_or_contradict=2), 2, 0, 0),
        (untagged, _classes(all_rejected=2), 0, 2, 0),
        (returns_no_records, _classes(no_records=2), 0, 2, 0),
        (raises, _classes(raised=1), 0, 0, 1),  # a raised call stops the assessment: one call, not two
    ],
    ids=["support", "neutral", "contradict_only", "all_rejected", "no_records", "raised"],
)
@pytest.mark.asyncio
async def test_each_reply_class_is_counted_and_persisted(check, classes, accepted, unusable, raised) -> None:
    """One facet, two streams (vector, graph): one call each. The run's ledger produces exactly the class named."""
    result = await _run(Dispatcher(check), CoverageBoundary(request=_request(1)))
    cov = _cov(result)
    block = _summary(cov)["coverage"]
    assert block["reply_classes"] == classes
    assert (block["accepted_records"], block["unusable_calls"], block["raised_calls"]) == (accepted, unusable, raised)
    assert sum(block["reply_classes"].values()) == block["assessment_calls"], "every attempted call has exactly one class"
    assert block["facets_conflicted"] == 0


@pytest.mark.asyncio
async def test_the_classes_are_counted_per_stream_and_per_call() -> None:
    """Three streams, each answered differently: the per-stream counts and the call log say which stream did what."""
    def per_stream(items: str, instruction: str) -> dict:
        text = json.loads(items)[0]["text"]
        if text.startswith("[primary source"):
            return supports_first_item(items, instruction)
        if text.startswith("vector"):
            return returns_no_records(items, instruction)
        return untagged(items, instruction)

    d = Dispatcher(per_stream, hydrates=True)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(1)), reserved_stream=_reserved()))
    block = _summary(cov)["coverage"]
    assert block["reply_classes"] == _classes(support=1, no_records=1, all_rejected=1)
    assert block["reply_by_stream"] == {
        "primary": _classes(support=1), "vector": _classes(no_records=1), "graph": _classes(all_rejected=1),
    }
    assert block["assessment_call_log"] == [["primary", 1, "support"], ["vector", 1, "no_records"], ["graph", 1, "all_rejected"]]


@pytest.mark.asyncio
async def test_the_call_log_carries_the_facets_per_call_so_single_and_multi_facet_calls_can_be_split() -> None:
    cap = coverage_ledger.MAX_FACETS_PER_ASSESSMENT_CALL
    cov = _cov(await _run(Dispatcher(supports_first_item), CoverageBoundary(request=_request(cap + 1))))
    block = _summary(cov)["coverage"]
    assert [entry[1] for entry in block["assessment_call_log"]] == [cap, 1, cap, 1]
    assert block["facets_required"] == cap + 1


@pytest.mark.asyncio
async def test_two_rounds_sum_their_classes_and_calls() -> None:
    """A repair adds a reassessment call: its class is added to the first round's, and the log keeps both."""
    cov = _cov(await _run(RepairDispatcher(), CoverageBoundary(request=_request(3))))
    assert cov.ledger.rounds == 2 and cov.repair.outcome == "completed"
    block = _summary(cov)["coverage"]
    assert block["reply_classes"] == _classes(support=2, neutral_or_contradict=1)
    assert block["assessment_call_log"] == [["vector", 3, "support"], ["graph", 3, "neutral_or_contradict"], ["vector", 1, "support"]]


def test_merging_two_reports_adds_the_new_counters() -> None:
    a = AssessmentReport(outcome="ok", calls=1, facets_per_call=(2,), streams=("vector",), accepted_records=3,
                         call_log=(AssessmentCall("vector", 2, "support", 3, 0),))
    b = AssessmentReport(outcome="failed", calls=1, facets_per_call=(1,), streams=("graph",), raised_calls=1,
                         call_log=(AssessmentCall("graph", 1, "raised", 0, 0),))
    m = a.merged_with(b)
    assert (m.accepted_records, m.raised_calls, m.unusable_calls) == (3, 1, 0)
    assert [c.reply for c in m.call_log] == ["support", "raised"]


# ── the failed outcome, split ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("check", "outcome", "cause"),
    [
        (raises, "failed", "raised_call"),
        (returns_no_records, "failed", "unusable_reply"),
        (untagged, "failed", "unusable_reply"),
        (supports_first_item, "ok", "none"),
    ],
    ids=["raised", "empty_reply", "all_rejected_reply", "healthy"],
)
@pytest.mark.asyncio
async def test_the_failed_outcome_is_split_into_unusable_reply_and_raised_call(check, outcome, cause) -> None:
    cov = _cov(await _run(Dispatcher(check), CoverageBoundary(request=_request(2))))
    block = _summary(cov)["coverage"]
    assert (block["assessment_outcome"], block["failure_cause"]) == (outcome, cause)


@pytest.mark.asyncio
async def test_a_good_call_then_a_raised_one_is_a_partial_failure_by_raised_call() -> None:
    calls = {"n": 0}

    def good_then_raise(items: str, instruction: str):
        calls["n"] += 1
        return supports_nothing(items, instruction) if calls["n"] == 1 else RuntimeError("down")

    block = _summary(_cov(await _run(Dispatcher(good_then_raise), CoverageBoundary(request=_request(2)))))["coverage"]
    assert (block["assessment_outcome"], block["failure_cause"]) == ("partial_failure", "raised_call")
    assert block["reply_classes"] == _classes(neutral_or_contradict=1, raised=1)


@pytest.mark.asyncio
async def test_an_unusable_reply_and_a_raised_call_in_one_run_are_mixed() -> None:
    calls = {"n": 0}

    def unusable_then_raise(items: str, instruction: str):
        calls["n"] += 1
        return returns_no_records(items, instruction) if calls["n"] == 1 else RuntimeError("down")

    block = _summary(_cov(await _run(Dispatcher(unusable_then_raise), CoverageBoundary(request=_request(2)))))["coverage"]
    assert (block["assessment_outcome"], block["failure_cause"]) == ("failed", "mixed")
    assert block["reply_classes"] == _classes(no_records=1, raised=1)


@pytest.mark.asyncio
async def test_a_skipped_assessment_is_not_a_failure_cause() -> None:
    d = Dispatcher()
    cov = _cov(await _run(d, CoverageBoundary(request=_request(2)), deadline=time.monotonic() + 3.0))
    block = _summary(cov)["coverage"]
    assert (block["assessment_outcome"], block["failure_cause"]) == ("skipped_deadline", "none")
    assert block["reply_classes"] == _classes() and block["assessment_calls"] == 0


# ── conflicted facets, counted apart from covered ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_conflicted_facet_is_counted_apart_and_covered_keeps_its_meaning() -> None:
    """f1 has a supporting and a contradicting item (covered, reason conflicted); f2 is clean. ``facets.covered`` is
    unchanged (both), ``facets_conflicted`` is the subset that is conflicted."""
    cov = _cov(await _run(Dispatcher(conflicted), CoverageBoundary(request=_request(2))))
    assert cov.conflicted_facet_ids == ("f1",)
    block = _summary(cov)["coverage"]
    assert block["facets"] == {"covered": 2, "unsupported": 0, "contradicted": 0, "unassessed": 0}
    assert block["facets_conflicted"] == 1
    assert block["reply_classes"] == _classes(support=2), "a call with a support is a support call, whatever else it returned"


# ── unassessed facets, by cause ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unassessed_by_cause_unusable_reply() -> None:
    cov = _cov(await _run(Dispatcher(returns_no_records), CoverageBoundary(request=_request(2))))
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(unusable_reply=2)


@pytest.mark.asyncio
async def test_unassessed_by_cause_raised_call() -> None:
    cov = _cov(await _run(Dispatcher(raises), CoverageBoundary(request=_request(2))))
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(raised_call=2)


@pytest.mark.asyncio
async def test_unassessed_by_cause_deadline_skip() -> None:
    cov = _cov(await _run(Dispatcher(), CoverageBoundary(request=_request(2)), deadline=time.monotonic() + 3.0))
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(deadline_skip=2)


@pytest.mark.asyncio
async def test_unassessed_by_cause_budget_skip() -> None:
    boundary = CoverageBoundary(request=_request(1))
    first = await _run(Dispatcher(), boundary)
    cov = await boundary.evaluate(_view(_cov(first), Dispatcher(), first, remaining_usd=lambda: 0.0))
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(budget_skip=1)


@pytest.mark.asyncio
async def test_unassessed_by_cause_deadline_skip_after_a_call_was_made() -> None:
    """The first call runs long enough that the stage allowance cannot fund the second: that is a deadline skip,
    not a raised call, though the outcome is ``partial_failure`` in both."""
    first = await _run(Dispatcher(), CoverageBoundary(request=_request(2)))
    prior = _cov(first)
    clock = {"now": 0.0}

    async def slow(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        clock["now"] += 118.0
        return supports_nothing(args["items"], args["check_instruction"])

    view = BoundaryView(
        evidence_texts=[a.content for a in prior.ledger.anchors], observed=True, step_outputs=list(first.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[], check=slow,
        remaining_usd=lambda: None, remaining_seconds=lambda: 300.0 - clock["now"],
    )
    cov = await CoverageBoundary(request=_request(2)).evaluate(view)
    assert cov.ledger.assessment.outcome == "partial_failure" and cov.ledger.assessment.calls == 1
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(deadline_skip=2)


@pytest.mark.asyncio
async def test_unassessed_by_cause_other_is_the_item_cap() -> None:
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 10):
        cov = _cov(await _run(Dispatcher(supports_nothing), CoverageBoundary(request=_request(2))))
    assert cov.ledger.assessment.skipped_items > 0
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(other=2)


@pytest.mark.asyncio
async def test_an_unusable_reply_outranks_a_later_skip_as_the_cause() -> None:
    """Facets whose first call came back unusable are counted unusable even if the stage then ran out of time."""
    first = await _run(Dispatcher(), CoverageBoundary(request=_request(2)))
    prior = _cov(first)
    clock = {"now": 0.0}

    async def slow_empty(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        clock["now"] += 118.0
        return returns_no_records(args["items"], args["check_instruction"])

    view = BoundaryView(
        evidence_texts=[a.content for a in prior.ledger.anchors], observed=True, step_outputs=list(first.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[], check=slow_empty,
        remaining_usd=lambda: None, remaining_seconds=lambda: 300.0 - clock["now"],
    )
    cov = await CoverageBoundary(request=_request(2)).evaluate(view)
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(unusable_reply=2)


@pytest.mark.asyncio
async def test_an_unusable_reply_outranks_an_earlier_item_cap_as_the_cause() -> None:
    """The vector call could not carry every item (the cap), then the graph call came back empty: the facets are
    counted unusable, the stronger cause, not ``other``."""
    def by_stream(items: str, instruction: str) -> dict:
        text = json.loads(items)[0]["text"]
        return supports_nothing(items, instruction) if text.startswith("vector") else returns_no_records(items, instruction)

    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 10):
        cov = _cov(await _run(Dispatcher(by_stream), CoverageBoundary(request=_request(2))))
    assert cov.ledger.assessment.skipped_items > 0
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(unusable_reply=2)


@pytest.mark.asyncio
async def test_a_run_with_nothing_unassessed_counts_zero_in_every_cause() -> None:
    cov = _cov(await _run(Dispatcher(), CoverageBoundary(request=_request(2))))
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes()


def not_the_check_shape(items: str, instruction: str) -> dict:  # noqa: ARG001
    """A reply with no evidence list: the unusable reply that stops the assessment."""
    return {"ok": True}


def _reasons(cov: CoverageResult) -> dict[str, str]:
    return {f.facet_id: f.reason for f in cov.ledger.findings if f.state == "unassessed"}


@pytest.mark.asyncio
async def test_groups_never_attempted_after_a_raised_call_are_stopped_after_failure_not_raised() -> None:
    """Three facets, two per call: the first call (vector, f1 and f2) raises and ends the pass, so f3's group (and every
    later stream's) was never attempted. f1 and f2 are the failing call's own facets; f3 is the stop rule's."""
    d = Dispatcher(raises)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(3), max_facets_per_call=2)))
    assert d.tools.count("operator_check") == 1, "the pass stops at the first raise"
    assert _reasons(cov) == {"f1": "assessment_raised", "f2": "assessment_raised", "f3": "assessment_stopped_after_failure"}
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(raised_call=2, stopped_after_failure=1)


@pytest.mark.asyncio
async def test_groups_never_attempted_after_a_malformed_reply_are_stopped_after_failure_not_unusable() -> None:
    cov = _cov(await _run(Dispatcher(not_the_check_shape), CoverageBoundary(request=_request(3), max_facets_per_call=2)))
    assert _reasons(cov) == {"f1": "assessment_unusable", "f2": "assessment_unusable", "f3": "assessment_stopped_after_failure"}
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(unusable_reply=2, stopped_after_failure=1)


@pytest.mark.asyncio
async def test_a_stop_after_failure_cause_is_absent_when_every_group_was_attempted() -> None:
    """Non-vacuity of the split: an unusable reply that does NOT stop the pass (an empty evidence list) leaves every
    group attempted, so nothing is counted as stopped."""
    cov = _cov(await _run(Dispatcher(returns_no_records), CoverageBoundary(request=_request(3), max_facets_per_call=2)))
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(unusable_reply=3)


@pytest.mark.asyncio
async def test_a_facet_whose_own_group_raised_is_raised_even_though_later_streams_were_never_reached() -> None:
    """Call 1 (vector, f1 and f2) supports; call 2 (vector, f3) raises. f3's own call failed, so f3 is ``raised_call``;
    the graph groups never ran but every facet in them already has a state, so nothing is ``stopped_after_failure``."""
    calls = {"n": 0}

    def support_then_raise(items: str, instruction: str):
        calls["n"] += 1
        return supports_first_item(items, instruction) if calls["n"] == 1 else RuntimeError("down")

    cov = _cov(await _run(Dispatcher(support_then_raise), CoverageBoundary(request=_request(3), max_facets_per_call=2)))
    by = {f.facet_id: (f.state, f.reason) for f in cov.ledger.findings}
    assert by["f1"][0] == by["f2"][0] == "covered"
    assert by["f3"] == ("unassessed", "assessment_raised")
    assert _summary(cov)["coverage"]["unassessed_by_cause"] == _causes(raised_call=1)


def test_the_new_cause_is_a_member_of_the_enumeration_with_other_still_last() -> None:
    assert "stopped_after_failure" in UNASSESSED_CAUSES and UNASSESSED_CAUSES[-1] == "other", "other stays the catch-all, last"


# ── whether the run entered repair ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_with_a_repairable_gap_entered_repair() -> None:
    cov = _cov(await _run(RepairDispatcher(), CoverageBoundary(request=_request(3))))
    s = _summary(cov)
    assert s["repair"]["entered"] is True and s["coverage"]["repair_entered"] is True


@pytest.mark.asyncio
async def test_a_run_that_entered_repair_and_found_nothing_still_counts_as_entered() -> None:
    """Repair yield is measured over the runs that entered it, so a repair that found nothing must be in that set."""
    cov = _cov(await _run(Dispatcher(supports_nothing), CoverageBoundary(request=_request(1))))
    assert cov.repair.outcome != "not_attempted"
    s = _summary(cov)
    assert s["repair"]["entered"] is True and s["coverage"]["repair_outcome"] == cov.repair.outcome


@pytest.mark.asyncio
async def test_a_fully_covered_run_did_not_enter_repair() -> None:
    cov = _cov(await _run(Dispatcher(), CoverageBoundary(request=_request(1))))
    s = _summary(cov)
    assert s["repair"]["entered"] is False and s["coverage"]["repair_entered"] is False


@pytest.mark.asyncio
async def test_repair_switched_off_is_not_entered_even_with_a_gap() -> None:
    cov = _cov(await _run(Dispatcher(supports_nothing), CoverageBoundary(request=_request(1), max_repair_queries=0)))
    assert cov.repair_facet_ids == ("f1",)
    assert _summary(cov)["repair"]["entered"] is False


@pytest.mark.asyncio
async def test_a_run_with_no_assessment_has_no_new_counters_to_report() -> None:
    s = build_run_summary(RunSummaryInputs(), outcome="ok", plan_id=None, step_count=0, duration_ms=1, cost_usd=None,
                          trace=True, coverage=None, terminal_reached=False)
    assert s["coverage"] is None and s["repair"] == {"attempts": 0, "facets_repairable": 0, "entered": False}


# ── the envelope carries the same counts ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_envelope_assessment_object_reports_the_classes_and_the_counts_report_the_facet_causes() -> None:
    cov = _cov(await _run(Dispatcher(conflicted), CoverageBoundary(request=_request(2))))
    wire = cov.to_wire()
    assert wire["assessment"]["reply_classes"] == _classes(support=2)
    assert wire["assessment"]["call_log"] == [["vector", 2, "support", 3, 0], ["graph", 2, "support", 2, 0]]
    assert wire["facets_conflicted"] == 1 and wire["unassessed_by_cause"] == _causes() and wire["repair_entered"] is False


# ── version, size and redaction ─────────────────────────────────────────────────────────────────────────────


def test_the_change_is_additive_so_the_version_stays_one() -> None:
    assert RUN_SUMMARY_VERSION == 1
    cov = CoverageResult("complete", "all_covered", False, CoverageLedger(assessment=AssessmentReport(outcome="ok")))
    s = _summary(cov)
    assert s["version"] == 1
    assert {"state", "reason", "facets", "assessment_outcome", "assessment_calls", "rejected_records"} <= set(s["coverage"]), (
        "every key version 1 already carried is still there")


@pytest.mark.asyncio
async def test_trace_false_carries_no_question_or_facet_text_in_the_new_blocks() -> None:
    req = _request(3)
    cov = _cov(await _run(RepairDispatcher(), CoverageBoundary(request=req)))
    s = _summary(cov, request=req, trace=False)
    text = canonical_json(s)
    assert "what the document says" not in text and "about topic" not in text, "no facet text"
    assert s["coverage"]["reply_classes"] and s["coverage"]["assessment_call_log"], "the counters are still there"
    assert all(isinstance(e[0], str) and e[0] in coverage_ledger.STREAM_ORDER for e in s["coverage"]["assessment_call_log"])


def test_a_pathological_call_log_is_capped_and_the_summary_stays_under_the_size_cap() -> None:
    log = tuple(AssessmentCall("vector", 6, "support", 6, 0) for _ in range(500))
    report = AssessmentReport(outcome="ok", calls=500, facets_per_call=(6,) * 500, streams=("vector",), accepted_records=3000, call_log=log)
    cov = CoverageResult("complete", "all_covered", False, CoverageLedger(assessment=report))
    s = _summary(cov)
    block = s["coverage"]
    assert block["reply_classes"]["support"] == 500, "the aggregate counts every call"
    assert len(block["assessment_call_log"]) <= 40 and block["assessment_call_log_truncated"] is True
    assert len(canonical_json(s).encode()) < RUN_SUMMARY_MAX_BYTES


@pytest.mark.asyncio
async def test_a_summary_over_the_cap_loses_the_call_log_before_the_aggregates() -> None:
    req = _request(3)
    cov = _cov(await _run(RepairDispatcher(), CoverageBoundary(request=req)))
    s = _summary(cov, request=req)
    full = len(canonical_json(s).encode())
    cut_log_first = False
    for limit in range(full, 700, -25):
        out = fit_summary(s, max_bytes=limit)
        if out.get("coverage") is None:
            break
        assert out["coverage"]["reply_classes"] == s["coverage"]["reply_classes"], f"aggregates survive a cut to {limit}"
        if "assessment_call_log" not in out["coverage"]:
            cut_log_first = True
            assert "coverage.assessment_call_log" in out["truncated"]
    assert cut_log_first, "non-vacuity: some cut dropped the log and kept the aggregates"
