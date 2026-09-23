# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog sweep-ghosts`` (nexus-29drn).

RDR-204's ghost sweep (``CatalogRepository.sweepGhostsAndMarkDormant``)
runs automatically at most ONCE PER TENANT for the life of the estate
(``ensureGhostSweepRanOnce``'s durable ``nexus.catalog_meta`` marker,
``rdr204_ghost_sweep_v1``): it clears the backlog the first time a tenant
makes an authenticated request after the marker is absent, then disarms
itself for that tenant forever. Any collection that BECOMES a ghost
afterwards (a quarantine sibling that finishes draining, a re-home whose
source collection empties out, ...) accumulates with nothing to collect
it — there was no way for an operator to run the sweep on demand.

Sam's ruling (2026-09-23): an operator CLI verb, dry-run by default,
``--apply`` to act, reusing the engine's existing classification predicate
via ``POST /v1/catalog/ghost-sweep`` rather than a client-side
reimplementation. The durable one-shot marker for the AUTOMATIC sweep is
untouched by this verb — calling it, in either mode, never sets
``rdr204_ghost_sweep_v1``, so the automatic per-tenant trigger still fires
at most once regardless of how many times an operator runs this.

Two dispositions this verb reports, per row:

* GHOSTS -- collections nothing references any more (no live row in any
  non-audit COLLECTION_SCOPED_TABLES entry). ``--apply`` physically
  deletes the registry row.
* DORMANT -- collections still referenced somewhere but with no
  ``collection_vector_stats`` row (nothing to embed or read).
  ``--apply`` flips ``lifecycle_state`` to ``'dormant'``; the row itself
  is NOT deleted (``nx doctor``'s "Collections dormant" row is how an
  operator later decides whether to re-index or remove the references).

A quarantine row still referenced by something is HELD unconditionally
(never relitigated into dormant) -- reported as a count only, no names,
since it is unchanged either way.
"""
from __future__ import annotations

import json

import click
import httpx
import structlog

_log = structlog.get_logger(__name__)


def _raise_engine_floor(exc: httpx.HTTPStatusError) -> None:
    raise click.ClickException(
        "nx catalog sweep-ghosts: this engine does not yet expose "
        "POST /v1/catalog/ghost-sweep (needs the nexus-29drn engine "
        "release). Upgrade the deployed engine-service, then re-run."
    ) from exc


@click.command("sweep-ghosts")
@click.option(
    "--apply", is_flag=True, default=False,
    help="Actually reclaim ghost collections and mark dormant ones. "
    "Without this flag the command is a dry-run report only.",
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit the report as JSON on stdout.",
)
def sweep_ghosts_cmd(apply: bool, json_out: bool) -> None:
    """Sweep RDR-204 ghost + referenced-but-empty collections on demand.

    \b
    Default is dry-run: reports what the sweep WOULD do without writing.
    Pass --apply to actually reclaim/mark.

    \b
    Examples:
      nx catalog sweep-ghosts          # dry-run report
      nx catalog sweep-ghosts --apply  # actually reclaim/mark
      nx catalog sweep-ghosts --json   # dry-run report as JSON

    \b
    This is the SAME classification the automatic per-tenant sweep runs
    (no separate implementation) -- see the module docstring for why
    that automatic sweep alone is not enough, and why calling this verb
    never interferes with its durable one-shot marker.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    writer = _cat_cmd._get_catalog_writer()
    try:
        result = writer.ghost_sweep(dry_run=not apply)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 404:
            _raise_engine_floor(exc)
        raise
    finally:
        writer.close()

    if json_out:
        click.echo(json.dumps(result, indent=2))
        return

    verb = "reclaimed" if apply else "would reclaim"
    dormant_verb = "marked dormant" if apply else "would mark dormant"
    click.echo(f"Ghost sweep ({'applied' if apply else 'dry-run'}):")
    click.echo(f"  scanned: {result.get('scanned', 0)}")
    click.echo(f"  ghosts ({verb}): {result.get('ghosts_deleted', 0)}")
    for name in result.get("ghost_names", []):
        click.echo(f"    {verb} {name}")
    click.echo(f"  referenced-but-empty ({dormant_verb}): {result.get('marked_dormant', 0)}")
    for name in result.get("dormant_names", []):
        click.echo(f"    {dormant_verb} {name}")
    click.echo(f"  quarantine held (unchanged): {result.get('quarantine_held', 0)}")

    if not apply:
        click.echo("\n(dry-run — nothing reclaimed. Add --apply to reclaim/mark.)")
    _log.info(
        "ghost_sweep_cmd_done",
        apply=apply, scanned=result.get("scanned", 0),
        ghosts=result.get("ghosts_deleted", 0),
        dormant=result.get("marked_dormant", 0),
    )


def register(group: click.Group) -> None:
    """Attach ``sweep-ghosts`` to the shared ``catalog`` group."""
    group.add_command(sweep_ghosts_cmd)
