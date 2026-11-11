# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.7: a tuple tool's unregistered-subspace error names the tool that lists the registered ones.

The probe battery (scripts/agent_error_probes.py) found the one case where today's error text left an
agent stuck: told only "no template registered for subspace 'ledger/probe-v0174'", it called a tool that
does not exist (``tuple_templates``) in 3 runs of 3. Sam's ruling 2026-11-10 (relayed by halcyon): keep only
this fix from the errors half of the bead. The engine's text stays first and unchanged.
"""

from __future__ import annotations

from nexus.db.t2.http_tuple_store import SchemaViolationError, UnknownSubspaceError
from nexus.mcp import core
from nexus.mcp._tool_failure import is_failure

_ENGINE_TEXT = "no template registered for subspace 'ledger/probe-v0174'"


def test_a_tuple_tools_unknown_subspace_error_names_tuple_registry() -> None:
    out = core._mcp_tool_error("tuple_rd", UnknownSubspaceError(_ENGINE_TEXT))
    assert is_failure(out)
    assert out.startswith(f"Error: {_ENGINE_TEXT}"), out
    assert "tuple_registry" in out


def test_other_tuple_errors_and_non_tuple_tools_are_unchanged() -> None:
    assert core._mcp_tool_error("tuple_rd", SchemaViolationError("bad keys")) == "Error: bad keys"
    # mailbox_send's subspace is built from a name: listing templates is not the fix there
    assert "tuple_registry" not in core._mcp_tool_error("mailbox_send", UnknownSubspaceError(_ENGINE_TEXT))
