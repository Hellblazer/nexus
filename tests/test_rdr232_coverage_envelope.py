# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.3.6): the structured envelope's top-level ``coverage`` object (RDR section 4).

The object is ``CoverageResult.to_wire(redact=not trace)`` for a run whose terminal reduction was assessed, at the
FINAL state the call returned (the same value ``run_summary.returned_coverage_state`` reports), and a small explicit
``unassessed`` object with a reason for a run that was not. It is always present. The continuation hand-off carries
the same object twice: top level and ``continuation.coverage``.

Two harnesses, both through the real ``nx_answer`` (model calls stubbed at the dispatcher):

* ``_drive`` with a ``dispatcher`` runs the real ``plan_run`` and boundary (tests/test_rdr232_coverage_wiring.py).
* ``_drive`` with a ``plan_result`` patches ``plan_run`` to return a fixed result, for the post-generation rules.
"""
from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nexus.mcp.core import nx_answer
from nexus.plans import switches
from nexus.plans.coverage_ledger import (
    AssessmentReport,
    CoverageBoundary,
    CoverageLedger,
    CoverageResult,
    EvidenceAnchor,
    FacetFinding,
    ScopeRow,
    SourceRow,
)
from nexus.plans.runner import PlanResult
from tests.test_nx_answer import _make_single_step_query_match
from tests.test_rdr232_coverage_ledger import Dispatcher, _request, supports_nothing
from tests.test_rdr232_coverage_repair import _handoff_patches, by_topic
from tests.test_rdr232_coverage_wiring import (
    COVERED,
    QUESTION,
    UNSUPPORTED,
    _FailingGenerate,
    _drive,
    _ledger_result,
    supports_f1_only,
)
from tests.test_rdr232_run_summary_wiring import FakeTelemetry, _db

@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    """The runner's non-evidentiary hygiene asks the catalog; these tests are tenant-free, so it answers 'nothing excluded'."""
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


#: Every key of the always-present object, with or without a boundary.
COVERAGE_KEYS = {
    "state", "reason", "skip_generation", "requested_facet_ids", "covered_facet_ids", "residual_gaps",
    "primary_source", "corpus_counts", "repair", "facets_required", "facets", "anchors", "rounds",
    "assessment", "findings", "sources", "scope",
}


def _gaps(coverage: dict[str, Any]) -> list[tuple[str, str]]:
    return [(g["facet_id"], g["state"]) for g in coverage["residual_gaps"]]


