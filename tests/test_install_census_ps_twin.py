# SPDX-License-Identifier: AGPL-3.0-or-later
"""The snapshot command asks for unlimited width, and only one place runs it.

procps truncates a piped command column to ``COLUMNS`` when that variable is
set, so a snapshot taken without ``ww`` can drop the tail of a long command
line: the part a holder census matches on.

This file used to compare ``install_census.PS_COMMAND`` against a ``ps``
invocation parsed out of ``census.sh``, because the two halves each ran their
own. The census twin collapsed and census.sh now dispatches, so there is one
invocation and nothing to compare. What is left worth checking is that the one
that remains asks for unlimited width, and that a second one has not reappeared
in the shell.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.install_census import PS_COMMAND

CENSUS_SH = Path(__file__).resolve().parent.parent / "src" / "nexus" / "_install" / "census.sh"


def test_the_snapshot_asks_for_unlimited_width() -> None:
    assert "ww" in PS_COMMAND[1], PS_COMMAND


def test_the_shell_half_runs_no_ps_of_its_own() -> None:
    """The comparison this file used to make, inverted.

    A ``ps`` reappearing in census.sh is a second snapshot: it would not be the
    one the census loop passes down, so generations would stop being attributed
    from a single view -- and it would be written without anyone thinking about
    ``ww``, which is what this file exists for.

    Comments are stripped first; the header discusses ``ps`` at length while
    explaining why one snapshot serves the whole pass.
    """
    code = "\n".join(
        line for line in CENSUS_SH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    found = re.findall(r"(?:^|[\s(`$])ps\s+[a-z-]", code)
    assert not found, (
        f"census.sh runs ps again ({found}); the snapshot belongs in "
        f"census_core.py, where one call serves the whole census and PS_COMMAND "
        f"is the single place -ww is spelled"
    )


def test_the_core_is_the_only_place_ps_is_spelled() -> None:
    """Non-vacuity for the check above: it passes trivially if PS_COMMAND has
    moved somewhere this file no longer looks."""
    from nexus._install import census_core

    assert census_core.PS_COMMAND == PS_COMMAND
    assert PS_COMMAND[0] == "ps"
