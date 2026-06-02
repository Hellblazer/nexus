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

import click

import nexus.config as _config
from nexus.config import set_config_value
from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL

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

    if choice == "bge-768":
        click.echo(
            f"The bge-768 model ({_BGE_DOWNLOAD_HINT}) downloads automatically to a "
            "stable cache on first local embed, once the [local] extra is installed."
        )
