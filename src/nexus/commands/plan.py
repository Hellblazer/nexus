# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx plan`` command group. RDR-092 Phase 0d.2.

Day-2 operations against the plan library. ``nx plan repair`` re-runs
the :func:`nexus.db.migrations._backfill_plan_dimensions` heuristic
and surfaces low-confidence rows so the operator can correct
edge-case inferences by hand.
"""
from __future__ import annotations

import sqlite3

import click


@click.group()
def plan() -> None:
    """Plan library maintenance commands."""


@plan.command("repair")
def repair_cmd() -> None:
    """Re-run plan-dimension backfill and list low-conf rows for review.

    Idempotent: once every plan row has a populated ``dimensions``
    column, subsequent runs report "0 backfilled" and exit cleanly.
    Low-confidence rows (those that reached the wh-fallback during the
    RDR-092 Phase 0d.1 backfill heuristic) are listed with their
    inferred verb so the operator can update them via SQL or future
    editor commands.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.migrations import _backfill_plan_dimensions

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(
            f"T2 database not found at {db_path}; nothing to do."
        )
        return

    # Context manager guards against a raise in _backfill_plan_dimensions
    # leaking the connection (RDR-092 code-review S-3).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")

        # Count NULL-dimension rows before the run so the report reflects
        # reality even when the migration itself is a no-op.
        pending_before = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE dimensions IS NULL"
        ).fetchone()[0]

        _backfill_plan_dimensions(conn)

        pending_after = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE dimensions IS NULL"
        ).fetchone()[0]
        backfilled = pending_before - pending_after

        # Surface low-confidence rows for operator review, oldest first so
        # a re-run reports a stable order.
        low_conf_rows = conn.execute(
            "SELECT id, query, verb FROM plans "
            "WHERE tags LIKE '%backfill-low-conf%' "
            "ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    click.echo(f"{backfilled} backfilled")
    if backfilled == 0 and pending_before == 0:
        click.echo("Nothing to do; every plan already carries dimensions.")

    if low_conf_rows:
        click.echo(
            f"\n{len(low_conf_rows)} low-conf row(s) need review "
            "(tagged backfill-low-conf):"
        )
        for row_id, query, verb in low_conf_rows:
            click.echo(
                f"  id={row_id} verb={verb or '-'}  "
                f"query={(query or '').strip()!r}"
            )
    else:
        click.echo("\n0 rows need review.")
