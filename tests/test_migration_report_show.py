# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-153 Phase 4 (bead nexus-j1l13, TDD-red): the triage surface —
``nx storage migration-report show <path>``.

The Phase-4 SQLite-deletion gate reads this report; the predicate is
``summary.total_failed == 0`` (``failed`` is reserved for unparseable/
unexpected — every expected-bad row is skipped/flagged/handled/
schema_corrected). The reader lives in ``src/nexus/migration`` so it
survives the RDR-152 P4 deletion of ``src/nexus/db/t2``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.migration.migration_report import IssueCollector, build_report


def _sample_report(*, failed: int = 0, verification: str | None = "verified") -> dict:
    c = IssueCollector()
    c.count_read("taxonomy", "topic_assignments", 180806)
    c.count_written("taxonomy", "topic_assignments", 129520)
    for i in range(3):
        c.record(
            "taxonomy", "topic_assignments",
            issue_class="orphan_parent",
            constraint="topic_assignments.topic_id -> topics.id",
            reason="topic_id references a deleted topic",
            action="skipped",
            sample_id=f"d{i}:t9",
        )
    c.record(
        "catalog", "links",
        issue_class="soft_dangler",
        constraint="links.from_tumbler -> documents.tumbler",
        reason="endpoint missing",
        action="flagged",
        sample_id="1.1.1:9.9.9",
    )
    c.record(
        "telemetry", "hook_failures",
        issue_class="format_anomaly",
        constraint="hook_failures.occurred_at",
        reason="space-form timestamp normalized",
        action="handled",
        sample_id="hf-1",
    )
    for i in range(failed):
        c.record(
            "memory", "memory",
            issue_class="unexpected",
            constraint="memory(project,title)",
            reason="row rejected during import: boom",
            action="failed",
            sample_id=f"p:t{i}",
        )
    report = build_report(
        c,
        source={"sqlite": "/cfg/memory.db", "catalog_db": "/cfg/.catalog.db"},
        target={"service_url": "http://127.0.0.1:5999"},
        migration_id="11111111-2222-3333-4444-555555555555",
    )
    if verification is not None:
        report["verification"] = verification
    return report


def _write(tmp_path: Path, report: dict) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return path


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestShowSummary:
    def test_clean_report_gate_pass(self, runner, tmp_path: Path) -> None:
        path = _write(tmp_path, _sample_report())
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "11111111-2222-3333-4444-555555555555" in out
        assert "GATE: PASS" in out
        assert "total_failed=0" in out
        assert "verification: verified" in out

    def test_max_severity_first_and_by_action_order(
        self, runner, tmp_path: Path,
    ) -> None:
        """max_severity leads the summary; by-action lines run
        severity-descending (failed, skipped, flagged, handled,
        schema_corrected)."""
        path = _write(tmp_path, _sample_report())
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        out = result.output
        sev_idx = out.index("max_severity=3")
        action_idx = out.index("skipped=3")
        assert sev_idx < action_idx
        assert out.index("skipped=3") < out.index("flagged=1")
        assert out.index("flagged=1") < out.index("handled=1")

    def test_issue_lines_severity_descending_with_samples(
        self, runner, tmp_path: Path,
    ) -> None:
        path = _write(tmp_path, _sample_report(failed=1))
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        out = result.output
        # The failed issue (severity 4) is listed before the skipped (3),
        # which precedes flagged (2) and handled (1).
        assert out.index("memory.memory") < out.index("taxonomy.topic_assignments")
        assert out.index("taxonomy.topic_assignments") < out.index("catalog.links")
        assert out.index("catalog.links") < out.index("telemetry.hook_failures")
        # Issue lines carry class, action, count, and a sample.
        assert "orphan_parent" in out
        assert "count=51286" not in out  # exact fixture counts only
        assert "count=3" in out
        assert "d0:t9" in out

    def test_failed_report_gate_fail_exit_nonzero(
        self, runner, tmp_path: Path,
    ) -> None:
        path = _write(tmp_path, _sample_report(failed=2))
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code != 0
        assert "GATE: FAIL" in result.output
        assert "total_failed=2" in result.output

    def test_verification_absent_reported_not_recorded(
        self, runner, tmp_path: Path,
    ) -> None:
        # Older artifacts (or per-store reports) carry no verification key —
        # the viewer says so explicitly rather than implying it passed.
        path = _write(tmp_path, _sample_report(verification=None))
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code == 0, result.output
        assert "verification: (not recorded)" in result.output

    def test_missing_file_clean_error(self, runner, tmp_path: Path) -> None:
        result = runner.invoke(main, [
            "storage", "migration-report", "show", str(tmp_path / "absent.json"),
        ])
        # exit 1 + the command's OWN message (a usage error would be exit 2
        # without it — the original assertions passed before the command
        # existed; P4 review tightened them).
        assert result.exit_code == 1
        assert "report not found" in result.output
        assert "Traceback" not in result.output

    def test_malformed_json_clean_error(self, runner, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code == 1
        assert "unreadable report" in result.output
        assert "Traceback" not in result.output

    def test_missing_summary_never_defaults_to_pass(
        self, runner, tmp_path: Path,
    ) -> None:
        """P4 review CRITICAL: a structurally damaged artifact (no summary)
        must FAIL the gate loudly — never evaluate the predicate against
        defaults. This is the deletion gate's last line of defense."""
        path = tmp_path / "stub.json"
        path.write_text(
            json.dumps({"schema_version": "1", "migration_id": "x", "stores": []})
        )
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code == 1
        assert "cannot evaluate the gate predicate" in result.output
        assert "GATE: PASS" not in result.output
        assert "Traceback" not in result.output

    def test_malformed_total_failed_clean_error(
        self, runner, tmp_path: Path,
    ) -> None:
        report = _sample_report()
        report["summary"]["total_failed"] = "oops"
        path = _write(tmp_path, report)
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code == 1
        assert "not an integer" in result.output
        assert "GATE: PASS" not in result.output
        assert "Traceback" not in result.output

    def test_gate_fail_carries_rerun_hint(self, runner, tmp_path: Path) -> None:
        path = _write(tmp_path, _sample_report(failed=1))
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code == 1
        assert "idempotent" in result.output

    def test_unknown_schema_version_warns_but_displays(
        self, runner, tmp_path: Path,
    ) -> None:
        report = _sample_report()
        report["schema_version"] = "99"
        path = _write(tmp_path, report)
        result = runner.invoke(main, ["storage", "migration-report", "show", str(path)])
        assert result.exit_code == 0, result.output
        assert "schema_version 99" in result.output  # loud note
        assert "GATE: PASS" in result.output         # best-effort display


class TestReaderModule:
    def test_reader_lives_in_migration_package(self) -> None:
        """§Approach 1: the reader must survive the RDR-152 P4 deletion of
        src/nexus/db/t2 — it lives in nexus.migration and imports nothing
        from nexus.db.t2."""
        import inspect

        from nexus.migration import migration_report as mod

        assert hasattr(mod, "load_report")
        src = inspect.getsource(mod)
        assert "nexus.db.t2" not in src

    def test_load_report_round_trip(self, tmp_path: Path) -> None:
        from nexus.migration.migration_report import load_report

        report = _sample_report()
        path = _write(tmp_path, report)
        assert load_report(path) == report

    def test_load_report_missing_fails_loud(self, tmp_path: Path) -> None:
        from nexus.migration.migration_report import load_report

        with pytest.raises(FileNotFoundError):
            load_report(tmp_path / "absent.json")
