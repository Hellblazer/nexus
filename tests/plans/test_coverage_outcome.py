# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 step 3 (nexus-jypmj.3.3): the post-generation half of the coverage-state table.

``decide_outcome`` turns the boundary's pre-generation state plus the shape the generated prose classifies as
into the result's ``answer_shape``, final coverage state, counter outcome and no-grow flag. Pure: no model, no
store. The table rows each test pins are named in its docstring.
"""
from __future__ import annotations

import pytest

from nexus.plans.answer_shape import (
    INSUFFICIENT_EVIDENCE_NOTICE_PREFIX,
    NON_ANSWER_SHAPES,
    AnswerShape,
    classify_answer_shape,
)
from nexus.plans.coverage_ledger import AssessmentReport, CoverageLedger, CoverageResult, FacetFinding
from nexus.plans.coverage_outcome import decide_outcome, render_insufficient_evidence


def _cov(state: str, reason: str, findings: tuple[FacetFinding, ...], *, skip: bool = False) -> CoverageResult:
    return CoverageResult(
        state=state, reason=reason, skip_generation=skip,  # type: ignore[arg-type]
        ledger=CoverageLedger(findings=findings, assessment=AssessmentReport(outcome="ok", calls=1, facets_per_call=(len(findings),))),
    )


COVERED = FacetFinding("f1", "covered", ("e1",))
UNSUPPORTED = FacetFinding("f2", "unsupported", (), "no_supporting_evidence")
CONTRADICTED = FacetFinding("f3", "contradicted", ("e2",))
UNASSESSED = FacetFinding("f4", "unassessed", (), "assessment_failed")


def test_complete_with_an_answer_is_answered_and_grows_as_today() -> None:
    v = decide_outcome(_cov("complete", "all_covered", (COVERED,)), AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.ANSWERED and v.success and not v.no_grow
    assert v.coverage is not None and v.coverage.state == "complete"


def test_partial_with_a_usable_answer_is_insufficient_evidence_success_no_grow() -> None:
    """Table row ``partial``: shape insufficient_evidence, success_count, growth blocked."""
    cov = _cov("partial", "unresolved_facets", (COVERED, UNSUPPORTED))
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.INSUFFICIENT_EVIDENCE
    assert v.success is True and v.no_grow is True
    assert v.coverage is not None and v.coverage.state == "partial"
    assert AnswerShape.INSUFFICIENT_EVIDENCE not in NON_ANSWER_SHAPES


def test_partial_whose_prose_is_a_payload_has_no_usable_answer_and_is_unavailable() -> None:
    """A usable answer needs prose ``classify_answer_shape`` reads as answered: the classified non-answer
    shape stands, the state becomes unavailable, the counter is a failure."""
    cov = _cov("partial", "unresolved_facets", (COVERED, UNSUPPORTED))
    v = decide_outcome(cov, AnswerShape.HYDRATION_DUMP)
    assert v.shape is AnswerShape.HYDRATION_DUMP and not v.success and v.no_grow
    assert v.coverage is not None and (v.coverage.state, v.coverage.reason) == ("unavailable", "no_usable_answer")


def test_complete_whose_prose_is_empty_is_unavailable_too() -> None:
    v = decide_outcome(_cov("complete", "all_covered", (COVERED,)), AnswerShape.EMPTY)
    assert v.shape is AnswerShape.EMPTY and not v.success
    assert v.coverage is not None and v.coverage.state == "unavailable"


@pytest.mark.parametrize("known_gap", [UNSUPPORTED, CONTRADICTED])
def test_prose_with_no_covered_facet_but_an_unassessed_one_keeps_the_prose_as_partial(known_gap: FacetFinding) -> None:
    """RDR-232 fix round (critique S2): an unassessed facet is not known to be uncovered. The pre-generation skip
    already requires none unassessed; the post-generation withhold must agree, so the generated prose is kept as a
    partial answer and the notice names the unassessed facet as not assessed."""
    cov = _cov("partial", "unresolved_facets", (known_gap, UNASSESSED))  # generation ran: one facet was unknown
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.INSUFFICIENT_EVIDENCE and v.success and v.no_grow
    assert v.coverage is cov and v.coverage.state == "partial", "the state is not rewritten to unavailable"
    out = render_insufficient_evidence(cov, {"f2": "a", "f3": "b", "f4": "what it cost"}, "The prose.")
    assert out.startswith("[insufficient-evidence: 2 unresolved]") and "f4 (not assessed): what it cost" in out
    assert out.endswith("The prose.")


def test_prose_with_no_covered_facet_and_none_unassessed_is_uncovered() -> None:
    """Sam's ruling (nexus-jypmj.3.12): prose with no covered facet, every facet known, is unavailable and its shape
    is ``uncovered``. (Reached when the skip switch is off or a ledger was built by hand: the boundary skips first.)"""
    cov = _cov("partial", "unresolved_facets", (UNSUPPORTED, FacetFinding("f3", "unsupported", (), "no_supporting_evidence")))
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.UNCOVERED and not v.success and v.no_grow
    assert v.coverage is not None and (v.coverage.state, v.coverage.reason) == ("unavailable", "uncovered")


def test_prose_whose_only_settled_facet_is_contradicted_is_a_partial_answer() -> None:
    """Sam's ruling 2026-10-27 (nexus-jypmj.3.13): a contradicted facet counts toward a usable answer, so the prose
    is kept behind the notice, which names the contradiction; a success, never grown."""
    cov = _cov("partial", "unresolved_facets", (UNSUPPORTED, CONTRADICTED))
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.INSUFFICIENT_EVIDENCE and v.success and v.no_grow
    assert v.coverage is not None and v.coverage.state == "partial"


def test_a_skipped_generation_is_uncovered_failure() -> None:
    """The boundary skipped the reduction (nothing covered): shape uncovered, failure, never grown."""
    cov = _cov("unavailable", "uncovered", (UNSUPPORTED, FacetFinding("f2b", "contradicted", ("e1",))), skip=True)
    v = decide_outcome(cov, AnswerShape.ANSWERED)  # the skipped output's marker text classifies as prose
    assert v.shape is AnswerShape.UNCOVERED and not v.success and v.no_grow
    assert AnswerShape.UNCOVERED in NON_ANSWER_SHAPES


@pytest.mark.parametrize("reason", ["no_evidence", "scope_conflict"])
def test_unavailable_without_evidence_is_no_evidence_not_uncovered(reason: str) -> None:
    """``no_evidence`` keeps its meaning (nothing reached the reduction); ``uncovered`` is for evidence that
    covered no facet."""
    cov = _cov("unavailable", reason, (UNSUPPORTED,))
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.NO_EVIDENCE and not v.success and v.no_grow
    assert v.coverage is not None and v.coverage.reason == reason


@pytest.mark.parametrize("coverage", [None, _cov("unassessed", "assessment_failed", (UNASSESSED,))])
@pytest.mark.parametrize("shape", [AnswerShape.ANSWERED, AnswerShape.RETRIEVAL_ONLY, AnswerShape.EMPTY])
def test_unassessed_keeps_today_exactly(coverage: CoverageResult | None, shape: AnswerShape) -> None:
    """Table row ``unassessed``: the classified shape, success iff it is an answer, growth as today."""
    v = decide_outcome(coverage, shape)
    assert v.shape is shape and v.success is (shape not in NON_ANSWER_SHAPES) and not v.no_grow
    assert v.coverage is coverage, "an unassessed ledger is never rewritten"


def test_the_vocabulary_names_both_new_shapes() -> None:
    assert AnswerShape.INSUFFICIENT_EVIDENCE.value == "insufficient_evidence"
    assert AnswerShape.UNCOVERED.value == "uncovered"


# ── the marker notice ───────────────────────────────────────────────────────────────────────────────────────


def test_the_notice_opens_with_the_marker_names_each_unresolved_facet_then_gives_the_prose() -> None:
    cov = _cov("partial", "unresolved_facets", (COVERED, UNSUPPORTED, CONTRADICTED, UNASSESSED))
    texts = {"f1": "what was decided", "f2": "who approved it", "f3": "when it shipped", "f4": "what it cost"}
    out = render_insufficient_evidence(cov, texts, "The decision was X.")
    assert out.startswith("[insufficient-evidence: 3 unresolved]\n")
    assert out.startswith(INSUFFICIENT_EVIDENCE_NOTICE_PREFIX)
    head, _, prose = out.partition("\n\n")
    assert prose == "The decision was X."
    for fid in ("f2", "f3", "f4"):
        assert f"{fid} " in head and texts[fid] in head
    assert "f1" not in head and "what was decided" not in head, "a covered facet is not listed as unresolved"
    assert "no supporting evidence" in head and "contradict" in head and "not assessed" in head


def test_a_covered_facet_with_conflicting_evidence_is_named_apart_from_the_unresolved() -> None:
    """Mixed evidence on one facet is covered (the supporting evidence answers it) and marked conflicted; the notice
    says the evidence conflicts without counting the facet among the unresolved."""
    conflicted = FacetFinding("f1", "covered", ("e1", "e2"), "conflicted")
    cov = _cov("partial", "unresolved_facets", (conflicted, UNSUPPORTED))
    out = render_insufficient_evidence(cov, {"f1": "what was decided", "f2": "who approved it"}, "The decision was X.")
    head, _, prose = out.partition("\n\n")
    assert prose == "The decision was X." and out.startswith("[insufficient-evidence: 1 unresolved]\n")
    assert "conflict" in head and "f1" in head and "what was decided" in head
    assert "f2 (no supporting evidence found)" in head


def test_the_notice_marker_carries_no_facet_text() -> None:
    """The marker line survives ``trace=False`` redaction, so it holds the count and nothing the caller wrote."""
    cov = _cov("partial", "unresolved_facets", (COVERED, UNSUPPORTED))
    out = render_insufficient_evidence(cov, {"f2": "SECRET-FACET-TEXT"}, "prose")
    first = out.splitlines()[0]
    assert first == "[insufficient-evidence: 1 unresolved]" and "SECRET" not in first


def test_a_missing_source_is_named_as_such_and_long_facet_text_is_capped() -> None:
    cov = _cov("partial", "unresolved_facets", (COVERED, FacetFinding("f2", "unsupported", (), "source_missing")))
    out = render_insufficient_evidence(cov, {"f2": "x" * 5000}, "prose")
    assert "named source" in out and len(out) < 1500


def test_the_notice_does_not_classify_as_a_non_answer() -> None:
    cov = _cov("partial", "unresolved_facets", (COVERED, UNSUPPORTED))
    out = render_insufficient_evidence(cov, {"f2": "a"}, "A real answer.")
    assert classify_answer_shape(out) is AnswerShape.ANSWERED
