# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The argv that runs ``nx`` as a child process (RDR-224, nexus-f9bgu.33).

On Windows a bare ``nx`` argv[0] is resolved by ``CreateProcess``, which
searches the CURRENT DIRECTORY before ``System32`` and ``PATH``, and
``shutil.which("nx")`` prepends the current directory too (Python 3.12). An
``nx.exe`` in a cloned repository or a download folder would run as the user.
So on Windows the child is ``[<console python>, "-m", "nexus.cli", ...]``: the
interpreter that is running this process, an absolute path no working
directory can change, running the same program the ``nx`` console script maps
to (``nx = "nexus.cli:main"`` in ``pyproject.toml``).

POSIX keeps the bare name. Its ``execvp`` searches ``PATH`` only, so the
current directory is not on the path unless the user put it there.

Stdlib only: the hooks import this on every tool call
(``tests/test_nx_bare_exec_lint.py`` names this module as the one place a bare
``"nx"`` argv[0] may appear).
"""
from __future__ import annotations

import ntpath
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import PureWindowsPath


def _platform() -> str:
    """``sys.platform``; one seam so the Windows arm runs under test anywhere."""
    return sys.platform


def console_python(python_exe: str) -> str:
    """*python_exe* with ``pythonw.exe`` mapped to the ``python.exe`` beside it.

    A child whose output the caller captures needs a console-subsystem
    interpreter: a ``pythonw`` child has no stdout to write to.
    """
    path = PureWindowsPath(python_exe)
    if path.name.lower() == "pythonw.exe":
        return str(path.with_name("python.exe"))
    return str(path)


def nx_argv(*args: str) -> list[str]:
    """argv for ``nx <args...>`` that cannot resolve ``nx`` from the cwd."""
    if _platform() == "win32":
        return [console_python(sys.executable), "-m", "nexus.cli", *args]
    return ["nx", *args]


def nx_argv_for(resolved: str, *args: str) -> list[str]:
    """argv for ``nx <args...>`` given *resolved*, the path a PATH lookup found.

    POSIX spawns exactly what the lookup found. Windows never spawns a looked-up
    path: it is the interpreter form of :func:`nx_argv`, so a lookup that went
    wrong (a working-directory hit) still cannot reach the argv."""
    if _platform() == "win32":
        return nx_argv(*args)
    return [resolved, *args]


_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})


def _has_location(name: str) -> bool:
    """True when *name* carries a directory part or a drive (``.\\x``, ``tools/x``,
    ``C:\\x\\y.exe``, ``C:x``, a UNC path). ``CreateProcess`` takes such a name as
    written and does not search ``PATH`` for it. Windows path rules, no I/O."""
    drive, tail = ntpath.splitdrive(name)
    return bool(drive) or "\\" in tail or "/" in tail


def _candidates(name: str, allow_batch: bool) -> list[str]:
    """The file names to try for *name*, in ``PATHEXT`` order: *name* as written
    when it already ends in a listed extension, else *name* plus each extension. An
    unset or empty ``PATHEXT`` is the default list (``shutil.which`` does the same).
    A ``.cmd`` or ``.bat`` candidate is dropped unless *allow_batch*."""
    listed = [e for e in (os.environ.get("PATHEXT") or _DEFAULT_PATHEXT).split(";") if e]
    if ntpath.splitext(name)[1].upper() in {e.upper() for e in listed}:
        names = [name]
    else:
        names = [name + e for e in listed]
    if allow_batch:
        return names
    return [n for n in names if ntpath.splitext(n)[1].lower() not in _BATCH_SUFFIXES]


def _search_dirs() -> list[str]:
    """The ``PATH`` entries a bare name may honestly be looked up in: absolute ones
    (drive and root, so not ``tools``, ``.``, ``C:x`` or ``\\x``, which resolve
    against the working directory), minus the working directory's own, however the
    entry spells it (case, separators, a trailing one, ``.`` and ``..`` parts; an 8.3
    short name is not recognised)."""
    cwd = PureWindowsPath(ntpath.normpath(os.getcwd()))
    return [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and PureWindowsPath(entry).is_absolute() and PureWindowsPath(ntpath.normpath(entry)) != cwd
    ]


def which_off_cwd(name: str, *, allow_batch: bool = False) -> str | None:
    """``shutil.which(name)`` that never answers with a current-directory hit on
    Windows, where ``which`` (and ``CreateProcess``) search the working directory
    before ``PATH``. POSIX is ``shutil.which`` unchanged: its search is ``PATH``
    only. Use it whenever the result reaches an argv (RDR-224 test review S3,
    nexus-f9bgu.35); a bare existence probe may keep ``shutil.which``.

    On Windows the lookup is this module's own ``PATH`` walk: ``shutil.which``
    searches the working directory first even when given ``path=``, so a planted
    copy would hide the installed one. ``PATHEXT`` is honoured, but a ``.cmd`` or
    ``.bat`` hit is not returned unless *allow_batch*: a bare ``git`` or ``bd``
    spawn only ever found an ``.exe``, and a batch file runs through ``cmd.exe``
    with quoting CPython does not escape for it (the BatBadBut class). The one
    caller that runs a ``.cmd`` shim on purpose, ``plugin_lockstep``'s ``claude``,
    passes ``allow_batch=True``. A *name* with a directory part or a drive is a path
    the caller chose: it is returned when it exists, and never searched for.
    """
    if _platform() != "win32":
        return shutil.which(name)
    names = _candidates(name, allow_batch)
    if _has_location(name):
        return next((c for c in names if os.path.isfile(c)), None)
    for entry in _search_dirs():
        for candidate in names:
            full = ntpath.join(entry, candidate)
            if os.path.isfile(full):
                return full
    return None


def argv_off_cwd(argv: Sequence[str], *, allow_batch: bool = False) -> list[str]:
    """*argv* with a bare ``argv[0]`` resolved by :func:`which_off_cwd` on Windows.

    The spelling for a hook verb that spawns ``git``, ``bd`` or any other tool by
    name: ``CreateProcess`` would take a ``git.exe`` planted in the project (the
    hook's cwd) before ``PATH``, so the child gets the ``PATH`` hit as an absolute
    path (RDR-224 Phase 4 critique S1, nexus-f9bgu.37). A head with a directory part
    or a drive (an absolute path, ``.\\x``, ``tools/x``) is an explicit path the
    caller chose, which ``CreateProcess`` does not search for; it is left alone.
    POSIX is returned unchanged in value (``execvp`` searches ``PATH`` only).

    Raises ``FileNotFoundError`` when Windows has no ``PATH`` hit for a bare name,
    which is what the spawn itself raised for a missing tool, so a caller's existing
    not-found handling is the one that runs. A tool found only as a ``.cmd`` or
    ``.bat`` counts as not found unless *allow_batch*. It never falls back to the
    bare name: that is the planted file.
    """
    out = list(argv)
    if not out or _platform() != "win32" or _has_location(out[0]):
        return out
    found = which_off_cwd(out[0], allow_batch=allow_batch)
    if found is None:
        raise FileNotFoundError(f"{out[0]}: not found on PATH outside the working directory")
    out[0] = found
    return out


__all__ = ["argv_off_cwd", "console_python", "nx_argv", "nx_argv_for", "which_off_cwd"]
