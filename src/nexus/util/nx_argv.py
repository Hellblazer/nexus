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

import os
import shutil
import sys
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


def _in_cwd(found: str) -> bool:
    """True when *found* sits in the current directory: a relative ``.\\x.exe``
    (what ``shutil.which`` returns for a current-directory hit) or an absolute
    path whose directory is the working directory. Windows path rules, no I/O."""
    path = PureWindowsPath(found)
    if not path.is_absolute():
        return True
    return path.parent == PureWindowsPath(os.getcwd())


def which_off_cwd(name: str) -> str | None:
    """``shutil.which(name)`` that never answers with a current-directory hit on
    Windows, where ``which`` (and ``CreateProcess``) search the working directory
    before ``PATH``. A hit there is retried against ``PATH`` with the working
    directory's own entry removed, and still refused if ``which`` hands back the
    planted file again. POSIX is ``shutil.which`` unchanged: its search is
    ``PATH`` only. Use it whenever the result reaches an argv (RDR-224 test review
    S3, nexus-f9bgu.35); a bare existence probe may keep ``shutil.which``."""
    found = shutil.which(name)
    if found is None or _platform() != "win32" or not _in_cwd(found):
        return found
    cwd = PureWindowsPath(os.getcwd())
    others = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and PureWindowsPath(entry) != cwd
    ]
    retry = shutil.which(name, path=os.pathsep.join(others))
    return None if retry is None or _in_cwd(retry) else retry


__all__ = ["console_python", "nx_argv", "nx_argv_for", "which_off_cwd"]
