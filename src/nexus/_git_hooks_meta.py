# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared sentinel + path helpers for the nx-managed git hook stanza.

nexus-8g79.10 (V2): hosted at the package root so library-layer
modules (``nexus.health``) can probe a repo's hook state without
importing up into ``nexus.commands.hooks`` (CLI presentation
layer). The CLI module re-exports the sentinels and the
``_effective_hooks_dir`` helper for back-compat with command code.
"""
from __future__ import annotations

from pathlib import Path

from nexus.bounded_subprocess import run_bounded

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"


def git_common_dir(repo: Path) -> Path:
    """Return the effective ``.git`` directory for *repo* (worktree-aware).

    Raises ``RuntimeError`` (not ``ClickException``) on failure so
    callers outside the Click CLI layer can handle the error in
    whatever shape they prefer. The CLI wrapper in
    ``nexus.commands.hooks`` translates this to ``ClickException``.
    """
    result = run_bounded(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
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
    result = run_bounded(
        ["git", "config", "core.hooksPath"],
        cwd=repo,
        timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        hpath = Path(result.stdout.strip())
        if not hpath.is_absolute():
            hpath = (repo / hpath).resolve()
        return hpath
    return git_common_dir(repo) / "hooks"


#: The hooks that index a repo after a commit, merge or rewrite. ``nx hooks install`` installs these.
INDEX_HOOK_NAMES: tuple[str, ...] = ("post-commit", "post-merge", "post-rewrite")
#: The hook that holds a push while a develop red is open and records the push once it lands (RDR-252, nexus-igea9).
#: It can refuse a push, so it is installed only on request or where a group the caller belongs to serves the repo.
PRE_PUSH_HOOK = "pre-push"
#: Every hook nexus manages with this sentinel: what ``update``, ``uninstall``, ``status``, the sweep and ``nx doctor`` walk.
MANAGED_HOOK_NAMES: tuple[str, ...] = (*INDEX_HOOK_NAMES, PRE_PUSH_HOOK)


def index_hooks_managed(hooks_dir: Path) -> bool:
    """Whether any of the INDEX hooks in *hooks_dir* carries the nexus stanza. ``nx index repo``'s "run nx hooks install" tip and
    ``nx doctor``'s indexing hint read this: a repo with only the pre-push hook is not indexed on commit."""
    for name in INDEX_HOOK_NAMES:
        try:
            if SENTINEL_BEGIN in (hooks_dir / name).read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def installed_detail(repo: Path, installed: list[str]) -> str:
    """The "git hooks" row's detail for a repo whose managed hooks are *installed*; says so when no index hook is among them."""
    text = f"{repo} ({', '.join(installed)})"
    return text if any(n in INDEX_HOOK_NAMES for n in installed) else f"{text}; indexing hooks not installed"


def index_hint(repo: Path, installed: list[str]) -> list[str]:
    """The fix suggestion for a repo with no index hook among its *installed* managed hooks, else none."""
    import shlex  # noqa: PLC0415 -- only this hint needs it

    return [] if any(n in INDEX_HOOK_NAMES for n in installed) else [f"nx hooks install {shlex.quote(str(repo))}"]
