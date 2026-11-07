# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""No page or doc tells a user to add ``--torch-backend`` to a bare
``uv tool install conexus`` (nexus-n6a8v).

A bare install carries no torch: torch is only in the ``[pdf]`` extra, and the
PDF install names the CPU build itself (nexus-t99tw dropped the flag from the
README and docs; web/index.html kept it until nexus-n6a8v). This pins the
sweep so a page cannot carry the stale line again.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

_ROOT = pathlib.Path(__file__).parent.parent
#: The surfaces a user reads: the README, the docs (RDRs are records, not
#: instructions) and the site pages.
_SURFACES = [
    _ROOT / "README.md",
    *sorted((_ROOT / "docs").glob("*.md")),
    *sorted((_ROOT / "web").glob("*.html")),
]
_BARE_INSTALL = re.compile(r"uv tool install\b[^\n<]*\bconexus\b")
#: The flag on the same command or in the "(on Linux, add ...)" aside that
#: follows it in prose.
_STALE = re.compile(r"uv tool install\b[^\n]*conexus[^\n]{0,160}--torch-backend")

#: Non-vacuity: the surfaces carry this many bare-install commands today.
_MIN_COMMANDS = 8


def test_surfaces_carry_bare_install_commands() -> None:
    n = sum(len(_BARE_INSTALL.findall(p.read_text(encoding="utf-8"))) for p in _SURFACES)
    assert n >= _MIN_COMMANDS, f"only {n} bare-install commands found; the sweep is vacuous"


def test_no_bare_install_names_torch_backend() -> None:
    hits = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _SURFACES
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _STALE.search(line)
    ]
    assert not hits, f"--torch-backend on a bare uv tool install: {hits}"
