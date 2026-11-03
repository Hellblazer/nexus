# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ix3fj: the degenerate class of an ``nx_answer`` run is decided on the FULL final text at write time.

Telemetry keeps ``final_text[:2000]``. A raw ids or extractions payload longer than that no longer parses as
JSON when ``nx answer-runs`` reads it back, so the read-time heuristic counted it as an answer (run 710, raw
extractions; run 980, raw ids JSON). The writer now records ``final_chars`` and ``answer_class`` in the run
summary, computed by the reader's own classifier over the full text; the reader prefers the recorded class,
falls back to the heuristic on a row without one, and reports how many rows were classified each way.
"""
from __future__ import annotations

import ast
import json
import pathlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.commands.answer_runs import (
    _classification_basis_counts,
    _classify_degenerate_row,
    _split_four_way,
    answer_runs_cmd,
)
from nexus.plans.answer_text_class import classify_answer_text, heuristic_class
from nexus.mcp.core import _nx_answer_record_complete, _nx_answer_record_run
from nexus.plans.run_summary import (
    RunSummaryInputs,
    RunSummaryRecorder,
    build_run_summary,
    fit_summary,
)
from tests.test_rdr232_run_summary_wiring import FakeTelemetry

REPO_ROOT = pathlib.Path(__file__).parent.parent
CORE_PY = REPO_ROOT / "src" / "nexus" / "mcp" / "core.py"

#: Run 710's shape: a raw ``extractions`` payload, well past the 2000 characters telemetry keeps.
RAW_EXTRACTIONS = json.dumps({"extractions": [{"id": f"chash:{i:04d}", "text": "evidence " * 12} for i in range(40)]})
#: Run 980's shape: a raw ids JSON (the retrieval envelope).
RAW_IDS = json.dumps({"ids": [f"chash:{i:040d}" for i in range(60)], "tumblers": [], "distances": [0.1] * 60})


def _kept(text: str) -> str:
    return text[:2000]


def _row(text: str, *, summary: dict[str, Any] | None = None, steps: int = 3, rid: int = 1) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": rid, "question": "a question", "plan_id": 7, "matched_confidence": 0.9, "step_count": steps,
        "final_text": text, "cost_usd": 0.01, "duration_ms": 9000, "created_at": "2026-11-03T00:00:00Z",
    }
    if summary is not None:
        row["run_summary"] = summary
    return row


def _recorded(cls: str, chars: int) -> dict[str, Any]:
    return {
        "version": 1, "kind": "nx_answer", "outcome": "success", "trace": True,
        "coverage_complete": 0, "coverage_partial": 0, "coverage_unavailable": 0, "coverage_unassessed": 1,
        "coverage_state": "unassessed", "final_chars": chars, "answer_class": cls,
    }


class TestFixturesAreRealRuns:
    """Non-vacuity: the payloads are over the cap, are degenerate on the full text, and read as answers cut."""

    @pytest.mark.parametrize("raw,cls", [(RAW_EXTRACTIONS, "non_answer/extractions_only"), (RAW_IDS, "non_answer/retrieval_only")])
    def test_the_payload_is_degenerate_whole_and_an_answer_cut(self, raw: str, cls: str) -> None:
        assert len(raw) > 2000
        assert classify_answer_text(raw, step_count=3) == cls
        assert classify_answer_text(_kept(raw), step_count=3) == "other", "the cut payload no longer parses: the bug"


class TestTheReaderPrefersTheRecordedClass:
    @pytest.mark.parametrize("raw,cls", [(RAW_EXTRACTIONS, "non_answer/extractions_only"), (RAW_IDS, "non_answer/retrieval_only")])
    def test_a_cut_raw_payload_with_a_recorded_class_is_degenerate(self, raw: str, cls: str) -> None:
        row = _row(_kept(raw), summary=_recorded(cls, len(raw)))
        assert _classify_degenerate_row(row) == cls
        executed_ok, executed_failed, _handed, degenerate, _reports = _split_four_way([row])
        assert degenerate == {cls: [row]} and not executed_ok and not executed_failed

    def test_a_row_without_a_recorded_class_still_classifies_by_the_heuristic(self) -> None:
        short_raw = json.dumps({"extractions": [{"id": "a", "text": "b"}]})
        assert _classify_degenerate_row(_row(short_raw)) == "non_answer/extractions_only"
        assert _classify_degenerate_row(_row(_kept(RAW_EXTRACTIONS))) == "other", "old row: the heuristic still undercounts"

    @pytest.mark.parametrize("bad", [None, "", 7, ["x"], "x" * 200])
    def test_an_unusable_recorded_class_falls_back_to_the_heuristic(self, bad: Any) -> None:
        summary = {**_recorded("other", 10), "answer_class": bad}
        assert _classify_degenerate_row(_row(_kept(RAW_EXTRACTIONS), summary=summary)) == "other"

    def test_a_redacted_row_keeps_its_redaction_class(self) -> None:
        row = _row("[redacted]", summary={**_recorded("non_answer/extractions_only", 9000), "trace": False})
        row["question"] = "[redacted]"
        assert _classify_degenerate_row(row) == "redacted"

    def test_a_recorded_answer_class_keeps_an_answer_an_answer(self) -> None:
        row = _row("A real answer.", summary=_recorded("other", 14))
        assert _classify_degenerate_row(row) == "other"
        executed_ok, _f, _h, degenerate, _r = _split_four_way([row])
        assert executed_ok == [row] and not degenerate


class TestTheBasisIsReported:
    def test_counts_each_basis(self) -> None:
        rows = [
            _row(_kept(RAW_EXTRACTIONS), summary=_recorded("non_answer/extractions_only", len(RAW_EXTRACTIONS)), rid=1),
            _row("An answer.", summary=_recorded("other", 10), rid=2),
            _row(_kept(RAW_IDS), rid=3),
            _row("Old answer.", rid=4),
        ]
        assert _classification_basis_counts(rows) == {"recorded": 2, "heuristic": 2}

    @staticmethod
    def _store(rows: list[dict[str, Any]]):
        class _Store:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):  # noqa: ARG002
                return {
                    "rows": rows, "total": len(rows), "oldest_created_at": "2026-11-03T00:00:00Z",
                    "hit_count": len(rows), "fallback_count": 0, "avg_duration_ms": 9000.0, "avg_cost_usd": 0.01,
                    "latency_buckets": {"under_5s": 0, "5s_to_30s": len(rows), "30s_to_2min": 0, "2min_to_5min": 0, "over_5min": 0},
                }

        return _Store()

    def _rows(self) -> list[dict[str, Any]]:
        return [
            _row(_kept(RAW_EXTRACTIONS), summary=_recorded("non_answer/extractions_only", len(RAW_EXTRACTIONS)), rid=1),
            _row(_kept(RAW_IDS), rid=2),
            _row("Old answer.", rid=3),
        ]

    def test_json_report_counts_degenerate_from_the_recorded_class_and_states_the_basis(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: self._store(self._rows()))
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["degenerate_breakdown"] == {"non_answer/extractions_only": 1}
        assert payload["executed_ok_count"] == 2
        assert payload["classification_basis"] == {"recorded": 1, "heuristic": 2}

    def test_human_report_says_the_heuristic_rows_are_a_floor(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: self._store(self._rows()))
        out = " ".join(CliRunner().invoke(answer_runs_cmd, []).output.split())
        assert "1 by recorded class" in out and "2 by read-time heuristic" in out
        assert "lower bound" in out

    def test_human_report_omits_the_floor_note_when_every_row_is_recorded(self, monkeypatch) -> None:
        rows = [_row("An answer.", summary=_recorded("other", 10))]
        monkeypatch.setattr("nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: self._store(rows))
        out = " ".join(CliRunner().invoke(answer_runs_cmd, []).output.split())
        assert "1 by recorded class" in out and "lower bound" not in out


class TestTheWriterRecordsTheFullTextClass:
    def _summary(self, text: str | None, *, step_count: int = 3, kind: str = "nx_answer") -> dict[str, Any]:
        return build_run_summary(
            RunSummaryInputs(), outcome="success", plan_id=1, step_count=step_count, duration_ms=5, cost_usd=None,
            trace=True, coverage=None, terminal_reached=True, kind=kind, answer_text=text,
        )

    def test_build_run_summary_records_chars_and_class(self) -> None:
        s = self._summary(RAW_EXTRACTIONS)
        assert s["final_chars"] == len(RAW_EXTRACTIONS) > 2000
        assert s["answer_class"] == "non_answer/extractions_only"
        assert s["version"] == 1, "additive: the schema version does not move"

    def test_no_text_means_no_keys(self) -> None:
        s = self._summary(None, kind="continuation_report")
        assert "final_chars" not in s and "answer_class" not in s

    def test_the_two_new_keys_survive_the_last_resort_cut(self) -> None:
        s = self._summary(RAW_IDS)
        s["request"] = {"filler": "x" * 70_000}
        s["coverage"] = {"filler": "x" * 70_000}
        out = fit_summary(s)
        assert out["truncated"] and out["final_chars"] == len(RAW_IDS) and out["answer_class"] == "non_answer/retrieval_only"

    def test_there_is_one_classifier(self) -> None:
        """The writer calls the shared classifier; a stand-in for it decides the recorded class."""
        with patch("nexus.plans.run_summary.classify_answer_text", return_value="sentinel/one") as spy:
            s = self._summary("anything")
        assert s["answer_class"] == "sentinel/one"
        spy.assert_called_once_with("anything", step_count=3)

    @pytest.mark.parametrize("text,steps", [
        (RAW_EXTRACTIONS, 3), (RAW_IDS, 2), ("Error: boom", 0), ("Planner error: x", 0), ("A real answer.", 4),
        ("[non-answer: uncovered]\nbody", 2), ("", 0), ("Found 3 documents (from 3 chunks across 1 collections)\n", 1),
    ])
    def test_recorded_class_equals_the_reader_heuristic_on_the_same_text(self, text: str, steps: int) -> None:
        s = self._summary(text, step_count=steps)
        assert s["answer_class"] == _classify_degenerate_row(_row(text, steps=steps))

    def test_round_trip_a_written_run_reads_back_degenerate_from_the_cut_row(self) -> None:
        """Run 710 end to end: write through the choke point with the cut text, read the row the engine would return."""
        tel = FakeTelemetry()
        rec = RunSummaryRecorder(trace=True, readback=False, inputs=lambda: RunSummaryInputs())
        db = MagicMock()
        db.telemetry = tel
        with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)):
            _nx_answer_record_complete(
                db, question="q", plan_id=7, matched_confidence=0.5, step_count=3, final_text=_kept(RAW_EXTRACTIONS),
                step_records=[], duration_ms=10, trace=True, success=True, composite_supported_at_start=True,
                early_bump_fired=False, run_summary=rec, answer_text=RAW_EXTRACTIONS,
            )
        [(kind, kw)] = tel.writes()
        assert kind == "complete" and len(kw["final_text"]) == 2000, "telemetry still keeps the cut text"
        assert kw["run_summary"]["final_chars"] == len(RAW_EXTRACTIONS)
        row = _row(kw["final_text"], summary=kw["run_summary"])
        assert _classify_degenerate_row(row) == "non_answer/extractions_only"

    def test_without_answer_text_the_choke_points_classify_the_text_they_were_given(self) -> None:
        tel = FakeTelemetry()
        rec = RunSummaryRecorder(trace=True, readback=False, inputs=lambda: RunSummaryInputs())
        _nx_answer_record_run(
            tel, question="q", plan_id=None, matched_confidence=None, step_count=0, final_text="Error: boom",
            step_records=[], duration_ms=1, trace=True, run_summary=rec,
        )
        [(_kind, kw)] = tel.writes()
        assert kw["run_summary"]["answer_class"] == "error" and kw["run_summary"]["final_chars"] == len("Error: boom")

    def test_a_trace_false_run_records_the_class_of_the_text_it_redacted(self) -> None:
        tel = FakeTelemetry()
        rec = RunSummaryRecorder(trace=False, readback=False, inputs=lambda: RunSummaryInputs())
        db = MagicMock()
        db.telemetry = tel
        with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)):
            _nx_answer_record_complete(
                db, question="secret", plan_id=7, matched_confidence=0.5, step_count=3, final_text=_kept(RAW_IDS),
                step_records=[], duration_ms=10, trace=False, success=True, composite_supported_at_start=True,
                early_bump_fired=False, run_summary=rec, answer_text=RAW_IDS,
            )
        [(_kind, kw)] = tel.writes()
        assert kw["final_text"] == "[redacted]" and kw["question"] == "[redacted]"
        assert kw["run_summary"]["answer_class"] == "non_answer/retrieval_only"
        assert "chash:" not in json.dumps(kw["run_summary"]), "a closed name and a count, never the text"


class TestEveryCutSiteHandsOverTheFullText:
    def test_each_call_that_cuts_final_text_also_passes_answer_text(self) -> None:
        tree = ast.parse(CORE_PY.read_text())
        cut_sites = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_nx_answer_record_complete"):
                continue
            kws = {k.arg: k.value for k in node.keywords}
            if isinstance(kws.get("final_text"), ast.Subscript):
                cut_sites += 1
                assert "answer_text" in kws, f"core.py:{node.lineno} cuts final_text and does not hand over the full text"
        assert cut_sites >= 5, f"only {cut_sites} cutting call sites found: the walk is not seeing them"


class TestOneClassifierInTheNeutralLayer:
    def test_the_reader_uses_the_same_function_the_writer_does(self) -> None:
        with patch("nexus.commands.answer_runs.heuristic_class", return_value="sentinel/reader") as spy:
            assert _classify_degenerate_row(_row("anything")) == "sentinel/reader"
        spy.assert_called_once()

    @pytest.mark.parametrize("module", ["run_summary", "answer_text_class"])
    def test_plans_modules_do_not_import_the_commands_layer(self, module: str) -> None:
        tree = ast.parse((REPO_ROOT / "src" / "nexus" / "plans" / f"{module}.py").read_text())
        imported = [
            (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [n])
        ]
        assert imported, "non-vacuity: the walk saw imports"
        assert not [m for m in imported if m.startswith("nexus.commands")], imported

    def test_the_classifier_text_lives_in_one_place(self) -> None:
        assert "def heuristic_class" in (REPO_ROOT / "src/nexus/plans/answer_text_class.py").read_text()
        assert "def _heuristic_class" not in (REPO_ROOT / "src/nexus/commands/answer_runs.py").read_text()
