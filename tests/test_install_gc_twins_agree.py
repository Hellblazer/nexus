# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``gc.sh`` dispatches to ``gc_core.py``, and the reap rules live in one place.

nexus-utpuw.6. Unlike the layout and census collapses, gc had no Python twin to
begin with: this was a PORT, so there was never a second implementation to
difference against in the tree. It was differenced against the shell half
before the wiring went in, over 18 dry-run tree shapes compared on decisions
and 8 real-deletion shapes compared on the resulting filesystem, stdout and
stderr exactly. Both harnesses were checked against deliberate breakage first:
dropping the rogue-symlink refusal failed the case that guards deleting outside
the tools root, and an off-by-one in the keep window failed 9 dry-run and 7
real cases.

What is pinned HERE is what a differential run cannot be: permanent. This is
the only code in the arc that deletes anything, so the pins below are on the
refusals and on the shape of the dispatch, not on the happy path that
``tests/scripts/test_generation_gc.py`` already covers.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_INSTALL = _REPO / "src" / "nexus" / "_install"
_GC_SH = _INSTALL / "gc.sh"
_GC_CORE = _INSTALL / "gc_core.py"


def _sh(snippet: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c",
         f'NX_LAYOUT_HOME="{_INSTALL}"; . "{_INSTALL}/layout.sh"; '
         f'. "{_INSTALL}/census.sh"; . "{_GC_SH}"; {snippet}'],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)},
    )


def _tools(tmp_path: Path, *names: str, receipts: bool = True) -> Path:
    tools = tmp_path / "tools"
    for name in names:
        (tools / name / "bin").mkdir(parents=True)
        if receipts:
            (tools / name / "nexus-install.json").write_text("{}\n")
    return tools


def test_the_shell_half_only_dispatches() -> None:
    """No reap logic left in shell. For THIS file the half-collapse hazard is
    not drift, it is two things that can delete and only one of them tested."""
    text = _GC_SH.read_text()
    match = re.search(r"^nx_gc_generations\(\)\s*\{(?P<body>.*?)^\}", text,
                      re.MULTILINE | re.DOTALL)
    assert match, "nx_gc_generations is not defined in gc.sh"
    body = match.group("body")
    for forbidden in ("rm ", "rm -", "readlink", "find ", "pyvenv.cfg"):
        assert forbidden not in body, (
            f"nx_gc_generations still does its own {forbidden!r}; the reap rules "
            f"live in gc_core.py now"
        )
    assert "_nx_gc_core" in body

    # Nothing anywhere in the file may delete. A helper outside the function
    # would satisfy the check above and still be a second deleter.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "rm -rf" not in code and "rm -f" not in code, (
        "gc.sh can still delete; the only deleter in this arc is gc_core.py"
    )


def test_a_rogue_generation_symlink_is_refused(tmp_path: Path) -> None:
    """THE data-loss case, and the reason the guard is two checks rather than
    one. Before it existed, a ``gen-rogue`` symlink pointing at an unrelated
    directory caused ``rm -rf`` of that directory; the only check was that the
    target was not literally "/", which is one value out of infinitely many
    dangerous ones."""
    tools = _tools(tmp_path, "gen-A", "gen-B")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "sub").mkdir(parents=True)
    (elsewhere / "sub" / "data").write_text("GOLD\n")
    (tools / "gen-rogue").symlink_to(elsewhere)

    r = _sh(f'nx_gc_generations --keep 1 "{tools}"', tmp_path)

    assert (elsewhere / "sub" / "data").read_text() == "GOLD\n", (
        "a rogue gen-* symlink got the sweep to delete outside the tools root"
    )
    assert (tools / "gen-rogue").is_symlink(), "the rogue pointer was removed"
    assert "refusing to reap through an unrecognised generation symlink" in r.stderr


def test_a_ledger_target_that_is_not_a_venv_keeps_its_tree(tmp_path: Path) -> None:
    """Only the reserved ledger name may be a symlink, and its target must look
    like the venv it claims to be. A wrong target has no pyvenv.cfg, so the
    POINTER is unlinked and the tree is left alone: failing that way leaves
    litter, failing the other way deletes data."""
    tools = _tools(tmp_path, "gen-A", "gen-B")
    victim = tmp_path / "not-a-venv"
    victim.mkdir()
    (victim / "precious").write_text("GOLD\n")
    (tools / "gen-legacy-uv-tool").symlink_to(victim)

    r = _sh(f'nx_gc_generations --keep 1 "{tools}"', tmp_path)

    assert (victim / "precious").read_text() == "GOLD\n"
    assert not (tools / "gen-legacy-uv-tool").exists(), "the pointer was not unlinked"
    assert "not a venv, unlinking the pointer only" in r.stderr


def test_the_legacy_uv_tree_is_reaped_through_its_ledger(tmp_path: Path) -> None:
    """The one case where the sweep deliberately deletes outside the tools root.
    Unlinking a symlink leaves its target untouched, which is exactly backwards
    for a reap whose whole job here is removing the legacy tree."""
    tools = _tools(tmp_path, "gen-A", "gen-B")
    uv_tree = tmp_path / "uv" / "conexus"
    (uv_tree / "bin").mkdir(parents=True)
    (uv_tree / "pyvenv.cfg").write_text("home = /x\n")
    (tools / "gen-legacy-uv-tool").symlink_to(uv_tree)

    r = _sh(f'nx_gc_generations --keep 1 "{tools}"', tmp_path)

    assert not uv_tree.exists(), "the legacy tree survived its reap"
    assert not (tools / "gen-legacy-uv-tool").exists()
    assert "uv no longer lists it" in r.stderr


