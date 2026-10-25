# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The on-demand PDF install and the upgrade rule, through the REAL builder.
nexus-2k66t.4.2.

Two things are proven here against ``install_generation.sh`` with a fake ``uv``
that records every argv:

1. The on-demand install (``perform_self_install(add_extras=("pdf",),
   version=<running>)``) reaches uv with the generation builder's own
   arguments: the ``conexus[pdf]==X`` spec (the version pin keeps the fetch from
   being an upgrade), CPU torch on Linux, the packaged overrides and the
   constraints the NEW package ships. A bare ``uv pip install conexus[pdf]``
   would pull CUDA torch on Linux.
2. The upgrade rule (Sam, 2026-10-23): an existing install ALWAYS carries the
   stack forward; only a fresh install goes slim. "Existing" is a receipt
   written before ``pdf`` became an extra (``installer_schema`` below 2).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH
from test_install_generation import _INSTALLER, _final_install, _installs, _make_executable, _stub_uv

from nexus import install_layout
from tests._module_seam import setattr_in


def _receipt(gen: Path, *, extras: list[str], installer_schema: int, source: str) -> None:
    spec = f"conexus[{','.join(extras)}]" if extras else "conexus"
    (gen / "nexus-install.json").write_text(json.dumps({
        "schema": 1, "version": "7.85.0", "spec": spec, "source_kind": "registry", "source": source,
        "extras": extras, "python": "3.12", "base_interpreter": "/usr/bin",
        "created_at": "2026-10-01T00:00:00Z", "installer_schema": installer_schema,
    }))


