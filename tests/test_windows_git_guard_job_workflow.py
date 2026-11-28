# SPDX-License-Identifier: AGPL-3.0-or-later
"""The git-guard routing tests' native-Windows job (git-guard-windows in windows-pg-bundle-rehearsal.yml).

Split from tests/test_windows_release_legs_workflow.py, which is at its file-size cap. The switch and
pull_request properties that hold for every job on the win-release label are pinned there; this pins the
job's own shape. The three files are spawn-bound (about 17,000 process starts, 1753 s serially on native
Windows), so they run under xdist in a job of their own rather than in conformance's serial set.
"""
from __future__ import annotations

import re

from tests.test_windows_release_legs_workflow import LABEL, REHEARSAL, SWITCH, WINDOWS_XDIST_SET, _doc


def test_the_git_guard_tests_run_natively_under_xdist_in_a_job_of_their_own_with_a_floor() -> None:
    """The three git-guard files are spawn-bound: 1753 s serially on native Windows, 673 s at -n 8 (process
    starts scale poorly there). A job of their own keeps conformance's serial set inside its 30-minute limit."""
    doc = _doc(REHEARSAL)
    job = doc["jobs"]["git-guard-windows"]
    conformance = doc["jobs"]["conformance"]
    assert job["runs-on"] == LABEL
    # The same gate as conformance, so it runs exactly when conformance does (the triggers are the workflow's).
    assert job["if"] == conformance["if"]
    assert "github.actor == 'Hellblazer'" in job["if"] and SWITCH in job["if"]
    assert "needs" not in job and "needs" not in conformance, "a red in either is the run's red; nothing gates it"
    assert job.get("env") == {"NX_TEST_T2_SUBSTRATE": "none"} == conformance.get("env")
    # At least twice the measured runtime (673 s of pytest plus about 1.5 min of checkout and setup), and room
    # for the up-to-30-minute wait a CI leg makes for a hand run holding lab-run-win's slot.
    assert 25 <= job["timeout-minutes"] <= 40, job["timeout-minutes"]
    steps = job["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]
    assert names[0].startswith("actions/checkout@")
    fixup = "Checkout fixups (LF, symlinks materialised)"
    assert steps[names.index(fixup)]["run"] == next(s for s in conformance["steps"] if s.get("name") == fixup)["run"]
    run_steps = [s["run"] for s in steps if "pytest" in s.get("run", "")]
    assert len(run_steps) == 1, "non-vacuity: exactly one pytest step in the job"
    run = run_steps[0]
    for target in WINDOWS_XDIST_SET:
        assert target in run, target
    workers = re.search(r"\s-n (\d+)\s", run)
    assert workers and int(workers.group(1)) >= 2, "the git-guard tests run under xdist, with at least two workers"
    assert "--junitxml=windows-git-guard-junit.xml" in run and 'addopts=""' not in run
    [floor] = [s for s in steps if "check_junit_floor.py" in s.get("run", "")]
    assert floor.get("if") == "${{ !cancelled() }}", "a red pytest step still reports its floor"
    assert "windows-git-guard-junit.xml" in floor["run"]
    # About 90% of the 522 that pass natively, and none of them skips there.
    assert int(re.search(r"--min-passed (\d+)", floor["run"]).group(1)) >= 470
    assert int(re.search(r"--max-skipped (\d+)", floor["run"]).group(1)) == 0
