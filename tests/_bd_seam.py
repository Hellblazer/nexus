# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seam for tests that fake a ``bd`` on PATH for the hook's beads render (GH #1689).

``session_context._render_beads`` resolves ``bd`` through ``argv_off_cwd`` before it spawns. On Windows that raises
``FileNotFoundError`` when no ``bd.exe`` is on PATH, so a fixture that fakes only ``which`` and the runner passes where a
``bd.exe`` happens to be installed and fails on a runner that has none. A test that fakes the runner calls
:func:`bd_present`, which pins both halves.
"""
from __future__ import annotations

import pytest

from nexus.hooks import session_context
from nexus.util import nx_argv


def bd_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bd`` is on PATH and resolves to itself: no real lookup, so the test does not depend on the box having a bd."""
    monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
    monkeypatch.setattr(nx_argv, "argv_off_cwd", lambda argv, **_kw: list(argv))
