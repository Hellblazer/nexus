#!/usr/bin/env python3
"""MCP stub server for cc-validation.
Tools log every call to STUB_LOG and return predictable values."""
import json
import os
import sys
import time
from mcp.server.fastmcp import FastMCP

LOG = os.environ.get("STUB_LOG", "/tmp/cc-val-stub.log")
NAME = os.environ.get("STUB_NAME", "stub")

mcp = FastMCP(NAME)


def _log(payload: dict) -> None:
    payload["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(payload) + "\n")


@mcp.tool()
def ping() -> str:
    """Liveness check."""
    _log({"tool": "ping"})
    return "pong"


@mcp.tool()
def record(payload: str = "") -> str:
    """Append payload to STUB_LOG, return confirmation."""
    _log({"tool": "record", "payload": payload})
    return f"recorded: {payload!r}"


@mcp.tool()
def emit_inject_json(marker: str = "MCP-RETURN") -> str:
    """Return SubagentStart additionalContext JSON contract embedding marker."""
    _log({"tool": "emit_inject_json", "marker": marker})
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": (
                "=== INJECTED BANNER ===\n"
                f"Marker: {marker}\n"
                "=== END BANNER ==="
            ),
        }
    }
    return json.dumps(out)


if __name__ == "__main__":
    mcp.run()
