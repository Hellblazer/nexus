#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tell the model at session start that Nexus is not installed, when it is not.

RDR-228 Phase 1 Step 3 (nexus-sv2wx.7). The plugin's servers and most of its
hooks are programs of the Nexus CLI, and a machine can have the plugin and no
CLI. This hook is the one place a session says so. Every other entry that finds
no CLI writes one line and stays out of the way.

Two outcomes in this phase:

* ``nx-mcp`` resolves (``_cli_lookup.resolve``, the launcher's lookup): print
  nothing. The fact is the one the launcher serves by, so the hook speaks
  exactly when the stand-in server is what the session got.
* It does not: tell the model Nexus is not installed and give the commands that
  install it, for the user to run. No question is asked; the question and the
  setup tools that act on its answer arrive in Phase 2.

It starts no process, opens no socket, makes no network request and writes no
file. It does not read its payload. Standard output carries the hook's JSON and
nothing else, and nothing is written to standard error.

Standard library only, and no ``nexus`` import: it runs under ``uv tool run
python`` on a machine with no CLI. The commands and the install guide's address
are the stand-in's (``conexus/launch/nx_standin.py``), so the two cannot differ.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_HERE)), "launch"))
sys.path.insert(0, _HERE)
import _cli_lookup  # noqa: E402 -- must follow the sys.path insert
import nx_standin  # noqa: E402 -- must follow the sys.path insert

#: The program whose absence means "no CLI": the one the launcher serves the
#: stand-in for.
PROBE_PROGRAM = "nx-mcp"


def notice() -> str:
    """What the model is told when there is no CLI."""
    commands = ", then ".join(f"`{command}`" for command in nx_standin.manual_commands())
    return (
        "Nexus is not installed on this machine: the conexus plugin is present and the "
        "Nexus CLI it runs is not. Nexus tools, the plugin's Nexus hooks and its `nx` "
        "commands do not work in this session. Tell the user this once, when it is "
        f"relevant. To install Nexus the user runs in a terminal: {commands}. Do not run "
        "these yourself unless the user asks. They download more than a gigabyte and take "
        "several minutes. Afterwards the user starts a new session. "
        f"Install guide: {nx_standin.INSTALL_PAGE}"
    )


def main() -> int:
    if _cli_lookup.resolve(PROBE_PROGRAM) is not None:
        return 0
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": notice()},
    }) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
