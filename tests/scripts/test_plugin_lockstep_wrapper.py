# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``tests/e2e/plugin-lockstep-tests.sh`` (nexus-rbmja.3): the verdict line and its non-vacuity floor.

The real wrapper runs in a throwaway git repo with a fake ``uv`` first on PATH that prints a canned
pytest result, so each assertion is about what the wrapper does with a result, not about pytest.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests import _tmp_root as tr

SCRIPT = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "plugin-lockstep-tests.sh"


def _run(tmp_path: Path, summary: str, rc: int, **env: str) -> subprocess.CompletedProcess[str]:
    repo = Path(tempfile.mkdtemp(dir=tmp_path))
    (repo / "tests" / "e2e").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "tests" / "e2e" / "plugin-lockstep-tests.sh")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "uv"
    fake.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$@\" > '{tmp_path}/uv.args'\n"
                    f"echo 'NX_TEST_T2_SUBSTRATE='\"$NX_TEST_T2_SUBSTRATE\" > '{tmp_path}/uv.env'\n"
                    f"printf '%b\\n' {summary!r}\nexit {rc}\n")  # %b: a case may pass several lines
    fake.chmod(0o755)
    return subprocess.run(
        ["bash", str(repo / "tests" / "e2e" / "plugin-lockstep-tests.sh")],
        capture_output=True, text=True, timeout=60, cwd=repo,
        env=tr.child_env_with_tmpdir(
            {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path), **env}, tmp_path
        ),
    )


def _last(r: subprocess.CompletedProcess[str]) -> str:
    return r.stdout.rstrip().splitlines()[-1]


def test_a_clean_run_prints_the_verdict_with_its_counts(tmp_path: Path) -> None:
    """Mutation (drop the PASSED echo, or read the wrong counts): the battery finds no verdict line
    and calls the leg MISSING, or the counts in the table are wrong."""
    r = _run(tmp_path, "41 passed, 2 warnings in 4.90s", 0)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _last(r) == "PLUGIN LOCKSTEP TESTS PASSED (41 passed, 0 skipped)"
    assert (tmp_path / "uv.env").read_text().strip() == "NX_TEST_T2_SUBSTRATE=none"
    args = (tmp_path / "uv.args").read_text()
    assert "tests/test_plugin_lockstep.py" in args and "tests/hooks/test_lockstep_survives_cli_skew.py" in args
    # pytest's own output reaches the battery log, ahead of the verdict line
    assert r.stdout.splitlines()[0] == "41 passed, 2 warnings in 4.90s", r.stdout


def test_the_marker_expression_is_emptied_so_the_default_addopts_cannot_deselect(tmp_path: Path) -> None:
    """``-m ""`` overrides the project's default marker deselection (nexus-rbmja.5). Mutation (drop it):
    a default-deselected lockstep test is silently not run and the count still reads PASSED."""
    _run(tmp_path, "41 passed in 1s", 0)
    lines = (tmp_path / "uv.args").read_text().splitlines()
    assert lines[lines.index("-m") + 1] == "", lines


def test_the_counts_come_from_the_last_summary_line_not_the_first(tmp_path: Path) -> None:
    """pytest can print an earlier line that looks like a summary (a nested run, a -rA section); the
    final one is the verdict. Mutation (``tail -1`` -> ``head -1``): this reads 5 passed, 1 skipped
    and fails the leg over its skip budget."""
    r = _run(tmp_path, "5 passed, 1 skipped in 1.0s\n41 passed in 4.9s", 0)
    assert r.returncode == 0 and _last(r) == "PLUGIN LOCKSTEP TESTS PASSED (41 passed, 0 skipped)", (r.stdout, r.stderr)


def test_zero_passed_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """Non-vacuity. Mutation (drop the ``PASSED -eq 0`` floor): a run that selected nothing exits 0
    with PASSED."""
    r = _run(tmp_path, "no tests ran in 0.01s", 5)  # pytest's own exit for "no tests collected"
    assert r.returncode == 1 and "PLUGIN LOCKSTEP TESTS FAILED" in _last(r), (r.stdout, r.stderr)
    r = _run(tmp_path, "3 skipped in 0.01s", 0, NX_LOCKSTEP_SKIP_BUDGET="99")
    assert r.returncode == 1 and "0 tests passed" in _last(r), (r.stdout, r.stderr)
    r = _run(tmp_path, "", 0)  # exit 0 and no summary at all
    assert r.returncode == 1 and "no summary line" in _last(r), (r.stdout, r.stderr)


def test_skips_over_the_budget_fail_and_a_stated_budget_admits_them(tmp_path: Path) -> None:
    """Mutation (drop the skip check): a lockstep file that skips itself passes."""
    r = _run(tmp_path, "40 passed, 1 skipped in 4.0s", 0)
    assert r.returncode == 1 and re.search(r"1 skipped, over the budget of 0", _last(r)), (r.stdout, r.stderr)
    r = _run(tmp_path, "40 passed, 1 skipped in 4.0s", 0, NX_LOCKSTEP_SKIP_BUDGET="1")
    assert r.returncode == 0 and _last(r) == "PLUGIN LOCKSTEP TESTS PASSED (40 passed, 1 skipped)", (r.stdout, r.stderr)


def test_a_red_test_fails_the_leg_whatever_the_counts_say(tmp_path: Path) -> None:
    """Mutation (judge by the counts alone): 40 passed beside 1 failed reads as PASSED."""
    r = _run(tmp_path, "1 failed, 40 passed in 4.0s", 1)
    assert r.returncode == 1 and _last(r).startswith("PLUGIN LOCKSTEP TESTS FAILED: pytest exit 1"), (r.stdout, r.stderr)


@pytest.mark.parametrize("budget", ["-1", "two"])
def test_a_malformed_budget_is_a_usage_error(tmp_path: Path, budget: str) -> None:
    r = _run(tmp_path, "41 passed in 1s", 0, NX_LOCKSTEP_SKIP_BUDGET=budget)
    assert r.returncode == 2 and "NX_LOCKSTEP_SKIP_BUDGET" in r.stderr, (r.stdout, r.stderr)
