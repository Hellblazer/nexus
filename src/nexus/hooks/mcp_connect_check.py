# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-hook mcp-connect-check``: a registered, silent no-op (nexus-qxyqz).

**What it was.** RDR-215 (nexus-veh77, round 5) wired this verb on
``UserPromptSubmit`` to warn, once per episode, "nx-mcp is not connected to
this session; conexus tool-tier hooks are being skipped" when the session's
connect marker (``nexus.mcp.connect_marker``) named a pid that was no longer
alive.

**Why it was deleted.** The warning was cosmetic: nothing branched on it, it
gated no hook, and the ``mcp_tool``-tier hooks it talked about are skipped or
not by Claude Code itself, whatever this verb printed. And it was wrong. The
marker is keyed on a session id and its owner is one process, and each of
these made a live, answering ``nx-mcp`` read as disconnected: a nested
``claude -p`` dispatch server or the ``nx doctor`` probe publishing under the
real session's id and then deleting the marker on exit, a ``/mcp`` reconnect
whose old server tore down after the new one published, and ``/clear`` or
``/resume`` moving the hook's session id without moving the marker. Making the
marker a reliable liveness signal (owner election, heartbeat, locking, expiry,
handoff tracking) cost far more than a courtesy note is worth, so the warning
went. The marker itself stays, for ``nexus.hooks.mcp_connect_wait``, the
startup barrier, which only asks whether it has appeared.

**Why the verb still exists.** Published plugins still name it in their
``hooks.json``, and ``tests/e2e/hook-cli-skew`` (with the hook-cli-skew gate)
fires every ``hooks.json`` entry against every CLI a user may still have. An
unknown verb exits 2, which blocks a ``UserPromptSubmit``. So it stays in
:data:`nexus._hook_runtime.entry.VERB_TABLE`, exits 0 and writes nothing.
This module must not import ``nexus.mcp`` or structlog: ``UserPromptSubmit``
fires on every prompt, and importing ``nexus.mcp`` here cost 270-300 ms per
prompt (it eagerly imports the whole MCP server, and structlog with it).

Per-session ``mcp_connect_check_state.<session_id>`` files written by earlier
versions are no longer read or written; they are inert and left on disk.
"""
from __future__ import annotations

from nexus._hook_runtime._io import HookResult


def run(payload: dict | None) -> HookResult:
    """Do nothing, successfully. See the module docstring."""
    return HookResult(stdout=None)