def test_a_relative_ledger_target_is_refused(tmp_path: Path) -> None:
    """Registration only ever writes a direct absolute symlink. A relative one
    would be resolved against whatever the sweep's cwd happened to be."""
    tools = _tools(tmp_path, "gen-A", "gen-B")
    (tools / "reltarget").mkdir()
    (tools / "gen-legacy-uv-tool").symlink_to("reltarget")

    r = _sh(f'nx_gc_generations --keep 1 "{tools}"', tmp_path)

    assert (tools / "reltarget").is_dir()
    assert "not an absolute path" in r.stderr


def test_the_sweep_never_leaves_the_tools_root(tmp_path: Path) -> None:
    """The parent directory is the data-loss hazard: it also holds chroma/ and
    fastembed_cache/, which nx uninstall deliberately keeps.

    Carries its own non-vacuity assert. A sweep that reaped NOTHING would leave
    the siblings alone too, and would pass this test while proving nothing.
    """
    tools = _tools(tmp_path, "gen-A", "gen-B", "gen-C", "gen-D")
    (tools / "chroma" / "sub").mkdir(parents=True)
    (tools / "chroma" / "sub" / "vectors.bin").write_text("DATA\n")
    (tools / "fastembed_cache").mkdir()

    # THE SIBLINGS ARE AGED, and that is the whole test. Left fresh, they are
    # protected by the receipt-less grace window rather than by the gen-*
    # scoping, so a sweep that walked the WHOLE parent directory would still
    # spare them and this test would pass having proved nothing. Measured: with
    # fresh siblings, mutating the scan to `if e.is_dir()` left all 12 tests in
    # this file green.
    old = 10_000
    for path in (tools / "chroma", tools / "chroma" / "sub",
                 tools / "chroma" / "sub" / "vectors.bin", tools / "fastembed_cache"):
        os.utime(path, (old, old))

    r = _sh(f'nx_gc_generations --keep 1 "{tools}"', tmp_path)

    assert r.stdout.count("reaped ") == 3, f"nothing was reaped:\n{r.stdout}"
    assert (tools / "chroma" / "sub" / "vectors.bin").read_text() == "DATA\n"
    assert (tools / "fastembed_cache").is_dir()


@pytest.mark.parametrize("keep", ["0", "-1", "x", ""])
def test_a_bad_keep_is_refused_before_anything_is_planned(keep: str, tmp_path: Path) -> None:
    """--keep 0 would leave only the four rules between the operator and an
    install with no fallback. Refused, and refused BEFORE the sweep runs."""
    tools = _tools(tmp_path, "gen-A", "gen-B", "gen-C", "gen-D")
    r = _sh(f'nx_gc_generations --keep "{keep}" "{tools}"', tmp_path)

    assert r.returncode != 0, f"--keep {keep!r} was accepted"
    assert r.stdout.strip() == "", "a refusal also printed a plan"
    for name in ("gen-A", "gen-B", "gen-C", "gen-D"):
        assert (tools / name).is_dir(), f"{name} was reaped by a refused sweep"


def test_the_operator_knobs_still_reach_the_core(tmp_path: Path) -> None:
    """NX_GC_BUILD_GRACE_MINUTES and its claim sibling are read from the
    ENVIRONMENT, not passed through argv, because they are operator knobs and
    threading them through arguments would mean every caller had to know about
    them to leave them alone. The dispatch is where that could silently stop
    working, and nothing else would notice: the default simply applies.
    """
    tools = tmp_path / "tools"
    (tools / "gen-A" / "bin").mkdir(parents=True)
    (tools / "gen-A" / "nexus-install.json").write_text("{}\n")
    wreck = tools / "gen-W"
    (wreck / "bin").mkdir(parents=True)
    old = 10_000
    for path in (wreck, wreck / "bin"):
        os.utime(path, (old, old))

    default = _sh(f'nx_gc_generations --keep 1 --dry-run "{tools}"', tmp_path)
    assert "would reap" in default.stdout, (
        f"the aged wreckage was not reapable by default:\n{default.stdout}"
    )

    r = subprocess.run(
        ["bash", "-c",
         f'NX_LAYOUT_HOME="{_INSTALL}"; . "{_INSTALL}/layout.sh"; '
         f'. "{_INSTALL}/census.sh"; . "{_GC_SH}"; '
         f'nx_gc_generations --keep 1 --dry-run "{tools}"'],
        capture_output=True, text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "NX_GC_BUILD_GRACE_MINUTES": "999999999",
        },
    )
    assert "would reap" not in r.stdout, (
        f"the grace knob did not reach the core; it still reaped:\n{r.stdout}"
    )
    assert "build in progress" in r.stdout


def test_a_malformed_knob_falls_back_rather_than_reaping(tmp_path: Path) -> None:
    """The shell half fed these straight into `find -mmin`, where a bad value
    makes find fail, the test comes back empty, and the tree reads as not
    recently written -- the REAPING direction. The core falls back to the
    default instead, which cannot delete anything the default would keep."""
    tools = tmp_path / "tools"
    (tools / "gen-A" / "bin").mkdir(parents=True)
    (tools / "gen-A" / "nexus-install.json").write_text("{}\n")
    (tools / "gen-W" / "bin").mkdir(parents=True)  # fresh: within any sane grace

    r = subprocess.run(
        ["bash", "-c",
         f'NX_LAYOUT_HOME="{_INSTALL}"; . "{_INSTALL}/layout.sh"; '
         f'. "{_INSTALL}/census.sh"; . "{_GC_SH}"; '
         f'nx_gc_generations --keep 1 --dry-run "{tools}"'],
        capture_output=True, text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "NX_GC_BUILD_GRACE_MINUTES": "not-a-number",
        },
    )
    assert "would reap" not in r.stdout, (
        f"a malformed knob made the sweep reap a fresh tree:\n{r.stdout}"
    )
