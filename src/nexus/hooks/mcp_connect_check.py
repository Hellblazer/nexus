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
``hooks.json``. An old plugin on a current CLI is the case that matters: were
the verb removed from :data:`nexus._hook_runtime.entry.VERB_TABLE`,
``entry.main`` would exit 0 but write a ``systemMessage`` ("conexus plugin is
ahead of the installed nx CLI ... Run `nx upgrade`") on EVERY prompt, a
misleading nag about a skew that does not exist. (An unknown verb only exits 2,
which blocks a prompt, on the 7.55 to 7.57 CLIs, and the plugin's
``nx_hook_shim.py`` already turns that into 0.) So it stays registered, exits 0
and writes nothing. ``tests/e2e/hook-cli-skew`` fires only the CURRENT
``hooks.json``, which no longer names this verb, so it does not cover that
direction; ``tests/hooks/test_mcp_connect_check_verb.py::TestSilentNoOp::
test_registered_in_the_verb_table`` is the pin for it.
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
