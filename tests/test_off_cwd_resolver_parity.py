# SPDX-License-Identifier: AGPL-3.0-or-later
"""The four off-cwd resolvers give the same answers.

RDR-224 review of the off-cwd sweep (nexus-f9bgu.37, I5; nexus-f9bgu.38, S4). Three
of the four are stdlib-only copies, because each runs where nexus is absent or not
yet importable:

* ``nexus.util.nx_argv`` (``argv_off_cwd``): the reference, used inside nexus.
* ``nexus._install.generation_core._off_cwd``: ``nx self install`` runs it with
  nexus absent.
* ``conexus/hooks/scripts/_exec_path.which_off_cwd``: a plugin script under
  ``uv tool run python``.
* ``mcpb/src/bootstrap._resolve_executable``: the Claude Desktop bundle launcher,
  which runs before conexus is installed.

The first copy had drifted (a relative ``PATH`` entry, an empty ``PATHEXT``, a
drive-relative head) and the test then compared only two of the four. The review
measured the bootstrap copy returning ``tools/uv.exe`` for ``PATH="tools"``. This
one parametrised test runs all four over the same simulated Windows cases, so the
next drift fails here instead of in a user's clone. Each case also states the answer
outright: copies that agree on a wrong answer would otherwise pass.

One difference is deliberate and asserted by name in ``_BATCH_EXCEPTIONS``: the hook
helper and the bundle launcher return a ``.cmd`` or ``.bat`` hit that ``nx_argv`` and
``generation_core`` refuse (BatBadBut, ``nx_argv.which_off_cwd``'s docstring).
"""
from __future__ import annotations

import ast
import importlib.util
import ntpath
import pathlib
import types

import pytest

from nexus._install import generation_core
from nexus.util import nx_argv
from tests._windows_box import CWD, TOOLS, install_windows

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_EXEC_PATH = _ROOT / "conexus" / "hooks" / "scripts" / "_exec_path.py"
_BOOTSTRAP = _ROOT / "mcpb" / "src" / "bootstrap.py"

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
    pytest.param({r"\tools\uv.exe"}, r"\tools;" + TOOLS, ".com;.exe;.bat;.cmd", "uv", _RAISES, id="rooted-path-entry-only"),
    pytest.param({r"C:tools\uv.exe"}, rf"C:tools;{TOOLS}", ".com;.exe;.bat;.cmd", "uv", _RAISES, id="drive-relative-path-entry-only"),
    pytest.param({r".\uv.exe"}, rf".;{TOOLS}", ".com;.exe;.bat;.cmd", "uv", _RAISES, id="dot-path-entry-only"),
    pytest.param({"uv.exe"}, rf";;{TOOLS}", ".com;.exe;.bat;.cmd", "uv", _RAISES, id="empty-path-entries-only"),
    pytest.param({rf"{CWD}\uv.exe"}, rf"{CWD}\sub\..;{CWD}\.;{TOOLS}", ".com;.exe;.bat;.cmd", "uv", _RAISES, id="cwd-spelled-with-dot-parts-only"),
    pytest.param({rf"{CWD}\uv.exe", rf"{TOOLS}\uv.exe"}, rf"{CWD}\sub\..;{TOOLS}", ".com;.exe;.bat;.cmd", "uv", rf"{TOOLS}\uv.exe", id="cwd-spelled-with-dot-parts-then-hit"),
    pytest.param({rf"{TOOLS}\uv.exe.exe"}, None, ".com;.exe;.bat;.cmd", "uv.exe", _RAISES, id="listed-extension-name-gets-no-second-extension"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", r"tools\uv", _SAME, id="head-with-directory-part"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", "./uv", _SAME, id="head-dot-slash"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", r"C:\x\uv.exe", _SAME, id="absolute-head"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", "C:uv", _SAME, id="drive-relative-head"),
    pytest.param(set(), None, ".com;.exe;.bat;.cmd", r"\\srv\share\uv.exe", _SAME, id="unc-head"),
]


