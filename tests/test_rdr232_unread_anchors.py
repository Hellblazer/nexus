# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 coverage check, anchors the assessment calls could not carry (nexus-jypmj.23; Sam, 2026-10-30).

The measured case (negative control N04, plan 467): the plan handed the boundary 301 anchors, the assessment
calls carried 127, seven calls all replied neutral, and 174 anchors were left out by the per-call size cap.
The facets then read ``unassessed`` (``assessment_incomplete``), so the check could never withhold, and
off-topic prose came back ``answered``.

The ruling: a facet no READ item supports or contradicts reads ``unsupported`` even when anchors were left
out, and the envelope and the notice say how many anchors were not read. A facet with any supporting or
contradicting read item keeps its verdict; a facet whose assessment failed, raised or was skipped stays
``unassessed``. The model call is stubbed at the dispatcher, as in ``tests/test_rdr232_measurement_counters.py``;
the per-call size cap is patched down so a handful of fixture items stands in for the 301.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nexus.plans import coverage_ledger, switches
from nexus.plans.answer_shape import AnswerShape, render_non_answer_notice
from nexus.plans.coverage_ledger import CoverageBoundary, state_for
from nexus.plans.coverage_outcome import render_insufficient_evidence
from tests.test_rdr232_coverage_ledger import (
    Dispatcher,
    _cov,
    _request,
    _tag_lines,
    returns_no_records,
    supports_nothing,
)
from tests.test_rdr232_coverage_repair import _run
from tests.test_rdr232_coverage_wiring import _drive
from tests.test_rdr232_coverage_wiring import _request as wiring_request

NO_REPAIR = {"max_repair_queries": 0}  # the first-round cases: a repair would read the unread anchor (tested below)
CAP = 10  # characters one assessment call may carry: the fixture's vector items are longer, so that call leaves one out


