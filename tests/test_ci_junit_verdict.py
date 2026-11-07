# SPDX-License-Identifier: AGPL-3.0-or-later
"""The qwent real-PID-namespace step judges its two tests by junit counts (nexus-odkc0).

Develop went red at cb1f5587d (shard 3, qwent): the two tests passed ("2 passed, 2 warnings in 1.08s"),
but the step matched the bare word "skipped" in pytest's whole output, which also carries the
storage-touch census line ("... skipped=0 failed=0 ..."). These pin the junit verdict and that the
step uses it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO: Path = Path(__file__).resolve().parents[1]
SCRIPT: Path = REPO / "scripts" / "ci_junit_verdict.py"
CI: Path = REPO / ".github" / "workflows" / "ci.yml"
STEP: str = "Real PID namespace on this qwent shard (nexus-odkc0)"


def _report(tmp_path: Path, *, tests: int, skipped: int = 0, failures: int = 0, errors: int = 0) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" skipped="{skipped}" tests="{tests}"'
        ' time="1.08" timestamp="2026-11-07T16:38:17" hostname="Qwentescence"></testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def test_two_passed_with_warnings_is_a_pass(tmp_path: Path) -> None:
    """The cb1f5587d run: two passed, warnings and a census line naming skipped=0 do not count."""
    result = _run(str(_report(tmp_path, tests=2)), "--expect", "2")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2 passed, none skipped"


@pytest.mark.parametrize(("counts", "shown"), [
    ({"tests": 2, "skipped": 1}, "skipped=1"),
    ({"tests": 2, "failures": 1}, "failures=1"),
    ({"tests": 2, "errors": 1}, "errors=1"),
    ({"tests": 1}, "tests=1"),
    ({"tests": 0}, "tests=0"),
])
def test_a_skip_failure_error_or_wrong_count_is_refused_with_the_counts(tmp_path: Path, counts, shown) -> None:
    result = _run(str(_report(tmp_path, **counts)), "--expect", "2")
    assert result.returncode == 1
    assert shown in result.stderr


def test_a_missing_or_malformed_report_is_refused(tmp_path: Path) -> None:
    assert _run(str(tmp_path / "absent.xml"), "--expect", "2").returncode == 1
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites><testsuite", encoding="utf-8")
    assert _run(str(bad), "--expect", "2").returncode == 1
    empty = tmp_path / "empty.xml"
    empty.write_text("<testsuites/>", encoding="utf-8")
    assert _run(str(empty), "--expect", "2").returncode == 1


def test_a_real_pytest_junit_report_is_read(tmp_path: Path) -> None:
    """Against pytest's own junit writer, with a test that prints the census-shaped "skipped=0"."""
    (tmp_path / "test_two.py").write_text(
        "import warnings\n"
        "def test_a():\n    print('mode=fail tests=2 skipped=0 failed=0'); warnings.warn('w')\n"
        "def test_b():\n    warnings.warn('w')\n",
        encoding="utf-8",
    )
    report = tmp_path / "out.xml"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest", "-c", "/dev/null",
         f"--rootdir={tmp_path}", f"--junitxml={report}", str(tmp_path / "test_two.py")],
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "skipped" not in run.stdout.split("\n")[-2], "non-vacuity: the summary line itself names no skip"
    result = _run(str(report), "--expect", "2")
    assert result.returncode == 0, result.stderr


def test_the_qwent_step_judges_by_the_junit_report_not_the_printed_text() -> None:
    text = CI.read_text(encoding="utf-8")
    start = text.index(f"- name: {STEP}")
    nxt = re.search(r"\n      - (name|uses):", text[start + 1:])
    step = text[start: start + 1 + nxt.start()] if nxt else text[start:]
    assert "--junitxml=" in step and "scripts/ci_junit_verdict.py" in step and "--expect 2" in step
    assert not re.search(r"=~\s*skipped", step), "the step must not match the word 'skipped' in pytest output"
