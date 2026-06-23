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
import shutil
import subprocess
from pathlib import Path

import click
import structlog

import nexus.config as _config
from nexus.config import set_config_value
from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL, _fastembed_available

_log = structlog.get_logger(__name__)

#: User-facing choice token → canonical model id understood by
#: ``LocalEmbeddingFunction(model_name=...)``.
_CHOICE_TO_MODEL: dict[str, str] = {
    "bge-768": _TIER1_MODEL,
    "minilm-384": _TIER0_MODEL,
}

#: Config key the choice is persisted under. Read by the EF selection path
#: in P3 (consumer wiring deferred — P2 only records the choice).
_EMBED_MODEL_KEY = "local.embed_model"

#: Approximate one-time download size for the bge-768 ONNX model, stated up
#: front so the user makes an informed choice.
_BGE_DOWNLOAD_HINT = "~140 MB"


# ── P3 (A) extra-add, editable-safe ───────────────────────────────────────────


def _local_extra_installed() -> bool:
    """True when the ``[local]`` extra (fastembed) is importable in-process."""
    return _fastembed_available()


def _uv_receipt_path() -> Path | None:
    """Return the uv-tool install receipt path, or None when this is not a
    uv-tool install (dev/editable tree, or ``uv`` not on PATH).

    Presence of ``$(uv tool dir)/conexus/uv-receipt.toml`` is the signal that
    a ``uv tool install --reinstall`` is safe (it won't clobber a dev tree).
    """
    if shutil.which("uv") is None:
        return None
    try:
        out = subprocess.run(
            ["uv", "tool", "dir"], capture_output=True, text=True, timeout=10, check=True
        )
    except (subprocess.SubprocessError, OSError):
        return None
    receipt = Path(out.stdout.strip()) / "conexus" / "uv-receipt.toml"
    return receipt if receipt.is_file() else None


