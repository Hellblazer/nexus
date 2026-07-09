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


@service.command("probe")
@click.option(
    "--url",
    "url",
    default=None,
    help="Managed service base URL. Defaults to NX_SERVICE_URL or "
    "https://api.conexus-nexus.com.",
)
def probe(url: str | None) -> None:
    """Probe a managed nexus service for reachability + version compatibility.

    Cloud-mode capability check (RDR-001): GETs the unauthenticated ``/version``
    handshake and FAILS LOUD with a remedy when the service is unreachable or
    incompatible. Performs no Postgres connection — this is the HTTPS client
    contract only.
    """
    from nexus.db.managed_endpoint import (  # noqa: PLC0415 — circular-dep avoidance; managed_endpoint imports config
        ManagedServiceError,
        probe_managed_service,
        resolve_managed_endpoint,
    )

    base = url or resolve_managed_endpoint(require_token=False)[0]
    try:
        caps = probe_managed_service(base_url=base)
    except ManagedServiceError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"✓ managed nexus service reachable: {caps.base_url}")
    click.echo(f"  release_version: {caps.release_version}")
    click.echo(f"  app_version:     {caps.app_version}")
    click.echo(f"  embedding_mode:  {caps.embedding_mode}")
    if caps.embedding_models:
        click.echo(f"  models:         {', '.join(caps.embedding_models)}")
    if caps.schema_latest_id:
        click.echo(
            f"  schema:         {caps.schema_latest_id} "
            f"({caps.schema_changeset_count} changesets)"
        )


def _normalize_engine_version(tag: str) -> str:
    """Reduce an engine tag to its bare ``X.Y.Z`` release version.

    Accepts ``engine-service-vX.Y.Z``, ``vX.Y.Z``, or ``X.Y.Z`` — the forms an
    operator or the engine-release skill might pass — and returns ``X.Y.Z`` for
    comparison against the live ``/version`` ``release_version`` field.
    """
    return tag.removeprefix("engine-service-").removeprefix("v")


@service.command("record-deploy")
@click.argument("tag")
@click.option("--commit", "commit", default="", help="Deployed commit SHA (provenance).")
@click.option(
    "--gate",
    "gate",
    default="",
    help="Cloud-gate result to record (e.g. PASSED).",
)
@click.option(
    "--url",
    "url",
    default=None,
    help="Managed service base URL. Defaults to NX_SERVICE_URL or "
    "https://api.conexus-nexus.com.",
)
def record_deploy(tag: str, commit: str, gate: str, url: str | None) -> None:
    """Record TAG as the cloud-deployed engine — GUARDED by a live ``/version`` read.

    Guards the ``deployed-engine-version`` T2 tracker against recording a version
    that disagrees with reality (nexus-dz6b1 / RDR-179). GETs the managed
    service's unauthenticated ``/version`` handshake, ASSERTS its
    ``release_version`` equals TAG's version, and only then writes the tracker.
    The recorded version is machine-sourced from the live read — never
    hand-typed — so the record cannot disagree with what the cloud is actually
    running, and a deploy that has not landed yet fails loud instead of writing a
    wrong fact.

    SCOPE — what this does NOT do: it does not guarantee the record is *made*.
    The original rot (v0.1.17 stale across three deploys) was an omission — the
    write step was skipped — and this command still has to be RUN (engine-release
    skill Step 8). Closing the omission vector (cloud-gate writes the tracker as a
    side effect of passing) is tracked separately (nexus-dz6b1 follow-up). The
    ``--commit`` / ``--gate`` values are operator-supplied provenance and are
    recorded verbatim — only ``release_version`` is verified against the live
    deploy.
    """
    from datetime import UTC, datetime  # noqa: PLC0415 — function-local: keep import cost off the CLI hot path

    from nexus.db.managed_endpoint import (  # noqa: PLC0415 — circular-dep avoidance; managed_endpoint imports config
        ManagedServiceError,
        probe_managed_service,
        resolve_managed_endpoint,
    )

    expected = _normalize_engine_version(tag)
    base = url or resolve_managed_endpoint(require_token=False)[0]
    try:
        caps = probe_managed_service(base_url=base)
    except ManagedServiceError as exc:
        raise click.ClickException(str(exc)) from exc

    live = caps.release_version
    if live != expected:
        raise click.ClickException(
            f"Refusing to record {tag}: the service at {caps.base_url} is running "
            f"release_version {live!r}, not {expected!r}. Deploy the tag first, "
            "then re-run record-deploy (the tracker only records verified deploys)."
        )

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"engine-service-v{live} @ {commit or '<commit unrecorded>'}"]
    parts.append(f"recorded {timestamp}")
    parts.append(f"gate {gate or '<gate result unrecorded>'}")
    parts.append(f"verified live at {caps.base_url}/version")
    content = "; ".join(parts)

    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 — circular-dep avoidance; _helpers imports command surfaces

    with t2_handle() as handle:
        handle.memory.put(
            project="nexus",
            title="deployed-engine-version",
            content=content,
            tags="engine,deploy,tracker,rdr-179",
            ttl=0,  # permanent operational record
        )

    _log.info("service.record_deploy", tag=tag, live=live, base_url=caps.base_url)
    click.echo(f"✓ recorded deployed engine: {content}")


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
@click.option("--scope", type=click.Choice(["tenant", "mint", "mint-locked"]), default=None,
              help="Token scope (nexus-868dq): default 'tenant'; 'mint' issues the "
                   "cross-tenant data-token mint credential (operator/root bearer required); "
                   "'mint-locked' issues a tenant-bound mint credential that may only mint "
                   "data tokens for the tenant it is bound to (RDR-005 2a, nexus-xidcq). "
                   "'data' tokens are minted only by POST /v1/data-tokens/mint, never issued here.")
def issue(tenant: str, label: str | None, ttl_seconds: int | None, scope: str | None) -> None:
    """Issue a new bound token for TENANT. Printed once; only the hash is stored."""
    with HttpTokenStore() as store:
        result = store.issue_token(tenant, label, ttl_seconds, scope)
    _log.info("service.token.issue", tenant=tenant, label=label, ttl_seconds=ttl_seconds,
              scope=scope)
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
