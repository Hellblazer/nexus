# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.3.5): the run summary against the REAL engine.

Boots the gate jar on an ephemeral Postgres (the fixtures of ``test_http_telemetry_store_integration``) and
drives the real ``HttpTelemetryStore`` and the real ``_nx_answer_record_complete`` / ``_nx_answer_record_run``
choke points. Where the engine of record is the new one (``nx_answer_run_summary_supported`` is true), the old
engine is modelled by the one thing that distinguishes it on the wire, and nothing else is faked:

* *capability off* (the A4 sub-leg): ``GET /version`` answers without the flag, so the store must not send
  the field. The engine underneath still keeps what it is sent, which is what makes the test sharp: a client
  that sent it anyway would be caught by the row.
* *rollback after a cached flag*: the cached flag says yes, and the field is stripped from the POST body the
  way an engine that predates the column drops an unknown key. The real engine then stores NULL and the
  readback has to say ``dropped``.

Integration-marked: it boots PostgreSQL and the jar (``scripts/build-gate-jar.sh`` first), so it is out of
the default loop and runs in CI's integration job. It adds no ``t2_tenant`` case.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus.mcp.core import _nx_answer_record_complete, _nx_answer_record_run
from nexus.plans.coverage_ledger import AssessmentCall, AssessmentReport, CoverageLedger, CoverageResult, FacetFinding
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.run_summary import (
    ENGINE_RUN_SUMMARY_LIMIT_BYTES,
    RunSummaryInputs,
    RunSummaryRecorder,
    canonical_json,
    summary_digest,
)
from nexus.plans.runner import PlanResult
from tests.db.test_http_telemetry_store_integration import (  # noqa: F401 — fixtures, re-exported to this module
    _ALL_PREREQS,
    java_service,
    pg_service,
    tel_store,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ALL_PREREQS, reason="skipped: missing jar or PG binaries (scripts/build-gate-jar.sh)"),
]

PRIVATE_QUESTION = "what did the SECRET-ACQUISITION plan decide about acme?"
PRIVATE_FACET = "PRIVATE-FACET-TEXT acme terms"


def _request() -> RequestCoverage:
    return RequestCoverage(
        answer_kind="synthesis", kind_decision="heuristic",
        facets=(Facet(id="f1", text=PRIVATE_FACET), Facet(id="f2", text=PRIVATE_FACET + " two")),
    )


def _coverage(state: str = "partial") -> CoverageResult:
    return CoverageResult(
        state=state, reason="unresolved_facets", skip_generation=False,
        ledger=CoverageLedger(
            findings=(FacetFinding("f1", "covered", ("e1",)), FacetFinding("f2", "unsupported", (), "no_supporting_evidence")),
            assessment=AssessmentReport(
                outcome="ok", calls=2, facets_per_call=(1, 1), latency_ms=900, accepted_records=2,
                call_log=(AssessmentCall("vector", 1, "support", 1, 0), AssessmentCall("graph", 1, "neutral_or_contradict", 1, 0)),
            ),
            rounds=2,
        ),
    )


def _recorder(*, trace: bool = True, readback: bool = True) -> RunSummaryRecorder:
    rec = RunSummaryRecorder(trace=trace, readback=readback, inputs=lambda: RunSummaryInputs(request=_request()))
    rec.observe_plan_result(PlanResult(coverage=_coverage("partial")))
    return rec


def _db(tel) -> MagicMock:
    db = MagicMock()
    db.telemetry = tel
    return db


def _complete(db, rec, *, question: str, trace: bool = True, composite: bool = True):
    with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)):
        _nx_answer_record_complete(
            db, question=question, plan_id=0, matched_confidence=0.5, step_count=2, final_text="the answer",
            step_records=[], duration_ms=1200, trace=trace, success=True,
            composite_supported_at_start=composite, early_bump_fired=False, run_summary=rec,
        )


def _rows(tel, question: str) -> list[dict[str, Any]]:
    return [r for r in tel.query_nx_answer_runs(limit=300)["rows"] if r["question"] == question]


class TestRealEngineAgrees:
    def test_the_engine_advertises_the_flag_the_client_reads(self, tel_store):
        assert tel_store._version_capabilities().get("nx_answer_run_summary_supported") is True
        assert tel_store._supports_nx_answer_run_summary() is True