class TestAssessedRun:
    @pytest.mark.asyncio
    async def test_partial_is_partial_with_the_unresolved_facet_ids_and_insufficient_evidence(self) -> None:
        run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2))
        out = run.out
        cov = out["coverage"]
        assert set(cov) >= COVERAGE_KEYS
        assert out["answer_shape"] == "insufficient_evidence", "a partial result is never labelled answered"
        assert cov["state"] == "partial" and cov["reason"] == "unresolved_facets"
        assert cov["requested_facet_ids"] == ["f1", "f2"]
        assert cov["covered_facet_ids"] == ["f1"]
        assert cov["residual_gaps"] == [{"facet_id": "f2", "state": "unsupported", "reason": "no_supporting_evidence"}]
        assert cov["skip_generation"] is False
        assert cov["state"] == out["run_summary"]["returned_coverage_state"], "one value, two reports"

    @pytest.mark.asyncio
    async def test_the_object_carries_primary_source_state_corpus_counts_and_repair_attempts(self) -> None:
        run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2))
        cov = run.out["coverage"]
        assert cov["primary_source"] == {"state": "not_named", "sources": 0}, "no source was named in this run"
        counts = cov["corpus_counts"]
        assert counts["anchors"] == len(cov["anchors"]) > 0
        assert sum(counts["by_stream"].values()) == counts["anchors"]
        assert sum(counts["by_collection"].values()) == counts["anchors"]
        assert cov["repair"]["facet_ids"] == ["f2"], "the one repair targeted the facet nothing held bore on"
        assert cov["repair"]["outcome"] != "not_attempted" and cov["repair"]["queries"] >= 1, "repair attempts are reported"

    @pytest.mark.asyncio
    async def test_complete_has_every_facet_covered_and_no_residual_gap(self) -> None:
        run = await _drive(plan_id=7, dispatcher=Dispatcher(), request=_request(2))
        cov = run.out["coverage"]
        assert run.out["answer_shape"] == "answered"
        assert cov["state"] == "complete" and cov["reason"] == "all_covered"
        assert cov["requested_facet_ids"] == cov["covered_facet_ids"] == ["f1", "f2"]
        assert cov["residual_gaps"] == []

    @pytest.mark.asyncio
    async def test_uncovered_is_unavailable_and_names_both_gaps(self) -> None:
        d = Dispatcher(check=supports_nothing)
        run = await _drive(plan_id=0, dispatcher=d, request=_request(2))
        cov = run.out["coverage"]
        assert run.out["answer_shape"] == "uncovered"
        assert cov["state"] == "unavailable" and cov["reason"] == "uncovered" and cov["skip_generation"] is True
        assert cov["covered_facet_ids"] == []
        assert _gaps(cov) == [("f1", "unsupported"), ("f2", "unsupported")]

    @pytest.mark.asyncio
    async def test_the_state_is_the_final_one_after_the_post_generation_decision(self) -> None:
        """The boundary said ``complete``; generation returned a payload, not prose. The call returned
        ``unavailable`` / ``no_usable_answer`` and the object says so, as the run summary does."""
        payload = json.dumps({"contents": ["a", "b"], "missing": []})
        pr = PlanResult(
            steps=[{"ids": ["c1"], "collections": ["rdr__1-1"]}, {"text": payload}],
            coverage=_ledger_result("complete", "all_covered", COVERED),
        )
        run = await _drive(plan_id=7, plan_result=pr, request=_request(1))
        cov = run.out["coverage"]
        assert (cov["state"], cov["reason"]) == ("unavailable", "no_usable_answer")
        assert run.out["run_summary"]["returned_coverage_state"] == "unavailable"
        assert cov["covered_facet_ids"] == ["f1"], "the ledger itself is unchanged: only the state moved"

    @pytest.mark.asyncio
    async def test_prose_with_no_covered_facet_downgrades_to_unavailable_uncovered(self) -> None:
        unassessed = FacetFinding("f3", "unassessed", (), "assessment_failed")
        pr = PlanResult(
            steps=[{"ids": ["c1"], "collections": ["rdr__1-1"], "chunk_text_hash": ["h1"], "chunk_collections": ["rdr__1-1"]},
                   {"text": "A fluent answer built on nothing."}],
            coverage=_ledger_result("partial", "unresolved_facets", UNSUPPORTED, unassessed),
        )
        run = await _drive(plan_id=0, plan_result=pr, request=_request(2))
        cov = run.out["coverage"]
        assert (cov["state"], cov["reason"]) == ("unavailable", "uncovered")
        assert _gaps(cov) == [("f2", "unsupported"), ("f3", "unassessed")], "an unassessed facet is a gap too"

    @pytest.mark.asyncio
    async def test_an_error_result_carries_the_key(self) -> None:
        """Generation raised, so ``plan_run`` returned no result and the runner has no assessment to report: the
        object is the explicit ``unassessed`` one, as the run summary's own ``none`` says the run never finished."""
        run = await _drive(plan_id=7, dispatcher=_FailingGenerate(), request=_request(2))
        assert run.out["answer_shape"] == "error"
        assert set(run.out["coverage"]) >= COVERAGE_KEYS
        assert (run.out["coverage"]["state"], run.out["coverage"]["reason"]) == ("unassessed", "terminal_reduction_not_reached")
        assert run.out["run_summary"]["returned_coverage_state"] == "none"


