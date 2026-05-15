# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx instances`` -- list running Nexus MCP instances via T2 liveness table.

RDR-111 P1.3 (nexus-r0vi).

Queries the liveness table in memory.db and formats the result as a
human-readable table or JSON. Stale rows (last_seen > 60 s ago) are
included by default -- they may represent processes that exited without
cleaning up; use ``--sweep`` to remove them first.
"""
from __future__ import annotations

import json as _json
import time
from pathlib import Path

import click

from nexus.commands._helpers import default_db_path

# Age threshold used to compute the human-readable age column.
_STALE_THRESHOLD_SECONDS = 60


def _age_str(last_seen: float) -> str:
    """Return a compact human-readable age string for a last_seen epoch float."""
    age = time.time() - last_seen
    if age < 0:
        return "now"
    if age < 90:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    return f"{int(age / 3600)}h ago"


@click.command("instances")
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit structured JSON instead of the human table.",
)
@click.option(
    "--sweep", is_flag=True, default=False,
    help="Sweep stale rows (last_seen > 60 s) before listing.",
)
def instances_cmd(json_out: bool, sweep: bool) -> None:
    """List running Nexus MCP instances from the T2 liveness table.

    Shows pid, machine, user, session, project, focus, activity, and age
    for every row in the liveness table. Stale rows (no heartbeat for
    > 60 s) are marked with an asterisk in human output.

    Under RDR-112 daemon mode both the MCP heartbeat and this CLI command
    route through the same ``MemoryStore.liveness_*`` methods via the
    ``mcp_infra.t2_ctx`` facade.
    """
    db_path = default_db_path()
    if not Path(db_path).exists():
        if json_out:
            click.echo(_json.dumps({"error": "T2 database not found", "path": str(db_path)}))
        else:
            click.echo(f"T2 database not found at {db_path}.", err=True)
        raise click.exceptions.Exit(1)

    from nexus.mcp_infra import t2_ctx
    with t2_ctx() as db:
        if sweep:
            deleted = db.memory.liveness_sweep()
            if not json_out:
                click.echo(f"Swept {deleted} stale row(s).")
        rows = db.memory.liveness_list()

    if json_out:
        click.echo(_json.dumps(rows, indent=2))
        return

    if not rows:
        click.echo("No active instances found.")
        return

    # Human table.
    now = time.time()
    hdr = f"{'PID':<8} {'MACHINE':<20} {'USER':<12} {'SESSION':<14} {'PROJECT':<14} {'FOCUS':<16} {'ACTIVITY':<16} AGE"
    sep = f"{'-'*8} {'-'*20} {'-'*12} {'-'*14} {'-'*14} {'-'*16} {'-'*16} {'-'*12}"
    click.echo(hdr)
    click.echo(sep)
    for r in rows:
        stale = (now - r["last_seen"]) > _STALE_THRESHOLD_SECONDS
        age = _age_str(r["last_seen"])
        marker = "*" if stale else " "
        click.echo(
            f"{marker}{r['pid']:<7} "
            f"{(r['machine'] or ''):<20} "
            f"{(r['user_id'] or ''):<12} "
            f"{(r['session'] or ''):<14} "
            f"{(r['project'] or ''):<14} "
            f"{(r['focus'] or ''):<16} "
            f"{(r['activity'] or ''):<16} "
            f"{age}"
        )
    if any((now - r["last_seen"]) > _STALE_THRESHOLD_SECONDS for r in rows):
        click.echo("  * stale (no heartbeat for > 60 s); use --sweep to remove")
