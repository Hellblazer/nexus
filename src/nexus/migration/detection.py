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
/ ``open_cloud_read_client``). It touches ``list_collections()``,
``Collection.count()``, and — for data-bearing collections that are EITHER
name-unsupported OR voyage-named-but-only-supported-because-a-local-key-is-
present (nexus-x7t5y: the latter closes the case where a pre-RDR-109
mislabeled bge-768 collection carries a voyage-conformant name and a key is
configured, so the name alone would otherwise short-circuit past the probe
and get billed-re-embedded as genuine voyage) — a best-effort one-vector
``Collection.get(include=["embeddings"])`` dim probe (nexus-nb7hr); it moves
NO data. A store that cannot be enumerated or a collection whose COUNT
cannot be probed is a LOUD error, never a silent skip — a dropped collection
is a silent half-migration. The dim probe alone is best-effort: its failure
degrades that collection to name-based classification (the pre-nb7hr
behavior), logged, never aborting detection.
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
#: The local-ONNX embedding dimension — the measured-dim bucket that makes a
#: name-unsupported collection remappable without credentials (nexus-nb7hr).
_ONNX_DIM: int = 768


def _probe_actual_dim(col: Any) -> int | None:
    """Measure the stored embedding dimension from ONE real vector.

    ``col.get(limit=1, include=["embeddings"])`` — the same primitive
    ``collection_audit.sample_live_distances`` uses, with the SAME
    best-effort error contract (review: both existing users of this call —
    collection_audit and doctor — catch and degrade; a vector-embedding
    fetch is heavier and more failure-prone than ``count()``, e.g. a
    cloud key scoped for list/count but not raw-vector export, or a
    transient blip). A probe failure returns ``None`` — the name-based
    classification stands (the pre-nb7hr behavior for that collection) —
    and logs a warning; it must never abort the whole detection.
    """
    try:
        sample = col.get(limit=1, include=["embeddings"])
    except Exception as exc:  # noqa: BLE001 — best-effort probe; degrade to name-based classification
        _log.warning(
            "migration_dim_probe_failed",
            collection=getattr(col, "name", "?"),
            error=str(exc),
        )
        return None
    embeddings = sample.get("embeddings") if isinstance(sample, dict) else None
    if embeddings is None or len(embeddings) == 0:
        return None
    first = embeddings[0]
    return len(first) if first is not None else None


def _probe_legacy_ids(col: Any) -> str | None:
    """Sample the FIRST page of chunk ids for a truly-legacy id.

    RDR-180 (nexus-jxizy.3): the CONFORMANT widths are now a SET — 32-char
    era ids (pre-flip sources, re-derived to the full digest on the wire by
    ``wire_reid``) AND 64-char full digests (the canonical identity, and
    what post-flip sources carry). Truly legacy = everything else.

    GH #1390 root cause (nexus-sot7v): pre-RDR-108-era Chroma stores hold
    16/18-char chunk ids; the pgvector chash identity is the full
    ``sha256(chunk_text)`` and the migration NEVER guesses ids
    (rewriting would sever the catalog-manifest chash join, which carries
    the same legacy values but no text to recompute from, and would break
    ``rollback_collections``' source-chash-set matching). Such a collection
    would otherwise 409 on every upsert MID-ETL — the wall that pushed an
    autonomous session into dropping the chash constraints. Detecting it at
    classification time blocks it at the pre-gate with the re-index
    diagnostic instead: before any write, remediation on screen.

    First-page sampling (quota-compliant 300) can miss bad ids on later
    pages; the ETL carries a per-batch hard guard
    (:func:`vector_etl._nonconformant_id`) as the complete backstop, so a
    miss here degrades UX (fails at ETL time instead of the pre-gate),
    never correctness. Probe failure returns ``None`` (the name-based
    classification stands) — same best-effort contract as
    :func:`_probe_actual_dim`.
    """
    try:
        sample = col.get(limit=300, include=[])
    except Exception as exc:  # noqa: BLE001 — best-effort probe; the ETL per-batch guard is the backstop
        _log.warning(
            "migration_legacy_id_probe_failed",
            collection=getattr(col, "name", "?"),
            error=str(exc),
        )
        return None
    ids = sample.get("ids") if isinstance(sample, dict) else None
    for chunk_id in ids or []:
        if len(chunk_id) not in (32, 64):
            return chunk_id
    return None


