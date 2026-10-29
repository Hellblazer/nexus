# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 step 3 (nexus-jypmj.3.3): ``nx answer-runs`` puts insufficient-evidence rows in their own bucket.

S1 of the gate critique: a ``trace=False`` row records ``final_text`` as ``[redacted]``, so the marker the partial
notice opens with cannot classify it. Rule under test:

* a row that carries a run summary is bucketed from the summary (``coverage_partial`` on a successful
  ``nx_answer`` run), for both ``trace`` values;
* a row without a summary is bucketed from the marker when its text is visible (``trace=True``), and stays
  redacted when it is not (``trace=False``) -- documented in the command's help text.

Rows are built by hand; a fake telemetry store stands in for the engine, as in ``tests/test_answer_runs_cli.py``.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands.answer_runs import (
    _classify_degenerate_row,
    _row_is_insufficient_evidence,
    _split_four_way,
    _split_insufficient_evidence,
    answer_runs_cmd,
)

MARKER = "[insufficient-evidence: 2 unresolved]\nUnresolved facets:\n  - f2: x\n\nThe answer."


def _summary(*, partial: int = 1, outcome: str = "success", kind: str = "nx_answer", trace: bool = True) -> dict[str, Any]:
    state = "partial" if partial else "complete"
    return {
        "version": 1, "kind": kind, "outcome": outcome, "trace": trace,
        "coverage_complete": int(not partial), "coverage_partial": partial, "coverage_unavailable": 0,
        "coverage_unassessed": 0, "coverage_state": state,
    }


def _row(*, text: str = MARKER, summary: dict[str, Any] | None = None, steps: int = 4, redacted: bool = False, rid: int = 1) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": rid, "question": "[redacted]" if redacted else "a question", "plan_id": 7, "matched_confidence": 0.9,
        "step_count": steps, "final_text": "[redacted]" if redacted else text, "cost_usd": 0.01, "duration_ms": 9000,
        "created_at": "2026-10-27T00:00:00Z",
    }
    if summary is not None:
        row["run_summary"] = summary
    return row


class TestTheBucketRule:
    def test_a_traced_row_with_a_summary_is_in_the_bucket(self) -> None:
        assert _row_is_insufficient_evidence(_row(summary=_summary())) is True

    def test_scenario_3_a_redacted_row_with_a_summary_lands_in_the_bucket_not_redacted(self) -> None:
        row = _row(redacted=True, summary=_summary(trace=False))
        assert _classify_degenerate_row(row) == "redacted", "non-vacuity: the text alone says redacted"
        assert _row_is_insufficient_evidence(row) is True
        bucket, rest = _split_insufficient_evidence([row])
        assert bucket == [row] and rest == []

    def test_scenario_4_a_redacted_row_without_a_summary_stays_redacted(self) -> None:
        row = _row(redacted=True, summary=None)
        assert _row_is_insufficient_evidence(row) is False
        bucket, rest = _split_insufficient_evidence([row])
        assert bucket == [] and rest == [row]
        assert _classify_degenerate_row({**row, "step_count": 0}) == "redacted"

    def test_a_traced_row_without_a_summary_is_bucketed_by_the_marker(self) -> None:
        assert _row_is_insufficient_evidence(_row(summary=None)) is True

    def test_a_summary_that_says_complete_beats_marker_text(self) -> None:
        """A prose answer that happens to open with the marker text is not a partial run when the run's own
        summary says it was complete (collision hardening, as for the continuation markers)."""
        assert _row_is_insufficient_evidence(_row(summary=_summary(partial=0))) is False

    @pytest.mark.parametrize("summary", [
        _summary(outcome="failure"), _summary(outcome="unscored"), _summary(outcome="handoff"),
        _summary(kind="continuation_report"),
    ])
    def test_a_partial_state_on_a_run_that_did_not_succeed_is_not_an_insufficient_evidence_result(self, summary: dict[str, Any]) -> None:
        assert _row_is_insufficient_evidence(_row(text="[budget exhausted (time) after step 2 of 4 — partial answer]", summary=summary)) is False

    def test_a_marker_that_is_not_at_the_start_is_not_a_marker(self) -> None:
        assert _row_is_insufficient_evidence(_row(text="The answer quotes [insufficient-evidence: 2 unresolved] mid-text.")) is False

    def test_an_insufficient_evidence_row_is_not_degenerate_and_not_executed_ok(self) -> None:
        row = _row(summary=_summary())
        ok, failed, handed, degenerate, reports = _split_four_way(_split_insufficient_evidence([row])[1])
        assert (ok, failed, handed, degenerate, reports) == ([], [], [], {}, [])