class TestSummarySurvivesEveryPath:
    """Scenario 4: the summary survives /record, /complete, queries and import, with no second write."""

    SUMMARY = {
        "version": 1, "kind": "nx_answer", "outcome": "success", "coverage_partial": 1,
        "run": {"cost_usd": 0.0314, "duration_ms": 4200, "whole": 3.0, "ratio": 0.1}, "list": [3, 1, 2],
        "nested": {"z": None, "a": True},
    }

    def test_record_returns_the_summary_and_the_digest_matches_after_jsonb(self, tel_store):
        q, stamp = "it-record", "2026-10-27T15:00:01.250000+00:00"
        tel_store.record_nx_answer_run(
            question=q, plan_id=None, matched_confidence=None, step_count=1, final_text="t", cost_usd=None,
            duration_ms=5, run_summary=self.SUMMARY, created_at=stamp,
        )
        [row] = _rows(tel_store, q)
        assert row["run_summary"] is not None
        assert summary_digest(row["run_summary"]) == summary_digest(self.SUMMARY), "key order and number form do not move the digest"
        assert tel_store._read_nx_answer_run_summary(question=q, created_at=stamp) == (True, [row["run_summary"]])
        assert row["created_at"] == "2026-10-27T15:00:01Z", "the engine reports the second; readback matches at that resolution"

    def test_complete_returns_the_summary(self, tel_store):
        q, stamp = "it-complete", "2026-10-27T15:00:02.500000+00:00"
        tel_store.record_nx_answer_run_complete(
            question=q, plan_id=None, matched_confidence=None, step_count=1, final_text="t", cost_usd=0.5,
            duration_ms=5, created_at=stamp, steps=None, success=True, run_summary=self.SUMMARY,
        )
        [row] = _rows(tel_store, q)
        assert summary_digest(row["run_summary"]) == summary_digest(self.SUMMARY)

    def test_single_and_batch_import_carry_the_summary(self, tel_store):
        base = dict(plan_id=None, matched_confidence=None, step_count=0, final_text="t", cost_usd=None, duration_ms=1)
        tel_store.import_nx_answer_run(question="it-import-one", created_at="2026-10-27T15:00:03Z", run_summary=self.SUMMARY, **base)
        n = tel_store.import_rows_batch("nx_answer_runs", [
            {"question": "it-import-batch", "created_at": "2026-10-27T15:00:04Z", "run_summary": self.SUMMARY, **base},
            {"question": "it-import-batch-bare", "created_at": "2026-10-27T15:00:05Z", **base},
        ])
        assert n == 2
        [one] = _rows(tel_store, "it-import-one")
        [batch] = _rows(tel_store, "it-import-batch")
        [bare] = _rows(tel_store, "it-import-batch-bare")
        assert summary_digest(one["run_summary"]) == summary_digest(self.SUMMARY)
        assert summary_digest(batch["run_summary"]) == summary_digest(self.SUMMARY)
        assert bare["run_summary"] is None, "a row imported without a summary reads back null"

    def test_an_old_client_record_reads_back_null_and_unharmed(self, tel_store):
        q = "it-old-client"
        tel_store.record_nx_answer_run(
            question=q, plan_id=None, matched_confidence=None, step_count=1, final_text="kept", cost_usd=None, duration_ms=5,
        )
        [row] = _rows(tel_store, q)
        assert row["run_summary"] is None and row["final_text"] == "kept"

    def test_the_engine_refuses_a_summary_over_its_cap_and_the_client_never_sends_one(self, tel_store):
        big = {"version": 1, "junk": "x" * (ENGINE_RUN_SUMMARY_LIMIT_BYTES + 10)}
        with pytest.raises(httpx.HTTPStatusError) as exc:
            tel_store._post("/v1/telemetry/nx_answer_runs/record", {"question": "it-big-raw", "run_summary": big})
        assert exc.value.response.status_code == 400
        assert _rows(tel_store, "it-big-raw") == [], "the engine loses the whole run record on a 400"
        # The store holds the line: the same call through it lands the record without the summary.
        tel_store.record_nx_answer_run(
            question="it-big-store", plan_id=None, matched_confidence=None, step_count=0, final_text="t",
            cost_usd=None, duration_ms=1, run_summary=big, created_at="2026-10-27T15:00:06+00:00",
        )
        [row] = _rows(tel_store, "it-big-store")
        assert row["run_summary"] is None


