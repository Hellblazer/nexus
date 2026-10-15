#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# ///
"""Start a Nexus CLI program, or handle its absence without an error.

RDR-228 Phase 1 Step 1 (nexus-sv2wx.5). The plugin's servers and most of its
hooks are programs of the CLI package, and a machine can have the plugin and no
CLI. Every server and hook entry that needs the CLI names this script and the
program it wants::

    uv tool run --directory ${CLAUDE_PLUGIN_ROOT} ... python launch/nx_launch.py nx-mcp
    uv tool run --directory ${CLAUDE_PLUGIN_ROOT} ... python launch/nx_launch.py nx-hook preflight

What happens is decided from two facts about the program, both read from files
and neither from anything the plugin writes: whether it resolves
(``_cli_lookup.resolve``: the generation layout, then ``PATH``, never the current
directory) and, when it does, whether it can start (``_cli_lookup.cannot_start``:
a stat and one small read, no process; Sam, 2026-10-14). An install done by hand
at any time is used at the next launch.

* It resolves and can start: run it. On POSIX the launcher replaces itself with
  the program (``os.execv``), so the launcher is not left between a running server
  and the application. ``uv`` is: it stays alive as the parent of what it runs,
  on every platform. On Windows the launcher starts the program with its own
  standard input and output and exits with the program's exit code.
* The real ``nx-mcp`` is handed ``NX_SETUP_SCRIPT``, the path of ``nx_setup.py``
  beside this file (RDR-228 Phase 2 Step 3). Its ``setup`` and ``setup_status``
  tools start that file, so the setup process has one copy, in the plugin. It is
  set in every state of the setup directory and the launcher reads none of it. A
  value inherited from the environment is replaced: the variable names the file
  this plugin ships. No other program gets it.
* It does not resolve and a hook program was asked for: exit 0 with one line on
  standard error. A missing CLI never blocks a prompt or a tool call.
* It does not resolve and a server was asked for: :func:`serve_stand_in`, a
  server that says Nexus is not installed (``nx_standin.py``).
* It resolves and cannot start (an interpreter ``uv`` pruned), or starting it
  fails: a hook program gets one line on standard error saying so and exit 0, and
  a server gets the same stand-in, which says "Nexus is installed but cannot
  start; setup repairs it" with the reason. The ``setup`` tool rebuilds the CLI.

The entries start this script with ``--directory ${CLAUDE_PLUGIN_ROOT}``, so its
working directory is the plugin root, and the programs read the project from
theirs. The launcher therefore moves to the project first. For a hook program
that is the payload's ``cwd``, else ``CLAUDE_PROJECT_DIR``, the rule
``nx_hook_shim.py`` uses; the payload is read to find it and handed on unchanged.
For a server it is ``CLAUDE_PROJECT_DIR``, and standard input, which is the
protocol stream, is not read.

On Windows the program is a child of the launcher, and nothing ties its life to
the launcher's: when the application ends the launcher at a hook's timeout, the
program runs on until it finishes. A server ends when the application closes its
standard input. ``nx_hook_shim.py`` has the same limit there.

Standard library only, and no ``nexus`` import: it has to run with no CLI
installed. It is under ``launch/`` because Cowork refuses a plugin that has a
top-level ``bin/``.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports: no .pyc in the plugin directory

import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from typing import BinaryIO  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "scripts"),
)
import _cli_lookup  # noqa: E402 -- must follow the sys.path insert

#: Programs a hook entry runs. Absent, they are skipped with a note.
HOOK_PROGRAMS = frozenset({"nx-hook", "nx-session-end-launcher"})

#: Programs a server entry runs. Absent, the stand-in is served.
SERVER_PROGRAMS = frozenset({"nx-mcp", "nx-mcp-catalog"})

#: The environment variable that carries the setup process's path to the real
#: ``nx-mcp``, which starts it from its ``setup`` tool.
SETUP_SCRIPT_ENV = "NX_SETUP_SCRIPT"


def serve_stand_in(name: str, broken: str | None = None) -> int:
    """Serve in place of the server *name* when there is no CLI, or when there is
    one and it cannot start (*broken* is the reason).

    The stand-in (``nx_standin.py``, beside this file) answers the handshake and
    says Nexus is not installed, or installed but cannot start. Nothing is written
    to standard error: for a stdio server Claude Code records each such line as
    an error. The import is here, not at module scope, so a hook launch does not
    pay for it.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import nx_standin  # noqa: PLC0415 -- see the docstring

    return nx_standin.main(name, broken)