def _probe_id_conformance(col: Any) -> tuple[str | None, bool]:
    """One-pass id conformance probe: ``(truly_legacy_example, era32_present)``.

    nexus-i5rbk (Hal decision 2026-07-21, era-hop): the 32-hex era ids
    (pre-RDR-180-flip pgvector half-digests) are a DISTINCT axis from
    truly-legacy ids — they are exactly re-derivable on the wire
    (``sha256(chunk_text)``, a recompute, not a guess), so they feed
    ``needs_reid`` without widening ``legacy_ids`` (whose consumers carry
    census display and collision_audit historical-map semantics) and
    without flipping model support. Same sampling contract as
    :func:`_probe_legacy_ids` (first page, quota-compliant 300; the ETL
    seam guard is the complete backstop; probe failure returns clean).
    """
    try:
        sample = col.get(limit=300, include=[])
    except Exception as exc:  # noqa: BLE001 — best-effort probe; the ETL per-batch guard is the backstop
        _log.warning(
            "migration_id_conformance_probe_failed",
            collection=getattr(col, "name", "?"),
            error=str(exc),
        )
        return None, False
    ids = sample.get("ids") if isinstance(sample, dict) else None
    truly_legacy: str | None = None
    era32 = False
    for chunk_id in ids or []:
        if len(chunk_id) == 32:
            era32 = True
        elif len(chunk_id) != 64 and truly_legacy is None:
            truly_legacy = chunk_id
    return truly_legacy, era32
    return None


#: RDR-180 land-then-transform (nexus-jxizy.10.8): the width era a chunk id
#: sample can land in. The legacy-id BLOCK that used to fire on a non-32/64
#: id (see the now-retired check in ``pregate.assert_models_supported``) is
#: repurposed under land-then-transform into LANDING-MANIFEST input — the
#: sequencer's landing phase and the rehearsal's non-vacuity asserts need to
#: know WHICH width era a data-bearing collection carries, not whether to
#: block it (chunk_text is rehashed server-side regardless of source width).
WidthEra = Literal["canonical-64", "legacy-32", "legacy-16", "mixed"]


def _id_width_era(chunk_id: str) -> WidthEra:
    """Bucket one chunk id by length.

    64 = the canonical full ``sha256(chunk_text)`` digest (RDR-180); 32 =
    the pre-flip / RDR-108-era width; anything else (typically 16-char) is
    the pre-RDR-108 legacy population land-then-transform now migrates by
    rehash (GH #1408) instead of blocking.
    """
    n = len(chunk_id)
    if n == 64:
        return "canonical-64"
    if n == 32:
        return "legacy-32"
    return "legacy-16"


def probe_width_era(col: Any) -> WidthEra | None:
    """Sample the FIRST page of chunk ids and classify the width era(s) observed.

    Same sampling mechanics as :func:`_probe_legacy_ids` (up to 300 ids,
    ``include=[]``) and the same best-effort failure contract: a probe
    failure or an empty sample returns ``None`` (unknown — the caller
    degrades gracefully; this must never abort detection). ``"mixed"`` when
    the sample spans more than one width era.

    Exposed (not underscore-prefixed, unlike the probe helpers above) so the
    pre-gate's landing-manifest builder
    (:func:`nexus.migration.pregate.landing_width_manifest`) and the
    sequencer's landing phase can call it directly against a live-opened
    collection — RDR-180 design Q4 item 3, nexus-jxizy.10.8.
    """
    try:
        sample = col.get(limit=300, include=[])
    except Exception as exc:  # noqa: BLE001 — best-effort probe; never abort detection
        _log.warning(
            "migration_width_era_probe_failed",
            collection=getattr(col, "name", "?"),
            error=str(exc),
        )
        return None
    ids = sample.get("ids") if isinstance(sample, dict) else None
    if not ids:
        return None
    eras = {_id_width_era(i) for i in ids}
    if len(eras) > 1:
        return "mixed"
    return eras.pop()


