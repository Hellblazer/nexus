# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F4 (nexus-rfvyy.7.11): the nested nx-mcp servers a tool-needing ``claude -p`` child
starts carry ``NX_SENSITIVITY_POSTURE=llm_bound`` in the env block the grant hands them, so their reads exclude
sensitive chunks whether or not Claude Code passes the parent's environment through (RDR-243 assumption A5).
Tested on the grant itself; no ``claude`` is launched."""
from __future__ import annotations

import pytest

from nexus.mcp.core import _subprocess_tool_grant
from nexus.sensitivity import POSTURE_ENV


@pytest.mark.parametrize("plugin_root", [None, "/opt/plugin"])
def test_every_server_in_the_grant_carries_the_llm_bound_posture(monkeypatch, plugin_root) -> None:
    if plugin_root is None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", plugin_root)
    servers, _allowed = _subprocess_tool_grant()

    assert servers, "the grant names servers: an empty one would make this vacuous"
    for name, spec in servers.items():
        assert spec.get("env", {}).get(POSTURE_ENV) == "llm_bound", f"{name} server env lacks the posture"
    if plugin_root:
        assert all(spec["env"]["CLAUDE_PLUGIN_ROOT"] == plugin_root for spec in servers.values())
