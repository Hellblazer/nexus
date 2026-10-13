# SPDX-License-Identifier: AGPL-3.0-or-later
"""``generation_core._off_cwd`` and ``nx_argv.argv_off_cwd`` give the same answers.

RDR-224 review of the off-cwd sweep (nexus-f9bgu.37, I5). ``nx self install`` runs
the stdlib-only ``generation_core`` with nexus absent, so it cannot import
``nexus.util.nx_argv`` and carries its own copy of the resolver. The copy had already
drifted: it returned a relative candidate for a relative ``PATH`` entry (which
``CreateProcess`` resolves against the planted working directory), found nothing when
``PATHEXT`` was empty, and treated a drive-relative head (``C:uv``) as a bare name.
This one parametrised test runs both over the same simulated Windows cases, so the
next drift fails here instead of in a user's clone. Each case also states the answer
outright: two helpers that agree on a wrong answer would otherwise pass.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from nexus._install import generation_core
from nexus.util import nx_argv
from tests._windows_box import CWD, TOOLS, install_windows

_SAME = "same"
_RAISES = "raises"

#: id, files that exist, PATH (None: cwd first, then TOOLS), PATHEXT (see
#: ``install_windows``), the head, and the expected outcome: the resolved path,
#: ``_SAME`` (head left as it is) or ``_RAISES`` (FileNotFoundError).
_CASES = [
    pytest.param({rf"{TOOLS}\uv.exe"}, None, ".com;.exe;.bat;.cmd", "uv", rf"{TOOLS}\uv.exe", id="path-hit"),
    pytest.param({rf"{CWD}\uv.exe"}, None, ".com;.exe;.bat;.cmd", "uv", _RAISES, id="cwd-only-copy"),
    pytest.param({rf"{CWD}\uv.exe", rf"{TOOLS}\uv.exe"}, None, ".com;.exe;.bat;.cmd", "uv", rf"{TOOLS}\uv.exe", id="cwd-copy-and-path-hit"),
    pytest.param({r"tools\uv.exe"}, rf"tools;{TOOLS}", ".com;.exe;.bat;.cmd", "uv", _RAISES, id="relative-path-entry-only"),
    pytest.param({r"tools\uv.exe", rf"{TOOLS}\uv.exe"}, rf"tools;{TOOLS}", ".com;.exe;.bat;.cmd", "uv", rf"{TOOLS}\uv.exe", id="relative-path-entry-then-hit"),
    pytest.param({rf"{TOOLS}\uv.exe"}, None, "", "uv", rf"{TOOLS}\uv.EXE", id="empty-pathext"),
    pytest.param({rf"{TOOLS}\uv.exe"}, None, None, "uv", rf"{TOOLS}\uv.EXE", id="unset-pathext"),
    pytest.param({rf"{TOOLS}\uv.cmd"}, None, ".com;.exe;.bat;.cmd", "uv", _RAISES, id="batch-only-hit"),
    pytest.param({rf"{TOOLS}\uv.bat"}, None, ".com;.exe;.bat;.cmd", "uv", _RAISES, id="bat-only-hit"),
    pytest.param({rf"{TOOLS}\uv.cmd", r"C:\more\uv.exe"}, rf"{CWD};{TOOLS};C:\more", ".com;.exe;.bat;.cmd", "uv", r"C:\more\uv.exe", id="batch-then-exe"),
    pytest.param({rf"{TOOLS}\uv.exe"}, None, ".com;.exe;.bat;.cmd", "uv.exe", rf"{TOOLS}\uv.exe", id="name-with-listed-extension"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", r"tools\uv", _SAME, id="head-with-directory-part"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", "./uv", _SAME, id="head-dot-slash"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", r"C:\x\uv.exe", _SAME, id="absolute-head"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", "C:uv", _SAME, id="drive-relative-head"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", r"\\srv\share\uv.exe", _SAME, id="unc-head"),
]


@pytest.mark.parametrize(("files", "path", "pathext", "head", "expected"), _CASES)
def test_the_stdlib_copy_and_the_nexus_helper_agree(monkeypatch, files, path, pathext, head, expected) -> None:
    monkeypatch.setattr(nx_argv, "_platform", lambda: "win32")
    for module in ("nexus.util.nx_argv", "nexus._install.generation_core"):
        install_windows(monkeypatch, module, files, path=path, pathext=pathext)
    argv = [head, "venv", "--clear"]

    def outcome(resolve):  # noqa: ANN001, ANN202
        try:
            return resolve(argv)
        except FileNotFoundError:
            return _RAISES

    core = outcome(lambda a: generation_core._off_cwd(a, platform="win32"))
    helper = outcome(nx_argv.argv_off_cwd)
    want = _RAISES if expected == _RAISES else [head if expected == _SAME else expected, "venv", "--clear"]
    assert (core, helper) == (want, want)


def test_the_stdlib_copy_imports_nothing_from_nexus() -> None:
    """It runs with nexus absent; a ``nexus`` import in it would make that a crash."""
    tree = ast.parse(pathlib.Path(generation_core.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
            [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
        )
        assert not [n for n in names if n == "nexus" or n.startswith("nexus.")], ast.dump(node)


def test_posix_returns_the_argv_unchanged_from_both(monkeypatch) -> None:
    monkeypatch.setattr(nx_argv, "_platform", lambda: "linux")
    argv = ["uv", "venv"]
    assert generation_core._off_cwd(argv, platform="linux") == argv
    assert nx_argv.argv_off_cwd(argv) == argv
