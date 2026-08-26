# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`nx doctor` under the generation layout. nexus-utpuw.11.

TWO FAILURES, RELATED BY THE SAME CAUSE.

1. "Process freshness" rendered SILENT GREEN -- the exact failure it exists to
   prevent. It was born from three live incidents (6.7.0/6.7.1) where doctor
   said "latest" and the whole machine was stale. Under generations
   ``report.stale`` was empty BY CONSTRUCTION (nexus-utpuw.10: the markers were
   pinned to the current generation, while a stale process by definition runs
   from a different one), so the green branch fired unconditionally and said
   "all running conexus processes match the installed version" having examined
   NOTHING.

   .10 fixed the enumeration. This fixes the sentence: a green must state what
   it examined, because "no stale processes" and "no processes found at all"
   are different answers that rendered identically. A green over an empty
   examined-set is vacuously true, and a vacuous truth in a health check is
   indistinguishable from a real one at the point it matters.

   Note what was ALREADY fixed and is not this: nexus-bawvu closed the
   VANISHING-ROW mode, where a probe failure returned [] and the row disappeared
   entirely. That remains closed; the tests below do not re-litigate it.

2. Doctor knew nothing about the layout at all. health.py carried ZERO
   references to install_layout, so every way the generation layout can be
   broken -- a dangling `current`, a receipt-less build, uv taking the shims
   back -- was invisible to the one command whose job is noticing.

FAILURE DIRECTION, stated once and applied throughout: uncertain means SAY SO.
A check that cannot determine its answer warns; it never returns ok. This arc
has paid for the other direction repeatedly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus import health


def _receipt(version: str = "7.18.0") -> str:
    return json.dumps({
        "schema": 1, "version": version, "spec": "conexus",
        "source_kind": "directory", "source": "/src/nexus", "extras": "",
        "python": "3.12", "base_interpreter": "/opt/py/bin",
        "created_at": "2026-08-26T00:00:00Z", "installer_schema": 1,
    })


def _generation(tools: Path, stamp: str, *, receipt: bool = True) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    # Real entry points: the shim check derives the names it owns from
    # <current>/bin rather than carrying a hardcoded list, so a generation
    # without them would make that check vacuous.
    for ep in ("nx", "nx-mcp"):
        (gen / "bin" / ep).write_text("#!/bin/sh\n")
    if receipt:
        (gen / "nexus-install.json").write_text(_receipt())
    return gen


@pytest.fixture
def layout(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    return tools, bin_dir


def _result(results, label_fragment):
    for r in results:
        if label_fragment.lower() in r.label.lower():
            return r
    raise AssertionError(f"no result labelled like {label_fragment!r}: {[r.label for r in results]}")


# --------------------------------------------------------------------------
# 1. a green must say what it examined
# --------------------------------------------------------------------------

def test_freshness_green_states_how_many_processes_it_examined(monkeypatch) -> None:
    """The silent green, killed at the sentence rather than the probe. With
    processes examined and none stale, the detail must say so with a COUNT --
    otherwise this row reads identically whether it checked 12 processes or
    zero."""
    from nexus import upgrade_finish as uf

    report = uf.SkewReport(installed_version="7.18.0", install_mtime=0.0)
    monkeypatch.setattr(uf, "detect_stale_processes", lambda: report)
    monkeypatch.setattr(uf, "install_source", lambda: "directory — /src/nexus")
    monkeypatch.setattr(uf, "enumerate_processes", lambda *a, **k: [
        (1, 10, "/g/bin/python /g/bin/nx-mcp"),
        (2, 10, "/g/bin/nx daemon service start --foreground"),
    ])

    row = _result(health._check_process_skew(), "Process freshness")

    assert row.ok is True
    assert "2" in row.detail, (
        f"a green that does not say what it examined is the silent green: {row.detail!r}"
    )


def test_freshness_green_says_plainly_when_it_examined_nothing(monkeypatch) -> None:
    """Zero conexus processes running is a legitimate state -- a fresh box with
    nothing started. What is NOT legitimate is rendering it as 'all running
    conexus processes match the installed version', which is a vacuous truth
    dressed as a positive finding. It must say it found none."""
    from nexus import upgrade_finish as uf

    report = uf.SkewReport(installed_version="7.18.0", install_mtime=0.0)
    monkeypatch.setattr(uf, "detect_stale_processes", lambda: report)
    monkeypatch.setattr(uf, "install_source", lambda: "directory — /src/nexus")
    monkeypatch.setattr(uf, "enumerate_processes", lambda *a, **k: [])

    row = _result(health._check_process_skew(), "Process freshness")

    detail = row.detail.lower()
    assert "no running conexus processes" in detail or "none" in detail, (
        f"an empty examined-set rendered as a positive finding: {row.detail!r}"
    )
    assert "all running conexus processes match" not in detail, (
        "claimed every process matches, having examined none"
    )


# --------------------------------------------------------------------------
# 2. the layout itself
# --------------------------------------------------------------------------

def test_a_healthy_layout_passes(layout) -> None:
    """Non-vacuity for everything below: a correct layout must be quiet, or the
    checks below prove nothing by failing."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\nexec \"$(readlink /t/current)/bin/nx\" \"$@\"\n")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is True, row.detail


def test_a_dangling_current_pointer_is_a_hard_failure(layout) -> None:
    """`current` is what every shim resolves at spawn. Dangling means nothing
    starts -- not a warning."""
    tools, _ = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    import shutil
    shutil.rmtree(gen)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False and row.warn is False, (
        f"a dangling current pointer did not fail: ok={row.ok} warn={row.warn}"
    )
    assert "current" in row.detail.lower()


def test_a_uv_owned_shim_symlink_is_reported(layout) -> None:
    """nexus-utpuw.7's ACCEPTED RISK, made visible. Between migration and reap
    uv still holds a valid receipt, so a stray `uv tool upgrade conexus`
    re-symlinks ~/.local/bin/nx over the nexus-owned shim and live sessions
    start resolving through uv's tree again. The mitigation is that re-running
    the installer repairs it -- which only helps if somebody knows to."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    uv_tree = tools.parent / "uvtools" / "conexus" / "bin"
    uv_tree.mkdir(parents=True)
    (uv_tree / "nx").write_text("#!/bin/sh\necho uv\n")
    (bin_dir / "nx").symlink_to(uv_tree / "nx")

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False, "uv taking the shim back was reported as healthy"
    assert "nx" in row.detail, row.detail


def test_a_receiptless_generation_directory_is_not_a_failure(layout) -> None:
    """Build wreckage, not breakage. A gen-* directory without a receipt is a
    build that died before finishing; nothing ever pointed `current` at it and
    GC reaps it. Reporting it as a fault would train the operator to ignore this
    row, which is how a real fault gets missed."""
    tools, bin_dir = layout
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    (bin_dir / "nx").write_text("#!/bin/sh\n")
    _generation(tools, "20260826T020000Z", receipt=False)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is True, (
        f"receipt-less build wreckage was reported as a fault: {row.detail!r}"
    )


def test_an_unreadable_layout_warns_rather_than_passing(layout, monkeypatch) -> None:
    """The failure direction, asserted rather than assumed. A check that cannot
    determine its answer must say so; returning ok would be the silent green
    this bead exists to remove, relocated one function over."""
    from nexus import install_layout

    def _boom(*a, **k):
        raise OSError("layout unreadable")

    monkeypatch.setattr(install_layout, "current_generation", _boom)

    row = _result(health._check_generation_layout(), "Generation layout")

    assert row.ok is False, "an unreadable layout passed"
    assert "could not" in row.detail.lower() or "unreadable" in row.detail.lower(), row.detail
