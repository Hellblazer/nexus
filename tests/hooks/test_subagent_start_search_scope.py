# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-egv6q: a maintainer's subagent gets a Search scope row (a gate critic that could not find a briefed
file ran ``find /`` over the whole disk).

The row is ``_ORCH_SEARCH_SCOPE``, one constant of the Orchestration table every injection carries.
nexus-sby49.3 (D10) made it SAM-WIDE: a user keeps the ``whole_disk_scan`` advisory, which fires for everyone.
``tests/hooks/test_subagent_start_hook.py::test_a_marked_nexus_checkout_gets_the_git_and_search_scope_rows`` proves the
row reaches the live output. That module mints a test tenant per test, so this pin reads the constant and the
renderer and mints none (the python_unit tenant ceiling, tests/storage_touch_ceiling.toml).
"""
from __future__ import annotations

from nexus.hooks import subagent_start


def test_the_search_scope_row_says_what_it_always_said() -> None:
    row = subagent_start._ORCH_SEARCH_SCOPE
    assert "| Search scope |" in row
    assert "Search only under the roots your brief names, or your working directory" in row
    assert "never a whole-disk or whole-home search" in row
    assert "hand back naming it" in row


def test_the_row_is_the_maintainers(nexus_marked_repo) -> None:
    assert "| Search scope |" in subagent_start._orchestration({"cwd": str(nexus_marked_repo)})


def test_a_user_does_not_get_the_row(tmp_path) -> None:
    table = subagent_start._orchestration({"cwd": str(tmp_path)})
    assert "| Search scope |" not in table
    assert "| Completion |" in table and "| Inbox |" in table
