# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 step 3 (nexus-jypmj.3.3): the coverage boundary wired into ``nx_answer``, and the shapes,
counters, marker notice and no-grow rule the coverage state decides.

Tenant-free. Two harnesses, both through the real ``nx_answer``:

* ``_drive`` with a ``dispatcher`` runs the REAL ``plan_run`` (and so the real boundary, repair and ledger) over a
  stub dispatcher; the model call (``operator_check``) is stubbed at the dispatcher, the boundary the runner uses.
* ``_drive`` with a ``plan_result`` patches ``plan_run`` to return a fixed ``PlanResult`` (the harness of
  ``tests/test_rdr232_run_summary_wiring.py``), for the post-generation rules a stub ledger decides.

The five Test Plan scenarios of the bead are the tests named ``test_scenario_N_*``.
"""
from __future__ import annotations

import ast
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from nexus.commands.plan import _hygiene_scan
from nexus.commands.answer_runs import _row_is_insufficient_evidence
from nexus.mcp.core import nx_answer
from nexus.plans import runner, switches
from nexus.plans.coverage_ledger import (
    AssessmentReport,
    CoverageBoundary,
    CoverageLedger,
    CoverageResult,
    FacetFinding,
)
from nexus.plans.evidence import ScopeConstraint
from nexus.plans.matcher import _is_always_failing
from nexus.plans.request_coverage import RequestCoverage
from nexus.plans.runner import PlanResult
from tests.test_nx_answer import _make_match, _make_single_step_query_match
from tests.test_rdr232_coverage_ledger import PLAN, Dispatcher, _request, supports_nothing
from tests.test_rdr232_run_summary_wiring import FakeTelemetry, _db

REPO_ROOT = pathlib.Path(__file__).parent.parent
CORE_PY = REPO_ROOT / "src" / "nexus" / "mcp" / "core.py"
QUESTION = "what did RDR-084 decide, and who approved it?"


@pytest.fixture(autouse=True)
def _coverage_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary ships OFF (RDR-232 Phase 3 fix round: the default flips only after the nexus-jypmj.3.10
    measurement), so every test here that drives the boundary opts in the way a caller does. The tests of the
    default itself delete the variable."""
    monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "1")


def supports_f1_only(items: str, instruction: str) -> dict:  # noqa: ARG001
    """A check reply that says the first item supports facet f1 and nothing else."""
    first = json.loads(items)[0]["id"]
    return {"ok": True, "evidence": [{"item_id": first, "quote": "[f1] a short quote", "role": "supports"}]}


def _match(plan_id: int):
    return _make_match(plan_id=plan_id, confidence=0.9 if plan_id else None, plan_json=json.dumps(PLAN))


def _ledger_result(state: str, reason: str, *findings: FacetFinding, skip: bool = False) -> CoverageResult:
    return CoverageResult(
        state=state, reason=reason, skip_generation=skip,  # type: ignore[arg-type]
        ledger=CoverageLedger(findings=findings, assessment=AssessmentReport(outcome="ok", calls=1, facets_per_call=(len(findings),))),
    )


COVERED = FacetFinding("f1", "covered", ("e1",))
UNSUPPORTED = FacetFinding("f2", "unsupported", (), "no_supporting_evidence")


class Run:
    """What one driven ``nx_answer`` call produced: its return, the telemetry double, the db mock, the plan_run kwargs."""

    def __init__(self, out: Any, tel: FakeTelemetry, db: MagicMock, plan_run_calls: list[dict[str, Any]],
                 dispatcher: Dispatcher | None) -> None:
        self.out, self.tel, self.db, self.plan_run_calls, self.dispatcher = out, tel, db, plan_run_calls, dispatcher

    @property
    def write(self) -> dict[str, Any]:
        [(_, kw)] = self.tel.writes()
        return kw

    @property
    def summary(self) -> dict[str, Any]:
        return self.write["run_summary"]

    @property
    def grew(self) -> bool:
        return self.db.plans.save_plan.called


