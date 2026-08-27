# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit-level tests for the legacy bridge primitives (nexus-utpuw.7, P3).

``src/nexus/_install/legacy.sh`` is SOURCED, like layout.sh/flip.sh/shims.sh/
census.sh -- it exposes the three functions ``migrate_legacy.sh`` composes:
resolving the legacy uv-tool venv dir, extracting its extras, and registering
it as a reapable pseudo-generation. End-to-end migration behaviour (the
build+flip+shim+register sequence, and the two-pass reap) lives in
``test_migrate_legacy.py``; this file is the seam-level coverage for each
piece on its own, including the idempotent-re-registration case the
accepted-risk mitigation (a stray ``uv tool upgrade`` repopulating the legacy
tree) depends on.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LEGACY = _REPO / "src" / "nexus" / "_install" / "legacy.sh"


def _sh(snippet: str, tools: Path, extra_env: dict | None = None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", f'. "{_LEGACY}"; {snippet}'],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def tools(tmp_path):
    t = tmp_path / "tools"
    t.mkdir()
    return t


def test_legacy_bridge_is_present() -> None:
    assert _LEGACY.is_file(), f"{_LEGACY} is missing"
    assert not os.access(_LEGACY, os.X_OK), "legacy.sh is SOURCED, never executed"


# --------------------------------------------------------------------------
# nx_legacy_venv_dir
# --------------------------------------------------------------------------

def test_legacy_venv_dir_honours_an_explicit_override(tools) -> None:
    result = _sh('nx_legacy_venv_dir "/somewhere/else"', tools)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/somewhere/else"


def test_legacy_venv_dir_defaults_to_uv_tool_dir_slash_conexus(tools, tmp_path) -> None:
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    uv = stub_bin / "uv"
    uv.write_text(f'#!/bin/sh\nif [ "$1" = tool ] && [ "$2" = dir ]; then echo "{tmp_path}/uv-managed"; exit 0; fi\nexit 1\n')
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

    result = _sh(
        "nx_legacy_venv_dir",
        tools,
        {"PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{tmp_path}/uv-managed/conexus"


# --------------------------------------------------------------------------
# nx_legacy_extras
# --------------------------------------------------------------------------

def test_legacy_extras_parses_a_single_extra(tools) -> None:
    legacy = tools.parent / "legacy"
    legacy.mkdir()
    (legacy / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "conexus", extras = ["local"] }]\n'
    )
    result = _sh(f'nx_legacy_extras "{legacy}"', tools)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "local"


def test_legacy_extras_drops_mineru_and_sorts(tools) -> None:
    legacy = tools.parent / "legacy"
    legacy.mkdir()
    (legacy / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "conexus", extras = ["mineru", "zeta", "local"] }]\n'
    )
    result = _sh(f'nx_legacy_extras "{legacy}"', tools)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "local,zeta"


def test_legacy_extras_is_empty_when_the_receipt_is_absent(tools) -> None:
    legacy = tools.parent / "legacy"
    legacy.mkdir()
    result = _sh(f'nx_legacy_extras "{legacy}"', tools)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_legacy_extras_is_empty_when_the_receipt_names_none(tools) -> None:
    legacy = tools.parent / "legacy"
    legacy.mkdir()
    (legacy / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "conexus" }]\n')
    result = _sh(f'nx_legacy_extras "{legacy}"', tools)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# --------------------------------------------------------------------------
# nx_register_legacy_generation
# --------------------------------------------------------------------------

def test_register_legacy_generation_creates_the_pseudo_generation_symlink(tools) -> None:
    legacy = tools.parent / "legacy"
    legacy.mkdir()

    result = _sh(f'nx_register_legacy_generation "{legacy}" "{tools}"', tools)

    assert result.returncode == 0, result.stderr
    pseudo = tools / "gen-legacy-uv-tool"
    assert pseudo.is_symlink()
    assert Path(os.readlink(pseudo)) == legacy


def test_register_legacy_generation_is_idempotent(tools) -> None:
    legacy = tools.parent / "legacy"
    legacy.mkdir()

    first = _sh(f'nx_register_legacy_generation "{legacy}" "{tools}"', tools)
    assert first.returncode == 0, first.stderr

    second = _sh(f'nx_register_legacy_generation "{legacy}" "{tools}"', tools)
    assert second.returncode == 0, second.stderr
    pseudo = tools / "gen-legacy-uv-tool"
    assert pseudo.is_symlink(), "a second registration must leave a valid pointer, not an error"
    assert Path(os.readlink(pseudo)) == legacy
    # No leftover .tmp swap file from either call -- proves the swap cleans
    # up after itself rather than merely "usually working".
    leftovers = list(tools.glob(".gen-legacy-uv-tool.tmp.*"))
    assert leftovers == [], f"idempotent re-registration left tmp files: {leftovers}"


def test_register_legacy_generation_refreshes_a_stale_target(tools) -> None:
    """The accepted-risk mitigation: a stray `uv tool upgrade conexus` during
    the legacy window can repopulate the tree at a slightly different path
    (or the same path with new content) -- re-running migration must
    reconcile the pointer rather than leaving it stale."""
    legacy_old = tools.parent / "legacy-old"
    legacy_old.mkdir()
    legacy_new = tools.parent / "legacy-new"
    legacy_new.mkdir()

    _sh(f'nx_register_legacy_generation "{legacy_old}" "{tools}"', tools)
    result = _sh(f'nx_register_legacy_generation "{legacy_new}" "{tools}"', tools)

    assert result.returncode == 0, result.stderr
    pseudo = tools / "gen-legacy-uv-tool"
    assert Path(os.readlink(pseudo)) == legacy_new


def test_register_legacy_generation_refuses_a_relative_target(tools) -> None:
    result = _sh(f'nx_register_legacy_generation "relative/legacy" "{tools}"', tools)
    assert result.returncode != 0
    assert "absolute" in result.stderr.lower()
