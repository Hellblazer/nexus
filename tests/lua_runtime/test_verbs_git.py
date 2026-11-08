# SPDX-License-Identifier: AGPL-3.0-or-later
"""git.diff_names and git.diff_stat against a real repository (RDR-242
Technical Design 10, Test Plan: Verbs; nexus-mdbds.8)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from nexus.lua_runtime import verb_types, verbs_git
from nexus.lua_runtime.verbs import VERB_TABLE
from nexus.lua_runtime.verb_types import ParamSpec, VerbContext, VerbError, VerbTimeout


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
    # The process's own directory never matters: the verbs read ctx.repo_root.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return r


def _ctx(repo: Path) -> VerbContext:
    return VerbContext(repo_root=repo)


def _deadline() -> float:
    return time.monotonic() + 30


def test_diff_names_lists_every_path_the_branch_changed(repo: Path) -> None:
    assert verbs_git.diff_names("base", deadline=_deadline(), ctx=_ctx(repo)) == [
        "a.txt",
        "b.txt",
        "bin.dat",
        "sub/c d.txt",
    ]


def test_diff_stat_counts_lines_per_file_and_totals(repo: Path) -> None:
    stat = verbs_git.diff_stat("base", deadline=_deadline(), ctx=_ctx(repo))
    assert stat["files"] == [
        {"path": "a.txt", "added": 2, "deleted": 1, "binary": False},
        {"path": "b.txt", "added": 0, "deleted": 1, "binary": False},
        {"path": "bin.dat", "added": None, "deleted": None, "binary": True},
        {"path": "sub/c d.txt", "added": 2, "deleted": 0, "binary": False},
    ]
    assert stat["added"] == 4
    assert stat["deleted"] == 2


def test_a_rename_shows_as_its_old_and_new_paths(repo: Path) -> None:
    _git(repo, "mv", "a.txt", "moved.txt")
    _git(repo, "commit", "-q", "-m", "move")
    _git(repo, "tag", "before-move", "HEAD~1")
    ctx = _ctx(repo)
    assert verbs_git.diff_names("before-move", deadline=_deadline(), ctx=ctx) == [
        "a.txt",
        "moved.txt",
    ]
    stat = verbs_git.diff_stat("before-move", deadline=_deadline(), ctx=ctx)
    assert stat["files"] == [
        {"path": "a.txt", "added": 0, "deleted": 4, "binary": False},
        {"path": "moved.txt", "added": 4, "deleted": 0, "binary": False},
    ]


def test_paths_are_repo_relative_whatever_diff_relative_says(repo: Path) -> None:
    _git(repo, "config", "diff.relative", "true")
    names = verbs_git.diff_names("base", deadline=_deadline(), ctx=_ctx(repo / "sub"))
    assert "a.txt" in names


def test_a_path_with_a_newline_or_cr_comes_back_exactly(repo: Path) -> None:
    odd = "cr\rname.txt"
    try:
        (repo / odd).write_text("z\n", encoding="utf-8")
    except OSError:
        pytest.skip("this filesystem refuses a CR in a name")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "odd")
    assert odd in verbs_git.diff_names("base", deadline=_deadline(), ctx=_ctx(repo))


def test_diff_is_taken_from_the_merge_base(repo: Path) -> None:
    # A commit on base's side after the fork is not the branch's change.
    _git(repo, "checkout", "-q", "-b", "other", "base")
    (repo / "elsewhere.txt").write_text("e\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "other side")
    _git(repo, "checkout", "-q", "main")
    names = verbs_git.diff_names("other", deadline=_deadline(), ctx=_ctx(repo))
    assert "elsewhere.txt" not in names


@pytest.mark.parametrize("verb", [verbs_git.diff_names, verbs_git.diff_stat])
def test_a_base_that_is_not_a_commit_is_a_verb_error(repo: Path, verb) -> None:
    with pytest.raises(VerbError, match="no-such-ref"):
        verb("no-such-ref", deadline=_deadline(), ctx=_ctx(repo))


# Two layers refuse these (the leading-dash check and --end-of-options), and
# either alone passes this test: no ref name starts with "-", so no input can
# reach one layer without the other.
@pytest.mark.parametrize("base", ["--output={out}", "-p", ""])
@pytest.mark.parametrize("verb", [verbs_git.diff_names, verbs_git.diff_stat])
def test_a_base_git_would_read_as_an_option_is_refused(
    repo: Path, verb, base: str, tmp_path: Path
) -> None:
    out = tmp_path / "written-by-git"
    with pytest.raises(VerbError):
        verb(base.format(out=out), deadline=_deadline(), ctx=_ctx(repo))
    assert not out.exists()


@pytest.mark.parametrize("verb", [verbs_git.diff_names, verbs_git.diff_stat])
def test_a_run_outside_a_repository_is_a_verb_error(verb) -> None:
    with pytest.raises(VerbError, match="not started inside a git repository"):
        verb("HEAD", deadline=_deadline(), ctx=VerbContext(repo_root=None))


def test_a_repository_with_no_commits_is_a_verb_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    with pytest.raises(VerbError, match="does not name a commit"):
        verbs_git.diff_names("HEAD", deadline=_deadline(), ctx=_ctx(empty))


@pytest.mark.parametrize("verb", [verbs_git.diff_names, verbs_git.diff_stat])
def test_a_passed_deadline_is_a_verb_timeout(repo: Path, verb) -> None:
    with pytest.raises(VerbTimeout):
        verb("base", deadline=time.monotonic() - 1, ctx=_ctx(repo))


def test_the_git_subprocess_gets_the_remaining_budget_and_the_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[float, object]] = []
    real = subprocess.run

    def spy(*a, **k):
        seen.append((k["timeout"], k["cwd"]))
        return real(*a, **k)

    monkeypatch.setattr(verbs_git.subprocess, "run", spy)
    verbs_git.diff_names("base", deadline=time.monotonic() + 7, ctx=_ctx(repo))
    assert seen and all(0 < t <= 7 and cwd == repo for t, cwd in seen)


def test_a_git_that_outlasts_the_budget_is_a_verb_timeout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k["timeout"])

    monkeypatch.setattr(verbs_git.subprocess, "run", slow)
    with pytest.raises(VerbTimeout):
        verbs_git.diff_stat("base", deadline=_deadline(), ctx=_ctx(repo))


def test_a_result_over_the_verb_result_cap_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verb_types, "VERB_RESULT_CAP_BYTES", 10)
    with pytest.raises(VerbError, match="cap"):
        verbs_git.diff_names("base", deadline=_deadline(), ctx=_ctx(repo))


def test_both_verbs_are_registered_with_one_string_parameter() -> None:
    by_name = {v.name: v for v in verbs_git.VERBS}
    assert set(by_name) == {"git.diff_names", "git.diff_stat"}
    for spec in by_name.values():
        assert spec.params == (ParamSpec("base", "string"),)
    assert by_name["git.diff_names"].fn is verbs_git.diff_names
    assert by_name["git.diff_stat"].fn is verbs_git.diff_stat


def test_the_verb_table_loads_both_verbs() -> None:
    assert {"git.diff_names", "git.diff_stat"} <= set(VERB_TABLE)


def test_an_inherited_git_dir_does_not_redirect_the_verb(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "t")
    (other / "theirs.txt").write_text("t\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "x")
    _git(other, "tag", "base")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    names = verbs_git.diff_names("base", deadline=_deadline(), ctx=_ctx(repo))
    assert "a.txt" in names
    assert "theirs.txt" not in names


def test_a_repo_root_that_is_not_a_repository_says_so(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(VerbError, match="not a git repository"):
        verbs_git.diff_names("HEAD", deadline=_deadline(), ctx=_ctx(plain))
