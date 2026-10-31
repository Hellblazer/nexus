# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 coverage repair, a covered facet whose anchors are mostly unread (nexus-z6yvk; Sam, 2026-10-31).

The measured case (RDR-232 close measurements, constructed exit-69 debug request, plan 467): 185 of 305 anchors
were left unread by the per-call size cap. 14 items carried the answer, 4 in the read part covered the facet, 10 sat
in the unread tail. A facet covered from the read part never entered repair, so the tail was never read.

The ruling: run the repair when MOST of a facet's anchors are unread (strictly more than half), within the existing
repair budget. Every facet is assessed against every anchor of the ledger, so a facet's unread share is the ledger's.
An unsupported facet is still repaired first; a covered facet that qualifies is repaired after it. The covered
facet stays covered whatever the tail says (a repair reads more, it never takes support away); a supporting tail
item adds to its evidence and a contradicting one marks it ``conflicted``. The repair report names why it ran.

Real ``plan_run`` and boundary; the model call is stubbed at the dispatcher, as in ``tests/test_rdr232_unread_anchors.py``.
The size cap is patched down so a dozen fixture items stand in for the 305.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from nexus.plans import coverage_ledger
from nexus.plans.coverage_ledger import CoverageBoundary
from nexus.plans.coverage_repair import MAX_REPAIR_ITEMS, MAX_REPAIR_QUERIES
from tests import test_rdr232_coverage_ledger as ledger_fixture
from tests.test_rdr232_coverage_ledger import _cov, _request, _tag_lines
from tests.test_rdr232_coverage_repair import RepairDispatcher, _run

#: Characters one assessment call may carry. Every vector item below is 20 characters, so a call reads four of them.
CAP = 90
#: The vector stream: HEAD is read, FILL items are neutral, TAIL sits right after the read part (the first unread
#: item), so the repair's own size-capped pass reaches it. The graph stream is one item that is always read.
READ_PER_STREAM = 4


@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


def _vector_texts(n: int) -> list[str]:
    texts = [f"vector FILL item {i:02d}" for i in range(n)]
    texts[0] = "vector HEAD item 00"
    texts[READ_PER_STREAM] = f"vector TAIL item {READ_PER_STREAM:02d}"
    return texts


def _check(*, tail: str | None, only: tuple[str, ...] = ("f1",)):
    """Supports ``f1`` with the HEAD item, and with the TAIL item per *tail* (``supports``, ``contradicts`` or None).
    Any other facet (the unsupported one) is supported by nothing; a reply with no record at all is an unusable
    assessment, so a neutral record stands in."""

    def check(items: str, instruction: str) -> dict:
        payload = json.loads(items)
        evidence: list[dict[str, str]] = []
        for tag in _tag_lines(instruction):
            if tag not in only:
                continue
            for item in payload:
                if "HEAD" in item["text"]:
                    evidence.append({"item_id": item["id"], "quote": f"[{tag}] head", "role": "supports"})
                elif "TAIL" in item["text"] and tail is not None:
                    evidence.append({"item_id": item["id"], "quote": f"[{tag}] tail", "role": tail})
        for tag in _tag_lines(instruction):
            if not any(e["quote"].startswith(f"[{tag}]") for e in evidence):
                evidence.append({"item_id": payload[0]["id"], "quote": f"[{tag}] only mentions the topic", "role": "neutral"})
        return {"ok": True, "evidence": evidence}

    return check


async def _drive(vector: int, *, facets: int = 1, tail: str | None = "supports", repair_hits: list[str] | None = None, **boundary: Any):
    d = RepairDispatcher(_check(tail=tail), repair_hits=repair_hits)
    with patch.object(ledger_fixture, "VECTOR_TEXTS", _vector_texts(vector)), \
         patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", CAP):
        result = await _run(d, CoverageBoundary(request=_request(facets), **boundary))
    return d, _cov(result)


def _anchor(cov, fragment: str):
    return next(a for a in cov.ledger.anchors if fragment in a.content)


# ── the threshold: strictly more than half ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vector", "unread", "anchors", "enters"),
    [(8, 4, 9, False), (9, 5, 10, False), (10, 6, 11, True), (12, 8, 13, True)],
    ids=["under_half", "exactly_half", "just_over_half", "the_measured_shape"],
)
async def test_a_covered_facet_enters_repair_only_when_more_than_half_its_anchors_are_unread(
    vector: int, unread: int, anchors: int, enters: bool,
) -> None:
    _, first = await _drive(vector, max_repair_queries=0)
    assert (first.ledger.assessment.skipped_items, first.ledger.assessment.anchors_total) == (unread, anchors), (
        "the fixture leaves exactly this many of this many anchors unread in the first round")
    d, cov = await _drive(vector)
    assert (cov.repair.outcome != "not_attempted") is enters
    assert cov.repair_entered is enters
    if enters:
        assert cov.ledger.rounds == 2, "one repair round, the reassessment"
    else:
        assert d.repair_searches() == [] and cov.ledger.rounds == 1
    f1 = cov.ledger.finding("f1")
    assert f1 is not None and f1.state == "covered", "covered from the read part either way"


@pytest.mark.asyncio
async def test_the_first_round_really_left_the_counted_anchors_unread() -> None:
    """Pins the fixture's arithmetic with the repair switched off: 12 vector items + 1 graph item, 8 unread."""
    _, cov = await _drive(12, max_repair_queries=0)
    a = cov.ledger.assessment
    assert (a.skipped_items, a.anchors_total) == (8, 13) and cov.repair.outcome == "not_attempted"


