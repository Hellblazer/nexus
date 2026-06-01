# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx hooks — git hook management for automatic repo indexing."""
import re
import stat
from pathlib import Path

import click

# nexus-8g79.10 (V2): sentinels + git helpers live in
# ``nexus._git_hooks_meta`` so library-layer probes
# (``nexus.health``) don't reach up into this CLI module. The
# lower-layer ``git_common_dir`` raises ``RuntimeError`` for
# non-git-repo; this CLI module translates to ``ClickException``
# at the boundary.
#
# Sentinels are constants (value-bound is fine). For
# ``effective_hooks_dir`` we use a thin wrapper so test
# monkeypatches on ``nexus._git_hooks_meta.effective_hooks_dir``
# reach the live binding at call time (a bare ``from … import …``
# captures the function at import time and bypasses patches).
from nexus import _git_hooks_meta as _ghm
from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END


def _effective_hooks_dir(repo):
    """Delegate to ``nexus._git_hooks_meta.effective_hooks_dir``."""
    return _ghm.effective_hooks_dir(repo)


def _git_common_dir_raw(repo):
    """Delegate to ``nexus._git_hooks_meta.git_common_dir``."""
    return _ghm.git_common_dir(repo)

_HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")

_STANZA = """\
{begin}
REPO_TOP="$(git rev-parse --show-toplevel)"
# pgrep guard (nexus-mkj6u 2026-05-23): skip if an indexer for THIS
# repo is already running. Belt-and-suspenders with --on-locked=skip,
# which races on lock acquisition under burst-commit workloads. The
# race fires when 2+ commits happen before the first indexer finishes
# its open()+truncate+write+flock sequence; the second indexer can
# truncate the lock file out from under the first and still get past
# its own flock if the timing aligns. pgrep at the hook layer catches
# 99%+ of pile-ups before they fork.
if pgrep -f "nx index repo $REPO_TOP" > /dev/null 2>&1; then
  exit 0
fi
nx index repo "$REPO_TOP" --on-locked=skip \\
  >> "$HOME/.config/nexus/index.log" 2>&1 &
disown
{end}""".format(begin=SENTINEL_BEGIN, end=SENTINEL_END)


# ── git helpers ───────────────────────────────────────────────────────────────


def _git_common_dir(repo: Path) -> Path:
    """CLI-layer wrapper: translate RuntimeError → ClickException."""
    try:
        return _git_common_dir_raw(repo)
    except RuntimeError as exc:
        raise click.ClickException(str(exc))


# ── stanza helpers ────────────────────────────────────────────────────────────


def _remove_stanza(content: str) -> str:
    """Remove the nexus sentinel stanza from *content*."""
    return re.sub(
        rf"\n?{re.escape(SENTINEL_BEGIN)}.*?{re.escape(SENTINEL_END)}\n?",
        "",
        content,
        flags=re.DOTALL,
    )


def _hook_status(hooks_dir: Path, hook_name: str) -> str:
    """Return status string: 'not installed' | 'unmanaged' | 'owned' | 'appended'."""
    hook_file = hooks_dir / hook_name
    if not hook_file.exists():
        return "not installed"
    content = hook_file.read_text()
    if SENTINEL_BEGIN not in content:
        return "unmanaged"
    remainder = _remove_stanza(content).strip()
    if remainder in ("", "#!/bin/sh"):
        return "owned"
    return "appended"


def _install_hook(hooks_dir: Path, hook_name: str) -> str:
    """Install or append nexus stanza. Returns 'created' | 'appended' | 'already installed'."""
    hook_file = hooks_dir / hook_name
    if not hook_file.exists():
        hook_file.write_text(f"#!/bin/sh\n{_STANZA}\n")
        hook_file.chmod(0o755)
        return "created"

    content = hook_file.read_text()
    if SENTINEL_BEGIN in content:
        return "already installed"

    # Append to existing hook
    hook_file.write_text(content.rstrip("\n") + "\n" + _STANZA + "\n")
    return "appended"


