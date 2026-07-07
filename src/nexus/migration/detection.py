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

from nexus.corpus import (
    embedding_model_for_collection_name,
    voyage_model_for_collection,
)
from nexus.migration.vector_etl import _VOYAGE_MODELS, _dim_for_collection

_log = structlog.get_logger(__name__)

#: The ONNX model token the SERVICE wires in every deployment mode (local +
#: cloud). RDR-160 swapped this MiniLM-384 → bge-768; it mirrors the service's
#: ``Bge768Embedder.modelToken()`` / ``EmbedderRouter`` local key, NOT the CLI's
#: selectable local embedder (a user may still index minilm-384 locally, but the
#: service cannot serve it — such collections are UNSUPPORTED until re-indexed).
_ONNX_MODEL: str = "bge-base-en-v15-768"

#: The voyage models (``_VOYAGE_MODELS``) are imported from ``vector_etl`` (single
#: source) so this module's billing decision and vector_etl's passthrough
#: eligibility can never silently diverge on a new Voyage model (review).

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


def cross_model_target_model(source: str, *, voyage_key_present: bool) -> str:
    """The embedding model a cross-model remap should re-embed *source* into.

    nexus-gilf2: the target must be a model the live deployment actually WIRES,
    else the upsert hits the ``nexus-pebfx.2`` fail-loud guard (HTTP 422, no
    embedder for the named model) and the migration blocks 0/N. The old
    unconditional bge-768 target was correct for local mode but wrong for the
    MIXED migrant — a deployment that ran local (minilm-384 collections) then
    migrates onto a voyage-mode (cloud) service, where bge-768 is not wired.

    Mode + content-type aware:

    * cloud mode (``voyage_key_present``) → the content-type-appropriate voyage
      model (prose ``docs__`` / ``rdr__`` / ``knowledge__`` → ``voyage-context-3``;
      ``code__`` and any other prefix → ``voyage-code-3``), so the re-embedded
      chunks land under a served model;
    * local mode → ONNX (``bge-base-en-v15-768``), the only wired embedder.

    The content-type dispatch reuses the READ-side
    :func:`nexus.corpus.voyage_model_for_collection`. The remap only swaps the
    model segment, leaving the content_type prefix intact, so the target's
    served model equals what the read path will later dispatch for it — no
    model/recall mismatch (RDR-059).
    """
    if voyage_key_present:
        return voyage_model_for_collection(source)
    return _ONNX_MODEL


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
    #: Whether the deployment wires the voyage embedders (cloud mode). Carried
    #: so the dry-run preview can resolve the cross-model re-embed TARGET and its
    #: throughput the same way the driver will (nexus-gilf2): voyage models in
    #: cloud mode, bge-768 in local. Defaults False (local) for the many test
    #: doubles that construct a report without a live mode signal.
    voyage_key_present: bool = False

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
    report = DetectionReport(
        classifications=tuple(classifications),
        voyage_key_present=voyage_key_present,
    )
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
    from nexus.config import get_credential  # noqa: PLC0415 — circular-dep avoidance (nexus.config)

    return bool(get_credential("voyage_api_key").strip())


def resolve_default_local_leg() -> Path:
    """Resolve the default local-Chroma path for the migration read leg.

    nexus-id750 (GH #1381): this detector historically defaulted to
    ``<config>/chroma`` (``~/.config/nexus/chroma``) — a directory the
    PRODUCT has never written local Chroma to. The real store has always
    lived at :func:`nexus.config._default_local_path`
    (``NX_LOCAL_CHROMA_PATH`` → ``$XDG_DATA_HOME/nexus/chroma`` →
    ``~/.local/share/nexus/chroma``; identical as far back as v5.4.5). So a
    bare ``nx guided-upgrade`` opened an empty directory, saw no footprint,
    and no-opped with "you are already on the service stack" while the
    user's real vectors sat at the XDG path.

    Resolution: the product path wins when it exists; the old config-dir
    location is retained as a secondary probe (defensive — nothing is known
    to write there, but any manually-placed data should not become invisible
    the other way); when neither exists, return the product path so the
    fresh-user no-op message points at the honest default.
    """
    from nexus.config import _default_local_path, nexus_config_dir  # noqa: PLC0415 — circular-dep avoidance (nexus.config)

    product = _default_local_path()
    if product.exists():
        return product
    legacy = nexus_config_dir() / "chroma"
    if legacy.exists():
        return legacy
    return product


def open_read_legs(
    local_path: str | Path | None = None,
) -> tuple[Any | None, Any | None]:
    """Open whichever Chroma read legs are present, returning ``(local, cloud)``.

    A missing local store (default: :func:`resolve_default_local_leg` — the
    product's own env-aware local-Chroma path, nexus-id750) or an
    unconfigured cloud leg yields ``None`` for that leg — a fresh user with
    neither leg gets ``(None, None)``, which :func:`classify_collections`
    treats as a clean no-op. Only the "absent leg" sentinels
    (``FileNotFoundError`` / the cloud half-configured ``RuntimeError``) are
    swallowed; any other failure (a corrupt store) propagates loud.
    """
    from nexus.migration.chroma_read import (  # noqa: PLC0415 — circular-dep avoidance (nexus.migration.chroma_read)
        open_cloud_read_client,
        open_local_read_client,
    )

    path = Path(local_path) if local_path else resolve_default_local_leg()
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