async def _drive(
    *, plan_id: int = 7, dispatcher: Dispatcher | None = None, plan_result: PlanResult | None = None,
    request: RequestCoverage | None = None, structured: bool = True, trace: bool = True,
    match: Any = None, tel: FakeTelemetry | None = None, **call_kw: Any,
) -> Run:
    tel = tel or FakeTelemetry()
    db = _db(tel)
    db.plans.save_plan = MagicMock(return_value=991)
    db.telemetry._supports_nx_answer_run_complete = lambda: True
    calls: list[dict[str, Any]] = []
    real = runner.plan_run

    if dispatcher is not None:
        async def fake_plan_run(m: Any, b: Any, **kw: Any) -> PlanResult:
            calls.append(dict(kw))
            return await real(m, b, dispatcher=dispatcher, bundle_operators=False, **kw)
    else:
        async def fake_plan_run(m: Any, b: Any, **kw: Any) -> PlanResult:  # noqa: ARG001
            calls.append(dict(kw))
            return plan_result  # type: ignore[return-value]

    match = match if match is not None else _match(plan_id)
    patches = [
        patch("nexus.plans.matcher.plan_match", return_value=[match] if match.plan_id else []),
        patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)),
        patch("nexus.plans.runner.plan_run", fake_plan_run),
        patch("nexus.mcp.core._t2_ctx"),
        patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)),
        patch("nexus.mcp.core.scratch", return_value="ok"),
        patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None),
    ]
    if request is not None:
        patches.append(patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=request)))
    mocks = [p.start() for p in patches]
    try:
        mocks[3].return_value.__enter__.return_value = db
        out = await nx_answer(question=QUESTION, structured=structured, trace=trace, **call_kw)
    finally:
        for p in reversed(patches):
            p.stop()
    return Run(out, tel, db, calls, dispatcher)


def _partial_result(prose: str = "The decision was X.") -> PlanResult:
    return PlanResult(
        steps=[{"ids": ["c1"], "tumblers": ["1.1.1"], "collections": ["rdr__1-1"], "chunk_text_hash": ["h1"],
                "chunk_collections": ["rdr__1-1"], "distances": [0.1]}, {"text": prose}],
        coverage=_ledger_result("partial", "unresolved_facets", COVERED, UNSUPPORTED),
    )


# ── the terminal flag: a run cut before its terminal reduction never reached it ───────────────────────────────


class TestTerminalReductionReached:
    @staticmethod
    def _cut(coverage: CoverageResult | None) -> PlanResult:
        return PlanResult(
            steps=[{"ids": ["c1"], "tumblers": ["1.1.1"], "collections": ["rdr__1-1"], "chunk_text_hash": ["h1"],
                    "chunk_collections": ["rdr__1-1"], "distances": [0.1]}],
            budget_exhausted_at_step=2, budget_exhausted_kind="time", total_planned_steps=2, coverage=coverage,
        )

    @pytest.mark.asyncio
    async def test_a_budget_exhausted_exit_records_coverage_none_and_the_envelope_says_why(self) -> None:
        """Code S3: the deadline expired in the retrieval segment, so the terminal reduction was never reached. The
        run row's summary counts none of the four states (coverage_state "none") and the envelope reason is
        terminal_reduction_not_reached, not "unassessed / boundary_not_run"."""
        run = await _drive(plan_id=7, plan_result=self._cut(None), request=_request(2))
        assert run.out["budget_exhausted_at_step"] == 2
        assert run.summary["coverage_state"] == "none"
        assert [run.summary[f"coverage_{s}"] for s in ("complete", "partial", "unavailable", "unassessed")] == [0, 0, 0, 0]
        assert run.out["run_summary"]["returned_coverage_state"] == "none"
        assert (run.out["coverage"]["state"], run.out["coverage"]["reason"]) == ("unassessed", "terminal_reduction_not_reached")

    @pytest.mark.asyncio
    async def test_a_cut_after_the_boundary_ran_keeps_the_ledger_it_produced(self) -> None:
        """The boundary ran, then generation hit the deadline: the terminal reduction was reached, so the ledger stands."""
        cov = _ledger_result("partial", "unresolved_facets", COVERED, UNSUPPORTED)
        run = await _drive(plan_id=7, plan_result=self._cut(cov), request=_request(2))
        assert run.summary["coverage_state"] == "partial" and run.summary["coverage_partial"] == 1
        assert run.out["coverage"]["state"] == "partial"

    @pytest.mark.asyncio
    async def test_a_run_that_finished_without_a_boundary_result_is_still_unassessed_not_none(self) -> None:
        run = await _drive(plan_id=7, plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(2))
        assert run.summary["coverage_state"] == "unassessed" and run.summary["coverage_unassessed"] == 1


