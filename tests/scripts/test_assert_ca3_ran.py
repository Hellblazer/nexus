# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/assert_ca3_ran.py: the non-vacuity asserter for the bundle gates. Its extra-name path
(nexus-f3yqd.2) makes the relocation step fail unless the pg_background worker test passed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "assert_ca3_ran_under_test", Path(__file__).resolve().parents[2] / "scripts" / "assert_ca3_ran.py"
)
assert _spec is not None and _spec.loader is not None
asserter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asserter)

CORE = "test_create_extension_vector_loads"
EXTRA = "test_create_extension_pg_background_runs_a_worker"


def _junit(tmp_path: Path, *cases: str) -> str:
    """cases: 'name' passes, 'name:skip' skips (platform), 'name:skip-bundle' skips for bundle absence,
    'name:fail' fails."""
    body = []
    for case in cases:
        name, _, outcome = case.partition(":")
        inner = {
            "": "",
            "skip": '<skipped message="minos floor is macOS-only"/>',
            "skip-bundle": '<skipped message="skipped: no CA-3 bundle (relocated)"/>',
            "fail": '<failure message="boom"/>',
        }[outcome]
        body.append(f'<testcase classname="c" name="{name}">{inner}</testcase>')
    path = tmp_path / "x.xml"
    path.write_text(f'<testsuites><testsuite>{"".join(body)}</testsuite></testsuites>')
    return str(path)


def test_the_default_core_test_alone_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    asserter.main(_junit(tmp_path, CORE, "test_x:skip"))
    assert "CA-3 PASS: 1 passed, 0 bundle-skips" in capsys.readouterr().out


def test_an_extra_name_that_passed_is_accepted(tmp_path: Path) -> None:
    asserter.main(_junit(tmp_path, CORE, EXTRA), EXTRA)


@pytest.mark.parametrize("extra_case", [None, f"{EXTRA}:skip", f"{EXTRA}:fail"])
def test_an_extra_name_that_is_absent_skipped_or_failed_is_refused(
    tmp_path: Path, extra_case: str | None
) -> None:
    cases = [CORE] + ([extra_case] if extra_case else [])
    with pytest.raises(AssertionError, match=EXTRA):
        asserter.main(_junit(tmp_path, *cases), EXTRA)


def test_the_core_test_is_still_required_when_extras_pass(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match=CORE):
        asserter.main(_junit(tmp_path, EXTRA), EXTRA)


def test_a_bundle_absence_skip_fails_whatever_else_passed(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="bundle-absence"):
        asserter.main(_junit(tmp_path, CORE, EXTRA, "test_y:skip-bundle"), EXTRA)
