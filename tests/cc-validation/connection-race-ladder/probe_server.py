#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Probe MCP server for the RDR-215 interactive connection-race ladder (nexus-veh77).

Stands in for ``nx-mcp`` as the target of ``mcp_tool`` hooks. Every call to
``probe`` appends one JSON line carrying the hook's marker and a wall-clock
timestamp, so "the tool-tier hook ran" is read from this file and never from
the TUI. ``PROBE_START_DELAY`` sleeps before the MCP handshake is served,
which widens the window in which the server is spawned but not connected;
that is what makes the ladder able to see a race at all, since the real
server connects in well under a second.

``PROBE_BROKEN=1`` exits before serving, which is the negative control: the
client records the connection as failed and every hook aimed here is skipped.
"""
import json
import os
import sys
import time
from typing import Any

LOG = os.environ["PROBE_LOG"]


def _log(payload: dict) -> None:
    payload["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(payload, default=repr) + "\n")


_DELAY = float(os.environ.get("PROBE_START_DELAY", "0"))
_log({"event": "process_launched", "pid": os.getpid(), "start_delay_s": _DELAY})
if os.environ.get("PROBE_BROKEN") == "1":
    _log({"event": "broken_exit"})
    sys.exit(3)
if _DELAY:
    time.sleep(_DELAY)
    _log({"event": "start_delay_elapsed"})

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("probe")


@mcp.tool()
def probe(marker: Any = "", session_id: Any = "") -> str:
    """Record that a hook reached this server."""
    _log({"event": "probe_called", "marker": marker, "session_id": session_id})
    return "probe recorded"


if __name__ == "__main__":
    _log({"event": "serving"})
    mcp.run()
