# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-205 ledger START projection, on the command tier (bead nexus-egm7p).

**Why this exists beside ``nexus.hooks.tuple_projection``, not folded into
it.** That module's ``run_start``/``run_stop`` are the ``mcp_tool``-tier
implementations: each spawns the real projection
(:func:`nexus.hooks.tuple_ledger_project.project`) on an OUTER daemon
thread so the tool call returns immediately, and its own docstring records
the tradeoff plainly -- "nothing in this process outlives the interpreter;
a daemon thread dies at interpreter exit," so a server that stops between
the event and the thread's own HTTP POST loses that projection.

That tradeoff was fine for a long-lived ``nx-mcp`` server process, where the
interpreter keeps running long past any one hook call. It is WRONG for a
command-tier verb: ``nx-hook``'s own process IS the unit of work, and it
exits (by design -- see ``nexus._hook_runtime.entry``'s docstring) the
instant its verb's ``run()`` returns. Spawning a daemon thread here and
returning immediately would make the row's fate a coin flip on process
scheduling, dropping it far more often than the mcp_tool path did.

So this module calls :func:`~nexus.hooks.tuple_ledger_project.project`
DIRECTLY and SYNCHRONOUSLY -- the analysis this bead is built from names
this explicitly: "the verb calls project() synchronously. Do NOT call
run_start/run_stop... a naive port drops every row."

The hook itself is SYNCHRONOUS in ``hooks.json`` (no ``"async"`` key,
nexus-wgalh): Claude Code waits for this process before the subagent
starts, so the POST completes or times out inside the hook's own lifetime.

**Why command tier at all, when the mcp_tool registration still works.**
Analysis (T2 ``nexus/analysis-nexus-egm7p-rdr205-projection``): no RDR-205
consumer depends on ledger completeness for a verdict (the TSV stays the
write-ahead record; RDR-205 already treats a projection loss as "silent,
recorded"), but an ``mcp_tool`` hook's very INVOCATION depends on this
session's ``plugin:conexus:nexus`` MCP connection being up -- exactly the
hazard nexus-5l8i8 fixed for the RDR-184 ledger's two writers. A disconnect
during the ~10-minute window before this session's own MCP server
reconnects drops the START row silently, with no SKIP line anywhere (the
call to ``project()`` never happens at all), while ``nx doctor``'s
tuple-projection row reads "no SKIPs recorded" as healthy. The command
tier has no such dependency: it is a plain subprocess the harness spawns
directly, MCP session or none.

Keeps the ``hook_subagent_start_tuple`` MCP tool registration in
``nexus.mcp.hooks.HOOK_TOOLS`` for diagnosis; only which tier ``hooks.json``
WIRES moved.

**Synchronous by decision (Sam, 2026-09-27, nexus-wgalh).** nexus-egm7p
first wired this ``"async": true``, the pre-9b1081514 shape RDR-205 CA 4
chose so projection never delays dispatch. Claude Code kills a still-running
async command hook at ``claude -p`` teardown ("Run hooks in the background >
Configure an async hook"), which RDR-205 had accepted as a silent, recorded
loss (``docs/rdr/rdr-205-linda-tuple-space-over-postgres.md`` ~231,
~1136-1137). Measured under ``claude -p`` with a 30 s SubagentStop hook: the
async run exited after 11 s and its write never happened; the synchronous run
waited and its write landed. So running synchronously removes the loss.

The cost is this process's wall time on every SubagentStart: about 0.16 s
warm, 0.4 s cold. The worst case is longer. ``project()`` may POST twice (a
retry after a below-floor engine's schema refusal), each bounded at
``_POST_TIMEOUT_S = 5``, after two interpreter starts (the shim and
``nx-hook``). The ``hooks.json`` ``"timeout": 20`` covers that. If it ever
fires, Claude Code cancels the hook and proceeds as if it had allowed, so a
timeout costs the row, never the dispatch. The RDR-184
``.expectations`` TSV ledger stays the authoritative record either way.
"""
from __future__ import annotations

from nexus._hook_runtime._io import HookResult
from nexus.hooks import tuple_ledger_project

__all__ = ["run"]


def run(payload: dict | None) -> HookResult:
    """Project this subagent's START tuple, synchronously, in this process.

    Always returns a silent, exit-0 result: :func:`~nexus.hooks.tuple_ledger_project.project`
    never raises, and every failure path it hits (unresolvable endpoint, no
    fresh data-token lease, transport failure) is logged to its own
    per-session log file rather than surfaced here.
    """
    tuple_ledger_project.project("start", payload)
    return HookResult()