def _ensure_local_extra() -> bool:
    """Install the ``[local]`` extra when safe.

    uv-tool install (receipt present) → shell an editable-safe reinstall that
    adds ``[local]``. Dev/editable tree (no receipt) → print the manual
    instruction and do NOT shell anything (clobber-a-dev-tree guard, CA-2).

    Returns True when a reinstall was shelled (the model fetches on first
    embed of the freshly-installed venv), False when the user must act.
    """
    receipt = _uv_receipt_path()
    if receipt is None:
        click.echo(
            "\nThe local embedder needs the [local] extra. This looks like a "
            "dev/editable tree, so install it manually:"
        )
        click.echo("  pip install 'conexus[local]'   # or: uv sync --extra local")
        return False

    click.echo("\nInstalling the [local] extra (fastembed) …")
    try:
        subprocess.run(
            ["uv", "tool", "install", "--reinstall", "--from", "conexus[local]", "conexus"],
            check=True,
            timeout=300,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log.warning("local_extra_install_failed", error=str(exc))
        click.echo(f"\nFailed to install the [local] extra: {exc}", err=True)
        click.echo(
            "Install it manually: uv tool install --reinstall 'conexus[local]' conexus",
            err=True,
        )
        return False
    click.echo(
        "Installed. The bge-768 model fetches automatically on first local "
        "embed — or re-run `nx init` to provision it now."
    )
    return True


# ── P4 existing-384 detection + SAFE cleanup/reindex ─────────────────────────


def _offer_stale_migration(assume_yes: bool) -> None:
    """Detect 384-dim collections under the now-active 768 embedder and
    offer the gate-locked safe migration (RDR-144 P4 / CA-3).

    The protocol is dry-run preview -> double-confirm -> reindex-first ->
    delete-after-verify. ``code__`` and sourceless collections are reported
    with their remediation but never auto-deleted (no source to reindex
    from = deleting is pure loss). Any detection failure is non-fatal:
    ``nx init`` must still complete.
    """
    from nexus.db.embed_migrate import detect_stale_local_collections  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.db.local_ef import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        _MODEL_DIMS,
        _MODEL_TOKENS,
        _resolve_local_model,
    )

    # Derive dim AND token from the SAME model resolution so a future
    # caller (e.g. P5a's doctor hint, or a third local model) cannot probe
    # with one model's dimension while naming another's token.
    active_model = _resolve_local_model(warn=False)
    active_token = _MODEL_TOKENS.get(active_model, "bge-base-en-v15-768")
    active_dim = _MODEL_DIMS.get(active_model, 768)

    try:
        from nexus.commands.store import _t3  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        db = _t3()
        stale = detect_stale_local_collections(
            db, active_dim=active_dim, active_token=active_token
        )
    except Exception as exc:  # noqa: BLE001 — detection must never block init
        _log.debug("stale_detection_failed", error=str(exc))
        return

    if not stale:
        return

    # A reindexable collection with sourceless chunks (e.g. indexed files +
    # manual store_put notes) can only be migrated with explicit acceptance
    # of the note loss — keep it separate from the clean reindexable set.
    clean = [s for s in stale if s.kind == "reindexable" and s.sourceless == 0]
    mixed = [s for s in stale if s.kind == "reindexable" and s.sourceless > 0]

    click.echo(
        f"\nFound {len(stale)} collection(s) indexed with a different "
        f"embedder than bge-768 — search returns nothing for these:"
    )
    for s in stale:
        if s.kind == "reindexable" and s.sourceless == 0:
            click.echo(
                f"  • {s.name} ({s.count} chunks) -> reindex into {s.target_name}"
            )
        elif s.kind == "reindexable":  # mixed
            click.echo(
                f"  • {s.name} ({s.count} chunks, including {s.sourceless} manual "
                f"note(s) with no source) -> reindexes files only; the manual "
                f"notes CANNOT be re-embedded"
            )
        elif s.kind == "code":
            click.echo(
                f"  • {s.name} ({s.count} chunks) -> reindex manually: "
                f"nx index repo <path>"
            )
        else:  # sourceless
            click.echo(
                f"  • {s.name} ({s.count} chunks) -> manual entries, no source "
                f"to reindex (left as-is; `nx collection delete {s.name}` to remove)"
            )

    if not clean and not mixed:
        click.echo(
            "\nNothing can be migrated automatically — see the manual steps above."
        )
        return

    # Double-confirm: the old collections are DELETED after a verified
    # reindex. Make the destructive step explicit and ask twice.
    if clean:
        if not assume_yes:
            if not click.confirm(
                f"\nReindex {len(clean)} collection(s) into bge-768 now?",
                default=True,
            ):
                click.echo("Skipped. Run `nx init` again any time to migrate.")
                return
            if not click.confirm(
                "This DELETES each old collection after its reindex is verified. "
                "Proceed?",
                default=False,
            ):
                click.echo("Skipped. Run `nx init` again any time to migrate.")
                return
        for s in clean:
            _run_migration(db, s, allow_sourceless_loss=False)

    # Mixed collections lose their manual notes on migration. Never under
    # --yes (we cannot auto-confirm a lossy delete); interactive only, with
    # a dedicated confirmation that names the loss.
    for s in mixed:
        if assume_yes:
            click.echo(
                f"\n{s.name}: {s.sourceless} manual note(s) cannot be "
                f"re-embedded — skipped to avoid silent loss. Re-run `nx init` "
                f"interactively (without --yes) to migrate it and accept the "
                f"note loss, or export the notes first.",
                err=True,
            )
            continue
        if click.confirm(
            f"\n{s.name} has {s.sourceless} manual note(s) that will be "
            f"PERMANENTLY LOST (only file-backed chunks can be re-embedded). "
            f"Migrate and accept that loss?",
            default=False,
        ):
            _run_migration(db, s, allow_sourceless_loss=True)
        else:
            click.echo(f"Skipped {s.name}.")


def _run_migration(db, stale, *, allow_sourceless_loss: bool) -> None:
    """Run one migration and report the outcome (RDR-144 P4)."""
    from nexus.db.embed_migrate import migrate_collection_safe  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    click.echo(f"\nReindexing {stale.name} -> {stale.target_name} …")
    outcome = migrate_collection_safe(
        db, stale, dry_run=False, allow_sourceless_loss=allow_sourceless_loss
    )
    if outcome.status == "migrated":
        click.echo(
            f"  Done: {outcome.after} chunks in {stale.target_name}; "
            f"old collection removed."
        )
    else:
        click.echo(f"  {outcome.status.upper()}: {outcome.reason}", err=True)


# ── P3 (B) model pre-fetch warmup, offline-safe ───────────────────────────────


