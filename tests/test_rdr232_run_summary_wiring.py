# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.3.5): the run summary threaded through ``nx_answer``'s telemetry writes.

The choke point ``_nx_answer_record_complete``, the two direct ``/record`` survivors and ``nx_answer`` itself
against a telemetry double that implements the store's real signatures and behaves like the two engine
generations. What each persistence state means and when it is set:

* ``persisted``: the row read back carries the same version and digest.
* ``dropped``: written, but the row came back without it (an engine rolled back under a cached flag), with
  another digest or version, or the write failed. The store's cached ``/version`` is refreshed.
* ``unsupported``: the engine does not advertise the flag; the field is not sent and nothing is written twice.
* ``unverified``: sent, readback not requested (a text call) or itself unreadable.
* ``not_recorded``: no write happened.

Whatever happens, a telemetry failure never fails the retrieval.
"""
from __future__ import annotations

import ast
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import structlog.testing

from nexus.mcp.core import _nx_answer_record_complete, _nx_answer_record_run, nx_answer, nx_answer_report
from nexus.plans.coverage_ledger import AssessmentReport, CoverageLedger, CoverageResult, FacetFinding
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.run_summary import RunSummaryInputs, RunSummaryRecorder, summary_digest
from nexus.plans.runner import PlanResult
from tests.test_nx_answer import _make_match

REPO_ROOT = pathlib.Path(__file__).parent.parent
CORE_PY = REPO_ROOT / "src" / "nexus" / "mcp" / "core.py"

PRIVATE_QUESTION = "what did the SECRET-ACQUISITION plan decide?"
PRIVATE_FACET = "PRIVATE-FACET-TEXT acme terms"


def _request() -> RequestCoverage:
    return RequestCoverage(
        answer_kind="synthesis", kind_decision="heuristic",
        facets=(Facet(id="f1", text=PRIVATE_FACET), Facet(id="f2", text=PRIVATE_FACET + " 2")),
    )


def _recorder(*, trace: bool = True, readback: bool = True) -> RunSummaryRecorder:
    return RunSummaryRecorder(trace=trace, readback=readback, inputs=lambda: RunSummaryInputs(request=_request()))


def _coverage(state: str = "partial") -> CoverageResult:
    return CoverageResult(
        state=state, reason="unresolved_facets", skip_generation=False,
        ledger=CoverageLedger(
            findings=(FacetFinding("f1", "covered", ("e1",)), FacetFinding("f2", "unsupported", (), "no_supporting_evidence")),
            assessment=AssessmentReport(outcome="ok", calls=1, facets_per_call=(2,), latency_ms=400),
        ),
    )


class FakeTelemetry:
    """The store's real call signatures, an engine that keeps or drops the summary, and a call log.

    ``readback``: ``match`` returns what was written, ``missing`` the row with no summary (what an old engine
    returns), ``other`` a different summary, ``version`` the same summary at another version, ``norow`` no row
    at all, ``raises`` a transport failure.
    """

    def __init__(self, *, supported: bool = True, readback: str = "match", write_error: Exception | None = None,
                 complete_404: bool = False) -> None:
        self.supported = supported
        self.readback = readback
        self.write_error = write_error
        self.complete_404 = complete_404
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: dict[tuple[str, str], Any] = {}
        self.refreshed = 0
        self.downgraded = 0

    def _supports_nx_answer_run_summary(self) -> bool:
        return self.supported

    def _store(self, kind: str, kw: dict[str, Any]) -> bool:
        self.calls.append((kind, kw))
        sent = kw.get("run_summary") is not None and self.supported
        if self.write_error is not None:
            raise self.write_error
        stamp = kw.get("created_at") or "server-stamped"
        self.rows[(kw["question"], stamp)] = json.loads(json.dumps(kw["run_summary"], sort_keys=True)) if sent else None
        return sent

    def record_nx_answer_run(self, *, question, plan_id, matched_confidence, step_count, final_text, cost_usd,
                             duration_ms, steps=None, run_summary=None, created_at=None) -> bool:
        return self._store("record", dict(
            question=question, plan_id=plan_id, matched_confidence=matched_confidence, step_count=step_count,
            final_text=final_text, cost_usd=cost_usd, duration_ms=duration_ms, steps=steps,
            **({"run_summary": run_summary} if run_summary is not None else {}),
            **({"created_at": created_at} if created_at is not None else {}),
        ))

    def record_nx_answer_run_complete(self, *, question, plan_id, matched_confidence, step_count, final_text,
                                      cost_usd, duration_ms, created_at, steps, success, run_summary=None) -> bool:
        if self.complete_404:
            self.calls.append(("complete-404", {"created_at": created_at}))
            raise httpx.HTTPStatusError("gone", request=httpx.Request("POST", "http://x"), response=httpx.Response(404))
        return self._store("complete", dict(
            question=question, plan_id=plan_id, matched_confidence=matched_confidence, step_count=step_count,
            final_text=final_text, cost_usd=cost_usd, duration_ms=duration_ms, created_at=created_at, steps=steps,
            success=success, **({"run_summary": run_summary} if run_summary is not None else {}),
        ))

    def _downgrade_nx_answer_run_complete_support(self) -> None:
        self.downgraded += 1

    def _read_nx_answer_run_summary(self, *, question, created_at, final_text=None, duration_ms=None,
                                    step_count=None, limit=50):
        self.calls.append(("readback", {
            "question": question, "created_at": created_at, "final_text": final_text,
            "duration_ms": duration_ms, "step_count": step_count,
        }))
        if self.readback == "raises":
            raise httpx.ConnectError("engine unreachable")
        if self.readback == "norow":
            return False, []
        stored = self.rows.get((question, created_at))
        if self.readback == "missing":
            return True, [None]
        if self.readback == "other":
            return True, [{**(stored or {}), "outcome": "something-else"}]
        if self.readback == "version":
            return True, [{**(stored or {}), "version": 2}]
        if self.readback == "twins":  # a second run of the same question in the same second, then ours
            return True, [{"version": 1, "outcome": "twin"}, stored]
        return True, [stored]

    def _refresh_version_capabilities(self) -> bool:
        self.refreshed += 1
        return True

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if c[0] in ("record", "complete")]


@pytest.fixture(autouse=True)
def _rearm_the_unsupported_warning(monkeypatch):
    monkeypatch.setattr("nexus.plans.run_summary._unsupported_warned", False)


def _complete(db, rec, *, trace=True, question=PRIVATE_QUESTION, success=True, composite=True, plan_id=0):
    with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)):
        _nx_answer_record_complete(
            db, question=question, plan_id=plan_id, matched_confidence=0.5, step_count=2, final_text="the answer",
            step_records=[], duration_ms=1200, trace=trace, success=success,
            composite_supported_at_start=composite, early_bump_fired=not composite and bool(plan_id),
            run_summary=rec,
        )


def _db(tel: FakeTelemetry) -> MagicMock:
    db = MagicMock()
    db.telemetry = tel
    return db


class TestPersistenceStates:
    def test_composite_write_then_readback_is_persisted(self):
        tel, rec = FakeTelemetry(), _recorder()
        rec.observe_plan_result(PlanResult(coverage=_coverage("partial")))
        _complete(_db(tel), rec)
        [(kind, kw)] = tel.writes()
        assert kind == "complete" and kw["run_summary"]["coverage_partial"] == 1
        assert rec.persistence == "persisted" and rec.detail == "digest_match"
        wire = rec.to_wire()
        assert wire["returned_coverage_state"] == "partial" and wire["persisted_coverage_state"] == "partial"
        assert wire["digest"] == summary_digest(kw["run_summary"])

    def test_degradation_path_sends_the_stamp_with_the_summary_and_reads_it_back(self):
        tel, rec = FakeTelemetry(), _recorder()
        _complete(_db(tel), rec, composite=False)
        [(kind, kw)] = tel.writes()
        assert kind == "record" and "created_at" in kw and kw["run_summary"]["version"] == 1
        assert rec.persistence == "persisted"
        [readback] = [c for k, c in tel.calls if k == "readback"]
        assert (readback["question"], readback["created_at"]) == (PRIVATE_QUESTION, kw["created_at"])
        assert (readback["final_text"], readback["duration_ms"], readback["step_count"]) == ("the answer", 1200, 2), (
            "the run's own text, duration and step count narrow the candidates"
        )

    def test_rolled_back_engine_is_dropped_not_success(self):
        # Scenario 1: the cached flag said yes; the engine underneath no longer keeps the field.
        tel, rec = FakeTelemetry(readback="missing"), _recorder()
        with structlog.testing.capture_logs() as logs:
            _complete(_db(tel), rec)
        assert rec.persistence == "dropped" and rec.detail == "summary_missing"
        assert tel.refreshed == 1, "a dropped summary refreshes the cached /version"
        assert any(e["event"] == "nx_answer_run_summary_dropped" and e["log_level"] == "warning" for e in logs)
        assert rec.to_wire()["persisted_coverage_state"] is None

    @pytest.mark.parametrize("mode,detail", [("other", "digest_mismatch"), ("version", "version_mismatch"), ("norow", "row_not_found")])
    def test_other_readback_failures_are_dropped_with_their_reason(self, mode, detail):
        tel, rec = FakeTelemetry(readback=mode), _recorder()
        _complete(_db(tel), rec)
        assert (rec.persistence, rec.detail) == ("dropped", detail)

    def test_two_rows_in_one_second_persist_if_either_holds_our_summary(self):
        tel, rec = FakeTelemetry(readback="twins"), _recorder()
        _complete(_db(tel), rec)
        assert rec.persistence == "persisted"

    def test_unreadable_readback_is_unverified_never_persisted_and_never_raises(self):
        tel, rec = FakeTelemetry(readback="raises"), _recorder()
        _complete(_db(tel), rec)
        assert rec.persistence == "unverified" and rec.detail.startswith("readback_failed")
        assert tel.refreshed == 0, "an unreadable row proves nothing about the capability"

    def test_old_engine_omits_the_summary_logs_the_drop_and_writes_once(self):
        # Scenarios 2 and 3: new client, old engine.
        tel, rec = FakeTelemetry(supported=False), _recorder()
        with structlog.testing.capture_logs() as logs:
            _complete(_db(tel), rec)
        [(kind, kw)] = tel.writes()
        assert "run_summary" not in kw and kind == "complete"
        assert kw["final_text"] == "the answer" and kw["success"] is True, "the existing record is intact"
        assert rec.persistence == "unsupported"
        assert [c for c in tel.calls if c[0] == "readback"] == [], "nothing was sent, so nothing is read back"
        assert any(e["event"] == "nx_answer_run_summary_unsupported_by_engine" for e in logs)
        assert rec.to_wire()["digest"] is None

    def test_the_unsupported_warning_is_given_once_per_process_but_the_state_every_time(self):
        logs_seen = []
        for _ in range(3):
            tel, rec = FakeTelemetry(supported=False), _recorder()
            with structlog.testing.capture_logs() as logs:
                _complete(_db(tel), rec)
            logs_seen.append([e for e in logs if e["event"] == "nx_answer_run_summary_unsupported_by_engine"])
            assert rec.persistence == "unsupported"
        assert [len(x) for x in logs_seen] == [1, 0, 0]

    def test_old_engine_degradation_path_sends_neither_summary_nor_stamp(self):
        tel, rec = FakeTelemetry(supported=False), _recorder()
        _complete(_db(tel), rec, composite=False)
        [(kind, kw)] = tel.writes()
        assert kind == "record" and "run_summary" not in kw and "created_at" not in kw

    def test_a_text_call_does_not_read_back(self):
        tel, rec = FakeTelemetry(), _recorder(readback=False)
        _complete(_db(tel), rec)
        assert rec.persistence == "unverified" and rec.detail == "readback_not_requested"
        assert [c for c in tel.calls if c[0] == "readback"] == []

    def test_a_failed_write_is_dropped_and_does_not_raise(self):
        tel, rec = FakeTelemetry(write_error=httpx.ConnectError("down")), _recorder()
        _complete(_db(tel), rec)  # must not raise
        assert rec.persistence == "dropped" and rec.detail == "write_failed:ConnectError"

    def test_no_recorder_leaves_the_write_exactly_as_it_was(self):
        tel = FakeTelemetry()
        _complete(_db(tel), None)
        [(kind, kw)] = tel.writes()
        assert kind == "complete" and "run_summary" not in kw
        assert tel.calls == [("complete", kw)], "no probe, no readback"

    def test_a_summary_that_cannot_be_built_loses_the_summary_not_the_record(self):
        rec = RunSummaryRecorder(trace=True, readback=True, inputs=lambda: 1 / 0)
        tel = FakeTelemetry()
        _complete(_db(tel), rec)
        [(kind, kw)] = tel.writes()
        assert kind == "complete" and "run_summary" not in kw
        assert (rec.persistence, rec.detail) == ("not_recorded", "build_failed:ZeroDivisionError")

    def test_a_404_from_complete_falls_back_to_record_with_the_same_stamp_and_still_reads_back(self):
        tel, rec = FakeTelemetry(complete_404=True), _recorder()
        _complete(_db(tel), rec, plan_id=5)
        assert tel.downgraded == 1
        stamps = {kw["created_at"] for kind, kw in tel.calls if kind in ("complete-404", "record")}
        assert len(stamps) == 1, "one stamp across both attempts"
        [(kind, kw)] = tel.writes()
        assert kind == "record" and kw["run_summary"]["version"] == 1
        assert rec.persistence == "persisted"

    def test_the_run_summary_state_is_not_recorded_until_a_write_happens(self):
        assert _recorder().to_wire() == {
            "version": 1, "persistence": "not_recorded", "detail": "", "returned_coverage_state": "none",
            "persisted_coverage_state": None, "digest": None,
        }


class TestDirectRecordArms:
    """The planner-failure and continuation-handoff survivors write through ``/record``."""

    def test_record_run_threads_the_summary_and_reads_back(self):
        tel, rec = FakeTelemetry(), _recorder()
        _nx_answer_record_run(
            tel, question=PRIVATE_QUESTION, plan_id=None, matched_confidence=None, step_count=0,
            final_text="Planner error: boom", step_records=[], duration_ms=9, trace=True, run_summary=rec,
            summary_outcome="planner_failure",
        )
        [(kind, kw)] = tel.writes()
        assert kind == "record" and kw["run_summary"]["outcome"] == "planner_failure"
        assert kw["run_summary"]["coverage_state"] == "none", "a run that never reached reduction has no coverage state"
        assert rec.persistence == "persisted"

    def test_record_run_without_a_recorder_is_unchanged(self):
        tel = FakeTelemetry()
        _nx_answer_record_run(
            tel, question="q", plan_id=None, matched_confidence=None, step_count=0, final_text="x",
            step_records=[], duration_ms=1, trace=True,
        )
        [(_, kw)] = tel.writes()
        assert "run_summary" not in kw and "created_at" not in kw


class TestRedactionAtTheWire:
    """Scenario 5: with trace=False the telemetry carries ids and counts, not the redacted question."""

    def test_trace_false_payload_and_logs_hold_no_question_or_facet_text(self):
        tel, rec = FakeTelemetry(readback="missing"), _recorder(trace=False)
        with structlog.testing.capture_logs() as logs:
            _complete(_db(tel), rec, trace=False)
        [(_, kw)] = tel.writes()
        blob = json.dumps(kw, default=str) + json.dumps(logs, default=str)
        assert kw["question"] == "[redacted]" and kw["final_text"] == "[redacted]"
        for needle in (PRIVATE_QUESTION, PRIVATE_FACET, "SECRET-ACQUISITION"):
            assert needle not in blob, needle
        assert kw["run_summary"]["trace"] is False
        assert [f["id"] for f in kw["run_summary"]["request"]["facets"]] == ["f1", "f2"]

    def test_the_readback_of_a_redacted_run_is_matched_on_the_redacted_question(self):
        tel, rec = FakeTelemetry(), _recorder(trace=False)
        _complete(_db(tel), rec, trace=False)
        assert rec.persistence == "persisted"
        [readback] = [c for k, c in tel.calls if k == "readback"]
        assert (readback["question"], readback["created_at"]) == ("[redacted]", tel.writes()[0][1]["created_at"])

    def test_trace_true_is_the_non_vacuity_control(self):
        tel, rec = FakeTelemetry(), _recorder(trace=True)
        _complete(_db(tel), rec, trace=True)
        [(_, kw)] = tel.writes()
        assert PRIVATE_FACET in json.dumps(kw["run_summary"])


# ── nx_answer end to end ─────────────────────────────────────────────────────────────────────────────────


async def _drive(tel: FakeTelemetry, *, structured: bool, coverage: CoverageResult | None, trace: bool = True):
    db = _db(tel)
    plan_result = PlanResult(steps=[{"text": "The final answer."}], coverage=coverage)
    with patch("nexus.plans.matcher.plan_match", return_value=[_make_match(confidence=0.9)]), \
         patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_result)), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db
        db.telemetry._supports_nx_answer_run_complete = lambda: True
        return await nx_answer(question=PRIVATE_QUESTION, structured=structured, trace=trace)


class TestNxAnswerEnvelope:
    @pytest.mark.asyncio
    async def test_envelope_shows_returned_and_persisted_coverage_apart(self):
        tel = FakeTelemetry()
        out = await _drive(tel, structured=True, coverage=_coverage("partial"))
        rs = out["run_summary"]
        assert rs["persistence"] == "persisted"
        assert rs["returned_coverage_state"] == "partial" and rs["persisted_coverage_state"] == "partial"
        [(kind, kw)] = tel.writes()
        assert kind == "complete" and kw["run_summary"]["coverage_partial"] == 1
        assert kw["run_summary"]["request"]["answer_kind"] != "", "the live request coverage reaches the summary"

    @pytest.mark.asyncio
    async def test_a_run_with_no_boundary_reports_unassessed_not_none(self):
        out = await _drive(FakeTelemetry(), structured=True, coverage=None)
        assert out["run_summary"]["returned_coverage_state"] == "unassessed"

    @pytest.mark.asyncio
    async def test_old_engine_envelope_says_unsupported_and_the_answer_still_returns(self):
        # Scenario 3: new client, old engine; retrieval is unaffected.
        tel = FakeTelemetry(supported=False)
        out = await _drive(tel, structured=True, coverage=_coverage("complete"))
        assert out["run_summary"]["persistence"] == "unsupported"
        assert "final answer" in out["final_text"].lower()
        [(_, kw)] = tel.writes()
        assert "run_summary" not in kw

    @pytest.mark.asyncio
    async def test_rollback_after_a_cached_flag_is_dropped_in_the_envelope(self):
        out = await _drive(FakeTelemetry(readback="missing"), structured=True, coverage=_coverage("complete"))
        assert out["run_summary"]["persistence"] == "dropped"
        assert "final answer" in out["final_text"].lower()

    @pytest.mark.asyncio
    async def test_a_text_call_neither_reads_back_nor_changes_its_return(self):
        tel = FakeTelemetry()
        out = await _drive(tel, structured=False, coverage=_coverage("complete"))
        assert isinstance(out, str) and "final answer" in out.lower()
        assert [c for c in tel.calls if c[0] == "readback"] == []

    @pytest.mark.asyncio
    async def test_a_telemetry_outage_never_fails_the_retrieval(self):
        out = await _drive(FakeTelemetry(write_error=httpx.ConnectError("down")), structured=True, coverage=None)
        assert "final answer" in out["final_text"].lower()
        assert out["run_summary"]["persistence"] == "dropped"

    @pytest.mark.asyncio
    async def test_trace_false_run_carries_no_private_text_to_telemetry(self):
        tel = FakeTelemetry()
        await _drive(tel, structured=True, coverage=_coverage("partial"), trace=False)
        [(_, kw)] = tel.writes()
        blob = json.dumps(kw, default=str)
        assert PRIVATE_QUESTION not in blob and "SECRET-ACQUISITION" not in blob


# ── census ───────────────────────────────────────────────────────────────────────────────────────────────


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


class TestEverySiteThreadsTheRecorder:
    """Every telemetry write nx_answer makes passes the per-call recorder, so a new arm cannot silently go
    without a summary. Falsifier: delete ``run_summary=_run_summary`` from one call and this reds."""

    def setup_method(self) -> None:
        self.tree = ast.parse(CORE_PY.read_text())

    @pytest.mark.parametrize("fn", ["_nx_answer_record_complete", "_nx_answer_record_run"])
    def test_every_call_in_nx_answer_passes_run_summary(self, fn):
        calls = _calls_named(self.tree, fn)
        assert len(calls) >= 2, f"census found {len(calls)} {fn} calls; the census is vacuous"
        missing = []
        for c in calls:
            kw = {k.arg: k.value for k in c.keywords}
            if not (isinstance(kw.get("run_summary"), ast.Name) and kw["run_summary"].id == "_run_summary"):
                missing.append(c.lineno)
        assert not missing, f"{fn} calls without run_summary=_run_summary at lines {missing}"

    def test_the_envelope_carries_the_run_summary_key(self):
        src = CORE_PY.read_text()
        assert '"run_summary": _run_summary.to_wire()' in src

    def test_the_report_tool_threads_a_summary_too(self):
        fn = next(n for n in ast.walk(self.tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "nx_answer_report")
        assert _calls_named(fn, "_nx_answer_report_summary"), "nx_answer_report builds no summary"
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "record_nx_answer_run"]
        assert calls, "census found no record_nx_answer_run call in nx_answer_report"
        # The summary and its stamp ride as `**_report_extra`, so a double without the new keywords is untouched
        # when the engine cannot take a summary.
        assert all(any(k.arg is None and isinstance(k.value, ast.Name) and k.value.id == "_report_extra" for k in c.keywords) for c in calls)


class TestReportTool:
    """``nx_answer_report`` writes a ``/record`` row too; it carries a minimal summary under the same rule."""

    @pytest.mark.asyncio
    async def test_report_row_carries_a_text_free_summary_on_a_supporting_engine(self):
        tel = FakeTelemetry()
        with patch("nexus.mcp.core._t2_ctx") as t2_ctx:
            t2_ctx.return_value.__enter__.return_value = _db(tel)
            out = await nx_answer_report(continuation_id="cid-1", ok=True, final_text_excerpt="PRIVATE-EXCERPT")
        assert out["recorded"] is True
        [(kind, kw)] = tel.writes()
        assert kind == "record" and kw["created_at"]
        assert kw["run_summary"]["kind"] == "continuation_report" and kw["run_summary"]["outcome"] == "report_ok"
        assert "PRIVATE-EXCERPT" not in json.dumps(kw)
        assert kw["run_summary"]["request"] is None, "the report knows nothing about the run it reports"

    @pytest.mark.asyncio
    async def test_report_row_on_an_old_engine_is_today_s_row(self):
        tel = FakeTelemetry(supported=False)
        with patch("nexus.mcp.core._t2_ctx") as t2_ctx:
            t2_ctx.return_value.__enter__.return_value = _db(tel)
            out = await nx_answer_report(continuation_id="cid-2", ok=False)
        assert out["recorded"] is True
        [(_, kw)] = tel.writes()
        assert "run_summary" not in kw and "created_at" not in kw
