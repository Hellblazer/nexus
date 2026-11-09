# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1d6pz.11.4: ``nx ci wait <sha>``, a wake for a session launched
without the channel: the outcome reduction, the row parsing, the wait (a row
already there and the newer of two rerun rows, a row after several parks, a
timeout) and the CLI's line and exit code.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus import ci_wait
from nexus.commands import ci_cmd
from nexus.commands.ci_cmd import ci_group
from nexus.db.t2.records import TupleRow

SHA = "a0dc17ec284ffce6d4ff5264a626b7ef8e1a293a"
OTHER = "261600dab59f41455dd633b7d1e2a1ff7a4880de"


def _row(body: object, tid: str = "t1", dims: dict[str, str] | None = None) -> TupleRow:
    return TupleRow(
        id=tid, subspace="board/ci/x--done", template="board/ci/<topic>", keys={}, dims=dims or {},
        body=body if isinstance(body, str) else json.dumps(body), claim_state=None, claimant=None, lease_until=None, attempts=0,
        consumed_at=None, consumed_by=None, expires_at=None, created_at="2026-11-08T12:00:00Z",
    )


class TestReduction:
    @pytest.mark.parametrize(("conclusions", "outcome"), [
        (["success"], "success"),
        (["success", "neutral", "skipped"], "success"),
        (["success", "failure"], "failure"),
        (["cancelled", "timed_out"], "failure"),
        (["startup_failure"], "failure"),
        (["action_required", "success"], "failure"),
        (["success", "cancelled"], "cancelled"),
        (["stale"], "cancelled"),
        (["success", None], None),
        ([None, "failure"], "failure"),
        (["success", "something_new"], None),
    ])
    def test_reduce(self, conclusions, outcome) -> None:
        assert ci_wait.reduce_outcome(conclusions) == outcome

    @pytest.mark.parametrize(("outcome", "code"), [
        ("success", 0), ("failure", 1), ("cancelled", 3), (None, 4),
    ])
    def test_exit_codes(self, outcome, code) -> None:
        assert ci_wait.wait_exit_code(outcome) == code


class TestParse:
    def test_a_pre_11_1_row_has_unknown_conclusions(self) -> None:
        done = ci_wait.parse_done_row(_row(
            {"state": "concluded", "sha": SHA, "runs": [[42, 1], [43, 2]], "url": "u"},
        ))
        assert done is not None
        assert [(r.run_id, r.attempt, r.workflow, r.conclusion) for r in done.runs] == [
            (42, 1, None, None), (43, 2, None, None),
        ]

    def test_an_11_1_row_carries_workflow_and_conclusion(self) -> None:
        done = ci_wait.parse_done_row(_row(
            {"state": "concluded", "sha": SHA, "runs": [[42, 1, "CI", "success"]], "url": "u"},
        ))
        assert done is not None and done.runs[0].workflow == "CI" and done.runs[0].conclusion == "success"

    @pytest.mark.parametrize("body", [
        "not json", {"runs": []}, {"sha": SHA, "runs": [[1]]}, {"sha": SHA, "runs": [[None, 1]]},
        {"sha": SHA, "runs": [["x", 1]]}, {"sha": SHA, "runs": 5}, {"sha": SHA, "runs": [[1, None]]},
        {"sha": SHA, "runs": [[True, 1]]},
    ])
    def test_a_body_that_is_not_a_done_body_is_none(self, body) -> None:
        assert ci_wait.parse_done_row(_row(body)) is None

    def test_fill_reads_only_the_unknown_runs(self) -> None:
        done = ci_wait.parse_done_row(_row(
            {"sha": SHA, "runs": [[42, 1, "CI", "success"], [43, 1]], "url": "u"},
        ))
        assert done is not None
        asked: list[int] = []

        def fetch(repo: str, run_id: int, attempt: int) -> tuple[str | None, str | None]:
            asked.append(run_id)
            assert attempt == 1
            assert repo == "Hellblazer/nexus"
            return "Service CI", "failure"

        filled = ci_wait.fill_conclusions(done, "Hellblazer/nexus", fetch)
        assert asked == [43]
        line, code = ci_wait.summarize(filled)
        assert code == 1
        assert line == f"{SHA[:12]} failure: CI success (run 42), Service CI failure (run 43) u"