@pytest.fixture(autouse=True)
def _coverage_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite pins the coverage check off (tests/conftest.py); the end-to-end test drives ``nx_answer``."""
    monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "1")


@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


def supports_second_item(items: str, instruction: str) -> dict:
    """The first item is read neutral and the second supports every facet the call lists: the supporting item is
    one the cap did NOT leave out, beside items it did."""
    payload = json.loads(items)
    pick = payload[1]["id"] if len(payload) > 1 else payload[0]["id"]
    return {"ok": True, "evidence": [
        {"item_id": pick, "quote": f"[{t}] it says so", "role": "supports"} for t in _tag_lines(instruction)
    ]}


@pytest.mark.asyncio
async def test_n04_shape_many_anchors_cap_hit_all_read_neutral_withholds() -> None:
    d = Dispatcher(check=supports_nothing)
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        cov = _cov(await _run(d, CoverageBoundary(request=_request(2), **NO_REPAIR)))
    assessment = cov.ledger.assessment
    assert assessment.skipped_items > 0, "the case is the one where anchors were left out of the calls"
    assert assessment.outcome == "ok" and assessment.calls > 0
    assert [f.state for f in cov.ledger.findings] == ["unsupported", "unsupported"]
    assert (cov.state, cov.reason) == ("unavailable", "uncovered") and cov.skip_generation
    assert "generate" not in d.tools, "the answer is withheld, not paid for"
    assert cov.unassessed_by_cause["other"] == 0


@pytest.mark.asyncio
async def test_a_supporting_read_item_among_omitted_anchors_still_covers() -> None:
    d = Dispatcher(check=supports_second_item)
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 10_000):
        whole = _cov(await _run(Dispatcher(check=supports_second_item), CoverageBoundary(request=_request(2), **NO_REPAIR)))
    assert whole.ledger.assessment.skipped_items == 0
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 40):
        cov = _cov(await _run(d, CoverageBoundary(request=_request(2), **NO_REPAIR)))
    assert cov.ledger.assessment.skipped_items > 0
    assert [f.state for f in cov.ledger.findings] == [f.state for f in whole.ledger.findings] == ["covered", "covered"]
    assert cov.state == whole.state and not cov.skip_generation
    assert "generate" in d.tools


def contradicts_first_item(items: str, instruction: str) -> dict:
    first = json.loads(items)[0]["id"]
    return {"ok": False, "evidence": [
        {"item_id": first, "quote": f"[{t}] says the opposite", "role": "contradicts"} for t in _tag_lines(instruction)
    ]}


@pytest.mark.asyncio
async def test_a_contradicting_read_item_among_omitted_anchors_keeps_the_contradicted_verdict() -> None:
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 40):
        cov = _cov(await _run(Dispatcher(check=contradicts_first_item), CoverageBoundary(request=_request(2), **NO_REPAIR)))
    assert cov.ledger.assessment.skipped_items > 0
    assert [f.state for f in cov.ledger.findings] == ["contradicted", "contradicted"]


@pytest.mark.asyncio
async def test_a_failed_call_beside_omitted_anchors_leaves_the_facet_unassessed() -> None:
    """The vector call omitted anchors and replied neutral; the graph call replied nothing usable. The unusable
    reply is a failed assessment: the facets are not known unsupported."""
    def by_stream(items: str, instruction: str) -> dict:
        text = json.loads(items)[0]["text"]
        return supports_nothing(items, instruction) if text.startswith("vector") else returns_no_records(items, instruction)

    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        cov = _cov(await _run(Dispatcher(by_stream), CoverageBoundary(request=_request(2), **NO_REPAIR)))
    assert [f.state for f in cov.ledger.findings] == ["unassessed", "unassessed"]
    assert {f.reason for f in cov.ledger.findings} == {"assessment_unusable"}


def test_the_uncovered_notice_says_how_many_anchors_were_not_read() -> None:
    plain = render_non_answer_notice(AnswerShape.UNCOVERED, plan_id=7, step_count=3)
    told = render_non_answer_notice(AnswerShape.UNCOVERED, plan_id=7, step_count=3, unread_items=174)
    assert "174" in told and "174" not in plain
    assert "not read" in told
    assert render_non_answer_notice(AnswerShape.UNCOVERED, plan_id=7, step_count=3, unread_items=0) == plain
    # With the total handed to the check the statement is about what it READ, not about all the evidence.
    full = render_non_answer_notice(AnswerShape.UNCOVERED, plan_id=7, step_count=3, unread_items=174, total_items=301)
    assert "none of the 127 items it read covered a requested part" in full
    assert "174 of 301 retrieved items were not read" in full
    assert "none of it covered" in plain and "none of it covered" not in full


@pytest.mark.asyncio
async def test_the_partial_notice_says_how_many_anchors_were_not_read() -> None:
    d = Dispatcher(check=supports_second_item)
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 40):
        cov = _cov(await _run(d, CoverageBoundary(request=_request(2), **NO_REPAIR)))
    unread = cov.ledger.assessment.skipped_items
    assert unread > 0
    from dataclasses import replace

    from nexus.plans.coverage_ledger import FacetFinding

    partial = replace(cov, state="partial", ledger=replace(cov.ledger, findings=(
        cov.ledger.findings[0], FacetFinding("f2", "unsupported", (), "no_supporting_evidence"))))
    text = render_insufficient_evidence(partial, {"f1": "one", "f2": "two"}, "the prose")
    total = cov.ledger.assessment.anchors_total
    assert total > unread and f"{unread} of {total} retrieved items were not read" in text
    clean = replace(partial, ledger=replace(partial.ledger, assessment=replace(partial.ledger.assessment, skipped_items=0)))
    assert "not read" not in render_insufficient_evidence(clean, {"f1": "one", "f2": "two"}, "the prose")


@pytest.mark.asyncio
async def test_the_envelope_and_the_returned_notice_carry_the_unread_count_end_to_end() -> None:
    d = Dispatcher(check=supports_nothing)

    async def no_repair(self, view, first, targets):  # noqa: ARG001 — a repair would read the one unread anchor (tested below)
        return first

    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP), patch.object(CoverageBoundary, "_repair", no_repair):
        run = await _drive(plan_id=0, dispatcher=d, request=wiring_request(2))
    unread = run.summary["coverage"]["assessment_skipped_items"]
    assert unread > 0
    assert run.out["answer_shape"] == "uncovered" and "generate" not in d.tools
    total = run.summary["coverage"]["assessment_anchors_total"]
    assert total > unread
    assert f"{unread} of {total} retrieved items were not read" in run.out["final_text"]
    assert f"none of the {total - unread} items it read covered" in run.out["final_text"]


# ── state_for: the reason matches the outcome (nexus-jypmj.23) ───────────────────────────────────────────────


def test_state_for_names_an_ok_assessment_as_incomplete_not_failed() -> None:
    """Every facet unassessed while the assessment outcome is ``ok`` (no call failed or was skipped) is not a
    failed assessment; the reason may not say it was."""
    state, reason, skip = state_for(("unassessed", "unassessed"), assessment_outcome="ok", anchors=3, observed=True)
    assert (state, reason, skip) == ("unassessed", "assessment_incomplete", False)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("failed", "assessment_failed"), ("partial_failure", "assessment_failed"),
     ("skipped_deadline", "assessment_skipped_deadline"), ("skipped_budget", "assessment_skipped_budget"),
     ("not_attempted", "assessment_not_attempted")],
)
def test_state_for_keeps_the_reasons_of_the_other_outcomes(outcome: str, reason: str) -> None:
    assert state_for(("unassessed",), assessment_outcome=outcome, anchors=3, observed=True) == ("unassessed", reason, False)


# ── the count is true and stated once; a repair reads the unread anchors (nexus-jypmj.23, round 2) ────────────────


def _unread_item_run(n_facets: int, d, kw: dict | None = None):
    """The capped run of the fixture plan: the vector stream has two items, so a 10-character cap leaves exactly one
    of them unread (the second); the graph stream's one item is read."""
    return _run(d, CoverageBoundary(request=_request(n_facets), **(kw or {})))


