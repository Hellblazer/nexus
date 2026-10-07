# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx self install`` works on a box with no ``python3`` on PATH.

layout.sh and its siblings run their logic as bare ``python3 <x>_core.py``.
Measured 2026-10-07 in a fresh ubuntu:24.04 container: the documented
``uv tool install conexus`` brings uv's own Python and nothing installs a
system ``python3``, so the first ``nx self install`` died with
"layout.sh: line 119: python3: command not found". ``_script_env`` appends
the running interpreter's venv ``bin`` (which always holds a ``python3``) to
the scripts' PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.commands import self_cmd
from nexus.commands.self_cmd import _script_env, _sh, packaged_install_dir
from tests._module_seam import setattr_in

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX install scripts")

#: What sourcing the install library and calling ``nx_tools_dir`` may exec.
_TOOLS = (
    "bash", "sh", "dirname", "basename", "cat", "sed", "awk", "grep", "mkdir",
    "rm", "ln", "mv", "readlink", "uname", "env", "head", "tail", "tr", "cut",
    "date", "ls", "stat", "id", "sort", "realpath", "printf", "test",
)


@pytest.fixture
def no_python_path(tmp_path, monkeypatch) -> Path:
    """A PATH holding the shell tools and no python3, like a minimal image."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        if found:
            (bindir / name).symlink_to(found)
    assert not (bindir / "python3").exists()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "tools"))
    return bindir


def test_control_the_library_fails_without_python3(no_python_path, tmp_path):
    """The condition the fix answers is real: with this PATH and no env help,
    sourcing layout.sh and calling into it cannot find python3."""
    install_dir = packaged_install_dir()
    r = subprocess.run(
        ["bash", "-c",
         f'NX_LAYOUT_HOME="{install_dir}"; . "{install_dir}/layout.sh"; nx_tools_dir'],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": str(no_python_path)},
    )
    assert r.returncode != 0
    assert "python3" in r.stderr


def test_sh_runs_the_library_with_no_python3_on_path(no_python_path, tmp_path):
    out = _sh(packaged_install_dir(), "nx_tools_dir").strip()
    assert Path(out) == tmp_path / "tools"


def test_script_env_appends_the_venv_bin_last(monkeypatch, tmp_path):
    setattr_in(monkeypatch, self_cmd, "sys.executable", str(tmp_path / "venv" / "bin" / "python"))
    monkeypatch.setenv("PATH", os.pathsep.join(["/a", "/b"]))
    path = _script_env()["PATH"].split(os.pathsep)
    assert path == ["/a", "/b", str(tmp_path / "venv" / "bin")]


def test_script_env_leaves_a_path_that_already_has_it(monkeypatch, tmp_path):
    venv_bin = str(tmp_path / "venv" / "bin")
    setattr_in(monkeypatch, self_cmd, "sys.executable", venv_bin + "/python")
    monkeypatch.setenv("PATH", os.pathsep.join([venv_bin, "/a"]))
    assert _script_env()["PATH"] == os.pathsep.join([venv_bin, "/a"])
