# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_resolve_nx_bin`` on POSIX prefers the ``nx`` shim over the running
interpreter when ``nx`` is off ``PATH`` (nexus-wkkeq).

A unit rendered with the interpreter form bakes the current generation's
venv python, which a later ``nx self install`` flips away from and the gc
reaps; the shim in ``bin_dir()`` follows ``current``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus.commands import daemon as daemon_cmd

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX arm; win32 always uses nx_argv")


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def posix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """POSIX platform, an empty PATH, and a bin dir under tmp_path."""
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "linux")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    return bin_dir


def test_nx_on_path_wins(posix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _executable(posix / "nx")
    on_path = _executable(tmp_path / "on-path" / "nx")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert daemon_cmd._resolve_nx_bin() == [str(on_path)]


def test_off_path_the_shim_is_used_not_the_interpreter(posix: Path) -> None:
    shim = _executable(posix / "nx")
    assert daemon_cmd._resolve_nx_bin() == [str(shim)]


def test_off_path_with_no_shim_the_interpreter_form_is_kept(posix: Path) -> None:
    assert not (posix / "nx").exists(), "non-vacuity: no shim"
    assert daemon_cmd._resolve_nx_bin() == [sys.executable, "-m", "nexus.cli"]


def test_a_shim_that_is_not_executable_is_not_used(posix: Path) -> None:
    shim = _executable(posix / "nx")
    shim.chmod(0o644)
    assert daemon_cmd._resolve_nx_bin() == [sys.executable, "-m", "nexus.cli"]


def test_a_directory_named_nx_is_not_used(posix: Path) -> None:
    (posix / "nx").mkdir(parents=True)
    assert daemon_cmd._resolve_nx_bin() == [sys.executable, "-m", "nexus.cli"]
