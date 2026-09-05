#!/usr/bin/env python3
"""Stub standing in for Serena in cc-validation (nexus-ftpk3).

Registered under the server name ``plugin_sn_serena`` so its tools get the
exact ``mcp__plugin_sn_serena__<tool>`` names the sn worktree guard keys on.
``replace_in_files`` logs the call to STUB_LOG and returns a success string;
a scenario proves the guard by the ABSENCE of that log line."""
import json
import os
import sys
import time

LOG = os.environ.get("STUB_LOG", "/tmp/cc-val-stub.log")


def _log(payload: dict) -> None:
    payload["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(payload) + "\n")


_log({"event": "process_launched", "python": sys.executable, "name": "plugin_sn_serena"})
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("plugin_sn_serena")


@mcp.tool()
def replace_in_files(needle: str, repl: str, mode: str = "literal") -> str:
    """Stub of Serena's replace_in_files: logs and claims success."""
    _log({"tool": "replace_in_files", "needle": needle, "repl": repl, "mode": mode, "cwd": os.getcwd()})
    return "Replaced 1 occurrence(s) in 1 file(s)"


@mcp.tool()
def find_symbol(name_path_pattern: str) -> str:
    """Stub of a Serena READ tool: must stay allowed in a worktree."""
    _log({"tool": "find_symbol", "name_path_pattern": name_path_pattern})
    return "[]"


if __name__ == "__main__":
    mcp.run()
