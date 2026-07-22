# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx init — guided first-run onboarding (RDR-144, collapsed by RDR-174).

``nx init`` is a single mode-detecting command (RDR-174 §1/§3). It resolves
LOCAL vs MANAGED via :func:`_resolve_init_mode` and either provisions and
starts the local service stack (Postgres + the bge-768 Java service) or runs
the reused RDR-166 managed-onboarding wizard + service probe. It is distinct
from the credentials-only wizard ``nx config init``.

This module DOES perform network and install work on the LOCAL path (PG
provisioning, native-binary acquisition, bge-768 ONNX download) — the RDR-144
non-service embedder picker it once hosted was removed in RDR-174 P1.3 (no
non-service local T3 path survives RDR-155/158).
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

#: Config key the active local embedder is persisted under. A real, live seam:
#: ``config.local_embed_model_choice()`` reads it and feeds the doctor advisory
#: (health.py), the MCP first-run advisory, and ``local_ef`` model selection.
#: ``_provision_service_embedder_step`` stamps it at provisioning time so those
#: consumers agree with what the service actually runs — bge-768 is the only
#: valid value in the service-stack topology (RDR-160); the RDR-144 multi-choice
#: picker that once varied it was removed in RDR-174 P1.3.
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

    _provision_service_crossencoder_step()


def _provision_service_crossencoder_step() -> None:
    """Fetch the ms-marco cross-encoder ONNX the engine's rerank stage reads.

    RDR-188 P1.3: local service installs rerank server-side with the ms-marco
    cross-encoder (the engine's ``CrossEncoderReranker`` lazy-loads it from the
    Java-read path). Deliberately NON-fatal, unlike the bge fetch above: the
    engine boots and serves without this file — the fused rerank stage degrades
    LOUD per request (``rerank_degraded=true``) until it exists, and the engine
    picks it up on the next rerank without a restart. Failure here is surfaced
    loudly (stderr + doctor keeps flagging it) but must not abort an otherwise
    good install.
    """
    from nexus.db.service_crossencoder_model import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        SERVICE_CROSSENCODER_DOWNLOAD_HINT,
        fetch_service_crossencoder_onnx,
    )

    click.echo(
        f"\nProvisioning the ms-marco cross-encoder the service reranks with "
        f"({SERVICE_CROSSENCODER_DOWNLOAD_HINT}) — one-time download …"
    )
    try:
        dest = fetch_service_crossencoder_onnx()
        click.echo(f"Done — service cross-encoder ready at {dest}.")
    except Exception as exc:  # noqa: BLE001 — must stay actionable
        _log.warning("service_crossencoder_provision_failed", error=str(exc))
        click.echo(
            f"\nWARNING: {exc}\n(The service still runs; server-side rerank stays "
            f"degraded — loudly — until the model is provisioned. `nx doctor` "
            f"tracks it.)",
            err=True,
        )


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


def _report_stray_t2_launchagent_cleanup(config_dir: Path) -> None:
    """nexus-c0vby (GH #1405 defect 2): once ``nx init --service`` confirms
    the storage service is genuinely serving, remove any stray
    com.nexus.t2 LaunchAgent left over from before the box switched to
    service mode — its ``KeepAlive=true`` would otherwise respawn an
    immediately-exiting process every ~10s forever. Thin CLI wrapper
    around :func:`nexus.upgrade_finish.unload_stale_t2_launchagent` (the
    SAME never-raising leg the automatic post-upgrade finish pass and
    ``nx daemon restart-stale`` already run) — echoes its action lines,
    silent on the common case (nothing to clean up).
    """
    from nexus.upgrade_finish import unload_stale_t2_launchagent  # noqa: PLC0415 — deferred local import — CLI startup cost

    try:
        for line in unload_stale_t2_launchagent(config_dir):
            click.echo(f"  {line}")
    except Exception as exc:  # noqa: BLE001 — this step must never break `nx init`'s success path
        click.echo(f"  T2 LaunchAgent cleanup failed ({exc}) — skipping this step.", err=True)


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
    _report_stray_t2_launchagent_cleanup(nexus_config_dir())
    click.echo("  nx init --service complete — the service backend is serving.")
    return lease


# ── P2.4: autostart decision + decide-first start (RDR-174 / RDR-175 Gap 3) ───