def probe_has_text(col: Any) -> bool | None:
    """Sample the FIRST page of chunk documents for ANY non-blank text.

    RDR-180 land-then-transform derives the canonical chash by rehashing
    ``chunk_text`` server-side (nexus_rdr/180-land-transform-design Q3 step
    1) — a collection whose sampled chunks carry NO text at all has nothing
    to rehash from and stays genuinely blocked at the pre-gate
    (:func:`nexus.migration.pregate.assert_models_supported`'s ``no_text``
    check), unlike the retired legacy-id width block (nexus-jxizy.10.8).
    Same best-effort sampling mechanics as :func:`_probe_legacy_ids` /
    :func:`probe_width_era` (up to 300 rows), with an honest three-way
    result:

    * ``True`` — at least one sampled chunk has non-blank text;
    * ``False`` — every sampled chunk's document is empty/blank (the
      genuine no-text signal that blocks);
    * ``None`` — the probe could not be run or returned nothing usable
      (unreachable collection, a backend without ``documents`` support) —
      inconclusive, NEVER treated as a positive no-text finding.
    """
    try:
        sample = col.get(limit=300, include=["documents"])
    except Exception as exc:  # noqa: BLE001 — best-effort probe; never abort detection
        _log.warning(
            "migration_text_presence_probe_failed",
            collection=getattr(col, "name", "?"),
            error=str(exc),
        )
        return None
    ids = sample.get("ids") if isinstance(sample, dict) else None
    if not ids:
        return None
    docs = sample.get("documents") if isinstance(sample, dict) else None
    if docs is None:
        return None
    return any((d or "").strip() for d in docs)


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


def is_measured_dim_override(c: "CollectionClassification") -> bool:
    """Whether *c* is a stale pre-RDR-109 mislabel (nexus-nb7hr / nexus-5b9v0).

    True iff *c*'s declared name/model says ``unsupported`` but a stored
    vector MEASURED as local bge/ONNX (768-dim) — i.e. the name lies: the
    content is provably local bge/ONNX despite carrying a voyage token (or no
    model segment at all). This is exactly the override condition
    :func:`cross_model_remappable` checks first; extracted so a caller that
    needs to know WHICH of several classifications is the likely-stale
    mislabel (rather than whether the migration would re-embed it) can reuse
    the identical test instead of re-deriving it and risking drift.
    """
    return c.support == "unsupported" and c.measured_dim == _ONNX_DIM


