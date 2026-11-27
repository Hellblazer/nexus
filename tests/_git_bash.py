# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bash a git-guard test spawns to run a command for real, and the spelling of a path inside that command's text.

On native Windows a bare ``"bash"`` argv[0] is the WSL launcher: ``CreateProcess`` searches ``System32`` before
PATH, and ``%SystemRoot%\\System32\\bash.exe`` runs the ``-c`` text inside the WSL Linux distribution, not in the
Windows cwd. Measured on qwent (2026-11-27): ``["bash", "-c", "echo a; echo b; echo hi > f"]`` exits 0, prints only
the WSL kernel line of its first command, and writes no ``f`` in the cwd; when WSL's service is unreachable it exits
1 with its error on stdout in UTF-16 and nothing on stderr. So a test that runs a git write for real saw "changed
nothing" or a ``CalledProcessError`` with empty stderr. Git for Windows' own bash is not on PATH there at all.
:func:`bash` resolves it from git itself (``git --exec-path`` is ``<root>/mingw64/libexec/git-core``, and
``<root>/bin/bash.exe`` is the launcher that puts the MSYS ``git``, ``sh``, ``ssh`` and ``env`` on PATH), the shell
Claude Code's Bash tool runs on Windows. Off Windows it is plain ``"bash"``, unchanged.

:func:`shell_path` spells a path for that shell's command text. bash reads an unquoted backslash as an escape, so
``C:\\Users\\x`` in a command becomes ``C:Usersx``; the forward-slash form is the same path to bash, git and Python.
Off Windows it is ``str(path)``, unchanged.

:func:`put_stubs_first` puts a test's stub executables (a fake ``ssh``) ahead of the ones that launcher adds.
"""
from __future__ import annotations

import functools
import os
import pathlib
import subprocess
import sys

__all__ = ["bash", "put_stubs_first", "shell_path"]


@functools.cache
def bash() -> str:
    """The bash to put in argv[0]: Git for Windows' ``bin/bash.exe`` on Windows, ``"bash"`` elsewhere."""
    if sys.platform != "win32":
        return "bash"
    exec_path = subprocess.run(
        ["git", "--exec-path"], capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout.strip()
    # <root>/mingw64/libexec/git-core (64-bit) or <root>/mingw32/libexec/git-core: the root is three up.
    candidate = pathlib.Path(exec_path).parents[2] / "bin" / "bash.exe"
    if not candidate.is_file():
        raise FileNotFoundError(
            f"Git for Windows' bash is not at {candidate} (git --exec-path is {exec_path!r}); "
            "a bare 'bash' here is the System32 WSL launcher, which runs the command in Linux, not this cwd"
        )
    return os.fspath(candidate)


def shell_path(path: os.PathLike[str] | str) -> str:
    """*path* spelled for a bash command line: forward slashes on Windows, ``str(path)`` elsewhere."""
    if sys.platform != "win32":
        return os.fspath(path)
    return pathlib.PureWindowsPath(os.fspath(path)).as_posix()


def _msys_path(path: os.PathLike[str] | str) -> str:
    """``C:\\a\\b`` as MSYS spells it inside bash's PATH: ``/c/a/b`` (a ``C:`` would split at the colon)."""
    win = pathlib.PureWindowsPath(os.fspath(path))
    return "/" + win.drive.rstrip(":").lower() + "/" + "/".join(win.parts[1:])


def put_stubs_first(env: dict[str, str], stubs: pathlib.Path) -> None:
    """Make the executables in *stubs* win the PATH lookup of a :func:`bash` command run with *env*.

    Prepending *stubs* to ``PATH`` is enough off Windows. On Windows Git's ``bin/bash.exe`` prepends
    ``/mingw64/bin:/usr/bin`` before it starts bash (measured on qwent: a stub ``ssh`` first on the Windows PATH lost
    to ``/usr/bin/ssh``), so *env* also names a ``BASH_ENV`` file, which bash sources before it runs ``-c``, that
    puts *stubs* back in front. The command text the guard reads is unchanged.
    """
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    if sys.platform != "win32":
        return
    startup = stubs.parent / f"{stubs.name}-bash-env.sh"
    startup.write_text(f"PATH='{_msys_path(stubs)}':\"$PATH\"\n", encoding="utf-8")
    env["BASH_ENV"] = shell_path(startup)
