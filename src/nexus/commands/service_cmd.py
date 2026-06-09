# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx service`` — storage-service administration (RDR-152 bead nexus-gmiaf.32.3).

The ``nx service token`` subgroup manages the bridge token lifecycle: issue, rotate
(zero-downtime overlap), revoke, and list. All SQL runs in the Java service; this is the
thin client surface. Minted tokens are shown ONCE; only their hash is stored.
"""

from __future__ import annotations

import click
import structlog

from nexus.db.t2.http_token_store import HttpTokenStore

_log = structlog.get_logger(__name__)


@click.group()
def service() -> None:
    """Administer the storage service."""


@service.group("token")
def token_group() -> None:
    """Manage service bearer tokens (issue / rotate / revoke / list)."""


def _print_issued(result: dict[str, object]) -> None:
    click.echo(f"Tenant: {result['tenant']}")
    click.echo("Token (shown once — store it now):")
    click.echo(str(result["token"]))


@token_group.command("issue")
@click.option("--tenant", required=True, help="Tenant to bind the token to.")
@click.option("--label", default=None, help="Optional human-readable label.")
@click.option("--ttl", "ttl_seconds", type=int, default=None,
              help="Optional lifetime in seconds (default: no expiry).")
def issue(tenant: str, label: str | None, ttl_seconds: int | None) -> None:
    """Issue a new bound token for TENANT. Printed once; only the hash is stored."""
    with HttpTokenStore() as store:
        result = store.issue_token(tenant, label, ttl_seconds)
    _log.info("service.token.issue", tenant=tenant, label=label, ttl_seconds=ttl_seconds)
    _print_issued(result)


@token_group.command("rotate")
@click.option("--tenant", required=True, help="Tenant whose tokens to rotate.")
@click.option("--grace", "grace_seconds", type=int, default=None,
              help="Overlap window in seconds before old tokens expire (service default: 300).")
def rotate(tenant: str, grace_seconds: int | None) -> None:
    """Rotate TENANT's tokens with zero downtime.

    Issues a new token and sets the previous live tokens to expire after the grace
    window, so both are valid during the overlap. Running clients pick up the new token
    by rediscovering the lease the storage-service supervisor publishes; they do not need
    a restart and will not see 401s during the window.
    """
    with HttpTokenStore() as store:
        result = store.rotate_token(tenant, grace_seconds)
    _log.info("service.token.rotate", tenant=tenant, grace_seconds=grace_seconds)
    click.echo(f"Rotated tokens for tenant '{tenant}'. Old tokens remain valid through the grace window.")
    _print_issued(result)


@token_group.command("revoke")
@click.argument("selector")
def revoke(selector: str) -> None:
    """Revoke a token by full hash or a unique hash prefix (SELECTOR).

    Revocation is immediate on the storage service that handles the request (its auth
    cache is invalidated in-process). For any other reader, revocation propagates within
    the AuthFilter token-cache TTL bound (default 30s).
    """
    with HttpTokenStore() as store:
        result = store.revoke_token(selector)
    if result.get("revoked"):
        _log.info("service.token.revoke", token_hash=result.get("token_hash"))
        click.echo(f"Revoked token {result.get('token_hash')}.")
    else:
        raise click.ClickException(f"No unique token matched selector: {selector!r}")


@token_group.command("list")
@click.option("--tenant", default=None, help="Filter to one tenant (default: all).")
def list_tokens(tenant: str | None) -> None:
    """List service tokens (id / tenant / label / status / created / expires / revoked).

    Never prints the raw token — only its hash. Use the 12-char id prefix with
    ``nx service token revoke``.
    """
    with HttpTokenStore() as store:
        rows = store.list_tokens(tenant)
    if not rows:
        click.echo("No tokens.")
        return
    click.echo(f"{'ID':<14}{'TENANT':<20}{'STATUS':<10}{'LABEL':<20}{'EXPIRES':<28}REVOKED")
    for row in rows:
        token_hash = str(row.get("token_hash", ""))
        click.echo(
            f"{token_hash[:12]:<14}"
            f"{str(row.get('tenant', '')):<20}"
            f"{str(row.get('status', '')):<10}"
            f"{str(row.get('label') or ''):<20}"
            f"{str(row.get('expires_at') or '-'):<28}"
            f"{row.get('revoked_at') or '-'}"
        )
