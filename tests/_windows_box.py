# SPDX-License-Identifier: AGPL-3.0-or-later
"""A simulated Windows file system and environment for the off-cwd resolvers.

``nexus.util.nx_argv`` (its Windows arm) and ``nexus._install.generation_core``
(``_off_cwd``) both walk ``PATH`` themselves and test each candidate with
``os.path.isfile``. :func:`install_windows` gives one module a private view of
``os`` in which the working directory, ``os.pathsep``, ``os.environ`` and
``os.path.isfile`` are the fakes, so the Windows arm runs under test on any host and
nothing process-wide is patched (``tests/test_module_seam.py``).

*files* are the paths that exist; membership is Windows-case-insensitive.
"""
from __future__ import annotations

import ntpath
from collections.abc import Iterable

import pytest

from tests._module_seam import setattr_in

CWD = r"C:\work\clone"
TOOLS = r"C:\tools\bin"


def install_windows(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    files: Iterable[str],
    *,
    path: str | None = None,
    cwd: str = CWD,
    pathext: str | None = ".com;.exe;.bat;.cmd",
) -> None:
    """Fake *module*'s view of ``os`` as a Windows host holding exactly *files*.

    *path* defaults to the working directory first, then ``TOOLS``: the order in
    which ``CreateProcess`` would have found a planted file before an installed one.
    *pathext* defaults to a lower-case list, so a hit reads ``git.exe``; ``None``
    leaves ``PATHEXT`` unset and ``""`` sets it empty (the resolvers then use their
    own upper-case default list, so a hit reads ``git.EXE``)."""
    # normpath as well as normcase: Windows resolves ``.`` and ``..`` parts when it opens a path.
    existing = {ntpath.normcase(ntpath.normpath(f)) for f in files}
    env = {"PATH": f"{cwd};{TOOLS}" if path is None else path}
    if pathext is not None:
        env["PATHEXT"] = pathext
    setattr_in(monkeypatch, module, "os.getcwd", lambda: cwd)
    setattr_in(monkeypatch, module, "os.pathsep", ";")
    setattr_in(monkeypatch, module, "os.environ", env)
    setattr_in(monkeypatch, module, "os.path.isfile", lambda p: ntpath.normcase(ntpath.normpath(p)) in existing)