def _uninstall_hook(hooks_dir: Path, hook_name: str) -> str:
    """Remove nexus stanza. Returns 'removed' | 'stanza removed' | 'not installed'."""
    hook_file = hooks_dir / hook_name
    if not hook_file.exists():
        return "not installed"
    content = hook_file.read_text()
    if SENTINEL_BEGIN not in content:
        return "not installed"

    new_content = _remove_stanza(content)
    if new_content.strip() in ("", "#!/bin/sh"):
        hook_file.unlink()
        return "removed"

    hook_file.write_text(new_content)
    return "stanza removed"


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group()
def hooks() -> None:
    """Manage git hooks for automatic repo indexing.

    Distinct from ``nx hook`` (singular) which handles Claude Code session hooks.
    """


@hooks.command("install")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_install(path: Path) -> None:
    """Install nexus git hooks into PATH (default: current directory).

    Installs post-commit, post-merge, and post-rewrite hooks that run
    ``nx index repo`` in the background after each qualifying git operation.
    Appends a sentinel-bounded stanza to existing hook files without
    overwriting them.
    """
    repo = path.resolve()

    try:
        hooks_dir = _effective_hooks_dir(repo)
    except click.ClickException as exc:
        raise exc

    # Check writeability
    if hooks_dir.exists() and not _is_writable(hooks_dir):
        raise click.ClickException(
            f"Hooks directory is not writable: {hooks_dir}\n"
            "Check core.hooksPath or directory permissions."
        )

    hooks_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"Installing hooks for {repo}…")

    for name in _HOOK_NAMES:
        action = _install_hook(hooks_dir, name)
        symbol = "✓" if action != "already installed" else "·"
        click.echo(f"  {symbol} {name}  ({action})")

    click.echo("Done. Indexing will run in the background after each commit.")


@hooks.command("uninstall")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_uninstall(path: Path) -> None:
    """Remove nexus git hooks from PATH (default: current directory).

    Removes the nexus-managed sentinel stanza; leaves any unrelated hook
    content intact.
    """
    repo = path.resolve()
    hooks_dir = _effective_hooks_dir(repo)

    click.echo(f"Removing nexus hooks from {repo}…")

    for name in _HOOK_NAMES:
        action = _uninstall_hook(hooks_dir, name)
        symbol = "✓" if action != "not installed" else "·"
        click.echo(f"  {symbol} {name}  ({action})")

    click.echo("Done.")


@hooks.command("update")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_update(path: Path) -> None:
    """Refresh nexus git hooks to the current stanza (nexus-mkj6u shakeout).

    Equivalent to ``nx hooks uninstall && nx hooks install`` in one step.
    Use this when ``nx doctor`` reports stanza drift — typically after a
    conexus upgrade that changed the stanza (e.g. the 2026-05-23 pgrep
    guard for the multi-indexer pile-up race).

    Only rewrites hooks that are currently nexus-managed (have the
    sentinel block); never touches unmanaged hook files.
    """
    repo = path.resolve()
    hooks_dir = _effective_hooks_dir(repo)

    if hooks_dir.exists() and not _is_writable(hooks_dir):
        raise click.ClickException(
            f"Hooks directory is not writable: {hooks_dir}\n"
            "Check core.hooksPath or directory permissions."
        )

    hooks_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"Updating nexus hooks in {repo}…")

    for name in _HOOK_NAMES:
        hook_file = hooks_dir / name
        if not hook_file.exists():
            click.echo(f"  · {name}  (not installed; skipped)")
            continue
        content = hook_file.read_text()
        if SENTINEL_BEGIN not in content:
            click.echo(f"  · {name}  (unmanaged; skipped)")
            continue
        # Rewrite: remove old stanza, install fresh one. The
        # _install_hook path handles both "owned" (file has only the
        # stanza + shebang) and "appended" (other content present)
        # cases correctly.
        _uninstall_hook(hooks_dir, name)
        action = _install_hook(hooks_dir, name)
        click.echo(f"  ✓ {name}  (refreshed: {action})")

    click.echo("Done. New stanza in effect from the next commit.")


def _refresh_managed_hooks(hooks_dir: Path) -> list[tuple[str, str]]:
    """Refresh every nexus-managed hook in *hooks_dir* to the current stanza.

    Only rewrites hooks that already carry the sentinel block; never touches
    unmanaged or absent hook files. Returns a list of ``(hook_name, action)``
    where action is ``refreshed:<install-action>`` | ``unmanaged`` |
    ``not installed``.
    """
    results: list[tuple[str, str]] = []
    for name in _HOOK_NAMES:
        hook_file = hooks_dir / name
        if not hook_file.exists():
            results.append((name, "not installed"))
            continue
        if SENTINEL_BEGIN not in hook_file.read_text():
            results.append((name, "unmanaged"))
            continue
        _uninstall_hook(hooks_dir, name)
        action = _install_hook(hooks_dir, name)
        results.append((name, f"refreshed:{action}"))
    return results


