#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The conexus ``sequential-thinking`` MCP server: standard library only.

A port of ``@modelcontextprotocol/server-sequential-thinking``'s one tool,
``sequentialthinking``, with the same name, input schema and result JSON, so
the tool name every skill and agent cites
(``mcp__plugin_conexus_sequential-thinking__sequentialthinking``) is unchanged.

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
echo would read as faults there. Like the upstream server, it keeps the thought
history in process memory for the session's life.
"""

from __future__ import annotations

import json
import sys
from typing import Any

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

TOOL_DESCRIPTION: str = """A detailed tool for dynamic and reflective problem-solving through thoughts.
This tool helps analyze problems through a flexible thinking process that can adapt and evolve.
Each thought can build on, question, or revise previous insights as understanding deepens.

When to use this tool:
- Breaking down complex problems into steps
- Planning and design with room for revision
- Analysis that might need course correction
- Problems where the full scope might not be clear initially
- Problems that require a multi-step solution
- Tasks that need to maintain context over multiple steps
- Situations where irrelevant information needs to be filtered out

Key features:
- You can adjust total_thoughts up or down as you progress
- You can question or revise previous thoughts
- You can add more thoughts even after reaching what seemed like the end
- You can express uncertainty and explore alternative approaches
- Not every thought needs to build linearly - you can branch or backtrack
- Generates a solution hypothesis
- Verifies the hypothesis based on the Chain of Thought steps
- Repeats the process until satisfied
- Provides a correct answer

Parameters explained:
- thought: Your current thinking step, which can include:
  * Regular analytical steps
  * Revisions of previous thoughts
  * Questions about previous decisions
  * Realizations about needing more analysis
  * Changes in approach
  * Hypothesis generation
  * Hypothesis verification
- nextThoughtNeeded: True if you need more thinking, even if at what seemed like the end
- thoughtNumber: Current number in sequence (can go beyond initial total if needed)
- totalThoughts: Current estimate of thoughts needed (can be adjusted up/down)
- isRevision: A boolean indicating if this thought revises previous thinking
- revisesThought: If is_revision is true, which thought number is being reconsidered
- branchFromThought: If branching, which thought number is the branching point
- branchId: Identifier for the current branch (if any)
- needsMoreThoughts: If reaching end but realizing more thoughts needed

You should:
1. Start with an initial estimate of needed thoughts, but be ready to adjust
2. Feel free to question or revise previous thoughts
3. Don't hesitate to add more thoughts if needed, even at the "end"
4. Express uncertainty when present
5. Mark thoughts that revise previous thinking or branch into new paths
6. Ignore information that is irrelevant to the current step
7. Generate a solution hypothesis when appropriate
8. Verify the hypothesis based on the Chain of Thought steps
9. Repeat the process until satisfied with the solution
10. Provide a single, ideally correct answer as the final output
11. Only set nextThoughtNeeded to false when truly done and a satisfactory answer is reached"""

_BOOL_OR_STRING: dict[str, Any] = {"type": ["boolean", "string"]}
_POSITIVE_INT: dict[str, Any] = {"type": "integer", "minimum": 1}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "description": "Your current thinking step"},
        "nextThoughtNeeded": {**_BOOL_OR_STRING, "description": "Whether another thought step is needed"},
        "thoughtNumber": {
            **_POSITIVE_INT,
            "description": "Current thought number (numeric value, e.g., 1, 2, 3)",
        },
        "totalThoughts": {
            **_POSITIVE_INT,
            "description": "Estimated total thoughts needed (numeric value, e.g., 5, 10)",
        },
        "isRevision": {**_BOOL_OR_STRING, "description": "Whether this revises previous thinking"},
        "revisesThought": {**_POSITIVE_INT, "description": "Which thought is being reconsidered"},
        "branchFromThought": {**_POSITIVE_INT, "description": "Branching point thought number"},
        "branchId": {"type": "string", "description": "Branch identifier"},
        "needsMoreThoughts": {**_BOOL_OR_STRING, "description": "If more thoughts are needed"},
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
    "title": "Sequential Thinking",
    "description": TOOL_DESCRIPTION,
    "inputSchema": INPUT_SCHEMA,
    "outputSchema": OUTPUT_SCHEMA,
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
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
    except (TypeError, ValueError):
        raise InvalidArguments(f"{name}: expected an integer >= 1, received {value!r}") from None
    if not number.is_integer() or number < 1:
        raise InvalidArguments(f"{name}: expected an integer >= 1, received {value!r}")
    return int(number)


class SequentialThinking:
    """Thought history and branches for one server process (one session)."""

    def __init__(self) -> None:
        self.thought_history: list[dict[str, Any]] = []
        self.branches: dict[str, list[dict[str, Any]]] = {}

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
        return {
            "thoughtNumber": data["thoughtNumber"],
            "totalThoughts": data["totalThoughts"],
            "nextThoughtNeeded": data["nextThoughtNeeded"],
            "branches": list(self.branches),
            "thoughtHistoryLength": len(self.thought_history),
        }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(state: SequentialThinking, message: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; None for a notification or a response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method = message.get("method")
    if "id" not in message or message.get("id") is None:
        return None  # a notification (initialized, cancelled, ...) or a stray response
    request_id = message["id"]
    if not isinstance(method, str):
        return None if ("result" in message or "error" in message) else _error(request_id, -32600, "Invalid Request")
    params = message.get("params") or {}
    match method:
        case "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            version = asked if asked in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            return _result(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        case "ping":
            return _result(request_id, {})
        case "tools/list":
            return _result(request_id, {"tools": [TOOL]})
        case "tools/call":
            if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
                name = params.get("name") if isinstance(params, dict) else None
                return _error(request_id, -32602, f"Unknown tool: {name!r}")
            try:
                payload = state.process(params.get("arguments") or {})
            except InvalidArguments as exc:
                text = json.dumps({"error": str(exc), "status": "failed"}, indent=2)
                return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                    "structuredContent": payload,
                },
            )
        case _:
            return _error(request_id, -32601, f"Method not found: {method}")


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    state = SequentialThinking()
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = _error(None, -32700, "Parse error")
        else:
            reply = handle(state, message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    # Text-mode stdio on Windows would translate "\n" to "\r\n" and decode with
    # the ANSI code page; MCP stdio is UTF-8 with bare newlines.
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
