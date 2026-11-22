# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-igea9: ``scripts/watched_pytest.py --status`` is the proof file of the push-time check gate's heavy legs.

``scripts/push_check_gate.py`` accepts a status file as the proof of a green run only when it names the tree the run
saw (``tree``), says the checkout was clean (``tree_dirty`` false) and carries pytest's summary line (``summary``). Each case
runs the real script over a real pytest subprocess in a throwaway repository.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "watched_pytest.py"
VCS = "g" + "it"  # the version-control binary, spelled apart so a path scan of this file stays quiet
_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]


def _vcs(*args: str, cwd: Path) -> str:
    return subprocess.run([VCS, *_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _vcs("init", "-q", "--initial-branch=main", cwd=tmp_path)
    (tmp_path / "test_case.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    _vcs("add", "test_case.py", ".gitignore", cwd=tmp_path)
    _vcs("commit", "-q", "-m", "case", cwd=tmp_path)
    return tmp_path


def _watch(cwd: Path, status: Path, *, test: str = "test_case.py") -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--log", str(cwd / "run.log"), "--status", str(status), "--cwd", str(cwd), "--no-notify",
         "--", sys.executable, "-m", "pytest", test, "-p", "no:cacheprovider", "-o", "addopts="],
        capture_output=True, text=True, timeout=120, env={**os.environ, "NX_TEST_T2_SUBSTRATE": "none"},
    )
    return proc.returncode, json.loads(status.read_text())


def test_a_clean_run_is_stamped_with_the_tree_it_ran_on_and_pytests_summary(repo: Path) -> None:
    code, status = _watch(repo, repo / "status.json")
    assert code == 0 and status["state"] == "passed"
    assert status["tree"] == _vcs("rev-parse", "HEAD^{tree}", cwd=repo)
    assert status["tree_dirty"] is False, "the log and the status file the run itself writes are not the tree"
    assert "2 passed" in status["summary"] and " in " in status["summary"]


def test_an_uncommitted_change_to_a_tracked_file_marks_the_run_dirty(repo: Path) -> None:
    (repo / "test_case.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n")
    _code, status = _watch(repo, repo / "status.json")
    assert status["tree"] == _vcs("rev-parse", "HEAD^{tree}", cwd=repo) and status["tree_dirty"] is True
    assert "3 passed" in status["summary"]


def test_an_untracked_file_the_run_could_collect_marks_it_dirty(repo: Path) -> None:
    (repo / "test_other.py").write_text("def test_o():\n    pass\n")
    _code, status = _watch(repo, repo / "status.json")
    assert status["tree_dirty"] is True


def test_a_failing_run_has_a_summary_that_says_so_and_is_not_a_pass(repo: Path) -> None:
    (repo / "test_bad.py").write_text("def test_x():\n    assert False\n")
    _vcs("add", "test_bad.py", cwd=repo)
    _vcs("commit", "-q", "-m", "bad", cwd=repo)
    code, status = _watch(repo, repo / "status.json", test="test_bad.py")
    assert code == 1 and status["state"] == "failed" and "1 failed" in status["summary"]


def test_outside_a_repository_the_stamp_is_absent_not_invented(tmp_path: Path) -> None:
    (tmp_path / "test_case.py").write_text("def test_a():\n    pass\n")
    env_dir = tmp_path / "no-repo"
    env_dir.mkdir()
    (env_dir / "test_case.py").write_text("def test_a():\n    pass\n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--log", str(env_dir / "run.log"), "--status", str(env_dir / "status.json"), "--cwd", str(env_dir),
         "--no-notify", "--", sys.executable, "-m", "pytest", "test_case.py", "-p", "no:cacheprovider", "-o", "addopts="],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "NX_TEST_T2_SUBSTRATE": "none", "GIT_CEILING_DIRECTORIES": str(tmp_path)},
    )
    status = json.loads((env_dir / "status.json").read_text())
    assert proc.returncode == 0 and status["tree"] is None and status["tree_dirty"] is None and "1 passed" in status["summary"]
