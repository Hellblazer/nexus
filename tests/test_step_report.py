# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared step reporter of nx upgrade and nx self install (nexus-mjmjl).

Time is a fake clock moved by hand and the ticker is driven directly, so no
test sleeps."""

from __future__ import annotations

import io

import pytest

from nexus.progress.step_report import (
    PLAIN_HEARTBEAT_S,
    TTY_HEARTBEAT_S,
    StepReporter,
    format_bytes,
    format_duration,
    no_ticker,
    run_ticker,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reporter(total: int, *, tty: bool = False, quiet: bool = False):
    clock = Clock()
    lines: list[str] = []
    stream = io.StringIO()
    rep = StepReporter(
        total, echo=lines.append, stream=stream, tty=tty, quiet=quiet, clock=clock, ticker=no_ticker,
    )
    return rep, clock, lines, stream


def test_each_step_is_announced_before_it_runs_and_timed_after() -> None:
    rep, clock, lines, _ = _reporter(2)
    seen_inside: list[list[str]] = []
    with rep.step("Update the plugins", "4 plugins"):
        seen_inside.append(list(lines))
        clock.now += 2.5
    with rep.step("Prune uv's package cache"):
        clock.now += 61
    assert seen_inside == [["Step 1 of 2: Update the plugins (4 plugins)"]]
    assert lines == [
        "Step 1 of 2: Update the plugins (4 plugins)",
        "  done in 2.5 s",
        "Step 2 of 2: Prune uv's package cache",
        "  done in 1 m 01 s",
    ]


def test_a_long_step_emits_a_heartbeat_with_elapsed_time_plain_output() -> None:
    rep, clock, lines, stream = _reporter(1)
    with rep.step("Walk the upgrade ladder") as step:
        clock.now += PLAIN_HEARTBEAT_S - 1
        step.tick()  # not due yet
        clock.now += 2
        step.tick()
        clock.now += 1
        step.tick()  # due again only a full interval later
    beats = [line for line in lines if "elapsed" in line]
    assert beats == [f"  ... {format_duration(PLAIN_HEARTBEAT_S + 1)} elapsed"]
    assert stream.getvalue() == ""  # plain output never draws in place
    assert all("\x1b" not in line and "\r" not in line for line in lines)


def test_a_terminal_step_that_prints_gets_whole_heartbeat_lines_never_a_dangling_redraw() -> None:
    """Non-compact (nx upgrade): a step prints its own lines, so a heartbeat
    is a whole line on a terminal too; an unterminated redraw would glue the
    step's next line onto it (review r1)."""
    rep, clock, lines, stream = _reporter(1, tty=True)
    with rep.step("Converge the engine") as step:
        clock.now += TTY_HEARTBEAT_S
        step.tick()
        lines.append("Precondition [engine]: installed 1.2")  # what a step prints itself
    assert stream.getvalue() == ""
    assert lines[1] == f"  ... {format_duration(TTY_HEARTBEAT_S)} elapsed"
    assert lines[2] == "Precondition [engine]: installed 1.2"


def test_a_failure_line_redacts_credentials_and_can_hide_the_reason() -> None:
    rep, clock, lines, _ = _reporter(1)
    with pytest.raises(RuntimeError), rep.step("List collections"):
        raise RuntimeError("401 for https://user:SUPERSECRET@example.test/v1 (token=SUPERSECRET)")
    assert "SUPERSECRET" not in "\n".join(lines)
    for leak in ("Authorization: Bearer abcdef0123456789abcdef", "key pa-0123456789abcdefghijklmnop"):
        with pytest.raises(RuntimeError), rep.step("Call the engine"):
            raise RuntimeError(f"rejected: {leak}")
        assert leak.split()[-1] not in lines[-1], lines[-1]
    hidden = StepReporter(1, echo=lines.append, stream=io.StringIO(), tty=False, clock=clock, ticker=no_ticker,
                          show_reason=False)
    with pytest.raises(RuntimeError), hidden.step("x"):
        raise RuntimeError("SUPERSECRET")
    assert lines[-1] == "  failed after 0.0 s"


def test_a_lazy_step_refused_before_work_leaves_no_line_and_a_discarded_one_neither() -> None:
    import click

    rep, clock, lines, _ = _reporter(2)
    with pytest.raises(click.ClickException), rep.step("Build", lazy=True):
        raise click.ClickException("already installed")
    with rep.step("Build", lazy=True) as step:
        step.discard = True
    assert lines == []
    with rep.step("Build", lazy=True) as step:
        clock.now += 2
        step.tick()  # past LAZY_ANNOUNCE_S: it announces now
    assert lines[0] == "Step 1 of 2: Build" and lines[-1] == "  done in 2.0 s"


def test_quiet_lines_go_to_stderr_by_default(capsys) -> None:
    rep = StepReporter(1, quiet=True, tty=False, ticker=no_ticker, stream=io.StringIO())
    with rep.step("Walk the ladder"):
        pass
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("Step 1 of 1: Walk the ladder")

def test_progress_shows_done_of_total() -> None:
    rep, clock, lines, _ = _reporter(1)
    with rep.step("Download the engine") as step:
        clock.now += PLAIN_HEARTBEAT_S
        step.progress(40e6, 90e6, "bytes")
    with rep.step("Backfill") as step:
        clock.now += PLAIN_HEARTBEAT_S
        step.progress(120, 400, "rows")
    beats = [line for line in lines if "elapsed" in line]
    assert beats[0].startswith("  ... 40.0 MB of 90.0 MB, ")
    assert beats[1].startswith("  ... 120 of 400 rows, ")


