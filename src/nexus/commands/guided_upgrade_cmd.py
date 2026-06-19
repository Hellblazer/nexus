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
with a remedy and NEVER migrates. Named ``guided-upgrade`` to avoid colliding
with the existing schema-migration ``nx upgrade``.

RE-RUN SEMANTICS (honest scope): re-running is SAFE — every reused step is
idempotent (provision short-circuits on a live lease; the migrate ETL upserts;
a blocked run leaves ``migrated-failed`` and is correctly retried). It is NOT a
no-op after a SUCCESSFUL migration: copy-not-move leaves the Chroma source
intact, the success path clears the migration-state sentinel, and there is no
persistent "migrated" marker to short-circuit on — so a re-run re-detects the
Chroma data and re-copies it (idempotent, but full-cost, with reads briefly
degraded during the redo). Detecting an already-migrated state (e.g. via a
pgvector count probe) is tracked separately, not part of this wiring.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import click
import structlog

from nexus.migration.guided_upgrade import (
    ProvisionResult,
    detect_pending_migration,
    establish_verified_service,
    footprint_has_voyage_collections,
    verify_voyage_capability,
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
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise click.BadParameter(
            f"--service-url must be an http(s) URL with a host, got {service_url!r}",
            param_hint="--service-url",
        )

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
    from nexus.daemon.storage_service_daemon import (  # noqa: PLC0415
        StorageServiceStartError,
    )

    try:
        if service_url:
            click.echo(f"\nVerifying the service at {service_url} …")
            readiness = establish_verified_service(
                timeout_s=timeout_s, provision=_provision_thunk_for_url(service_url)
            )
        else:
            click.echo("\nProvisioning and starting the service stack …")
            readiness = establish_verified_service(timeout_s=timeout_s)
    except StorageServiceStartError as exc:
        # provision_and_serve (-> init.provision_and_start_service) raises this
        # when no native binary is available; render the remedy it carries
        # rather than a traceback (code-review H2).
        click.echo("", err=True)
        click.echo(f"Could not start the storage service: {exc}", err=True)
        raise SystemExit(1)

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

    # 2a. VOYAGE-CAPABILITY GATE (nexus-8o9pm) — if the footprint has voyage-model
    #     collections, the target MUST be voyage-capable. Voyage collections are
    #     NOT cross-model-remapped to bge (re-embedding voyage text into bge
    #     changes recall), so a bge-only target would block them mid-migration.
    #     Catch it HERE with a precise message, keyed on the service's ACTUAL
    #     /version capability (the authoritative server-side signal), not the
    #     client's voyage-key probe.
    # NOTE (substantive-critic Sig-1): this gate reads /version at
    # readiness.service_url; the migration's own pre-gate (RDR-159 P1) re-reads
    # /version via resolve_service_config() (NX_SERVICE_URL env). They converge
    # because _run_migration below sets NX_SERVICE_URL = readiness.service_url
    # BEFORE the pre-gate runs, so both observe the same service. The coupling is
    # implicit; making it explicit (thread the verified url to run_guided_upgrade)
    # is tracked as a follow-up.
    if footprint_has_voyage_collections(pre.report):
        cap = verify_voyage_capability(readiness.service_url)
        if not cap.ok:
            click.echo("", err=True)
            click.echo(
                f"Your footprint includes voyage-model collection(s), but the "
                f"target service cannot serve voyage: {cap.reason}. Configure "
                "voyage on the service (NX_VOYAGE_API_KEY / nx config set "
                "voyage_api_key) and restart it, then re-run — or migrate "
                "elsewhere; these collections cannot be re-embedded into bge.",
                err=True,
            )
            raise SystemExit(1)

    # 2b. SELF-LOAD CREDENTIALS — the manual path sources pg_credentials between
    #     `nx init --service` and `nx migrate-to-service`; this is ONE process, so
    #     load the freshly-provisioned NX_SERVICE_TOKEN / NX_STORAGE_BACKEND into
    #     the env before the handoff (else _run_migration sees no token). Skip on
    #     the --service-url path: that targets an external service whose token the
    #     user supplies via NX_SERVICE_TOKEN directly.
    if not service_url:
        from nexus.config import nexus_config_dir  # noqa: PLC0415
        from nexus.db.pg_provision import (  # noqa: PLC0415
            load_service_credentials_into_env,
        )

        if not load_service_credentials_into_env(nexus_config_dir()):
            click.echo("", err=True)
            click.echo(
                "Service provisioned but no NX_SERVICE_TOKEN is available "
                "(neither in the environment nor pg_credentials) — cannot "
                "authenticate the migration. Re-run after `nx init --service`.",
                err=True,
            )
            raise SystemExit(1)
    elif not os.environ.get("NX_SERVICE_TOKEN", "").strip():
        # --service-url path: the external service's token is the user's to
        # supply. Gate here (not deep inside _run_migration's HTTP layer) so the
        # remedy is a guided-upgrade checkpoint, not an opaque auth error
        # (substantive-critic Sig-2).
        click.echo("", err=True)
        click.echo(
            f"--service-url {service_url} requires NX_SERVICE_TOKEN to be set "
            "(the managed service's bearer token) — export it and re-run.",
            err=True,
        )
        raise SystemExit(1)

    # 3. HAND OFF — drive the existing migrate-to-service against the VERIFIED
    #    url. _run_migration renders the verdict and raises SystemExit(1) on any
    #    block (sentinel migrated-failed + rollback offer), exits 0 on success.
    from nexus.commands.migrate_cmd import _run_migration  # noqa: PLC0415

    # _run_migration sets os.environ["NX_SERVICE_URL"] as a process-level side
    # effect (its own contract assumes subprocess-exit cleanup). We call it as a
    # library function in-process, so save/restore to avoid leaking the url into
    # anything that runs after this command in the same process (code-review M1).
    _prev_url = os.environ.get("NX_SERVICE_URL")
    try:
        _run_migration(local_path, db_path, catalog_db_path, readiness.service_url)
    finally:
        if _prev_url is None:
            os.environ.pop("NX_SERVICE_URL", None)
        else:
            os.environ["NX_SERVICE_URL"] = _prev_url

    # 4. ADVISORY post-step — verify the migrated stack end-to-end.
    click.echo("")
    click.echo("Advisory: verify the migrated stack:")
    click.echo("  nx doctor")