# ── the wait, over an in-memory reader with a virtual clock ─────────────────
#
# The reader keeps the engine's read contract for what find_done_row uses:
# rows in (created_at, id) order, `since` exclusive, and a park that returns
# as soon as a row past the cursor is visible, else after timeout_s. Rows
# become visible at a virtual time, so a wait over several parks runs with no
# sleep (tests/AGENTS.md: fixed clocks). The park itself is the engine's and
# is covered against the real engine in tests/test_mcp_tuple_tools.py and
# tests/test_tuple_wait.py; this keeps the four cases off the tenant ceiling.


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Board:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.rows: list[tuple[float, TupleRow]] = []
        self.parks: list[int] = []

    def post(
        self, sha: str, runs: list[list[object]], *, at: float = 0.0, url: str = "https://example/run",
        dims: dict[str, str] | None = None, body: object = None,
    ) -> str:
        seq = len(self.rows) + 1
        tid = f"{seq:064x}"
        row = _row(body if body is not None else {"state": "concluded", "sha": sha, "runs": runs, "url": url},
                   tid, dims)
        row = TupleRow(**{**{f: getattr(row, f) for f in row.__dataclass_fields__},
                          "created_at": f"2026-11-08T12:00:{seq:02d}Z"})
        self.rows.append((at, row))
        return tid

    def _visible(self, since: tuple[str, str] | None) -> list[TupleRow]:
        out = [r for at, r in self.rows if at <= self.clock.now]
        if since is not None:
            out = [r for r in out if (r.created_at, r.id) > since]
        return out

    def rd(self, subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0):
        assert subspace == "board/ci/nexus-develop--done"
        rows = self._visible(since)
        if not rows and timeout_s:
            self.parks.append(timeout_s)
            due = [at for at, r in self.rows if at > self.clock.now]
            if due and min(due) <= self.clock.now + timeout_s:
                self.clock.now = min(due)
            else:
                self.clock.now += timeout_s
            rows = self._visible(since)
        return rows[:n]