@pytest.mark.asyncio
async def test_seven_facets_two_groups_count_the_unread_item_once() -> None:
    """Seven facets are two groups of the call cap (6). The one item the cap left out of the vector stream was
    counted once per group (attached to each group's last pack), so a seven-facet request said 2 of 3 items."""
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        cov = _cov(await _unread_item_run(7, Dispatcher(check=supports_nothing), NO_REPAIR))
    a = cov.ledger.assessment
    assert len(cov.ledger.anchors) == 3 and a.anchors_total == 3
    assert sorted(set(a.facets_per_call)) == [1, 6], "two facet groups (6 and 1), so the old sum had two terms"
    assert a.skipped_items == 1 and a.unread_ids == (cov.ledger.anchors[1].item_id,)
    assert [f.state for f in cov.ledger.findings] == ["unsupported"] * 7


@pytest.mark.asyncio
async def test_a_stream_whose_calls_are_all_skipped_keeps_its_unread_count() -> None:
    """A budget-skipped call does not make the items the cap left out of its stream vanish from the count."""
    from nexus.plans.coverage_ledger import BoundaryView

    first = await _unread_item_run(2, Dispatcher(check=supports_nothing))
    prior = _cov(first)
    view = BoundaryView(
        evidence_texts=[a.content for a in prior.ledger.anchors], observed=True, step_outputs=list(first.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[],
        check=None, remaining_usd=lambda: 0.0, remaining_seconds=lambda: 300.0,
    )
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        cov = await CoverageBoundary(request=_request(2)).evaluate(view)
    assert cov.ledger.assessment.outcome == "skipped_budget" and cov.ledger.assessment.calls == 0
    assert cov.ledger.assessment.skipped_items == 1


def _by_topic():
    from tests.test_rdr232_coverage_repair import by_topic

    return by_topic()


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_hits", [[], ["vector hit about the gate"]], ids=["no_hits", "hit_is_the_unread_anchor"])
async def test_repair_reads_the_unread_anchor_and_a_facet_supported_there_becomes_covered(repair_hits) -> None:
    """f2's support ("gate") sits in the second vector item, which the cap left unread in the first round. The
    targeted query finds nothing new (or only that held chunk): the repair still reassesses the unread anchor, so
    f2 ends covered and the unread count drops to 0. Before this fix the repair was blind to unread anchors (they
    counted as held), ended ``no_new_evidence`` and f2 stayed unsupported."""
    from tests.test_rdr232_coverage_repair import RepairDispatcher

    d = RepairDispatcher(_by_topic(), repair_hits=repair_hits)
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        cov = _cov(await _unread_item_run(2, d))
    assert cov.repair.outcome == "completed", "repair is entered on the capped path and does not stop at no_new_evidence"
    assert [f.state for f in cov.ledger.findings] == ["covered", "covered"]
    assert cov.ledger.assessment.skipped_items == 0 and cov.ledger.rounds == 2
    # bounded: the first round's two streams, then ONE delta pass over the one unread anchor (one stream, one group)
    assert d.tools.count("operator_check") == 3
    last = d.args_of("operator_check")[-1]
    assert _tag_lines(last["check_instruction"]) == ["f2"] and [i["text"] for i in json.loads(last["items"])] == ["vector hit about the gate"]


@pytest.mark.asyncio
async def test_a_repair_that_leaves_the_same_anchor_unread_does_not_count_it_twice() -> None:
    """f2 and f3 are unsupported after the first round (one anchor unread). The repair brings new evidence for f3,
    which takes the one slot the cap leaves in the vector stream, so the unread anchor stays unread: the final count
    is that one anchor, not one per round (merged rounds used to add the counts)."""
    from tests.test_rdr232_coverage_repair import REPAIR_TEXT, RepairDispatcher

    d = RepairDispatcher(_by_topic())
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        cov = _cov(await _unread_item_run(3, d))
    a = cov.ledger.assessment
    assert cov.repair.outcome == "completed" and a.calls > 2
    assert {f.facet_id: f.state for f in cov.ledger.findings}["f3"] == "covered"
    assert any(x.content == REPAIR_TEXT for x in cov.ledger.anchors)
    assert a.skipped_items == 1 and len(a.unread_ids) == 1 and a.skipped_items <= a.anchors_total == len(cov.ledger.anchors)


def test_the_reassessment_estimate_counts_the_unread_anchors_packs() -> None:
    from nexus.plans.coverage_repair import planned_reassessment_calls

    assert planned_reassessment_calls(2, 6) == 1
    assert planned_reassessment_calls(2, 6, extra_items=174, items_per_call=20) == 10
    assert planned_reassessment_calls(7, 6, extra_items=1, items_per_call=20) == 4
