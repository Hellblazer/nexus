#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The servers ``nx_launch.py`` serves when there is no Nexus CLI.

RDR-228 Phase 1 Step 2 (nexus-sv2wx.6). The plugin's ``nexus`` and
``nexus-catalog`` servers are programs of the CLI package. On a machine with the
plugin and no CLI the launcher serves one of these in their place, so the
application shows a connected server and the model is told the truth.

* In place of ``nx-mcp``: the handshake carries ``instructions`` saying Nexus is
  not installed and which commands install it, and the server offers two tools,
  ``setup`` and ``setup_status``. The same text is in both tools' descriptions,
  because a Claude Desktop conversation is passed a server's tools and not its
  ``instructions``.
* In place of ``nx-mcp-catalog``: no tools and no instructions, so the notice is
  not given twice.

In this phase the stand-in installs nothing. ``setup`` takes no argument, starts
no process and writes no file: it answers with the manual commands. Phase 2
gives it its work.

A stand-in never serves a Nexus tool, and it does not swap itself for the real
server inside one connection. The launcher decides at each start from one fact,
whether the program resolves, so the next start after an install runs the real
server.

Transport: MCP stdio, newline-delimited JSON-RPC 2.0 on stdin/stdout. It writes
nothing to stderr: Claude Code records every stderr line of a stdio server as an
``error`` entry in its MCP log. Standard library only, and no ``nexus`` import.
"""
from __future__ import annotations

import json
import sys
from typing import Any

SERVER_VERSION: str = "1.0.0"

#: Protocol revisions this server speaks; the newest is offered when the client
#: asks for one it does not know.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTALL_PAGE: str = "https://hellblazer.github.io/nexus/getting-started.html"

#: The first install, as README.md and the install page give it.
MANUAL_COMMANDS: tuple[str, ...] = (
    "uv tool install conexus --python 3.12",
    "nx self install",
    "nx init",
)

_COMMANDS_INLINE: str = ", then ".join(f"`{command}`" for command in MANUAL_COMMANDS)

NOT_INSTALLED: str = (
    "Nexus is not installed on this machine: the conexus plugin is present and the "
    "Nexus CLI it runs is not, so no Nexus tool is available in this session."
)

#: One sentence, as the record asks.
INSTRUCTIONS: str = (
    "Nexus is not installed on this machine, so this server has no Nexus tools; to install "
    f"it the user runs {_COMMANDS_INLINE} in a terminal and then reconnects this server "
    f"(install page: {INSTALL_PAGE})."
)

SETUP_TEXT: str = (
    f"{NOT_INSTALLED}\n\n"
    "This tool installed nothing. To install Nexus, run these in a terminal, in order:\n\n"
    + "".join(f"    {command}\n" for command in MANUAL_COMMANDS)
    + "\nThe first installs the CLI, the second moves it to its permanent place, and the "
    "third downloads and starts the local service (about 0.6 GB, two to five minutes). "
    "Then reconnect the `nexus` server or start a new session.\n\n"
    f"Platform notes and troubleshooting: {INSTALL_PAGE}"
)

STATUS_TEXT: str = (
    "Nexus is not installed: no Nexus CLI was found in its install directory or on PATH. "
    "No setup has run. The `setup` tool gives the commands that install it."
)

_NO_ARGUMENTS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
_READ_ONLY: dict[str, Any] = {
    "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False,
}

SETUP_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "setup",
        "title": "Set up Nexus",
        "description": (
            "Nexus is not installed on this machine, so no Nexus tool is available. Call this "
            "when the user asks for something Nexus would do (search, memory, indexing) or asks "
            "to set Nexus up. It installs nothing: it returns the terminal commands that install "
            "Nexus, for you to show the user."
        ),
        "inputSchema": _NO_ARGUMENTS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "setup_status",
        "title": "Nexus setup status",
        "description": (
            "Nexus is not installed on this machine. Reports that state. The `setup` tool gives "
            "the commands that install it."
        ),
        "inputSchema": _NO_ARGUMENTS,
        "annotations": _READ_ONLY,
    },
)

_TOOL_TEXT: dict[str, str] = {"setup": SETUP_TEXT, "setup_status": STATUS_TEXT}


class StandIn:
    """What one stand-in server offers: its name, instructions and tools."""

    def __init__(self, name: str, instructions: str, tools: tuple[dict[str, Any], ...]) -> None:
        self.name = name
        self.instructions = instructions
        self.tools = tools


#: The stand-in for each CLI server program.
STAND_INS: dict[str, StandIn] = {
    "nx-mcp": StandIn("nexus-not-installed", INSTRUCTIONS, SETUP_TOOLS),
    "nx-mcp-catalog": StandIn("nexus-catalog-not-installed", "", ()),
}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(server: StandIn, message: Any) -> dict[str, Any] | None:
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
            result: dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": server.name, "version": SERVER_VERSION},
            }
            if server.instructions:
                result["instructions"] = server.instructions
            return _result(request_id, result)
        case "ping":
            return _result(request_id, {})
        case "tools/list":
            return _result(request_id, {"tools": list(server.tools)})
        case "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if not any(tool["name"] == name for tool in server.tools):
                return _error(request_id, -32602, f"Unknown tool: {name!r}. Nexus is not installed.")
            return _result(request_id, {"content": [{"type": "text", "text": _TOOL_TEXT[str(name)]}]})
        case _:
            return _error(request_id, -32601, f"Method not found: {method}")


def serve(server: StandIn, stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = _error(None, -32700, "Parse error")
        else:
            reply = handle(server, message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def main(program: str) -> int:
    """Serve the stand-in for the CLI server *program* on this process's stdio."""
    # Text-mode stdio on Windows would translate "\n" to "\r\n" and decode with
    # the ANSI code page; MCP stdio is UTF-8 with bare newlines.
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    return serve(STAND_INS[program])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "nx-mcp"))