def _stdin_is_interactive() -> bool:
    """True when stdin is a TTY (so a prompt can be shown). Factored for tests."""
    import sys  # noqa: PLC0415 — trivially cheap; kept local so the seam is patchable

    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):  # detached / closed stdin
        return False


def _decide_autostart(assume_yes: bool, no_autostart: bool) -> bool:
    """Decide whether to register the service autostart unit — BEFORE any
    supervisor starts (RDR-175 Gap 3 decide-first).

    Precedence: an explicit ``--no-autostart`` always wins (explicit decline).
    ``--yes`` accepts non-interactively. Otherwise prompt only when interactive
    (default yes). CONSENT GATE: a non-interactive run with no flag declines —
    a system unit is NEVER written without an explicit yes / ``--yes``.
    """
    if no_autostart:
        return False
    if assume_yes:
        return True
    if not _stdin_is_interactive():
        click.echo(
            "[non-interactive: skipping service autostart registration — "
            "re-run with --yes to register it, or `nx daemon service install "
            "--autostart` later]",
            err=True,
        )
        return False
    return click.confirm(
        "Register the storage service to start automatically at login/boot?",
        default=True,
    )


def _poll_service_lease(config_dir: Path, *, timeout: float = 60.0):  # noqa: ANN201 — returns LeaseRecord | None (avoid import cycle)
    """Poll the storage-service registry for a fresh lease, up to ``timeout``s.

    The autostart unit's supervisor (``nx daemon service start --foreground``)
    publishes the lease once /health is green; ``nx init`` polls for it rather
    than starting a second supervisor (decide-first; no parallel arbiter —
    RDR-149 owns arbitration).
    """
    import time  # noqa: PLC0415 — deferred local import

    from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred local import — CLI startup cost

    registry = ServiceRegistry(dir=config_dir, tier="storage_service")
    scope = str(os.getuid())  # POSIX-only; service mode is Linux/macOS
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = registry.discover(scope)
        if rec is not None:
            return rec
        time.sleep(0.5)
    return None


def _provision_and_autostart_service(embedder: str | None):  # noqa: ANN201 — returns LeaseRecord | None
    """Decide-first autostart path: provision, install the unit as the SOLE
    starter, then poll the lease it publishes. Never starts a session supervisor
    underneath the unit (RDR-175 Gap 3). Self-reporting (prints its own outcome).

    On ``ActivationError`` (headless / no session bus — `install_autostart`
    cannot activate the unit) nothing was started, so falling back to a session
    supervisor is coexistence-safe and leaves the user with a serving backend.
    """
    from nexus.daemon import installer  # noqa: PLC0415 — deferred local import — CLI startup cost

    if not provision_service_stack(embedder):
        # Cloud-internal (e.g. --service with NX_LOCAL=0): PG provisioned, no
        # local service to start. Mirror the plain-path cloud message.
        click.echo(
            "\nPostgres is provisioned, but no local service was started: this "
            "host resolves to cloud/remote serving (is_local_mode is false). Set "
            "NX_LOCAL=1 to force a local service, or configure a managed endpoint "
            "with `nx config init`."
        )
        return None

    config_dir = _config.nexus_config_dir()
    try:
        result = installer.install_autostart(tier="service")
    except installer.ActivationError as exc:
        # Headless / no session bus: the unit could not be ACTIVATED, so nothing
        # was started — falling back to a session supervisor is coexistence-safe
        # and leaves the user with a serving backend. (Installation is idempotent
        # — PG and the binary are not re-provisioned on a later retry.)
        click.echo(
            f"\nCould not activate the autostart unit ({exc}); starting the "
            "service for this session instead. Re-run `nx daemon service install "
            "--autostart` once a login session bus is available to persist it "
            "(idempotent — PG and the binary are not re-provisioned).",
            err=True,
        )
        return _start_service_step()
    except installer.InstallerError as exc:
        # SymlinkRefusedError / ContentDiffersError: a real install conflict (e.g.
        # a pre-existing unit with different content). Do NOT session-fall-back
        # (the user must resolve the conflict); fail loud + actionable.
        click.echo(
            f"\nCould not install the autostart unit ({exc}). Re-run "
            "`nx daemon service install --autostart --force` to overwrite, or "
            "`nx init --no-autostart` to start a session supervisor instead.",
            err=True,
        )
        raise SystemExit(1)

    # Idempotent re-run (substantive-critic SIG-2): when the unit was ALREADY
    # present we must not present it as freshly registered, and a not-yet-ready
    # lease is NOT a failure — the OS unit owns bring-up. Only a FRESH install
    # that never serves is an error.
    already_present = result.status is installer.InstallStatus.ALREADY_PRESENT
    click.echo(
        "\nStorage service autostart unit already registered."
        if already_present
        else f"\nRegistered the storage service autostart unit ({result.dest})."
    )

    lease = _poll_service_lease(config_dir)
    if lease is None:
        if already_present:
            # The unit was already there; the service simply has not (re)published
            # a lease yet (e.g. a re-run right after reboot while it activates).
            # Nothing for init to fix — exit 0; the OS unit brings it up.
            click.echo(
                "  The service has not published a lease yet; the autostart unit "
                "will bring it up. Check `nx daemon service status`.",
                err=True,
            )
            return None
        # FRESH install that never served: NOT confirmed serving. Exit non-zero
        # (parity with the no-binary path) so automation chaining init with later
        # steps does not proceed against an unavailable backend.
        click.echo(
            "\nAutostart unit installed, but the service did not become ready in "
            "time — NOT confirmed serving. The OS will keep retrying; check "
            "`nx daemon service status` and <config_dir>/logs/storage_service.log.",
            err=True,
        )
        raise SystemExit(1)

    ep = lease.endpoint
    click.echo(
        f"  Service running on {ep.get('host')}:{ep.get('port')} "
        f"(generation {lease.generation}) via the autostart unit."
    )
    _report_stray_t2_launchagent_cleanup(config_dir)
    click.echo(
        "  nx init complete — the service backend is serving and will restart "
        "at login/boot."
    )
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
    or ``None`` when no bundle archive is on disk — the caller
    (:func:`_provision_postgres_step`) then downloads it from the pinned tag
    via :func:`_acquire_pg_bundle_step` (GH #1381: always-install, no host-PG
    fallback).

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


