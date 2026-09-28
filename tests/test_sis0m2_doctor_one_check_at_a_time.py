# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.2: nx doctor with several --check-* flags does not silently run
only one of them.

Shakeout 7.64.1 Surface F F7 (T2 nexus/shakeout-7.64.1-local-driver-2026-09-28):
``nx doctor --check-references --check-assignments`` ran only the first by
precedence and exited 0, so the operator believed both had passed.
"""
from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from nexus.cli import main


def test_two_check_flags_are_refused_naming_both():
    ran: list[str] = []
    with patch("nexus.doctor_references.run_check_references", lambda **k: ran.append("references")), \
            patch("nexus.doctor_assignments.run_check_assignments", lambda **k: ran.append("assignments")):
        result = CliRunner().invoke(main, ["doctor", "--check-references", "--check-assignments"])

    assert result.exit_code == 2, result.output
    assert "--check-references" in result.output and "--check-assignments" in result.output, result.output
    assert ran == [], "nothing runs when the request cannot be honoured as asked"


def test_one_check_flag_still_runs():
    ran: list[str] = []
    with patch("nexus.doctor_references.run_check_references", lambda **k: ran.append("references")):
        result = CliRunner().invoke(main, ["doctor", "--check-references"])
    assert result.exit_code == 0, result.output
    assert ran == ["references"]


def test_check_mcp_logs_that_scanned_nothing_is_not_a_pass(tmp_path, monkeypatch):
    """Shakeout 7.64.1 Surface E F5: --check-mcp-logs keys Claude Code's cache
    directory on cwd. From a directory Claude Code never ran in it scanned 0
    files and printed "No silent-death ... signatures found", rc 0."""
    import nexus.commands.doctor as doctor

    empty_project = tmp_path / "claude-cli-nodejs" / "-private-tmp"
    empty_project.mkdir(parents=True)
    monkeypatch.setattr(doctor, "_resolve_claude_cache_dir", lambda cwd=None: empty_project)
    monkeypatch.setattr(doctor, "_resolve_nexus_log_dir", lambda: tmp_path / "logs")

    result = CliRunner().invoke(main, ["doctor", "--check-mcp-logs"])

    assert result.exit_code == 1, result.output
    assert "No silent-death" not in result.output, result.output
    assert "nothing was checked" in result.output, result.output
    assert str(empty_project) in result.output


def test_a_check_mode_and_a_fix_mode_are_refused_together():
    """Review of 2896b2507: --fix, --fix-paths, --trim-telemetry and the
    --clean-* modes sit in the same run-and-return chain, so
    `nx doctor --check-schema --fix` silently dropped --fix."""
    result = CliRunner().invoke(main, ["doctor", "--check-schema", "--fix"])
    assert result.exit_code == 2, result.output
    assert "--check-schema" in result.output and "--fix" in result.output
