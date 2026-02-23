# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx install / uninstall — Claude Code integration management."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import click

# ── Hook command strings (must contain "nx" for detection during uninstall) ───

_SESSION_START_CMD = "nx hook session-start"
_SESSION_END_CMD   = "nx hook session-end"

_NX_HOOK_MARKER = "nx hook"  # sentinel for install/uninstall detection


# ── SKILL.md source path ──────────────────────────────────────────────────────

# Evaluated lazily (at command invocation, not at import) to avoid crashing
# the entire nx CLI with FileNotFoundError when installed outside the source tree.
_SKILL_MD_PATH = Path(__file__).parents[3] / "nx" / "skills" / "nexus" / "SKILL.md"


def _read_skill_md() -> str:
    """Read SKILL.md content, raising ClickException with install hint if absent."""
    if not _SKILL_MD_PATH.exists():
        raise click.ClickException(
            f"SKILL.md not found at {_SKILL_MD_PATH}.\n"
            "This usually means nx is installed outside its source tree.\n"
            "Install permanently with: uv tool install nexus  or  pipx install nexus"
        )
    return _SKILL_MD_PATH.read_text()


# ── Settings helpers ──────────────────────────────────────────────────────────

def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_settings() -> dict:
    path = _settings_path()
    if path.exists():
        try:
            return json.loads(path.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_settings(data: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _nx_hook_entry(command: str) -> dict:
    return {"command": command}


# ── PATH check ────────────────────────────────────────────────────────────────

_VENV_INDICATORS = (".venv", "/venv/", "virtualenvs", "site-packages")

_INSTALL_HINT = """\

Warning: nx is running from an isolated environment ({path}).
Claude Code session hooks call 'nx hook session-start/end' in a plain shell
where that environment won't be active — the hooks will silently fail.

Install nx permanently so it's always on PATH:
  macOS (Homebrew):  brew tap Hellblazer/nexus https://github.com/Hellblazer/nexus
                     brew install Hellblazer/nexus/nx
  any platform:      uv tool install nexus
                     pipx install nexus
"""


def _warn_if_transient_install() -> None:
    """Emit a warning when nx is running from a venv that hooks won't see."""
    nx_path = shutil.which("nx") or sys.argv[0]
    if any(ind in nx_path for ind in _VENV_INDICATORS):
        click.echo(_INSTALL_HINT.format(path=nx_path), err=True)


# ── Install ───────────────────────────────────────────────────────────────────

@click.group("install")
def install_group() -> None:
    """Install Nexus integrations."""


@install_group.command("claude-code")
def install_claude_code() -> None:
    """Install SKILL.md and session hooks for Claude Code."""
    _warn_if_transient_install()

    # Write SKILL.md (read here, not at import time, to avoid startup crashes)
    skill_path = Path.home() / ".claude" / "skills" / "nexus" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(_read_skill_md())
    click.echo(f"Wrote SKILL.md → {skill_path}")

    # Update settings.json
    data = _load_settings()
    hooks = data.setdefault("hooks", {})

    for hook_key, cmd in [
        ("SessionStart", _SESSION_START_CMD),
        ("SessionEnd", _SESSION_END_CMD),
    ]:
        entries = hooks.setdefault(hook_key, [])
        entry = _nx_hook_entry(cmd)
        if not any(e.get("command") == cmd for e in entries if isinstance(e, dict)):
            entries.append(entry)

    _save_settings(data)
    click.echo(f"Updated hooks → {_settings_path()}")
    click.echo("Claude Code integration installed.")


# ── Uninstall ─────────────────────────────────────────────────────────────────

@click.group("uninstall")
def uninstall_group() -> None:
    """Remove Nexus integrations."""


@uninstall_group.command("claude-code")
def uninstall_claude_code() -> None:
    """Remove SKILL.md and session hooks installed by 'nx install claude-code'."""
    # Remove SKILL.md
    skill_path = Path.home() / ".claude" / "skills" / "nexus" / "SKILL.md"
    if skill_path.exists():
        skill_path.unlink()
        click.echo(f"Removed {skill_path}")
    else:
        click.echo(f"SKILL.md not found at {skill_path} (already removed?)")

    # Remove nx hook entries from settings.json
    data = _load_settings()
    hooks = data.get("hooks", {})
    changed = False
    for hook_key in list(hooks.keys()):
        before = hooks[hook_key]
        after = [
            e for e in before
            if not (isinstance(e, dict) and _NX_HOOK_MARKER in e.get("command", ""))
        ]
        if len(after) != len(before):
            hooks[hook_key] = after
            changed = True

    if changed:
        _save_settings(data)
        click.echo(f"Removed nx hook entries from {_settings_path()}")
    else:
        click.echo("No nx hook entries found in settings.json.")

    click.echo("Claude Code integration removed.")