def _acquire_pg_bundle_step(config_dir: Path) -> Path:
    """Download + verify + select the signed PG bundle from the pinned tag.

    GH #1381 / nexus-yv5m4: nexus ALWAYS provisions from its own signed,
    self-contained ``nexus-pg-<platform>.txz`` (pgvector baked in) — host
    PostgreSQL is never probed or silently used, so there is no Homebrew /
    build-pgvector-from-source dead end and no environment-dependent
    behavior. The bundle comes through the same verified seam as
    ``nx daemon service install-binary`` (sha256 + keyless Sigstore). The
    only override is an explicit ``NEXUS_PG_BIN`` (checked by the caller).

    Returns the selected ``bin/`` dir. Fails LOUD (``SystemExit``) when no
    tag is pinned or the download/verification/extraction fails — there is
    deliberately no fallback.
    """
    from importlib.metadata import PackageNotFoundError, version as _pkg_version  # noqa: PLC0415 — deferred to keep CLI startup fast

    from nexus.daemon.binary_install import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        BinaryVerificationError,
        install_pg_bundle,
        resolve_service_tag,
    )

    tag = resolve_service_tag()
    if not tag:
        click.echo(
            "\nNo engine-service tag is pinned for this build, so the bundled "
            "PostgreSQL cannot be acquired.\n"
            "Either set NEXUS_SERVICE_TAG=engine-service-vX.Y.Z, pre-stage the "
            "bundle with `nx daemon service install-binary <tag>`, or point "
            "NEXUS_PG_BIN at a pgvector-capable PostgreSQL bin/ directory.",
            err=True,
        )
        raise SystemExit(1)
    try:
        _nx_version = _pkg_version("conexus")
    except PackageNotFoundError:
        _nx_version = "unknown"

    click.echo(f"  Fetching the bundled PostgreSQL ({tag}) …")
    try:
        install_pg_bundle(tag, config_dir, installed_by=f"conexus {_nx_version}")
    except BinaryVerificationError as exc:
        _log.error("pg_bundle_acquire_failed", tag=tag, error=str(exc))
        click.echo(
            f"\nPG bundle download failed: {exc}\n"
            "Check connectivity and re-run, or pre-stage it with "
            f"`nx daemon service install-binary {tag}`.",
            err=True,
        )
        raise SystemExit(1)
    try:
        bin_dir = _select_bundled_pg(config_dir)
    except Exception as exc:  # noqa: BLE001 — user-facing, must stay actionable
        # A verified download that fails to EXTRACT (disk full, permissions,
        # corrupt-on-disk) must not escape as a traceback (code-review High).
        _log.error("pg_bundle_extract_failed", tag=tag, error=str(exc))
        click.echo(f"\nDownloaded PG bundle could not be extracted: {exc}", err=True)
        raise SystemExit(1)
    if bin_dir is None:  # defensive: archive just placed, selection must resolve
        click.echo(
            "\nPG bundle was downloaded but could not be located for "
            "extraction — re-run `nx init`, or pre-stage it with "
            f"`nx daemon service install-binary {tag}`.",
            err=True,
        )
        raise SystemExit(1)
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

    # nexus always provisions from its own signed PG bundle (GH #1381 /
    # nexus-yv5m4): extract one already on disk, otherwise download it from
    # the pinned tag. Host PostgreSQL is never probed or silently used — the
    # only override is an explicit NEXUS_PG_BIN (a deliberate, even broken,
    # override must surface, never be downloaded over). One exception keeps
    # existing installs untouched: a cluster data directory that already
    # exists (serving OR stopped) skips acquisition entirely — it keeps
    # whatever PostgreSQL created it (never pg_ctl-start an existing pgdata
    # with freshly downloaded, possibly different-major binaries).
    try:
        bundle_bin = _select_bundled_pg(config_dir)
    except Exception as exc:  # noqa: BLE001 — user-facing, must stay actionable
        _log.error("pg_bundle_extract_failed", error=str(exc))
        click.echo(f"\nBundled PostgreSQL extraction failed: {exc}", err=True)
        raise SystemExit(1)

    if bundle_bin is not None:
        click.echo("  Using bundled PostgreSQL (extracted on first run).")
    elif not os.environ.get("NEXUS_PG_BIN", "").strip():
        from nexus.db.pg_provision import existing_cluster_present  # noqa: PLC0415 — deferred import — heavy/optional dep loaded only when provisioning runs

        if not existing_cluster_present(config_dir):
            _acquire_pg_bundle_step(config_dir)  # fails LOUD; no fallback
            click.echo("  Using bundled PostgreSQL (downloaded + verified).")

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
    if not provision_service_stack(embedder):
        return None
    return _start_service_step()


