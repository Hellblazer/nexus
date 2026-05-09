# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CLI command: ``nx aspects`` — aspect extraction queue management.

Subcommands:

  drain   -- drain the aspect extraction queue before a PK migration.

K2 fix (RDR-108 Phase 1, nexus-lh8c): adds the ``nx aspects drain``
operator-facing verb so upgrade docs and MigrationError messages can
point to a concrete, runnable command.
"""
from __future__ import annotations

import click
import structlog

_log = structlog.get_logger(__name__)


@click.group(name="aspects")
def aspects_group() -> None:
    """Aspect extraction queue management."""


@aspects_group.command(name="drain")
@click.option(
    "--timeout",
    default=30.0,
    type=float,
    show_default=True,
    help="Seconds to wait for in-flight rows to complete before raising.",
)
@click.option(
    "--poll-interval",
    default=0.1,
    type=float,
    show_default=True,
    help="Seconds between queue-empty checks.",
)
def aspects_drain(timeout: float, poll_interval: float) -> None:
    """Drain the aspect extraction queue.

    Stops the singleton AspectExtractionWorker (if running in this process),
    then waits until all pending and in-progress rows are processed or the
    timeout elapses.

    Use this before running ``nx upgrade`` when the MigrationError reports
    that the aspect_extraction_queue is not drained.

    Exit codes:
      0  Queue is drained (or was already empty).
      1  Timeout: queue still has active rows after --timeout seconds.
    """
    from nexus.aspect_worker import DrainTimeoutError, drain_worker
    from nexus.commands._helpers import default_db_path

    mem_path = default_db_path()
    click.echo(f"Draining aspect queue at {mem_path} (timeout={timeout}s)...")

    try:
        drain_worker(mem_path, timeout=timeout, poll_interval=poll_interval)
    except DrainTimeoutError as e:
        click.echo(
            f"Drain timeout: {e.stuck_count} row(s) still active after {timeout}s. "
            "Re-run after the worker processes or times out its in-flight rows.",
            err=True,
        )
        raise SystemExit(1) from e

    click.echo("Aspect queue drained. Safe to run 'nx upgrade'.")
