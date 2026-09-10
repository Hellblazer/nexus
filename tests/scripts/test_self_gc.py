# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx self gc`` (nexus-xn84f): the install-time reap, callable on its own.

Why it exists: ``nx self install`` was the ONLY caller of gc.sh's reap, so a
generation a long-lived ``nx-mcp`` held at install time stayed on disk until
the next install, and on a box whose sessions live for days every generation
since those sessions started was held at every install. Measured 2026-09-09:
1.7 GB per upgrade, never reclaimed. These tests drive the same fixture bed
``nx self install`` uses, with a stubbed ``ps`` naming the holders.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from _generation_harness import SAFE_BASE_PATH, stub_uv
from click.testing import CliRunner

from test_self_install import _hosting_generation


def _stub_ps(bin_dir: Path, lines: list[str]) -> None:
    ps = bin_dir / "ps"
    ps.write_text("#!/bin/sh\ncat <<'PSEOF'\n" + "\n".join(lines) + "\nPSEOF\n")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def bed(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_uv(stub_bin)
    _stub_ps(stub_bin, ["  999 /usr/bin/vim unrelated.txt"])
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("PATH", f"{stub_bin}:{SAFE_BASE_PATH}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tools, bin_dir, stub_bin


def _five_generations(tools: Path) -> list[Path]:
    gens = [_hosting_generation(tools, f"2026010{i}T000000Z") for i in range(1, 6)]
    (tools / "current").symlink_to(gens[-1])
    return gens


def test_gc_reaps_without_installing(bed, monkeypatch) -> None:
    from nexus.commands.self_cmd import perform_self_gc  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, _ = bed
    gens = _five_generations(tools)
    monkeypatch.setattr(sys, "prefix", str(gens[-1]))

    lines = perform_self_gc(keep=2)

    assert lines is not None
    assert not gens[0].exists() and not gens[1].exists() and not gens[2].exists(), lines
    assert gens[3].exists() and gens[4].exists()
    assert (tools / "current").resolve() == gens[-1].resolve(), "the reap moved current"
    assert any("reaped" in line for line in lines), lines


def test_a_held_generation_is_kept_and_named(bed, monkeypatch) -> None:
    from nexus.commands.self_cmd import perform_self_gc  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, stub_bin = bed
    gens = _five_generations(tools)
    _stub_ps(stub_bin, [f" 4242 {gens[0]}/bin/python -m nexus.mcp"])
    monkeypatch.setattr(sys, "prefix", str(gens[-1]))

    lines = perform_self_gc(keep=2)

    assert gens[0].exists(), "a held generation was reaped"
    assert not gens[1].exists(), "the free generation survived (the test proved nothing)"
    assert any(line.startswith(f"kept {gens[0]}: held by 4242") for line in lines or []), lines


def test_gc_never_reaps_the_generation_it_runs_from(bed, monkeypatch) -> None:
    """Rule (d), passed by this caller: the running process's tree is outside
    the keep window here and still survives."""
    from nexus.commands.self_cmd import perform_self_gc  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, _ = bed
    gens = _five_generations(tools)
    monkeypatch.setattr(sys, "prefix", str(gens[0]))

    perform_self_gc(keep=1)

    assert gens[0].exists(), "the generation hosting the reaper was reaped"
    assert not gens[1].exists()


def test_dry_run_deletes_nothing(bed, monkeypatch) -> None:
    from nexus.commands.self_cmd import perform_self_gc  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, _ = bed
    gens = _five_generations(tools)
    monkeypatch.setattr(sys, "prefix", str(gens[-1]))

    lines = perform_self_gc(keep=1, dry_run=True)

    assert all(g.exists() for g in gens)
    assert sum(1 for line in lines or [] if line.startswith("would reap")) == 4, lines


def test_no_generation_layout_is_a_silent_no_op(bed, monkeypatch) -> None:
    """The SessionStart hook runs this on every box; a dev checkout or a
    legacy uv box has nothing to reap and must say nothing."""
    from nexus.commands.self_cmd import perform_self_gc  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, _ = bed
    monkeypatch.setattr(sys, "prefix", str(tools.parent / "checkout" / ".venv"))

    assert perform_self_gc() is None
    assert not any(p.name.startswith("gen-") for p in tools.iterdir())


def test_the_click_surface(bed, monkeypatch) -> None:
    from nexus.commands.self_cmd import self_group  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, _ = bed
    gens = _five_generations(tools)
    monkeypatch.setattr(sys, "prefix", str(gens[-1]))

    result = CliRunner().invoke(self_group, ["gc", "--keep", "2"])

    assert result.exit_code == 0, result.output
    assert "reaped" in result.output
    assert not gens[0].exists()


def test_prune_uv_cache_flag_runs_uv_cache_prune(bed, monkeypatch) -> None:
    from nexus.commands.self_cmd import self_group  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, stub_bin = bed
    gens = _five_generations(tools)
    monkeypatch.setattr(sys, "prefix", str(gens[-1]))
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _record(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)):
            calls.append([str(c) for c in cmd])
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _record)

    result = CliRunner().invoke(self_group, ["gc", "--prune-uv-cache"])

    assert result.exit_code == 0, result.output
    assert ["uv", "cache", "prune"] in calls, calls
    assert "uv cache prune" in result.output


def test_install_prunes_the_uv_cache_after_a_flip(bed, monkeypatch) -> None:
    """nexus-xn84f: the wheel archive every build feeds is emptied of
    unreachable objects after every successful install."""
    from nexus.commands import self_cmd  # noqa: PLC0415 — deferred, matches the file's other in-test imports

    tools, _, _ = bed
    gens = _five_generations(tools)
    monkeypatch.setattr(sys, "prefix", str(gens[-1]))
    monkeypatch.setattr(self_cmd, "perform_self_install", lambda **kw: gens[-1])
    pruned: list[bool] = []
    monkeypatch.setattr(self_cmd, "prune_uv_cache", lambda: pruned.append(True) or "uv cache prune: ok")

    result = CliRunner().invoke(self_cmd.self_group, ["install"])

    assert result.exit_code == 0, result.output
    assert pruned == [True]
