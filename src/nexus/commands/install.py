# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx install / uninstall — Claude Code integration management."""
from __future__ import annotations

import json
from pathlib import Path

import click

# ── Hook command strings (must contain "nx" for detection during uninstall) ───

_SESSION_START_CMD = "nx hook session-start"
_SESSION_END_CMD   = "nx hook session-end"

_NX_HOOK_MARKER = "nx hook"  # sentinel for install/uninstall detection


# ── SKILL.md content ──────────────────────────────────────────────────────────

_SKILL_MD = """\
---
name: nexus
description: Use Nexus (nx) for semantic search, memory, knowledge, and project management.
---

# Nexus — Agent Usage Guide

Nexus replaces cloud ingest (Mixedbread) with a locally-controlled pipeline.
Use these commands for search, memory, and PM operations.

## Search

```bash
nx search "query"                    # semantic search across T3 knowledge
nx search "query" --corpus code      # code-only search
nx search "query" --hybrid           # hybrid: semantic + ripgrep + frecency
nx search "query" -a                 # search + Haiku answer synthesis
nx search "query" --vimgrep          # vimgrep-compatible output
nx search "query" --json             # JSON output
nx search "query" --mxbai            # include Mixedbread fan-out
nx search "query" --agentic          # Haiku-driven query refinement
```

## Memory (T2 SQLite — persistent across sessions)

```bash
nx memory put "content" -p myproject -t title.md  # store with title
nx memory get --project myproject --title title.md
nx memory search "query" --project myproject
nx memory list --project myproject
nx memory expire                                   # remove TTL-expired entries
```

## Knowledge Store (T3 ChromaDB cloud)

```bash
nx store put "content" --collection knowledge --title "My Finding"
nx store search "query" --collection knowledge
nx store list
```

## Scratch (T1 in-memory — cleared at session end)

```bash
nx scratch put "content" --tags "hypothesis"
nx scratch put "content" --persist --project myproject --title finding.md
nx scratch search "query"
nx scratch list
nx scratch flag <id>                     # mark for SessionEnd flush
nx scratch clear                         # explicit clear
```

## Indexing

```bash
nx index code <path>                     # register and index a code repo
nx index pdf <path>                      # index a PDF into docs__ T3 collection
nx index md <path>                       # index a markdown file
```

## Project Management (PM)

```bash
nx pm init                               # create 5 standard PM docs in T2
nx pm resume                             # inject CONTINUATION.md into session
nx pm status                             # show phase, agent, blockers
nx pm block "reason"                     # add a blocker
nx pm unblock 1                          # remove blocker by line number
nx pm phase next                         # advance to next phase
nx pm search "query"                     # FTS5 search across PM docs
nx pm archive                            # synthesize → T3 + T2 decay
nx pm restore <project>                  # restore within decay window
nx pm reference "query"                  # semantic search across PM archives
nx pm expire                             # remove TTL-expired PM docs
```

## Health

```bash
nx doctor                                # verify all services and credentials
nx serve                                 # start persistent indexing server
```
"""


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


# ── Install ───────────────────────────────────────────────────────────────────

@click.group("install")
def install_group() -> None:
    """Install Nexus integrations."""


@install_group.command("claude-code")
def install_claude_code() -> None:
    """Install SKILL.md and session hooks for Claude Code."""
    # Write SKILL.md
    skill_path = Path.home() / ".claude" / "skills" / "nexus" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(_SKILL_MD)
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
