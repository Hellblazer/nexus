# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci wait --latest-attempt``: follow a re-run workflow to its latest attempt.

By default the wait returns the newest ``--done`` row for the sha already on the topic. After a
re-run starts, that is still the earlier attempt's row, so the wait reports the attempt the re-run
replaces. With ``--latest-attempt`` a row is accepted only when GitHub's push runs for the sha show
no newer attempt of any run in it. The property the review gate set: the mode never reports a row
green while a newer attempt of any run in it is pending or red, whatever GitHub's reads do.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus import ci_wait
from nexus.commands import ci_cmd
from nexus.commands.ci_cmd import ci_group


@pytest.fixture(autouse=True)
def _origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nx ci wait` reads the repo from the origin remote and has no default repo: these tests are nexus's."""
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "git@github.com:Hellblazer/nexus.git")
from tests.test_ci_wait import SHA, _Board, _Clock

SUBSPACE = ci_wait.done_subspace("nexus", "develop")
R = ci_wait.Run


class _GitHub:
    """GitHub's push runs for the sha, as a function of the fake clock: ``timeline`` maps a time to the
    runs listed from then on (None: the read fails)."""

    def __init__(self, clock: _Clock, timeline: dict[float, list[ci_wait.Run] | None]) -> None:
        self.clock, self.timeline, self.reads = clock, timeline, []

    def __call__(self, gh_repo: str, sha: str, branch: str) -> list[ci_wait.Run] | None:
        assert (gh_repo, sha, branch) == ("Hellblazer/nexus", SHA, "develop"), "read by the row's full sha"
        self.reads.append(self.clock.now)
        return self.timeline[max(t for t in self.timeline if t <= self.clock.now)]


def _sleeper(clock: _Clock):
    def sleep(seconds: float) -> None:
        assert seconds > 0, "a sleep that does not advance the clock would spin"
        clock.now += seconds
    return sleep


def _wait(board: _Board, clock: _Clock, github: _GitHub, timeout_s: float = 600) -> ci_wait.LatestAttempt:
    return ci_wait.wait_latest_attempt(board, SUBSPACE, SHA[:10], "Hellblazer/nexus", "develop",
                                       timeout_s=timeout_s, clock=clock, sleep=_sleeper(clock), runs=github)


def _done(runs: list[list[object]], n: int | None = None) -> ci_wait.DoneRow:
    body: dict[str, object] = {"state": "concluded", "sha": SHA, "runs": runs, "url": "u"}
    if n is not None:
        body["n"] = n
    from tests.test_ci_wait import _row  # noqa: PLC0415 — the shared row builder
    import json  # noqa: PLC0415

    row = ci_wait.parse_done_row(_row(json.dumps(body)))
    assert row is not None
    return row


class TestBehindLatest:
    def test_any_run_behind_counts_not_only_the_first_or_the_last(self) -> None:
        row = _done([[10, 1, "CI", "success"], [11, 1, "Docs", "success"], [12, 1, "Service CI", "success"]])
        assert ci_wait.behind_latest(row, [R(10, 1, "CI", "success"), R(11, 2, "Docs", None), R(12, 1, "Service CI", "success")]) \
            == [R(11, 2, "Docs", None)], "the middle run is behind"
        assert ci_wait.behind_latest(row, [R(10, 1, "CI", "success"), R(11, 1, "Docs", "success"), R(12, 3, "Service CI", "failure")]) \
            == [R(12, 3, "Service CI", "failure")], "the last run is behind"

    def test_a_run_github_does_not_list_is_taken_at_the_rows_attempt(self) -> None:
        row = _done([[10, 2, "CI", "success"]])
        assert ci_wait.behind_latest(row, []) == []
        assert ci_wait.behind_latest(row, [R(10, 2, "CI", "success")]) == []

    def test_a_capped_rows_dropped_run_on_a_later_attempt_is_behind(self) -> None:
        row = _done([[10, 1, "CI", "success"]], n=2)
        assert row.capped
        assert ci_wait.behind_latest(row, [R(10, 1, "CI", "success"), R(11, 2, "Service CI", None)]) == [R(11, 2, "Service CI", None)]
        assert ci_wait.behind_latest(row, [R(10, 1, "CI", "success"), R(11, 1, "Service CI", "success")]) == []


def test_a_rerun_in_progress_is_followed_to_its_own_row() -> None:
    """Attempt 1 failed and its row is on the topic; the re-run (attempt 2) is running and posts its
    green row later. The default wait returns the failure; the latest-attempt wait returns the green."""
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"]], url="https://example/attempt1")
    board.post(SHA, [[10, 2, "CI", "success"]], at=150, url="https://example/attempt2")
    github = _GitHub(clock, {0: [R(10, 2, "CI", None)], 150: [R(10, 2, "CI", "success")]})

    default = ci_wait.find_done_row(board, SUBSPACE, SHA[:10], timeout_s=600, clock=clock)
    assert default is not None and ci_wait.summarize(default)[1] == 1, "control: the default reads attempt 1"

    got = _wait(board, clock, github)
    assert got.row is not None
    assert ci_wait.summarize(got.row) == (f"{SHA[:12]} success: CI success (run 10 attempt 2) https://example/attempt2", 0)
    gaps = [b - a for a, b in zip(github.reads, github.reads[1:])]
    assert gaps and all(g >= ci_wait.GITHUB_POLL_S for g in gaps), "GitHub is read at most once a poll interval"


def test_two_runs_rerun_separately_wait_for_both() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"], [11, 1, "Service CI", "failure"]])
    board.post(SHA, [[10, 2, "CI", "success"], [11, 1, "Service CI", "failure"]], at=100)
    board.post(SHA, [[10, 2, "CI", "success"], [11, 2, "Service CI", "success"]], at=300)
    github = _GitHub(clock, {0: [R(10, 2, "CI", None), R(11, 1, "Service CI", "failure")],
                             100: [R(10, 2, "CI", "success"), R(11, 2, "Service CI", None)],
                             300: [R(10, 2, "CI", "success"), R(11, 2, "Service CI", "success")]})
    got = _wait(board, clock, github)
    assert got.row is not None and [(r.run_id, r.attempt) for r in got.row.runs] == [(10, 2), (11, 2)]
    assert ci_wait.summarize(got.row)[1] == 0


def test_a_row_at_the_latest_attempt_returns_at_once_with_one_github_read() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "success"], [11, 1, "Service CI", "success"]])
    github = _GitHub(clock, {0: [R(10, 1, "CI", "success"), R(11, 1, "Service CI", "success")]})
    got = _wait(board, clock, github)
    assert got.row is not None and ci_wait.summarize(got.row)[1] == 0
    assert github.reads == [0] and clock.now == 0 and board.parks == []


def test_a_rerun_still_running_at_the_deadline_never_returns_the_old_row() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "success"]])
    github = _GitHub(clock, {0: [R(10, 2, "CI", None)]})
    got = _wait(board, clock, github, timeout_s=200)
    assert got.row is None and got.stale is not None and not got.settled
    assert got.listed == [R(10, 2, "CI", None)]


def test_a_failed_github_read_after_a_newer_attempt_was_seen_never_accepts_the_old_row() -> None:
    """Review F1/C1: one failed read used to return the attempt-1 row; it is retried instead."""
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "success"]])
    github = _GitHub(clock, {0: [R(10, 2, "CI", None)], 60: None})
    got = _wait(board, clock, github, timeout_s=300)
    assert got.row is None and got.stale is not None
    assert len(github.reads) > 2, "the failed read is retried at the poll interval"


def test_a_failed_first_read_is_retried_and_a_later_read_decides() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "success"]])
    board.post(SHA, [[10, 2, "CI", "failure"]], at=200)
    github = _GitHub(clock, {0: None, 60: [R(10, 2, "CI", None)], 200: [R(10, 2, "CI", "failure")]})
    got = _wait(board, clock, github)
    assert got.row is not None and ci_wait.summarize(got.row)[1] == 1, "attempt 2's red, not attempt 1's green"


def test_github_never_readable_returns_the_row_unchecked() -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "success"]])
    github = _GitHub(clock, {0: None})
    got = _wait(board, clock, github, timeout_s=200)
    assert got.row is not None and got.unchecked and got.listed is None
    assert len(github.reads) >= 3, "retried until the deadline, not given up on the first failure"


def test_a_dropped_row_for_the_latest_attempt_settles_after_two_finished_reads() -> None:
    """Critic C3: GitHub shows the re-run finished but its board row never comes; two consecutive
    finished reads settle it, long before a 60-minute timeout."""
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"]])
    github = _GitHub(clock, {0: [R(10, 2, "CI", "success")]})
    got = _wait(board, clock, github, timeout_s=3600)
    assert got.row is None and got.settled and got.listed == [R(10, 2, "CI", "success")]
    assert ci_wait.GITHUB_POLL_S <= clock.now <= 2 * ci_wait.GITHUB_POLL_S, "settles on the second finished read, not the first"
    assert len(github.reads) == 2


def test_a_lagging_read_does_not_bring_back_the_replaced_row() -> None:
    """Confirmation O1: a read that shows attempt 2, then one that lags back to attempt 1, must not
    accept the attempt-1 row: the highest attempt seen per run is kept."""
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "success"]])
    github = _GitHub(clock, {0: [R(10, 2, "CI", None)], 60: [R(10, 1, "CI", "success")]})
    got = _wait(board, clock, github, timeout_s=300)
    assert got.row is None and got.stale is not None


class TestCli:
    def _cli(self, monkeypatch, board: _Board, clock: _Clock, github: _GitHub, *args: str, sha: str = SHA):
        monkeypatch.setattr(ci_cmd, "_store", lambda: board)
        monkeypatch.setattr(ci_cmd, "_now", clock)
        monkeypatch.setattr(ci_cmd, "_sleep", _sleeper(clock))
        monkeypatch.setattr(ci_wait, "gh_push_runs", github)
        return CliRunner().invoke(ci_group, ["wait", sha, "--latest-attempt", *args])

    def test_the_flag_follows_the_rerun_and_exits_on_its_result(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        board.post(SHA, [[10, 2, "CI", "success"]], at=150, url="https://example/attempt2")
        github = _GitHub(clock, {0: [R(10, 2, "CI", None)], 150: [R(10, 2, "CI", "success")]})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "10")
        assert result.exit_code == 0, result.output
        assert result.stdout.strip().splitlines()[-1] == f"{SHA[:12]} success: CI success (run 10 attempt 2) https://example/attempt2"

    @pytest.mark.parametrize("given", [SHA, SHA[:10]])
    def test_a_rerun_whose_row_never_comes_is_reported_from_github(self, monkeypatch, given: str) -> None:
        """A prefix works too: the full sha is the row's (review F3, critic C2)."""
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        github = _GitHub(clock, {0: [R(10, 2, "CI", None)], 100: [R(10, 2, "CI", "success")]})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "60", sha=given)
        assert result.exit_code == 0, result.output
        assert "no board row for the latest attempt: read from GitHub" in result.stdout and "attempt 2" in result.stdout

    def test_a_rerun_still_running_at_the_deadline_times_out_and_says_why(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "success"]])
        github = _GitHub(clock, {0: [R(10, 2, "CI", None)]})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "5", sha=SHA[:10])
        assert result.exit_code == 2, result.output
        line = result.stdout.strip().splitlines()[-1]
        assert "only a replaced attempt's row" in line and "1 of 1 runs still running" in line and "no row on" not in line

    def test_a_finished_listing_followed_by_failed_reads_is_never_reported_green(self, monkeypatch) -> None:
        """Confirmation B1: the last good listing showed the re-run green, every read since failed. The
        deadline must read GitHub fresh (here unreadable: a timeout), never report the old listing."""
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        github = _GitHub(clock, {0: [R(10, 2, "CI", "success")], 1: None})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "5")
        assert result.exit_code == 2, result.output
        line = result.stdout.strip().splitlines()[-1]
        assert "GitHub could not be read" in line and "only a replaced attempt's row" in line

    def test_a_red_newer_attempt_is_never_reported_green(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "success"]])
        github = _GitHub(clock, {0: [R(10, 2, "CI", "failure")]})
        result = self._cli(monkeypatch, board, clock, github, "--timeout", "60")
        assert result.exit_code == 1, result.output

    def test_github_never_readable_is_not_reported_as_success(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "success"]])
        result = self._cli(monkeypatch, board, clock, _GitHub(clock, {0: None}), "--timeout", "5")
        assert result.exit_code == 4, result.output
        assert "latest attempt not checked: GitHub could not be read; not reported as success" in result.stdout

    def test_github_never_readable_keeps_a_red_row_red(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        result = self._cli(monkeypatch, board, clock, _GitHub(clock, {0: None}), "--timeout", "5")
        assert result.exit_code == 1, result.output
        assert "latest attempt not checked" in result.stdout

    def test_fresh_and_latest_attempt_together_is_unusable_exit_5(self, monkeypatch) -> None:
        clock = _Clock()
        result = self._cli(monkeypatch, _Board(clock), clock, _GitHub(clock, {0: []}), "--fresh")
        assert result.exit_code == 5 and "--fresh" in result.output and "--latest-attempt" in result.output


def test_the_default_mode_is_unchanged_and_reads_no_github(monkeypatch) -> None:
    clock = _Clock()
    board = _Board(clock)
    board.post(SHA, [[10, 1, "CI", "failure"]])
    monkeypatch.setattr(ci_cmd, "_store", lambda: board)
    monkeypatch.setattr(ci_wait, "gh_push_runs", lambda *a: pytest.fail("read GitHub without --latest-attempt"))
    result = CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1"])
    assert result.exit_code == 1, result.output
    assert result.stdout.strip().splitlines()[-1] == f"{SHA[:12]} failure: CI failure (run 10) https://example/run"
