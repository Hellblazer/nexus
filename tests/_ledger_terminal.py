# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fixture that lets a ledger test's report-mode lines past pytest's capture (RDR-258 Phase 4, nexus-2jomg.5).

``scripts/ledger_mode.py`` prints report lines through a sink. pytest captures a passing test's output, so a report-mode pass
would list its findings nowhere a run (the push gate, a CI log) could read them. A ledger test module imports
``ledger_terminal`` and lists it in ``pytestmark``: the sink then writes with capture suspended, each line at column 0
(a leading newline keeps it off a progress dot's line).

Only the tree's own check may speak. A module that also runs the check on a synthetic world (a fixture tree, a stubbed diff)
names its live tests in a module-level ``LEDGER_LIVE_TESTS`` tuple of function names; every other test of that module gets a
sink that drops the lines, so a synthetic finding or summary can never be relayed as the tree's. A module with no such tuple
speaks from every test that asks for the fixture (its fixture is used by name on the one live test).
"""
from __future__ import annotations

import sys
from collections.abc import Iterator

import ledger_mode
import pytest


@pytest.fixture
def ledger_terminal(request: pytest.FixtureRequest, capfd: pytest.CaptureFixture[str]) -> Iterator[None]:
    live = getattr(request.module, "LEDGER_LIVE_TESTS", None)
    speaks = live is None or request.function.__name__ in live

    def sink(line: str) -> None:
        if not speaks:
            return
        with capfd.disabled():
            sys.stdout.write(f"\n{line}\n")
            sys.stdout.flush()

    ledger_mode.set_sink(sink)
    try:
        yield
    finally:
        ledger_mode.set_sink(None)