class TestCoverageDecidedNonAnswers:
    @pytest.mark.parametrize("shape", ["uncovered", "no_evidence"])
    def test_a_row_with_executed_steps_and_the_notice_is_degenerate_not_executed_ok(self, shape: str) -> None:
        """Without the two classes in the well-formed set, a row with steps would fall through to executed-ok."""
        row = _row(text=f"[non-answer: {shape}] The plan (plan_id=7, 4 steps) ran.", summary=None, steps=4)
        ok, failed, handed, degenerate, reports = _split_four_way([row])
        assert ok == [] and failed == [] and list(degenerate) == [f"non_answer/{shape}"]

    @pytest.mark.parametrize(("reason", "cls"), [
        ("uncovered", "non_answer/uncovered"),
        ("no_evidence", "non_answer/no_evidence"),
        ("scope_conflict", "non_answer/no_evidence"),
        ("no_usable_answer", "non_answer/no_usable_answer"),
    ])
    def test_a_redacted_row_whose_summary_says_unavailable_is_degenerate_not_executed_ok(self, reason: str, cls: str) -> None:
        """Shakedown 2026-10-28 finding 4 (nexus-jypmj.3.15): run 744 (trace=False, uncovered) counted executed-ok
        while its trace=True twin 745 was degenerate, because the redacted text was classified before the summary
        was read."""
        summary = {**_summary(partial=0, trace=False), "coverage_complete": 0, "coverage_unavailable": 1,
                   "coverage_state": "unavailable", "coverage": {"state": "unavailable", "reason": reason}}
        row = _row(redacted=True, summary=summary, steps=5)
        assert _classify_degenerate_row(row) == cls
        ok, failed, handed, degenerate, reports = _split_four_way([row])
        assert ok == [] and failed == [] and list(degenerate) == [cls]

    def test_a_redacted_unavailable_row_whose_coverage_block_was_cut_is_still_degenerate(self) -> None:
        """fit_summary may cut the ``coverage`` block; the kept ``coverage_state`` still says unavailable."""
        summary = {**_summary(partial=0, trace=False), "coverage_complete": 0, "coverage_unavailable": 1,
                   "coverage_state": "unavailable"}
        ok, _, _, degenerate, _ = _split_four_way([_row(redacted=True, summary=summary, steps=5)])
        assert ok == [] and list(degenerate) == ["non_answer/no_usable_answer"]

    def test_a_redacted_complete_row_stays_redacted_and_executed_ok(self) -> None:
        """Control: only an unavailable summary moves a redacted row."""
        row = _row(redacted=True, summary=_summary(partial=0, trace=False), steps=5)
        assert _classify_degenerate_row(row) == "redacted"
        assert _split_four_way([row])[0] == [row]


class TestTheReport:
    @staticmethod
    def _store(rows: list[dict[str, Any]]):
        class _Store:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):  # noqa: ARG002
                return {
                    "rows": rows, "total": len(rows), "oldest_created_at": "2026-10-27T00:00:00Z",
                    "hit_count": len(rows), "fallback_count": 0, "avg_duration_ms": 9000.0, "avg_cost_usd": 0.01,
                    "latency_buckets": {"under_5s": 0, "5s_to_30s": len(rows), "30s_to_2min": 0, "2min_to_5min": 0, "over_5min": 0},
                }

        return _Store()

    def _rows(self) -> list[dict[str, Any]]:
        return [
            _row(rid=1, summary=_summary()),                                    # traced, partial
            _row(rid=2, redacted=True, summary=_summary(trace=False)),          # redacted, partial (scenario 3)
            _row(rid=3, redacted=True, summary=None),                           # redacted, no summary (scenario 4)
            _row(rid=4, text="A complete answer.", summary=_summary(partial=0)),
        ]

    def test_json_counts_the_bucket_and_keeps_it_out_of_the_other_buckets(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: self._store(self._rows()))
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["insufficient_evidence_count"] == 2
        assert payload["executed_ok_count"] == 2, "the redacted summary-less row (scenario 4) and the complete answer"
        assert payload["degenerate_count"] == 0 and payload["executed_failed_count"] == 0

    def test_human_report_names_the_bucket(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: self._store(self._rows()))
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "insufficient-evidence 2" in result.output

    def test_help_states_the_no_summary_fallback(self) -> None:
        text = " ".join(CliRunner().invoke(answer_runs_cmd, ["--help"]).output.split())
        assert "insufficient-evidence" in text
        assert "run summary" in text and "coverage_partial" in text
        assert "trace=False" in text and "redacted" in text and "without a summary" in text
