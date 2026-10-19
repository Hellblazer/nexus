# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A missing git is a warning in ``nx doctor``, never a failure. nexus-36qyh.

A Claude Desktop-only Windows user has no git: Desktop gets the plugin from the
account, and git is needed only to index repositories and for Claude Code's
plugin marketplace. The row used to be fatal, so the setup verify step
(``nx doctor``'s exit code) ended ``doctor-failed`` on a box where the service,
the logon task and search all worked.

Every test runs the real row producer with a ``PATH`` that holds no git.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.commands.doctor as doctor_mod
import nexus.health as health_mod
from nexus.cli import main
from nexus.health import (
    EXIT_HEALTHY,
    HealthResult,
    _check_tools,
    format_health_for_cli,
    health_exit_code,
)


@pytest.fixture
def no_git_path(tmp_path, monkeypatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def _git_row() -> HealthResult:
    (row,) = [r for r in _check_tools() if r.label == "git"]
    return row


def test_missing_git_is_a_warning_not_a_failure(no_git_path) -> None:
    row = _git_row()

    assert row.ok is False
    assert row.warn is True
    assert row.fatal is False


def test_missing_git_names_git_and_what_needs_it(no_git_path) -> None:
    row = _git_row()

    assert "not found on PATH" in row.detail
    assert "indexing" in row.detail
    assert "marketplace" in row.detail
    assert row.fix_suggestions, "the install hints stay"


def test_missing_git_leaves_the_exit_code_healthy(no_git_path) -> None:
    results = [HealthResult(label="ok", ok=True), _git_row()]

    assert health_exit_code(results) == EXIT_HEALTHY
    output, failed = format_health_for_cli(results, local_mode=True)
    assert failed is False
    assert "⚠ git: " in output
    assert "✗ git" not in output


@pytest.mark.t2_tenant
def test_doctor_cli_exits_zero_with_only_git_missing(no_git_path, monkeypatch) -> None:
    results = [r for r in _check_tools() if r.label == "git"]
    monkeypatch.setattr(health_mod, "run_health_checks", lambda *_a, **_kw: (results, False))
    monkeypatch.setattr(doctor_mod, "_run_supplementary_checks", lambda *_a, **_kw: None)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "git" in result.output
