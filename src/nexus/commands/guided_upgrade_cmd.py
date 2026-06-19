# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx guided-upgrade`` — RDR-002 one-command upgrade to the service stack.

Stands up + verifies the bge-768 engine-service, then drives the EXISTING
``nx migrate-to-service`` (RDR-162). A thin orchestrator over reused pieces:

  pre-flight detect (ez5.2) → provision + health-gate + version-pin (ez5.6/5/4,
  via establish_verified_service ez5.7) → migrate-to-service (RDR-162
  _run_migration) → advisory ``nx catalog rebuild``.

SERVICE path only (never embed_migrate). A fresh user with nothing to migrate
no-ops WITHOUT provisioning. A not-ready / wrong-version service hard-fails
with a remedy and NEVER migrates. Idempotent: re-running is safe (every reused
step is idempotent). Named ``guided-upgrade`` to avoid colliding with the
existing schema-migration ``nx upgrade``.
"""

from __future__ import annotations

from urllib.parse import urlparse

import click
import structlog

from nexus.migration.guided_upgrade import (
    ProvisionResult,
    detect_pending_migration,
    establish_verified_service,
)

_log = structlog.get_logger(__name__)

#: Default health-gate deadline. Generous: a cold service (PG provision +
#: supervisor spawn + migration apply) can take a while to answer /health.
_DEFAULT_TIMEOUT_S = 120.0


def _provision_thunk_for_url(service_url: str):  # noqa: ANN202
    """Build a provision step that wraps an ALREADY-running service url.

    When ``--service-url`` is given the command gates the existing service
    instead of provisioning a new one; host/port are parsed best-effort (only
    informational — the gate keys off the url string).
    """
    url = service_url.rstrip("/")
    parsed = urlparse(url)

    def _thunk() -> ProvisionResult:
        return ProvisionResult(
            service_url=url,
            host=parsed.hostname or url,
            port=parsed.port or 0,
            pid=None,
            generation=None,
        )

    return _thunk


@click.command("guided-upgrade")
@click.option("--local-path", default=None, help="Override the local Chroma path.")
@click.option("--db", "db_path", default=None, help="Override the T2 SQLite path.")
@click.option("--catalog-db", "catalog_db_path", default=None,
              help="Override the catalog SQLite path.")
@click.option("--service-url", default=None,
              help="Gate an already-running service at this URL instead of "
                   "provisioning a new one.")
@click.option("--timeout", "timeout_s", type=float, default=_DEFAULT_TIMEOUT_S,
              help="Seconds to wait for the service to become healthy.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, default=False,
              help="Proceed without the confirmation prompt.")
def guided_upgrade_cmd(
    local_path: str | None,
    db_path: str | None,
    catalog_db_path: str | None,
    service_url: str | None,
    timeout_s: float,
    assume_yes: bool,
) -> None:
    """Stand up the service stack and migrate Chroma → pgvector in one command."""
    # 1. PRE-FLIGHT — is there anything to migrate? A fresh user no-ops here,
    #    WITHOUT provisioning a service for an empty footprint.
    pre = detect_pending_migration(local_path=local_path)
    if not pre.needs_migration:
        click.echo(
            "No pre-RDR-160 Chroma footprint detected — nothing to migrate; "
            "you are already on the service stack."
        )
        return

    click.echo(
        f"Detected {pre.data_bearing_count} data-bearing Chroma collection(s) "
        f"to migrate ({pre.classified_unsupported_count} classified unsupported "
        "— legacy-model collections are auto-remapped, not blocked)."
    )
    if not assume_yes and not click.confirm(
        "Provision the service stack and migrate now?"
    ):
        click.echo("Aborted — no changes made.")
        return

    # 2. PROVISION + VERIFY — stand up (or gate) the service and confirm it is
    #    healthy AND version-pinned. Never migrate a not-ready/wrong service.
    if service_url:
        click.echo(f"\nVerifying the service at {service_url} …")
        readiness = establish_verified_service(
            timeout_s=timeout_s, provision=_provision_thunk_for_url(service_url)
        )
    else:
        click.echo("\nProvisioning and starting the service stack …")
        readiness = establish_verified_service(timeout_s=timeout_s)

    if not readiness.ready:
        click.echo("", err=True)
        click.echo(f"Service not ready — NOT migrating: {readiness.reason}", err=True)
        click.echo(
            "Remedy: resolve the above, then re-run `nx guided-upgrade` "
            "(it is idempotent and safe to retry).",
            err=True,
        )
        raise SystemExit(1)

    click.echo(
        f"  Service verified at {readiness.service_url} "
        "(healthy + version-pinned)."
    )

    # 3. HAND OFF — drive the existing migrate-to-service against the VERIFIED
    #    url. _run_migration renders the verdict and raises SystemExit(1) on any
    #    block (sentinel migrated-failed + rollback offer), exits 0 on success.
    from nexus.commands.migrate_cmd import _run_migration  # noqa: PLC0415

    _run_migration(local_path, db_path, catalog_db_path, readiness.service_url)

    # 4. ADVISORY post-step — vectors now live in pgvector; refresh the catalog.
    click.echo("")
    click.echo(
        "Advisory: refresh the catalog now that vectors serve from pgvector:"
    )
    click.echo("  nx catalog rebuild")
