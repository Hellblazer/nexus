# SPDX-License-Identifier: AGPL-3.0-or-later
"""git.diff_names and git.diff_stat against a real repository (RDR-242
Technical Design 10, Test Plan: Verbs; nexus-mdbds.8)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from nexus.lua_runtime import verbs_git
from nexus.lua_runtime.verb_types import ParamSpec, VerbError, VerbTimeout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (r / "b.txt").write_text("gone\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "base")
    (r / "a.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
    (r / "b.txt").unlink()
    (r / "sub").mkdir()
    (r / "sub" / "c d.txt").write_text("x\ny\n", encoding="utf-8")
    (r / "bin.dat").write_bytes(b"\x00\x01\x02\x00")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "change")
    monkeypatch.chdir(r)
    return r


def _deadline() -> float:
    return time.monotonic() + 30


def test_diff_names_lists_every_path_the_branch_changed(repo: Path) -> None:
    assert verbs_git.diff_names("base", deadline=_deadline()) == [
        "a.txt",
        "b.txt",
        "bin.dat",
        "sub/c d.txt",
    ]


def test_diff_names_reads_from_a_subdirectory_of_the_checkout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo / "sub")
    assert "a.txt" in verbs_git.diff_names("base", deadline=_deadline())


def test_diff_stat_counts_lines_per_file_and_totals(repo: Path) -> None:
    stat = verbs_git.diff_stat("base", deadline=_deadline())
    assert stat["files"] == [
        {"path": "a.txt", "added": 2, "deleted": 1, "binary": False},
        {"path": "b.txt", "added": 0, "deleted": 1, "binary": False},
        {"path": "bin.dat", "added": None, "deleted": None, "binary": True},
        {"path": "sub/c d.txt", "added": 2, "deleted": 0, "binary": False},
    ]
    assert stat["added"] == 4
    assert stat["deleted"] == 2


def test_diff_is_taken_from_the_merge_base(repo: Path) -> None:
    # A commit on base's side after the fork is not the branch's change.
    _git(repo, "checkout", "-q", "-b", "other", "base")
    (repo / "elsewhere.txt").write_text("e\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "other side")
    _git(repo, "checkout", "-q", "main")
    assert "elsewhere.txt" not in verbs_git.diff_names("other", deadline=_deadline())


@pytest.mark.parametrize("verb", [verbs_git.diff_names, verbs_git.diff_stat])
def test_a_base_that_is_not_a_commit_is_a_verb_error(repo: Path, verb) -> None:
    with pytest.raises(VerbError, match="no-such-ref"):
        verb("no-such-ref", deadline=_deadline())


@pytest.mark.parametrize("base", ["--output={out}", "-p", ""])
@pytest.mark.parametrize("verb", [verbs_git.diff_names, verbs_git.diff_stat])
def test_a_base_git_would_read_as_an_option_is_refused(
    repo: Path, verb, base: str, tmp_path: Path
) -> None:
    out = tmp_path / "written-by-git"
    with pytest.raises(VerbError):
        verb(base.format(out=out), deadline=_deadline())
    assert not out.exists()


def test_outside_a_checkout_is_a_verb_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(VerbError, match="not a git"):
        verbs_git.diff_names("HEAD", deadline=_deadline())


@pytest.mark.parametrize("verb", [verbs_git.diff_names, verbs_git.diff_stat])
def test_a_passed_deadline_is_a_verb_timeout(repo: Path, verb) -> None:
    with pytest.raises(VerbTimeout):
        verb("base", deadline=time.monotonic() - 1)


def test_the_git_subprocess_gets_the_remaining_budget_as_its_timeout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float] = []
    real = subprocess.run

    def spy(*a, **k):
        seen.append(k["timeout"])
        return real(*a, **k)

    monkeypatch.setattr(verbs_git.subprocess, "run", spy)
    verbs_git.diff_names("base", deadline=time.monotonic() + 7)
    assert seen and all(0 < t <= 7 for t in seen)


def test_a_git_that_outlasts_the_budget_is_a_verb_timeout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k["timeout"])

    monkeypatch.setattr(verbs_git.subprocess, "run", slow)
    with pytest.raises(VerbTimeout):
        verbs_git.diff_stat("base", deadline=_deadline())


def test_both_verbs_are_registered_with_one_string_parameter() -> None:
    by_name = {v.name: v for v in verbs_git.VERBS}
    assert set(by_name) == {"git.diff_names", "git.diff_stat"}
    for spec in by_name.values():
        assert spec.params == (ParamSpec("base", "string"),)
    assert by_name["git.diff_names"].fn is verbs_git.diff_names
    assert by_name["git.diff_stat"].fn is verbs_git.diff_stat
