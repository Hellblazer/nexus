# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx thought — session-scoped sequential thinking chains backed by T2.

Each Claude Code session gets an isolated namespace via os.getsid(0).
Chains survive context compaction (T2 is external to Claude's context window)
and expire automatically after 24 hours. A fresh session always starts clean.

Mimics the sequential-thinking MCP server: every `add` returns the full
accumulated chain, so Claude always has complete context in the tool result.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from nexus.commands._helpers import default_db_path
from nexus.db.t2 import T2Database

_DIVIDER = "═" * 52
_TTL_CHAIN = 1          # days — session lifetime
_TTL_POINTER = 1        # days


def _session_project(repo: str) -> str:
    gid = os.getsid(0)
    return f"{repo}_thoughts_{gid}"


def _repo_name() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return Path(r.stdout.strip()).name
    except Exception:
        pass
    return "default"


def _get_current_id(db: T2Database, project: str) -> str | None:
    entry = db.get(project=project, title="_current")
    if entry and entry["content"].strip():
        return entry["content"].strip()
    return None


def _set_current_id(db: T2Database, project: str, chain_id: str) -> None:
    db.put(project=project, title="_current", content=chain_id, ttl=_TTL_POINTER)


def _get_chain_content(db: T2Database, project: str, chain_id: str) -> str:
    entry = db.get(project=project, title=chain_id)
    return entry["content"] if entry else ""


def _put_chain_content(db: T2Database, project: str, chain_id: str, content: str) -> None:
    db.put(project=project, title=chain_id, content=content, ttl=_TTL_CHAIN)


def _count_thoughts(content: str) -> int:
    return content.count("**Thought ")


def _print_chain(chain_id: str, content: str, *, next_number: int | None = None) -> None:
    count = _count_thoughts(content)
    click.echo(f"Chain: {chain_id}  ({count} thought{'s' if count != 1 else ''})")
    click.echo(_DIVIDER)
    click.echo(content)
    click.echo(_DIVIDER)
    if next_number is not None:
        click.echo(f'Next: nx thought add "**Thought {next_number} of ~?" ...')


@click.group("thought")
def thought_group() -> None:
    """Session-scoped sequential thinking chains (survive context compaction)."""


@thought_group.command("add")
@click.argument("content")
@click.option("--chain", "-c", default=None, help="Chain ID (defaults to current active chain)")
def add_cmd(content: str, chain: str | None) -> None:
    """Append a thought and print the full chain.

    Returns the complete accumulated chain so Claude always has full context
    in the tool result — identical behaviour to the sequential-thinking MCP server.
    Chains are scoped to the current session and expire after 24 hours.
    """
    repo = _repo_name()
    project = _session_project(repo)

    with T2Database(default_db_path()) as db:
        chain_id = chain or _get_current_id(db, project)
        if not chain_id:
            chain_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            _set_current_id(db, project, chain_id)

        existing = _get_chain_content(db, project, chain_id)
        updated = (existing + "\n\n" + content).strip()
        _put_chain_content(db, project, chain_id, updated)

        next_num = _count_thoughts(updated) + 1
        _print_chain(chain_id, updated, next_number=next_num)


@thought_group.command("show")
@click.option("--chain", "-c", default=None, help="Chain ID (defaults to current)")
def show_cmd(chain: str | None) -> None:
    """Print the current thought chain."""
    repo = _repo_name()
    project = _session_project(repo)

    with T2Database(default_db_path()) as db:
        chain_id = chain or _get_current_id(db, project)
        if not chain_id:
            click.echo("No active thought chain. Start one with: nx thought add \"**Thought 1 of ~N** ...\"")
            return
        content = _get_chain_content(db, project, chain_id)
        if not content:
            click.echo(f"Chain {chain_id!r} not found (may have expired).")
            return
        _print_chain(chain_id, content)


@thought_group.command("close")
@click.option("--chain", "-c", default=None, help="Chain ID (defaults to current)")
def close_cmd(chain: str | None) -> None:
    """Mark the active chain complete and clear the current pointer."""
    repo = _repo_name()
    project = _session_project(repo)

    with T2Database(default_db_path()) as db:
        chain_id = chain or _get_current_id(db, project)
        if not chain_id:
            click.echo("No active thought chain.")
            return
        _set_current_id(db, project, "")
        click.echo(f"Chain {chain_id!r} closed.")


@thought_group.command("list")
def list_cmd() -> None:
    """List thought chains for the current session."""
    repo = _repo_name()
    project = _session_project(repo)

    with T2Database(default_db_path()) as db:
        current_id = _get_current_id(db, project)
        entries = db.list_entries(project=project)
        chains = [e for e in entries if not e["title"].startswith("_")]

    if not chains:
        click.echo("No thought chains in this session.")
        return
    for e in chains:
        marker = "  ← active" if e["title"] == current_id else ""
        ts = e["timestamp"][:16].replace("T", " ")
        click.echo(f"  {e['title']}{marker}  ({ts})")
