# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The Python and shell statements of holder attribution must agree.

nexus-utpuw.10. The shell half already says this is coming -- census.sh's header
reads "src/nexus/upgrade_finish.py:50 hardcodes _PROC_MARKERS ... .10 does the
same for the Python side."

Two implementations exist for the same reason ``layout.sh`` and
``install_layout.py`` both exist (see tests/test_install_layout_twins_agree.py):
the callers have incompatible import constraints. ``_install/census.sh`` is
sourced by GC and the installer, which run with NOTHING installed and cannot
import nexus. ``install_census.py`` is imported by ``upgrade_finish.py`` and
``health.py``, which run after the install and can.

WHY THIS PINS RATHER THAN TRUSTS. The bug this bead fixes IS a drifted marker:
upgrade_finish's ``_PROC_MARKERS`` and ``running_from_tool_install()`` both
hardcode ``uv/tools/conexus``, which stopped matching the moment the layout
moved -- and nothing failed, it just silently stopped finding anything. A second
copy of holder attribution that drifts the same way would restore exactly the
defect being removed, so the two halves are compared against the SAME snapshot
rather than each being tested against its own idea of one.

The snapshots below are the real argv shapes, carried over from the census
suite: a shell-script holder, the four daemon classes, a shebang-wrapped MCP
server, a stamp-collision sibling, and a process that merely mentions a path
inside the tree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.install_census import generation_holder_pids

_REPO = Path(__file__).resolve().parent.parent
_CENSUS_SH = _REPO / "src" / "nexus" / "_install" / "census.sh"


def _shell_pids(generation: str, snapshot: str) -> list[int]:
    """What the shell half says, given the same snapshot."""
    r = subprocess.run(
        ["bash", "-c", f'. "{_CENSUS_SH}"; nx_generation_holder_pids "$1" "$2"',
         "_", generation, snapshot],
        capture_output=True, text=True, timeout=60,
    )
    return [int(t) for t in r.stdout.split()]


def _snapshot(*lines: str) -> str:
    return "\n".join(lines)


@pytest.fixture
def gen(tmp_path):
    g = tmp_path / "tools" / "gen-A"
    (g / "bin").mkdir(parents=True)
    (g / "nexus-install.json").write_text("{}")
    return g


_CASES = {
    "python-hosted-mcp": ["  101 {gen}/bin/python {gen}/bin/nx-mcp"],
    "storage-service": ["  102 {gen}/bin/nx daemon service start --foreground"],
    "aspect-worker": ["  103 {gen}/bin/nx daemon aspect-worker start --tenant default"],
    # The only canonical holder whose argv does not begin <gen>/bin/nx.
    "mineru": ["  104 {gen}/bin/mineru-api --port 8899"],
    "in-flight-nx": ["  105 {gen}/bin/nx search grep"],
    "merely-mentions": ["  106 /usr/bin/vim {gen}/README.txt"],
    "no-holders": ["  107 /usr/bin/vim unrelated.txt"],
    "several": [
        "  108 {gen}/bin/python {gen}/bin/nx-mcp",
        "  109 {gen}/bin/nx daemon service start --foreground",
        "  110 /usr/bin/vim unrelated.txt",
    ],
}


@pytest.mark.parametrize("case", sorted(_CASES), ids=sorted(_CASES))
def test_both_halves_attribute_the_same_pids(gen, case) -> None:
    snapshot = _snapshot(*(line.format(gen=gen) for line in _CASES[case]))

    shell = _shell_pids(str(gen), snapshot)
    python = generation_holder_pids(gen, snapshot=snapshot)

    assert python == shell, (
        f"the halves disagree on '{case}': shell={shell} python={python}. "
        "A drifted second copy of holder attribution is the defect nexus-utpuw.10 "
        "exists to remove, not a thing to add."
    )


def test_both_halves_ignore_a_stamp_collision_sibling(tmp_path) -> None:
    """install_generation.sh suffixes a same-second collision, so gen-<stamp>
    and gen-<stamp>a coexist by design. Neither half may borrow the other's
    holders -- the shell half enforces it with a '/' path boundary."""
    tools = tmp_path / "tools"
    base = tools / "gen-20260826T0100Z"
    sibling = tools / "gen-20260826T0100Za"
    for d in (base, sibling):
        (d / "bin").mkdir(parents=True)
        (d / "nexus-install.json").write_text("{}")
    snapshot = _snapshot(f"  201 {sibling}/bin/python {sibling}/bin/nx-mcp")

    assert _shell_pids(str(base), snapshot) == []
    assert generation_holder_pids(base, snapshot=snapshot) == []


def test_both_halves_resolve_a_pseudo_generation_symlink(tmp_path) -> None:
    """.7 registers the legacy uv tree as a gen-* SYMLINK pointing outside
    tools/. A live holder's argv names the REAL path it exec'd from, never the
    ledger pointer, so both halves must resolve one level before matching."""
    tools = tmp_path / "tools"
    tools.mkdir()
    real = tmp_path / "uvtools" / "conexus"
    (real / "bin").mkdir(parents=True)
    ledger = tools / "gen-legacy-uv-tool"
    ledger.symlink_to(real)
    snapshot = _snapshot(f"  301 {real}/bin/python {real}/bin/nx-mcp")

    shell = _shell_pids(str(ledger), snapshot)
    python = generation_holder_pids(ledger, snapshot=snapshot)

    assert shell == [301], f"shell half failed to resolve the ledger: {shell}"
    assert python == shell


def test_both_halves_refuse_the_filesystem_root(tmp_path) -> None:
    """"/" normalises to the empty string, and an empty match would make the
    boundary pattern "/" -- every process on the machine a holder of
    everything. The shell half refuses with a usage exit (nexus-qzawu)."""
    snapshot = _snapshot("  401 /usr/bin/vim notes.txt")

    assert _shell_pids("/", snapshot) == []
    with pytest.raises(ValueError):
        generation_holder_pids("/", snapshot=snapshot)
