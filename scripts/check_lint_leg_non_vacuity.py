#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI gate: fail loud if the `pytest (lint markers)` leg executed near-zero
tests (nexus-wixar).

Root cause this closes: CI's `test-lint` job runs `uv run pytest -m lint -q`
against a runner that deliberately provisions no service jar. Before
nexus-wixar, the autouse `_pin_t2_substrate` fixture (tests/conftest.py)
pulled in the engine substrate for every test regardless of marker, and
`t2_service_env`'s own CI-only graceful-skip branch then fired for every
lint-marked item -- the entire ~830-test corpus. `pytest`'s own exit code is
0 when every selected test is SKIPPED (skip is not a failure), so the job
reported success having executed zero tests (`938 skipped, 13459 deselected`,
real CI run, PR #1459) and nothing caught it.

The fixture-level fix (`NX_TEST_T2_SUBSTRATE=none` in the job's env, mirroring
the `test-mode-census` job's identical nexus-vdti6 fix) removes the ROOT
CAUSE. This script is the independent, mechanized BACKSTOP the acceptance bar
requires: even if the substrate pin regresses again, or a future change
introduces some OTHER mass-skip mechanism nobody has thought of yet, a lint
leg that executes below the floor fails the job outright instead of quietly
reporting green.

Usage:
    uv run pytest -m lint -q | tee lint-output.txt
    uv run python scripts/check_lint_leg_non_vacuity.py lint-output.txt

Reads pytest's own `-q` terminal summary line (the last line pytest prints,
e.g. "823 passed, 6 skipped, 13459 deselected in 45.23s" or, in the bug's own
words, "938 skipped, 13459 deselected in 8.23s") -- no `--junitxml`/plugin
dependency needed, this is plain-text pytest output.
"""
from __future__ import annotations

import argparse
import re
import sys

# Corpus is ~829 lint-marked tests as of 2026-08-18 (nexus-wixar). This floor
# leaves generous headroom for legitimate shrinkage (tests deleted, markers
# moved) while making the bug's own signature -- 0 executed -- and anything
# close to it structurally impossible to pass again.
LINT_EXECUTED_FLOOR = 400

# pytest's own final summary line always ends "in <seconds>s" (e.g.
# "823 passed, 6 skipped, 13459 deselected in 45.23s") -- anchoring on that
# suffix is what keeps this from matching arbitrary prose elsewhere in the
# captured output. Found the hard way: an unrelated startup banner
# (nexus-zryqm's "Rebuild BEFORE trusting this run, or ~73 errors will
# surface at the END:" notice) contains the substring "73 errors" and was
# silently miscounted as 73 executed tests before this line was anchored to
# the summary specifically -- a false positive that happened to still trip
# the floor on that particular input, but would not in general.
_SUMMARY_LINE_RE = re.compile(
    r"^.* in [\d.]+s(?: \(\d+:\d+:\d+\))?\s*$", re.MULTILINE
)
_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) failed")
_ERROR_RE = re.compile(r"(\d+) errors?\b")


def _last_summary_line(pytest_output: str) -> str:
    """Return pytest's own final summary line, or "" if none is found (e.g.
    a run that crashed before printing one)."""
    matches = _SUMMARY_LINE_RE.findall(pytest_output)
    return matches[-1] if matches else ""


def count_executed(pytest_output: str) -> tuple[int, int, int]:
    """Return (passed, failed_plus_errored, executed) parsed from pytest's
    own `-q` summary LINE (not the whole captured output -- see
    ``_last_summary_line``).

    "executed" = passed + failed + errored -- deliberately EXCLUDING skipped
    and deselected, which is exactly the distinction the underlying bug
    erased (a run that only ever skips or deselects still reports these as
    0/0/0).  Missing counters (pytest omits a category entirely when it is
    zero, e.g. no "0 passed" is ever printed for an all-skipped run) count as
    zero rather than raising -- the all-skipped case is precisely the input
    this function must handle without erroring on a technicality.
    """
    line = _last_summary_line(pytest_output)
    passed = sum(int(m) for m in _PASSED_RE.findall(line))
    failed = sum(int(m) for m in _FAILED_RE.findall(line))
    errored = sum(int(m) for m in _ERROR_RE.findall(line))
    failed_plus_errored = failed + errored
    return passed, failed_plus_errored, passed + failed_plus_errored


def check(pytest_output: str, floor: int = LINT_EXECUTED_FLOOR) -> str | None:
    """Return an error message if *pytest_output* shows fewer than *floor*
    executed tests, else ``None``."""
    passed, failed, executed = count_executed(pytest_output)
    if executed < floor:
        return (
            f"lint leg executed only {executed} test(s) (passed={passed} "
            f"failed={failed}, floor={floor}) -- the lint-marked corpus is "
            "not running for real. This is the nexus-wixar vacuous-CI-gate "
            "class: a mass-skip that still exits 0."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "output_file",
        nargs="?",
        help="path to captured `pytest -m lint -q` output; reads stdin if omitted",
    )
    parser.add_argument(
        "--floor",
        type=int,
        default=LINT_EXECUTED_FLOOR,
        help=f"minimum executed (passed+failed) count required (default: {LINT_EXECUTED_FLOOR})",
    )
    args = parser.parse_args(argv)

    if args.output_file:
        with open(args.output_file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    passed, failed, executed = count_executed(text)
    print(f"lint leg: passed={passed} failed={failed} executed={executed} floor={args.floor}")

    reason = check(text, args.floor)
    if reason is not None:
        print(f"::error::{reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
