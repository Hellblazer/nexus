# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P0 (nexus-ue6g7.2): the per-collection detection classifier.

The guided upgrade engine's first step (RDR-159 §Approach P0) classifies a
user's Chroma footprint per collection along TWO orthogonal axes before any
data moves:

* **source leg** — local ``PersistentClient`` vs ChromaCloud REST;
* **embedding model** — parsed from the conformant collection-name segment
  (``<content_type>__<owner>__<model>__v<n>``), resolved to a *support* class
  against the service's wired embedders.

Support resolution is a PURE function of deployment mode, NOT a live-service
query (plan-audit condition, 2026-06-13): P0 runs PRE-provisioning, so it
cannot reach a running ``EmbedderRouter``. The wired-embedder set is
deterministic and mirrors
``service/.../vectors/EmbedderRouter.java``:

* ONNX (``bge-base-en-v15-768``) is wired in EVERY mode (RDR-160 swapped the
  service's local ONNX embedder MiniLM-384 → bge-768; this classifier mirrors
  the service, not the CLI's selectable local embedder);
* the voyage models (``voyage-code-3`` / ``voyage-context-3`` / ``voyage-3``)
  are wired iff ``NX_VOYAGE_API_KEY`` is present (cloud mode);
* anything else — e.g. a legacy ``minilm-l6-v2-384`` collection, a KNOWN dim in
  ``vector_etl._MODEL_DIMS`` but wired by NO service embedder — is UNSUPPORTED.
  A known dim is NOT a license to migrate: the service would 422-reject its
  upserts, so it must be flagged for re-index pre-migration (gate S1).

The live-``EmbedderRouter`` check at the P1 pre-gate (service up) is a
belt-and-suspenders confirmation, not the P0 source of truth.

The classifier takes already-opened read clients (dependency injection — the
CLI ``--dry-run`` layer wires the real ``chroma_read.open_local_read_client``
/ ``open_cloud_read_client``). It touches only ``list_collections()`` and
``Collection.count()``; it moves NO data. A store that cannot be enumerated or
a collection that cannot be probed is a LOUD error, never a silent skip — a
dropped collection is a silent half-migration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog

from nexus.corpus import embedding_model_for_collection_name
from nexus.migration.vector_etl import _dim_for_collection

_log = structlog.get_logger(__name__)

#: The ONNX model token the SERVICE wires in every deployment mode (local +
#: cloud). RDR-160 swapped this MiniLM-384 → bge-768; it mirrors the service's
#: ``Bge768Embedder.modelToken()`` / ``EmbedderRouter`` local key, NOT the CLI's
#: selectable local embedder (a user may still index minilm-384 locally, but the
#: service cannot serve it — such collections are UNSUPPORTED until re-indexed).
_ONNX_MODEL: str = "bge-base-en-v15-768"

#: The voyage models wired only in cloud mode (``NX_VOYAGE_API_KEY`` present).
#: Mirrors ``EmbedderRouter``'s cloud-mode ``modelEmbedders`` keys.
_VOYAGE_MODELS: frozenset[str] = frozenset(
    {"voyage-code-3", "voyage-context-3", "voyage-3"}
)

Leg = Literal["local", "cloud"]
Support = Literal["supported-onnx", "supported-voyage-1024", "unsupported"]


def wired_models(*, voyage_key_present: bool) -> frozenset[str]:
    """The set of embedding-model tokens the service would wire in this mode.

    Pure function of deployment mode — the P0 source of truth for support
    (no running service required). Local mode wires only ONNX; cloud mode
    additionally wires the voyage models.
    """
    if voyage_key_present:
        return frozenset({_ONNX_MODEL}) | _VOYAGE_MODELS
    return frozenset({_ONNX_MODEL})


def classify_model_support(
    model: str | None,
    *,
    voyage_key_present: bool,
    wired: frozenset[str] | None = None,
) -> tuple[Support, str]:
    """Resolve a model token to a support class + diagnostic reason.

    ``model`` is the token parsed from a conformant collection name, or
    ``None`` when the name is non-conformant. Returns ``(support, reason)``
    where ``reason`` is empty for supported models and a fix-pointing
    diagnostic for unsupported ones:

    * voyage model + no key → point at the cheap fix (add ``NX_VOYAGE_API_KEY``);
    * a model wired by no embedder in any mode (e.g. a legacy minilm-384
      collection) → point at the expensive fix (re-index), distinct from the
      credential diagnostic.

    ``wired`` overrides the wired-model set used for the membership decision.
    P0 (detection) leaves it ``None`` and uses the pure deployment-mode
    :func:`wired_models` (no running service). The RDR-159 P1 pre-gate passes
    the LIVE ``EmbedderRouter`` registry here so the classification is resolved
    against what the service actually wired — belt-and-suspenders over the pure
    function, never a static onnx-vs-voyage assumption.
    """
    if model is None:
        return "unsupported", (
            "collection name is not four-segment conformant "
            "(<content_type>__<owner>__<model>__v<n>) — cannot resolve an "
            "embedding model; re-index under a conformant name"
        )
    if wired is None:
        wired = wired_models(voyage_key_present=voyage_key_present)
    if model == _ONNX_MODEL:
        return "supported-onnx", ""
    if model in wired:
        # The only non-ONNX wired models are the 1024-dim voyage family.
        return "supported-voyage-1024", ""
    if model in _VOYAGE_MODELS:
        # Voyage model, but no key in this deployment → cheap fix: add the key.
        return "unsupported", (
            f"collection uses voyage model '{model}' but no NX_VOYAGE_API_KEY "
            "is configured — add the key (nx config set / export "
            "NX_VOYAGE_API_KEY) and re-run; no re-index needed"
        )
    # In _MODEL_DIMS or not, the service wires no embedder for it → re-index.
    return "unsupported", (
        f"embedding model '{model}' is wired by no service embedder in this "
        "deployment (re-index required) — the service would reject its "
        "upserts; re-index these collections under a supported model"
    )


def cross_model_remappable(c: "CollectionClassification") -> bool:
    """Whether *c* is a legacy collection the cross-model migrate can re-embed.

    RDR-162 P2: a collection is auto-migratable via stored-text re-embed (rather
    than blocked with the re-index diagnostic) iff ALL hold:

    * it is data-bearing (an empty collection has nothing to migrate);
    * its name is four-segment conformant (so the model segment can be remapped
      by :func:`vector_etl.cross_model_target_name`);
    * its model is NOT a voyage model — a voyage collection that is unsupported
      is the credential case (gate C3: "add NX_VOYAGE_API_KEY"), NOT a model
      switch; re-embedding voyage text into bge would silently change recall, so
      it stays blocked;
    * it is currently ``unsupported`` — wired by no service embedder.

    The non-voyage unsupported case is INTENTIONALLY a catch-all, not a
    minilm-384 allow-list: the cross-model migrate re-embeds the STORED chunk
    text, which is model-agnostic, so ANY such collection (minilm-384, or any
    third-party local embedder the service does not wire) can be re-embedded into
    bge-768. This is the truthful upgrade path — re-embed whatever the service
    cannot serve, rather than emit a dead-end re-index diagnostic. Voyage
    collections are the deliberate exception (credential case, not a model
    switch); a supported collection (already bge-768 or a wired voyage model)
    migrates byte-for-byte and is never remapped.

    The decision is policy: the orchestrator builds the ``target_names`` map from
    this predicate and the pre-gate exempts exactly these collections.
    """
    if not c.has_data or c.model is None:
        return False
    if len(c.collection.split("__")) != 4:
        return False
    if c.model in _VOYAGE_MODELS:
        return False
    return c.support == "unsupported"


@dataclass(frozen=True)
class CollectionClassification:
    """Per-collection detection result along both axes (RF-2).

    ``model`` / ``dim`` are ``None`` for a non-conformant name. ``dim`` is the
    pgvector table dimension from ``_MODEL_DIMS`` when the model segment is a
    KNOWN dim — informational only; a known dim is NOT support (a legacy
    minilm-384 collection has ``dim == 384`` yet ``support == "unsupported"``).
    """

    collection: str
    leg: Leg
    model: str | None
    dim: int | None
    support: Support
    source_count: int
    has_data: bool
    reason: str = ""


@dataclass(frozen=True)
class DetectionReport:
    """The classified Chroma footprint across all detected legs."""

    classifications: tuple[CollectionClassification, ...]

    @property
    def legs_with_data(self) -> frozenset[str]:
        """Legs holding at least one non-empty collection.

        Drives the "refuse partial-leg success" sequencing (RDR-159 P2): a
        leg with only empty collections is not a data-bearing leg.
        """
        return frozenset(c.leg for c in self.classifications if c.has_data)

    @property
    def unsupported(self) -> tuple[CollectionClassification, ...]:
        """Collections the pre-gate must BLOCK on (re-index / key diagnostic)."""
        return tuple(c for c in self.classifications if c.support == "unsupported")


def _classify_leg(
    client: Any, leg: Leg, *, voyage_key_present: bool
) -> list[CollectionClassification]:
    """Classify every collection visible to one read client.

    Enumeration or a per-collection probe failure propagates as a LOUD error
    — a silently-skipped collection is a silent half-migration.
    """
    out: list[CollectionClassification] = []
    for col in client.list_collections():
        name = col.name
        # Probe count off the listed Collection object directly — no second
        # get_collection round-trip (an extra read per collection on the
        # cloud leg). A corrupt collection fails loud here, never silently.
        source_count = col.count()
        model = embedding_model_for_collection_name(name)
        dim, _dim_reason = _dim_for_collection(name)
        support, reason = classify_model_support(
            model, voyage_key_present=voyage_key_present
        )
        out.append(
            CollectionClassification(
                collection=name,
                leg=leg,
                model=model,
                dim=dim,
                support=support,
                source_count=source_count,
                has_data=source_count > 0,
                reason=reason,
            )
        )
    return out


def classify_collections(
    *,
    local_client: Any | None = None,
    cloud_client: Any | None = None,
    voyage_key_present: bool,
) -> DetectionReport:
    """Classify the Chroma footprint per collection across both source legs.

    ``local_client`` / ``cloud_client`` are already-opened Chroma read clients
    (or ``None`` when that leg is absent). A fresh user with neither leg yields
    an empty report — a clean no-op the orchestrator treats as
    nothing-to-migrate success.
    """
    classifications: list[CollectionClassification] = []
    if local_client is not None:
        classifications.extend(
            _classify_leg(
                local_client, "local", voyage_key_present=voyage_key_present
            )
        )
    if cloud_client is not None:
        classifications.extend(
            _classify_leg(
                cloud_client, "cloud", voyage_key_present=voyage_key_present
            )
        )
    report = DetectionReport(classifications=tuple(classifications))
    _log.info(
        "migration_detect_classified",
        total=len(report.classifications),
        legs_with_data=sorted(report.legs_with_data),
        unsupported=len(report.unsupported),
        voyage_key_present=voyage_key_present,
    )
    return report


def voyage_key_available() -> bool:
    """Whether the service would wire the voyage embedders in this deployment.

    The deterministic P0 deployment-mode signal (plan-audit condition): the
    service wires voyage iff ``NX_VOYAGE_API_KEY`` is set at launch
    (``Main.java:111``), and the supervisor plumbs that from
    ``VOYAGE_API_KEY`` / config.yml credentials (``Main.java:127-128``). So
    pre-provisioning we predict it from the same chain: the direct
    ``NX_VOYAGE_API_KEY`` env, else the resolved ``voyage_api_key`` credential
    (``VOYAGE_API_KEY`` env or config).
    """
    if os.environ.get("NX_VOYAGE_API_KEY", "").strip():
        return True
    from nexus.config import get_credential  # noqa: PLC0415

    return bool(get_credential("voyage_api_key").strip())


def open_read_legs(
    local_path: str | Path | None = None,
) -> tuple[Any | None, Any | None]:
    """Open whichever Chroma read legs are present, returning ``(local, cloud)``.

    A missing local store (default ``~/.config/nexus/chroma``) or an
    unconfigured cloud leg yields ``None`` for that leg — a fresh user with
    neither leg gets ``(None, None)``, which :func:`classify_collections`
    treats as a clean no-op. Only the "absent leg" sentinels
    (``FileNotFoundError`` / the cloud half-configured ``RuntimeError``) are
    swallowed; any other failure (a corrupt store) propagates loud.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415
    from nexus.migration.chroma_read import (  # noqa: PLC0415
        open_cloud_read_client,
        open_local_read_client,
    )

    path = Path(local_path) if local_path else nexus_config_dir() / "chroma"
    local: Any | None
    try:
        local = open_local_read_client(path)
    except FileNotFoundError:
        local = None

    cloud: Any | None
    try:
        cloud = open_cloud_read_client()
    except RuntimeError:
        # half-configured / unconfigured cloud leg — absent, not an error
        cloud = None

    return local, cloud


# ── Dry-run preview (RDR-159 §Approach P0) ─────────────────────────────────
#
# The estimate is TOKEN-VOLUME + TIME only (decision 2026-06-13): no dollar
# figure is reported (Voyage pricing is not pinned here, and a fabricated
# price is worse than an honest token count). All figures are ROUGH planning
# estimates, labelled as such — the binding counts come from the live ETL.

#: Coarse average tokens per chunk for the token-volume estimate. Chunking
#: targets a few hundred tokens; 512 is a deliberately conservative round
#: figure for a re-embed planning estimate, NOT a measured per-corpus value.
_EST_TOKENS_PER_CHUNK: int = 512

#: Coarse re-embed throughput (chunks/sec) per path, for the time estimate.
#: Voyage is network/batch-bound; ONNX is local-CPU-bound. Order-of-magnitude
#: planning figures only.
_EST_VOYAGE_CHUNKS_PER_SEC: float = 200.0
_EST_ONNX_CHUNKS_PER_SEC: float = 100.0


@dataclass(frozen=True)
class ModelGroup:
    """A dry-run rollup of one ``(leg, model, support)`` bucket."""

    leg: Leg
    model: str | None
    support: Support
    collection_count: int
    chunk_count: int
    #: Token volume / time are estimated ONLY for migratable groups (supported
    #: OR cross-model re-embed); genuinely-blocked groups contribute zero.
    est_tokens: int
    est_seconds: float
    #: RDR-162 P2: True when this is an ``unsupported`` group the migrate will
    #: CROSS-MODEL re-embed into bge-768 (legacy minilm, etc.) rather than block.
    #: It counts toward the migratable totals; ``support`` stays ``unsupported``
    #: (its current name is unservable) but it is NOT in ``DryRunPreview.unsupported``.
    cross_model: bool = False


@dataclass(frozen=True)
class DryRunPreview:
    """A read-only preview of what a migration would move (touches NO data)."""

    groups: tuple[ModelGroup, ...]
    unsupported: tuple[CollectionClassification, ...]
    legs_with_data: frozenset[str]
    migratable_chunks: int
    total_est_tokens: int
    est_seconds: float


def _throughput_for_support(support: Support) -> float:
    if support == "supported-voyage-1024":
        return _EST_VOYAGE_CHUNKS_PER_SEC
    return _EST_ONNX_CHUNKS_PER_SEC


def build_dry_run_preview(report: DetectionReport) -> DryRunPreview:
    """Roll a :class:`DetectionReport` into a per-leg/per-model dry-run preview.

    Supported groups contribute token-volume + time estimates; unsupported
    collections are surfaced separately (their re-index / key diagnostics) and
    contribute nothing to the migratable totals — they would be BLOCKED, not
    migrated.
    """
    buckets: dict[tuple[Leg, str | None, Support], list[CollectionClassification]] = {}
    for c in report.classifications:
        buckets.setdefault((c.leg, c.model, c.support), []).append(c)

    # RDR-162 P2: a cross-model-remappable collection is migratable (re-embed to
    # bge-768), not blocked. Decide per classification so a bucket is consistent.
    remappable = {
        id(c) for c in report.classifications if cross_model_remappable(c)
    }

    groups: list[ModelGroup] = []
    migratable_chunks = 0
    total_est_tokens = 0
    est_seconds = 0.0
    for (leg, model, support), members in buckets.items():
        chunk_count = sum(m.source_count for m in members)
        is_cross_model = support == "unsupported" and all(
            id(m) in remappable for m in members
        )
        if support == "unsupported" and not is_cross_model:
            # Genuinely blocked (voyage-no-key, non-conformant) — zero estimate.
            groups.append(
                ModelGroup(leg, model, support, len(members), chunk_count, 0, 0.0)
            )
            continue
        # Supported byte-for-byte OR cross-model re-embed: both migratable. The
        # cross-model re-embed runs through the local ONNX (bge-768) path.
        tokens = chunk_count * _EST_TOKENS_PER_CHUNK
        rate = _EST_ONNX_CHUNKS_PER_SEC if is_cross_model else _throughput_for_support(support)
        seconds = chunk_count / rate
        groups.append(
            ModelGroup(
                leg, model, support, len(members), chunk_count, tokens, seconds,
                cross_model=is_cross_model,
            )
        )
        migratable_chunks += chunk_count
        total_est_tokens += tokens
        est_seconds += seconds

    # Stable order: leg, then support, then model — deterministic preview text.
    groups.sort(key=lambda g: (g.leg, g.support, g.model or ""))
    # Only GENUINELY-blocked collections remain in unsupported — cross-model
    # collections are migratable and must not gate the dry-run exit.
    blocked = tuple(
        c for c in report.unsupported if not cross_model_remappable(c)
    )
    return DryRunPreview(
        groups=tuple(groups),
        unsupported=blocked,
        legs_with_data=report.legs_with_data,
        migratable_chunks=migratable_chunks,
        total_est_tokens=total_est_tokens,
        est_seconds=round(est_seconds, 1),
    )


def render_dry_run_preview(preview: DryRunPreview) -> str:
    """Render a :class:`DryRunPreview` as operator-facing text.

    Pure formatting — no I/O — so it is unit-testable and the CLI is a thin
    ``click.echo`` over it.
    """
    lines: list[str] = []
    lines.append("Chroma -> service migration — DRY RUN (no data will be moved)")
    lines.append("")
    if not preview.groups:
        lines.append(
            "No Chroma data detected (no local store, no configured cloud "
            "leg) — nothing to migrate."
        )
        return "\n".join(lines)

    migratable = [
        g for g in preview.groups if g.support != "unsupported" or g.cross_model
    ]
    if migratable:
        lines.append("Would migrate (per leg / model):")
        for g in migratable:
            kind = (
                f"{g.model} -> bge-768 cross-model re-embed"
                if g.cross_model
                else f"{g.model} ({g.support})"
            )
            lines.append(
                f"  [{g.leg}] {kind}: "
                f"{g.collection_count} collection(s), {g.chunk_count} chunk(s), "
                f"~{g.est_tokens:,} tokens, ~{g.est_seconds:.1f}s"
            )
    else:
        lines.append(
            "Would migrate: nothing — every detected collection is blocked "
            "(see below)."
        )

    if preview.unsupported:
        lines.append("")
        lines.append(
            f"BLOCKED — {len(preview.unsupported)} unsupported collection(s) "
            "(NOT migrated; must be resolved first):"
        )
        for c in preview.unsupported:
            lines.append(
                f"  [{c.leg}] {c.collection} ({c.source_count} chunk(s)): "
                f"{c.reason}"
            )

    lines.append("")
    lines.append(
        f"Totals (rough estimate): {preview.migratable_chunks} migratable "
        f"chunk(s) across legs {sorted(preview.legs_with_data) or '(none)'}; "
        f"~{preview.total_est_tokens:,} tokens; ~{preview.est_seconds:.1f}s "
        "re-embed time."
    )
    lines.append(
        "Estimates are coarse planning figures, not a binding commitment; the "
        "live run reports exact counts."
    )
    return "\n".join(lines)
