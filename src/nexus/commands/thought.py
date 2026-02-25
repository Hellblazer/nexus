# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx thought — session-scoped sequential thinking chains backed by T2.

Each Claude Code session gets an isolated namespace via os.getsid(0).
Chains survive context compaction (T2 is external to Claude's context window)
and expire automatically after 24 hours. A fresh session always starts clean.

Semantic equivalence with the sequential-thinking MCP server:
- Each `add` appends a thought and returns the full accumulated chain text
  PLUS structured metadata (thoughtHistoryLength, branches, nextThoughtNeeded,
  thoughtNumber, totalThoughts) — identical fields to the MCP server response.
- totalThoughts is auto-adjusted upward when thoughtNumber exceeds the estimate.
- Branches are tracked by branchId when [BRANCH ...] annotations appear.
- nx thought is strictly better than the MCP server: it also returns the full
  thought text (MCP only returns metadata; the model must rely on context window
  for text content).
"""

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from nexus.commands._helpers import default_db_path
from nexus.db.t2 import T2Database

_DIVIDER = "═" * 52
_TTL_CHAIN = 1      # days — session lifetime
_TTL_POINTER = 1    # days

# Matches: **Thought N of ~T** or **Thought N of ~T** [flags]
_HEADER_RE = re.compile(r'\*\*Thought\s+(\d+)\s+of\s+~(\d+)\*\*')
# Matches: nextThoughtNeeded: true|false  (case-insensitive)
_NEXT_RE = re.compile(r'nextThoughtNeeded:\s*(true|false)', re.IGNORECASE)
# Matches: [BRANCH from Thought N — branchId]
_BRANCH_RE = re.compile(r'\[BRANCH from Thought \d+\s*[—–-]\s*([^\]]+)\]')


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _parse_thoughts(content: str) -> list[dict]:
    """Split accumulated content into individual thought dicts."""
    thoughts: list[dict] = []
    for m in _HEADER_RE.finditer(content):
        thought_num = int(m.group(1))
        total = int(m.group(2))
        start = m.start()
        nxt = _HEADER_RE.search(content, m.end())
        end = nxt.start() if nxt else len(content)
        body = content[start:end].strip()

        next_needed_m = _NEXT_RE.search(body)
        next_needed = next_needed_m.group(1).lower() == 'true' if next_needed_m else True

        branch_m = _BRANCH_RE.search(body)
        branch_id = branch_m.group(1).strip() if branch_m else None

        thoughts.append({
            'number': thought_num,
            'total': total,
            'text': body,
            'nextThoughtNeeded': next_needed,
            'branchId': branch_id,
        })
    return thoughts


def _extract_branches(thoughts: list[dict]) -> list[str]:
    """Return sorted list of unique branchIds seen across the chain."""
    seen: list[str] = []
    for t in thoughts:
        if t['branchId'] and t['branchId'] not in seen:
            seen.append(t['branchId'])
    return seen


# ── Session / storage helpers ─────────────────────────────────────────────────

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


# ── Output formatting ─────────────────────────────────────────────────────────

def _print_chain(chain_id: str, content: str, thoughts: list[dict]) -> None:
    """Print full chain text plus MCP-equivalent structured metadata."""
    branches = _extract_branches(thoughts)
    last = thoughts[-1] if thoughts else {}
    history_length = len(thoughts)
    thought_num = last.get('number', history_length)
    total = last.get('total', history_length)
    next_needed = last.get('nextThoughtNeeded', True)

    click.echo(f"Chain: {chain_id}")
    click.echo(_DIVIDER)
    click.echo(content)
    click.echo(_DIVIDER)
    # Structured metadata — matches MCP server response fields
    click.echo(f"thoughtNumber: {thought_num}")
    click.echo(f"totalThoughts: {total}")
    click.echo(f"nextThoughtNeeded: {str(next_needed).lower()}")
    click.echo(f"thoughtHistoryLength: {history_length}")
    click.echo(f"branches: {branches}")
    if next_needed:
        click.echo(f'Next: nx thought add "**Thought {thought_num + 1} of ~{total}** ..."')
    else:
        click.echo("Chain complete. Run: nx thought close")


# ── Commands ──────────────────────────────────────────────────────────────────

@click.group("thought")
def thought_group() -> None:
    """Session-scoped sequential thinking chains (survive context compaction)."""


@thought_group.command("add")
@click.argument("content")
@click.option("--chain", "-c", default=None, help="Chain ID (defaults to current active chain)")
def add_cmd(content: str, chain: str | None) -> None:
    """Append a thought and print the full chain plus structured metadata.

    Returns the complete accumulated chain so Claude always has full context
    in the tool result — identical behaviour to the sequential-thinking MCP
    server, but also includes the full thought text (MCP only returns metadata).

    totalThoughts is auto-adjusted upward if thoughtNumber exceeds the estimate.
    Chains are scoped to the current session (os.getsid) and expire after 24h.
    """
    repo = _repo_name()
    project = _session_project(repo)

    with T2Database(default_db_path()) as db:
        chain_id = chain or _get_current_id(db, project)
        if not chain_id:
            chain_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
            _set_current_id(db, project, chain_id)

        existing = _get_chain_content(db, project, chain_id)
        updated = (existing + "\n\n" + content).strip() if existing else content.strip()

        # MCP behaviour: auto-adjust totalThoughts if thoughtNumber exceeds estimate
        thoughts = _parse_thoughts(updated)
        if thoughts:
            last = thoughts[-1]
            if last['number'] > last['total']:
                # Re-write the last thought's header with adjusted total
                adjusted_total = last['number']
                updated = _HEADER_RE.sub(
                    lambda m: f"**Thought {m.group(1)} of ~{adjusted_total}**"
                    if m.group(1) == str(last['number']) else m.group(0),
                    updated,
                )
                thoughts = _parse_thoughts(updated)

        _put_chain_content(db, project, chain_id, updated)
        _print_chain(chain_id, updated, thoughts)


@thought_group.command("show")
@click.option("--chain", "-c", default=None, help="Chain ID (defaults to current)")
def show_cmd(chain: str | None) -> None:
    """Print the current thought chain and metadata."""
    repo = _repo_name()
    project = _session_project(repo)

    with T2Database(default_db_path()) as db:
        chain_id = chain or _get_current_id(db, project)
        if not chain_id:
            click.echo('No active thought chain. Start one with: nx thought add "**Thought 1 of ~N** ..."')
            return
        content = _get_chain_content(db, project, chain_id)
        if not content:
            click.echo(f"Chain {chain_id!r} not found (may have expired).")
            return
        thoughts = _parse_thoughts(content)
        _print_chain(chain_id, content, thoughts)


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