def provision_service_stack(embedder: str | None = None) -> bool:
    """Provision the local service stack WITHOUT starting a supervisor.

    Runs the provision steps shared by every local-serving path: Postgres, the
    bge-768 service embedder (RDR-160), and the native binary (RDR-161). Returns
    ``True`` when the host is local-serving (caller should start a supervisor —
    either a session detach or an autostart unit), ``False`` in cloud-internal
    mode (Postgres provisioned, embeddings run server-side, nothing to start).

    Split out for RDR-174 P2.4 / RDR-175 Gap 3 decide-first ordering: ``nx init``
    provisions first, decides autostart, THEN chooses the single starter — so a
    session supervisor is never started underneath a unit.

    Raises :class:`StorageServiceStartError` when no native binary is available.
    """
    from nexus.daemon.storage_service_daemon import StorageServiceStartError  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    # RDR-188 P3.1 fold (reviewer Critical, T2 [21057]): resolve the mode
    # BEFORE provisioning — _provision_postgres_step() writes pg_credentials,
    # which is itself a positive local signal to is_local_mode(); deciding
    # after the write would let the just-written file flip the answer within
    # this very call and defeat the cloud-internal early return below.
    local = _config.is_local_mode()
    _provision_postgres_step()
    if not local:
        return False
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
    return True


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