# ── A: the boundary is wired ────────────────────────────────────────────────────────────────────────────────


class TestBoundaryWiring:
    @pytest.mark.asyncio
    async def test_nx_answer_passes_a_boundary_built_from_inputs_it_already_holds(self) -> None:
        req = _request(2)
        run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=req, scope="rdr__1-1")
        [kw] = run.plan_run_calls
        boundary = kw["coverage_boundary"]
        assert isinstance(boundary, CoverageBoundary)
        assert boundary.request is req, "the Phase 2 RequestCoverage"
        assert boundary.scope == ScopeConstraint.parse("rdr__1-1"), "the caller's scope"
        assert boundary.named_sources is None and boundary.primary is None and boundary.lifecycle is None
        assert boundary.owner_resolution is not None and boundary.owner_resolution["status"] == "none", "the owner resolution"
        assert boundary.dropped_scope == ""

    @pytest.mark.asyncio
    async def test_a_dropped_comma_list_scope_reaches_the_boundary(self) -> None:
        run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(1), scope="rdr__a,rdr__b")
        [kw] = run.plan_run_calls
        assert kw["coverage_boundary"].dropped_scope == "rdr__a,rdr__b"
        assert kw["coverage_boundary"].scope == ScopeConstraint.parse(run.out["scope_normalized"]["applied"])

    @pytest.mark.asyncio
    async def test_the_kill_switch_passes_no_boundary_and_every_run_is_unassessed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "0")
        run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(2))
        [kw] = run.plan_run_calls
        assert "coverage_boundary" not in kw, "a disabled switch leaves the plan_run call exactly as before Phase 3"
        assert run.out["run_summary"]["returned_coverage_state"] == "unassessed"
        assert run.out["answer_shape"] == "answered"

    @pytest.mark.asyncio
    async def test_the_switch_is_off_by_default_and_every_run_is_unassessed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(switches.ANSWER_COVERAGE_ENV, raising=False)
        run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(2))
        [kw] = run.plan_run_calls
        assert "coverage_boundary" not in kw, "an unset switch leaves the plan_run call exactly as before Phase 3"
        assert run.out["run_summary"]["returned_coverage_state"] == "unassessed"
        assert run.out["coverage"]["state"] == "unassessed" and run.out["coverage"]["reason"] == "switch_off"
        assert run.out["answer_shape"] == "answered"

    @pytest.mark.asyncio
    async def test_an_unset_switch_runs_no_assessment_no_repair_and_one_generation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """nx_answer with the switch unset behaves as it did before Phase 3: the real runner is driven over the stub
        dispatcher and no operator_check or repair call is made, even for a request nothing covers."""
        monkeypatch.delenv(switches.ANSWER_COVERAGE_ENV, raising=False)
        d = Dispatcher(check=supports_nothing)
        run = await _drive(plan_id=7, dispatcher=d, request=_request(2))
        assert "operator_check" not in d.tools and d.tools.count("generate") == 1
        assert d.tools.count("search") == 1, "no repair retrieval"
        assert run.out["answer_shape"] == "answered" and run.write["success"] is True
        assert run.summary["coverage_state"] == "unassessed"

    @pytest.mark.asyncio
    async def test_the_opt_in_value_hands_the_runner_the_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "1")
        run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(2))
        assert isinstance(run.plan_run_calls[0]["coverage_boundary"], CoverageBoundary)

    @pytest.mark.asyncio
    async def test_an_unrecognised_switch_value_warns_once_and_stays_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A value in neither vocabulary is read as the default, which is now off: a misspelled opt-in or opt-out
        never switches the destructive gate on. It warns once so the typo is visible."""
        monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "disable")
        with structlog.testing.capture_logs() as logs:
            first = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(1))
            second = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(1))
        assert "coverage_boundary" not in first.plan_run_calls[0] and "coverage_boundary" not in second.plan_run_calls[0]
        warned = [e for e in logs if e["event"] == "answer_switch_value_unrecognised" and e["variable"] == "NX_ANSWER_COVERAGE"]
        assert len(warned) == 1 and warned[0]["read_as"] == "off"

    @pytest.mark.asyncio
    async def test_the_continuation_fallback_run_gets_the_boundary_too(self) -> None:
        """Both plan_run calls of a continuation call (the prefix-only run and the fallback re-run) are handed the
        boundary; only the first is cut."""
        multi = _make_match(plan_id=7, confidence=0.9, plan_json=json.dumps({"steps": [
            {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
            {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "x"}},
        ]}))
        prefix = PlanResult(steps=[{"ids": ["a"], "tumblers": ["1.1"], "collections": ["knowledge"],
                                    "chunk_text_hash": ["h"], "chunk_collections": ["knowledge"], "distances": [0.1]}])
        full = PlanResult(steps=[prefix.steps[0], {"text": "the final answer"}])
        results = iter([prefix, full])
        calls: list[dict[str, Any]] = []

        async def fake_plan_run(m: Any, b: Any, **kw: Any) -> PlanResult:  # noqa: ARG001
            calls.append(dict(kw))
            return next(results)

        tel = FakeTelemetry()
        db = _db(tel)
        db.plans.save_plan = MagicMock(return_value=1)
        db.telemetry._supports_nx_answer_run_complete = lambda: True
        with patch("nexus.plans.matcher.plan_match", return_value=[multi]), \
             patch("nexus.plans.runner.plan_run", fake_plan_run), \
             patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=_request(1))), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch("nexus.plans.continuation_envelope.assemble_continuation_envelope", return_value=None), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db
            out = await nx_answer(question=QUESTION, continuation=True, structured=True)
        assert len(calls) == 2, "the prefix-only run, then the fallback re-run"
        assert "continuation_cut_at_step" in calls[0] and "continuation_cut_at_step" not in calls[1]
        assert isinstance(calls[0]["coverage_boundary"], CoverageBoundary)
        assert calls[1]["coverage_boundary"] is calls[0]["coverage_boundary"]
        assert out["final_text"].endswith("the final answer")

    def test_census_every_plan_run_call_in_nx_answer_is_fed_the_boundary_kwargs(self) -> None:
        """The two call sites (the run and the continuation fallback) both spread a kwargs dict that
        ``coverage_boundary`` is put into, so a third call added later without the dict is caught here."""
        tree = ast.parse(CORE_PY.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "nx_answer")
        sites = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_plan_run_recorded"]
        assert len(sites) == 2, "the run and the continuation fallback are the only plan_run call sites"
        spreads = {ast.unparse(k.value) for c in sites for k in c.keywords if k.arg is None}
        assert spreads == {"_plan_run_kwargs", "_continuation_fallback_kwargs"}
        src = ast.unparse(fn)
        assert "_plan_run_kwargs['coverage_boundary']" in src
        assert "_continuation_fallback_kwargs = dict(_plan_run_kwargs)" in src

    @pytest.mark.asyncio
    async def test_the_single_query_fast_path_has_no_reduction_so_no_boundary_and_state_none(self) -> None:
        tel = FakeTelemetry()
        db = _db(tel)
        db.telemetry._supports_nx_answer_run_complete = lambda: True
        plan_run = AsyncMock()
        with patch("nexus.plans.matcher.plan_match", return_value=[_make_single_step_query_match()]), \
             patch("nexus.plans.runner.plan_run", plan_run), \
             patch("nexus.mcp.core.query", return_value="one result line"), \
             patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=_request(2))), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db
            out = await nx_answer(question=QUESTION, structured=True)
        plan_run.assert_not_called()
        [(_, kw)] = tel.writes()
        assert kw["run_summary"]["coverage_state"] == "none", "it never reached a terminal reduction"
        assert all(kw["run_summary"][f"coverage_{s}"] == 0 for s in ("complete", "partial", "unavailable", "unassessed"))
        assert out["run_summary"]["returned_coverage_state"] == "none"


# ── scenario 1: execution succeeds but required evidence is missing ──────────────────────────────────────────


class TestScenario1Partial:
    @pytest.mark.asyncio
    async def test_scenario_1_partial_is_insufficient_evidence_with_the_marker_then_the_prose(self) -> None:
        d = Dispatcher(check=supports_f1_only, answer="The decision was X.")
        run = await _drive(plan_id=7, dispatcher=d, request=_request(2))
        out = run.out
        assert out["answer_shape"] == "insufficient_evidence"
        assert out["final_text"].startswith("[insufficient-evidence: 1 unresolved]\n")
        assert "what the document says about topic 2" in out["final_text"].split("\n\n")[0], "the unresolved facet is named"
        assert "\n\nThe decision was X." in out["final_text"], "the prose follows the notice"
        assert d.tools.count("generate") == 1, "a partial answer is still generated, once"
        assert run.summary["coverage_state"] == "partial" and run.summary["coverage_partial"] == 1
        assert run.summary["outcome"] == "success"
        assert run.summary["coverage"]["facets"] == {"covered": 1, "unsupported": 1, "contradicted": 0, "unassessed": 0}
        assert out["run_summary"]["returned_coverage_state"] == "partial"
        assert out["run_summary"]["persisted_coverage_state"] == "partial"

    @pytest.mark.asyncio
    async def test_scenario_1_a_library_plan_counts_a_success(self) -> None:
        run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2))
        assert run.write["success"] is True, "partial counts as success_count (RDR section 4)"
        assert run.write["plan_id"] == 7

    @pytest.mark.asyncio
    async def test_scenario_1_an_inline_planned_run_does_not_grow(self) -> None:
        d = Dispatcher(check=supports_f1_only)
        run = await _drive(plan_id=0, dispatcher=d, request=_request(2))
        assert run.out["answer_shape"] == "insufficient_evidence"
        assert not run.grew, "growth is blocked by the no-grow flag, not by a non-answer shape"
        assert run.write["success"] is True
        # the control: the same inline run with every facet covered does grow
        control = await _drive(plan_id=0, dispatcher=Dispatcher(), request=_request(2))
        assert control.out["answer_shape"] == "answered" and control.grew, "non-vacuity: complete still grows as today"

    @pytest.mark.asyncio
    async def test_scenario_1_a_text_call_opens_with_the_marker(self) -> None:
        run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2), structured=False)
        assert isinstance(run.out, str) and run.out.startswith("[insufficient-evidence: 1 unresolved]")

    @pytest.mark.asyncio
    async def test_the_recorded_row_carries_the_marker_and_trace_false_redacts_the_text_not_the_summary(self) -> None:
        traced = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2), trace=True)
        assert traced.write["final_text"].startswith("[insufficient-evidence: 1 unresolved]")
        redacted = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2), trace=False)
        assert redacted.write["final_text"] == "[redacted]"
        assert redacted.summary["coverage_partial"] == 1 and "what the document says" not in json.dumps(redacted.summary)


class TestScenario1RowLandsInTheBucket:
    """The row ``nx_answer`` itself wrote, read back by ``nx answer-runs``' rule (S1 of the gate critique)."""

    @staticmethod
    def _row(run: Run) -> dict[str, Any]:
        kw = run.write
        row = {"question": kw["question"], "final_text": kw["final_text"], "step_count": kw["step_count"], "plan_id": kw["plan_id"]}
        if kw.get("run_summary") is not None:
            row["run_summary"] = kw["run_summary"]
        return row

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trace", [True, False])
    async def test_scenario_1_a_row_with_a_summary_is_in_the_bucket_for_both_trace_values(self, trace: bool) -> None:
        run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2), trace=trace)
        row = self._row(run)
        assert "run_summary" in row
        assert _row_is_insufficient_evidence(row) is True

    @pytest.mark.asyncio
    async def test_a_row_written_to_an_engine_without_the_summary_is_bucketed_by_the_marker_only_when_traced(self) -> None:
        traced = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2),
                              tel=FakeTelemetry(supported=False), trace=True)
        redacted = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2),
                                tel=FakeTelemetry(supported=False), trace=False)
        assert "run_summary" not in self._row(traced) and _row_is_insufficient_evidence(self._row(traced)) is True
        assert "run_summary" not in self._row(redacted) and _row_is_insufficient_evidence(self._row(redacted)) is False


