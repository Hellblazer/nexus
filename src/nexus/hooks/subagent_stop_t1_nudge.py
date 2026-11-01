# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SubagentStop: nudge an agent once when its transcript shows no T1 put
(nexus-mtheb.3, Phase C).

An agent that finishes without writing to T1 leaves its caller nothing to
search for. This verb reads the agent's own transcript
(``agent_transcript_path``, one file per ``agent_id``), counts its T1 puts,
and when the count is zero gives the agent one more turn with a single
sentence telling it to write.

**Contract.** Claude Code's SubagentStop decision control
(code.claude.com/docs/en/hooks, read through Context7 on 2026-11-01):
``hookSpecificOutput.additionalContext`` with ``hookEventName`` set to
``"SubagentStop"`` is "non-error feedback that keeps the subagent running";
it goes through the same loop protections as ``decision: "block"`` (the
``stop_hook_active`` input and an 8-consecutive-continuation cap) but is
shown as hook feedback, not a hook error. The envelope has no ``decision``
key, so this is a warning and not a deciding hook; it is still wired on the
command tier because SubagentStop fires whether or not this session's MCP
connection exists (nexus-egm7p).

**Once.** ``stop_hook_active`` is true on the stop that follows a
continuation, and the verb says nothing then. The guard is stateless on
purpose: no marker file to clean up. The residual is a named agent resumed
later (a new stop episode, ``stop_hook_active`` false again) with still no
put anywhere in its transcript is nudged once more for that episode.

**Orphans.** About 95% of SubagentStop events are harness-internal and carry
no ``agent_type`` (nexus-zfxo3). ``hooks.json`` matches ``.+`` so Claude Code
never spawns this verb for them, and the verb returns silent for an empty
``agent_type`` as well, in case a payload reaches it anyway.

**Fail open.** A missing, unreadable or unparseable transcript is not
evidence of zero puts: :func:`t1_put_count` returns ``None`` for it and the
verb says nothing. Any crash is swallowed by ``never_fail``. Exit is always 0.

**Counting.** Only an assistant ``tool_use`` counts: an MCP ``scratch`` call
whose ``action`` is ``put``, or a Bash ``nx scratch put``. A put whose result
is an error (``is_error`` or the ``Error:`` prefix every ``_mcp_tool_error``
return carries) does not count; a put with no result yet does, because
absence of a result is not positive evidence of failure. Text an agent merely
read, inside a ``tool_result``, never counts.

Stdlib only at module scope: this runs on every real subagent stop, and
``src/nexus/hooks`` pays its import cost on every call.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from nexus._hook_runtime._io import HookResult, additional_context, never_fail

__all__ = ["NUDGE", "record_dispatch", "run", "t1_put_count"]

#: The one sentence the agent reads. ``<bead>`` is literal: the agent fills it.
NUDGE = (
    "You wrote nothing to T1. Put your findings in T1 "
    "(mcp__plugin_conexus_nexus__scratch action=put, tags <bead>,write-back) "
    "and return the id."
)

_ERROR_PREFIX = "Error:"
_BASH_PUT = re.compile(r"\bnx\s+scratch\s+put\b")


def _is_t1_put(name: object, tool_input: object) -> bool:
    if not isinstance(tool_input, dict):
        return False
    if name == "Bash":
        command = tool_input.get("command")
        return isinstance(command, str) and _BASH_PUT.search(command) is not None
    if isinstance(name, str) and name.startswith("mcp__") and name.rsplit("__", 1)[-1] == "scratch":
        return str(tool_input.get("action", "")).strip().lower() == "put"
    return False


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def t1_put_count(path: str) -> int | None:
    """Successful T1 puts in the transcript at *path*, or ``None``.

    ``None`` means the transcript gave no usable evidence (missing,
    unreadable, no parseable entry); the caller treats it as "do not nudge".
    A line that does not parse is skipped, so a damaged line can only remove
    puts it hid, never invent a count.
    """
    if not path or not os.path.isfile(path) or not os.access(path, os.R_OK):
        return None
    pending: set[Any] = set()
    failed: set[Any] = set()
    parsed = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            parsed += 1
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use" and entry.get("type") == "assistant":
                    if _is_t1_put(block.get("name"), block.get("input")):
                        pending.add(block.get("id"))
                elif kind == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid in pending and (
                        block.get("is_error") is True
                        or _result_text(block).lstrip().startswith(_ERROR_PREFIX)
                    ):
                        failed.add(tid)
    if parsed == 0:
        return None
    return len(pending - failed)


def record_dispatch(session_id: str, agent_id: str, agent_type: str, t1_puts: int) -> None:
    """The seam for the per-dispatch puts counter.

    TODO(nexus-mtheb.4): call the engine route that records one row per
    dispatch (payload ``session_id``, ``agent_id``, ``agent_type``,
    ``t1_puts``, ``ts``) once its client method exists on develop; none does
    yet, so this is a deliberate no-op. It is called once per real stop,
    including the re-stop after a nudge (``stop_hook_active`` true), whose
    count is the final one, so the receiver should keep the last row per
    (session_id, agent_id). Importing the engine client here would load
    ``nexus.cli``-class modules into a hook that runs on every real subagent
    stop (0.8 s measured by RDR-215 research-5); do it behind a deferred
    import, bounded well inside the hook timeout, and never let it raise.
    """


def _field(data: dict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _decide(payload: dict | None) -> HookResult:
    data = payload if isinstance(payload, dict) else {}
    agent_type = _field(data, "agent_type")
    if not agent_type:
        return HookResult()
    puts = t1_put_count(_field(data, "agent_transcript_path"))
    if puts is None:
        return HookResult()
    try:
        record_dispatch(_field(data, "session_id"), _field(data, "agent_id"), agent_type, puts)
    except Exception:  # noqa: BLE001 — a counter must never cost the nudge or the stop
        pass
    if puts > 0 or bool(data.get("stop_hook_active")):
        return HookResult()
    return HookResult(stdout=additional_context("SubagentStop", NUDGE))


def run(payload: dict | None) -> HookResult:
    """Return the nudge envelope when a real agent stops with no T1 put."""
    return never_fail(lambda: _decide(payload), "subagent-stop-t1-nudge")
