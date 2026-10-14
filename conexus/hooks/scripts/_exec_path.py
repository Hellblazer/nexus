# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve an executable on PATH alone, never through the current directory.

RDR-224 review finding A (nexus-f9bgu.36). On Windows ``CreateProcess`` looks in
the current directory before ``PATH``, and so does ``shutil.which``. Claude Code
runs a hook with the project as its cwd, so a ``git.exe``, ``uv.exe`` or
``nx-hook.exe`` planted in a cloned repository would be run by any hook that
spawns that name bare. For the shim it would also be relayed as the hook's own
verdict, and two of the shim's three entries are auto-approve.

Every spawn in ``conexus/hooks`` therefore goes through :func:`which_off_cwd` and
passes the absolute path it returns. ``tests/test_hooks_bare_exec_lint.py`` fails
on a bare-name spawn or a ``shutil.which`` call anywhere else under this tree.

Windows: PATHEXT is honoured (``os.execvp`` and ``Popen`` ignore it), no exec bit
is needed, and a relative or empty PATH entry, or one that is the cwd itself, is
skipped because it resolves to the cwd, which is the same hole. A ``.cmd`` or
``.bat`` hit is not returned, as in ``nexus.util.nx_argv``: a batch file runs
through ``cmd.exe`` with quoting CPython does not escape for it, and every program
a hook spawns (``nx-hook``, ``nx``, ``uv``, ``git``, a Python) is an ``.exe`` in a
real install. A tool found only as a batch file counts as not found. POSIX:
``shutil.which``, which already ignores the cwd unless PATH itself names ``.`` or
has an empty entry; the lookup is unchanged there.

Standard library only, no ``nexus`` import: hook scripts run under
``uv tool run python``. ``bootstrap.py`` in ``mcpb/src`` carries the same rule
in its own file because the desktop bundle ships separately.

``platform``/``path``/``pathext`` are injectable so the Windows branch runs on any
host. Not ``shutil.which`` for that branch: it decides Windows-ness from the real
``sys.platform`` and cannot be driven from a test on another OS.
"""
from __future__ import annotations

import ntpath
import os
import sys
from pathlib import PureWindowsPath

_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})


def _is_search_dir(directory: str, cwd: PureWindowsPath) -> bool:
    """True when a Windows ``PATH`` entry may honestly be searched: it has a drive
    and a root (so not ``tools``, ``.``, ``C:x`` or ``\\x``, which resolve against
    the working directory or its drive) and is not the working directory itself.

    On a real Windows host only a drive-and-root entry counts; ``os.path.isabs``
    also accepts a rooted ``\\x`` before Python 3.13. A Windows run injected on a
    POSIX host (the tests) also takes a POSIX absolute path, as its fixtures are
    real temporary directories.
    """
    if not directory:
        return False
    entry = PureWindowsPath(directory)
    absolute = entry.is_absolute() or (os.name != "nt" and os.path.isabs(directory))
    # normpath: an entry that spells the working directory with ``.`` or ``..`` parts is still it.
    return absolute and PureWindowsPath(ntpath.normpath(directory)) != cwd


def which_off_cwd(
    name: str,
    platform: str | None = None,
    path: str | None = None,
    pathext: str | None = None,
) -> str | None:
    """The absolute path *name* resolves to on PATH, or ``None``.

    A name that already carries a directory part is not searched; it is returned
    as given when it is a file, the way ``CreateProcess`` treats it. No hook
    call site passes one.
    """
    platform = sys.platform if platform is None else platform
    windows = platform == "win32"
    if path is None:
        path = os.environ.get("PATH", "")
    if os.path.dirname(name) or (windows and ("/" in name or "\\" in name)):
        return name if os.path.isfile(name) else None
    if not windows:
        import shutil  # noqa: PLC0415 -- lazy: the Windows branch never needs it, and the session-start hook has 100 ms

        return shutil.which(name, path=path)
    if pathext is None:
        pathext = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    listed = [e for e in pathext.split(";") if e]
    has_ext = os.path.splitext(name)[1].upper() in [e.upper() for e in listed]
    # A name that already ends in a listed extension is tried as written only, as
    # ``shutil.which`` and ``nexus.util.nx_argv`` do; appending to it finds ``uv.exe.cmd``.
    exts = [""] if has_ext else listed
    names = [n for n in (name + e for e in exts) if os.path.splitext(n)[1].lower() not in _BATCH_SUFFIXES]
    cwd = PureWindowsPath(ntpath.normpath(os.getcwd()))
    for directory in path.split(";"):
        if not _is_search_dir(directory, cwd):
            continue
        for candidate_name in names:
            candidate = os.path.join(directory, candidate_name)
            if os.path.isfile(candidate):
                return candidate
    return None
