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
#: The surfaces a user reads: the README, the docs at any depth (RDRs and their post-mortems are records, not
#: instructions) and the site pages.
_SURFACES = [
    _ROOT / "README.md",
    *sorted(p for p in (_ROOT / "docs").rglob("*.md") if "rdr" not in p.relative_to(_ROOT / "docs").parts),
    *sorted((_ROOT / "web").glob("*.html")),
]
_BARE_INSTALL = re.compile(r"uv tool install\b[^\n<]*\bconexus\b")
#: The flag on the same command, before or after the package name, or in the "(on Linux, add ...)" aside that
#: follows it in prose.
_STALE = re.compile(
    r"uv tool install\b[^\n]*conexus[^\n]{0,160}--torch-backend"
    r"|uv tool install\b[^\n]*--torch-backend[^\n]*\bconexus\b"
)
#: The uv version the --torch-backend option was once said to need: uv pip install has taken it since 0.6.9 and
#: 0.9.19 only added it to the uv tool commands, which the install no longer gives it (nexus-n6a8v fact pass).
_STALE_UV_FLOOR = re.compile(r"0\.9\.19")

#: Non-vacuity: the surfaces carry this many bare-install commands (counted 2026-11-07); a sweep that lost most of
#: its input reads below it.
_MIN_COMMANDS = 30


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Lines with shell continuations (a trailing backslash) joined, each with its first line's number."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for i, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = i
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def test_surfaces_carry_bare_install_commands() -> None:
    n = sum(len(_BARE_INSTALL.findall(p.read_text(encoding="utf-8"))) for p in _SURFACES)
    assert n >= _MIN_COMMANDS, f"only {n} bare-install commands found; the sweep is vacuous"


def test_no_bare_install_names_torch_backend() -> None:
    hits = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _SURFACES
        for i, line in _logical_lines(p.read_text(encoding="utf-8"))
        if _STALE.search(line)
    ]
    assert not hits, f"--torch-backend on a bare uv tool install: {hits}"


def test_no_surface_claims_the_option_needs_uv_0_9_19() -> None:
    hits = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _SURFACES
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _STALE_UV_FLOOR.search(line)
    ]
    assert not hits, f"a surface still names uv 0.9.19: {hits}"


@pytest.mark.parametrize("line", [
    "uv tool install conexus --python 3.12 --torch-backend cpu",
    "uv tool install --torch-backend cpu conexus",
    "uv tool install conexus --python 3.12 \\\n  --torch-backend cpu",
    "run uv tool install conexus --python 3.12 (on Linux, add --torch-backend cpu)",
])
def test_the_stale_shapes_are_caught(line: str) -> None:
    assert any(_STALE.search(text) for _, text in _logical_lines(line)), line
