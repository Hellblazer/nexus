# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx migrate-to-service`` — the guided Chroma-to-service upgrade (RDR-159).

The single survivable command that turns the ~8 manual Chroma-to-pgvector
upgrade steps into one guided flow (RDR-159, the load-bearing piece of the
``nexus-luxe6`` release-blocker lift).

P0 ships the read-only front half only: ``--dry-run`` classifies the user's
Chroma footprint per collection (source leg x embedding model, model resolved
against the service's wired embedders by deployment mode) and previews what
would migrate — per-leg/per-model counts, unsupported collections flagged for
re-index, and a coarse token/time estimate. It touches NO data.

The full orchestrated execution (provision -> quiesce -> pre-gate -> T2 -> T3
-> validate -> unlock/rollback) lands in later RDR-159 phases; until then the
non-``--dry-run`` invocation errors loudly rather than half-running.
"""
from __future__ import annotations

import contextlib
import sys
from typing import Any

import click
import structlog

from nexus.migration.detection import (
    build_dry_run_preview,
    classify_collections,
    open_read_legs,
    render_dry_run_preview,
    voyage_key_available,
)

_log = structlog.get_logger(__name__)


@click.command(name="migrate-to-service")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Classify the Chroma footprint and preview the migration without "
    "moving any data.",
)
@click.option(
    "--local-path",
    type=click.Path(),
    default=None,
    help="Override the local Chroma store path "
    "(default: ~/.config/nexus/chroma).",
)
def migrate_to_service_cmd(dry_run: bool, local_path: str | None) -> None:
    """Guided Chroma-to-service upgrade migration (RDR-159).

    Currently only ``--dry-run`` is available; the full migration ships in a
    later release.
    """
    if not dry_run:
        raise click.ClickException(
            "The full guided migration is not available yet — run with "
            "--dry-run to preview your Chroma footprint and what would "
            "migrate. The orchestrated execution ships in a later release "
            "(RDR-159 P1+)."
        )

    local, cloud = open_read_legs(local_path)
    try:
        report = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=voyage_key_available(),
        )
        preview = build_dry_run_preview(report)
        click.echo(render_dry_run_preview(preview))
        if preview.unsupported:
            # Unsupported collections would BLOCK a real run — make the
            # dry-run exit non-zero so a script gates on it (gate S1: never a
            # silent OK). Inside the try so a classify/build failure instead
            # propagates its own error (and still closes the clients).
            sys.exit(1)
    finally:
        for client in (local, cloud):
            _close_quietly(client)


def _close_quietly(client: Any | None) -> None:
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()
