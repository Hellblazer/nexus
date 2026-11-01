# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 (nexus-x3eoc rulings A and B, Sam 2026-10-31): what a request whose extraction did not run is judged by.

A request whose structured extraction call failed (``error``, ``timeout``, ``invalid``) or was needed and skipped for
lack of time budget (``skipped_deadline``) keeps its first-pass kind. Its answer-kind gate runs in shadow: the rows of
the matrix report what they would exclude and exclude nothing (``extraction_failed`` / ``extraction_skipped``). The
one-source rule (nexus-4xmpv: a request naming exactly one source is not given citation-traversal) is NOT one of those
rows; it keeps excluding, and the envelope lists it in ``enforced_reasons``. A request that needed no call
(``not_needed``) enforces everything. The health counter still counts real failures only.

Real matcher over a fake library, as in ``tests/test_rdr232_eligibility.py``.
"""
from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from nexus.plans import eligibility as el
from nexus.plans import request_coverage as rc
from tests.test_rdr232_eligibility import (
    CITATION_467,
    G06,
    LOOKUP_473,
    SYNTH_12,
    _Cache,
    _drive,
    _extraction_that_works,
    _Library,
    _single_source_requests,
    real_plan_match,
)
from tests.test_rdr232_gate_defaults import _FailingDispatch

#: A one-source request the first pass leaves ``unknown``: it needs the extraction call, and a failed call leaves it so.
S01 = _single_source_requests()[1]
HITS_ONE_SOURCE = [(467, 0.01), (473, 0.1), (12, 0.4)]
LIBRARY_ONE_SOURCE = [CITATION_467, LOOKUP_473, SYNTH_12]
OPEN_QUESTION = "Tell me about plan matching."
HITS_KIND = [(473, 0.1), (12, 0.4)]


@pytest.fixture
def failing_extraction(monkeypatch: pytest.MonkeyPatch) -> _FailingDispatch:
    fake = _FailingDispatch(RuntimeError("claude exited 1"))
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", fake)
    return fake


# ── the filter ───────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", ["extraction_failed", "extraction_skipped"])
def test_the_one_source_exclusion_is_enforced_in_those_shadows_and_the_kind_rows_are_not(reason: str) -> None:
    filt = el.EligibilityFilter(
        "unknown", "unknown", enforce=False, shadow_reason=reason, named_source_count=1,
    )
    out = real_plan_match(
        "q", library=_Library(LIBRARY_ONE_SOURCE), cache=_Cache(HITS_ONE_SOURCE), eligibility=filt, n=5,
    )
    ids = [m.plan_id for m in out]
    assert 467 not in ids, "citation-traversal stays refused for a request naming one source"
    assert ids[0] == 473, "the kind row (an unknown request admits no lookup plan) is reported and not enforced"
    wire = filt.to_wire()
    assert wire["enforced"] is False and wire["shadow_reason"] == reason
    assert wire["enforced_reasons"] == ["single_source_synthesis"]
    assert wire["excluded_plan_ids"] == [467, 473] and wire["excluded_total"] == 2
    assert wire["branches"]["cosine"]["reasons"] == {"single_source_synthesis": 1, "unknown_request_narrow_plan": 1}
    assert filt.enforced_excluded_total == 1 and wire["enforced_excluded_total"] == 1


@pytest.mark.parametrize("reason", ["disabled_by_setting", "extraction_disabled", "library_not_reseeded"])
def test_every_other_shadow_enforces_nothing(reason: str) -> None:
    filt = el.EligibilityFilter("unknown", "unknown", enforce=False, shadow_reason=reason, named_source_count=1)
    out = real_plan_match(
        "q", library=_Library(LIBRARY_ONE_SOURCE), cache=_Cache(HITS_ONE_SOURCE), eligibility=filt, n=5,
    )
    assert [m.plan_id for m in out][0] == 467, "nothing is excluded: the best-scoring plan is returned"
    assert filt.enforced_reasons == () and filt.enforces_any is False and filt.to_wire()["enforced_reasons"] == []
    assert filt.enforced_excluded_total == 0


def test_a_filter_that_enforces_lists_no_shadow_enforced_reasons_and_counts_every_exclusion() -> None:
    filt = el.EligibilityFilter("unknown", "unknown", enforce=True, named_source_count=1)
    real_plan_match("q", library=_Library(LIBRARY_ONE_SOURCE), cache=_Cache(HITS_ONE_SOURCE), eligibility=filt, n=5)
    assert filt.enforces_any is True and filt.enforced_reasons == () and filt.enforced_excluded_total == 2


def test_the_enforced_reasons_belong_to_the_closed_vocabulary() -> None:
    assert el.SHADOW_REASONS == {
        "disabled_by_setting", "extraction_disabled", "extraction_failed", "extraction_skipped", "library_not_reseeded",
    }
    assert set(el.SHADOW_ENFORCED_REASONS) <= el.SHADOW_REASONS
    assert all(r == "single_source_synthesis" for rs in el.SHADOW_ENFORCED_REASONS.values() for r in rs)


def test_a_one_source_exclusion_in_a_shadow_that_enforces_it_is_why_nothing_matched() -> None:
    filt = el.EligibilityFilter("unknown", "unknown", enforce=False, shadow_reason="extraction_failed", named_source_count=1)
    out = real_plan_match("q", library=_Library([CITATION_467], fts=[467]), cache=_Cache([(467, 0.05)]), eligibility=filt, n=5)
    assert out == []
    assert el.fallback_reason(force_dynamic=False, matches=out, hit=False, filt=filt) == "all_candidates_ineligible"


def test_a_reported_only_exclusion_is_not_why_nothing_matched() -> None:
    filt = el.EligibilityFilter("lookup", "unknown", enforce=False, shadow_reason="extraction_failed")
    assert filt.admit(SYNTH_12, "cosine") is True, "a lookup request is refused a synthesis plan, in shadow only reported"
    assert filt.excluded_total == 1 and filt.enforced_excluded_total == 0
    assert el.fallback_reason(force_dynamic=False, matches=[], hit=False, filt=filt) == "no_plan_candidates"


# ── through nx_answer: ruling A, the one-source rule keeps enforcing when the extraction failed ──────────────


def test_a_one_source_synthesis_request_with_a_failed_extraction_is_not_given_citation_traversal(failing_extraction) -> None:
    env, miss, _ = _drive(S01, LIBRARY_ONE_SOURCE, HITS_ONE_SOURCE)
    assert failing_extraction.calls == 1 and env["request_coverage"]["extraction"]["outcome"] == "error"
    wire = env["plan_eligibility"]
    assert (wire["enforced"], wire["shadow_reason"]) == (False, "extraction_failed")
    assert wire["enforced_reasons"] == ["single_source_synthesis"]
    assert env["plan_id"] != 467, "citation-traversal outscored the others and is not selected"
    assert 467 in wire["excluded_plan_ids"] and wire["enforced_excluded_total"] == 1
    assert wire["branches"]["cosine"]["reasons"]["single_source_synthesis"] == 1
    assert wire["selected"]["plan_id"] == 473, "the answer-kind row is shadowed: the lookup plan is kept and reported"
    assert wire["selected"]["eligible"] is False and wire["selected"]["reason"] == "unknown_request_narrow_plan"


def test_a_one_source_request_with_only_citation_traversal_and_a_failed_extraction_goes_inline(failing_extraction) -> None:
    env, miss, _ = _drive(S01, [CITATION_467], [(467, 0.05)], fts=[467])
    assert env["plan_eligibility"]["fallback_reason"] == "all_candidates_ineligible"
    miss.assert_awaited()


def test_a_failed_extraction_without_a_named_source_enforces_no_exclusion(failing_extraction) -> None:
    env, miss, _ = _drive(OPEN_QUESTION, [LOOKUP_473, SYNTH_12], HITS_KIND)
    wire = env["plan_eligibility"]
    assert (wire["enforced"], wire["shadow_reason"], wire["enforced_reasons"]) == (False, "extraction_failed", ["single_source_synthesis"])
    assert wire["enforced_excluded_total"] == 0 and env["plan_id"] == 473 and not miss.called


# ── ruling B: skipped_deadline shadows the gate like a failed call; not_needed enforces ──────────────────────


def test_a_deadline_skipped_extraction_runs_the_gate_in_shadow_with_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FailingDispatch(RuntimeError("must not be called"))
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", fake)
    env, miss, _ = _drive(OPEN_QUESTION, [LOOKUP_473, SYNTH_12], HITS_KIND, budget_seconds=0.5)
    ext = env["request_coverage"]["extraction"]
    assert fake.calls == 0 and ext["outcome"] == "skipped_deadline" and ext["calls"] == 0
    wire = env["plan_eligibility"]
    assert (wire["enforced"], wire["shadow_reason"]) == (False, "extraction_skipped")
    assert wire["excluded_plan_ids"] == [473] and env["plan_id"] == 473 and not miss.called, "reported, not enforced"


def test_a_deadline_skipped_extraction_keeps_the_one_source_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", _FailingDispatch(RuntimeError("never")))
    env, _, _ = _drive(S01, LIBRARY_ONE_SOURCE, HITS_ONE_SOURCE, budget_seconds=0.5)
    wire = env["plan_eligibility"]
    assert env["request_coverage"]["extraction"]["outcome"] == "skipped_deadline"
    assert (wire["shadow_reason"], wire["enforced_reasons"]) == ("extraction_skipped", ["single_source_synthesis"])
    assert env["plan_id"] != 467 and 467 in wire["excluded_plan_ids"]


def test_a_request_that_needed_no_extraction_enforces_every_row(failing_extraction) -> None:
    """G06 is settled by the first pass (``not_needed``): no call, nothing to shadow, the gate enforces as before."""
    env, miss, _ = _drive(G06, [LOOKUP_473, SYNTH_12], HITS_KIND)
    assert env["request_coverage"]["extraction"]["outcome"] == "not_needed" and failing_extraction.calls == 0
    wire = env["plan_eligibility"]
    assert (wire["enforced"], wire["shadow_reason"], wire["enforced_reasons"]) == (True, None, [])
    assert wire["excluded_plan_ids"] == [473] and env["plan_id"] == 12


def test_a_working_extraction_enforces_every_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", _extraction_that_works)
    env, _, _ = _drive(OPEN_QUESTION, [LOOKUP_473, SYNTH_12], HITS_KIND)
    assert env["request_coverage"]["extraction"]["outcome"] == "unchanged"
    wire = env["plan_eligibility"]
    assert (wire["enforced"], wire["shadow_reason"], wire["enforced_reasons"]) == (True, None, [])


def test_a_skipped_extraction_is_not_a_failure_for_the_health_counter() -> None:
    """Ruling B: skipped_deadline shadows the gate but stays out of FAILED_OUTCOMES, so the persistent-failure
    warning and the doctor row count real failures only."""
    assert "skipped_deadline" not in rc.FAILED_OUTCOMES
    rc.reset_extraction_health()
    with capture_logs() as logs:
        fired = [rc.note_extraction_outcome("skipped_deadline", gate_enforcing=True) for _ in range(rc.PERSISTENT_FAILURE_THRESHOLD + 2)]
    assert not any(fired) and not [e for e in logs if e["event"] == "answer_kind_gate_extraction_failing"]