def close_read_client(client: Any | None) -> None:
    """Close a Chroma read leg, swallowing absence/teardown failures.

    The canonical leg-teardown primitive shared by the detection consumers
    (:func:`classify_collections` callers and the guided-upgrade pre-flight):
    a ``None`` leg or a client that exposes no callable ``close`` is a no-op,
    and a close that raises is logged at DEBUG, never propagated — teardown
    must not mask the detection result it follows.
    """
    if client is None:
        return
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort
        _log.debug("migration_read_client_close_failed", error=str(exc))


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

#: Coarse Voyage re-embed price (USD per 1M tokens), for the cost guardrail
#: (nexus-cewad / RDR-166 Gap 4). Order-of-magnitude planning figure across the
#: voyage-context-3 / voyage-code-3 family — NOT a billing-accurate quote; the
#: operator's actual invoice is set by Voyage's current pricing. Billed only for
#: a cross-model→voyage RE-EMBED. Same-model voyage migrations use vector
#: passthrough (nexus-hxry2) — stored vectors are copied, not re-embedded, so
#: they do not bill. ONNX/bge re-embeds run locally and never bill.
_VOYAGE_COST_USD_PER_1M_TOKENS: float = 0.12


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
    #: CROSS-MODEL re-embed (legacy minilm, etc.) rather than block. It counts
    #: toward the migratable totals; ``support`` stays ``unsupported`` (its
    #: current name is unservable) but it is NOT in ``DryRunPreview.unsupported``.
    cross_model: bool = False
    #: nexus-gilf2: the model the cross-model re-embed targets, resolved from the
    #: deployment mode + content_type (voyage models in cloud, bge-768 in local).
    #: ``None`` for non-cross-model groups. Carried so the preview names the
    #: ACTUAL target (not a hard-coded bge-768) and estimates the right rate.
    target_model: str | None = None


