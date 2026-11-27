# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one detector for a ``bd`` that is installed but has no database (GH #1689).

Three readers (the console's needs-sam read, the SessionStart ``nx brief beads`` topic and the ``nx command-context``
preamble) tell this case from a ``bd`` that is broken. They share ``nexus.beads_state``; a fourth copy of the sentinel
is what this file refuses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import beads_state


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("Error: no beads database found\nHint: run 'bd init'",), True),
        (("", "ERROR: No Beads Database Found"), True),
        ((None, "Error: dolt server unreachable"), False),
        (("",), False),
        ((), False),
    ],
)
def test_the_detector_reads_either_stream_case_insensitively(texts, expected) -> None:
    assert beads_state.is_no_beads_database(*texts) is expected


def test_failure_summary_names_the_exit_and_the_first_line() -> None:
    assert beads_state.failure_summary(2, "", "Error: boom\nmore") == "exit 2: Error: boom"
    assert beads_state.failure_summary(2, "", "") == "exit 2"


def test_the_sentinel_text_has_one_owner() -> None:
    """A second literal of bd's error text in src/ is a fourth copy of the detector."""
    root = Path(__file__).resolve().parents[1] / "src" / "nexus"
    owners = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py") if "no beads database found" in p.read_text(encoding="utf-8").lower()
    )
    assert owners == ["beads_state.py"]
