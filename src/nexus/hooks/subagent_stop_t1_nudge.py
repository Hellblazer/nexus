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

#: What the agent reads. The agent's final message is what its caller
#: receives, so the nudge asks for the full report again, not the id alone
#: (review r1 of fd1ca12a5).
NUDGE = (
    "You wrote nothing to T1. Put your findings in T1 "
    "(mcp__plugin_conexus_nexus__scratch action=put, tagged with your bead id and write-back), "
    "then repeat your complete final report unchanged, ending with the T1 id."
)

#: Agent types (the part after any ``plugin:`` prefix) that have no T1 tool
#: or whose return is parsed as a fixed format: a nudge only wastes their turn
#: or breaks the parse (review r1 of fd1ca12a5).
NO_T1_AGENTS: frozenset[str] = frozenset({
    "web-fetch",
    "statusline-setup",
    "claude-code-guide",
    "agent-creator",
    "skill-reviewer",
    "plugin-validator",
    "line-editor",
})

_ERROR_PREFIX = "Error:"
#: ``nx scratch put`` as a command, at the start of the command line or after
#: ``;``, ``&``, ``|``, ``(``, a backtick, ``{``, ``then``, ``do`` or a newline, optionally
#: behind ``VAR=value`` assignments, a wrapper (``sudo``, ``env``, ``time``,
#: ``nohup``, ``timeout N``, ``uv run``) and a path to ``nx``; the same words inside a quoted argument (``echo "nx scratch
#: put"``, ``grep -rn "nx scratch put" docs/``) are not a put.
_BASH_PUT = re.compile(
    r"(?:^|[;&|(`{\n]|\bthen\b|\bdo\b)\s*"  # a command position
    # VAR=value assignments. The three value forms start with different
    # characters (a double quote, a single quote, anything else), so a value
    # has one parse; with a bare \S* as the third form, a quoted value matched
    # two ways and a run of `"" 0=` backtracked exponentially (CodeQL
    # py/redos on PR #1637).
    r"(?:\w+=(?:\"[^\"]*\"\S*|'[^']*'\S*|(?:[^\s\"']\S*)?)\s+)*"
    r"(?:(?:sudo|env|time|nohup|timeout\s+\S+|uv\s+run)\s+)*"  # wrapper words
    r"(?:\S*/)?nx\s+scratch\s+put\b"  # nx, bare or by path
)


def _is_t1_put(name: object, tool_input: object) -> bool:
    if not isinstance(tool_input, dict):
        return False
    if name == "Bash":
        command = tool_input.get("command")
        return isinstance(command, str) and _BASH_PUT.search(command) is not None
    if isinstance(name, str) and name.startswith("mcp__") and name.endswith("__scratch"):
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
    """Record this dispatch's T1 put count in the engine (nexus-mtheb.4).

    Hands the row to a detached child (:mod:`nexus.hooks.agent_dispatch_record`)
    and returns at once, so the hook never waits on the network; the child
    makes one bounded request. Called once per real stop, including the
    re-stop after a nudge (``stop_hook_active`` true), whose count is the
    final one; the engine upsert keeps the larger count per (session_id,
    agent_id). An agent type in ``NO_T1_AGENTS`` is never recorded: it cannot
    put, so it would only dilute the puts-per-dispatch measure.
    """
    from nexus.hooks import agent_dispatch_record  # noqa: PLC0415 — stdlib-only at import; deferred so a stop that records nothing pays nothing

    agent_dispatch_record.spawn(session_id, agent_id, agent_type, t1_puts)


def _field(data: dict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _decide(payload: dict | None) -> HookResult:
    data = payload if isinstance(payload, dict) else {}
    agent_type = _field(data, "agent_type")
    if not agent_type or agent_type.rsplit(":", 1)[-1] in NO_T1_AGENTS:
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
