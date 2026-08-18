# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for scripts/check_lint_leg_non_vacuity.py (nexus-wixar).

Task 5b's counterfactual: feed the parse step a "938 skipped" summary (the
bug's own real signature) and confirm it errors; feed it a healthy summary
and confirm it passes.

Two false-positive classes were found and fixed WHILE writing this gate,
against real local reproductions (hiding this checkout's own service jar
and running `uv run pytest -m lint -q` under the exact `test-lint` job env,
before and after the fix) rather than hand-typed fixtures alone:

1. The parser originally scanned the WHOLE captured output for "N error(s)"
   anywhere, not just pytest's own final summary line. The real mass-skip
   run's startup banner (nexus-zryqm's stale-jar notice) contains the prose
   "~73 errors will surface at the END" -- matched as 73 executed tests,
   masking the true 0. Fixed by anchoring to the line ending "in <N>s"
   (pytest's own summary-line signature) and parsing counts from ONLY that
   line.
2. A run over ~60s prints "in 66.17s (0:01:06)" -- the trailing
   "(H:MM:SS)" broke the first anchored-regex attempt's end-of-line match,
   which would have false-failed every healthy long-enough real CI run.
"""

from __future__ import annotations

import pathlib

# scripts/ is on pythonpath via [tool.pytest.ini_options] in pyproject.toml
# (same convention as the sibling check_remediation_commits_ride_release /
# check_engine_release_floor tests), so the gate module imports directly
# with no sys.path hack.
from check_lint_leg_non_vacuity import check, count_executed, main

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# The exact real-world signature from PR #1459's vacuous lint leg.
_BUG_SIGNATURE = "938 skipped, 13459 deselected in 8.23s"

_HEALTHY_SUMMARY = "823 passed, 6 skipped, 13459 deselected in 45.23s"

_WITH_FAILURES = "820 passed, 3 failed, 6 skipped, 13459 deselected in 45.23s"

_WITH_ERRORS = "800 passed, 2 errors, 6 skipped, 13459 deselected in 40.11s"


def test_count_executed_all_skipped_is_zero() -> None:
    """The bug's own real signature: no 'passed' token is printed at all
    when everything skips -- must count as 0 executed, not raise."""
    assert count_executed(_BUG_SIGNATURE) == (0, 0, 0)


def test_count_executed_healthy_run() -> None:
    assert count_executed(_HEALTHY_SUMMARY) == (823, 0, 823)


def test_count_executed_counts_failures_as_executed() -> None:
    """A failing run still EXECUTED its tests -- failed must count toward
    the floor, only skipped/deselected are excluded."""
    assert count_executed(_WITH_FAILURES) == (820, 3, 823)


def test_count_executed_counts_errors_as_executed() -> None:
    assert count_executed(_WITH_ERRORS) == (800, 2, 802)


def test_check_flags_the_bugs_own_signature() -> None:
    """THE REGRESSION TEST: this exact string is what CI actually printed
    on the vacuous run (PR #1459, 938 skipped / 0 executed) -- must error."""
    reason = check(_BUG_SIGNATURE)
    assert reason is not None
    assert "0 test(s)" in reason
    assert "nexus-wixar" in reason


def test_check_passes_a_healthy_run() -> None:
    assert check(_HEALTHY_SUMMARY) is None


def test_check_respects_custom_floor() -> None:
    assert check(_HEALTHY_SUMMARY, floor=900) is not None
    assert check(_HEALTHY_SUMMARY, floor=100) is None


def test_check_trips_just_below_default_floor() -> None:
    just_under = "399 passed, 6 skipped, 13459 deselected in 20s"
    assert check(just_under) is not None


def test_check_clears_just_at_default_floor() -> None:
    at_floor = "400 passed, 6 skipped, 13459 deselected in 20s"
    assert check(at_floor) is None


def test_main_exits_nonzero_and_prints_error_marker_on_vacuous_input(
    tmp_path, capsys
) -> None:
    """End-to-end through main(): the ::error annotation CI's log scraper
    keys on must actually be printed, and the exit code must be 1."""
    output_file = tmp_path / "lint-output.txt"
    output_file.write_text(_BUG_SIGNATURE)

    rc = main([str(output_file)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "::error::" in captured.out
    assert "nexus-wixar" in captured.out


def test_main_exits_zero_on_healthy_input(tmp_path, capsys) -> None:
    output_file = tmp_path / "lint-output.txt"
    output_file.write_text(_HEALTHY_SUMMARY)

    rc = main([str(output_file)])

    assert rc == 0
    captured = capsys.readouterr()
    assert "::error::" not in captured.out


def test_ignores_error_count_mentioned_in_unrelated_banner_prose() -> None:
    """FALSE-POSITIVE CLASS 1 (found against a real local repro). The
    nexus-zryqm stale-jar startup banner says "~73 errors will surface at
    the END" ahead of the real summary -- must not be counted as 73
    executed tests when the real summary line says 0."""
    output = (
        "SERVICE JAR STALE — engine-substrate tests will error: ...\n"
        "Rebuild BEFORE trusting this run, or ~73 errors will surface at "
        "the END:\n"
        "    mvn -f service/pom.xml package -DskipTests\n"
        "ssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssss "
        "[100%]\n"
        "938 skipped, 13602 deselected, 1 warning in 6.07s\n"
    )
    assert count_executed(output) == (0, 0, 0)
    assert check(output) is not None


def test_handles_long_run_hms_duration_suffix() -> None:
    """FALSE-POSITIVE CLASS 2 (found against a real local repro). A run
    over ~60s prints "in 66.17s (0:01:06)" -- the trailing "(H:MM:SS)" must
    not break the summary-line match (it did, in the first cut of this
    parser: the anchor required "in <N>s" at the exact end of line, which
    this fixture's real captured output does not satisfy)."""
    output = (
        "..                                                    [100%]\n"
        "831 passed, 107 skipped, 13602 deselected, 1 warning "
        "in 66.17s (0:01:06)\n"
    )
    assert count_executed(output) == (831, 0, 831)
    assert check(output) is None


def test_real_ci_mass_skip_fixture_trips_the_floor() -> None:
    """Real captured output (nexus-wixar local repro: this checkout's own
    service jar hidden, `GITHUB_ACTIONS=true` and no
    `NX_TEST_T2_SUBSTRATE` set -- the pre-fix `test-lint` job's exact
    shape) -- 938 skipped, 0 executed, exit 0. Must trip the floor."""
    text = (_FIXTURES / "wixar_real_ci_mass_skip_output.txt").read_text()
    assert count_executed(text) == (0, 0, 0)
    reason = check(text)
    assert reason is not None
    assert "nexus-wixar" in reason


def test_real_ci_fixed_fixture_clears_the_floor() -> None:
    """Real captured output (nexus-wixar local repro: same hidden-jar box,
    `NX_TEST_T2_SUBSTRATE=none` added -- the post-fix `test-lint` job's
    exact shape) -- 831 passed, 107 skipped, 0 failed. Must clear the
    floor."""
    text = (_FIXTURES / "wixar_real_ci_fixed_output.txt").read_text()
    passed, failed, executed = count_executed(text)
    assert passed == 831
    assert failed == 0
    assert executed == 831
    assert check(text) is None


def test_main_reads_stdin_when_no_file_given(monkeypatch, capsys) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(_BUG_SIGNATURE))

    rc = main([])

    assert rc == 1
    assert "::error::" in capsys.readouterr().out
