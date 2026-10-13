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
no process and writes no file: it answers with the manual commands, for the
user to run. Phase 2 gives it its work, under the same two tool names, with
arguments and other descriptions.

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
import os
import sys
from typing import Any

SERVER_VERSION: str = "1.0.0"

#: Protocol revisions this server speaks; the newest is offered when the client
#: asks for one it does not know.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

#: The site's install guide ("Installing Nexus"), which has the commands per
#: operating system.
INSTALL_PAGE: str = "https://hellblazer.github.io/nexus/"


def manual_commands(platform: str | None = None) -> tuple[str, ...]:
    """The first install for this machine, as README.md and the install page
    give it. On Linux the first command names the CPU build of torch; without
    it uv installs the GPU build on x86-64, 4.3 GB that Nexus never uses."""
    platform = sys.platform if platform is None else platform
    first = "uv tool install conexus --python 3.12"
    if platform.startswith("linux"):
        first += " --torch-backend cpu"
    return (first, "nx self install", "nx init")


NOT_INSTALLED: str = (
    "Nexus is not installed on this machine: the conexus plugin is present and the "
    "Nexus CLI it runs is not, so no Nexus tool is available in this session."
)


def instructions(platform: str | None = None) -> str:
    """The server instructions. One sentence, as the record asks."""
    inline = ", then ".join(f"`{command}`" for command in manual_commands(platform))
    return (
        "Nexus is not installed on this machine, so this server has no Nexus tools; to install "
        f"it the user runs {inline} in a terminal and then reconnects this server "
        f"(install guide: {INSTALL_PAGE})."
    )


def setup_text(platform: str | None = None) -> str:
    """What ``setup`` answers in this phase: the commands, for the user to run."""
    return (
        f"{NOT_INSTALLED}\n\n"
        "This tool installed nothing. Show the user the commands below and let them decide. "
        "They are for the user to run in a terminal, in order; do not run them yourself "
        "unless the user asks you to.\n\n"
        + "".join(f"    {command}\n" for command in manual_commands(platform))
        + "\nThe first installs the CLI, the second moves it to its permanent place, and the "
        "third downloads and starts the local service and asks whether to start it at login. "
        "Together they download more than a gigabyte and take several minutes. Afterwards the "
        "user reconnects the `nexus` server (in Claude Code: /mcp, the server, Reconnect) or "
        "starts a new session.\n\n"
        f"Commands for each operating system, and troubleshooting: {INSTALL_PAGE}"
    )


STATUS_TEXT: str = (
    "Nexus is not installed: no Nexus CLI was found in its install directory or on PATH. "
    "No setup has run. The `setup` tool gives the commands that install it."
)

_NO_ARGUMENTS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

#: No ``annotations``: Phase 2 gives ``setup`` real work under the same name, and
#: a read-only hint given now must not be what an application remembers then.
SETUP_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "setup",
        "title": "Set up Nexus",
        "description": (
            "Nexus is not installed on this machine, so no Nexus tool is available. Call this "
            "when the user asks for something Nexus would do (search, memory, indexing) or asks "
            "to set Nexus up. It installs nothing: it returns the terminal commands that install "
            "Nexus, for you to show the user. Do not run them yourself unless the user asks."
        ),
        "inputSchema": _NO_ARGUMENTS,
    },
    {
        "name": "setup_status",
        "title": "Nexus setup status",
        "description": (
            "Nexus is not installed on this machine. Reports that state. The `setup` tool gives "
            "the commands that install it."
        ),
        "inputSchema": _NO_ARGUMENTS,
    },
)


def _tool_text(name: str) -> str:
    return setup_text() if name == "setup" else STATUS_TEXT


class StandIn:
    """What one stand-in server offers: its name, instructions and tools."""

    def __init__(self, name: str, instructions: str, tools: tuple[dict[str, Any], ...]) -> None:
        self.name = name
        self.instructions = instructions
        self.tools = tools


#: The stand-in for each CLI server program.
STAND_INS: dict[str, StandIn] = {
    "nx-mcp": StandIn("nexus-not-installed", instructions(), SETUP_TOOLS),
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
            return _result(request_id, {"content": [{"type": "text", "text": _tool_text(str(name))}]})
        case _:
            return _error(request_id, -32601, f"Method not found: {method}")


def _answer(server: StandIn, line: str) -> dict[str, Any] | None:
    """The reply to one input line. Never raises: an uncaught error would be a
    traceback on standard error and the end of the server."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):  # bad JSON, a 5000-digit integer, deep nesting
        return _error(None, -32700, "Parse error")
    try:
        return handle(server, message)
    except Exception:  # noqa: BLE001 -- see the docstring
        request_id = message.get("id") if isinstance(message, dict) else None
        return _error(request_id, -32603, "Internal error")


def serve(server: StandIn, stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF, or until the client
    stops reading."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        reply = _answer(server, line)
        if reply is None:
            continue
        try:
            # ASCII output: a lone surrogate echoed in an id cannot be encoded, and
            # U+2028 inside a string must not read as a line end.
            stdout.write(json.dumps(reply, allow_nan=False) + "\n")
            stdout.flush()
        except ValueError:  # an id of NaN or Infinity is not JSON
            stdout.write(json.dumps(_error(None, -32600, "Invalid Request")) + "\n")
            stdout.flush()
        except OSError:  # the client went away
            return _client_gone()
    return 0


def _client_gone() -> int:
    """Leave quietly after a failed write: point fd 1 at the null device so the
    interpreter's own flush at exit does not report the dead pipe."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    except OSError:
        pass
    return 0


def main(program: str) -> int:
    """Serve the stand-in for the CLI server *program* on this process's stdio."""
    if sys.stdin is None or sys.stdout is None:  # started with a stream closed
        return 0
    # Text-mode stdio on Windows would translate "\n" to "\r\n" and decode with
    # the ANSI code page; MCP stdio is UTF-8 with bare newlines. Bytes that are
    # not UTF-8 become a parse error for their line, not the end of the server.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    try:
        return serve(STAND_INS[program])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "nx-mcp"))