def _managed_onboarding(ctx: click.Context) -> None:
    """RDR-174 P1.2 managed-path onboarding: ensure RDR-166 creds, then probe.

    Reuses (does not re-implement) the RDR-166 managed-onboarding wizard
    (``nx config init``) and the ``nx service probe`` core
    (``probe_managed_service`` — the click ``probe`` command is itself a thin
    wrapper over it). The managed path NEVER provisions locally: it returns on
    probe success and fails loud (SystemExit 1, actionable remedy) on failure.
    """
    from nexus.commands.config_cmd import config_init  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.db.managed_endpoint import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        ManagedServiceError,
        probe_managed_service,
        resolve_managed_endpoint,
    )

    # Ensure both credentials are set; prompt via the RDR-166 wizard only when
    # one is missing (the wizard itself skips env-provided values).
    if not (
        _config.get_credential("service_url")
        and _config.get_credential("service_token")
    ):
        ctx.invoke(config_init)

    # Fail loud if the wizard left service_url unset (the user entered nothing).
    # Without this guard resolve_managed_endpoint silently falls back to
    # DEFAULT_MANAGED_SERVICE_URL and we would probe — and green-light — the
    # public default endpoint the user never configured (no silent fallback for
    # a correctness-critical value; substantive-critic SIG-1).
    missing = [
        name
        for name in ("service_url", "service_token")
        if not _config.get_credential(name)
    ]
    if missing:
        click.echo(
            f"\nManaged setup incomplete: {', '.join(missing)} not set. Re-run "
            "`nx config init` and enter the missing value(s), then re-run "
            "`nx init`.",
            err=True,
        )
        raise SystemExit(1)

    base = resolve_managed_endpoint(require_token=False)[0]
    try:
        caps = probe_managed_service(base_url=base)
    except ManagedServiceError as exc:
        # We call the probe CORE (the same function the `nx service probe`
        # command wraps) rather than ctx.invoke(probe) so we can attach an
        # init-specific remedy — intentionally more actionable than probe's bare
        # ClickException (the error-format divergence is deliberate).
        click.echo(f"\nManaged service probe FAILED: {exc}", err=True)
        click.echo(
            "Fix the endpoint/token (`nx config init`) or check connectivity, "
            "then re-run `nx init`.",
            err=True,
        )
        raise SystemExit(1)
    # Success output mirrors `nx service probe`'s fields to avoid drift.
    click.echo(f"\n✓ Managed nexus service reachable: {caps.base_url}")
    click.echo(
        f"  release_version: {caps.release_version}  "
        f"app_version: {caps.app_version}"
    )
    click.echo(f"  embedding_mode:  {caps.embedding_mode}")
    if caps.embedding_models:
        click.echo(f"  models:          {', '.join(caps.embedding_models)}")
    click.echo("Indexing/search route to the managed service — no local stack to start.")