def _warmup_bge() -> None:
    """Pre-fetch the bge-768 model by running one warmup embed through the P1
    chokepoint (lands in the stable XDG cache, not $TMPDIR).

    Wrapped so an offline / cache-miss never crashes or wedges first search:
    fastembed logs an error and returns None on a failed download, which would
    otherwise None-deref (CA-1 Refinement B). Convert any failure into an
    actionable message naming the cache path.
    """
    from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    click.echo(f"\nFetching bge-768 ({_BGE_DOWNLOAD_HINT}) — one-time download …")
    try:
        ef = LocalEmbeddingFunction(model_name=_TIER1_MODEL)
        vectors = ef(["warmup"])
        if not vectors:
            raise RuntimeError("embedder returned no vectors")
        click.echo("Done — bge-768 is cached and ready.")
    except Exception as exc:  # noqa: BLE001 — any failure must stay actionable
        cache = _config.fastembed_cache_dir()
        _log.warning("bge_warmup_failed", error=str(exc), cache=str(cache))
        click.echo(
            f"Could not fetch the bge-768 model (offline or download failed): {exc}\n"
            f"It will be retried automatically on your next local search/index.\n"
            f"Cache location: {cache}",
            err=True,
        )


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
      (no silent fallback), mirroring :func:`_warmup_bge`.
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
        # Fail loud AND fatal: unlike the Python fastembed path (_warmup_bge),
        # which auto-fetches on first use, the Java service has NO retry — it
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


@click.command("init")
@click.option(
    "--embedder",
    type=click.Choice(["bge-768", "minilm-384"]),
    default=None,
    help="Select the local embedder non-interactively (skips the prompt).",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Accept the recommended default (bge-768 for local) without prompting.",
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
    """Guided first-run setup: choose your local embedding model.

    In cloud mode there is no local model to provision — embeddings run
    server-side via Voyage. In local mode this records your embedder choice
    so subsequent indexing/search uses it. The model itself is fetched later
    (``nx init`` does not download or install anything in this phase).

    Pass ``--service`` to also provision the local Postgres cluster required
    by the RDR-152 Java service backend.
    """
    # Postgres provisioning: run if --service flag passed, or if the global
    # NX_STORAGE_BACKEND is already set to 'service' (operator re-running
    # init to refresh a provisioned cluster).
    _auto_service = (
        not provision_service
        and "service" in os.environ.get("NX_STORAGE_BACKEND", "").lower()
    )
    if provision_service or _auto_service:
        from nexus.daemon.storage_service_daemon import StorageServiceStartError  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        try:
            lease = provision_and_start_service(embedder)
        except StorageServiceStartError:
            # No native binary available and none acquirable. PG is provisioned
            # and _ensure_service_binary_step already printed an actionable
            # install instruction; do NOT start (the legacy JVM path is expunged,
            # RDR-161 P3 — starting without a binary fails loud, CRE C1). Exit
            # non-zero so the incomplete setup is not mistaken for serving.
            click.echo(
                "\nService NOT started: no native binary available. Install one "
                "(see above), then re-run `nx init --service` to finish.",
                err=True,
            )
            raise SystemExit(1)
        if lease is None:
            # Cloud mode + service backend: embeddings run server-side via Voyage,
            # no local service to start.
            return
        # RDR-157 P4.1: one command left the user with a running backend. The
        # interactive local-embedder prompt below is for the non-service Python
        # (fastembed) path only.
        return

    # Import-site call so tests can patch ``nexus.config.is_local_mode``
    # (mem:feedback_pin_local_mode_in_cloud_tests).
    if not _config.is_local_mode():
        click.echo("Nexus is configured for CLOUD mode (Voyage embeddings).")
        click.echo("Embeddings run server-side — there is no local model to provision.")
        click.echo("Manage cloud credentials with `nx config init`.")
        return

    click.echo("Nexus local mode — choose your on-device embedding model.\n")
    click.echo(
        "  bge-768     BAAI/bge-base-en-v1.5 (768-dim) — RECOMMENDED. Materially"
    )
    click.echo(
        f"              better local search quality. One-time {_BGE_DOWNLOAD_HINT} "
        "model download on first use."
    )
    click.echo(
        "  minilm-384  all-MiniLM-L6-v2 (384-dim) — bundled, instant, lower quality.\n"
    )

    choice = embedder
    if choice is None:
        choice = (
            "bge-768"
            if assume_yes
            else click.prompt(
                "Embedder",
                type=click.Choice(["bge-768", "minilm-384"]),
                default="bge-768",
            )
        )

    model = _CHOICE_TO_MODEL[choice]
    set_config_value(_EMBED_MODEL_KEY, model)
    click.echo(f"\nSaved: {_EMBED_MODEL_KEY} = {model}")

    if choice != "bge-768":
        # minilm-384 is bundled — nothing to fetch or install.
        return

    # bge-768: provision the extra + model. If fastembed is already importable
    # in THIS process, warmup now. Otherwise add the extra (a fresh venv the
    # running process can't import from) and let first-embed fetch the model.
    if _local_extra_installed():
        _warmup_bge()
        # bge is now the active embedder in this process; any pre-existing
        # 384-dim collections are now stale and silently unsearchable.
        # Offer the gate-locked safe migration (P4 / CA-3).
        _offer_stale_migration(assume_yes)
    else:
        _ensure_local_extra()
