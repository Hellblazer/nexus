# SPDX-License-Identifier: AGPL-3.0-or-later
"""Platform seams shared by tests that run on POSIX and native Windows (RDR-224).

One marker for "this test pins a POSIX-only mechanism", so a Windows run's
skips are named and greppable instead of ad-hoc ``skipif`` strings, and one
assertion for "this file is owner-only", which is a mode on POSIX and a DACL
on Windows. A test whose product behaviour exists on Windows asserts the
Windows form through these helpers rather than skipping.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

IS_WINDOWS: bool = os.name == "nt"

#: First line of a Windows fake program's script: the interpreter, quoted, as
#: setuptools' console launcher reads it.
WINDOWS_SHEBANG: str = f'#!"{sys.executable}"'


def install_windows_exe_fake(bindir: Path, name: str, script: str) -> Path:
    """Put a fake program *name* in *bindir* as a real ``<name>.exe``; returns it.

    The plugin's ``PATH`` lookups refuse a ``.cmd`` or ``.bat`` hit, as the CLI's
    do, so a Windows fake has to be an executable. The one the suite can place
    is setuptools' console launcher: copied as ``<name>.exe`` it runs
    ``<name>-script.py`` beside it with the interpreter that file's ``#!`` line
    names, passes arguments and stdio through and returns its exit code.
    *script* is that file's whole text; start it with :data:`WINDOWS_SHEBANG`.
    """
    import setuptools  # noqa: PLC0415 -- Windows only; its launcher is the one real .exe the suite can place

    launcher = Path(setuptools.__file__).parent / "cli-64.exe"
    assert launcher.is_file(), f"setuptools ships no console launcher at {launcher}"
    exe = bindir / f"{name}.exe"
    shutil.copyfile(launcher, exe)
    (bindir / f"{name}-script.py").write_text(script)
    return exe


def prune_python(venv: Path, gone: Path) -> Path:
    """Leave *venv* as ``uv`` leaves a venv whose managed CPython it pruned (the
    pipx#146 / uv#8028 class); returns *gone*, the directory that no longer exists.

    ``pyvenv.cfg`` names *gone* as ``home`` (every platform). On POSIX the venv's
    ``bin/python`` and ``bin/python3`` are also links into the pruned directory, so
    they dangle. Both facts are what a real prune produces, and the second is what
    the plugin and the CLI require beside the first: Python starts from a venv whose
    ``home`` is gone as long as the link still resolves (measured on macOS and
    Linux), so ``home`` alone is no evidence on POSIX. Windows has no links, and
    ``home`` is the whole of it.
    """
    cfg = venv / "pyvenv.cfg"
    kept = [
        line for line in (cfg.read_text(encoding="utf-8").splitlines() if cfg.exists() else [])
        if line.partition("=")[0].strip() != "home"
    ]
    cfg.write_text("\n".join([f"home = {gone}", *kept]) + "\n", encoding="utf-8")
    if not IS_WINDOWS:
        bindir = venv / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for name in ("python", "python3"):
            link = bindir / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(gone / "bin" / name)
    return gone


#: The most a shebang can hold on Linux (``BINPRM_BUF_SIZE``); ``uv`` writes an ``sh``
#: polyglot instead of a shebang for an interpreter path longer than this.
SHEBANG_LIMIT: int = 127


def long_dir(root: Path) -> Path:
    """A directory under *root*, created, deep enough that ``<it>/bin/python`` is longer
    than :data:`SHEBANG_LIMIT` characters."""
    deep = root
    while len(str(deep / "bin" / "python")) <= SHEBANG_LIMIT:
        deep = deep / ("d" + "x" * 40)
    deep.mkdir(parents=True, exist_ok=True)
    return deep


def uv_polyglot_script(interpreter: str | Path, body: str = "import sys\nsys.exit(0)\n") -> str:
    """The text of a console script as ``uv`` writes it when the interpreter's path
    will not fit a shebang (nexus-sv2wx.20; ``tests/hooks/test_cli_cannot_start.py``
    pins this text against a script ``uv`` itself wrote).

    The first line is ``#!/bin/sh``; the second is read by the shell as
    ``exec <python> "$0" "$@"`` and by Python as an empty string, a name and a
    string. The path is one single-quoted shell word, an embedded quote written
    ``'"'"'``. *body* stands for the entry-point import and call that follows.
    """
    word = "'" + str(interpreter).replace("'", "'\"'\"'") + "'"
    return f"#!/bin/sh\n'''exec' {word} \"$0\" \"$@\"\n' '''\n# -*- coding: utf-8 -*-\n{body}"


def write_uv_polyglot(path: Path, interpreter: str | Path, body: str = "import sys\nsys.exit(0)\n") -> Path:
    """Write the :func:`uv_polyglot_script` for *interpreter* at *path*, executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(uv_polyglot_script(interpreter, body), encoding="utf-8")
    path.chmod(0o755)
    return path


def posix_only(reason: str) -> pytest.MarkDecorator:
    """Skip on native Windows; *reason* names the POSIX-only mechanism pinned."""
    return pytest.mark.skipif(IS_WINDOWS, reason=f"POSIX-only: {reason}")


def scrubbed_env(**env: str) -> dict[str, str]:
    """*env* for a child process started with a deliberately minimal
    environment. On Windows it also carries ``SYSTEMROOT`` (without it Winsock
    cannot load, WinError 10106 at ``import asyncio``) and ``COMSPEC`` (the
    shell a ``.cmd`` runs under); POSIX gets exactly *env*."""
    if IS_WINDOWS:
        for key in ("SYSTEMROOT", "COMSPEC"):
            if key in os.environ and key not in env:
                env[key] = os.environ[key]
    return env


def assert_owner_only(path: str | os.PathLike[str]) -> None:
    """*path* is reachable by its owner only: mode ``0o600`` on POSIX, an
    owner-only DACL on Windows (``nexus._winsec.owner_only_problem``)."""
    if not IS_WINDOWS:
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        return
    from nexus._winsec import owner_only_problem

    problem = owner_only_problem(path, Path(path).stat().st_mode)
    assert problem is None, f"{path} is not owner-only: {problem}"


def make_group_readable(path: str | os.PathLike[str]) -> None:
    """Widen *path* the way a careless writer would: ``chmod 0o644`` on POSIX,
    a DACL that also grants Everyone read on Windows."""
    if not IS_WINDOWS:
        os.chmod(path, 0o644)
        return
    from nexus._winsec import _windows_set_dacl, _windows_user_sid

    _windows_set_dacl(str(path), f"D:P(A;;FA;;;{_windows_user_sid()})(A;;FR;;;WD)", protected=True)