def _host(tools: Path, stamp: str, *, extras: list[str], installer_schema: int) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    for ep in ("nx", "nx-mcp", "nx-mcp-catalog", "nx-session-end-launcher", "nx-hook"):
        (gen / "bin" / ep).write_text("#!/bin/sh\n")
    (gen / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.8\n")
    _receipt(gen, extras=extras, installer_schema=installer_schema, source="conexus")
    return gen


@pytest.fixture
def bed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    argv_log = tmp_path / "uv-argv.log"
    _stub_uv(stub_bin, argv_log=argv_log)
    _make_executable(stub_bin / "uname", "#!/bin/bash\necho Linux\n")
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("PATH", f"{stub_bin}:{SAFE_BASE_PATH}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NX_TORCH_BACKEND", raising=False)
    monkeypatch.delenv("NX_INSTALL_UNPINNED", raising=False)
    # These tests read the RUNNING installer's arguments off the stub uv; the
    # target-installer fetch (nexus-2k66t.4.3) would add a call of its own.
    monkeypatch.setenv("NX_INSTALL_RUNNING_INSTALLER", "1")
    return tools, argv_log


def _run_as_host(monkeypatch, host: Path, **kw):
    from nexus.commands.self_cmd import perform_self_install

    setattr_in(monkeypatch, ("nexus.commands.self_cmd", "nexus.upgrade_finish"), "sys.prefix", str(host))
    return perform_self_install(**kw)


def _new_receipt(tools: Path) -> install_layout.Receipt:
    return install_layout.read_receipt((tools / "current").resolve())


# ── 1. the on-demand install reaches uv with the builder's own arguments ─────


def test_the_on_demand_install_uses_the_builders_arguments_on_linux(bed, monkeypatch) -> None:
    tools, argv_log = bed
    host = _host(tools, "20260101T000000Z", extras=[], installer_schema=2)  # a fresh, slim install
    (tools / "current").symlink_to(host)

    _run_as_host(monkeypatch, host, add_extras=("pdf",), version="9.9.9")

    installs = _installs(argv_log)
    assert "conexus[pdf]==9.9.9" in installs[0], installs[0]  # the version pin: not an upgrade
    assert "--no-deps" in installs[0]
    final = _final_install(argv_log)
    assert "conexus[pdf]==9.9.9" in final
    assert "--torch-backend cpu" in final, "a bare install would resolve CUDA torch on Linux"
    assert "--overrides" in final and "overrides.txt" in final
    shipped = "site-packages/nexus/_install/constraints.txt"
    assert "--constraints" in final and shipped in final, "the NEW package's pins, not the running nx's"
    receipt = _new_receipt(tools)
    assert receipt.extras == ["pdf"] and receipt.spec == "conexus[pdf]==9.9.9"
    assert receipt.installer_schema == install_layout.INSTALLER_SCHEMA


def test_the_pdf_extra_is_kept_by_every_later_install(bed, monkeypatch) -> None:
    """The receipt records the extra, so `nx self install`, the lockstep and
    claude-restart-sessions carry it with no PDF-specific code."""
    tools, argv_log = bed
    host = _host(tools, "20260101T000000Z", extras=[], installer_schema=2)
    (tools / "current").symlink_to(host)
    _run_as_host(monkeypatch, host, add_extras=("pdf",), version="9.9.9")
    with_pdf = (tools / "current").resolve()

    argv_log.write_text("")
    _run_as_host(monkeypatch, with_pdf)  # a plain upgrade, no flags
    assert "conexus[pdf]" in _final_install(argv_log)
    assert "pdf" in _new_receipt(tools).extras


# ── 2. the upgrade rule ──────────────────────────────────────────────────────


def test_an_existing_installs_first_upgrade_keeps_the_stack_unconditionally(bed, monkeypatch) -> None:
    """A receipt written before the split (installer_schema 1) has never listed
    `pdf`, because the stack was a default dependency. The rebuild adds it."""
    tools, argv_log = bed
    host = _host(tools, "20260101T000000Z", extras=["local"], installer_schema=1)
    (tools / "current").symlink_to(host)

    _run_as_host(monkeypatch, host)

    assert "conexus[local,pdf]" in _final_install(argv_log), _final_install(argv_log)
    receipt = _new_receipt(tools)
    assert receipt.extras == ["local", "pdf"]
    # And the new receipt is split-aware, so the carry is not repeated as a
    # standing rule: it is now an ordinary extra the user can see.
    assert receipt.installer_schema == install_layout.INSTALLER_SCHEMA >= install_layout.PDF_SPLIT_INSTALLER_SCHEMA


def test_a_fresh_slim_installs_next_upgrade_stays_slim(bed, monkeypatch) -> None:
    tools, argv_log = bed
    host = _host(tools, "20260101T000000Z", extras=[], installer_schema=2)
    (tools / "current").symlink_to(host)

    _run_as_host(monkeypatch, host)

    assert "conexus[pdf" not in _final_install(argv_log)
    assert _new_receipt(tools).extras == []


def test_a_receipt_without_the_installer_schema_field_reads_as_pre_split() -> None:
    payload = {
        "schema": 1, "version": "7.0.0", "spec": "conexus", "source_kind": "registry", "source": "conexus",
        "python": "3.12", "base_interpreter": "/usr/bin", "created_at": "2026-01-01T00:00:00Z",
    }
    receipt = install_layout.Receipt.from_dict(payload)
    assert receipt.installer_schema == 1
    assert install_layout.receipt_predates_pdf_split(receipt)
    assert not install_layout.receipt_predates_pdf_split(
        install_layout.Receipt.from_dict({**payload, "installer_schema": 2}),
    )


def test_the_builder_stamps_the_split_aware_schema_on_a_fresh_generation(tmp_path: Path) -> None:
    """The shell half writes the receipt a Python reader classifies."""
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    argv_log = tmp_path / "uv-argv.log"
    _stub_uv(bin_dir, argv_log=argv_log)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NX_TOOLS_DIR": str(tools)}
    proc = subprocess.run(
        ["bash", str(_INSTALLER), "--source", "conexus", "--version", "9.9.9"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    gen = Path(proc.stdout.strip().splitlines()[-1])
    receipt = install_layout.read_receipt(gen)
    assert receipt.installer_schema == 2 and receipt.extras == []
    assert not install_layout.receipt_predates_pdf_split(receipt)


def test_a_legacy_uv_tree_moving_onto_generations_carries_the_stack(tmp_path: Path) -> None:
    """migrate_legacy.sh bridges the legacy receipt's extras one last time; a
    legacy tree predates the split by definition, so `pdf` joins them."""
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    argv_log = tmp_path / "uv-argv.log"
    _stub_uv(bin_dir, argv_log=argv_log)
    legacy = tmp_path / "uvtools" / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "conexus", extras = ["local"] }]\n',
    )
    migrate = _INSTALLER.parent / "migrate_legacy.sh"
    env = {
        **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NX_TOOLS_DIR": str(tools),
        "NX_BIN_DIR": str(tmp_path / "binout"),
    }
    proc = subprocess.run(
        ["bash", str(migrate), "--source", "conexus", "--legacy-venv", str(legacy)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "conexus[local,pdf]" in _final_install(argv_log), _final_install(argv_log)


# ── the checkout flow: scripts/reinstall-tool.sh reads the same receipt ─────

_REINSTALL = Path(__file__).resolve().parents[2] / "scripts" / "reinstall-tool.sh"


def _full_receipt(**over) -> dict:
    """A complete receipt as an installer writes it, with *over* applied (None deletes a key)."""
    payload = {
        "schema": 1, "version": "7.0.0", "spec": "conexus", "source_kind": "registry", "source": "conexus",
        "python": "3.12.8", "base_interpreter": "/py", "created_at": "2026-10-01T00:00:00Z",
        "extras": [], "installer_schema": 2, "constraints": "applied",
    }
    for key, value in over.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    extras = sorted(set(payload["extras"]))
    payload["spec"] = f"conexus[{','.join(extras)}]" if extras else "conexus"  # one fact, not two
    return payload


def _reinstall_extras(tmp_path: Path, receipt: dict) -> str:
    """The extras reinstall-tool.sh carries forward, through the shell function it calls
    (``nx_receipt_extras``, layout.sh -> layout_core.read_receipt): the real reader, not a copy."""
    generation = tmp_path / "gen-20261001T000000Z"
    generation.mkdir(exist_ok=True)
    (generation / "nexus-install.json").write_text(json.dumps(receipt))
    install = _REINSTALL.parents[1] / "src" / "nexus" / "_install"
    proc = subprocess.run(
        ["bash", "-c", f'export NX_LAYOUT_HOME="{install}"; . "{install}/layout.sh" && nx_receipt_extras "{generation}"'],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize("receipt, expected", [
    (_full_receipt(extras=["local"], installer_schema=1), "local,pdf"),  # an existing install carries the stack
    (_full_receipt(extras=[], installer_schema=1), "pdf"),
    (_full_receipt(extras=["local"], installer_schema=None), "local,pdf"),  # no field at all reads as pre-split
    (_full_receipt(extras=["local"], installer_schema=2), "local"),  # split-aware and slim stays slim
    (_full_receipt(extras=["local", "pdf"], installer_schema=2), "local,pdf"),
    (_full_receipt(extras=["local", "mineru"], installer_schema=2), "local"),  # the retired extra is dropped
    # nexus-ff366: a receipt from a newer installer (a later schema, a field this one never heard of)
    (_full_receipt(extras=["local"], schema=7, field_from_the_future={"x": [1]}), "local"),
])
def test_reinstall_tool_carries_the_stack_for_a_pre_split_receipt_only(tmp_path, receipt, expected) -> None:
    assert _reinstall_extras(tmp_path, receipt) == expected


def test_reinstall_tool_no_longer_parses_the_receipt_in_inline_python() -> None:
    text = _REINSTALL.read_text()
    assert "nx_receipt_extras" in text and "nx_receipt_kind" in text
    assert "json.load(open(os.environ" not in text, "a private copy of the reader drifts from the real one"
