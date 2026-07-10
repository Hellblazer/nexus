# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-c3rer: non-vacuity gate for the schema-upgrade rehearsal CI step.

Covers ``scripts/assert_rehearsal_ran.py`` — the Surefire-report parser that
fails ``service-ci`` loudly if ``SchemaUpgradeRehearsalIntegrationTest`` was
skipped or never executed (a JUnit skip is invisible behind a green Actions
checkmark; see the script docstring and CLAUDE.md's gates-scripted-not-ambient
directive). Mirrors ``tests/scripts/test_check_engine_release_floor.py``'s
pattern for release-support scripts.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "assert_rehearsal_ran.py"

spec = importlib.util.spec_from_file_location("assert_rehearsal_ran", _SCRIPT)
_mod = importlib.util.module_from_spec(spec)
sys.modules["assert_rehearsal_ran"] = _mod
spec.loader.exec_module(_mod)


def _report(tmp_path: Path, body: str) -> str:
    p = tmp_path / "TEST-dev.nexus.service.SchemaUpgradeRehearsalIntegrationTest.xml"
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="dev.nexus.service.SchemaUpgradeRehearsalIntegrationTest">\n'
        f"{body}\n"
        "</testsuite>\n",
        encoding="utf-8",
    )
    return str(p)


def test_ran_clean_passes(tmp_path, capsys):
    path = _report(
        tmp_path,
        '<testcase name="oldTagToHeadUpgradeSurvivesInjectedDivergence" time="42.0"/>',
    )
    _mod.main(path)  # must not raise
    assert "non-vacuity OK" in capsys.readouterr().out


def test_missing_report_fails(tmp_path):
    with pytest.raises(SystemExit, match="never executed"):
        _mod.main(str(tmp_path / "TEST-nope.xml"))


def test_zero_testcases_fails(tmp_path):
    path = _report(tmp_path, "")
    with pytest.raises(SystemExit, match="zero"):
        _mod.main(path)


def test_skipped_case_fails_and_surfaces_reason(tmp_path):
    path = _report(
        tmp_path,
        '<testcase name="oldTagToHeadUpgradeSurvivesInjectedDivergence">\n'
        '  <skipped message="old tag engine-service-v0.1.17 unavailable"/>\n'
        "</testcase>",
    )
    with pytest.raises(SystemExit, match="engine-service-v0.1.17 unavailable"):
        _mod.main(path)
