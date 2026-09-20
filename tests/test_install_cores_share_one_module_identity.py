# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``_install`` cores must resolve to ONE module object per process.

Each core reaches its neighbours through a ``_sibling`` resolver, because at
bootstrap there is no package to import from. The obvious implementation loads
the neighbour by path every time, and that is wrong in a way nothing else
notices:

  - ``LayoutError`` becomes several classes, so ``except InstallLayoutError``
    in health.py and self_cmd.py stops catching errors raised inside a
    path-loaded copy;
  - a test patching ``nexus._install.layout_core`` does not reach the caller;
  - each copy carries its own module state.

Measured before the fix: gc_core loaded a layout_core, census_core loaded
another under the SAME sys.modules key and clobbered it, and three distinct
layout_core objects coexisted in one process, with three distinct LayoutError
classes. Nothing in the suite failed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_INSTALL = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install"


def test_the_package_world_shares_one_layout_core() -> None:
    from nexus._install import census_core, gc_core
    from nexus._install import layout_core as package_layout

    assert gc_core._layout() is package_layout
    assert census_core._sibling_layout() is package_layout
    assert gc_core._census() is census_core


def test_the_package_world_shares_one_error_class() -> None:
    """The one with teeth. A second LayoutError is invisible until something
    raises it through a catcher that was written against the first."""
    from nexus._install import gc_core
    from nexus.install_layout import InstallLayoutError

    assert gc_core._layout().LayoutError is InstallLayoutError


def test_the_bootstrap_world_also_shares_one(tmp_path: Path) -> None:
    """Same claim where the package does not exist, which is the branch that
    actually path-loads and therefore the one that can duplicate."""
    program = f'''
import sys, runpy
sys.modules["nexus"] = None
sys.path.insert(0, {str(_INSTALL)!r})
gc = runpy.run_path({str(_INSTALL / "gc_core.py")!r}, run_name="gc_probe")
layout_via_gc = gc["_layout"]()
census_via_gc = gc["_census"]()
layout_via_census = census_via_gc._sibling_layout()
assert layout_via_gc is layout_via_census, "two layout_core objects at bootstrap"
assert layout_via_gc.LayoutError is layout_via_census.LayoutError
print("OK")
'''
    r = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_every_core_agrees_on_the_usage_exit() -> None:
    """Each core restates EX_USAGE rather than importing it, so a refusal never
    depends on a sibling loading. Restating is the duplication this arc
    removes, so the three are pinned equal."""
    from nexus._install.census_core import CENSUS_USAGE_EXIT
    from nexus._install.gc_core import GC_USAGE_EXIT
    from nexus._install.layout_core import LAYOUT_USAGE_EXIT

    assert LAYOUT_USAGE_EXIT == CENSUS_USAGE_EXIT == GC_USAGE_EXIT == 64
