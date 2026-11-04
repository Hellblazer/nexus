# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-egv6q: every subagent gets a Search scope row (a gate critic that could not find a briefed
file ran ``find /`` over the whole disk).

The row lives in ``_ORCH``, the Orchestration table that every injection carries;
``tests/hooks/test_subagent_start_hook.py::test_orchestration_directive_rows_injected`` proves
``_ORCH`` reaches the live output. That module mints a test tenant per test, so this pin reads the
constant instead and mints none (the python_unit tenant ceiling, tests/storage_touch_ceiling.toml).
"""
from __future__ import annotations

from nexus.hooks import subagent_start


def test_the_orchestration_table_carries_the_search_scope_row() -> None:
    row = subagent_start._ORCH
    assert "| Search scope |" in row
    assert "Search only under the roots your brief names, or your working directory" in row
    assert "never a whole-disk or whole-home search" in row
    assert "hand back naming it" in row
