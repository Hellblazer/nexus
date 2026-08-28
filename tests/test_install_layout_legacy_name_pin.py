# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The two halves name the legacy pseudo-generation identically.

``legacy.sh`` writes ``<tools>/gen-$NX_LEGACY_GENERATION_NAME``;
``install_census`` reads ``install_layout.legacy_generation_link``. If the
names drift, the Python census silently stops seeing a registered legacy
tree -- the under-reporting direction, which is the one that lets a held
tree look free.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nexus import install_layout

_LAYOUT_SH = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install" / "layout.sh"


def test_both_halves_name_the_legacy_generation_identically(tmp_path) -> None:
    r = subprocess.run(
        ["bash", "-c", f'. "{_LAYOUT_SH}"; printf "%s" "$NX_LEGACY_GENERATION_NAME"'],
        capture_output=True, text=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout, "the shell half defines no NX_LEGACY_GENERATION_NAME"
    assert r.stdout == install_layout.LEGACY_GENERATION_NAME


def test_the_python_link_is_under_the_generation_prefix(tmp_path) -> None:
    link = install_layout.legacy_generation_link(tools=tmp_path)
    assert link.parent == tmp_path
    assert link.name == f"{install_layout.GENERATION_PREFIX}{install_layout.LEGACY_GENERATION_NAME}"
