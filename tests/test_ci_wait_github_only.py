# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-x4jok: ``nx ci wait`` on a branch the CI board does not watch.

board/ci/ has topics for develop and main only; a release PR's CI runs on
pull_request events. The wait then reads the sha's check-runs from GitHub.
``gh`` is faked at the ``run_bounded`` seam ci_wait already uses; the recorded
output is what ``gh api --paginate --jq '.check_runs[] | [.id, .name, .status,
.conclusion]'`` prints, one array per line, for
``repos/Hellblazer/nexus/commits/<sha>/check-runs``. The clock and sleep are
injected, so no test waits.
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from nexus import ci_wait
from nexus.commands import ci_cmd
from nexus.commands.ci_cmd import ci_group


@pytest.fixture(autouse=True)
def _origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nx ci wait` reads the repo from the origin remote and has no default repo: these tests are nexus's."""
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "git@github.com:Hellblazer/nexus.git")
    monkeypatch.setattr(ci_cmd, "_default_branch", lambda: "develop")  # no --branch: the default is the repo's, not this checkout's refs
from tests.test_ci_wait import SHA, _Board, _Clock

RELEASE = "release/v8.3.0"


def _lines(*checks: tuple[int, str, str, str | None]) -> str:
    return "".join(json.dumps(list(c)) + "\n" for c in checks)


_RUNNING = _lines((1, "lint", "completed", "success"), (2, "pytest (shard 1)", "in_progress", None))
_TWO_DONE = _lines((1, "lint", "completed", "success"), (2, "pytest (shard 1)", "completed", "success"))
_GREEN = _lines((1, "lint", "completed", "success"), (2, "pytest (shard 1)", "completed", "success"),
                (3, "docs", "completed", "skipped"))
_RED = _lines((1, "lint", "completed", "success"), (2, "pytest (shard 1)", "completed", "failure"))


def _gh_error(stderr: str, rc: int = 1) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(rc, ["gh"], output="", stderr=stderr)


class _Gh:
    """``run_bounded`` stand-in: one recorded stdout (or error) per call; the last repeats."""

    def __init__(self, *outputs: str | BaseException) -> None:
        self.outputs = list(outputs)
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.timeouts.append(kwargs["timeout"])
        out = self.outputs[min(len(self.calls), len(self.outputs)) - 1]
        if isinstance(out, BaseException):
            raise out
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


class TestGithubOnly:
    @pytest.fixture
    def clock(self, monkeypatch) -> _Clock:
        """The CLI's GitHub-only wait driven by a fixed clock; sleeps advance it."""
        clock = _Clock()
        self.sleeps: list[float] = []

        def sleep(s: float) -> None:
            self.sleeps.append(s)
            clock.now += s

        monkeypatch.setattr(
            ci_wait, "wait_on_github", functools.partial(ci_wait.wait_on_github, clock=clock, sleep=sleep),
        )
        return clock

    @pytest.fixture(autouse=True)
    def _no_board(self, monkeypatch) -> None:
        def boom():
            raise AssertionError("the board was opened for a branch it does not watch")
        monkeypatch.setattr(ci_cmd, "_store", boom)

    def _wait(self, *extra: str, branch: str = RELEASE, minutes: str = "10"):
        return CliRunner().invoke(ci_group, ["wait", SHA, "--branch", branch, "--timeout", minutes, *extra])

    def test_a_release_branch_wakes_with_the_pull_request_result(self, monkeypatch, clock) -> None:
        gh = _Gh(_RUNNING, _GREEN, _GREEN)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait()
        assert result.exit_code == 0, result.output
        line = result.stdout.strip().splitlines()[-1]
        assert line == (
            f"{SHA[:12]} success: 3 check-runs, all passed https://github.com/Hellblazer/nexus/commit/{SHA} "
            f"(GitHub only: the board was not used for {RELEASE})"
        )
        # one request per interval, the interval is the documented constant
        assert len(gh.calls) == 3 and self.sleeps == [ci_wait.GITHUB_POLL_S] * 2 == [60.0, 60.0]
        assert gh.calls[0][:3] == [
            "gh", "api", f"repos/Hellblazer/nexus/commits/{SHA}/check-runs?per_page=100&filter=latest",
        ]
        assert "--paginate" in gh.calls[0]

    def test_a_check_run_that_registers_late_is_waited_for(self, monkeypatch, clock) -> None:
        # Two workflows done, the third registers on the second read: the first complete set is
        # not reported; only two consecutive reads of the same set are.
        gh = _Gh(_TWO_DONE, _GREEN, _GREEN)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait()
        assert result.exit_code == 0 and "3 check-runs, all passed" in result.stdout
        assert len(gh.calls) == 3

    def test_one_completed_read_is_not_enough(self, monkeypatch, clock) -> None:
        gh = _Gh(_GREEN)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait(minutes="1")  # reads at 0 and 60: the second confirms
        assert result.exit_code == 0 and len(gh.calls) == 2
        gh = _Gh(_GREEN)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait(minutes="0.5")  # only the read at 0 fits
        assert result.exit_code == 2 and "not yet confirmed by a second read" in result.stdout

    def test_a_failed_check_exits_1_at_once_and_is_named(self, monkeypatch, clock) -> None:
        gh = _Gh(_RED)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait()
        assert result.exit_code == 1, result.output
        assert "failure: 2 check-runs, 1 not passed: pytest (shard 1) failure" in result.stdout
        assert self.sleeps == [] and len(gh.calls) == 1

    @pytest.mark.parametrize(("conclusions", "code", "word"), [
        (["success", "neutral"], 0, "success"),
        (["success", "action_required"], 1, "failure"),
        (["failure", "cancelled"], 1, "failure"),
        (["success", "stale"], 3, "cancelled"),
        (["success", "cancelled"], 3, "cancelled"),
        (["success", None], 4, "unknown"),
        (["success", "something_new"], 4, "unknown"),
    ])
    def test_the_exit_code_follows_the_reduction(self, monkeypatch, clock, conclusions, code, word) -> None:
        out = _lines(*[(i, f"check{i}", "completed", c) for i, c in enumerate(conclusions, 1)])
        monkeypatch.setattr(ci_wait, "run_bounded", _Gh(out))
        result = self._wait()
        assert result.exit_code == code, result.output
        assert f" {word}: " in result.stdout

    def test_a_timeout_is_bounded_and_exits_2(self, monkeypatch, clock) -> None:
        gh = _Gh(_RUNNING)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait(minutes="2.5")  # 150 s: reads at 0, 60, 120; 180 is past the deadline
        assert result.exit_code == 2, result.output
        assert "timeout: 1 of 2 check-runs still running (GitHub only" in result.stdout
        assert len(gh.calls) == 3 and self.sleeps == [60.0, 60.0]
        assert clock.now == 120.0

    def test_no_read_comes_sooner_than_the_interval_after_the_previous(self, monkeypatch, clock) -> None:
        times: list[float] = []

        def gh(argv, **kwargs):
            times.append(clock.now)
            clock.now += 7  # a slow read: the next is due 60 s after this one STARTED
            return subprocess.CompletedProcess(argv, 0, stdout=_RUNNING, stderr="")

        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        self._wait(minutes="5")
        assert len(times) >= 4
        assert all(b - a >= ci_wait.GITHUB_POLL_S for a, b in zip(times, times[1:], strict=False))

    def test_a_read_is_never_given_more_time_than_remains(self, monkeypatch, clock) -> None:
        gh = _Gh(_RUNNING)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        self._wait(minutes="2.5")  # 150 s: read 1 has min(150, 60), read 3 at 120 has 30
        assert gh.timeouts == [60.0, 60.0, 30.0]

    def test_no_check_runs_yet_is_not_complete(self, monkeypatch, clock) -> None:
        gh = _Gh("", _GREEN, _GREEN)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait()
        assert result.exit_code == 0 and len(gh.calls) == 3

    def test_no_check_runs_by_the_deadline_exits_2(self, monkeypatch, clock) -> None:
        monkeypatch.setattr(ci_wait, "run_bounded", _Gh(""))
        result = self._wait(minutes="0")
        assert result.exit_code == 2 and "no check-runs for it yet" in result.stdout

    def test_a_read_that_fails_once_is_retried_and_the_wait_goes_on(self, monkeypatch, clock) -> None:
        gh = _Gh(_gh_error("HTTP 502"), _GREEN, _GREEN)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        assert self._wait().exit_code == 0 and len(gh.calls) == 3

    def test_the_same_error_three_times_exits_5_naming_it(self, monkeypatch, clock) -> None:
        gh = _Gh(_gh_error("HTTP 502 bad gateway"))
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait()
        assert result.exit_code == 5 and len(gh.calls) == 3
        assert "HTTP 502 bad gateway" in result.stdout and "GitHub could not be read" in result.stdout

    @pytest.mark.parametrize("error", [
        FileNotFoundError(2, "No such file or directory: 'gh'"),
        _gh_error("gh: Not Found (HTTP 404)"),
        _gh_error("gh: Validation Failed (HTTP 422)"),
        _gh_error("gh: Bad credentials (HTTP 401)"),
        _gh_error("To get started with GitHub CLI, please run:  gh auth login"),
    ])
    def test_a_permanent_error_exits_5_at_once(self, monkeypatch, clock, error) -> None:
        gh = _Gh(error)
        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait()
        assert result.exit_code == 5 and len(gh.calls) == 1 and self.sleeps == []

    def test_errors_that_differ_are_retried_to_the_deadline(self, monkeypatch, clock) -> None:
        calls = {"n": 0}

        def gh(argv, **kwargs):
            calls["n"] += 1
            raise _gh_error(f"HTTP 50{calls['n'] % 2}")

        monkeypatch.setattr(ci_wait, "run_bounded", gh)
        result = self._wait(minutes="5")
        assert result.exit_code == 5 and "until the deadline" in result.stdout and calls["n"] > 3

    @pytest.mark.parametrize("bad", [
        "not json\n", '{"a": 1}\n', '[1, "n", "completed"]\n', '["x", "n", "completed", null]\n',
    ])
    def test_an_output_line_that_is_not_a_check_run_is_a_failed_read(self, monkeypatch, bad) -> None:
        monkeypatch.setattr(ci_wait, "run_bounded", _Gh(bad))
        with pytest.raises(ci_wait.GhReadError, match="not a check-run"):
            ci_wait.gh_check_runs("o/r", SHA)

    def test_the_jq_filter_reads_a_recorded_api_page(self, monkeypatch) -> None:
        # The filter is what gh runs on each page; here the real jq does, over the shape of
        # GET /repos/{r}/commits/{sha}/check-runs (two pages, as --paginate emits them).
        jq = shutil.which("jq")
        if jq is None:
            pytest.skip("jq is not on PATH: the filter itself is not exercised on this host "
                        "(the other tests fake its output)")
        page1 = {"total_count": 3, "check_runs": [
            {"id": 11, "name": "lint", "status": "completed", "conclusion": "success", "app": {"slug": "gha"}},
            {"id": 12, "name": "pytest", "status": "in_progress", "conclusion": None},
        ]}
        page2 = {"total_count": 3, "check_runs": [
            {"id": 13, "name": "docs", "status": "completed", "conclusion": "neutral"},
        ]}
        raw = json.dumps(page1) + "\n" + json.dumps(page2) + "\n"
        out = subprocess.run([jq, "-c", ci_wait.CHECK_RUNS_JQ], input=raw, capture_output=True, text=True,
                             check=True).stdout
        assert len(out.splitlines()) == 3, out  # non-vacuity: the filter produced the three rows
        monkeypatch.setattr(ci_wait, "run_bounded", _Gh(out))
        assert ci_wait.gh_check_runs("o/r", SHA) == [
            ci_wait.CheckRun(11, "lint", "completed", "success"),
            ci_wait.CheckRun(12, "pytest", "in_progress", None),
            ci_wait.CheckRun(13, "docs", "completed", "neutral"),
        ]

    @pytest.mark.parametrize("gh_repo", ["nexus", "a/b/c", "../x", "x/..", "a b/c", "o/r;rm", "o/r?x=1"])
    def test_a_bad_gh_repo_exits_5(self, monkeypatch, gh_repo) -> None:
        monkeypatch.setattr(ci_wait, "run_bounded", lambda *a, **k: pytest.fail("gh ran for a bad repo"))
        result = self._wait("--gh-repo", gh_repo)
        assert result.exit_code == 5 and "OWNER/NAME" in result.stdout

    def test_a_watched_branch_whose_topic_the_engine_refuses_falls_back(self, monkeypatch, clock) -> None:
        from nexus.db.t2.http_tuple_store import UnknownSubspaceError  # noqa: PLC0415

        class Refusing:
            def rd(self, *a, **k):
                raise UnknownSubspaceError("no template for board/ci/nexus-main--done")

        monkeypatch.setattr(ci_cmd, "_store", lambda: Refusing())
        monkeypatch.setattr(ci_wait, "run_bounded", _Gh(_GREEN, _GREEN))
        result = self._wait(branch="main")
        assert result.exit_code == 0 and "GitHub only: the board was not used for main" in result.stdout

    def test_the_fallback_gets_the_time_that_remains(self, monkeypatch) -> None:
        from nexus.db.t2.http_tuple_store import UnknownSubspaceError  # noqa: PLC0415

        class Refusing:
            def rd(self, *a, **k):
                raise UnknownSubspaceError("no template")

        ticks = iter([1000.0, 1000.0 + 10 * 60])  # the board read took 10 of the 60 minutes
        monkeypatch.setattr(ci_cmd, "_now", lambda: next(ticks))
        monkeypatch.setattr(ci_cmd, "_store", lambda: Refusing())
        got: list[float] = []

        def fake(gh_repo, sha, branch, *, timeout_s):
            got.append(timeout_s)
            return "line", 0

        monkeypatch.setattr(ci_wait, "wait_on_github", fake)
        result = CliRunner().invoke(ci_group, ["wait", SHA, "--branch", "main", "--timeout", "60"])
        assert result.exit_code == 0 and got == [50 * 60.0]

    def test_develop_and_main_still_use_the_board(self, monkeypatch) -> None:
        for branch in ("develop", "main"):
            board = _Board(_Clock())
            board.post(SHA, [[10, 1, "CI", "success"]])
            board_seen: list[str] = []
            orig = board.rd

            def rd(subspace, *a, _orig=orig, **k):
                board_seen.append(subspace)
                return _orig("board/ci/nexus-develop--done", *a, **k)

            board.rd = rd  # type: ignore[method-assign]
            monkeypatch.setattr(ci_cmd, "_store", lambda b=board: b)
            monkeypatch.setattr(ci_wait, "run_bounded", lambda *a, **k: pytest.fail("gh read on the board path"))
            result = CliRunner().invoke(ci_group, ["wait", SHA, "--branch", branch, "--timeout", "1"])
            assert result.exit_code == 0 and "GitHub only" not in result.stdout
            assert board_seen[0] == f"board/ci/nexus-{branch}--done"
