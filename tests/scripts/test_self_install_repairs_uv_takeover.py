# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A uv takeover self-repairs. nexus-hibpr follow-on (7.21.0).

Measured against uv 0.8 in a sandbox (2026-08-28): on a generation box a
plain ``uv tool install conexus`` rebuilds uv's tree (a [local]-less copy)
but refuses to overwrite a nexus-owned shim; ``--force`` takes the shims,
after which every spawn resolves through uv's tree -- wrong install, maybe
wrong version, wrong extras -- and ``nx doctor`` was the only thing that
noticed. ``uv tool uninstall`` deletes the shims at those paths, so it was
never the remedy.

``repair_uv_takeover`` puts the box back: shims to ``current``, uv's tree
registered for reap, and -- when uv's tree is NEWER, i.e. the user meant to
upgrade -- a generation at that version built from current's OWN receipt,
so [local] survives. ``nx upgrade`` (the SessionStart hook) and
``nx self install`` both run it; ``nx self install`` running FROM uv's tree
beside a layout takes this path instead of migrate_legacy.sh, whose extras
bridge would read the rebuilt uv receipt -- the one that dropped [local].
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH, stub_uv

from nexus import install_layout
from nexus.commands import self_cmd
from nexus.commands.self_cmd import perform_self_install, repair_uv_takeover

ENTRY_POINTS = ("nx", "nx-mcp")


def _receipt(gen: Path, *, source: str, extras: list[str], version: str) -> None:
    spec = f"{source}[{','.join(extras)}]" if extras else source
    (gen / "nexus-install.json").write_text(json.dumps({
        "schema": 1, "version": version,
        "spec": spec, "source_kind": "directory", "source": source,
        "extras": extras, "python": "3.12", "base_interpreter": "/usr/bin",
        "created_at": "2026-08-26T00:00:00Z", "installer_schema": 1,
    }))


