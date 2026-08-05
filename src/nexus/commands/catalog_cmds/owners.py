# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Owner-management commands for the ``nx catalog`` group (nexus-kgyoz seam 3).

Carved verbatim out of ``commands.catalog`` (the ~6.9k-line god module).
``register`` attaches ``owners`` (list registered owners) to the shared
``catalog`` group so ``nx catalog owners`` resolves exactly as before.

``dedupe-owners`` was REMOVED in nexus-i711w Stage 2 sub-stage C-store. It was
deep maintenance: it mutated through the local rich Catalog's low-level event
log and ``_db`` transactions, which is not expressible as a service call, so it
had no service-mode implementation and already refused there. With the local
catalog deleted there is no path left to implement, and Hal ruled it
unsupported rather than reimplemented (2026-07-29). Its only helper,
``nexus.catalog.dedupe``, had no other consumer and died with it.

Shared helpers stay in ``commands.catalog``; ``owners_cmd`` reaches
``_get_catalog`` through the module object (not a bound import) so the existing
``patch("nexus.commands.catalog._get_catalog", …)`` test seam keeps working.

``--census`` (nexus-7kl32) is a read-only diagnostic arm: it classifies every
registered repo owner's on-disk root as ``healthy`` / ``path_vanished`` /
``path_exists_empty`` / ``unreadable``, surfacing the dead-owner debris
population (bench-index sandboxes, throwaway probe checkouts, stale
worktrees) that ``nx doctor``'s git-hooks check used to render as a
signal-free green (nexus-9t86i). There is deliberately NO mutation arm this
round: ``nexus.catalog_owners`` carries no soft-delete column and no engine
route exists to deregister or tombstone an owner row (confirmed by reading
``catalog-001-baseline.xml`` and ``CatalogHandler.java`` — register/upsert/
list/sweep_next_seq_drift only). Adding one needs new Liquibase DDL + a Java
route + a client method, which is out of scope for this Python-only change
per the bead's own constraint ("if deregistration needs a new engine route,
STOP that sub-part and report instead"). The census output says so plainly
rather than pretending a working mutation exists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader  # noqa: F401 — PEP 563 deferred annotation use

#: Classification buckets a repo owner's root path can land in
#: (nexus-7kl32). Order matches the human-report presentation order.
_CENSUS_CLASSES = ("healthy", "path_vanished", "path_exists_empty", "unreadable")

#: Itemized-row cap per bucket in the human report (mirrors the
#: reconcile-stale precedent's ``_CAP_ACTION``/``_CAP_INFO`` convention).
_CENSUS_ROW_CAP = 20

_MUTATION_NOTE = (
    "Mutation (deregistering/tombstoning dead owners) is NOT implemented "
    "this round: the catalog_owners engine table carries no soft-delete "
    "column and no owner-delete/deactivate route exists yet — adding one "
    "needs new Liquibase DDL + a Java route + a client method (nexus-7kl32). "
    "This census is diagnosis-only."
)


def _classify_owner_root(repo_root: str) -> str:
    """Classify a registered owner's on-disk root path (nexus-7kl32).

    Four states — a check that cannot confidently establish "healthy" must
    never silently default to it (the same honesty principle nexus-9t86i
    forced onto doctor's git-hooks rendering):

    * ``path_vanished``     — the root does not exist at all. The dead-owner
      debris population: bench-index sandboxes, throwaway probe checkouts,
      stale worktrees.
    * ``path_exists_empty`` — the root is still there but has been emptied
      out (contents removed, directory left behind).
    * ``unreadable``        — the root's existence or contents could not be
      confirmed (e.g. a permission error). Distinct from ``healthy`` on
      purpose: an unreadable directory is not evidence of health.
    * ``healthy``           — the root exists and has content.
    """
    p = Path(repo_root)
    try:
        exists = p.exists()
    except OSError:
        return "unreadable"
    if not exists:
        return "path_vanished"
    if p.is_dir():
        try:
            if not any(p.iterdir()):
                return "path_exists_empty"
        except OSError:
            return "unreadable"
    return "healthy"


def _run_census(cat: "CatalogReader", *, as_json: bool) -> None:
    """Read-only owner-root census (nexus-7kl32 arm a). Never constructs a
    catalog writer — this is report-first, same discipline as
    ``reconcile-stale``'s default mode."""
    owners = cat.list_owners_by_type("repo")

    buckets: dict[str, list[dict]] = {c: [] for c in _CENSUS_CLASSES}
    no_root: list[dict] = []
    for o in owners:
        root = o.get("repo_root") or ""
        row = {
            "tumbler": o.get("tumbler_prefix"),
            "name": o.get("name"),
            "repo_root": root,
        }
        if not root:
            # Mirrors reconcile-stale's ``no_repo_root`` disposition: absence
            # of a root is not evidence the owner is dead, just that this
            # census cannot say anything about it either way.
            no_root.append(row)
            continue
        buckets[_classify_owner_root(root)].append(row)

    dead_count = len(buckets["path_vanished"]) + len(buckets["path_exists_empty"])

    if as_json:
        payload = {
            "total_repo_owners": len(owners),
            "no_repo_root": no_root,
            **buckets,
            "dead_owner_count": dead_count,
            "mutation_status": "not_implemented",
            "mutation_note": _MUTATION_NOTE,
        }
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(
        f"Owner-root census: {len(owners)} repo owner(s) examined "
        f"({len(no_root)} with no repo_root, skipped)."
    )
    for cls in _CENSUS_CLASSES:
        click.echo(f"  {cls:<18} {len(buckets[cls])}")

    for cls in ("path_vanished", "path_exists_empty", "unreadable"):
        rows = buckets[cls]
        if not rows:
            continue
        click.echo(f"\n{cls} ({len(rows)}):")
        for row in rows[:_CENSUS_ROW_CAP]:
            click.echo(f"    {row['tumbler'] or '':<10} {row['repo_root']}")
        if len(rows) > _CENSUS_ROW_CAP:
            click.echo(f"    ... and {len(rows) - _CENSUS_ROW_CAP} more")

    click.echo(
        f"\n{dead_count} dead owner(s) (path_vanished + path_exists_empty) "
        "are GC candidates."
    )
    click.echo(f"\n{_MUTATION_NOTE}")


@click.command("owners")
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--census", "do_census", is_flag=True,
    help=(
        "Classify repo-owner root paths as healthy / path_vanished / "
        "path_exists_empty / unreadable (nexus-7kl32). Read-only."
    ),
)
def owners_cmd(as_json: bool, do_census: bool) -> None:
    """List registered owners, or run a root-path census with --census."""
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()

    if do_census:
        _run_census(cat, as_json=as_json)
        return

    owners = cat.list_owners()
    if as_json:
        data = [
            {
                "tumbler": o.get("tumbler_prefix"),
                "name": o.get("name"),
                "type": o.get("owner_type"),
                "repo_hash": o.get("repo_hash"),
                "description": o.get("description"),
                "next_seq": o.get("next_seq"),
            }
            for o in owners
        ]
        click.echo(json.dumps(data, indent=2))
    else:
        for o in owners:
            click.echo(
                f"{o.get('tumbler_prefix', ''):<8} "
                f"{o.get('owner_type', ''):<10} "
                f"{o.get('name', '')}"
            )


def register(group: click.Group) -> None:
    """Attach the owner-management commands to the shared ``catalog`` group."""
    group.add_command(owners_cmd)
