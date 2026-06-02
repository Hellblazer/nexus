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
    from nexus.db.embed_migrate import (
        detect_stale_local_collections,
        migrate_collection_safe,
    )
    from nexus.db.local_ef import _MODEL_DIMS, _TIER1_MODEL, local_model_token

    active_token = local_model_token()
    active_dim = _MODEL_DIMS.get(_TIER1_MODEL, 768)

    try:
        from nexus.commands.store import _t3

        db = _t3()
        stale = detect_stale_local_collections(
            db, active_dim=active_dim, active_token=active_token
        )
    except Exception as exc:  # noqa: BLE001 — detection must never block init
        _log.debug("stale_detection_failed", error=str(exc))
        return

    if not stale:
        return

    reindexable = [s for s in stale if s.kind == "reindexable"]
    deferred = [s for s in stale if s.kind != "reindexable"]

    click.echo(
        f"\nFound {len(stale)} collection(s) indexed with a different "
        f"embedder than bge-768 — search returns nothing for these:"
    )
    for s in stale:
        if s.kind == "reindexable":
            click.echo(
                f"  • {s.name} ({s.count} chunks) -> reindex into {s.target_name}"
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

    if not reindexable:
        click.echo(
            "\nNothing can be migrated automatically — see the manual steps above."
        )
        return

    # Double-confirm: the old collections are DELETED after a verified
    # reindex. Make the destructive step explicit and ask twice.
    if not assume_yes:
        if not click.confirm(
            f"\nReindex {len(reindexable)} collection(s) into bge-768 now?",
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

    for s in reindexable:
        click.echo(f"\nReindexing {s.name} -> {s.target_name} …")
        outcome = migrate_collection_safe(db, s, dry_run=False)
        if outcome.status == "migrated":
            click.echo(
                f"  Done: {outcome.after} chunks in {s.target_name}; "
                f"old collection removed."
            )
        else:
            click.echo(
                f"  {outcome.status.upper()}: {outcome.reason}", err=True
            )


# ── P3 (B) model pre-fetch warmup, offline-safe ───────────────────────────────


def _warmup_bge() -> None:
    """Pre-fetch the bge-768 model by running one warmup embed through the P1
    chokepoint (lands in the stable XDG cache, not $TMPDIR).

    Wrapped so an offline / cache-miss never crashes or wedges first search:
    fastembed logs an error and returns None on a failed download, which would
    otherwise None-deref (CA-1 Refinement B). Convert any failure into an
    actionable message naming the cache path.
    """
    from nexus.db.local_ef import LocalEmbeddingFunction

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
def init_cmd(embedder: str | None, assume_yes: bool) -> None:
    """Guided first-run setup: choose your local embedding model.

    In cloud mode there is no local model to provision — embeddings run
    server-side via Voyage. In local mode this records your embedder choice
    so subsequent indexing/search uses it. The model itself is fetched later
    (``nx init`` does not download or install anything in this phase).
    """
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