class TestFindDoneRow:
    def _setup(self) -> tuple[_Clock, _Board]:
        clock = _Clock()
        return clock, _Board(clock)

    def test_a_row_already_there_returns_without_a_park_and_a_rerun_row_wins(self) -> None:
        clock, board = self._setup()
        board.post(OTHER, [[1, 1, "CI", "failure"]])
        board.post(SHA, [[10, 1, "CI", "failure"]], url="https://example/attempt1")
        board.post(SHA, [[10, 2, "CI", "success"]], url="https://example/attempt2")
        row = ci_wait.find_done_row(board, ci_wait.done_subspace("nexus", "develop"), SHA[:10],
                                    timeout_s=60, clock=clock)
        assert row is not None and board.parks == []
        assert ci_wait.summarize(row) == (f"{SHA[:12]} success: CI success (run 10 attempt 2) https://example/attempt2", 0)

    def test_a_row_that_arrives_after_several_parks_ends_the_wait(self) -> None:
        clock, board = self._setup()
        board.post(OTHER, [[1, 1, "CI", "success"]])
        board.post(OTHER, [[2, 1, "CI", "success"]], at=30)  # a peer's sha arrives first
        board.post(SHA, [[20, 1, "CI", "success"], [21, 1, "Service CI", "cancelled"]], at=80)
        row = ci_wait.find_done_row(board, ci_wait.done_subspace("nexus", "develop"), SHA,
                                    timeout_s=600, clock=clock)
        assert row is not None and clock.now == 80
        assert len(board.parks) >= 3, board.parks
        assert ci_wait.summarize(row) == (
            f"{SHA[:12]} cancelled: CI success (run 20), Service CI cancelled (run 21) https://example/run", 3,
        )

    def test_no_row_before_the_timeout_is_none(self) -> None:
        clock, board = self._setup()
        board.post(OTHER, [[1, 1, "CI", "success"]])
        board.post(SHA, [[1, 1, "CI", "success"]], at=200)
        assert ci_wait.find_done_row(board, ci_wait.done_subspace("nexus", "develop"), SHA,
                                     timeout_s=60, clock=clock) is None
        assert 60 <= clock.now <= 60 + ci_wait.PARK_CAP_S
        assert all(p <= ci_wait.PARK_CAP_S for p in board.parks)


    def test_fresh_ignores_the_earlier_attempt_already_on_the_topic(self) -> None:
        """A wait started on a rerun: attempt 1's failed row is still there."""
        clock, board = self._setup()
        board.post(SHA, [[10, 1, "CI", "failure"]], url="https://example/attempt1")
        board.post(SHA, [[10, 2, "CI", "success"]], at=80, url="https://example/attempt2")
        sub = ci_wait.done_subspace("nexus", "develop")
        stale = ci_wait.find_done_row(board, sub, SHA, timeout_s=600, clock=_Clock())
        assert stale is not None and stale.url == "https://example/attempt1"  # the default takes it
        row = ci_wait.find_done_row(board, sub, SHA, timeout_s=600, fresh=True, clock=clock)
        assert row is not None and row.url == "https://example/attempt2" and clock.now == 80

    def test_a_malformed_row_for_any_sha_is_skipped(self) -> None:
        clock, board = self._setup()
        board.post(OTHER, [], body={"sha": OTHER, "runs": [["x", 1]]})
        board.post(SHA, [], body={"sha": SHA, "runs": 5})
        board.post(SHA, [[10, 1, "CI", "success"]], at=40)
        row = ci_wait.find_done_row(board, ci_wait.done_subspace("nexus", "develop"), SHA,
                                    timeout_s=600, clock=clock)
        assert row is not None and row.runs[0].run_id == 10

    def test_a_read_that_fails_is_retried_and_a_board_down_to_the_deadline_is_unreadable(self) -> None:
        clock, board = self._setup()
        board.post(SHA, [[10, 1, "CI", "success"]])
        fails = {"left": 2}
        real_rd = board.rd

        def flaky(*a, **k):
            if fails["left"]:
                fails["left"] -= 1
                raise ConnectionError("engine restarting")
            return real_rd(*a, **k)

        board.rd = flaky  # type: ignore[method-assign]

        def sleep(seconds: float) -> None:
            clock.now += seconds

        sub = ci_wait.done_subspace("nexus", "develop")
        row = ci_wait.find_done_row(board, sub, SHA, timeout_s=60, clock=clock, sleep=sleep)
        assert row is not None and clock.now == 10  # two retries of 5 s

        down = _Board(clock)
        down.rd = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))  # type: ignore[method-assign]
        with pytest.raises(ci_wait.BoardUnreadableError, match="down"):
            ci_wait.find_done_row(down, sub, SHA, timeout_s=30, clock=clock, sleep=sleep)

    def test_an_outcome_dim_that_disagrees_with_the_conclusions_exits_4(self) -> None:
        board = _Board(_Clock())
        board.post(SHA, [[10, 1, "CI", "failure"]], dims={"outcome": "success"})
        row = ci_wait.find_done_row(board, ci_wait.done_subspace("nexus", "develop"), SHA,
                                    timeout_s=1, clock=board.clock)
        assert row is not None
        line, code = ci_wait.summarize(row)
        assert code == 4 and "outcome mismatch: the board says success" in line


