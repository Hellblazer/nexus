# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx init — guided first-run onboarding (RDR-144).

Makes the local embedder a guided, informed choice rather than a silent
packaging default. ``nx init`` is a NEW top-level verb, distinct from the
credentials wizard ``nx config init`` (gate-locked, RDR-144).

Phase 2 scope: detect cloud-vs-local, present the choice, persist it to
``config.yml``. The model fetch and ``[local]`` extra-add are P3; the
config-key → embedding-function wiring lands with P3 (where the extra is
guaranteed present). This module performs NO network or install work.
"""
from __future__ import annotations

import os
from pathlib import Path

import click
import structlog

import nexus.config as _config
from nexus.config import set_config_value
from nexus.db.local_ef import _TIER1_MODEL

_log = structlog.get_logger(__name__)

#: Config key the choice is persisted under. Read by the EF selection path
#: in P3 (consumer wiring deferred — P2 only records the choice).
_EMBED_MODEL_KEY = "local.embed_model"


# ── P3 service-embedder provisioning (RDR-160 P3.1 / P3.2) ────────────────────


def _provision_service_embedder_step(embedder: str | None) -> None:
    """Lock the service embedder to bge-768 and fetch the standard ONNX it reads.

    RDR-160: a ``--service`` install routes EVERY collection through the Java
    service's bge-768 embedder (768-dim). Two things follow:

    * **P3.2 (RDR-144 reverse-gap):** ``minilm-384`` is non-operative on the
      service T3 path, so an explicit ``--embedder minilm-384`` gets an ADVISORY
      rather than being silently ignored. (minilm-384 stays valid for a
      non-service local install.)
    * **P3.1:** the CLI fetches the STANDARD un-fused bge ONNX (NOT fastembed's
      optimized cache, which onnxruntime-java cannot load) to the stable
      Java-read path; the service only reads the file. Offline failure is loud
      (no silent fallback).
    """
    from nexus.db.service_bge_model import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        SERVICE_BGE_DOWNLOAD_HINT,
        fetch_service_bge_onnx,
    )

    if embedder == "minilm-384":
        click.echo(
            "\nNote: a --service install embeds every collection with bge-768 "
            "(768-dim) in the Java service. The minilm-384 choice is non-operative "
            "for the service T3 path; provisioning bge-768 instead. (minilm-384 "
            "remains valid for a non-service local install.)",
            err=True,
        )

    # Record bge-768 as the active local model so the rest of nx (catalog model
    # segment, doctor advisory) agrees with what the service actually runs.
    set_config_value(_EMBED_MODEL_KEY, _TIER1_MODEL)
    click.echo(f"\nSaved: {_EMBED_MODEL_KEY} = {_TIER1_MODEL} (service: bge-768 only)")

    click.echo(
        f"\nProvisioning the standard bge-768 ONNX the service reads "
        f"({SERVICE_BGE_DOWNLOAD_HINT}) — one-time download …"
    )
    try:
        dest = fetch_service_bge_onnx()
        click.echo(f"Done — service bge-768 model ready at {dest}.")
    except Exception as exc:  # noqa: BLE001 — must stay actionable
        # Fail loud AND fatal: unlike a Python fastembed path that auto-fetches
        # on first use, the Java service has NO retry — it
        # fail-loud-crashes at boot without this file. So a swallowed failure
        # would make `nx init --service` look successful while leaving an
        # un-bootable service. Surface it as a hard error; re-run when online
        # (idempotent — PG provisioning is skipped on the retry).
        _log.warning("service_bge_provision_failed", error=str(exc))
        raise click.ClickException(str(exc)) from exc


def _ensure_service_binary_step(config_dir: Path) -> bool:
    """Acquire the signed native service binary if none is already installed.

    RDR-161 P1: between PG provisioning and starting the service, place the
    verified native binary so ``_start_service_step`` has something to exec.

    Idempotent: a binary already resolvable at the well-known location (or via
    ``NEXUS_SERVICE_BIN``) is a no-op — it is NOT re-downloaded. When no binary
    is present, the explicit ``engine-service-v*`` tag comes from
    ``resolve_service_tag`` (``NEXUS_SERVICE_TAG`` env, then the build-time
    pin); there is no "latest" resolution (RF-161-2). When no tag is
    configured, instruct the user to install one explicitly rather than
    guessing a tag or downloading silently.

    Returns ``True`` when a native binary is ready to exec, ``False`` when none
    is available and none could be acquired (no tag configured). The caller
    MUST NOT start the service on ``False`` — since RDR-161 P3 expunged the
    legacy JVM launch path, starting without a binary now fails loud rather than
    silently degrading, but skipping the start keeps the UX clean (CRE C1).
    Hard failures (broken ``NEXUS_SERVICE_BIN`` override, a configured tag that
    fails verification) raise ``SystemExit``.
    """
    import os  # noqa: PLC0415 — deferred to keep CLI startup fast

    from importlib.metadata import PackageNotFoundError, version as _pkg_version  # noqa: PLC0415 — deferred to keep CLI startup fast

    from nexus.daemon.binary_install import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        BinaryVerificationError,
        install_binary,
        resolve_service_tag,
    )
    from nexus.daemon.storage_service_daemon import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        StorageServiceStartError,
        _find_service_binary,
    )

    # Explicit dev/test JAR opt-in (RDR-161 amendment): when NEXUS_SERVICE_JAR is
    # set the supervisor launches the JVM, so no native binary need be acquired.
    # The supervisor's own resolution fails loud if the path is set-but-missing.
    if os.environ.get("NEXUS_SERVICE_JAR", "").strip():
        click.echo(
            "  Using NEXUS_SERVICE_JAR (dev/test JVM launch, UNVERIFIED) — "
            "skipping native binary acquisition."
        )
        return True

    try:
        existing = _find_service_binary(config_dir)
    except StorageServiceStartError as exc:
        # e.g. NEXUS_SERVICE_BIN set-but-missing — surface, never auto-download
        # over a deliberate (if broken) operator override.
        click.echo(f"\nService binary check failed: {exc}", err=True)
        raise SystemExit(1)

    if existing is not None:
        click.echo(f"  Native service binary already installed: {existing} (no download).")
        return True

    tag = resolve_service_tag()
    if not tag:
        click.echo(
            "\n  No native service binary is installed and no service tag is "
            "pinned for this build.\n"
            "  Install one explicitly, then re-run `nx init --service`:\n"
            "    nx daemon service install-binary engine-service-vX.Y.Z\n"
            "  (or set NEXUS_SERVICE_TAG=engine-service-vX.Y.Z)."
        )
        return False

    try:
        _nx_version = _pkg_version("conexus")
    except PackageNotFoundError:
        _nx_version = "unknown"

    click.echo(f"\nInstalling the native service binary ({tag}) …")
    try:
        dest, prov = install_binary(
            tag, config_dir, installed_by=f"conexus {_nx_version}",
        )
    except BinaryVerificationError as exc:
        click.echo(f"\nService binary install failed: {exc}", err=True)
        raise SystemExit(1)
    click.echo(f"  Installed {prov['asset']} -> {dest} (sha256+signature verified).")
    return True


def _start_service_step():  # noqa: ANN201 — returns LeaseRecord (avoid import cycle)
    """Start the PERSISTENT storage-service supervisor and confirm it is serving.

    Returns the live ``LeaseRecord`` so a programmatic caller (RDR-002
    ``nx guided-upgrade`` provision sequence) can read the serving endpoint;
    ``init_cmd`` ignores the return and uses it purely for its side effect.

    RDR-157 P4.1: the final step of the ``nx init --service`` one-command
    collapse. nexus-qke1e: routes through ``ensure_storage_supervisor`` (the
    single persistent-start path shared with ``nx daemon service start``) rather
    than the old transient ``start_storage_service``. The transient path
    published a lease WITHOUT a heartbeating supervisor, so the service looked
    'serving' at init time but the lease aged out by TTL before the next client
    (e.g. ``nx migrate-to-service``) could discover it. The persistent supervisor
    heartbeats the lease for the process lifetime, so 'serving' stays true.
    Idempotent — a live lease short-circuits, so re-running ``nx init`` is safe.
    Any failure surfaces as an actionable error with a remedy, never a traceback.
    """
    from nexus.commands.daemon import ensure_storage_supervisor  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.daemon.storage_service_daemon import StorageServiceStartError  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    click.echo("\nStarting the storage service …")
    try:
        lease = ensure_storage_supervisor(nexus_config_dir())
    except StorageServiceStartError as exc:
        # StorageServiceStartError messages already carry a remedy (build/install
        # the artifact, re-run init, etc.); relay verbatim, fail fatal.
        click.echo(f"\nStorage service failed to start: {exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 — user-facing, must stay actionable
        _log.error("service_start_failed", error=str(exc))
        click.echo(f"\nStorage service failed to start: {exc}", err=True)
        raise SystemExit(1)

    ep = lease.endpoint
    click.echo(
        f"  Service running on {ep.get('host')}:{ep.get('port')} "
        f"(pid {ep.get('pid')}, generation {lease.generation})."
    )
    click.echo("  nx init --service complete — the service backend is serving.")
    return lease


# ── P5 (A) Postgres provisioning ──────────────────────────────────────────────


def _select_bundled_pg(
    config_dir: Path, *, search_dirs: list[Path] | None = None
) -> Path | None:
    """First-run: extract the ship-alongside PG bundle and select it for provisioning.

    RDR-157 P3.4 (bead nexus-vwvv5.13). When a ``nexus-pg-<platform>.txz`` is
    locatable (``NEXUS_PG_BUNDLE`` or next to the binary), extract it once under the
    config dir and point ``NEXUS_PG_BIN`` at it so ``provision`` discovers the
    bundle's PostgreSQL ahead of any host install. Returns the selected ``bin/`` dir,
    or ``None`` when no bundle is present (dev / host-PG mode) — in which case
    discovery proceeds unchanged.

    An explicit pre-existing ``NEXUS_PG_BIN`` wins (operator override / tests): the
    bundle is not consulted, so a deliberately pointed PG is never overridden.
    """
    if os.environ.get("NEXUS_PG_BIN", "").strip():
        return None
    from nexus.daemon.binary_lifecycle import well_known_binary_path  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.db.pg_bundle import ensure_pg_bundle  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    if search_dirs is None:
        # RF-161-3: default to <config_dir>/service/ — where the P2 acquire seam
        # (install-binary / install-pg-bundle) places nexus-pg-<tag>.txz — not the
        # venv bin/ that ensure_pg_bundle/locate_bundle_archive would otherwise
        # use. The NEXUS_PG_BUNDLE env override still wins (checked first inside
        # locate_bundle_archive) and an explicitly injected search_dirs is honoured.
        search_dirs = [well_known_binary_path(config_dir).parent]
    bin_dir = ensure_pg_bundle(config_dir, search_dirs=search_dirs)
    if bin_dir is not None:
        os.environ["NEXUS_PG_BIN"] = str(bin_dir)
        _log.info("pg_bundle_selected", bin_dir=str(bin_dir))
    return bin_dir


def _provision_postgres_step() -> None:
    """Provision (or verify) the nx-managed local Postgres cluster.

    Called from init_cmd when ``--service`` is passed or when the service
    storage backend is already configured (``NX_STORAGE_BACKEND=service``).

    Structured to be robust: any failure is reported as a clear, actionable
    error rather than a traceback — the user needs an install hint, not a
    Python exception.
    """
    from nexus.db.pg_provision import PgBinaryNotFoundError, provision  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    config_dir = _config.nexus_config_dir()
    click.echo("\nProvisioning local Postgres cluster for the service backend …")

    # First-run local distribution: extract + select the ship-alongside PG bundle
    # (no-op when none is shipped — host PG is then discovered as before).
    try:
        if _select_bundled_pg(config_dir) is not None:
            click.echo("  Using bundled PostgreSQL (extracted on first run).")
    except Exception as exc:  # noqa: BLE001 — user-facing, must stay actionable
        _log.error("pg_bundle_extract_failed", error=str(exc))
        click.echo(f"\nBundled PostgreSQL extraction failed: {exc}", err=True)
        raise SystemExit(1)

    try:
        result = provision(config_dir)
    except PgBinaryNotFoundError as exc:
        click.echo(f"\nPostgres binaries not found.\n{exc}", err=True)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 — user-facing
        _log.error("pg_provision_failed", error=str(exc))
        click.echo(f"\nPostgres provisioning failed: {exc}", err=True)
        raise SystemExit(1)

    if result.already_provisioned:
        click.echo(f"  Postgres already running on port {result.port} — no changes.")
        return

    lines = []
    if result.cluster_created:
        lines.append("  Cluster initialised.")
    if result.db_created:
        lines.append(f"  Database 'nexus' created at {result.credentials_path.parent / 'postgres'}.")
    if result.admin_role_created:
        lines.append("  Role nexus_admin created (schema owner).")
    if result.svc_role_created:
        lines.append("  Role nexus_svc created (DML service role).")
    if result.vector_extension_created:
        lines.append("  Extension 'vector' (pgvector) created.")
    lines.append(f"  Credentials written to {result.credentials_path} (0600).")
    lines.append(
        f"  Cluster listening on 127.0.0.1:{result.port}.\n"
        f"  Set NX_STORAGE_BACKEND=service and source {result.credentials_path} "
        f"before starting the service."
    )
    for line in lines:
        click.echo(line)


def provision_and_start_service(embedder: str | None = None):  # noqa: ANN201
    """The full ``nx init --service`` service sequence — shared with guided-upgrade.

    Provision PG, then (LOCAL mode only) lock the embedder + provision the bge-768
    ONNX the service reads, acquire the native binary, and start the persistent
    supervisor. Returns the live ``LeaseRecord`` on the local-serving path, or
    ``None`` in cloud mode (embeddings run server-side via Voyage; there is no
    local service to start).

    RDR-002: ``nx guided-upgrade`` reuses THIS so its provisioning cannot diverge
    from ``nx init --service`` — the guided path previously called only
    ``_provision_postgres_step`` + ``_start_service_step`` and skipped the
    embedder/model fetch, so the service crashed on a missing bge ONNX. Raises
    :class:`StorageServiceStartError` when no native binary is available.
    """
    from nexus.daemon.storage_service_daemon import StorageServiceStartError  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    _provision_postgres_step()
    if not _config.is_local_mode():
        return None
    # Local service backend (RDR-160): the Java service embeds every collection
    # with bge-768. Lock the embedder + provision the standard ONNX it reads.
    _provision_service_embedder_step(embedder)
    # RDR-161 P1: acquire the verified native binary before starting (no-op when
    # one is already installed). With the legacy JVM launch path expunged
    # (RDR-161 P3), starting without a binary fails loud rather than degrading.
    if not _ensure_service_binary_step(_config.nexus_config_dir()):
        raise StorageServiceStartError(
            "no native nexus-service binary available and none could be acquired "
            "— install one (`nx daemon service install-binary` or configure the "
            "engine-service tag), then retry"
        )
    return _start_service_step()


def _resolve_init_mode() -> str:
    """Resolve the init dispatch mode with explicit precedence (RDR-174 P1.1).

    Returns ``"local"`` or ``"managed"``. Precedence (critic SIG-1, gate-locked):

      1. ``NX_LOCAL=1`` → ``"local"`` — orthogonal override, always wins. It wins
         even with a stale ``service_url`` still in config, which preserves the
         migration / rollback-rehearsal pattern (force-LOCAL provisioning while a
         managed endpoint is still configured).
      2. ``NX_LOCAL=0`` → ``"managed"``.
      3. Otherwise dispatch on ``get_credential("service_url")`` (reads
         ``NX_SERVICE_URL`` env or ``config.yml`` ``service_url``): present →
         ``"managed"``, absent → ``"local"``.

    Deliberately NOT ``is_local_mode()`` — that helper is ``service_url``-blind
    across its 57 callers (RDR-174 RF-5). The global blindness is a separately
    filed, out-of-scope bead; init must not route through it.
    """
    nx_local = os.environ.get("NX_LOCAL", "").strip()
    if nx_local == "1":
        return "local"
    if nx_local == "0":
        return "managed"
    return "managed" if _config.get_credential("service_url") else "local"


@click.command("init")
@click.option(
    "--embedder",
    type=click.Choice(["bge-768", "minilm-384"]),
    default=None,
    help=(
        "Local service embedder selector (minilm-384 gets an advisory — the "
        "Java service embeds with bge-768 only)."
    ),
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Accepted for compatibility; provisioning is non-interactive (no-op).",
)
@click.option(
    "--service",
    "provision_service",
    is_flag=True,
    default=False,
    help=(
        "Provision the local Postgres cluster for the RDR-152 Java service backend. "
        "Creates nexus_admin and nexus_svc roles and writes credentials to "
        "~/.config/nexus/pg_credentials. Idempotent: safe to re-run."
    ),
)
def init_cmd(embedder: str | None, assume_yes: bool, provision_service: bool) -> None:
    """Guided first-run setup — one command that provisions the right backend.

    RDR-174 §1/§3 collapse: ``nx init`` is mode-detecting. ``_resolve_init_mode``
    decides LOCAL vs MANAGED (NX_LOCAL wins; otherwise on a configured
    ``service_url``). In LOCAL mode a plain ``nx init`` provisions and starts the
    local service stack (Postgres + the bge-768 Java service, RDR-160) — the
    same body the now-deprecated ``--service`` flag drives. In MANAGED mode a
    remote nexus service serves every tier, so there is nothing to provision
    locally (the credential wizard for that path lands in P1.2, nexus-r2auz).

    ``--embedder`` selects bge-768 vs minilm-384 for the local service-embedder
    step (minilm-384 gets an advisory — the Java service is bge-768 only).
    ``--service`` is still accepted (it forces local provisioning) and is slated
    for a deprecation notice in P3.1.
    """
    if assume_yes:
        # RDR-174 P1.3: the interactive embedder picker is gone, so there is no
        # prompt left to auto-accept. The flag is retained for CLI/script
        # compatibility (formal deprecation tracked with --service in P3.1) but
        # is now a no-op — say so rather than silently change its semantics.
        click.echo(
            "[--yes/-y is a no-op since the embedder picker was removed; "
            "provisioning is non-interactive]",
            err=True,
        )

    # RDR-174 P1.3 dispatch. PRIMARY oracle is the gate-locked mode helper
    # (_resolve_init_mode — service_url-based, NOT is_local_mode which is
    # service_url-blind across 57 callers, RF-5). We additionally honour the
    # genuine cloud case (is_local_mode False — Voyage server-side embeddings,
    # cloud keys present, no service_url) as a SECONDARY no-provision guard: such
    # a user must NOT have a local Postgres cluster silently provisioned (that
    # would be a regression vs the pre-P1.3 cloud early-return). NX_LOCAL=1 still
    # forces local provisioning even with cloud keys (migration/rehearsal). An
    # explicit ``--service`` forces local provisioning regardless of mode.
    mode = _resolve_init_mode()
    if not provision_service and (mode == "managed" or not _config.is_local_mode()):
        # Data plane served remotely — nothing local to provision. The old
        # cloud-mode early return is FOLDED here (not orphaned). P1.2
        # (nexus-r2auz) replaces the MANAGED arm with the RDR-166 credential
        # wizard + ``nx service`` probe.
        if mode == "managed":
            click.echo("Nexus is configured for MANAGED mode (remote nexus service).")
            click.echo(
                "Every tier is served remotely — there is no local model or "
                "cluster to provision."
            )
            click.echo(
                "Set or update the managed endpoint with "
                "`nx config set service_url` / `service_token`."
            )
        else:
            click.echo("Nexus is configured for CLOUD mode (Voyage embeddings).")
            click.echo(
                "Embeddings run server-side — there is no local model to provision."
            )
            click.echo("Manage cloud credentials with `nx config init`.")
        return

    # LOCAL (or explicit ``--service``): plain ``nx init`` now provisions and
    # starts the local service stack. provision_and_start_service forces bge-768
    # (RDR-160) and is the SAME body ``nx init --service`` and
    # guided_upgrade._default_serve invoke — signature/behaviour unchanged.
    from nexus.daemon.storage_service_daemon import StorageServiceStartError  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    try:
        lease = provision_and_start_service(embedder)
    except StorageServiceStartError:
        # No native binary available and none acquirable. PG is provisioned and
        # _ensure_service_binary_step already printed an actionable install
        # instruction; do NOT start (legacy JVM path expunged, RDR-161 P3 —
        # starting without a binary fails loud, CRE C1). Exit non-zero so the
        # incomplete setup is not mistaken for serving.
        click.echo(
            "\nService NOT started: no native binary available. Install one "
            "(see above), then re-run `nx init` to finish.",
            err=True,
        )
        raise SystemExit(1)
    if lease is None:
        # provision_and_start_service resolved cloud-internal (is_local_mode
        # False): embeddings run server-side via Voyage, no local service to
        # start.
        return
    # RDR-157 P4.1 / RDR-174 §3: one command left the user with a running backend.
    return