# ── scenario 5: the uncovered shape ──────────────────────────────────────────────────────────────────────────


class TestScenario5Uncovered:
    @pytest.mark.asyncio
    async def test_scenario_5_uncovered_records_failure_never_grows_and_carries_the_standard_notice(self) -> None:
        d = Dispatcher(check=supports_nothing)
        run = await _drive(plan_id=0, dispatcher=d, request=_request(2))
        out = run.out
        assert "generate" not in d.tools, "the boundary skipped terminal generation"
        assert out["answer_shape"] == "uncovered"
        assert out["final_text"].startswith("[non-answer: uncovered]")
        assert "chash:" in out["final_text"], "the standard notice lists the retrieved chunk references"
        assert out["chunks"], "and the envelope carries them"
        assert run.write["success"] is False
        assert not run.grew
        assert run.summary["coverage_state"] == "unavailable" and run.summary["coverage_unavailable"] == 1
        assert run.summary["coverage"]["reason"] == "uncovered" and run.summary["outcome"] == "failure"

    @pytest.mark.asyncio
    async def test_scenario_5_a_library_plan_counts_a_failure(self) -> None:
        run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_nothing), request=_request(2))
        assert run.out["answer_shape"] == "uncovered" and run.write["success"] is False and run.write["plan_id"] == 7

    @pytest.mark.asyncio
    async def test_prose_with_no_covered_facet_and_none_unassessed_is_withheld_and_uncovered(self) -> None:
        """Generation ran (the skip was off) and every facet is known uncovered: the prose is withheld."""
        contradicted = FacetFinding("f3", "contradicted", ("e1",), "contradicted")
        pr = PlanResult(
            steps=[{"ids": ["c1"], "collections": ["rdr__1-1"], "chunk_text_hash": ["h1"], "chunk_collections": ["rdr__1-1"]},
                   {"text": "A fluent answer built on nothing."}],
            coverage=_ledger_result("partial", "unresolved_facets", UNSUPPORTED, contradicted),
        )
        run = await _drive(plan_id=0, plan_result=pr, request=_request(2))
        assert run.out["answer_shape"] == "uncovered" and "fluent answer" not in run.out["final_text"]
        assert run.write["success"] is False and not run.grew
        assert run.summary["coverage_state"] == "unavailable", "the summary carries the FINAL state"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("known_gap", [UNSUPPORTED, FacetFinding("f3", "contradicted", ("e1",), "contradicted")])
    async def test_prose_with_no_covered_facet_but_an_unassessed_one_is_kept_as_a_partial_answer(self, known_gap: FacetFinding) -> None:
        """An unassessed facet is not known to be uncovered (code M6, critique S2): the prose generation paid for is
        returned behind the partial notice, which names the unassessed facet as not assessed."""
        unassessed = FacetFinding("f4", "unassessed", (), "assessment_failed")
        pr = PlanResult(
            steps=[{"ids": ["c1"], "collections": ["rdr__1-1"], "chunk_text_hash": ["h1"], "chunk_collections": ["rdr__1-1"]},
                   {"text": "A fluent answer built on part of the evidence."}],
            coverage=_ledger_result("partial", "unresolved_facets", known_gap, unassessed),
        )
        run = await _drive(plan_id=0, plan_result=pr, request=_request(2))
        assert run.out["answer_shape"] == "insufficient_evidence"
        assert "fluent answer" in run.out["final_text"] and "(not assessed)" in run.out["final_text"]
        assert run.write["success"] is True and not run.grew
        assert run.summary["coverage_state"] == "partial", "not rewritten to unavailable"


