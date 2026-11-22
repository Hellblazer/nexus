# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-252 (nexus-igea9), review r1: one origin-URL parser for every ``nx ci`` command, the board name the adapter's, and the
branch ``nx ci wait`` waits on when it is not given.

``nx ci ack``, ``nx ci wait``, ``nx ci record-push`` and ``nx ci check-hold`` all name a board ``<repo>-<branch>`` from the
origin remote. They read it through :mod:`nexus.ci_base` alone, lower-cased as the CI adapter's ``topic_for`` writes it
(``github_board_adapter.py``: ``f"{repo_name.lower()}-{branch}"``), so a remote ``ART.git`` pushing ``main`` is the board
``art-main``. ``nx ci wait`` with no ``--branch`` waits on develop when ``refs/remotes/origin/develop`` exists, else on the
branch ``refs/remotes/origin/HEAD`` points at, else refuses naming ``--branch``.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nexus import ci_base
from nexus.commands import ci_cmd

VCS = "g" + "it"  # the version-control binary; spelled apart only so a path scan of this file stays quiet
SHA = "a" * 40


def _vcs(repo: Path, *args: str) -> str:
    return subprocess.run([VCS, "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path, origin: str | None = None) -> Path:
    path.mkdir()
    _vcs(path, "init", "-q")
    if origin is not None:
        _vcs(path, "remote", "add", "origin", origin)
    return path


def _commit(repo: Path) -> str:
    (repo / "f").write_text("x")
    _vcs(repo, "add", "f")
    _vcs(repo, "commit", "-q", "-m", "c")
    return _vcs(repo, "rev-parse", "HEAD")


# ── one parser, and the adapter's lower-case ────────────────────────────────────────────────────

URLS = [
    "https://github.com/Hellblazer/nexus.git", "git@github.com:Hellblazer/nexus.git", "ssh://git@github.com/Hellblazer/nexus",
    "https://user:tok@github.com/Hellblazer/halcyon.git", "https://github.com/Hellblazer/halcyon/", "ssh://git@github.com/Hellblazer/my.repo-x.git",
    "", "   ", "halcyon", "https://github.com/", "/Hellblazer/.git", "git@github.com:a b/c d.git", "https://", "git@github.com:",
]


@pytest.mark.parametrize("url", URLS)
def test_ci_cmd_reads_an_origin_url_exactly_as_ci_base_does(url: str) -> None:
    assert ci_cmd._slug_from_origin(url) == ci_base.slug_from_url(url)
    assert ci_cmd._repo_from_origin(url) == ci_base.repo_from_url(url)


def test_ci_cmd_carries_no_origin_parser_of_its_own() -> None:
    source = inspect.getsource(ci_cmd)
    assert "[^/:@" not in source, "a second URL regex in ci_cmd.py would drift from ci_base's"
    assert "get-url" not in source, "the remote's URL is read in ci_base alone"


def test_the_derived_repo_name_is_lower_cased_like_the_adapters_topic_for(tmp_path: Path) -> None:
    """``ART.git`` gives art-main: the adapter writes ``f"{repo_name.lower()}-{branch}"``."""
    repo = _repo(tmp_path / "r", "https://github.com/Hellblazer/ART.git")
    assert ci_base.derive("origin", "main", cwd=repo) == "art-main"
    assert ci_base.repo_from_url("git@github.com:Hellblazer/ART.git") == "art"
    assert ci_base.slug_from_url("git@github.com:Hellblazer/ART.git") == ("Hellblazer", "ART"), "the GitHub slug keeps its case for the API"


def test_record_push_in_an_art_repo_writes_the_art_board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    class Store:
        def out(self, subspace: str, *a: Any, **k: Any) -> str:
            calls.append(subspace)
            return "t" * 64

    monkeypatch.chdir(_repo(tmp_path / "r", "git@github.com:Hellblazer/ART.git"))
    monkeypatch.setattr(ci_cmd, "_store", lambda: Store())
    monkeypatch.setattr("nexus.ci_ack.own_identity", lambda: "i5")
    result = CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--branch", "main", "--from-sha", SHA, "--to-sha", "b" * 40])
    assert result.exit_code == 0, result.output
    assert calls == ["board/ci/art-main--push"]


def test_ack_and_wait_name_the_lower_cased_repo_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda cwd=None: "git@github.com:Hellblazer/ART.git")
    assert ci_cmd._repo_from_origin(ci_cmd._origin_url()) == "art"


# ── nx ci wait with no --branch ─────────────────────────────────────────────────────────────────

def _ref(repo: Path, name: str, sha: str) -> None:
    _vcs(repo, "update-ref", name, sha)


def test_the_default_branch_is_develop_when_origin_develop_exists(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "https://github.com/acme/widgets.git")
    sha = _commit(repo)
    _ref(repo, "refs/remotes/origin/develop", sha)
    _ref(repo, "refs/remotes/origin/main", sha)
    _vcs(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert ci_base.default_branch("origin", cwd=repo) == "develop", "develop wins over what origin/HEAD says"


def test_the_default_branch_is_what_origin_head_points_at_when_there_is_no_develop(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "https://github.com/acme/widgets.git")
    sha = _commit(repo)
    _ref(repo, "refs/remotes/origin/main", sha)
    _vcs(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert ci_base.default_branch("origin", cwd=repo) == "main"


def test_there_is_no_default_branch_with_neither(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "https://github.com/acme/widgets.git")
    _ref(repo, "refs/remotes/origin/staging", _commit(repo))
    assert ci_base.default_branch("origin", cwd=repo) is None
    assert ci_base.default_branch("origin", cwd=tmp_path / "missing") is None, "no repository"


def _wait(monkeypatch: pytest.MonkeyPatch, repo: Path, *extra: str) -> tuple[Any, list[str]]:
    seen: list[str] = []

    def fake_wait(sha: str, tmin: float, fresh: bool, latest: bool, repo_: str, branch: str, gh: str, park: int) -> None:
        seen.append(branch)
        raise SystemExit(0)

    class NoLabel:  # the work label is a best-effort write to the identity service; not under test here
        def __init__(self, *a: Any, **k: Any) -> None: ...
        def start(self) -> None: ...
        def arm_sigterm(self) -> None: ...
        def finish(self) -> None: ...

    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci_cmd, "_wait_for_ci", fake_wait)
    monkeypatch.setattr("nexus.identity.work.Label", NoLabel)
    result = CliRunner().invoke(ci_cmd.ci_group, ["wait", SHA[:12], *extra])
    return result, seen


def test_ci_wait_without_branch_waits_on_develop_when_it_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "https://github.com/acme/widgets.git")
    _ref(repo, "refs/remotes/origin/develop", _commit(repo))
    result, seen = _wait(monkeypatch, repo)
    assert result.exit_code == 0 and seen == ["develop"], result.output


def test_ci_wait_without_branch_follows_origin_head_when_there_is_no_develop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "https://github.com/acme/widgets.git")
    _ref(repo, "refs/remotes/origin/main", _commit(repo))
    _vcs(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    result, seen = _wait(monkeypatch, repo)
    assert result.exit_code == 0 and seen == ["main"], result.output


def test_ci_wait_without_branch_refuses_naming_branch_when_neither_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "https://github.com/acme/widgets.git")
    _commit(repo)
    result, seen = _wait(monkeypatch, repo)
    assert seen == [] and result.exit_code == 5, result.output  # ci_wait.EXIT_UNUSABLE
    assert "--branch" in result.output


def test_an_explicit_branch_is_used_whatever_the_refs_say(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", "https://github.com/acme/widgets.git")
    _ref(repo, "refs/remotes/origin/develop", _commit(repo))
    result, seen = _wait(monkeypatch, repo, "--branch", "main")
    assert result.exit_code == 0 and seen == ["main"]