def test_a_failure_says_how_long_it_ran_and_the_remedy_and_propagates() -> None:
    rep, clock, lines, _ = _reporter(1)
    with pytest.raises(RuntimeError), rep.step("Update the plugins", remedy="Run `nx upgrade` again once online."):
        clock.now += 3
        raise RuntimeError("git fetch failed: network is unreachable\nmore")
    assert lines[1:] == [
        "  failed after 3.0 s: git fetch failed: network is unreachable",
        "  Run `nx upgrade` again once online.",
    ]
    assert not rep.records[0].ok


def test_the_summary_lists_each_steps_duration() -> None:
    rep, clock, lines, _ = _reporter(2)
    with rep.step("Run migrations"):
        clock.now += 0.4
    with rep.step("Update the plugins"):
        clock.now += 12
    summary = rep.summary()
    assert summary == [
        "Summary: 2 step(s) in 12.4 s",
        "   1. Run migrations      0.4 s",
        "   2. Update the plugins  12.0 s",
    ]
    assert lines[-3:] == summary


def test_quiet_mode_writes_nothing_to_stdout_and_logs_every_line(capsys) -> None:
    clock = Clock()
    lines: list[str] = []
    logged: list[str] = []
    stream = io.StringIO()
    rep = StepReporter(
        1, echo=lines.append, stream=stream, tty=True, quiet=True, clock=clock, ticker=no_ticker, log=logged.append,
    )
    with rep.step("Walk the upgrade ladder") as step:
        clock.now += PLAIN_HEARTBEAT_S
        step.tick()
    rep.summary()
    assert lines == [] and stream.getvalue() == ""
    assert capsys.readouterr().out == ""
    assert logged[0] == "Step 1 of 1: Walk the upgrade ladder"
    assert any("elapsed" in text for text in logged)
    assert any(text.startswith("Summary:") for text in logged)


def test_skip_keeps_the_count_honest() -> None:
    rep, _clock, lines, _ = _reporter(3)
    with rep.step("a"):
        pass
    rep.skip()
    with rep.step("c"):
        pass
    assert lines[0] == "Step 1 of 3: a" and lines[2] == "Step 2 of 2: c"


def test_the_ticker_loop_ticks_until_stopped() -> None:
    answers = iter([False, False, True])
    ticks: list[int] = []
    run_ticker(lambda _interval: next(answers), lambda: ticks.append(1))
    assert ticks == [1, 1]


def test_the_thread_ticker_starts_and_stops_with_the_step() -> None:
    import threading  # noqa: PLC0415 — test-only

    before = {t.name for t in threading.enumerate()}
    rep = StepReporter(1, echo=lambda _s: None, stream=io.StringIO(), tty=False)
    with rep.step("x"):
        assert any(t.name == "step-1-ticker" for t in threading.enumerate())
    assert {t.name for t in threading.enumerate()} <= before | set()


@pytest.mark.parametrize(
    ("seconds", "text"), [(0.04, "0.0 s"), (59.9, "59.9 s"), (65, "1 m 05 s"), (3725, "1 h 02 m")],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


def test_format_bytes() -> None:
    assert [format_bytes(n) for n in (512, 3_400_000, 1.2e9)] == ["512 B", "3.4 MB", "1.2 GB"]


def _compact(total: int, *, tty: bool):
    clock = Clock()
    lines: list[str] = []
    stream = io.StringIO()
    rep = StepReporter(
        total, echo=lines.append, stream=stream, tty=tty, clock=clock, ticker=no_ticker,
        compact=True, slow_s=3.0, heartbeat_s=3.0,
    )
    return rep, clock, lines, stream


def test_compact_on_a_terminal_redraws_the_check_name_and_prints_only_slow_ones() -> None:
    rep, clock, lines, stream = _compact(2, tty=True)
    with rep.step("python"):
        clock.now += 0.1
    with rep.step("t3 local") as step:
        clock.now += 3
        step.tick()
        clock.now += 1
    drawn = stream.getvalue()
    assert "\r\x1b[KStep 1 of 2: python" in drawn
    assert "\r\x1b[KStep 2 of 2: t3 local ... 3.0 s elapsed" in drawn
    assert lines == ["  t3 local: done in 4.0 s"]


def test_compact_plain_output_names_each_check_as_it_starts_and_lists_the_slow_ones_at_the_end() -> None:
    rep, clock, lines, stream = _compact(2, tty=False)
    with rep.step("python"):
        clock.now += 0.1
    with rep.step("t3 local"):
        clock.now += 4
    rep.summary(min_s=1.0)
    assert stream.getvalue() == ""
    assert lines == [
        "Step 1 of 2: python",
        "Step 2 of 2: t3 local",
        "  t3 local: done in 4.0 s",
        "Summary: 2 step(s) in 4.1 s; 1 took 1.0 s or more",
        "   2. t3 local  4.0 s",
    ]


def test_a_compact_step_that_prints_is_announced_as_a_whole_line() -> None:
    """Doctor's supplementary checks echo their own result: their step line is
    not redrawn in place, so the result does not glue onto it (review r2)."""
    clock = Clock()
    lines: list[str] = []
    stream = io.StringIO()
    rep = StepReporter(0, echo=lines.append, stream=stream, tty=True, clock=clock, ticker=no_ticker,
                       compact=True, heartbeat_s=3.0, announce="Checking {what}{detail}")
    with rep.step("resources", prints=True) as step:
        lines.append("  ok: something")
        clock.now += 3
        step.tick()
    assert stream.getvalue() == ""
    assert lines[:2] == ["Checking resources", "  ok: something"]
    assert lines[2] == "  ... 3.0 s elapsed"
