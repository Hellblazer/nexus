# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``scripts/reinstall-tool.sh`` downgrade guard — resolve from the TARGET
venv, not the ambient PATH (nexus-zfutt).

Root cause: the guard read the "installed" version via a bare ``nx --version``
lookup on ``$PATH``. ``tests/e2e/release-sandbox.sh`` activates an isolated
sandbox ``$HOME`` and PREPENDS ``$SANDBOX/.local/bin`` to ``$PATH`` before
calling this script — but on a fresh (or not-yet-populated) sandbox no ``nx``
exists there yet, so ``command -v nx`` falls through to the REST of ``$PATH``
and resolves the live global install instead. A develop checkout's
``pyproject.toml`` version (which lags the last released version between
releases) then reads as a "downgrade" against the live global version, and the
guard refuses a perfectly legitimate isolated sandbox install.

These tests never touch the real global ``uv`` tool environment: a stub
``uv`` (handling only ``tool dir``, no-op otherwise) and stub ``nx`` binaries
are placed on ``$PATH`` ahead of everything else, and ``$HOME`` points at a
throwaway ``tmp_path``.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reinstall-tool.sh"

# A curated base PATH for these subprocess runs — deliberately EXCLUDES
# ~/.local/bin (and any other uv-tool-managed bin dir), which is where the
# REAL `nx` / `uv` live on a dev box. These tests must never be able to reach
# the real global install (fewer-permission-prompts / "don't break the live
# nexus install"); only the stub `uv`/`nx` binaries these tests place on
# PATH may be found. Includes just enough of the real system PATH for
# bash/git/python3/sed/grep/ps to resolve.
_SAFE_BASE_PATH = ":".join(
    p for p in (
        "/opt/homebrew/bin",
        "/opt/homebrew/opt/python@3.13/libexec/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    )
    if Path(p).is_dir()
)


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stub_uv(bin_dir: Path, *, tool_dir: Path, marker: Path) -> None:
    """A stub ``uv`` that answers ``tool dir`` and, for anything else
    (notably ``tool install``), just drops a marker file so tests can
    assert whether the real install step was ever reached."""
    _make_executable(
        bin_dir / "uv",
        f"""#!/bin/bash
if [[ "$1" == "tool" && "$2" == "dir" ]]; then
    echo "{tool_dir}"
    exit 0
fi
if [[ "$1" == "tool" && "$2" == "install" ]]; then
    touch "{marker}"
    exit 0
fi
exit 0
""",
    )


def _stub_nx(path: Path, version: str) -> None:
    _make_executable(
        path,
        f"""#!/bin/bash
if [[ "$1" == "--version" ]]; then
    echo "nx, version {version}"
    exit 0
fi
exit 0
""",
    )


def _write_pyproject(source_dir: Path, version: str) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "pyproject.toml").write_text(
        f'[project]\nname = "conexus"\nversion = "{version}"\n'
    )