def _generation(tools: Path, stamp: str, *, source: str, extras: list[str], version: str) -> Path:
    """A generation the shim writer can read: a python that declares its entry
    points (the shape the shared uv stub fabricates) plus the executables."""
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    (gen / "bin" / "python").write_text("#!/bin/sh\nprintf '%s\\n' " + " ".join(ENTRY_POINTS) + "\n")
    (gen / "bin" / "python").chmod(0o755)
    for ep in ENTRY_POINTS:
        (gen / "bin" / ep).write_text("#!/bin/sh\necho gen\n")
        (gen / "bin" / ep).chmod(0o755)
    (gen / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.8\n")
    _receipt(gen, source=source, extras=extras, version=version)
    return gen


def _uv_tree(uv_tools: Path, *, extras_in_uv_receipt: str = "") -> Path:
    """uv's rebuilt tree: bin/, a python, entry points, and a uv receipt that
    lists whatever extras uv was told -- by default NONE, the [local]-dropping shape."""
    legacy = uv_tools / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "python").write_text("#!/bin/sh\n")
    for ep in ENTRY_POINTS:
        (legacy / "bin" / ep).write_text("#!/bin/sh\necho uv-tree\n")
        (legacy / "bin" / ep).chmod(0o755)
    (legacy / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.8\n")
    (legacy / "uv-receipt.toml").write_text(
        f'[tool]\nname = "conexus"\nextras = [{extras_in_uv_receipt}]\n'
    )
    return legacy


def _take_shims(bin_dir: Path, legacy: Path) -> None:
    """What `uv tool install --force` does: uv's symlinks at the shim paths."""
    for ep in ENTRY_POINTS:
        (bin_dir / ep).unlink(missing_ok=True)
        (bin_dir / ep).symlink_to(legacy / "bin" / ep)


def _write_nexus_shims(bin_dir: Path, tools: Path) -> None:
    for ep in ENTRY_POINTS:
        (bin_dir / ep).write_text(install_layout.render_shim(ep, tools=tools))
        (bin_dir / ep).chmod(0o755)


@pytest.fixture()
def bed(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    src = tmp_path / "src-nexus"
    src.mkdir()
    (src / "pyproject.toml").write_text('[project]\nversion = "7.21.0"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_uv(stub_bin)
    uv_tools = tmp_path / "uv-tools"
    uv_tools.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
    monkeypatch.setenv("PATH", f"{stub_bin}:{SAFE_BASE_PATH}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    current = _generation(tools, "20260801T000000Z", source=str(src), extras=["local"], version="7.20.0")
    (tools / "current").symlink_to(current)
    _write_nexus_shims(bin_dir, tools)
    # Versions come from each tree's own metadata in production; here from a map.
    versions: dict[str, str] = {str(current): "7.20.0"}
    monkeypatch.setattr(self_cmd, "_installed_version", lambda venv: versions.get(str(venv)))
    return tools, bin_dir, uv_tools, current, versions


def _shims_are_nexus_owned(bin_dir: Path, tools: Path) -> bool:
    return all(
        not (bin_dir / ep).is_symlink()
        and (bin_dir / ep).read_text() == install_layout.render_shim(ep, tools=tools)
        for ep in ENTRY_POINTS
    )


def test_a_healthy_box_is_left_alone(bed) -> None:
    tools, bin_dir, uv_tools, current, _ = bed

    assert repair_uv_takeover() == []
    assert (tools / "current").resolve() == current.resolve()
    assert _shims_are_nexus_owned(bin_dir, tools)


def test_a_uv_managed_interpreter_link_is_left_alone(bed) -> None:
    """GH #1487 (nexus-50hm9): `nx upgrade` printed "shims python3.12 in
    ~/.local/bin were uv symlinks: rewriting them" for a uv-managed
    interpreter link (`uv python install`) that merely shares a name with the
    generation venv's own python3.12. The owned set is what the distribution
    declares, so the link is never a candidate and is never touched."""
    tools, bin_dir, uv_tools, current, versions = bed
    (current / "bin" / "python3.12").write_text("#!/bin/sh\n")
    uv_python = tools.parent / "uv-python" / "cpython-3.12" / "bin" / "python3.12"
    uv_python.parent.mkdir(parents=True)
    uv_python.write_text("#!/bin/sh\n")
    (bin_dir / "python3.12").symlink_to(uv_python)

    lines = repair_uv_takeover()

    assert lines == [], lines
    assert (bin_dir / "python3.12").is_symlink()
    assert os.readlink(bin_dir / "python3.12") == str(uv_python)
    for ep in ENTRY_POINTS:
        assert not (bin_dir / ep).is_symlink()


def test_a_pure_uv_box_is_not_a_takeover(bed) -> None:
    """No generation layout at all: that is nx self install's convergence, not a repair."""
    tools, bin_dir, uv_tools, current, versions = bed
    (tools / "current").unlink()
    legacy = _uv_tree(uv_tools)
    versions[str(legacy)] = "7.21.0"

    assert repair_uv_takeover() == []
    assert not install_layout.legacy_generation_link(tools=tools).is_symlink()


def test_taken_shims_are_rewritten_to_current_and_the_tree_registered(bed) -> None:
    """uv --force at the SAME version: nothing to build; shims back, tree registered."""
    tools, bin_dir, uv_tools, current, versions = bed
    legacy = _uv_tree(uv_tools)
    versions[str(legacy)] = "7.20.0"
    _take_shims(bin_dir, legacy)
    assert (bin_dir / "nx").is_symlink(), "fixture: shims are not taken"

    lines = repair_uv_takeover()

    assert _shims_are_nexus_owned(bin_dir, tools), "shims still resolve through uv's tree"
    assert (tools / "current").resolve() == current.resolve(), "nothing should have been built"
    assert os.readlink(install_layout.legacy_generation_link(tools=tools)) == str(legacy)
    assert any("were uv symlinks" in line for line in lines), lines
    assert any("registered for reap" in line for line in lines), lines


def test_a_newer_uv_tree_means_the_user_meant_to_upgrade(bed) -> None:
    """Build a generation at uv's version from CURRENT's receipt: [local] survives
    even though uv's rebuilt receipt lists no extras."""
    tools, bin_dir, uv_tools, current, versions = bed
    legacy = _uv_tree(uv_tools, extras_in_uv_receipt="")
    versions[str(legacy)] = "7.21.0"
    _take_shims(bin_dir, legacy)

    lines = repair_uv_takeover()

    new_current = (tools / "current").resolve()
    assert new_current != current.resolve(), "no generation was built for the newer version"
    receipt = install_layout.read_receipt(new_current)
    assert receipt.version == "7.21.0", receipt
    assert receipt.extras == ["local"], "the extras came from uv's rebuilt receipt, not current's"
    assert _shims_are_nexus_owned(bin_dir, tools)
    assert os.readlink(install_layout.legacy_generation_link(tools=tools)) == str(legacy)
    assert any("newer than current" in line for line in lines), lines
    assert current.is_dir(), "the repair must not reap; that is the next install's pass"


def test_a_rebuilt_tree_without_taken_shims_is_still_registered(bed) -> None:
    """The measured plain-install shape: uv rebuilt its tree but refused to
    overwrite the shims. Nothing to rewrite; the tree still gets registered."""
    tools, bin_dir, uv_tools, current, versions = bed
    legacy = _uv_tree(uv_tools)
    versions[str(legacy)] = "7.20.0"

    lines = repair_uv_takeover()

    assert _shims_are_nexus_owned(bin_dir, tools)
    assert os.readlink(install_layout.legacy_generation_link(tools=tools)) == str(legacy)
    assert not any("were uv symlinks" in line for line in lines), lines


def test_dry_run_describes_and_changes_nothing(bed) -> None:
    tools, bin_dir, uv_tools, current, versions = bed
    legacy = _uv_tree(uv_tools)
    versions[str(legacy)] = "7.21.0"
    _take_shims(bin_dir, legacy)

    lines = repair_uv_takeover(dry_run=True)

    assert lines, "a dry run must say what it would do"
    assert (bin_dir / "nx").is_symlink(), "dry run rewrote the shims"
    assert (tools / "current").resolve() == current.resolve(), "dry run built a generation"
    assert not install_layout.legacy_generation_link(tools=tools).is_symlink()


def test_self_install_from_uvs_tree_repairs_instead_of_migrating(bed, monkeypatch) -> None:
    """THE trap: the user's next `nx self install` runs FROM uv's tree (the
    shims point there). migrate_legacy.sh would bridge extras from uv's
    receipt -- none -- and build a [local]-less generation. The repair path
    builds from current's receipt instead."""
    tools, bin_dir, uv_tools, current, versions = bed
    legacy = _uv_tree(uv_tools, extras_in_uv_receipt="")
    versions[str(legacy)] = "7.21.0"
    _take_shims(bin_dir, legacy)
    # "Running from uv's tree": running_generation() reads sys.prefix, and the
    # packaged-vs-checkout gate reads the DISTRIBUTION's install root, which
    # in a test is the dev venv -- so patch the gate the way self_cmd's own
    # docstring says to.
    monkeypatch.setattr(sys, "prefix", str(legacy))
    monkeypatch.setattr("nexus.upgrade_finish.running_from_tool_install", lambda: True)

    result = perform_self_install()

    assert result is None, "the repair path returns None; the build path returns the generation"
    new_current = (tools / "current").resolve()
    assert new_current != current.resolve()
    assert install_layout.read_receipt(new_current).extras == ["local"], (
        "extras were bridged from uv's rebuilt receipt -- the exact way [local] is lost"
    )
    assert _shims_are_nexus_owned(bin_dir, tools)