class TestCli:
    def test_a_found_row_prints_one_line_and_exits_on_its_outcome(self, monkeypatch) -> None:
        clock = _Clock()
        board = _Board(clock)
        board.post(SHA, [[10, 1, "CI", "failure"]])
        monkeypatch.setattr(ci_cmd, "_store", lambda: board)
        result = CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1"])
        assert result.exit_code == 1, result.output
        assert result.stdout.strip().splitlines()[-1] == f"{SHA[:12]} failure: CI failure (run 10) https://example/run"

    def test_a_pre_11_1_row_reads_its_conclusions_once(self, monkeypatch) -> None:
        board = _Board(_Clock())
        board.post(SHA, [[30, 1], [31, 2]])
        monkeypatch.setattr(ci_cmd, "_store", lambda: board)
        asked: list[tuple[str, int]] = []

        def fake_gh(gh_repo: str, run_id: int, attempt: int) -> tuple[str | None, str | None]:
            asked.append((gh_repo, run_id, attempt))
            return ("CI" if run_id == 30 else "Service CI"), "success"

        monkeypatch.setattr(ci_wait, "gh_run_conclusion", fake_gh)
        result = CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1"])
        assert result.exit_code == 0, result.output
        assert asked == [("Hellblazer/nexus", 30, 1), ("Hellblazer/nexus", 31, 2)]

    def test_an_unreadable_conclusion_exits_4(self, monkeypatch) -> None:
        board = _Board(_Clock())
        board.post(SHA, [[30, 1]])
        monkeypatch.setattr(ci_cmd, "_store", lambda: board)
        monkeypatch.setattr(ci_wait, "gh_run_conclusion", lambda repo, run_id, attempt: (None, None))
        result = CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1"])
        assert result.exit_code == 4, result.output
        assert "unknown" in result.stdout


    def test_no_store_exits_5_not_1(self, monkeypatch) -> None:
        def boom():
            raise RuntimeError("no engine configured")

        monkeypatch.setattr(ci_cmd, "_store", boom)
        result = CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1"])
        assert result.exit_code == 5, result.output
        assert "unusable: no tuple store" in result.stdout

    @pytest.mark.parametrize(("runs", "code", "needle"), [
        (None, 2, "GitHub could not be read"),
        ([], 2, "GitHub lists no push runs"),
        ([ci_wait.Run(1, 1, "CI", "success"), ci_wait.Run(2, 1, "Service CI", None)], 2, "1 of 2 runs still running"),
        ([ci_wait.Run(1, 1, "CI", "success"), ci_wait.Run(2, 1, "Service CI", "failure")], 1,
         "the wake was dropped; read from GitHub"),
    ])
    def test_at_the_deadline_github_is_read_once(self, monkeypatch, runs, code, needle) -> None:
        board = _Board(_Clock())
        monkeypatch.setattr(ci_cmd, "_store", lambda: board)
        asked: list[str] = []

        def fake_runs(gh_repo: str, sha: str):
            asked.append(sha)
            return runs

        monkeypatch.setattr(ci_wait, "gh_push_runs", fake_runs)
        result = CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "0"])
        assert result.exit_code == code, result.output
        assert asked == [SHA]
        assert result.stdout.strip().splitlines()[-1].startswith(f"{SHA[:12]} ")
        assert needle in result.stdout

    def test_a_prefix_times_out_without_reading_github(self, monkeypatch) -> None:
        board = _Board(_Clock())
        monkeypatch.setattr(ci_cmd, "_store", lambda: board)
        monkeypatch.setattr(ci_wait, "gh_push_runs", lambda *a: pytest.fail("read GitHub for a prefix"))
        result = CliRunner().invoke(ci_group, ["wait", SHA[:10], "--timeout", "0"])
        assert result.exit_code == 2 and "give the full sha" in result.stdout


def test_a_sha_that_is_not_hex_exits_5_not_the_timeout_code() -> None:
    result = CliRunner().invoke(ci_group, ["wait", "develop"])
    assert result.exit_code == 5 and "7 to 40 hex digits" in result.output
