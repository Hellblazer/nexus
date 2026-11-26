# SPDX-License-Identifier: AGPL-3.0-or-later
"""The compaction checkpoint measures unlanded work against the project's integration branch (nexus-sby49.4, nexus-sby49.11).

It used to measure against ``origin/develop`` for every repository, a bug for any repo whose default branch has another name.
The base is the project's ``integration_branch`` when ``origin/<value>`` resolves; else what ``origin/HEAD`` points at; with
none set, ``origin/main`` or ``origin/master`` when one exists; with none of those the count is unknown (-1), never a guess.
The ``origin/develop`` guess is gone (RDR-259 Research 5): a project whose integration branch is ``develop`` says so.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.hooks.test_session_facts import FACTS


@pytest.fixture(scope="module")
def sf():
    spec = importlib.util.spec_from_file_location("session_facts_default_branch_under_test", FACTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def _no_origin_head(repo: Path) -> None:
    """Newer git sets ``origin/HEAD`` on a fetch; take it away so the fallback names are what answers."""
    subprocess.run(["git", "remote", "set-head", "origin", "--delete"], cwd=repo, capture_output=True)


def _origin(tmp_path: Path, branch: str) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", branch, str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", branch, cwd=seed)
    (seed / "f").write_text("1")
    _git("add", "f", cwd=seed)
    _git("commit", "-q", "-m", "one", cwd=seed)
    _git("push", "-q", str(origin), "HEAD:" + branch, cwd=seed)
    return origin


def _clone(tmp_path: Path, origin: Path) -> Path:
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


def _fetched(tmp_path: Path, origin: Path) -> Path:
    """A repository that fetched *origin* and has no ``origin/HEAD``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "work", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("fetch", "-q", "origin", cwd=repo)
    _no_origin_head(repo)
    return repo


def test_a_clone_measures_against_whatever_origin_head_names(sf, tmp_path: Path) -> None:
    clone = _clone(tmp_path, _origin(tmp_path, "trunk"))
    assert sf.default_branch_ref(str(clone), "git") == "origin/trunk"
    work = sf.worktree_work(str(clone), "", "git")
    assert work.unlanded == 0
    (clone / "g").write_text("2")
    _git("add", "g", cwd=clone)
    _git("commit", "-q", "-m", "two", cwd=clone)
    assert sf.worktree_work(str(clone), "", "git").unlanded == 1


@pytest.mark.parametrize("name", ["main", "master"])
def test_without_origin_head_the_usual_names_are_tried(sf, tmp_path: Path, name: str) -> None:
    repo = _fetched(tmp_path, _origin(tmp_path, name))
    assert subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo, capture_output=True).returncode != 0
    assert sf.default_branch_ref(str(repo), "git") == "origin/" + name


def test_origin_develop_is_no_longer_guessed(sf, tmp_path: Path) -> None:
    """A repository whose only remote branch is ``develop`` and that sets no integration branch counts as unknown."""
    repo = _fetched(tmp_path, _origin(tmp_path, "develop"))
    assert sf.default_branch_ref(str(repo), "git") == ""


def test_a_repository_with_no_default_branch_it_can_name_counts_unlanded_as_unknown(sf, tmp_path: Path) -> None:
    origin = _origin(tmp_path, "release")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "work", cwd=repo)
    (repo / "f").write_text("1")
    _git("add", "f", cwd=repo)
    _git("commit", "-q", "-m", "one", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("fetch", "-q", "origin", cwd=repo)
    _no_origin_head(repo)
    assert sf.default_branch_ref(str(repo), "git") == ""
    assert sf.worktree_work(str(repo), "", "git").unlanded == -1


def test_the_integration_branch_is_the_base_when_origin_has_it(sf, tmp_path: Path) -> None:
    """Scenario: ``integration_branch: develop``. The count is measured against ``origin/develop``, not ``origin/main``."""
    origin = _origin(tmp_path, "main")
    seed = tmp_path / "seed"
    _git("checkout", "-q", "-b", "develop", cwd=seed)
    (seed / "d").write_text("1")
    _git("add", "d", cwd=seed)
    _git("commit", "-q", "-m", "develop only", cwd=seed)
    _git("push", "-q", str(origin), "HEAD:develop", cwd=seed)
    clone = _clone(tmp_path, origin)
    assert sf.default_branch_ref(str(clone), "git") == "origin/main"
    assert sf.default_branch_ref(str(clone), "git", integration_branch="develop") == "origin/develop"
    _git("checkout", "-q", "-b", "feature", "origin/develop", cwd=clone)
    (clone / "g").write_text("2")
    _git("add", "g", cwd=clone)
    _git("commit", "-q", "-m", "mine", cwd=clone)
    assert sf.worktree_work(str(clone), "", "git", integration_branch="develop").unlanded == 1
    assert sf.worktree_work(str(clone), "", "git").unlanded == 2, "against origin/main both commits are unlanded"


def test_a_value_origin_does_not_have_falls_back_to_the_default_order_and_is_reported(sf, tmp_path: Path) -> None:
    clone = _clone(tmp_path, _origin(tmp_path, "main"))
    seen: list[str] = []
    assert sf.default_branch_ref(str(clone), "git", integration_branch="nope", unresolved=seen.append) == "origin/main"
    assert seen == ["nope"]


@pytest.mark.parametrize("value", ["--output=x", "-q", "a..b", "with space"])
def test_a_value_that_reads_as_an_option_or_a_range_is_never_a_ref(sf, tmp_path: Path, value: str) -> None:
    """The value goes to git after ``--end-of-options``, so even one the validation missed cannot be read as an option."""
    clone = _clone(tmp_path, _origin(tmp_path, "main"))
    seen: list[str] = []
    assert sf.default_branch_ref(str(clone), "git", integration_branch=value, unresolved=seen.append) == "origin/main"
    assert seen == [value]
    assert not (clone / "x").exists()


def test_an_unresolved_report_that_raises_does_not_stop_the_measure(sf, tmp_path: Path) -> None:
    clone = _clone(tmp_path, _origin(tmp_path, "main"))

    def boom(_value: str) -> None:
        raise RuntimeError("the log is gone")

    assert sf.default_branch_ref(str(clone), "git", integration_branch="nope", unresolved=boom) == "origin/main"


def test_the_checkpoint_text_does_not_name_origin_develop(sf) -> None:
    text = Path(sf.__file__).read_text(encoding="utf-8")
    assert "not on origin/develop" not in text
    assert "not on the default branch" in text
    assert '"origin/develop"' not in text, "the origin/develop guess is removed (RDR-259 Research 5)"