@pytest.mark.asyncio
async def test_repair_switched_off_stays_off_whatever_the_unread_share() -> None:
    d, cov = await _drive(12, max_repair_queries=0)
    assert cov.repair.outcome == "not_attempted" and d.repair_searches() == []


# ── what the repair does for such a facet ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_tail_is_read_and_a_supporting_tail_item_joins_the_covered_facets_evidence() -> None:
    d, cov = await _drive(12, tail="supports")
    head, tail = _anchor(cov, "HEAD"), _anchor(cov, "TAIL")
    f1 = cov.ledger.finding("f1")
    assert f1 is not None and f1.state == "covered" and f1.reason == ""
    assert set(f1.evidence_ids) == {head.item_id, tail.item_id}, "the read part's support is kept, the tail's is added"
    assert tail.item_id in cov.ledger.assessment.read_ids
    assert cov.ledger.assessment.skipped_items < 8, "the repair read part of the unread tail"
    last = d.args_of("operator_check")[-1]
    assert _tag_lines(last["check_instruction"]) == ["f1"], "only the tail facet is reassessed"
    assert any("TAIL" in i["text"] for i in json.loads(last["items"])), "against the unread anchors"
    assert not any("HEAD" in i["text"] for i in json.loads(last["items"])), "not against what was already read"


@pytest.mark.asyncio
async def test_a_tail_that_says_nothing_never_takes_the_read_parts_support_away() -> None:
    _, cov = await _drive(12, tail=None)
    f1 = cov.ledger.finding("f1")
    head = _anchor(cov, "HEAD")
    assert f1 is not None and (f1.state, f1.reason, f1.evidence_ids) == ("covered", "", (head.item_id,))
    assert cov.repair.outcome != "not_attempted" and cov.state == "complete"


@pytest.mark.asyncio
async def test_a_contradicting_tail_item_marks_the_covered_facet_conflicted_and_keeps_it_covered() -> None:
    _, cov = await _drive(12, tail="contradicts")
    f1 = cov.ledger.finding("f1")
    assert f1 is not None and f1.state == "covered" and f1.reason == "conflicted"
    assert set(f1.evidence_ids) == {_anchor(cov, "HEAD").item_id, _anchor(cov, "TAIL").item_id}
    assert cov.conflicted_facet_ids == ("f1",)


# ── the envelope says why the repair ran ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_repair_report_names_the_unread_tail_as_the_reason_and_the_facet() -> None:
    _, cov = await _drive(12)
    r = cov.repair
    assert (r.reason, r.facet_ids, r.unread_facet_ids) == ("unread_tail", ("f1",), ("f1",))
    wire = r.to_wire()
    assert wire["reason"] == "unread_tail" and wire["unread_facet_ids"] == ["f1"] and wire["facet_ids"] == ["f1"]
    for key in ("outcome", "facet_ids", "queries", "items_found", "items_merged", "latency_ms", "detail", "noted_facets"):
        assert key in wire, "the existing keys stay"
    assert cov.counts()["repair_entered"] is True and cov.counts()["repair_facets"] == 1


@pytest.mark.asyncio
async def test_an_unsupported_facet_alone_keeps_its_old_reason_and_no_unread_facets() -> None:
    _, cov = await _drive(8, facets=2)  # f2 is supported by nothing; 4 of 9 anchors unread: no tail facet
    assert cov.ledger.finding("f2").state == "unsupported"
    assert (cov.repair.reason, cov.repair.facet_ids, cov.repair.unread_facet_ids) == ("unsupported", ("f2",), ())


@pytest.mark.asyncio
async def test_a_report_that_did_not_run_has_no_reason() -> None:
    _, cov = await _drive(8)
    assert cov.repair.outcome == "not_attempted" and cov.repair.reason == "" and cov.repair.unread_facet_ids == ()


# ── an unsupported facet goes first; the budget is the one it always was ─────────────────────────────────────


@pytest.mark.asyncio
async def test_unsupported_facets_are_queried_before_unread_tail_facets() -> None:
    """f1 is covered with most anchors unread, f2 is unsupported. With room for one query, f2 gets it."""
    d, cov = await _drive(12, facets=2, max_repair_queries=1)
    texts = [a["query"] for a in d.repair_searches()]
    assert texts == ["what the document says about topic 2"]
    assert (cov.repair.reason, cov.repair.facet_ids, cov.repair.unread_facet_ids) == (
        "unsupported_and_unread_tail", ("f2", "f1"), ("f1",)), "unsupported first in the report as well"


@pytest.mark.asyncio
async def test_with_room_for_both_the_tail_facet_is_queried_second() -> None:
    d, _ = await _drive(12, facets=2, max_repair_queries=2)
    assert [a["query"] for a in d.repair_searches()] == [
        "what the document says about topic 2", "what the document says about topic 1"]


@pytest.mark.asyncio
async def test_the_repair_budget_is_unchanged_and_still_binds_a_tail_repair() -> None:
    assert (MAX_REPAIR_ITEMS, MAX_REPAIR_QUERIES) == (12, 6), "Sam's ruling spends the existing budget, it does not raise it"
    hits = [f"repair hit number {i:02d} with some text" for i in range(30)]
    d, cov = await _drive(12, repair_hits=hits)
    assert len(d.repair_searches()) == 1, "one tail facet, one plan target, one query"
    assert 0 < cov.repair.items_merged <= MAX_REPAIR_ITEMS
    assert cov.repair.queries <= MAX_REPAIR_QUERIES and cov.ledger.rounds == 2
    assert d.tools.count("generate") == 1
