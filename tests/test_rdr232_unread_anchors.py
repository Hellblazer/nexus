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
        cov = _cov(await _run(d, CoverageBoundary(request=_request(2))))
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
        whole = _cov(await _run(Dispatcher(check=supports_second_item), CoverageBoundary(request=_request(2))))
    assert whole.ledger.assessment.skipped_items == 0
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 40):
        cov = _cov(await _run(d, CoverageBoundary(request=_request(2))))
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
        cov = _cov(await _run(Dispatcher(check=contradicts_first_item), CoverageBoundary(request=_request(2))))
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
        cov = _cov(await _run(Dispatcher(by_stream), CoverageBoundary(request=_request(2))))
    assert [f.state for f in cov.ledger.findings] == ["unassessed", "unassessed"]
    assert {f.reason for f in cov.ledger.findings} == {"assessment_unusable"}


def test_the_uncovered_notice_says_how_many_anchors_were_not_read() -> None:
    plain = render_non_answer_notice(AnswerShape.UNCOVERED, plan_id=7, step_count=3)
    told = render_non_answer_notice(AnswerShape.UNCOVERED, plan_id=7, step_count=3, unread_items=174)
    assert "174" in told and "174" not in plain
    assert "not read" in told
    assert render_non_answer_notice(AnswerShape.UNCOVERED, plan_id=7, step_count=3, unread_items=0) == plain


@pytest.mark.asyncio
async def test_the_partial_notice_says_how_many_anchors_were_not_read() -> None:
    d = Dispatcher(check=supports_second_item)
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 40):
        cov = _cov(await _run(d, CoverageBoundary(request=_request(2))))
    unread = cov.ledger.assessment.skipped_items
    assert unread > 0
    from dataclasses import replace

    from nexus.plans.coverage_ledger import FacetFinding

    partial = replace(cov, state="partial", ledger=replace(cov.ledger, findings=(
        cov.ledger.findings[0], FacetFinding("f2", "unsupported", (), "no_supporting_evidence"))))
    text = render_insufficient_evidence(partial, {"f1": "one", "f2": "two"}, "the prose")
    assert f"{unread} retrieved item(s) were not read" in text
    clean = replace(partial, ledger=replace(partial.ledger, assessment=replace(partial.ledger.assessment, skipped_items=0)))
    assert "not read" not in render_insufficient_evidence(clean, {"f1": "one", "f2": "two"}, "the prose")


@pytest.mark.asyncio
async def test_the_envelope_and_the_returned_notice_carry_the_unread_count_end_to_end() -> None:
    d = Dispatcher(check=supports_nothing)
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        run = await _drive(plan_id=0, dispatcher=d, request=wiring_request(2))
    unread = run.summary["coverage"]["assessment_skipped_items"]
    assert unread > 0
    assert run.out["answer_shape"] == "uncovered" and "generate" not in d.tools
    assert f"{unread} retrieved item(s) were not read" in run.out["final_text"]


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