def _cannot_start(name: str, exe: str, reason: str) -> int:
    """The program *name*, found at *exe*, cannot start for *reason*. A server is
    served by the stand-in that says so; a hook is skipped with one line; any other
    program fails with one line."""
    if name in SERVER_PROGRAMS:
        return serve_stand_in(name, reason)
    sys.stderr.write(_cli_lookup.cannot_start_note(exe, reason))
    return 0 if name in HOOK_PROGRAMS else 1


def _skipped(name: str, args: list[str]) -> str:
    """What is skipped when the hook program *name* is absent."""
    if name == "nx-hook" and args:
        return f"the {args[0]} hook"
    if name == "nx-session-end-launcher":
        return "the session-end hook"
    return f"`{name}`"


def _hook_stdin() -> tuple[BinaryIO | None, str | None]:
    """Read a hook's payload; returns it as a rewound file, and the project.

    The payload names the project (``_cli_lookup.project_dir``), and the program
    still has to read it, so it goes into an unnamed temporary file that becomes
    the program's standard input.
    """
    try:
        payload = sys.stdin.buffer.read()
    except (AttributeError, OSError, ValueError):  # no stdin, or a closed one
        return None, _cli_lookup.project_dir(b"")
    try:
        handoff = tempfile.TemporaryFile()  # noqa: SIM115 -- outlives this function by design
        handoff.write(payload)
        handoff.seek(0)
    except OSError as exc:
        raise OSError(f"the hook payload could not be handed on: {exc}") from exc
    return handoff, _cli_lookup.project_dir(payload)


def _hand_off(name: str, exe: str, args: list[str]) -> int:
    """Run *exe* in the project directory and return its exit code (POSIX: never
    returns on success)."""
    if name == "nx-mcp":  # before the move to the project: __file__ may be relative to this directory
        setup_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nx_setup.py")
        if os.path.isfile(setup_script):
            os.environ[SETUP_SCRIPT_ENV] = setup_script
        else:
            os.environ.pop(SETUP_SCRIPT_ENV, None)  # never hand on a path this plugin did not ship
    stdin: BinaryIO | None = None
    if name in HOOK_PROGRAMS:
        stdin, project = _hook_stdin()
    else:
        project = _cli_lookup.project_dir(b"")
    if project is not None:
        try:
            os.chdir(project)
        except OSError:
            pass
    argv = [exe, *args]
    if os.name != "nt":
        if stdin is not None:
            os.dup2(stdin.fileno(), 0)
        os.execv(exe, argv)
    proc = subprocess.Popen(argv, stdin=stdin)  # noqa: S603 -- an absolute path from _cli_lookup.resolve
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:  # the console sends Ctrl+C to the child too; wait for its exit
            continue


def main(argv: list[str]) -> int:
    if not argv or not _cli_lookup.PROGRAM_NAME.match(argv[0]):
        sys.stderr.write("nx_launch: expected the name of a Nexus CLI program, then its arguments\n")
        return 1
    name, args = argv[0], argv[1:]
    exe = _cli_lookup.resolve(name)
    if exe is not None:
        # Found, and the files say it cannot start: no process is started to find out.
        reason = _cli_lookup.cannot_start(exe)
        if reason is not None:
            return _cannot_start(name, exe, reason)
        try:
            return _hand_off(name, exe, args)
        except OSError as exc:
            # Found and not startable after all (a generation reaped between the
            # lookup and the start, a file the system will not execute). Treated as
            # the case above: a hook is skipped and a server is served the stand-in
            # that says "installed but cannot start", never "not installed".
            return _cannot_start(name, exe, str(exc))
    if name in HOOK_PROGRAMS:
        sys.stderr.write(_cli_lookup.missing_note(name, _skipped(name, args)))
        return 0
    if name in SERVER_PROGRAMS:
        return serve_stand_in(name)
    sys.stderr.write(_cli_lookup.missing_note(name, "`" + " ".join(argv) + "`"))
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
