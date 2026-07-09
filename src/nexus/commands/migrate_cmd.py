# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx migrate-to-service`` — the guided Chroma-to-service upgrade (RDR-159).

The single survivable command that turns the ~8 manual Chroma-to-pgvector
upgrade steps into one guided flow (RDR-159, the load-bearing piece of the
``nexus-luxe6`` release-blocker lift).

``--dry-run`` ships the read-only front half: it classifies the user's Chroma
footprint per collection (source leg x embedding model, model resolved against
the service's wired embedders by deployment mode) and previews what would
migrate — per-leg/per-model counts, unsupported collections flagged for
re-index, and a coarse token/time estimate. It touches NO data.

The full non-dry-run invocation drives the proven P0-P3 engine through
``nexus.migration.driver.run_guided_upgrade``: detect → sequence (quiesce →
pre-gate → T2 → T3-per-leg) → validate (taxonomy floor + counts + manifest
orphans) → unlock on a clean verdict, or leave the sentinel ``migrated-failed``
and offer rollback on a block. This command is a THIN renderer over that engine
— it builds the clients/paths and prints the result; no orchestration logic
lives here (RDR-159 §Components).
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

import click
import structlog

from nexus.migration.detection import (
    build_dry_run_preview,
    classify_collections,
    open_read_legs,
    render_cost_confirmation,
    render_dry_run_preview,
    voyage_key_available,
)

_log = structlog.get_logger(__name__)


def _confirm_voyage_cost(
    preview: Any,
    *,
    assume_yes: bool,
    confirm: Any = None,
) -> bool:
    """Estimate-and-confirm gate for a billed Voyage re-embed (nexus-cewad).

    Returns ``True`` when the caller may proceed with the billed migration,
    ``False`` when the operator declined. A migration that bills nothing
    proceeds silently; ``assume_yes`` proceeds without prompting (the scripted
    / CI path). Otherwise the cost + re-run foot-gun is shown and an interactive
    confirmation gates the call. ``confirm`` is injectable for tests; production
    uses :func:`click.confirm` (which aborts on a non-interactive stream, so a
    billed run without ``--yes`` never proceeds unattended).
    """
    warning = render_cost_confirmation(preview)
    if warning is None:
        return True  # nothing billed — no prompt
    if assume_yes:
        click.echo(warning)
        click.echo("Proceeding (--yes): the operator's Voyage key will be billed.")
        return True
    click.echo(warning)
    _confirm = confirm if confirm is not None else click.confirm
    return bool(_confirm("Proceed with the billed re-embed?"))


@click.command(name="migrate-to-service")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Classify the Chroma footprint and preview the migration without "
    "moving any data.",
)
@click.option(
    "--local-path",
    type=click.Path(),
    default=None,
    help="Override the local Chroma store path "
    "(default: ~/.config/nexus/chroma).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(),
    default=None,
    help="SQLite T2 source (default: NX_DB_PATH or the canonical path).",
)
@click.option(
    "--catalog-db",
    "catalog_db_path",
    type=click.Path(),
    default=None,
    help="SQLite catalog source (default: NX_CATALOG_DB_PATH or the "
    "canonical path).",
)
@click.option(
    "--service-url",
    default=None,
    help="Override the nexus-service base URL (default: the supervisor lease).",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the Voyage re-embed cost confirmation (scripted / non-interactive "
    "runs). The operator's Voyage key is billed without a prompt.",
)
def migrate_to_service_cmd(
    dry_run: bool,
    local_path: str | None,
    db_path: str | None,
    catalog_db_path: str | None,
    service_url: str | None,
    assume_yes: bool,
) -> None:
    """Guided Chroma-to-service upgrade migration (RDR-159).

    --dry-run previews the footprint; the bare invocation runs the full guided
    migration end-to-end (detect, sequence T2 then T3, validate, unlock).
    """
    if dry_run:
        _run_dry_run(local_path)
        return
    _run_migration(
        local_path, db_path, catalog_db_path, service_url, assume_yes=assume_yes
    )


def _run_dry_run(local_path: str | None) -> None:
    local, cloud = open_read_legs(local_path)
    try:
        report = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=voyage_key_available(),
        )
        preview = build_dry_run_preview(report)
        click.echo(render_dry_run_preview(preview))
        if preview.unsupported:
            # Unsupported collections would BLOCK a real run — make the
            # dry-run exit non-zero so a script gates on it (gate S1: never a
            # silent OK).
            sys.exit(1)
    finally:
        for client in (local, cloud):
            _close_quietly(client)


