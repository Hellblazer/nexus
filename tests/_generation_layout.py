# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Build a generation layout for tests. nexus-utpuw.13.

WHY THIS EXISTS. ``tests/conftest.py`` fences ``$HOME`` session-wide
(nexus-pfuns), and ``install_layout.tools_dir()`` defaults to
``$HOME/.local/share/nexus/tools`` -- so every test in this suite sees a box
with NO generation layout unless it says otherwise. That is correct isolation
and it has a sharp edge: any code path that behaves differently under the
generation layout is, by default, tested ONLY on the legacy branch. A sweep
that retargets user-facing advice onto the layout would go entirely
unfalsified without an explicit opt-in like this one.
"""
from __future__ import annotations

import os
from pathlib import Path

from nexus import install_layout


def point_current(link: Path, target: Path) -> None:
    """Make *link* point at *target* the way the product does on this OS.

    On Windows the product's pointer is a pointer FILE (``layout_core.write_pointer``; nexus-f9bgu.47:
    a junction a non-elevated process made is refused on traversal by RedirectionGuard, and a symlink
    needs Developer Mode). Elsewhere it is a symlink. To leave a dangling pointer, remove the target
    afterwards.
    """
    if os.name == "nt":
        from nexus._install import layout_core  # noqa: PLC0415

        layout_core.write_pointer(link, target)
    else:
        link.symlink_to(target)


def build(tmp_path: Path, monkeypatch, *, stamp: str = "20260826T010000Z") -> Path:
    """Point ``NX_TOOLS_DIR`` at a tools root whose ``current`` resolves.

    Returns the generation directory ``current`` names.
    """
    tools = tmp_path / "nx-tools"
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    (gen / install_layout.RECEIPT_NAME).write_text("{}")
    point_current(tools / install_layout.CURRENT_LINK_NAME, gen)
    monkeypatch.setenv(install_layout.TOOLS_DIR_ENV, str(tools))
    return gen
