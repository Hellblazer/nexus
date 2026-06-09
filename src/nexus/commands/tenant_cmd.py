# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx tenant`` — tenant provisioning (RDR-152 bead nexus-gmiaf.32.3).

Mints a new tenant principal and its first bound service token. Provisioning rides the
existing bootstrap ``NX_SERVICE_TOKEN`` to authenticate to the storage service until
per-tenant tokens exist. The minted token is shown ONCE; the service stores only its hash.
"""

from __future__ import annotations

import click
import structlog

from nexus.db.t2.http_token_store import HttpTokenStore

_log = structlog.get_logger(__name__)


@click.group()
def tenant() -> None:
    """Manage storage-service tenants (provisioning)."""


@tenant.command("create")
@click.argument("name")
def create(name: str) -> None:
    """Create tenant NAME and mint its first bound service token.

    The token is printed ONCE. Store it now; only its hash is kept server-side.
    The name '*' is reserved for the bootstrap token and is rejected.
    """
    with HttpTokenStore() as store:
        result = store.create_tenant(name)
    _log.info("tenant.create", tenant=result["tenant"])
    click.echo(f"Tenant '{result['tenant']}' created.")
    click.echo("Initial token (shown once — store it now):")
    click.echo(result["token"])
