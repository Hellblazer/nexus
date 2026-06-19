# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx migration`` — inspect or recover the cross-process migration sentinel.

RDR-159 §"Atomicity + crash recovery". The guided upgrade (``nx
migrate-to-service``) writes a ``migration.state`` sentinel that every
long-lived reader polls to degrade-LOUD while a migration is in flight. A CLI
crash between a clean T3 copy and the UNLOCK clear can STRAND that sentinel at
``migrating`` / ``migrated-failed``, banner-wrapping every read surface forever.

``nx migration --clear-state`` is the named escape hatch. Clearing is SAFE
because a resumed ``nx migrate-to-service`` recomputes done-vs-total from live
source-vs-target counts (the ETL is idempotent on ``(tenant, collection,
chash)``), never trusting the stale marker. Bare ``nx migration`` prints the
current sentinel read-only.
"""
from __future__ import annotations

import click

from nexus.migration.state import (
    MIGRATED_FAILED,
    MIGRATING,
    clear_state,
    read_state,
)


@click.command(name="migration")
@click.option(
    "--clear-state",
    "do_clear",
    is_flag=True,
    default=False,
    help="Remove a stranded migration sentinel (safe: a resumed "
    "nx migrate-to-service recomputes progress from live counts).",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Clear even a 'migrating' sentinel, which MAY be a live migration in "
    "another process. Only use if that process actually crashed.",
)
def migration_cmd(do_clear: bool, force: bool) -> None:
    """Inspect or recover the cross-process migration sentinel (RDR-159)."""
    state = read_state()

    if do_clear:
        if state is None:
            click.echo("No migration state to clear (already not-migrating).")
            return
        # A 'migrated-failed' sentinel is unambiguously safe to clear (its writer
        # is dead). A 'migrating' sentinel MAY be a live migration in another
        # process; clearing it drops the read-surface banner mid-migration, so
        # gate it behind --force (RDR-159 escape hatch is for a CRASHED writer).
        if state.phase == MIGRATING and not force:
            raise click.ClickException(
                "Sentinel phase is 'migrating' — a migration may be running in "
                "another process. Clearing now would drop the read-surface "
                "banner mid-migration. Clear only if the migration process "
                "crashed: re-run with --force to confirm."
            )
        clear_state()
        click.echo(
            f"Cleared migration sentinel (was {state.phase}, "
            f"{state.collections_done}/{state.collections_total} collections). "
            "Reads serve normally again; re-run nx migrate-to-service to resume."
        )
        return

    # Read-only status.
    if state is None:
        click.echo("not-migrating (no sentinel; reads serve normally).")
        return
    line = (
        f"{state.phase}: {state.collections_done}/{state.collections_total} "
        "collections"
    )
    if state.started_at:
        line += f", started {state.started_at}"
    click.echo(line)
    if state.failure:
        click.echo(f"failure: {state.failure}")
    if state.phase == MIGRATED_FAILED:
        click.echo("Recover a stranded sentinel with: nx migration --clear-state")
    elif state.phase == MIGRATING:
        click.echo(
            "Migration in progress. If the process crashed, recover with: "
            "nx migration --clear-state --force"
        )