# ── C: the rest of the post-generation half ──────────────────────────────────────────────────────────────────


class TestPostGeneration:
    @pytest.mark.asyncio
    async def test_a_payload_in_place_of_prose_is_unavailable_whatever_the_ledger_said(self) -> None:
        payload = json.dumps({"contents": ["a", "b"], "missing": []})
        pr = PlanResult(steps=[{"ids": ["c1"], "collections": ["rdr__1-1"]}, {"text": payload}],
                        coverage=_ledger_result("complete", "all_covered", COVERED))
        run = await _drive(plan_id=7, plan_result=pr, request=_request(1))
        assert run.out["answer_shape"] == "hydration_dump" and run.write["success"] is False
        assert run.summary["coverage_state"] == "unavailable" and run.summary["coverage"]["reason"] == "no_usable_answer"

    @pytest.mark.asyncio
    async def test_unavailable_without_evidence_is_no_evidence_a_failure_and_never_grows(self) -> None:
        pr = PlanResult(
            steps=[{"ids": ["c1"], "collections": ["rdr__1-1"]}, {"text": "[no usable evidence]"}],
            coverage=_ledger_result("unavailable", "no_evidence", UNSUPPORTED),
        )
        run = await _drive(plan_id=0, plan_result=pr, request=_request(1))
        assert run.out["answer_shape"] == "no_evidence" and run.out["final_text"].startswith("[non-answer: no_evidence]")
        assert run.write["success"] is False and not run.grew
        assert run.summary["coverage_state"] == "unavailable"

    @pytest.mark.asyncio
    async def test_unassessed_keeps_todays_behaviour(self) -> None:
        pr = PlanResult(steps=[{"ids": ["c1"], "collections": ["rdr__1-1"]}, {"text": "The answer."}],
                        coverage=_ledger_result("unassessed", "assessment_failed", FacetFinding("f1", "unassessed", (), "assessment_failed")))
        run = await _drive(plan_id=0, plan_result=pr, request=_request(1))
        assert run.out["answer_shape"] == "answered" and run.out["final_text"] == "The answer."
        assert run.write["success"] is True and run.grew, "answered, success, and growth as today"
        assert run.summary["coverage_state"] == "unassessed"

    @pytest.mark.asyncio
    async def test_complete_is_answered_as_today(self) -> None:
        pr = PlanResult(steps=[{"ids": ["c1"], "collections": ["rdr__1-1"]}, {"text": "The answer."}],
                        coverage=_ledger_result("complete", "all_covered", COVERED))
        run = await _drive(plan_id=0, plan_result=pr, request=_request(1))
        assert run.out["answer_shape"] == "answered" and run.out["final_text"] == "The answer."
        assert run.write["success"] is True and run.grew

    @pytest.mark.asyncio
    async def test_a_generation_failure_keeps_the_existing_error_shape(self) -> None:
        run = await _drive(plan_id=7, dispatcher=_FailingGenerate(), request=_request(2))
        assert run.out["answer_shape"] == "error" and run.out["final_text"].startswith("[non-answer: error]")
        assert run.write["success"] is False


