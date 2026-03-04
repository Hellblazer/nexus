# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx hooks — git hook management for automatic repo indexing."""
import re
import stat
import subprocess
from pathlib import Path

import click

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"

_HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")

_STANZA = """\
{begin}
nx index repo "$(git rev-parse --show-toplevel)" --on-locked=skip \\
  >> "$HOME/.config/nexus/index.log" 2>&1 &
disown
{end}""".format(begin=SENTINEL_BEGIN, end=SENTINEL_END)


# ── git helpers ───────────────────────────────────────────────────────────────


def _git_common_dir(repo: Path) -> Path:
    """Return the effective .git directory (handles worktrees)."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise click.ClickException(f"Not a git repository: {repo}")
    git_common = Path(result.stdout.strip())
    if not git_common.is_absolute():
        git_common = (repo / git_common).resolve()
    return git_common


def _effective_hooks_dir(repo: Path) -> Path:
    """Return the hooks directory for *repo*, respecting core.hooksPath."""
    # Check for custom hooks path
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

    # Default: <git-common-dir>/hooks
    return _git_common_dir(repo) / "hooks"


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
