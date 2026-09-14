# SPDX-License-Identifier: AGPL-3.0-or-later
"""``install_census.PS_COMMAND`` and the shell half's ``_nx_ps_snapshot`` run
the same ``ps`` command, and both ask for unlimited width.

procps truncates a piped command column to ``COLUMNS`` when that variable is
set, so a snapshot taken without ``ww`` can drop the tail of a long command
line: the part a holder census matches on. The Python constant said it was
byte-identical to the shell function, and nothing checked it.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.install_census import PS_COMMAND

CENSUS_SH = Path(__file__).resolve().parent.parent / "src" / "nexus" / "_install" / "census.sh"


def _shell_snapshot_command() -> str:
    body = CENSUS_SH.read_text(encoding="utf-8")
    fn = re.search(r"_nx_ps_snapshot\(\) \{(.*?)\n\}", body, re.S)
    assert fn, "_nx_ps_snapshot() not found in census.sh"
    cmd = re.search(r'\$\((ps [^)]*?) 2>/dev/null\)', fn.group(1))
    assert cmd, "no ps invocation inside _nx_ps_snapshot()"
    return cmd.group(1)


def test_python_and_shell_snapshots_run_the_same_ps_command() -> None:
    assert _shell_snapshot_command() == " ".join(PS_COMMAND)


def test_the_snapshot_asks_for_unlimited_width() -> None:
    assert "ww" in PS_COMMAND[1], PS_COMMAND