#: Where the hook helper and the bundle launcher knowingly answer differently: they return
#: a ``.cmd`` or ``.bat`` hit that ``nx_argv`` and ``generation_core`` refuse. Kept as named,
#: asserted entries so the difference cannot widen or close unnoticed. The hook shim's own
#: Windows tests install a fake ``nx-hook.cmd`` and ``tests/test_mcpb_bootstrap.py`` pins
#: ``.cmd`` winning when ``PATHEXT`` lists it first; a real install has ``nx-hook.exe`` and
#: ``uv.exe``. Closing it changes plugin and bundle behaviour (nexus-f9bgu.38 decision).
_BATCH_EXCEPTIONS: dict[str, str] = {
    "batch-only-hit": rf"{TOOLS}\uv.cmd",
    "bat-only-hit": rf"{TOOLS}\uv.bat",
    "batch-then-exe": rf"{TOOLS}\uv.cmd",
}
_BATCH_LENIENT = ("hooks", "bootstrap")


def _load(path: pathlib.Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _norm(value):  # noqa: ANN001, ANN202
    """Windows spelling: case and separators do not make two answers different, and
    ``os.path.join`` on a POSIX host (the stdlib copies use it) writes ``/``."""
    return ntpath.normcase(value) if isinstance(value, str) else value


def _outcomes(monkeypatch, files, path, pathext, head) -> dict[str, object]:  # noqa: ANN001
    hooks = _load(_EXEC_PATH, "_exec_path_parity")
    bootstrap = _load(_BOOTSTRAP, "mcpb_bootstrap_parity")
    monkeypatch.setattr(nx_argv, "_platform", lambda: "win32")
    for module in ("nexus.util.nx_argv", "nexus._install.generation_core", hooks, bootstrap):
        install_windows(monkeypatch, module, files, path=path, pathext=pathext)
    argv = [head, "venv", "--clear"]

    def argv_style(resolve):  # noqa: ANN001, ANN202
        try:
            out = resolve(argv)
        except FileNotFoundError:
            return _RAISES
        assert out[1:] == argv[1:]
        return _SAME if out[0] == head else out[0]

    def path_style(resolve):  # noqa: ANN001, ANN202
        """``which``-style: a path or ``None``. An explicit path is returned as the
        caller wrote it and never searched; no call site passes one (``_exec_path``'s
        docstring), so the ``_SAME`` rows are not asked of these two."""
        found = resolve(head)
        return _RAISES if found is None else found

    return {
        "core": argv_style(lambda a: generation_core._off_cwd(a, platform="win32")),
        "helper": argv_style(nx_argv.argv_off_cwd),
        "hooks": path_style(lambda h: hooks.which_off_cwd(h, platform="win32")),
        "bootstrap": path_style(lambda h: bootstrap._resolve_executable(h, platform="win32")),
    }


@pytest.mark.parametrize(("files", "path", "pathext", "head", "expected"), _CASES)
def test_the_four_resolvers_agree(request, monkeypatch, files, path, pathext, head, expected) -> None:
    got = _outcomes(monkeypatch, files, path, pathext, head)
    case_id = request.node.callspec.id
    want = {name: expected for name in got}
    if expected == _SAME:
        # explicit-path heads: the two argv-style copies leave them alone
        got = {name: got[name] for name in ("core", "helper")}
        want = {name: _SAME for name in got}
    for name in _BATCH_LENIENT if case_id in _BATCH_EXCEPTIONS else ():
        want[name] = _BATCH_EXCEPTIONS[case_id]
    assert {k: _norm(v) for k, v in got.items()} == {k: _norm(v) for k, v in want.items()}, case_id


def test_the_batch_exceptions_are_exactly_the_cases_where_nx_argv_refuses_a_batch_hit() -> None:
    """The named exceptions are real cases, and no other case is excused."""
    ids = {p.id for p in _CASES}
    assert set(_BATCH_EXCEPTIONS) <= ids
    for case_id, want in _BATCH_EXCEPTIONS.items():
        assert ntpath.splitext(want)[1].lower() in {".cmd", ".bat"}, case_id


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
