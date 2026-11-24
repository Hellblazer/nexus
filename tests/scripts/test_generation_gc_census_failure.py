# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generation GC must not reap on a process census it could not take.

Rule (c) keeps a generation a live process runs from, and the census behind it
(``census_core.ps_snapshot``) returns an empty string when ``ps`` fails, times
out or is not on PATH. ``gc_core.plan`` read that empty view as "no process
holds anything" and reaped every complete generation outside the keep window
and the pointers: the under-reporting direction the census docstrings name as
the one that lets a live tree look free.

Incident 2026-11-21/22: four long-lived MCP servers (tamsin, elsinore, kestrel,
and a ~/git/conexus session) ran from install generations that were deleted
while they ran; their next HTTP client build failed on the vanished CA bundle
and T1 scratch broke for the rest of each session. The census matches today's
nx-mcp argv (``<gen>/bin/python <gen>/bin/nx-mcp``, pinned by
``test_rule_c_never_deletes_a_generation_with_a_live_holder``), and gc_core is
the only code that deletes a complete generation, so an empty census is the
path in this code by which a held generation goes. Which process ran the reap
with a failing ``ps`` was not recoverable (the SessionStart sweep discards its
output).

A real process table is never empty (``ps`` lists at least itself and its
parent), so an empty one is a census failure: complete generations are kept
and said so; receipt-less wreckage, which nothing can run from, is still reaped.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from tests.scripts.test_generation_gc import _gen, _names, _point, _sh


def _ps_script(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    ps = bin_dir / "ps"
    # A working stub lists the process that ran it ($PPID, gc_core), as a real ps does.
    self_row = "" if "exit " in body else "echo \"$PPID python3 gc_core.py\"\n"
    ps.write_text("#!/bin/sh\n" + self_row + body + "\n")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)


def _layout(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    tools = tmp_path / "tools"
    tools.mkdir()
    gens = [_gen(tools, f"{i:02d}") for i in range(6)]
    _point(tools, "current", gens[5])
    _point(tools, "previous", gens[4])
    return tools, tmp_path / "stubbin", gens


def test_a_failing_ps_reaps_no_generation(tmp_path: Path) -> None:
    """``ps`` exits non-zero: the census is unknown, so nothing a process could
    be running from is deleted, and the kept trees are named."""
    tools, stub_bin, gens = _layout(tmp_path)
    _ps_script(stub_bin, "echo 'ps: sysctl: Operation not permitted' >&2; exit 1")

    result = _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert result.returncode == 0, result.stderr
    assert _names(tools) == {g.name for g in gens}, (
        "a gc whose process census failed reaped generations a live MCP server may have been running from"
    )
    assert "process census unavailable" in result.stdout, result.stdout


def test_an_empty_process_table_reaps_no_generation(tmp_path: Path) -> None:
    """``ps`` exits 0 and prints nothing (a stripped or sandboxed ``ps``): a real
    table always lists ``ps`` itself, so empty is a failure, not "no holders"."""
    tools, stub_bin, gens = _layout(tmp_path)
    _ps_script(stub_bin, "exit 0")

    result = _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert result.returncode == 0, result.stderr
    assert _names(tools) == {g.name for g in gens}


def test_a_failed_census_still_reaps_receipt_less_wreckage(tmp_path: Path) -> None:
    """A tree with no receipt was never pointed at and nothing runs from it; a
    census failure is no reason to keep it."""
    tools, stub_bin, gens = _layout(tmp_path)
    wreck = tools / "gen-wreck"
    (wreck / "bin").mkdir(parents=True)
    old = 1_000_000_000  # long past the build-in-progress grace window
    os.utime(wreck / "bin", (old, old))
    os.utime(wreck, (old, old))
    _ps_script(stub_bin, "exit 1")

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert not wreck.exists(), "receipt-less wreckage survived a census failure"
    assert {g.name for g in gens} <= _names(tools)


def test_a_working_census_still_reaps_an_unheld_generation(tmp_path: Path) -> None:
    """The control: with a census that lists processes and none of them run from
    the old trees, the old trees go, so the refusal above is about the census."""
    tools, stub_bin, gens = _layout(tmp_path)
    _ps_script(stub_bin, "echo '  999 /usr/bin/vim unrelated.txt'")

    _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert _names(tools) == {gens[4].name, gens[5].name}


def test_a_live_mcp_server_keeps_its_generation(tmp_path: Path) -> None:
    """A live nx-mcp, argv as ps shows it today, keeps the generation it runs
    from through a reap that removes everything else outside the window."""
    tools, stub_bin, gens = _layout(tmp_path)
    _ps_script(stub_bin, f"echo ' 4592 {gens[0]}/bin/python {gens[0]}/bin/nx-mcp'")

    result = _sh("nx_gc_generations --keep 2", tools, stub_bin)

    assert _names(tools) == {gens[0].name, gens[4].name, gens[5].name}, result.stdout
    assert f"kept {gens[0]}: held by 4592" in result.stdout