class _FailingGenerate(Dispatcher):
    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "generate":
            self.calls.append((tool, dict(args)))
            raise RuntimeError("generation backend down")
        return await super().__call__(tool, args)


# ── scenario 2 and E: partial runs keep a plan matchable; unavailable runs retire it ───────────────────────


class TestCountersAndPlanHygiene:
    @staticmethod
    def _row(successes: int, failures: int) -> dict[str, Any]:
        return {
            "id": 7, "query": "q", "verb": "research", "tags": "", "project": "",
            "plan_json": json.dumps({"steps": [{"tool": "search"}]}),
            "success_count": successes, "failure_count": failures,
        }

    @staticmethod
    def _library(row: dict[str, Any]) -> Any:
        class _Lib:
            disabled: list = []

            def list_plans(self, limit=20, project="", *, include_disabled=False):  # noqa: ARG002
                return [row]

            def set_plan_disabled(self, plan_id, *, reason=""):
                self.disabled.append((plan_id, reason))
                return True

        return _Lib()

    @pytest.mark.asyncio
    async def test_scenario_2_a_plan_whose_runs_are_all_partial_is_still_matchable_and_not_hygiene_flagged(self) -> None:
        successes = failures = 0
        for _ in range(3):
            run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_f1_only), request=_request(2))
            assert run.out["answer_shape"] == "insufficient_evidence"
            successes += int(run.write["success"] is True)
            failures += int(run.write["success"] is False)
        assert (successes, failures) == (3, 0)
        row = self._row(successes, failures)
        assert _is_always_failing(row) is False, "the matcher's always-failing skip does not see partial runs"
        assert _hygiene_scan(self._library(row)) == [], "and `nx plan hygiene` does not retire it either"

    @pytest.mark.asyncio
    async def test_scenario_2_a_plan_with_three_unavailable_runs_and_no_success_is_skipped_and_flagged(self) -> None:
        successes = failures = 0
        for _ in range(3):
            run = await _drive(plan_id=7, dispatcher=Dispatcher(check=supports_nothing), request=_request(2))
            assert run.out["answer_shape"] == "uncovered"
            successes += int(run.write["success"] is True)
            failures += int(run.write["success"] is False)
        assert (successes, failures) == (0, 3)
        row = self._row(successes, failures)
        assert _is_always_failing(row) is True
        findings = _hygiene_scan(self._library(row))
        assert [f["id"] for f in findings] == [7] and "always-failing" in findings[0]["reason"]


