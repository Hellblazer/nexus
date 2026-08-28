# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A hybrid box converges: ``nx self install`` registers the legacy uv tree.
nexus-hibpr.

A generation layout beside a legacy ``uv tool install`` tree took the
generation branch of ``perform_self_install`` every time, so
``_converge_legacy_install`` -- the only thing that ever put the legacy tree
in gc.sh's ledger -- was unreachable on exactly the boxes that have one.
Every checkout-driven generation box is that box. Measured 2026-08-27: 8
processes still bound to an unregistered 7.19.0 tree, and nothing on the
machine that would ever reap it.

Registration is the convergence. Nothing is deleted: the assertions below
check the ledger POINTER and that the legacy tree is byte-identical after.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH, stub_uv

from nexus.commands.self_cmd import (
    _register_legacy_tree_if_present,
    packaged_install_dir,
    perform_self_install,
)


def _receipt(gen: Path, *, source: str) -> None:
    (gen / "nexus-install.json").write_text(json.dumps({
        "schema": 1, "version": "7.19.0",
        "spec": source, "source_kind": "directory", "source": source,
        "extras": [], "python": "3.12", "base_interpreter": "/usr/bin",
        "created_at": "2026-08-26T00:00:00Z", "installer_schema": 1,
    }))


def _hosting_generation(tools: Path, stamp: str, *, source: str) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    for ep in ("nx", "nx-mcp"):
        (gen / "bin" / ep).write_text("#!/bin/sh\n")
    (gen / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.8\n")
    _receipt(gen, source=source)
    return gen


def _legacy_tree(root: Path) -> Path:
    """A convincing ``uv tool install conexus`` venv: bin/, a python, pyvenv.cfg."""
    legacy = root / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "python").write_text("#!/bin/sh\n")
    (legacy / "bin" / "nx").write_text("#!/bin/sh\necho legacy\n")
    (legacy / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.8\n")
    return legacy


@pytest.fixture
def bed(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    src = tmp_path / "src-nexus"
    src.mkdir()
    (src / "pyproject.toml").write_text('[project]\nversion = "7.20.0"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_uv(stub_bin)
    uv_tools = tmp_path / "uv-tools"
    uv_tools.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    # uv's tool root, resolved the way uv does (nexus-orhp5): UV_TOOL_DIR wins.
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools))
    monkeypatch.setenv("PATH", f"{stub_bin}:{SAFE_BASE_PATH}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    host = _hosting_generation(tools, "20260101T000000Z", source=str(src))
    (tools / "current").symlink_to(host)
    monkeypatch.setattr(sys, "prefix", str(host))
    return tools, uv_tools


def _ledger(tools: Path) -> Path:
    return tools / "gen-legacy-uv-tool"


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_a_hybrid_box_registers_its_legacy_tree(bed) -> None:
    tools, uv_tools = bed
    legacy = _legacy_tree(uv_tools)
    before = _tree(legacy)
    assert before, "fixture is vacuous: the legacy tree has no files"

    perform_self_install()

    link = _ledger(tools)
    assert link.is_symlink(), (
        "the legacy tree was not registered; gc.sh will never reap it and the "
        "holder census will never see it (nexus-hibpr)"
    )
    assert os.readlink(link) == str(legacy), os.readlink(link)
    assert _tree(legacy) == before, "registration must never touch the legacy tree"


def test_the_first_install_never_reaps_what_it_just_registered(bed) -> None:
    """THE two-pass rule (.7): register on one pass, reap on a LATER one.
    Nothing runs from the tree here, so a reap in the same process would take
    it -- and did, when registration ran before the GC call."""
    tools, uv_tools = bed
    legacy = _legacy_tree(uv_tools)

    perform_self_install()

    assert legacy.is_dir(), "the install that registered the tree reaped it in the same pass"
    assert _ledger(tools).is_symlink()


def test_the_next_install_reaps_a_legacy_tree_nothing_runs_from(bed) -> None:
    """END-TO-END CONVERGENCE. Pass N registers; pass N+1 finds no holders and
    reaps -- tree and ledger pointer both gone. This is what the hybrid box
    could never do before nexus-hibpr."""
    tools, uv_tools = bed
    legacy = _legacy_tree(uv_tools)

    perform_self_install()
    assert _ledger(tools).is_symlink()
    perform_self_install()

    assert not legacy.exists(), "the free legacy tree survived a second install; it will never converge"
    assert not _ledger(tools).is_symlink(), "the ledger pointer dangles after the reap"


def test_a_held_legacy_tree_survives_the_next_install(bed, tmp_path, monkeypatch) -> None:
    """RULE (c) through this caller: a process on the legacy tree keeps it
    alive across as many installs as it likes. gc.sh takes its census from
    `ps` on PATH, so the holder is a stubbed ps line naming the tree."""
    tools, uv_tools = bed
    legacy = _legacy_tree(uv_tools)
    ps_bin = tmp_path / "psbin"
    ps_bin.mkdir()
    (ps_bin / "ps").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '49234 {legacy}/bin/python -m nexus.aspect_worker'\n"
    )
    (ps_bin / "ps").chmod(0o755)
    monkeypatch.setenv("PATH", f"{ps_bin}:{os.environ['PATH']}")

    perform_self_install()
    perform_self_install()

    assert legacy.is_dir(), "a HELD legacy tree was reaped; a live session lost its tree"
    assert os.readlink(_ledger(tools)) == str(legacy)


def test_registration_is_idempotent(bed) -> None:
    tools, uv_tools = bed
    legacy = _legacy_tree(uv_tools)

    assert _register_legacy_tree_if_present(packaged_install_dir(), tools) == legacy
    assert _register_legacy_tree_if_present(packaged_install_dir(), tools) == legacy

    assert os.readlink(_ledger(tools)) == str(legacy)
    assert len([p for p in tools.iterdir() if p.name.startswith("gen-legacy")]) == 1


def test_no_legacy_tree_means_no_ledger_entry(bed) -> None:
    """Non-vacuity: a clean generation box must not grow a dangling pointer."""
    tools, uv_tools = bed

    perform_self_install()

    assert not _ledger(tools).exists()
    assert not _ledger(tools).is_symlink()


def test_a_uv_tools_dir_without_a_bin_is_not_a_tree(bed) -> None:
    """``<uv tools>/conexus`` with no ``bin/`` is leftover, not something a
    process can run from; registering it would hand gc.sh a non-venv."""
    tools, uv_tools = bed
    (uv_tools / "conexus").mkdir()

    perform_self_install()

    assert not _ledger(tools).is_symlink()


def test_dry_run_registers_nothing(bed) -> None:
    tools, uv_tools = bed
    _legacy_tree(uv_tools)

    assert perform_self_install(dry_run=True) is None

    assert not _ledger(tools).is_symlink()
