# SPDX-License-Identifier: AGPL-3.0-or-later
"""Platform seams shared by tests that run on POSIX and native Windows (RDR-224).

One marker for "this test pins a POSIX-only mechanism", so a Windows run's
skips are named and greppable instead of ad-hoc ``skipif`` strings, and one
assertion for "this file is owner-only", which is a mode on POSIX and a DACL
on Windows. A test whose product behaviour exists on Windows asserts the
Windows form through these helpers rather than skipping.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

IS_WINDOWS: bool = os.name == "nt"


def posix_only(reason: str) -> pytest.MarkDecorator:
    """Skip on native Windows; *reason* names the POSIX-only mechanism pinned."""
    return pytest.mark.skipif(IS_WINDOWS, reason=f"POSIX-only: {reason}")


def scrubbed_env(**env: str) -> dict[str, str]:
    """*env* for a child process started with a deliberately minimal
    environment. On Windows it also carries ``SYSTEMROOT`` (without it Winsock
    cannot load, WinError 10106 at ``import asyncio``) and ``COMSPEC`` (the
    shell a ``.cmd`` runs under); POSIX gets exactly *env*."""
    if IS_WINDOWS:
        for key in ("SYSTEMROOT", "COMSPEC"):
            if key in os.environ and key not in env:
                env[key] = os.environ[key]
    return env


def assert_owner_only(path: str | os.PathLike[str]) -> None:
    """*path* is reachable by its owner only: mode ``0o600`` on POSIX, an
    owner-only DACL on Windows (``nexus._winsec.owner_only_problem``)."""
    if not IS_WINDOWS:
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        return
    from nexus._winsec import owner_only_problem

    problem = owner_only_problem(path, Path(path).stat().st_mode)
    assert problem is None, f"{path} is not owner-only: {problem}"


def make_group_readable(path: str | os.PathLike[str]) -> None:
    """Widen *path* the way a careless writer would: ``chmod 0o644`` on POSIX,
    a DACL that also grants Everyone read on Windows."""
    if not IS_WINDOWS:
        os.chmod(path, 0o644)
        return
    from nexus._winsec import _windows_set_dacl, _windows_user_sid

    _windows_set_dacl(str(path), f"D:P(A;;FA;;;{_windows_user_sid()})(A;;FR;;;WD)", protected=True)
