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


__all__ = ["console_python", "nx_argv"]
