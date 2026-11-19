# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci wait --latest-attempt``: follow a re-run workflow to its latest attempt.

By default the wait returns the newest ``--done`` row for the sha already on the topic. After a
re-run starts, that is still the earlier attempt's row, so the wait reports the attempt the re-run
replaces. With ``--latest-attempt`` a row is accepted only when GitHub's push runs for the sha show
no newer attempt of any run in it; otherwise the wait goes on for the newer row, reading GitHub at
most once per :data:`nexus.ci_wait.GITHUB_POLL_S`, and at the deadline it reports from GitHub (the
dropped-wake read) or times out.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus import ci_wait
from nexus.commands import ci_cmd
from nexus.commands.ci_cmd import ci_group
from tests.test_ci_wait import SHA, _Board, _Clock

SUBSPACE = ci_wait.done_subspace("nexus", "develop")


class _GitHub:
    """GitHub's push runs for the sha, as a function of the fake clock: ``timeline`` maps a time to
    the runs listed from then on."""

    def __init__(self, clock: _Clock, timeline: dict[float, list[ci_wait.Run] | None]) -> None:
        self.clock, self.timeline, self.reads = clock, timeline, []

    def __call__(self, gh_repo: str, sha: str, branch: str) -> list[ci_wait.Run] | None:
        assert (gh_repo, sha, branch) == ("Hellblazer/nexus", SHA, "develop")
        self.reads.append(self.clock.now)
        return self.timeline[max(t for t in self.timeline if t <= self.clock.now)]


def _sleeper(clock: _Clock):
    def sleep(seconds: float) -> None:
        clock.now += seconds
    return sleep


def _wait(board: _Board, clock: _Clock, github: _GitHub, timeout_s: float = 600):
    return ci_wait.wait_latest_attempt(board, SUBSPACE, SHA[:10], "Hellblazer/nexus", "develop",
                                       timeout_s=timeout_s, clock=clock, sleep=_sleeper(clock), runs=github)


def test_a_rerun_in_progress_is_followed_to_its_own_row() -> None:
    """Attempt 1 failed and its row is on the topic; the re-run (attempt 2) is running and posts its
    green row later. The default wait returns the failure; the latest-attempt wait returns the green."""
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"]], url="https://example/attempt1")
    board.post(SHA, [[10, 2, "CI", "success"]], at=150, url="https://example/attempt2")
    github = _GitHub(clock, {0: [ci_wait.Run(10, 2, "CI", None)], 150: [ci_wait.Run(10, 2, "CI", "success")]})

    default = ci_wait.find_done_row(board, SUBSPACE, SHA[:10], timeout_s=600, clock=clock)
    assert default is not None and ci_wait.summarize(default)[1] == 1, "control: the default reads attempt 1"

    row, _ = _wait(board, clock, github)
    assert row is not None
    assert ci_wait.summarize(row) == (f"{SHA[:12]} success: CI success (run 10 attempt 2) https://example/attempt2", 0)
    gaps = [b - a for a, b in zip(github.reads, github.reads[1:])]
    assert gaps and all(g >= ci_wait.GITHUB_POLL_S for g in gaps), "GitHub is read at most once a poll interval"


def test_a_row_at_the_latest_attempt_returns_at_once_with_one_github_read() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "success"], [11, 1, "Service CI", "success"]])
    github = _GitHub(clock, {0: [ci_wait.Run(10, 1, "CI", "success"), ci_wait.Run(11, 1, "Service CI", "success")]})
    row, _ = _wait(board, clock, github)
    assert row is not None and ci_wait.summarize(row)[1] == 0
    assert github.reads == [0] and clock.now == 0 and board.parks == []


def test_a_rerun_still_running_at_the_deadline_is_a_timeout_not_the_old_result() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"]])
    github = _GitHub(clock, {0: [ci_wait.Run(10, 2, "CI", None)]})
    row, listed = _wait(board, clock, github, timeout_s=200)
    assert row is None, "the attempt-1 row is never returned while attempt 2 exists"
    assert listed == [ci_wait.Run(10, 2, "CI", None)]


def test_github_that_cannot_be_read_leaves_the_row_standing() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"]])
    row, listed = _wait(board, clock, _GitHub(clock, {0: None}))
    assert row is not None and listed is None and ci_wait.summarize(row)[1] == 1


class TestCli:
    def _cli(self, monkeypatch, board: _Board, clock: _Clock, github: _GitHub, *args: str):
        monkeypatch.setattr(ci_cmd, "_store", lambda: board)
        monkeypatch.setattr(ci_cmd, "_now", clock)
        monkeypatch.setattr(ci_cmd, "_sleep", _sleeper(clock))
        monkeypatch.setattr(ci_wait, "gh_push_runs", github)
        return CliRunner().invoke(ci_group, ["wait", SHA, "--latest-attempt", *args])

    def test_the_flag_follows_the_rerun_and_exits_on_its_result(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        board.post(SHA, [[10, 2, "CI", "success"]], at=150, url="https://example/attempt2")
        github = _GitHub(clock, {0: [ci_wait.Run(10, 2, "CI", None)], 150: [ci_wait.Run(10, 2, "CI", "success")]})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "10")
        assert result.exit_code == 0, result.output
        assert result.stdout.strip().splitlines()[-1] == f"{SHA[:12]} success: CI success (run 10 attempt 2) https://example/attempt2"

    def test_a_rerun_whose_row_never_comes_is_read_from_github_at_the_deadline(self, monkeypatch) -> None:
        """The newer attempt finished but its board wake was dropped: the deadline read reports it."""
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        github = _GitHub(clock, {0: [ci_wait.Run(10, 2, "CI", None)], 100: [ci_wait.Run(10, 2, "CI", "success")]})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "5")
        assert result.exit_code == 0, result.output
        assert "the wake was dropped; read from GitHub" in result.stdout and "attempt 2" in result.stdout

    def test_a_rerun_still_running_at_the_deadline_times_out(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        github = _GitHub(clock, {0: [ci_wait.Run(10, 2, "CI", None)]})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "5")
        assert result.exit_code == 2, result.output
        assert "1 of 1 runs still running" in result.stdout

    def test_an_unreadable_github_says_the_attempt_was_not_checked(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        result = self._cli(monkeypatch, board, clock, _GitHub(clock, {0: None}), "--timeout", "5")
        assert result.exit_code == 1, result.output
        assert "latest attempt not checked: GitHub could not be read" in result.stdout

    def test_fresh_and_latest_attempt_together_is_a_usage_error(self, monkeypatch) -> None:
        clock = _Clock()
        result = self._cli(monkeypatch, _Board(clock), clock, _GitHub(clock, {0: []}), "--fresh")
        assert result.exit_code == 2 and "--fresh" in result.output and "--latest-attempt" in result.output


def test_the_default_mode_is_unchanged_and_reads_no_github(monkeypatch) -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"]])
    monkeypatch.setattr(ci_cmd, "_store", lambda: board)
    monkeypatch.setattr(ci_wait, "gh_push_runs", lambda *a: pytest.fail("read GitHub without --latest-attempt"))
    result = CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1"])
    assert result.exit_code == 1, result.output
