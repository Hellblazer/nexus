# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/check_junit_floor.py`` fails an all-skip or too-thin run (nexus-f9bgu.19)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_junit_floor.py"
spec = importlib.util.spec_from_file_location("check_junit_floor", SCRIPT)
assert spec is not None and spec.loader is not None
floor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(floor)


def _report(tmp_path: Path, *, passed: int = 0, skipped: int = 0, failed: int = 0, errored: int = 0) -> Path:
    cases = (
        [f'<testcase name="p{i}"/>' for i in range(passed)]
        + [f'<testcase name="s{i}"><skipped type="pytest.skip"/></testcase>' for i in range(skipped)]
        + [f'<testcase name="f{i}"><failure message="x"/></testcase>' for i in range(failed)]
        + [f'<testcase name="e{i}"><error message="x"/></testcase>' for i in range(errored)]
    )
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>')
    return path


def test_a_run_that_meets_the_floor_passes(tmp_path: Path) -> None:
    assert floor.main([str(_report(tmp_path, passed=70, skipped=4)), "--min-passed", "65", "--max-skipped", "4"]) == 0


def test_an_all_skip_run_fails(tmp_path: Path) -> None:
    report = _report(tmp_path, passed=0, skipped=70)
    assert floor.check(report, min_passed=65, max_skipped=4)
    assert floor.main([str(report), "--min-passed", "65", "--max-skipped", "4"]) == 1


def test_too_few_passes_fails_even_with_no_skips(tmp_path: Path) -> None:
    assert floor.main([str(_report(tmp_path, passed=10)), "--min-passed", "65", "--max-skipped", "4"]) == 1


def test_one_more_skip_than_the_ceiling_fails(tmp_path: Path) -> None:
    assert floor.main([str(_report(tmp_path, passed=70, skipped=5)), "--min-passed", "65", "--max-skipped", "4"]) == 1


@pytest.mark.parametrize("kind", ["failed", "errored"])
def test_a_failure_or_error_fails_a_run_that_otherwise_meets_the_floor(tmp_path: Path, kind: str) -> None:
    report = _report(tmp_path, passed=70, **{kind: 1})
    assert floor.main([str(report), "--min-passed", "65", "--max-skipped", "4"]) == 1


def _named_report(tmp_path: Path, cases: list[tuple[str, str, str]]) -> Path:
    """``(classname, name, "pass"|"skip"|"fail")`` testcases, plus filler to clear the count floor."""
    body = []
    for classname, name, status in cases:
        inner = {"pass": "", "skip": '<skipped type="pytest.skip"/>', "fail": '<failure message="x"/>'}[status]
        body.append(f'<testcase classname="{classname}" name="{name}">{inner}</testcase>')
    body += [f'<testcase classname="tests.filler" name="p{i}"/>' for i in range(10)]
    path = tmp_path / "named.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{"".join(body)}</testsuite></testsuites>')
    return path


_FLOOR = ["--min-passed", "5", "--max-skipped", "9"]


def test_a_required_test_that_passed_meets_the_floor(tmp_path: Path) -> None:
    report = _named_report(tmp_path, [("tests.test_winsec.TestRealWindows", "test_a", "pass")])
    assert floor.main([str(report), *_FLOOR, "--require-passed", "tests.test_winsec.TestRealWindows"]) == 0


def test_a_required_test_that_was_skipped_fails_though_the_counts_are_fine(tmp_path: Path) -> None:
    """The point of the option: a skipped real-kernel test sits well inside the skip ceiling."""
    report = _named_report(tmp_path, [("tests.test_winsec.TestRealWindows", "test_a", "skip")])
    assert floor.main([str(report), *_FLOOR]) == 0, "non-vacuity: the counts alone accept this run"
    assert floor.main([str(report), *_FLOOR, "--require-passed", "tests.test_winsec.TestRealWindows"]) == 1


def test_a_required_test_that_is_absent_from_the_report_fails(tmp_path: Path) -> None:
    report = _named_report(tmp_path, [("tests.other", "test_a", "pass")])
    problems = floor.check(report, min_passed=5, max_skipped=9, require_passed=["TestRealWindows"])
    assert problems == ["required test 'TestRealWindows' is not in the report"]


def test_every_match_must_pass_not_just_one(tmp_path: Path) -> None:
    report = _named_report(tmp_path, [
        ("tests.test_winsec.TestRealWindows", "test_a", "pass"),
        ("tests.test_winsec.TestRealWindows", "test_b", "skip"),
    ])
    problems = floor.check(report, min_passed=5, max_skipped=9, require_passed=["TestRealWindows"])
    assert len(problems) == 1 and "test_b (skipped)" in problems[0] and "test_a" not in problems[0]


def test_a_required_test_that_failed_is_reported_as_not_passed(tmp_path: Path) -> None:
    report = _named_report(tmp_path, [("tests.x.TestK", "test_job", "fail")])
    problems = floor.check(report, min_passed=5, max_skipped=9, require_passed=["test_job"])
    assert any("test_job (failed)" in p for p in problems)


def test_the_pattern_may_be_given_more_than_once(tmp_path: Path) -> None:
    report = _named_report(tmp_path, [("tests.a.TestA", "t", "pass"), ("tests.b.TestB", "t", "skip")])
    argv = [str(report), *_FLOOR, "--require-passed", "TestA", "--require-passed", "TestB"]
    assert floor.main(argv) == 1
    assert floor.main(argv[:-2]) == 0


def test_an_unreadable_report_is_a_failed_gate_not_a_pass(tmp_path: Path) -> None:
    assert floor.main([str(tmp_path / "missing.xml"), "--min-passed", "1", "--max-skipped", "9"]) == 2
    bad = tmp_path / "bad.xml"
    bad.write_text("not xml")
    assert floor.main([str(bad), "--min-passed", "1", "--max-skipped", "9"]) == 2