class TestUnassessedRun:
    @pytest.mark.asyncio
    async def test_the_switch_off_gives_unassessed_with_its_reason_and_the_same_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "0")
        run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(2))
        cov = run.out["coverage"]
        assert set(cov) >= COVERAGE_KEYS
        assert (cov["state"], cov["reason"]) == ("unassessed", "switch_off")
        assert cov["requested_facet_ids"] == [] and cov["covered_facet_ids"] == [] and cov["residual_gaps"] == []
        assert run.out["run_summary"]["returned_coverage_state"] == "unassessed"

    @pytest.mark.asyncio
    async def test_a_request_without_facets_is_unassessed_no_facets(self) -> None:
        run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(0))
        assert (run.out["coverage"]["state"], run.out["coverage"]["reason"]) == ("unassessed", "no_facets")

    @pytest.mark.asyncio
    async def test_the_single_query_fast_path_never_reached_a_terminal_reduction(self) -> None:
        tel = FakeTelemetry()
        db = _db(tel)
        db.telemetry._supports_nx_answer_run_complete = lambda: True
        with patch("nexus.plans.matcher.plan_match", return_value=[_make_single_step_query_match()]), \
             patch("nexus.plans.runner.plan_run", AsyncMock()), \
             patch("nexus.mcp.core.query", return_value="one result line"), \
             patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=_request(2))), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db
            out = await nx_answer(question=QUESTION, structured=True)
        cov = out["coverage"]
        assert set(cov) >= COVERAGE_KEYS
        assert (cov["state"], cov["reason"]) == ("unassessed", "terminal_reduction_not_reached")
        assert out["run_summary"]["returned_coverage_state"] == "none", "the summary's own word for never reached"

    @pytest.mark.asyncio
    async def test_a_failure_before_any_plan_ran_still_carries_the_key(self) -> None:
        """Planner failure: no plan ever ran, so no boundary did; the error envelope still has the key."""
        tel = FakeTelemetry()
        db = _db(tel)
        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(side_effect=RuntimeError("planner down"))), \
             patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=_request(2))), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db
            out = await nx_answer(question=QUESTION, structured=True)
        assert out["answer_shape"] == "planner_error"
        assert (out["coverage"]["state"], out["coverage"]["reason"]) == ("unassessed", "terminal_reduction_not_reached")


class TestRedaction:
    @pytest.mark.asyncio
    async def test_trace_false_carries_no_facet_text_and_no_source_hint(self) -> None:
        req = _request(2, hints={"f1": ("RDR-084",)})
        for trace in (True, False):
            run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=req, trace=trace)
            blob = json.dumps(run.out["coverage"])
            assert "what the document says about topic" not in blob, "facet text is never in the object"
            assert ("RDR-084" in blob) is False, "a source hint is never in the object"
            assert run.out["coverage"]["requested_facet_ids"] == ["f1", "f2"], "ids stay"

    def test_trace_false_blanks_the_source_labels_and_details_not_the_states(self) -> None:
        result = CoverageResult(
            "partial", "unresolved_facets", False,
            CoverageLedger(
                anchors=(EvidenceAnchor("e1", "primary", "chunk", "h" * 64, "rdr__1-1", "1.1.1", "", "SRC-LABEL", "evidence text"),),
                findings=(FacetFinding("f1", "covered", ("e1",)), FacetFinding("f2", "unsupported", (), "source_missing")),
                sources=(SourceRow("RDR-084 caller wording", "partial", "SECRET-DETAIL"),),
                scope=ScopeRow("widened", "SCOPE-DETAIL"),
                assessment=AssessmentReport(outcome="ok", calls=1, facets_per_call=(2,)),
            ),
        )
        redacted = json.dumps(result.to_wire(redact=True))
        for secret in ("RDR-084 caller wording", "SECRET-DETAIL", "SCOPE-DETAIL", "evidence text"):
            assert secret not in redacted, secret
        full = result.to_wire(redact=False)
        assert full["primary_source"] == {"state": "partial", "sources": 1}
        assert result.to_wire(redact=True)["primary_source"] == full["primary_source"], "the state survives redaction"
        assert "RDR-084 caller wording" in json.dumps(full), "non-vacuity: the traced form keeps the label"


