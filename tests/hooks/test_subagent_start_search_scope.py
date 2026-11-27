# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every subagent gets a Search scope row (a gate critic that could not find a briefed file ran ``find /`` over the whole disk).

The row is ``_ORCH_SEARCH_SCOPE``, one constant of the Orchestration table every injection carries: process, with no tracker
id in the text, for every user. ``tests/hooks/test_subagent_start_audience_run.py`` proves the row reaches the live output. That
module's neighbour mints a test tenant per test, so this pin reads the constant and the renderer and mints none (the
python_unit tenant ceiling, tests/storage_touch_ceiling.toml).
"""
from __future__ import annotations

from nexus.hooks import subagent_start


def test_the_search_scope_row_says_what_it_always_said() -> None:
    row = subagent_start._ORCH_SEARCH_SCOPE
    assert "| Search scope |" in row
    assert "Search only under the roots your brief names, or your working directory" in row
    assert "never a whole-disk or whole-home search" in row
    assert "hand back naming it" in row


def test_the_row_is_every_sessions(project_strict_repo, tmp_path) -> None:
    for cwd in (project_strict_repo, tmp_path):
        table = subagent_start._orchestration({"cwd": str(cwd)})
        assert "| Search scope |" in table
        assert "| Completion |" in table and "| Inbox |" in table


def test_the_row_names_no_tracker_id() -> None:
    import re

    assert re.search(r"\bnexus-[a-z0-9]{4,}\b", subagent_start._ORCH_SEARCH_SCOPE) is None