@dataclass(frozen=True)
class DryRunPreview:
    """A read-only preview of what a migration would move (touches NO data)."""

    groups: tuple[ModelGroup, ...]
    unsupported: tuple[CollectionClassification, ...]
    legs_with_data: frozenset[str]
    migratable_chunks: int
    total_est_tokens: int
    est_seconds: float
    #: nexus-cewad: token volume that will be RE-EMBEDDED through a Voyage model
    #: and therefore billed to the operator key — counts cross-model→voyage
    #: re-embed groups only. Same-model voyage migrations use vector passthrough
    #: (nexus-hxry2: stored vectors copied, not re-embedded) and contribute zero,
    #: as do ONNX/bge re-embeds (local).
    billed_voyage_tokens: int = 0
    #: Coarse USD estimate for ``billed_voyage_tokens`` at
    #: :data:`_VOYAGE_COST_USD_PER_1M_TOKENS`. ``0.0`` when nothing is billed.
    est_voyage_cost_usd: float = 0.0
    #: nexus-hxry2: same-model voyage token volume that migrates FREE via vector
    #: passthrough (stored vectors copied, not re-embedded). It is NOT billed —
    #: but the dry-run can't see per-chunk vector presence, and any source chunk
    #: lacking a stored vector re-embeds that batch (and bills). Surfaced as a
    #: caveat so the ``$0`` estimate is honest about that fallback (review).
    passthrough_voyage_tokens: int = 0


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
    # RDR-162 P2: a cross-model-remappable collection is migratable (re-embed),
    # not blocked. nexus-gilf2: bucket cross-model groups by their RESOLVED
    # target too — in cloud mode a single source model (minilm-384) splits into
    # voyage-code-3 (code__) and voyage-context-3 (prose) targets, so one bucket
    # would otherwise name only one of them. ``target`` is None for non-cross-
    # model classifications, keeping supported/blocked buckets unchanged.
    def _cross_target(c: CollectionClassification) -> str | None:
        if not cross_model_remappable(c):
            return None
        return cross_model_target_model(
            c.collection, voyage_key_present=report.voyage_key_present
        )

    buckets: dict[
        tuple[Leg, str | None, Support, str | None], list[CollectionClassification]
    ] = {}
    for c in report.classifications:
        buckets.setdefault((c.leg, c.model, c.support, _cross_target(c)), []).append(c)

    groups: list[ModelGroup] = []
    migratable_chunks = 0
    total_est_tokens = 0
    est_seconds = 0.0
    billed_voyage_tokens = 0
    passthrough_voyage_tokens = 0
    for (leg, model, support, target_model), members in buckets.items():
        chunk_count = sum(m.source_count for m in members)
        is_cross_model = target_model is not None
        if support == "unsupported" and not is_cross_model:
            # Genuinely blocked (voyage-no-key, non-conformant) — zero estimate.
            groups.append(
                ModelGroup(leg, model, support, len(members), chunk_count, 0, 0.0)
            )
            continue
        # Supported byte-for-byte OR cross-model re-embed: both migratable. The
        # cross-model re-embed runs through whatever embedder serves the TARGET
        # name (voyage in cloud mode, ONNX in local) — estimate that rate.
        tokens = chunk_count * _EST_TOKENS_PER_CHUNK
        if is_cross_model:
            rate = (
                _EST_VOYAGE_CHUNKS_PER_SEC
                if target_model in _VOYAGE_MODELS
                else _EST_ONNX_CHUNKS_PER_SEC
            )
        else:
            rate = _throughput_for_support(support)
        seconds = chunk_count / rate
        groups.append(
            ModelGroup(
                leg, model, support, len(members), chunk_count, tokens, seconds,
                cross_model=is_cross_model,
                target_model=target_model,
            )
        )
        migratable_chunks += chunk_count
        total_est_tokens += tokens
        est_seconds += seconds
        # Billed iff the migration RE-EMBEDS through a Voyage model:
        #   * cross-model→voyage → re-embedded with the target voyage model → BILLED.
        #   * same-model voyage (supported-voyage-1024) → vector PASSTHROUGH
        #     (nexus-hxry2): the stored vectors are copied verbatim, NOT
        #     re-embedded, so it no longer bills. (Best case: a same-model chunk
        #     missing its source vector falls back to a billed re-embed per batch.)
        #   * supported-onnx / cross-model→bge → local ONNX (bge-768) → free.
        billed = is_cross_model and target_model in _VOYAGE_MODELS
        if billed:
            billed_voyage_tokens += tokens
        elif not is_cross_model and support == "supported-voyage-1024":
            # Same-model voyage → vector passthrough (free), barring missing-vector
            # batch fallback. Tracked so the estimate can caveat the $0 (review).
            passthrough_voyage_tokens += tokens

    # Stable order: leg, support, model, then target — deterministic preview text.
    groups.sort(key=lambda g: (g.leg, g.support, g.model or "", g.target_model or ""))
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
        billed_voyage_tokens=billed_voyage_tokens,
        est_voyage_cost_usd=billed_voyage_tokens / 1_000_000 * _VOYAGE_COST_USD_PER_1M_TOKENS,
        passthrough_voyage_tokens=passthrough_voyage_tokens,
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
                f"{g.model} -> {g.target_model} cross-model re-embed"
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
    if preview.billed_voyage_tokens > 0:
        lines.append(
            f"Voyage re-embed cost (billed to the operator key): "
            f"~{preview.billed_voyage_tokens:,} tokens, est. "
            f"~${preview.est_voyage_cost_usd:.2f} "
            f"(~${_VOYAGE_COST_USD_PER_1M_TOKENS:.2f}/1M tokens; a re-run re-embeds "
            "at full cost — not deduplicated)."
        )
    if preview.passthrough_voyage_tokens > 0:
        lines.append(
            f"Voyage passthrough (free): ~{preview.passthrough_voyage_tokens:,} "
            "tokens migrate same-model by copying stored vectors — no re-embed, "
            "no charge. Caveat: any source chunk missing its stored vector "
            "re-embeds that batch (and bills); the estimate assumes all vectors "
            "are present."
        )
    lines.append(
        "Estimates are coarse planning figures, not a binding commitment; the "
        "live run reports exact counts."
    )
    return "\n".join(lines)


def render_cost_confirmation(preview: DryRunPreview) -> str | None:
    """Operator-facing cost warning for a billed Voyage re-embed, or ``None``.

    Returns ``None`` when the migration bills nothing (no cross-model→voyage
    re-embed) — the caller then proceeds without a cost prompt. When there IS a
    billed re-embed, returns a warning that surfaces (a) the coarse USD estimate
    and the token volume it is based on, and (b) the re-run-at-full-cost
    foot-gun (nexus-1sx01): this guardrail WARNS and CONFIRMS; it does NOT
    deduplicate or avoid re-billing a repeated run (copy-not-move has no
    server-side cost memory). Pure formatting — no I/O.
    """
    if preview.billed_voyage_tokens <= 0:
        return None
    return (
        f"WARNING: this migration will RE-EMBED ~{preview.billed_voyage_tokens:,} tokens "
        f"through a Voyage model, billed to the operator's Voyage key — an "
        f"estimated ${preview.est_voyage_cost_usd:.2f} (coarse planning figure at "
        f"~${_VOYAGE_COST_USD_PER_1M_TOKENS:.2f}/1M tokens; the real invoice "
        f"follows Voyage's current pricing).\n"
        f"   Re-running this migration re-embeds the same chunks again at full "
        f"cost — the copy carries no server-side cost memory, so a repeat run is "
        f"billed in full, not deduplicated."
    )
