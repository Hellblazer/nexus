#!/usr/bin/env python3
"""MCP stub server for cc-validation.
Tools log every call to STUB_LOG and return predictable values."""
import json
import os
import sys
import time

LOG = os.environ.get("STUB_LOG", "/tmp/cc-val-stub.log")
NAME = os.environ.get("STUB_NAME", "stub")


def _log(payload: dict) -> None:
    payload["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(payload) + "\n")


# Startup markers (diagnostic): these fire BEFORE the mcp import so STUB_LOG can
# distinguish three cases for an inline-agent / project server: (1) no marker at
# all = the process was never spawned by Claude Code; (2) process_launched +
# mcp_import_failed = spawned but the interpreter lacks `mcp` (bare python3); (3)
# mcp_imported_ok = healthy. They log `event`, not `tool`, so tool_ran checks are
# unaffected.
_log({"event": "process_launched", "python": sys.executable, "name": NAME})
try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover - diagnostic path
    _log({"event": "mcp_import_failed", "python": sys.executable, "error": str(exc)})
    raise
_log({"event": "mcp_imported_ok", "python": sys.executable})

mcp = FastMCP(NAME)


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