def _converge_ladder_best_effort() -> None:
    """Finish first-run setup with a converged upgrade ladder (nexus-9xfx5).

    A virgin install otherwise boots with a pending chash-rekey rung
    (vacuously convergeable on empty stores) and the diag conformance view
    missing until the first ``nx upgrade`` — so a fresh box's very first
    ``nx doctor`` looked broken. Runs the ladder walk (`_run_ladder`) —
    NOT the full ``nx upgrade`` (no ``_quiesce_daemon`` / ``_run_upgrade``
    / ``_converge_preconditions``; those are no-ops or not-applicable on
    the just-provisioned service-mode box this runs against, per
    reviewer-3modes trace — this is not a general upgrade substitute).
    Best-effort:
    init already provisioned a serving backend, so a convergence failure
    points at ``nx upgrade`` (idempotent) instead of failing init.
    """
    try:
        from nexus.commands.upgrade import _run_ladder  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        _run_ladder(dry_run=False, auto_mode=False)
    except Exception as exc:  # noqa: BLE001 — provisioning succeeded; convergence is retryable via `nx upgrade`, so surface-and-continue beats failing a fresh init
        click.echo(
            f"  Upgrade-ladder convergence deferred ({exc}); run `nx upgrade` "
            "to converge.",
            err=True,
        )


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
    help="Accept the service-autostart registration non-interactively (local mode).",
)
@click.option(
    "--no-autostart",
    "no_autostart",
    is_flag=True,
    default=False,
    help="Do not register the service autostart unit; start a session "
    "supervisor only (local mode). Takes precedence over --yes.",
)
@click.option(
    "--service",
    "provision_service",
    is_flag=True,
    default=False,
    help=(
        "[DEPRECATED — plain `nx init` now does this by default] Provision the "
        "local Postgres cluster for the RDR-152 Java service backend. Creates "
        "nexus_admin and nexus_svc roles and writes credentials to "
        "~/.config/nexus/pg_credentials. Idempotent: safe to re-run."
    ),
)
@click.pass_context
def init_cmd(
    ctx: click.Context,
    embedder: str | None,
    assume_yes: bool,
    no_autostart: bool,
    provision_service: bool,
) -> None:
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
    # nexus-gynt2: stranded-install refusal, FIRST — before any provisioning.
    # Disarmed (constant-check no-op) on every migration-capable release; at
    # N+1 an init on a box carrying unmigrated pre-PG data must refuse with
    # the two-hop redirect rather than provision a fresh empty install
    # beside it (indistinguishable from data loss). Deliberately NOT wrapped
    # in try/except (unlike the CLI/MCP banner sites, which are advisory and
    # fail open): a detector crash here must abort init loudly rather than
    # fall through into provisioning — that fall-through IS the failure mode
    # this guard exists to prevent.
    stranded = _config.detect_stranded_install_default()
    if stranded is not None:
        click.echo(f"Refusing to initialize: {stranded.message}", err=True)
        ctx.exit(1)

    # RDR-174 P2.4: ``--yes`` now accepts the service-autostart registration
    # non-interactively (the embedder picker that previously made it a no-op was
    # removed in P1.3). ``--no-autostart`` declines. The decision is made
    # decide-first in the local block below (RDR-175 Gap 3).

    # RDR-174 P3.1 (§Approach 5): ``--service`` is deprecated — plain ``nx init``
    # now drives the same local provision path. The flag stays wired (back-compat
    # for docs, muscle memory, and direct callers) but emits a notice. Fired here,
    # before dispatch, so it surfaces on every path the flag forces into.
    if provision_service:
        click.echo(
            "Note: `nx init --service` is deprecated — plain `nx init` now "
            "provisions the local service backend by default. The flag still "
            "works but will be removed in a future release.",
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
    #
    # ⚠ CROSS-PHASE INVARIANT (autostart): BOTH no-provision exits below — the
    # managed arm and the cloud arm (mode=="local" yet is_local_mode() False) —
    # must remain reachable WITHOUT ever starting a local service or registering
    # an autostart unit. The autostart decision (P2.4) is made decide-first at
    # the TOP of the LOCAL block below, BEFORE any supervisor is started or
    # provisioning runs (RDR-175 Gap 3 — never start a session supervisor under a
    # unit; do NOT move the `_decide_autostart` call later). Do not collapse this
    # guard to `_resolve_init_mode()`-only — that would route cloud-Voyage users
    # into provisioning.
    mode = _resolve_init_mode()
    if not provision_service and (mode == "managed" or not _config.is_local_mode()):
        # Data plane served remotely — nothing local to provision. The old
        # cloud-mode early return is FOLDED here (not orphaned). P1.2
        # (nexus-r2auz) replaces the MANAGED arm with the RDR-166 credential
        # wizard + ``nx service`` probe.
        if mode == "managed":
            # MANAGED: ensure RDR-166 creds (reused wizard) + probe the remote
            # service, then STOP. _managed_onboarding exits non-zero on probe
            # failure and never reaches local provisioning.
            _managed_onboarding(ctx)
        else:
            click.echo("Nexus is configured for CLOUD mode (Voyage embeddings).")
            click.echo(
                "Embeddings run server-side — there is no local model to provision."
            )
            click.echo("Manage cloud credentials with `nx config init`.")
        return

    # LOCAL (or explicit ``--service``): provision the stack, then DECIDE-FIRST
    # (RDR-174 P2.4 / RDR-175 Gap 3) — decide autostart BEFORE starting any
    # supervisor, so a session supervisor is never started underneath a unit.
    #   autostart=yes → install the unit as the SOLE starter + poll its lease.
    #   autostart=no  → provision_and_start_service (session detach), as before
    #                   (the SAME body guided_upgrade invokes — unchanged).
    from nexus.daemon.storage_service_daemon import StorageServiceStartError  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    autostart = _decide_autostart(assume_yes, no_autostart)
    try:
        if autostart:
            # Self-reporting; handles cloud-internal, activation-error fallback,
            # lease-timeout, and success messaging internally.
            lease = _provision_and_autostart_service(embedder)
            if lease is not None:
                _converge_ladder_best_effort()
            return
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
        # Cloud-internal (is_local_mode False — e.g. ``--service`` with
        # NX_LOCAL=0): Postgres was provisioned but no local service started.
        # Say so rather than exit silently (CRE LOW-2).
        click.echo(
            "\nPostgres is provisioned, but no local service was started: this "
            "host resolves to cloud/remote serving (is_local_mode is false). Set "
            "NX_LOCAL=1 to force a local service, or configure a managed endpoint "
            "with `nx config init`."
        )
        return
    # nexus-9xfx5: converge the ladder now that the backend is serving, so the
    # first `nx doctor` on a fresh box is clean (no vacuous pending rung, no
    # missing diag view).
    _converge_ladder_best_effort()
    # RDR-157 P4.1 / RDR-174 §3: one command left the user with a running backend.
    return
