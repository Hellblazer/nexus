#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The conexus ``sequential-thinking`` MCP server: standard library only.

A port of ``@modelcontextprotocol/server-sequential-thinking``'s one tool,
``sequentialthinking``, with the same name, field names and result JSON, so
the tool name every skill and agent cites
(``mcp__plugin_conexus_sequential-thinking__sequentialthinking``) is unchanged.
Synced with upstream 2.2.0 (2026-11-18): its annotations and its history bound.
Two differences are deliberate. The tool and field descriptions are reworded
(GH #1661, see TOOL_DESCRIPTION). The protocol list stops at 2025-06-18:
upstream also serves 2025-11-25 and the 2026-07-28 ``server/discover`` era, and
Claude Code still connects here through ``initialize``.

Why it is not the npm package any more (nexus-f9bgu): conexus/.mcp.json used to
start it with ``npx -y``. That needs Node.js, which conexus does not otherwise
need and a clean Windows install does not have, so on a clean Windows 11 box the
server failed to connect in every session. A shell-free ``npx`` also cannot be
spawned on Windows at all (it is ``npx.cmd``; the documented workaround,
``cmd /c npx``, cannot be written in an .mcp.json shared with macOS and Linux).
conexus already requires uv and Python 3.12+, so the server runs on those.

Transport: MCP stdio, newline-delimited JSON-RPC 2.0 on stdin/stdout. It writes
nothing to stderr: Claude Code records every stderr line of a stdio server as an
``error`` entry in its MCP log, so the upstream startup banner and per-thought
echo would read as faults there, and so would a traceback: input that is not
UTF-8, too deeply nested, or too long to parse gets a JSON-RPC error, and a
client that stops reading ends the server quietly. Like the upstream server, it
keeps the step history in process memory for the session's life, bounded as
upstream bounds it (DEFAULT_MAX_THOUGHT_HISTORY).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# The stdio loop is shared with the setup stand-in (conexus/launch/nx_standin.py) and
# lives beside it, in conexus/launch/: both are started from the plugin directory, so
# this finds it from this file's own location and not from the working directory or
# sys.path[0] (an interpreter run with -I or PYTHONSAFEPATH has neither). No .pyc is
# written into the plugin directory.
sys.dont_write_bytecode = True
_LAUNCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launch")
if _LAUNCH_DIR not in sys.path:
    sys.path.insert(0, _LAUNCH_DIR)
import _stdio_loop  # noqa: E402 -- must follow the sys.path insert

SERVER_NAME: str = "sequential-thinking-server"
SERVER_VERSION: str = "1.0.0"
#: Protocol revisions this server speaks; the newest is offered when the client
#: asks for one it does not know (MCP lifecycle: the server answers with a
#: version it supports and the client decides whether to continue).
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

TOOL_NAME: str = "sequentialthinking"

#: The description differs from upstream's on purpose (GH #1661): the API safety classifier stops requests
#: with ``[reasoning_extraction]``, and upstream's text frames the tool as a way to write out the model's own
#: thinking ("reflective problem-solving through thoughts", "your current thinking step"). The tool records
#: steps of the work instead. Like upstream 2.2.0 it carries only when and how to call the tool and stays under
#: 1024 characters, the cap OpenAI and Azure OpenAI put on a tool description; each field is explained in its
#: own input-schema description. The tool and field names are upstream's, so every caller still works.
TOOL_DESCRIPTION: str = """A step log for structured multi-step work. Record one step per call: a sub-question, a finding with its evidence, a hypothesis to test, a decision, or a correction to an earlier step. The server keeps the steps and their branches and returns only counts.

When to use this tool:
- Breaking a complex task into ordered steps
- Planning and design that may need correcting
- Tracking hypotheses and the evidence for or against each
- Tasks whose full scope is not clear at the start
- Keeping a sequence of decisions in order over many tool calls

How to use it:
1. Estimate totalThoughts up front and adjust it as you go; thoughtNumber may exceed it
2. Correct an earlier step (isRevision with revisesThought), or start an alternative path (branchFromThought with branchId)
3. Record uncertainty, and leave out information irrelevant to the current step
4. Test each hypothesis against the evidence recorded so far
5. Set nextThoughtNeeded to false only when the task's answer is reached"""

_BOOL_OR_STRING: dict[str, Any] = {"type": ["boolean", "string"]}
_POSITIVE_INT: dict[str, Any] = {"type": "integer", "minimum": 1}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string",
            "description": "This step's content: a sub-question, a finding with its evidence, a hypothesis, a decision, or a correction",
        },
        "nextThoughtNeeded": {**_BOOL_OR_STRING, "description": "Whether another step follows"},
        "thoughtNumber": {
            **_POSITIVE_INT,
            "description": "This step's number (numeric value, e.g., 1, 2, 3)",
        },
        "totalThoughts": {
            **_POSITIVE_INT,
            "description": "Estimated number of steps (numeric value, e.g., 5, 10)",
        },
        "isRevision": {**_BOOL_OR_STRING, "description": "Whether this step corrects an earlier step"},
        "revisesThought": {**_POSITIVE_INT, "description": "Which step this one corrects"},
        "branchFromThought": {**_POSITIVE_INT, "description": "The step this branch starts from"},
        "branchId": {"type": "string", "description": "Branch identifier"},
        "needsMoreThoughts": {**_BOOL_OR_STRING, "description": "Whether more steps are needed than estimated"},
    },
    "required": ["thought", "nextThoughtNeeded", "thoughtNumber", "totalThoughts"],
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thoughtNumber": {"type": "number"},
        "totalThoughts": {"type": "number"},
        "nextThoughtNeeded": {"type": "boolean"},
        "branches": {"type": "array", "items": {"type": "string"}},
        "thoughtHistoryLength": {"type": "number"},
    },
    "required": ["thoughtNumber", "totalThoughts", "nextThoughtNeeded", "branches", "thoughtHistoryLength"],
}

TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "title": "Sequential Step Log",
    "description": TOOL_DESCRIPTION,
    "inputSchema": INPUT_SCHEMA,
    "outputSchema": OUTPUT_SCHEMA,
    # As upstream (#4721): every call appends to the in-memory history, and a call with both branchFromThought
    # and branchId also appends to that branch, so the tool is neither read-only nor idempotent.
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
}


class InvalidArguments(ValueError):
    """A tool argument failed validation; the message names the field."""


def _as_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise InvalidArguments(f'{name}: expected boolean or "true"/"false" string, received {value!r}')


def _as_positive_int(name: str, value: Any) -> int:
    # Upstream coerces with z.coerce.number().int().min(1): "3" and 3.0 pass.
    if isinstance(value, bool):
        raise InvalidArguments(f"{name}: expected an integer >= 1, received {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an int too big for a float
        raise InvalidArguments(f"{name}: expected an integer >= 1, received {value!r}") from None
    if not number.is_integer() or number < 1:
        raise InvalidArguments(f"{name}: expected an integer >= 1, received {value!r}")
    return int(number)


#: Upstream 2.2.0's bound on the history (``MAX_THOUGHT_HISTORY``, default 1000): past it the oldest step is
#: dropped, from its branch too, so a long session's memory stays bounded. A value that is not a positive integer
#: falls back to the default; upstream says so on stderr, this server writes nothing there (module docstring).
DEFAULT_MAX_THOUGHT_HISTORY: int = 1000


def max_thought_history(environ: dict[str, str] | None = None) -> int:
    raw = (os.environ if environ is None else environ).get("MAX_THOUGHT_HISTORY", "").strip()
    return int(raw) if raw.isdigit() and int(raw) >= 1 else DEFAULT_MAX_THOUGHT_HISTORY


class SequentialThinking:
    """Step history and branches for one server process (one session)."""

    def __init__(self, max_history: int | None = None) -> None:
        self.thought_history: list[dict[str, Any]] = []
        self.branches: dict[str, list[dict[str, Any]]] = {}
        self.max_history = max_thought_history() if max_history is None else max_history

    def process(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Record one thought; return the upstream result object."""
        if not isinstance(arguments, dict):
            raise InvalidArguments("arguments: expected an object")
        thought = arguments.get("thought")
        if not isinstance(thought, str):
            raise InvalidArguments(f"thought: expected a string, received {thought!r}")
        for required in ("nextThoughtNeeded", "thoughtNumber", "totalThoughts"):
            if required not in arguments:
                raise InvalidArguments(f"{required}: required")
        data: dict[str, Any] = {
            "thought": thought,
            "thoughtNumber": _as_positive_int("thoughtNumber", arguments["thoughtNumber"]),
            "totalThoughts": _as_positive_int("totalThoughts", arguments["totalThoughts"]),
            "nextThoughtNeeded": _as_bool("nextThoughtNeeded", arguments["nextThoughtNeeded"]),
        }
        for key in ("isRevision", "needsMoreThoughts"):
            if arguments.get(key) is not None:
                data[key] = _as_bool(key, arguments[key])
        for key in ("revisesThought", "branchFromThought"):
            if arguments.get(key) is not None:
                data[key] = _as_positive_int(key, arguments[key])
        if arguments.get("branchId") is not None:
            if not isinstance(arguments["branchId"], str):
                raise InvalidArguments(f"branchId: expected a string, received {arguments['branchId']!r}")
            data["branchId"] = arguments["branchId"]

        if data["thoughtNumber"] > data["totalThoughts"]:
            data["totalThoughts"] = data["thoughtNumber"]
        self.thought_history.append(data)
        if data.get("branchFromThought") and data.get("branchId"):
            self.branches.setdefault(data["branchId"], []).append(data)
        while len(self.thought_history) > self.max_history:
            oldest = self.thought_history.pop(0)
            branch = self.branches.get(oldest.get("branchId", ""))
            if branch is not None and oldest in branch:
                branch.remove(oldest)
                if not branch:
                    del self.branches[oldest["branchId"]]
        return {
            "thoughtNumber": data["thoughtNumber"],
            "totalThoughts": data["totalThoughts"],
            "nextThoughtNeeded": data["nextThoughtNeeded"],
            "branches": list(self.branches),
            "thoughtHistoryLength": len(self.thought_history),
        }


def handle(state: SequentialThinking, message: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; None for a notification or a response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _stdio_loop.error(None, -32600, "Invalid Request")
    method = message.get("method")
    if "id" not in message or message.get("id") is None:
        return None  # a notification (initialized, cancelled, ...) or a stray response
    request_id = message["id"]
    if not isinstance(method, str):
        return None if ("result" in message or "error" in message) else _stdio_loop.error(request_id, -32600, "Invalid Request")
    params = message.get("params") or {}
    match method:
        case "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            version = asked if asked in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            return _stdio_loop.result(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        case "ping":
            return _stdio_loop.result(request_id, {})
        case "tools/list":
            return _stdio_loop.result(request_id, {"tools": [TOOL]})
        case "tools/call":
            if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
                name = params.get("name") if isinstance(params, dict) else None
                return _stdio_loop.error(request_id, -32602, f"Unknown tool: {name!r}")
            try:
                payload = state.process(params.get("arguments") or {})
            except InvalidArguments as exc:
                text = json.dumps({"error": str(exc), "status": "failed"}, indent=2)
                return _stdio_loop.result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})
            return _stdio_loop.result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                    "structuredContent": payload,
                },
            )
        case _:
            return _stdio_loop.error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    state = SequentialThinking()
    # ``handle`` is looked up at each message, so a test can replace it.
    return _stdio_loop.serve_stdio(lambda message: handle(state, message))


if __name__ == "__main__":
    raise SystemExit(main())
