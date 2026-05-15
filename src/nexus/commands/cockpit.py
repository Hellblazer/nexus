# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx cockpit — user-facing status surface for the ORB tuplespace + bridge.

Closes the RDR-111 "no consumer for hook events" gap surfaced in the
2026-05-15 deep-review pass: prior to this command, the only way to see
that the hook bridge was firing was to run raw ``sqlite3 ... SELECT
count(*) FROM tuples``. ``nx cockpit status`` reads the local tuples.db
directly and renders a one-page summary.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import click


@click.group(name="cockpit")
def cockpit_group() -> None:
    """Cockpit surface for the ORB tuplespace (RDR-111).

    Subcommands:

    \b
      status   — show recent hook events + per-subspace counts
    """


@cockpit_group.command(name="status")
@click.option(
    "--window",
    "-w",
    default="1h",
    show_default=True,
    help="Time window (e.g. 10m, 1h, 24h, 7d) for recent-event counts.",
)
@click.option(
    "--db",
    "db_path_arg",
    type=click.Path(path_type=Path),
    default=None,
    help="Override path to tuples.db (defaults to ~/.config/nexus/tuples.db).",
)
def status_cmd(window: str, db_path_arg: Path | None) -> None:
    """Show tuplespace status: db size, per-subspace counts, most-recent timestamps.

    Reads ``~/.config/nexus/tuples.db`` directly. Works in direct mode and
    in daemon mode (the daemon owns the DB but a read-only client snapshot
    is safe under WAL).
    """
    db_path = db_path_arg or _default_tuples_db()
    if not db_path.exists():
        click.echo(f"tuples.db not found at {db_path}", err=True)
        click.echo(
            "The hook bridge has not run yet (or NX_BRIDGE_DISABLE is set).",
            err=True,
        )
        raise SystemExit(1)

    window_seconds = _parse_window(window)
    now = time.time()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM tuples").fetchone()["n"]
        # Per-subspace summary
        rows = conn.execute(
            """
            SELECT subspace,
                   COUNT(*) AS total_count,
                   SUM(CASE WHEN created_at > ? THEN 1 ELSE 0 END) AS recent_count,
                   MAX(created_at) AS most_recent_at
              FROM tuples
             GROUP BY subspace
             ORDER BY most_recent_at DESC NULLS LAST
            """,
            (now - window_seconds,),
        ).fetchall()
    finally:
        conn.close()

    size_bytes = db_path.stat().st_size

    click.echo(f"tuples.db        {db_path}")
    click.echo(f"size             {_human_bytes(size_bytes)}")
    click.echo(f"total tuples     {total}")
    click.echo(f"window           {window}  (recent = within window)")
    click.echo("")

    if not rows:
        click.echo("No subspaces have tuples yet.")
        return

    click.echo(f"{'subspace':<48}  {'total':>8}  {'recent':>8}  {'most-recent':>20}")
    click.echo("-" * 90)
    for r in rows:
        ts = r["most_recent_at"]
        ts_str = (
            _fmt_relative(now - ts) if ts is not None else "—"
        )
        click.echo(
            f"{r['subspace']:<48}  {r['total_count']:>8}  "
            f"{r['recent_count']:>8}  {ts_str:>20}"
        )


def _default_tuples_db() -> Path:
    return Path(os.path.expanduser("~/.config/nexus/tuples.db"))


def _parse_window(s: str) -> float:
    """Parse a window string like '10m', '1h', '24h', '7d' → seconds."""
    s = s.strip().lower()
    if not s:
        raise click.BadParameter("empty window")
    unit = s[-1]
    try:
        value = float(s[:-1])
    except ValueError:
        raise click.BadParameter(f"invalid window: {s!r}") from None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        raise click.BadParameter(
            f"unknown window unit {unit!r}; use s/m/h/d"
        )
    return value * multipliers[unit]


def _human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}TiB"


def _fmt_relative(seconds_ago: float) -> str:
    if seconds_ago < 60:
        return f"{int(seconds_ago)}s ago"
    if seconds_ago < 3600:
        return f"{int(seconds_ago / 60)}m ago"
    if seconds_ago < 86400:
        return f"{seconds_ago / 3600:.1f}h ago"
    return f"{seconds_ago / 86400:.1f}d ago"