class TestPrimarySourceState:
    @staticmethod
    def _wire(*rows: SourceRow) -> dict[str, Any]:
        return CoverageResult("complete", "all_covered", False, CoverageLedger(sources=rows)).to_wire()["primary_source"]

    def test_none_named(self) -> None:
        assert self._wire() == {"state": "not_named", "sources": 0}

    def test_any_missing_source_makes_the_state_missing(self) -> None:
        assert self._wire(SourceRow("A", "covered"), SourceRow("B", "missing")) == {"state": "missing", "sources": 2}

    def test_partial_beats_covered(self) -> None:
        assert self._wire(SourceRow("A", "covered"), SourceRow("B", "partial")) == {"state": "partial", "sources": 2}

    def test_all_covered(self) -> None:
        assert self._wire(SourceRow("A", "covered")) == {"state": "covered", "sources": 1}


class TestContinuationCarriesTheSameObject:
    @staticmethod
    async def _headless(boundary: CoverageBoundary, check) -> dict[str, Any]:
        generated: list = []
        db_stub, patches = _handoff_patches(boundary, check, generated)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            t2_ctx = stack.enter_context(patch("nexus.mcp.core._t2_ctx"))
            t2_ctx.return_value.__enter__.return_value = db_stub
            return await nx_answer(question="q", structured=True)

    @staticmethod
    async def _handoff(boundary: CoverageBoundary, check, *, own_boundary: bool = True) -> dict[str, Any]:
        generated: list = []
        db_stub, patches = _handoff_patches(boundary, check, generated)
        if not own_boundary:
            patches = patches[:5] + patches[6:]  # everything but the plan_run wrapper: nx_answer's own boundary (or none)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            t2_ctx = stack.enter_context(patch("nexus.mcp.core._t2_ctx"))
            t2_ctx.return_value.__enter__.return_value = db_stub
            return await nx_answer(question="q", continuation=True, structured=True)

    @pytest.mark.asyncio
    async def test_the_hand_off_envelope_has_the_top_level_key_equal_to_continuation_coverage(self) -> None:
        handed = await self._handoff(CoverageBoundary(request=_request(3)), by_topic())
        assert handed["continuation"] is not None
        assert handed["coverage"] == handed["continuation"]["coverage"]
        assert handed["coverage"]["state"] == "complete"

    @pytest.mark.asyncio
    async def test_the_hand_off_coverage_is_the_headless_runs_coverage_for_the_same_run_shape(self) -> None:
        handed = await self._handoff(CoverageBoundary(request=_request(3)), by_topic())
        headless = await self._headless(CoverageBoundary(request=_request(3)), by_topic())
        assert headless["continuation"] is None
        stable = ("state", "reason", "skip_generation", "requested_facet_ids", "covered_facet_ids", "residual_gaps",
                  "primary_source", "corpus_counts", "findings", "facets", "rounds")
        assert {k: handed["coverage"][k] for k in stable} == {k: headless["coverage"][k] for k in stable}
        assert handed["coverage"]["state"] == handed["run_summary"]["returned_coverage_state"]

    @pytest.mark.asyncio
    async def test_a_hand_off_with_the_switch_off_is_unassessed_at_both_places(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "0")
        handed = await self._handoff(CoverageBoundary(request=None), by_topic(), own_boundary=False)
        assert handed["continuation"] is not None and handed["continuation"]["coverage"] is None
        assert (handed["coverage"]["state"], handed["coverage"]["reason"]) == ("unassessed", "switch_off")
