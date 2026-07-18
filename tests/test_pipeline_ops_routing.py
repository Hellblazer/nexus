# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ops-surface routing for the engine-backed pipeline buffer (RDR-186 .16).

The pinned client work list's item 4 (health/doctor routing) — coverage the
critic flagged as absent: ``nx doctor --clean-pipelines`` and health's
``_check_orphan_pipelines`` exercised against the fake engine, plus their
engine-unreachable degradation paths (doctor: clean ClickException; health:
best-effort ok=True skip).
"""
from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from nexus.commands.doctor import doctor_cmd
from nexus.health import _check_orphan_pipelines
from tests.pipeline_fake_engine import make_fake_engine_db


def _seed_orphan(db) -> None:
    db.create_pipeline("gone" * 8, "/nonexistent/gone.pdf", "docs__t")
    db.create_pipeline("live" * 8, __file__, "docs__t")
    db.flush_all()


class TestDoctorCleanPipelines:
    def test_clean_pipelines_deletes_orphans_via_engine(self):
        db, engine = make_fake_engine_db()
        _seed_orphan(db)
        with patch("nexus.db.http_pipeline_client.HttpPipelineDB", return_value=db):
            result = CliRunner().invoke(doctor_cmd, ["--clean-pipelines"])
        assert result.exit_code == 0, result.output
        assert "Deleted 1 orphaned" in result.output
        assert "gone" * 8 not in engine.pipelines
        assert "live" * 8 in engine.pipelines

    def test_clean_pipelines_none_found(self):
        db, _engine = make_fake_engine_db()
        with patch("nexus.db.http_pipeline_client.HttpPipelineDB", return_value=db):
            result = CliRunner().invoke(doctor_cmd, ["--clean-pipelines"])
        assert result.exit_code == 0, result.output
        assert "No orphaned pipeline entries" in result.output

    def test_clean_pipelines_engine_unreachable_fails_clean(self):
        """Engine down = clean ClickException, never a stack trace
        (code-review Medium fold)."""
        with patch(
            "nexus.db.http_pipeline_client.HttpPipelineDB",
            side_effect=ConnectionError("engine down"),
        ):
            result = CliRunner().invoke(doctor_cmd, ["--clean-pipelines"])
        assert result.exit_code != 0
        assert "pipeline scan unavailable" in result.output
        assert "Traceback" not in result.output


class TestHealthOrphanPipelines:
    def test_reports_orphans_with_fix_suggestion(self):
        db, _engine = make_fake_engine_db()
        _seed_orphan(db)
        with patch("nexus.db.http_pipeline_client.HttpPipelineDB", return_value=db):
            results = _check_orphan_pipelines()
        assert len(results) == 1 and not results[0].ok
        assert "1 orphaned" in results[0].detail
        assert any("--clean-pipelines" in s for s in results[0].fix_suggestions)

    def test_clean_state_reports_ok(self):
        db, _engine = make_fake_engine_db()
        db.create_pipeline("live" * 8, __file__, "docs__t")
        with patch("nexus.db.http_pipeline_client.HttpPipelineDB", return_value=db):
            results = _check_orphan_pipelines()
        assert len(results) == 1 and results[0].ok
        assert "none orphaned" in results[0].detail

    def test_engine_unreachable_is_best_effort_skip(self):
        """Health must degrade to ok=True skip — a down engine must not
        fail the whole doctor run over an advisory check."""
        with patch(
            "nexus.db.http_pipeline_client.HttpPipelineDB",
            side_effect=ConnectionError("engine down"),
        ):
            results = _check_orphan_pipelines()
        assert len(results) == 1 and results[0].ok
        assert "scan failed" in results[0].detail
