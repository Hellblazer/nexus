# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx doctor``'s exit code reports its own reds. nexus-be6x8.

The main sweep is driven end to end through the CLI with the health results
stubbed, so this pins the CONTRACT a script sees -- ``$?`` -- not the helper.
Three states, three codes: 0 healthy-or-warn, 1 hard ✗, 2 fatal ✗.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.commands.doctor as doctor_mod
import nexus.health as health_mod
from nexus.cli import main
from nexus.health import HealthResult


def _stub_sweep(monkeypatch, results: list[HealthResult]) -> None:
    monkeypatch.setattr(
        health_mod, "run_health_checks",
        lambda *_a, **_kw: (results, False),
    )
    # The supplementary checks are real probes of the box; the exit code
    # under test is the main sweep's. Keep them out of the picture.
    monkeypatch.setattr(doctor_mod, "_run_supplementary_checks", lambda: None)


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([HealthResult(label="ok", ok=True)], 0),
        ([HealthResult(label="soft", ok=False, warn=True)], 0),
        ([HealthResult(label="ok", ok=True), HealthResult(label="hard", ok=False)], 1),
        ([HealthResult(label="hard", ok=False), HealthResult(label="fatal", ok=False, fatal=True)], 2),
    ],
    ids=["healthy", "warn-only", "hard-failure", "fatal"],
)
def test_doctor_exit_code_matches_the_glyphs(monkeypatch, results, expected) -> None:
    _stub_sweep(monkeypatch, results)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == expected, result.output


def test_doctor_json_mode_uses_the_same_exit_code(monkeypatch) -> None:
    _stub_sweep(monkeypatch, [HealthResult(label="hard", ok=False, detail="x")])

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 1, result.output
    assert '"status": "fail"' in result.output


def test_a_red_line_and_a_zero_exit_can_no_longer_coexist(monkeypatch) -> None:
    """The measured shape: ✗ printed, $? == 0. Assert it is impossible now."""
    _stub_sweep(monkeypatch, [
        HealthResult(label="git hooks", ok=False, detail="older nexus stanza under core.hooksPath"),
        HealthResult(label="git hooks", ok=False, detail="older nexus stanza under core.hooksPath"),
    ])

    result = CliRunner().invoke(main, ["doctor"])

    assert "✗" in result.output
    assert result.exit_code != 0
