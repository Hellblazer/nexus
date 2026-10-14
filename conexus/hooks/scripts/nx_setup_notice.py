#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# ///
"""Tell the model at session start that Nexus is not installed, when it is not.

RDR-228 Phase 1 Step 3 (nexus-sv2wx.7). The plugin's servers and most of its
hooks are programs of the Nexus CLI, and a machine can have the plugin and no
CLI. This hook is the one place a session says so. Every other entry that finds
no CLI writes one line and stays out of the way.

Three outcomes in this phase:

* The hook runs in a remote workspace (``CLAUDE_CODE_REMOTE`` is true): print
  nothing. A Desktop conversation's loop runs in a cloud workspace that has
  ``uv`` and no CLI, while the plugin's servers run on the user's machine and
  reach the conversation through the application's bridge. The hook cannot see
  those servers from there, and its notice was false for a working install
  (measured 2026-10-13, nexus-217c3). When the user's machine has no CLI the
  stand-in's tool descriptions say so through the bridge.
* ``nx-mcp`` resolves (``_cli_lookup.resolve``, the launcher's lookup): print
  nothing. The fact is the one the launcher serves by, so the hook speaks
  exactly when the stand-in server is what the session got.
* It does not: tell the model Nexus is not installed and give the commands that
  install it, for the user to run. No question is asked; the question and the
  setup tools that act on its answer arrive in Phase 2.

It starts no process, opens no socket, makes no network request and writes no
file, its own bytecode cache included. It does not read its payload. Standard
output carries the hook's JSON and nothing else, nothing is written to standard
error, and it exits 0 whatever happens: a session start must not be disturbed by
this hook failing.

Standard library only, and no ``nexus`` import: it runs under ``uv tool run
python`` on a machine with no CLI. The commands and the install guide's address
are the stand-in's (``conexus/launch/nx_standin.py``), so the two cannot differ.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports: no .pyc in the plugin directory

import json  # noqa: E402
import os  # noqa: E402

#: The program whose absence means "no CLI": the one the launcher serves the
#: stand-in for.
PROBE_PROGRAM = "nx-mcp"

#: Claude Code sets this to ``true`` where the session's loop, and so this hook,
#: runs in a remote workspace and not on the user's machine.
REMOTE_ENV = "CLAUDE_CODE_REMOTE"


def notice(commands: tuple[str, ...], install_page: str) -> str:
    """What the model is told when there is no CLI."""
    inline = ", then ".join(f"`{command}`" for command in commands)
    return (
        "Nexus is not installed on this machine, or its install is incomplete: the conexus "
        f"plugin is present and the Nexus CLI program it runs (`{PROBE_PROGRAM}`) was not "
        "found, so this session has no Nexus tools. To install Nexus the user runs in a "
        f"terminal: {inline}. Do not run these yourself unless the user asks. Together they "
        "download more than a gigabyte and take several minutes. Afterwards the user "
        "reconnects the `nexus` server or starts a new session. "
        f"Install guide: {install_page}"
    )


def in_remote_workspace() -> bool:
    return (os.environ.get(REMOTE_ENV) or "").strip().lower() in ("true", "1")


def main() -> int:
    if in_remote_workspace():
        return 0
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(here)), "launch"))
    sys.path.insert(0, here)
    import _cli_lookup  # noqa: PLC0415 -- after the sys.path insert, inside the guard below
    import nx_standin  # noqa: PLC0415

    if _cli_lookup.resolve(PROBE_PROGRAM) is not None or sys.stdout is None:
        return 0
    text = notice(nx_standin.manual_commands(), nx_standin.INSTALL_PAGE)
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
    try:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    except OSError:
        # The reader went away. Point fd 1 at the null device so the
        # interpreter's own flush at exit does not report the dead pipe.
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- see the docstring: exit 0 whatever happens
        pass
    sys.exit(0)