class TestChokePointAgainstTheEngine:
    def test_composite_write_is_persisted_by_readback(self, tel_store):
        q, rec = "it-choke-complete", _recorder()
        _complete(_db(tel_store), rec, question=q)
        assert rec.persistence == "persisted", rec.detail
        [row] = _rows(tel_store, q)
        assert row["run_summary"]["coverage_partial"] == 1 and row["run_summary"]["repair"]["attempts"] == 1
        assert rec.to_wire()["persisted_coverage_state"] == "partial"
        assert summary_digest(row["run_summary"]) == rec.digest

    def test_the_measurement_counters_survive_jsonb_and_the_digest_still_matches(self, tel_store):
        """nexus-jypmj.3.10: the reply classes, the call log (nested lists), the booleans and the cause counts go through
        the engine's JSONB and come back equal under the canonical-JSON digest."""
        q, rec = "it-choke-counters", _recorder()
        _complete(_db(tel_store), rec, question=q)
        assert rec.persistence == "persisted", rec.detail
        [row] = _rows(tel_store, q)
        stored = row["run_summary"]["coverage"]
        assert stored["reply_classes"] == {"support": 1, "neutral_or_contradict": 1, "all_rejected": 0, "no_records": 0, "raised": 0}
        assert stored["assessment_call_log"] == [["vector", 1, "support"], ["graph", 1, "neutral_or_contradict"]]
        assert stored["unassessed_by_cause"]["other"] == 0 and stored["repair_entered"] is False and stored["facets_conflicted"] == 0
        assert row["run_summary"]["repair"]["entered"] is False and row["run_summary"]["version"] == 1
        assert summary_digest(row["run_summary"]) == rec.digest

    def test_degradation_path_is_persisted_by_readback(self, tel_store):
        q, rec = "it-choke-record", _recorder()
        _complete(_db(tel_store), rec, question=q, composite=False)
        assert rec.persistence == "persisted", rec.detail
        assert len(_rows(tel_store, q)) == 1

    def test_planner_failure_arm_threads_the_summary_through_record(self, tel_store):
        q, rec = "it-choke-planner-failure", RunSummaryRecorder(trace=True, readback=True, inputs=lambda: RunSummaryInputs(request=_request()))
        _nx_answer_record_run(
            tel_store, question=q, plan_id=None, matched_confidence=None, step_count=0, final_text="Planner error: x",
            step_records=[], duration_ms=7, trace=True, run_summary=rec, summary_outcome="planner_failure",
        )
        assert rec.persistence == "persisted", rec.detail
        [row] = _rows(tel_store, q)
        assert row["run_summary"]["outcome"] == "planner_failure" and row["run_summary"]["coverage_state"] == "none"

    def test_trace_false_row_and_summary_hold_no_private_text(self, tel_store):
        rec = _recorder(trace=False)
        _complete(_db(tel_store), rec, question=PRIVATE_QUESTION, trace=False)
        assert rec.persistence == "persisted", rec.detail
        rows = _rows(tel_store, "[redacted]")
        assert rows, "the redacted row is there"
        stored = json.dumps([r["run_summary"] for r in rows]) + json.dumps([r["final_text"] for r in rows])
        for needle in (PRIVATE_QUESTION, "SECRET-ACQUISITION", PRIVATE_FACET):
            assert needle not in stored, needle
        assert any(r["run_summary"] and r["run_summary"]["trace"] is False for r in rows)


class TestOldEngineAndRollback:
    def test_capability_off_sub_leg_sends_no_summary_and_keeps_the_record(self, tel_store):
        # A4's capability-off sub-leg: the store reads /version without the flag. The engine under it WOULD
        # keep the field, so a client that sent it anyway would leave a summary on the row.
        real_get = tel_store._get

        def get_without_flag(path, *a, **kw):
            body = real_get(path, *a, **kw)
            if path == "/version":
                body = {k: v for k, v in body.items() if k != "nx_answer_run_summary_supported"}
            return body

        q, rec = "it-capability-off", _recorder()
        tel_store._version_capabilities_cache = None
        with patch.object(tel_store, "_get", get_without_flag):
            _complete(_db(tel_store), rec, question=q)
        assert rec.persistence == "unsupported"
        rows = _rows(tel_store, q)
        assert len(rows) == 1, "no second write"
        assert rows[0]["run_summary"] is None and rows[0]["final_text"] == "the answer", "the record is intact"
        assert rec.to_wire()["digest"] is None
        tel_store._version_capabilities_cache = None

    def test_rollback_after_a_cached_flag_is_dropped_and_refreshes_the_probe(self, tel_store):
        # The cache says the engine keeps summaries; the engine then drops an unknown key (modelled by
        # stripping it from the POST body, which is all an engine that predates the column does with it).
        assert tel_store._supports_nx_answer_run_summary() is True
        real_post = tel_store._post

        def post_dropping_the_summary(path, payload, *a, **kw):
            return real_post(path, {k: v for k, v in payload.items() if k != "run_summary"}, *a, **kw)

        refreshes: list[bool] = []
        real_refresh = tel_store._refresh_version_capabilities
        q, rec = "it-rollback", _recorder()
        with patch.object(tel_store, "_post", post_dropping_the_summary), \
             patch.object(tel_store, "_refresh_version_capabilities", lambda: refreshes.append(real_refresh()) or refreshes[-1]):
            _complete(_db(tel_store), rec, question=q)
        assert rec.persistence == "dropped" and rec.detail == "summary_missing"
        assert refreshes == [False], "the probe was re-read (the real engine still says yes, so nothing changed)"
        [row] = _rows(tel_store, q)
        assert row["run_summary"] is None and row["final_text"] == "the answer"
        assert rec.to_wire()["persisted_coverage_state"] is None
