# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake clock for tests of scripts/claude-restart-sessions (suite audit item 1, 2026-11-19).

Every deadline loop in the script (went_busy, _status_after, run_when_idle) sleeps on each pass and reads time.time
for its deadline. A test that only silences time.sleep leaves the real clock running, so each loop it reaches
busy-spins for its full 30-120 s wait. fake_clock makes the script's sleep return at once and move its clock on by
the seconds asked for. tests/scripts/test_claude_restart_sessions_clock.py keeps every fake in the restart tests
going through it.
"""

from __future__ import annotations

import time
from typing import Any

from tests._module_seam import setattr_in


def fake_clock(crs: Any, monkeypatch: Any, record: list[float] | None = None) -> list[float]:
    """Patch the script's time.sleep and time.time onto one fake clock, starting at the real time so files a test
    writes still read as recent; *record*, when given, receives each sleep's seconds. Returns the clock cell."""
    now = [time.time()]

    def sleep(seconds: float) -> None:
        if record is not None:
            record.append(seconds)
        now[0] += max(float(seconds), 0.0)

    setattr_in(monkeypatch, crs, "time.sleep", sleep)
    setattr_in(monkeypatch, crs, "time.time", lambda: now[0])
    return now
