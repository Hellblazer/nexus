# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``ps`` call that reads a command line asks for unlimited width.

procps (Linux) truncates ``ps`` output to COLUMNS when stdout is a pipe unless
``-ww`` is given, so a match on the tail of a command line silently misses.
It hid a live mailbox watcher on CI (7.46.0, 5957a0055), and the sweep that
fixed it missed two more calls: the drain hook's watcher probe and
``nx doctor``'s orphan-tracker count. macOS ps does not truncate, so no local
run shows the defect; this scan is what catches the next one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = ("src/nexus", "conexus/hooks/scripts")

#: A Python argv list or tuple whose first element is "ps".
_PY_PS = (
    re.compile(r'\[\s*"ps"\s*,(?P<args>[^\]]*)\]'),
    re.compile(r'\(\s*"ps"\s*,(?P<args>[^)]*)\)'),
)
#: A shell ps invocation, up to the end of its pipeline stage.
_SH_PS = re.compile(r'(?:^|[\s(`$])ps\s+(?P<args>[^|;)\n]*)')
_READS_COMMAND = re.compile(r"\b(?:command|args)\b")


def _command_reading_calls() -> list[tuple[str, str]]:
    """``(location, args)`` for every ps call whose output includes a
    command line, with or without ``-ww``."""
    calls: list[tuple[str, str]] = []
    for base in _SOURCES:
        for path in sorted((_ROOT / base).rglob("*")):
            if path.suffix == ".py":
                text = path.read_text(encoding="utf-8")
                for pattern in _PY_PS:
                    for m in pattern.finditer(text):
                        line = text.count("\n", 0, m.start()) + 1
                        calls.append((f"{path.relative_to(_ROOT)}:{line}", m.group("args")))
            elif path.suffix == ".sh":
                for line_no, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1,
                ):
                    if line.lstrip().startswith("#"):
                        continue
                    for m in _SH_PS.finditer(line):
                        calls.append((f"{path.relative_to(_ROOT)}:{line_no}", m.group("args")))
    return [(loc, args) for loc, args in calls if _READS_COMMAND.search(args)]


def test_every_command_reading_ps_call_passes_ww() -> None:
    offenders = [f"{loc}: ps {args.strip()}" for loc, args in _command_reading_calls()
                 if "ww" not in args]
    assert offenders == [], (
        "ps without -ww truncates the command column to COLUMNS on Linux:\n"
        + "\n".join(offenders)
    )


def test_the_scan_sees_the_calls_it_guards() -> None:
    """Non-vacuity: a scan that found nothing would pass the check above.

    RDR-211 nexus-rplay.14 deleted the former CLI mailbox-watch module's
    ``ps`` call along with the module itself, and the mailbox drain hook's
    own ``ps`` call along with the per-prompt re-arm it served -- neither
    site exists any more, so neither is named below.
    """
    locations = [loc for loc, _args in _command_reading_calls()]
    assert len(locations) >= 6, locations
    for expected in (
        "src/nexus/install_census.py",
        "src/nexus/commands/doctor.py",
        "src/nexus/_install/census.sh",
    ):
        assert any(loc.startswith(expected + ":") for loc in locations), expected
