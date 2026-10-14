# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/hooks/scripts/_cli_lookup.py ``cannot_start``: a CLI program that is
found and cannot run (RDR-228 Phase 2 Step 4, nexus-sv2wx.17; Sam's decision of
2026-10-14).

A generation whose interpreter ``uv`` pruned is found by the lookup and fails when
started. ``cannot_start`` says so from files alone (a stat and one small read, no
process), because the launcher and the setup hook must decide in a few
milliseconds. It claims "cannot start" only on positive evidence: a fake program
with no venv around it, or a shebang that names something that exists, is
runnable.

The Windows-shaped cases run on every host: ``windows=True`` selects the branch
that has no shebang to read, and the programs are ordinary files in a temporary
directory, so they pin the decision and not the operating system. The launcher
and hook tests drive the real thing on native Windows.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from tests._platform import posix_only

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"


@pytest.fixture(scope="module")
def lookup():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_cli_lookup_cannot_start", _SCRIPTS / "_cli_lookup.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _script(path: Path, first_line: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(first_line + "\nprint('hi')\n")
    path.chmod(0o755)
    return path


def _venv(root: Path, *, home: Path | None, with_cfg: bool = True) -> Path:
    """A venv-shaped generation: ``pyvenv.cfg`` (naming *home*) and ``bin/``."""
    (root / "bin").mkdir(parents=True)
    if with_cfg and home is not None:
        (root / "pyvenv.cfg").write_text(f"home = {home}\ninclude-system-site-packages = false\nversion = 3.12.1\n")
    return root


# -- POSIX: the shebang --------------------------------------------------------------------------


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_naming_a_missing_interpreter_cannot_start(lookup, tmp_path: Path) -> None:
    exe = _script(tmp_path / "bin" / "nx-mcp", f"#!{tmp_path}/pruned/bin/python3")
    reason = lookup.cannot_start(str(exe), windows=False)
    assert reason is not None
    assert str(tmp_path / "pruned" / "bin" / "python3") in reason


@posix_only("a dangling symlink is how uv leaves a pruned interpreter")
def test_a_shebang_naming_a_dangling_symlink_cannot_start(lookup, tmp_path: Path) -> None:
    gone = tmp_path / "uv" / "cpython-3.12" / "bin" / "python3.12"
    link = tmp_path / "gen" / "bin" / "python"
    link.parent.mkdir(parents=True)
    os.symlink(gone, link)
    exe = _script(tmp_path / "gen" / "bin" / "nx-mcp", f"#!{link}")
    assert lookup.cannot_start(str(exe), windows=False) is not None


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_naming_an_interpreter_that_exists_can_start(lookup, tmp_path: Path) -> None:
    exe = _script(tmp_path / "bin" / "nx-mcp", f"#!{sys.executable}")
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("a shebang names the interpreter the kernel starts")
def test_a_shebang_naming_a_file_that_is_not_executable_cannot_start(lookup, tmp_path: Path) -> None:
    interp = tmp_path / "python"
    interp.write_text("not a program")
    interp.chmod(0o644)
    exe = _script(tmp_path / "bin" / "nx-mcp", f"#!{interp}")
    assert lookup.cannot_start(str(exe), windows=False) is not None


@posix_only("a shebang names the interpreter the kernel starts")
def test_an_env_shebang_needs_its_program_on_path(lookup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _script(tmp_path / "bin" / "nx-hook", "#!/usr/bin/env pythonx-not-here")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert "pythonx-not-here" in (lookup.cannot_start(str(exe), windows=False) or "")
    fake = _script(tmp_path / "onpath" / "pythonx-not-here", "#!/bin/sh")
    monkeypatch.setenv("PATH", str(fake.parent))
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("a shebang names the interpreter the kernel starts")
@pytest.mark.parametrize(
    "line", ["#!python3", "#!/usr/bin/env -S python3 -u", "#!/usr/bin/env FOO=1 python3", "#!", "#! "],
    ids=["relative", "env-with-option", "env-with-assignment", "empty", "blank"],
)
def test_a_shebang_it_cannot_judge_is_runnable(lookup, tmp_path: Path, line: str) -> None:
    """No guess: only a shebang whose meaning is certain can say "cannot start"."""
    exe = _script(tmp_path / "bin" / "nx-mcp", line)
    assert lookup.cannot_start(str(exe), windows=False) is None


@posix_only("reads a shebang")
def test_a_binary_and_an_empty_file_are_not_judged_by_a_shebang(lookup, tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "nx-mcp"
    binary.parent.mkdir()
    binary.write_bytes(b"\x7fELF\x02\x01\x01" + bytes(64))
    binary.chmod(0o755)
    empty = tmp_path / "bin" / "nx-hook"
    empty.write_bytes(b"")
    empty.chmod(0o755)
    assert lookup.cannot_start(str(binary), windows=False) is None
    assert lookup.cannot_start(str(empty), windows=False) is None


def test_a_program_that_vanished_is_not_judged(lookup, tmp_path: Path) -> None:
    assert lookup.cannot_start(str(tmp_path / "bin" / "gone"), windows=False) is None
    assert lookup.cannot_start(str(tmp_path / "bin" / "gone.exe"), windows=True) is None


# -- the venv around the program: POSIX and Windows-shaped ----------------------------------------


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_a_generation_whose_base_python_was_pruned_cannot_start(lookup, tmp_path: Path, windows: bool) -> None:
    """``pyvenv.cfg`` names the interpreter the venv was built from; ``uv`` removes
    that directory when it prunes the Python. On Windows the program is a launcher
    copy with no shebang, and this is the whole check."""
    gen = _venv(tmp_path / "gen-20260101T000000Z", home=tmp_path / "uv" / "cpython-3.12-gone")
    exe = gen / "bin" / ("nx-mcp.exe" if windows else "nx-mcp")
    exe.write_bytes(b"MZ not a real launcher" if windows else b"\x7fELF" + bytes(8))
    exe.chmod(0o755)
    reason = lookup.cannot_start(str(exe), windows=windows)
    assert reason is not None
    assert "cpython-3.12-gone" in reason


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_a_generation_whose_base_python_is_there_can_start(lookup, tmp_path: Path, windows: bool) -> None:
    home = tmp_path / "uv" / "cpython-3.12"
    home.mkdir(parents=True)
    gen = _venv(tmp_path / "gen-20260101T000000Z", home=home)
    exe = gen / "bin" / ("nx-mcp.exe" if windows else "nx-mcp")
    exe.write_bytes(b"MZ" if windows else b"\x7fELF")
    exe.chmod(0o755)
    assert lookup.cannot_start(str(exe), windows=windows) is None


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_a_generation_with_no_pyvenv_cfg_is_not_judged(lookup, tmp_path: Path, windows: bool) -> None:
    """A fake generation (the suite's) or a half-written one: no evidence either way."""
    gen = _venv(tmp_path / "gen", home=None, with_cfg=False)
    exe = gen / "bin" / ("nx-mcp.exe" if windows else "nx-mcp")
    exe.write_bytes(b"x")
    assert lookup.cannot_start(str(exe), windows=windows) is None


def test_a_pyvenv_cfg_without_a_home_line_is_not_judged(lookup, tmp_path: Path) -> None:
    gen = _venv(tmp_path / "gen", home=None, with_cfg=False)
    (gen / "pyvenv.cfg").write_text("version = 3.12.1\n")
    exe = gen / "bin" / "nx-mcp"
    exe.write_bytes(b"x")
    assert lookup.cannot_start(str(exe), windows=True) is None


@posix_only("a symlinked program")
def test_a_symlink_into_a_pruned_venv_cannot_start(lookup, tmp_path: Path) -> None:
    """A ``PATH`` entry that links to a program inside a venv is judged by that venv."""
    gen = _venv(tmp_path / "tool", home=tmp_path / "gone-python")
    target = gen / "bin" / "nx"
    target.write_bytes(b"\x7fELF")
    target.chmod(0o755)
    link = tmp_path / "local-bin" / "nx"
    link.parent.mkdir()
    os.symlink(target, link)
    assert lookup.cannot_start(str(link), windows=False) is not None


def test_a_home_that_is_a_file_cannot_start(lookup, tmp_path: Path) -> None:
    """``home`` must be a directory; a file at that path is no interpreter directory."""
    home = tmp_path / "home-file"
    home.write_text("x")
    gen = _venv(tmp_path / "gen", home=home)
    exe = gen / "bin" / "nx-mcp.exe"
    exe.write_bytes(b"MZ")
    assert lookup.cannot_start(str(exe), windows=True) is not None


def test_the_check_starts_no_process_and_opens_no_socket() -> None:
    """Standard library only, and not ``subprocess``: a stat and one small read."""
    import ast

    tree = ast.parse((_SCRIPTS / "_cli_lookup.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {"subprocess", "socket", "urllib", "http"}, imported