def _iter_managed_repo_roots() -> list[Path]:
    """Return existing registered repo working trees (catalog ∪ registry).

    Reuses ``list_repos_dual`` — the same canonical enumeration ``nx doctor``
    uses for its git-hook drift check — so every repo the doctor reports drift
    for is reachable here. Resilient: returns ``[]`` when the catalog is
    uninitialised or unreadable rather than raising, because the caller
    (``nx upgrade``) treats hook refresh as best-effort.
    """
    try:
        from nexus.catalog.catalog import Catalog  # noqa: PLC0415
        from nexus.config import catalog_path, nexus_config_dir  # noqa: PLC0415
        from nexus.repos import list_repos_dual  # noqa: PLC0415

        cat_dir = catalog_path()
        if not (cat_dir / ".catalog.db").exists():
            return []
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        registry_path = nexus_config_dir() / "repos.json"
        repo_strs = list_repos_dual(cat=cat, registry_path=registry_path)
    except Exception:  # noqa: BLE001 — best-effort enumeration
        return []

    seen: set[Path] = set()
    repos: list[Path] = []
    for repo_str in repo_strs:
        repo = Path(repo_str)
        if repo in seen or not repo.is_dir():
            continue
        seen.add(repo)
        repos.append(repo)
    return repos


def refresh_all_managed_hooks(*, echo: bool = False) -> dict[str, int]:
    """Refresh nexus-managed git hooks across every catalog-registered repo.

    Best-effort: a repo that can't be resolved (non-git, hooks dir not
    writable, etc.) is counted under ``errors`` and skipped — one bad repo
    never aborts the sweep. Returns a summary dict with ``repos``,
    ``refreshed``, and ``errors`` counts.
    """
    summary = {"repos": 0, "refreshed": 0, "errors": 0}
    for repo in _iter_managed_repo_roots():
        try:
            hooks_dir = _effective_hooks_dir(repo)
            if hooks_dir.exists() and not _is_writable(hooks_dir):
                summary["errors"] += 1
                if echo:
                    click.echo(f"  ! {repo}  (hooks dir not writable; skipped)")
                continue
            results = _refresh_managed_hooks(hooks_dir)
        except click.ClickException as exc:
            summary["errors"] += 1
            if echo:
                click.echo(f"  ! {repo}  ({exc.format_message()})")
            continue

        refreshed = [n for n, a in results if a.startswith("refreshed")]
        if refreshed:
            summary["repos"] += 1
            summary["refreshed"] += len(refreshed)
            if echo:
                click.echo(f"  ✓ {repo}  ({len(refreshed)} hook(s) refreshed)")
    return summary


@hooks.command("update-all")
def hooks_update_all() -> None:
    """Refresh nexus-managed git hooks across ALL catalog-registered repos.

    Sweeps every ``repo`` owner in the catalog and refreshes any hook that
    already carries the nexus stanza, so a single command brings every repo
    to the current stanza after a conexus upgrade. Unmanaged and uninstalled
    hooks are left untouched. This is also run automatically by ``nx upgrade``.
    """
    click.echo("Refreshing nexus hooks across all registered repos…")
    summary = refresh_all_managed_hooks(echo=True)
    if summary["repos"] == 0 and summary["errors"] == 0:
        click.echo("No nexus-managed hooks found in any registered repo.")
        return
    click.echo(
        f"Done. {summary['refreshed']} hook(s) refreshed across "
        f"{summary['repos']} repo(s)"
        + (f"; {summary['errors']} repo(s) skipped." if summary["errors"] else ".")
    )


@hooks.command("status")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_status(path: Path) -> None:
    """Show nexus git hook status for PATH (default: current directory)."""
    repo = path.resolve()
    hooks_dir = _effective_hooks_dir(repo)

    click.echo(f"Hooks directory: {hooks_dir}")

    for name in _HOOK_NAMES:
        s = _hook_status(hooks_dir, name)
        symbol = "✓" if s.startswith(("owned", "appended")) else "·"
        click.echo(f"  {symbol} {name}: {s}")


# ── internal ──────────────────────────────────────────────────────────────────


def _is_writable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IWUSR)