def cross_model_remappable(
    c: "CollectionClassification", *, rehashes_ids: bool = False
) -> bool:
    """Whether *c* is a legacy collection the cross-model migrate can re-embed.

    ``rehashes_ids`` (nexus-leunq) declares that the CALLER's migration path
    derives chunk ids by rehashing ``chunk_text`` server-side rather than
    copying them verbatim — true under RDR-180 land-then-transform. It relaxes
    only the legacy-id exclusion, which exists solely because a verbatim copy
    would carry a non-conformant id into the target. Defaults to False so
    every existing caller, including the historical reconstruction in
    :mod:`nexus.migration.collision_audit`, keeps its current answer.

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
    if not c.has_data:
        return False
    # GH #1390 / nexus-sot7v: the cross-model remap re-embeds stored TEXT but
    # keeps the chunk ids VERBATIM — a legacy-id collection would violate the
    # chash identity in the remapped target exactly as in a same-name copy.
    #
    # ...on a path that copies ids. Under RDR-180 land-then-transform the ids
    # are REHASHED server-side from chunk_text, so the verbatim-preservation
    # premise does not hold and the exclusion is stale there (nexus-leunq).
    # Opt-in rather than a policy change, for two reasons: paths that still
    # copy ids verbatim must keep the exclusion, and
    # build_cross_model_target_names is ALSO how collision_audit reconstructs
    # HISTORICAL target maps — flipping the default would have it audit maps
    # no past run ever produced, the precise drift its docstring warns about.
    if c.legacy_ids and not rehashes_ids:
        return False
    # nexus-nb7hr measured-dim override: a stored vector PROVED the content
    # is local bge/ONNX (768-dim), so the name-based exclusions below do not
    # apply — a voyage-labeled name is a pre-RDR-109 mislabel (re-embedding
    # bge text into bge is loss-free, unlike genuine voyage content), and a
    # non-conformant name gets a synthesized conformant target
    # (cross_model_target_name).
    if is_measured_dim_override(c):
        return True
    if c.model is None:
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


def remap_target_model(
    c: "CollectionClassification", *, voyage_key_present: bool
) -> str:
    """The target model for a remappable classification (nexus-nb7hr).

    A collection whose vectors MEASURED as local bge (768) targets the ONNX
    model in EVERY mode — re-embedding provably-bge content into a voyage
    model would bill tokens and contradict the classifier's "no Voyage key
    needed" diagnostic (bge is wired in both deployment modes per
    :func:`wired_models`; the deployed engine's /version smoke lists it).
    Everything else keeps the mode-aware :func:`cross_model_target_model`
    policy (nexus-gilf2).
    """
    if c.measured_dim == _ONNX_DIM:
        return _ONNX_MODEL
    return cross_model_target_model(
        c.collection, voyage_key_present=voyage_key_present
    )


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
    #: GROUND-TRUTH embedding dimension, measured from one stored vector
    #: (nexus-nb7hr / GH #1381). ``None`` when the collection is empty or
    #: was not probed (probing happens only for name-unsupported,
    #: data-bearing collections — the case where the name lies matters).
    measured_dim: int | None = None
    #: GH #1390 / nexus-sot7v: the collection holds legacy non-32-char chunk
    #: ids (pre-RDR-108 era). MUST NOT migrate (verbatim ids violate the
    #: chash identity) and MUST NOT cross-model remap (remap re-embeds text
    #: but keeps ids). Blocked at the pre-gate with a re-index diagnostic.
    legacy_ids: bool = False
    #: nexus-i5rbk (Hal decision 2026-07-21): the collection holds 32-hex
    #: ERA ids (the pre-RDR-180-flip pgvector half-digest). A distinct axis
    #: from ``legacy_ids``: re-derivable on the wire (sha256(chunk_text) is
    #: a recompute, not a guess), it feeds the substrate rung's
    #: ``needs_reid`` WITHOUT the census/remap semantics legacy_ids
    #: carries and WITHOUT flipping model support. Post-flip the engine
    #: boundary 400s non-64 ids, so an era-32 leg without the wire
    #: transform can never land (the era-hop's
    #: etl_seam_nonconformant_post_transform failure).
    era32_ids: bool = False


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
    #: nexus-p8nd5 dv708 fold: why the CONFIGURED cloud read leg was skipped
    #: (dead creds / unreachable host — the nexus-dv708 degrade), or ``None``
    #: when the leg was read or was never configured. Lets non-stderr
    #: consumers (dry-run preview JSON, doctor) distinguish
    #: "skipped-unreadable" from "never configured" — previously only a WARN
    #: log carried the distinction.
    cloud_leg_skipped_reason: str | None = None

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
        # nexus-nb7hr (GH #1381, Steve's 5.x install): the name can LIE —
        # pre-RDR-109 writes mislabeled local-ONNX collections voyage-*, and
        # pre-RDR-103 names have no model segment at all. Measure ONE stored
        # vector when the NAME says unsupported and there is data to move;
        # the measurement, not the label, decides (the read-side twin of the
        # RDR-109 write-side honest-naming fix). Probe failure propagates
        # LOUD, same as the count probe above.
        # GH #1390 / nexus-sot7v: truly-legacy (non-32/64-char) chunk ids hard-block the
        # migration BEFORE model classification is even interesting — the ids
        # are the identity the pgvector side keys on, and no migration path
        # (verbatim copy OR cross-model re-embed) rewrites them. Checked for
        # EVERY data-bearing collection (a conformant, supported-model name
        # can still hold legacy-era ids — the canon-chat store did).
        legacy_ids = False
        era32_ids = False
        measured_dim: int | None = None
        if source_count > 0:
            bad_id, era32_ids = _probe_id_conformance(col)
            if bad_id is not None:
                legacy_ids = True
                support = "unsupported"
                reason = (
                    f"collection holds legacy chunk ids (e.g. "
                    f"{bad_id!r}; pre-RDR-108 era) — the pgvector chash "
                    "identity is the full sha256(chunk_text) hexdigest "
                    "(RDR-180) and the migration will not guess ids; "
                    "re-index this collection from its source content "
                    "before migrating. Do NOT drop or weaken the chash "
                    "width constraints to force the upserts through "
                    "(GH #1390)."
                )
        # nexus-x7t5y (GH follow-on to nb7hr): a voyage-NAMED collection with
        # a local NX_VOYAGE_API_KEY present short-circuits classify_model_support
        # to "supported-voyage-1024" BEFORE this point — the plain
        # support == "unsupported" gate below would never see it, so a
        # pre-RDR-109 mislabeled bge-768 collection carrying a voyage-
        # conformant name classified as genuine voyage and would be
        # billed-re-embedded into a voyage collection of the WRONG
        # semantics (the stored vectors were never voyage to begin with).
        # Probe that case too — scoped to voyage-named collections only
        # (not a blanket enable-for-everything) since that is the one
        # shape a key's presence can hide.
        # nexus-leunq: legacy ids used to suppress this probe, which made a
        # single non-conformant id enough to hide what the vectors ARE. A
        # pre-RDR-109 voyage-NAMED collection holding measured-bge 768 vectors
        # plus one 16-char-id row then classified as genuinely-voyage-
        # unsupported, and guided-upgrade blocked the whole run with
        # "Configure voyage on the service (NX_VOYAGE_API_KEY)" — wrong twice
        # over: the content is bge, so configuring voyage would bill a
        # re-embed of never-voyage vectors; and under land-then-transform the
        # ids are rehashed server-side, so they were never the obstacle.
        #
        # Id shape and vector dimension are independent facts. Measuring one
        # has never depended on the other; the coupling was the bug.
        should_probe_dim = source_count > 0 and (
            support == "unsupported"
            or (support == "supported-voyage-1024" and model in _VOYAGE_MODELS)
        )
        if should_probe_dim:
            measured_dim = _probe_actual_dim(col)
            if measured_dim == _ONNX_DIM:
                # The vectors ARE local bge/ONNX regardless of the name or
                # key presence. Force support to "unsupported" — even when
                # it arrived here as "supported-voyage-1024" — so the
                # downstream cross_model_remappable / is_measured_dim_override
                # checks (which both require support == "unsupported")
                # recognize this as a measured-dim override needing the
                # free local-ONNX remap, not a genuine (billed) voyage
                # collection. The measured dim makes it cross-model
                # REMAPPABLE (see cross_model_remappable): migrated by
                # re-embedding stored text under a corrected conformant
                # name — in local mode via the free local ONNX embedder,
                # no credentials, no Voyage cost.
                support = "unsupported"
                name_issue = (
                    f"name claims voyage model '{model}'" if model in _VOYAGE_MODELS
                    else "name is not four-segment conformant"
                )
                reason = (
                    f"{name_issue}, but a stored vector measures "
                    f"{measured_dim}-dim (local bge/ONNX) — auto-remapped at "
                    "migration to a corrected conformant name; no Voyage key "
                    "or re-index needed (nexus-nb7hr / nexus-x7t5y)"
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
                measured_dim=measured_dim,
                legacy_ids=legacy_ids,
                era32_ids=era32_ids,
            )
        )
    return out


def classify_collections(
    *,
    local_client: Any | None = None,
    cloud_client: Any | None = None,
    voyage_key_present: bool,
    cloud_leg_skipped_reason: str | None = None,
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
        cloud_leg_skipped_reason=cloud_leg_skipped_reason,
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
    skipped_out: dict | None = None,
) -> tuple[Any | None, Any | None]:
    """Open whichever Chroma read legs are present, returning ``(local, cloud)``.

    A missing local store (default: :func:`resolve_default_local_leg` — the
    product's own env-aware local-Chroma path, nexus-id750) or an
    unconfigured cloud leg yields ``None`` for that leg — a fresh user with
    neither leg gets ``(None, None)``, which :func:`classify_collections`
    treats as a clean no-op. The "absent leg" sentinels
    (``FileNotFoundError`` / the cloud half-configured ``RuntimeError``)
    are swallowed silently; a CONFIGURED-but-unreadable cloud leg (dead
    creds, unreachable host — nexus-dv708) degrades to absent with a LOUD
    warning instead of crashing detection. Only a LOCAL-leg failure beyond
    file-absence (a corrupt store) still propagates.
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
    except Exception as exc:  # noqa: BLE001 — nexus-dv708: DEAD-cred / transport failures degrade LOUD, never crash detection
        # Critique narrowing: only the OBSERVED dead-leg shapes degrade —
        # chromadb's ChromaError family (revoked key at CloudClient
        # construction) and ValueError (chromadb wraps httpx transport
        # failures). A programming bug (TypeError/AttributeError from a
        # bad refactor) must still crash loud, not re-file itself as an
        # unreadable leg. chromadb import deferred: optional heavy dep.
        try:
            from chromadb.errors import ChromaError  # noqa: PLC0415 — deferred import; optional heavy dep
        except ImportError:  # pragma: no cover — chromadb absent: no cloud leg possible anyway
            ChromaError = ()  # type: ignore[assignment,misc]
        if not isinstance(exc, (ChromaError, ValueError)):
            raise
        # A CONFIGURED cloud leg whose credentials are dead (revoked key →
        # chromadb ChromaError/AuthError at CloudClient construction) or
        # whose host is unreachable (httpx transport wrapped as ValueError
        # by chromadb.api.client) previously propagated out of detect()
        # and hard-failed the whole upgrade walk ("detect raised", rung
        # FAILED, did not converge). The leg is a retired MIGRATION SOURCE
        # — an unreadable one means "this source is unavailable", which is
        # the same detect() verdict as absent, said LOUDLY (the 6.15.0
        # shakeout's dead-VOYAGE-key class). The LOCAL leg deliberately
        # keeps propagating: a corrupt local store is a real error.
        _log.warning(
            "detection.cloud_read_leg_unreadable",
            error=f"{type(exc).__name__}: {exc}",
            guidance="cloud migration-source leg skipped — fix or remove the "
                     "CHROMA_API_KEY/tenant config if this source still matters",
        )
        if skipped_out is not None:
            # dv708 structured residual (nexus-p8nd5): carry the skip reason
            # to DetectionReport so non-stderr consumers see it.
            skipped_out["cloud"] = f"{type(exc).__name__}: {exc}"
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
    #: dv708 structured residual (nexus-p8nd5): a CONFIGURED cloud leg that
    #: was skipped-unreadable (dead creds / unreachable) — the preview must
    #: say so, or "no cloud collections" silently means two different things.
    cloud_leg_skipped_reason: str | None = None


def _throughput_for_support(support: Support) -> float:
    if support == "supported-voyage-1024":
        return _EST_VOYAGE_CHUNKS_PER_SEC
    return _EST_ONNX_CHUNKS_PER_SEC


def build_dry_run_preview(
    report: DetectionReport, *, rehashes_ids: bool = False
) -> DryRunPreview:
    """Roll a :class:`DetectionReport` into a per-leg/per-model dry-run preview.

    Supported groups contribute token-volume + time estimates; unsupported
    collections are surfaced separately (their re-index / key diagnostics) and
    contribute nothing to the migratable totals — they would be BLOCKED, not
    migrated.

    ``rehashes_ids`` must match what the REAL run will do, and is forwarded to
    :func:`cross_model_remappable`. A preview that answers differently from the
    run it previews is worse than no preview: nexus-leunq's first fix threaded
    the flag into the live guided path only, leaving this one at the default,
    so a legacy-id measured-bge collection rendered as
    "BLOCKED ... must be resolved first" directly beside its own reason text
    "auto-remapped at migration ... no Voyage key or re-index needed" — two
    contradictory statements in one line, about a collection the real run
    migrates fine. Worse than cosmetic, because ``migrate_cmd`` exits non-zero
    on ``preview.unsupported``, so a script gating on the dry run blocked on a
    collection that was never actually blocked.
    """
    # RDR-162 P2: a cross-model-remappable collection is migratable (re-embed),
    # not blocked. nexus-gilf2: bucket cross-model groups by their RESOLVED
    # target too — in cloud mode a single source model (minilm-384) splits into
    # voyage-code-3 (code__) and voyage-context-3 (prose) targets, so one bucket
    # would otherwise name only one of them. ``target`` is None for non-cross-
    # model classifications, keeping supported/blocked buckets unchanged.
    def _cross_target(c: CollectionClassification) -> str | None:
        if not cross_model_remappable(c, rehashes_ids=rehashes_ids):
            return None
        return remap_target_model(
            c, voyage_key_present=report.voyage_key_present
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
        c for c in report.unsupported
        if not cross_model_remappable(c, rehashes_ids=rehashes_ids)
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
        cloud_leg_skipped_reason=report.cloud_leg_skipped_reason,
    )


def render_dry_run_preview(preview: DryRunPreview) -> str:
    """Render a :class:`DryRunPreview` as operator-facing text.

    Pure formatting — no I/O — so it is unit-testable and the CLI is a thin
    ``click.echo`` over it.
    """
    lines: list[str] = []
    if preview.cloud_leg_skipped_reason:
        lines.append(
            f"  ⚠ cloud migration-source leg SKIPPED (unreadable, not absent): "
            f"{preview.cloud_leg_skipped_reason} — fix or remove the cloud "
            f"config if that source still matters"
        )
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