# ── B: the kill switch's vocabulary ──────────────────────────────────────────────────────────────────────────

OFF_VALUES = ["0", "false", "off", "no", "OFF", " False ", "No"]
ON_VALUES = ["1", "true", "yes", "on", "ON", " Yes "]
DEFAULT_VALUES = ["", "   ", "anything-else", "disable", "enabled"]


@pytest.mark.parametrize("value", ON_VALUES)
def test_each_opt_in_value_turns_the_boundary_on(value: str) -> None:
    assert switches.coverage_enabled({switches.ANSWER_COVERAGE_ENV: value}) is True


@pytest.mark.parametrize("value", DEFAULT_VALUES)
def test_unset_blank_and_unrecognised_values_are_the_default_which_is_off(value: str) -> None:
    assert switches.ANSWER_COVERAGE_ENABLED is False
    assert switches.coverage_enabled({switches.ANSWER_COVERAGE_ENV: value}) is False


def test_the_switch_name_and_the_unset_default() -> None:
    assert switches.ANSWER_COVERAGE_ENV == "NX_ANSWER_COVERAGE"
    assert switches.coverage_enabled({}) is False


@pytest.mark.parametrize("value", OFF_VALUES)
def test_each_off_value_is_the_kill_switch(value: str) -> None:
    assert switches.coverage_enabled({switches.ANSWER_COVERAGE_ENV: value}) is False


def test_the_default_has_one_source_the_module_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switches, "ANSWER_COVERAGE_ENABLED", True)
    assert switches.coverage_enabled({}) is True
    assert switches.coverage_enabled({switches.ANSWER_COVERAGE_ENV: "0"}) is False


def test_an_unrecognised_value_reads_as_off_even_when_the_default_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mistyped value must never switch an assessor on: once the default flips to on (nexus-jypmj.3.10),
    NX_ANSWER_COVERAGE=disable still reads as off (fix-round confirmation critique S-A). Unset and blank keep the
    default."""
    monkeypatch.setattr(switches, "ANSWER_COVERAGE_ENABLED", True)
    switches.reset_warned()
    assert switches.coverage_enabled({switches.ANSWER_COVERAGE_ENV: "disable"}) is False
    assert switches.coverage_enabled({switches.ANSWER_COVERAGE_ENV: ""}) is True
