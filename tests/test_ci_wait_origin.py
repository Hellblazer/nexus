# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci wait`` has no default repo (Sam, T2 nexus/sam-ruling-generalize-252-2026-11-22): without ``--repo`` it reads
the repo from the ``origin`` remote, and refuses, naming the flag, when it cannot. It used to default to ``nexus`` and
``Hellblazer/<repo>``, so a wait run in another repo read nexus's board.
"""
from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from nexus import ci_wait
from nexus.commands import ci_cmd
from nexus.commands.ci_cmd import ci_group
from tests.test_ci_wait import SHA, _row


class _Store:
    """A board that records the subspaces read and answers one pre-11.1 done row for SHA (conclusions unread, so the
    wait asks GitHub for them: the repo it asks about is observable)."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def rd(self, subspace: str, keys_pattern: Any = None, *, n: int = 1, since: Any = None, timeout_s: int = 0) -> list[Any]:
        self.reads.append(subspace)
        row = _row({"state": "concluded", "sha": SHA, "runs": [[30, 1]], "url": "https://example/run"}, "a" * 64)
        return [row] if since is None else []


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> _Store:
    store = _Store()
    monkeypatch.setattr(ci_cmd, "_store", lambda: store)
    return store


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def fake(gh_repo: str, run_id: int, attempt: int) -> tuple[str | None, str | None]:
        asked.append(gh_repo)
        return "CI", "success"

    monkeypatch.setattr(ci_wait, "gh_run_conclusion", fake)
    return asked


def _wait(*args: str) -> Any:
    return CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1", *args])


def test_with_no_repo_flag_the_board_and_the_github_repo_come_from_the_origin_remote(
    monkeypatch: pytest.MonkeyPatch, board: _Store, github: list[str],
) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "git@github.com:acme/halcyon.git")
    result = _wait()
    assert result.exit_code == 0, result.output
    assert set(board.reads) == {"board/ci/halcyon-develop--done"}, "the repo is the origin's, never nexus"
    assert set(github) == {"acme/halcyon"}, "and so is the GitHub owner: nothing is Hellblazer by default"


def test_with_no_origin_the_wait_refuses_naming_repo_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, board: _Store, github: list[str],
) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "")
    result = _wait()
    assert result.exit_code == ci_wait.EXIT_UNUSABLE, result.output
    assert "--repo" in result.output and "origin" in result.output
    assert board.reads == [] and github == []


def test_a_repo_flag_that_is_not_the_origins_needs_gh_repo_for_the_github_owner(
    monkeypatch: pytest.MonkeyPatch, board: _Store, github: list[str],
) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "git@github.com:acme/halcyon.git")
    refused = _wait("--repo", "other")
    assert refused.exit_code == ci_wait.EXIT_UNUSABLE and "--gh-repo" in refused.output
    assert board.reads == []
    ok = _wait("--repo", "other", "--gh-repo", "acme/other")
    assert ok.exit_code == 0, ok.output
    assert set(board.reads) == {"board/ci/other-develop--done"} and set(github) == {"acme/other"}


def test_a_repo_flag_with_no_origin_still_needs_gh_repo(monkeypatch: pytest.MonkeyPatch, board: _Store, github: list[str]) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "")
    assert _wait("--repo", "x").exit_code == ci_wait.EXIT_UNUSABLE
    assert _wait("--repo", "x", "--gh-repo", "o/x").exit_code == 0
    assert set(github) == {"o/x"}


def test_no_wait_default_names_a_repo_or_an_owner() -> None:
    from pathlib import Path  # noqa: PLC0415

    src = (Path(__file__).resolve().parent.parent / "src" / "nexus" / "commands" / "ci_cmd.py").read_text(encoding="utf-8")
    assert "Hellblazer" not in src and 'default="nexus"' not in src
