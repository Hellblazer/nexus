# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The Windows dispatcher: what a launcher on the user PATH runs (nexus-f9bgu.47).

``generation_core.ensure_dispatchers`` builds a small venv, ``<tools>\\shim``,
and installs into it one tiny distribution per launcher name whose console
script is this module's :func:`main` (the source is copied into each wheel, so a
distribution needs nothing installed). The venv's launcher executables are copied
to ``<tools>\\bin``, the one directory the user PATH names. ``nx.exe`` there is
therefore a dispatcher, the same for every generation, and it never changes when
a generation does:

    nx.exe args -> read <tools>\\current -> run <generation>\\Scripts\\nx.exe args

WHY NOT THE GENERATION'S OWN LAUNCHER ON THE PATH. Putting the generation's
launchers in a fixed directory means replacing them on each flip, and a running
launcher cannot be renamed, replaced or deleted (WinError 32 / 5, measured
2026-11-28 with ``nx-mcp.exe``). Putting them behind a directory link means a
junction or a symlink, and RedirectionGuard refuses to traverse a junction a
non-elevated process made (WinError 448) while a symlink needs Developer Mode
(WinError 1314). The pointer is a file, and only this fixed program reads it.

This file is the source of the wheel module as well as an importable module, so
it must stay standard library only and import nothing from nexus. It imports
``subprocess`` lazily and nothing else beyond ``os`` and ``sys``: every import is
paid on every ``nx`` call. ``tests/test_install_dispatcher.py`` pins both the
import set and the parity of :data:`POINTER_MAGIC` with ``layout_core``.
"""
import os
import sys

#: ``layout_core.POINTER_MAGIC``, restated because this module is copied out on its own.
POINTER_MAGIC = "nx-pointer 1"

#: Exit status when there is nothing to dispatch to (the shell's "command not found").
EXIT_NOT_FOUND = 127


def read_pointer(path):
    """The target a pointer file names, or a junction's target, or ``None``.

    A pointer file is the magic line, then the absolute path. A junction or a
    symlink (an install from before RedirectionGuard, not yet migrated) is read
    with ``readlink``, which does not traverse it.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
    except OSError:
        raw = b""
    if raw:
        try:
            lines = raw.decode("utf-8").split("\n")
        except ValueError:
            lines = []
        if len(lines) >= 3 and lines[0].rstrip("\r") == POINTER_MAGIC and not lines[2].strip():
            return lines[1].rstrip("\r") or None
    try:
        target = os.readlink(path)
    except (OSError, ValueError):
        return None
    for prefix in ("\\\\?\\UNC\\", "\\\\?\\", "\\??\\"):
        if target.startswith(prefix):
            target = ("\\\\" if "UNC" in prefix else "") + target[len(prefix):]
            break
    return target or None


def resolve(name, tools):
    """``(program, problem)``: the generation's launcher for *name*, or why not."""
    generation = read_pointer(os.path.join(tools, "current"))
    if not generation:
        return None, "no generation is installed (%s is not a pointer); run `nx self install`" % os.path.join(tools, "current")
    program = os.path.join(generation, "Scripts", name + ".exe")
    if not os.path.isfile(program):
        return None, "%s is not in the current generation (%s)" % (name, generation)
    return program, None


def tools_dir(executable):
    """``<tools>`` from the dispatcher venv's interpreter, ``<tools>\\shim\\Scripts\\python.exe``."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(executable))))


def main(argv=None, executable=None):
    """Run the current generation's launcher of the same name, and exit with its status."""
    argv = list(sys.argv if argv is None else argv)
    name = os.path.splitext(os.path.basename(argv[0]))[0]
    program, problem = resolve(name, tools_dir(sys.executable if executable is None else executable))
    if program is None:
        sys.stderr.write("nexus: %s\n" % problem)
        return EXIT_NOT_FOUND
    import subprocess

    try:
        return subprocess.call([program] + argv[1:])
    except KeyboardInterrupt:
        # The child shares the console and has seen the same Ctrl-C; its status is the answer.
        return 130
    except OSError as exc:
        sys.stderr.write("nexus: could not run %s: %s\n" % (program, exc))
        return EXIT_NOT_FOUND


def console_main():
    """The console-script entry: ``sys.exit(main())``."""
    sys.exit(main())
