# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``session-start`` hook verb (RDR-215 MVV port B, bead nexus-q02nx.5).

This is the first real port onto the command tier's dispatch mechanism
(``nexus._hook_runtime.entry``, bead nexus-q02nx.2): the SessionStart entry that
already reads a JSON stdin payload today, via ``nexus.commands.hook``'s
Click verb (``nx hook session-start``). RDR-215 Approach item 4 ("one
implementation, two entries") applies here in its narrowest form -- this
module's :func:`run` is what a future ``hook_session_start`` tool-tier
registration would call too, though ``SessionStart`` never reaches the tool
tier at all (Approach item 1: it fires before any MCP server is
guaranteed connected), so today only ``nx-hook`` calls it.

**Payload contract (T2 ``nexus_rdr/215-hook-contract-map``, "Registration
pattern" section).** ``session-start`` is the only hook this epic ports that
reads BOTH ``session_id`` and ``source`` out of one stdin payload -- a
stream can only be read once (nexus-d76vc). Unlike the Click verb, this
module does not read stdin itself: ``nexus._hook_runtime.entry.main`` already calls
:func:`nexus._hook_runtime._io.read_payload` before dispatching to :func:`run`, and
that reader's TTY-aware/empty/malformed contract is byte-for-byte the same
one ``nexus.commands.hook._read_stdin_payload`` implements (both were built
against the same nexus-rv2x repro) -- so ``run`` here only has to do the
same field extraction ``session_start_cmd`` performs on the dict it gets.

**Not a ledger verb.** ``session-start`` never appears in
:data:`nexus._hook_runtime.entry.LEDGER_VERBS`: it has no caller that branches on
an exit code (RDR-215 Contracts), so :class:`~nexus._hook_runtime._io.HookResult`'s
default ``exit_code=0`` is exactly right, and ``entry.main`` forces 0 for
every non-ledger verb regardless.
"""
from __future__ import annotations

from nexus import hooks
from nexus._hook_runtime._io import HookResult, configure_hook_logging


def run(payload: dict | None) -> HookResult:
    """Run the SessionStart hook from an already-parsed stdin *payload*.

    Mirrors ``nexus.commands.hook.session_start_cmd``'s field extraction
    exactly (same ``isinstance`` guards, same two fields, same call), so the
    two entries produce identical output for identical input -- the parity
    the bead's own verification step drives with a real payload on stdin
    through both ``nx-hook session-start`` and ``nx hook session-start``.
    """
    # nexus.hooks logs through a module-scope ``structlog.get_logger()`` this
    # verb does not own, and an unconfigured structlog writes to stdout. This
    # verb already pays for structlog by importing nexus.hooks at all, so the
    # sink is free here -- unlike in nx-hook's main(), where it charged every
    # stdlib-only verb the same 0.06 s (nexus-br31l).
    configure_hook_logging()

    session_id: str | None = None
    source: str | None = None
    if payload is not None:
        sid = payload.get("session_id")
        session_id = sid if isinstance(sid, str) and sid else None
        src = payload.get("source")
        source = src if isinstance(src, str) and src else None
    output = hooks.session_start(claude_session_id=session_id, source=source)
    return HookResult(stdout=output)
