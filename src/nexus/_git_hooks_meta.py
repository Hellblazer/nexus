# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared sentinel + path helpers for the nx-managed git hook stanza.

nexus-8g79.10 (V2): hosted at the package root so library-layer
modules (``nexus.health``) can probe a repo's hook state without
importing up into ``nexus.commands.hooks`` (CLI presentation
layer). The CLI module re-exports the sentinels and the
``_effective_hooks_dir`` helper for back-compat with command code.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"


def git_common_dir(repo: Path) -> Path:
    """Return the effective ``.git`` directory for *repo* (worktree-aware).

    Raises ``RuntimeError`` (not ``ClickException``) on failure so
    callers outside the Click CLI layer can handle the error in
    whatever shape they prefer. The CLI wrapper in
    ``nexus.commands.hooks`` translates this to ``ClickException``.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Not a git repository: {repo}")
    git_common = Path(result.stdout.strip())
    if not git_common.is_absolute():
        git_common = (repo / git_common).resolve()
    return git_common


def effective_hooks_dir(repo: Path) -> Path:
    """Return the hooks directory for *repo*, respecting ``core.hooksPath``."""
    result = subprocess.run(
        ["git", "config", "core.hooksPath"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        hpath = Path(result.stdout.strip())
        if not hpath.is_absolute():
            hpath = (repo / hpath).resolve()
        return hpath
    return git_common_dir(repo) / "hooks"