def _run_migration(
    local_path: str | None,
    db_path: str | None,
    catalog_db_path: str | None,
    service_url: str | None,
    *,
    assume_yes: bool = False,
    skip_t2_stores: frozenset[str] = frozenset(),
) -> None:
    """Drive the full guided migration through the nexus engine.

    Builds the live clients/paths, then delegates ALL sequencing + validation
    to ``run_guided_upgrade`` and renders the verdict. A block leaves the
    sentinel ``migrated-failed`` (reads stay degraded-LOUD) and exits non-zero;
    rollback is offered, never auto-invoked (RF-5, copy-not-move).

    ``skip_t2_stores`` (RDR-178 Gap 7, nexus-1sx01): T2 store names the
    caller's already-migrated pre-flight (``guided_upgrade.
    detect_already_migrated``) has confirmed are covered by a clean report
    with no newer local writes. Threaded to ``run_guided_upgrade`` as a
    ``functools.partial(migrate_all, skip_stores=...)`` override for the T2
    step — empty (the default) reproduces the prior unconditional behavior
    exactly.
    """
    from nexus.migration.driver import run_guided_upgrade  # noqa: PLC0415 — heavy migration dep deferred to subcommand scope
    from nexus.migration.orchestrator import EtlSources  # noqa: PLC0415 — heavy migration dep deferred to subcommand scope

    # Process-level for this one-shot CLI invocation; HttpVectorClient +
    # make_catalog_client_for_migration below resolve the endpoint from it.
    # No restore (the process exits); a test harness embedding this command
    # must isolate it with monkeypatch.setenv.
    if service_url:
        os.environ["NX_SERVICE_URL"] = service_url

    sqlite_path = _resolve_db_path(db_path)
    catalog_path = _resolve_catalog_db_path(catalog_db_path)
    for label, path in (("T2", sqlite_path), ("catalog", catalog_path)):
        if not path.exists():
            raise click.ClickException(
                f"SQLite {label} source not found: {path}\n"
                f"Set the env override or pass the --{'db' if label == 'T2' else 'catalog-db'} flag."
            )

    # Pre-flight the service endpoint so an unresolvable service is a clean
    # early error BEFORE the (potentially long) detect+ETL, mirroring
    # `storage migrate vectors`.
    from nexus.db.http_vector_client import _resolve_endpoint, get_http_vector_client  # noqa: PLC0415 — deferred import; http_vector_client only needed in this branch

    try:
        _resolve_endpoint()
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not token:
        raise click.ClickException(
            "NX_SERVICE_TOKEN is required for the guided migration (the T2 "
            "catalog ETL + manifest validation call the service).\n"
            "Set it to the bearer token configured in the nexus-service."
        )

    # COST GUARDRAIL (nexus-cewad) — a cross-model→voyage re-embed is billed to
    # the operator's Voyage key. Estimate it from a read-only classify pass and
    # gate the billed run behind an explicit confirmation. A migration that
    # bills nothing (byte-for-byte copy / local ONNX re-embed) proceeds silently.
    local_read, cloud_read = open_read_legs(local_path)
    try:
        cost_preview = build_dry_run_preview(
            classify_collections(
                local_client=local_read,
                cloud_client=cloud_read,
                voyage_key_present=voyage_key_available(),
            )
        )
    finally:
        for client in (local_read, cloud_read):
            _close_quietly(client)
    if not _confirm_voyage_cost(cost_preview, assume_yes=assume_yes):
        raise click.Abort()

    from nexus.catalog.factory import make_catalog_client_for_migration  # noqa: PLC0415 — deferred import; catalog factory only needed in this branch

    # nexus-b6qlf Fix 1 (CRITICAL): this used to construct a bare
    # HttpVectorClient() directly, bypassing the fail-loud engine-version-
    # floor probe entirely -- exactly the highest-stakes cloud operation
    # (a data migration) for a stale/incompatible engine to matter.
    # get_http_vector_client() runs the same probe every other cloud-mode
    # T3 caller goes through (ManagedServiceError is a RuntimeError, so this
    # reuses the same ClickException-wrapping convention as _resolve_endpoint
    # above).
    try:
        vector_client = get_http_vector_client()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        catalog_client = make_catalog_client_for_migration(
            base_url=service_url, token=token
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    def _on_leg_result(r: Any) -> None:
        line = (
            f"{r.status:<13} {r.collection}: source={r.source_count} "
            f"written={r.written_count} ({r.duration_s:.1f}s)"
        )
        if r.reason:
            line += f" — {r.reason}"
        is_err = r.status in ("failed", "skipped")
        click.echo(line, err=is_err)
        (sys.stderr if is_err else sys.stdout).flush()

    def _on_progress(done: int, total: int) -> None:
        click.echo(f"  progress: {done}/{total} collection(s) migrated")
        sys.stdout.flush()

    run_t2 = None
    if skip_t2_stores:
        from functools import partial  # noqa: PLC0415 — deferred import — avoids import-time cost / circular deps
        from nexus.migration.orchestrator import migrate_all  # noqa: PLC0415 — heavy migration dep deferred to subcommand scope

        run_t2 = partial(migrate_all, skip_stores=skip_t2_stores)

    try:
        result = run_guided_upgrade(
            sources=EtlSources(
                sqlite_path=sqlite_path, catalog_db_path=catalog_path
            ),
            vector_client=vector_client,
            catalog_client=catalog_client,
            t2_db_path=sqlite_path,
            local_path=local_path,
            on_progress=_on_progress,
            on_leg_result=_on_leg_result,
            run_t2=run_t2,
        )
    finally:
        _close_quietly(catalog_client)
        _close_quietly(vector_client)

    _render_result(result)


def _render_result(result: Any) -> None:
    """Render a :class:`GuidedUpgradeResult`; exit non-zero on any block."""
    seq = result.sequence

    # Fresh user: nothing data-bearing, no migration ran.
    if seq.phase == "not-migrating":
        click.echo(
            "No Chroma data detected (no local store, no configured cloud "
            "leg) — nothing to migrate; you are already on the service stack."
        )
        return

    # Sequence block / partial-leg: the sentinel is migrated-failed; T3 did not
    # complete, so there is no validated copy to gate — re-run (idempotent).
    if result.validation is None:
        click.echo("", err=True)
        click.echo(
            f"Migration BLOCKED before completion: {seq.blocked_reason}", err=True
        )
        click.echo(
            "The migration-state sentinel is 'migrated-failed' — reads stay "
            "degraded-LOUD until you re-run (the vector upsert is idempotent) "
            "or clear the state.",
            err=True,
        )
        raise SystemExit(1)

    val = result.validation
    if result.ok:
        click.echo("")
        click.echo(
            f"Migration VERIFIED and unlocked — {seq.collections_done} "
            f"collection(s) migrated and validated; serving from pgvector."
        )
        for note in val.advisory_notes:
            click.echo(f"  advisory: {note}")
        return

    # Validated block: a migrated copy exists but failed validation. Leave it
    # migrated-failed and OFFER rollback (never auto-invoke — copy-not-move
    # keeps Chroma intact, RF-5).
    click.echo("", err=True)
    click.echo("Migration completed the copy but FAILED validation:", err=True)
    for reason in val.blocking_reasons:
        click.echo(f"  - {reason}", err=True)
    for note in val.advisory_notes:
        click.echo(f"  advisory: {note}", err=True)
    if val.rollback_available:
        legs = sorted(result.detection.legs_with_data)
        click.echo("", err=True)
        click.echo(
            "Rollback is available — your Chroma source is untouched "
            "(copy-not-move). To return to a fully-working pre-upgrade state:",
            err=True,
        )
        for leg in legs:
            flag = " --cloud" if leg == "cloud" else ""
            click.echo(f"    nx storage migrate vectors --rollback{flag}", err=True)
    raise SystemExit(1)


def _resolve_db_path(explicit: str | None) -> Path:
    """Resolve the SQLite T2 path: explicit → ``NX_DB_PATH`` → canonical."""
    if explicit is not None:
        return Path(explicit)
    env_path = os.environ.get("NX_DB_PATH", "")
    if env_path:
        return Path(env_path)
    from nexus.config import default_db_path  # noqa: PLC0415 — command-local import (config)

    return default_db_path()


def _resolve_catalog_db_path(explicit: str | None) -> Path:
    """Resolve the SQLite catalog path: explicit → ``NX_CATALOG_DB_PATH`` →
    ``~/.config/nexus/catalog/.catalog.db``."""
    if explicit is not None:
        return Path(explicit)
    env_path = os.environ.get("NX_CATALOG_DB_PATH", "")
    if env_path:
        return Path(env_path)
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — command-local import (config)

    return nexus_config_dir() / "catalog" / ".catalog.db"


def _close_quietly(client: Any | None) -> None:
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()