def _run(env_path: str, home: Path, source: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = env_path
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(_SCRIPT), str(source)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestDowngradeGuardResolvesFromTargetVenv:
    def test_does_not_falsely_refuse_when_only_ambient_path_nx_is_newer(
        self, tmp_path: Path
    ) -> None:
        """The bug: a fresh/isolated target venv has no ``nx`` of its own
        yet, but the REST of ``$PATH`` (the live global install) still
        resolves to a much newer ``nx``. That must NOT read as a downgrade —
        there is nothing installed in the target venv to downgrade."""
        home = tmp_path / "sandbox-home"
        home.mkdir()
        tool_dir = home / "tools"  # VENV_DIR = tool_dir/conexus — deliberately NOT populated
        marker = tmp_path / "install-ran.marker"

        stub_bin = tmp_path / "stubbin"
        stub_bin.mkdir()
        _stub_uv(stub_bin, tool_dir=tool_dir, marker=marker)

        # The "ambient" live global install: a DIFFERENT, later-in-PATH nx
        # reporting a much newer version than the source checkout.
        global_bin = tmp_path / "globalbin"
        global_bin.mkdir()
        _stub_nx(global_bin / "nx", "6.16.0")

        source = tmp_path / "checkout"
        _write_pyproject(source, "6.11.0")

        env_path = f"{stub_bin}:{global_bin}:{_SAFE_BASE_PATH}"
        result = _run(env_path, home, source)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "REFUSING" not in result.stdout
        assert marker.exists()  # the (stubbed) install actually ran

    def test_still_refuses_a_real_downgrade_of_the_target_venv(
        self, tmp_path: Path
    ) -> None:
        """The guard's real purpose must survive: when the TARGET venv's own
        ``nx`` (the one about to be overwritten) is genuinely ahead of the
        source checkout, refuse without ``--force``."""
        home = tmp_path / "real-home"
        home.mkdir()
        tool_dir = home / "tools"
        venv_bin = tool_dir / "conexus" / "bin"
        venv_bin.mkdir(parents=True)
        marker = tmp_path / "install-ran.marker"

        stub_bin = tmp_path / "stubbin"
        stub_bin.mkdir()
        _stub_uv(stub_bin, tool_dir=tool_dir, marker=marker)

        # The nx actually installed in the target venv — this is the one
        # about to be overwritten, and the one the guard must protect.
        _stub_nx(venv_bin / "nx", "6.16.0")

        source = tmp_path / "checkout"
        _write_pyproject(source, "6.11.0")

        env_path = f"{stub_bin}:{_SAFE_BASE_PATH}"
        result = _run(env_path, home, source)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "REFUSING" in result.stdout
        assert "DOWNGRADE" in result.stdout
        assert not marker.exists()  # never reached the install step

    def test_force_bypasses_a_real_downgrade(self, tmp_path: Path) -> None:
        home = tmp_path / "real-home"
        home.mkdir()
        tool_dir = home / "tools"
        venv_bin = tool_dir / "conexus" / "bin"
        venv_bin.mkdir(parents=True)
        marker = tmp_path / "install-ran.marker"

        stub_bin = tmp_path / "stubbin"
        stub_bin.mkdir()
        _stub_uv(stub_bin, tool_dir=tool_dir, marker=marker)
        _stub_nx(venv_bin / "nx", "6.16.0")

        # A PATH-reachable nx too — needed for the script's own post-install
        # `nx --version` echo (unrelated to the guard under test here; the
        # guard itself no longer needs anything on PATH).
        global_bin = tmp_path / "globalbin"
        global_bin.mkdir()
        _stub_nx(global_bin / "nx", "6.16.0")

        source = tmp_path / "checkout"
        _write_pyproject(source, "6.11.0")

        env = dict(os.environ)
        env["PATH"] = f"{stub_bin}:{global_bin}:{_SAFE_BASE_PATH}"
        env["HOME"] = str(home)
        result = subprocess.run(
            ["bash", str(_SCRIPT), str(source), "--force"],
            env=env, capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.exists()

    @pytest.mark.parametrize("global_version", ["6.16.0", "6.10.0"])
    def test_fresh_target_venv_never_compares_against_ambient_path(
        self, tmp_path: Path, global_version: str
    ) -> None:
        """Regardless of whether the stray ambient ``nx`` happens to look
        newer or older than the source checkout, a genuinely empty target
        venv must never trigger the guard at all — there is nothing there to
        compare against."""
        home = tmp_path / "sandbox-home"
        home.mkdir()
        tool_dir = home / "tools"
        marker = tmp_path / "install-ran.marker"

        stub_bin = tmp_path / "stubbin"
        stub_bin.mkdir()
        _stub_uv(stub_bin, tool_dir=tool_dir, marker=marker)

        global_bin = tmp_path / "globalbin"
        global_bin.mkdir()
        _stub_nx(global_bin / "nx", global_version)

        source = tmp_path / "checkout"
        _write_pyproject(source, "6.11.0")

        env_path = f"{stub_bin}:{global_bin}:{_SAFE_BASE_PATH}"
        result = _run(env_path, home, source)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "REFUSING" not in result.stdout
        assert marker.exists()
