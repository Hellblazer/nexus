# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fixture that lets a ledger test's report-mode lines past pytest's capture (RDR-258 Phase 4, nexus-2jomg.5).

``scripts/ledger_mode.py`` prints report lines through a sink. pytest captures a passing test's output, so a report-mode pass
would list its findings nowhere a run (the push gate, a CI log) could read them. A ledger test module imports
``ledger_terminal`` and lists it in ``pytestmark``: the sink then writes with capture suspended, each line at column 0
(a leading newline keeps it off a progress dot's line).
"""
from __future__ import annotations

from collections.abc import Iterator

import ledger_mode
import pytest


@pytest.fixture
def ledger_terminal(capfd: pytest.CaptureFixture[str]) -> Iterator[None]:
    def sink(line: str) -> None:
        with capfd.disabled():
            print(f"\n{line}", flush=True)

    ledger_mode.set_sink(sink)
    try:
        yield
    finally:
        ledger_mode.set_sink(None)
