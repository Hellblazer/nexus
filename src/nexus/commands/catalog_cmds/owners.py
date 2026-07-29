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
"""
from __future__ import annotations

import json

import click


@click.command("owners")
@click.option("--json", "as_json", is_flag=True)
def owners_cmd(as_json: bool) -> None:
    """List registered owners."""
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    owners = cat.list_owners()
    if as_json:
        data = [
            {
                "tumbler": o.get("tumbler_prefix"),
                "name": o.get("name"),
                "type": o.get("owner_type"),
                "repo_hash": o.get("repo_hash"),
                "description": o.get("description"),
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
