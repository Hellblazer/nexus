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
run_start/run_stop... a naive port drops every row." There is nothing to
detach from: the whole point of wiring this on the command tier with
``"async": true`` in ``hooks.json`` (the pre-9b1081514 shape, restored
here) is that CLAUDE CODE runs the *process* in the background and does
not wait on it -- the in-process daemon-thread trick this module's sibling
needs for the mcp_tool tier is redundant one layer up.

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

**ACCEPTED RESIDUAL, not reopened by this bead (review round, nexus-egm7p):
``claude -p`` kills a still-running ``"async": true`` command hook at
session teardown, no grace.** Claude Code's own hooks docs, "Run hooks in
the background > Configure an async hook", state this explicitly for
non-interactive mode. So a SubagentStart that fires as the LAST act of a
short-lived ``-p`` invocation can have its START projection killed
mid-flight, same as any other async hook. This is not new to this move:
RDR-205 already researched and priced in exactly this loss mode for
these two projections specifically (``docs/rdr/rdr-205-linda-tuple-space-
over-postgres.md`` line 231, "``async: true`` hooks are never read, never
timed out, and killed without grace at session end", and lines 1136-1137,
"Silent, recorded: an async projection hook is killed without trace at
session end, so the space can be behind the TSV") -- this bead restores
the SAME shape the RDR's own research covered (the pre-9b1081514 async
wiring), not a new one. The RDR-184 ``.expectations`` TSV ledger, written
synchronously by a different hook on a different event, stays the
authoritative record either way; this projection is a best-effort
secondary view, never the thing anything correctness-sensitive reads.

``project()``'s own ``_POST_TIMEOUT_S = 5`` bound is the number that
actually matters here. This entry's ``hooks.json`` shape carries no
``"timeout"`` key at all (dropped from the mcp_tool-tier entry's carried-
over value, review round, nexus-egm7p): Claude Code does not enforce a
timeout on an ``"async": true`` command hook (same docs section as
above), so the key would have bounded nothing while reading as if it
did. Nothing in ``tests/test_hooks_json_shape_lint.py`` requires one
either.
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
