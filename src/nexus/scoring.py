# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure scoring primitives: normalization, hybrid scoring, reranking, interleaving."""
from __future__ import annotations

from typing import Any

import structlog

from nexus.types import SearchResult

_log = structlog.get_logger()

_EPSILON = 1e-9
_FILE_SIZE_THRESHOLD = 30
RG_FLOOR_SCORE = 0.5

# Default scoring weights (kept as module constants for backward compatibility).
# Override by passing explicit weights to hybrid_score() / apply_hybrid_scoring().
_VECTOR_WEIGHT: float = 0.7
_FRECENCY_WEIGHT: float = 0.3


def _file_size_factor(chunk_count: int, threshold: int = _FILE_SIZE_THRESHOLD) -> float:
    """Return a [0, 1] penalty factor for files larger than the threshold.

    Files at or below *threshold* chunks return 1.0 (no penalty).
    Larger files return threshold / chunk_count, linearly reducing the score.
    """
    return min(1.0, threshold / max(1, chunk_count))


# ── nexus-tox2m: cross-model distance calibration ───────────────────────────
#
# Mirrors config.py's ``search.distance_threshold`` defaults (code=0.45,
# knowledge/docs/rdr=0.65) -- the same empirically-calibrated per-corpus
# relevance boundary ``search_engine._threshold_for_collection`` already
# enforces at the filter stage (search_engine.py:528). Reused here as a
# SCALE CORRECTION on raw cosine distance, not a second classification: a
# corpus's distance is rescaled toward voyage-code-3's tighter absolute
# range (0.45, the smallest/most demanding threshold) in proportion to how
# much more lenient that model's threshold is, before ONE pooled min-max
# window is computed over every result. This keeps the merge on a single
# genuinely-comparable absolute scale, unlike either (a) raw distance,
# where voyage-code-3's naturally tighter scale dominates regardless of
# relevance, or (b) independent per-corpus windows, which mint a "winner"
# out of each corpus's local best regardless of whether that candidate is
# actually a good match (code review Critical 2, first round).
#
# nexus-tox2m follow-on (code review Critical, second round, 2026-09-01):
# the scale gap this calibration corrects exists ONLY because cloud/service
# mode embeds code__ with voyage-code-3 and knowledge__/docs__/rdr__ with
# voyage-context-3. In LOCAL-MODE installs (the DEFAULT install path — see
# fresh-install-mvv) every collection shares ONE local embedder
# (bge-base-en-v15-768 or minilm), so there is no gap to correct at all.
# Keying purely on COLLECTION PREFIX (the first version of this fix) fired
# regardless — falsified directly: two results at an IDENTICAL raw
# distance, one code__ one knowledge__ (the exact tie a shared-model
# deployment produces), scored 0.0 vs 1.0 purely from the collection name.
# Classification is therefore by the RESOLVED EMBEDDING MODEL
# (:func:`nexus.corpus.embedding_model_for_collection` — the same resolver
# the index/query write and read paths already treat as authoritative),
# and calibration is a hard no-op (factor 1.0 for every result) whenever
# the call's result set resolves to ONE distinct model — see
# :func:`_resolve_calibration_factors`, which is where that gate lives.
_CALIBRATION_THRESHOLDS_BY_MODEL: dict[str, float] = {
    "voyage-code-3": 0.45,
    "voyage-context-3": 0.65,
}
_CALIBRATION_BASELINE: float = _CALIBRATION_THRESHOLDS_BY_MODEL["voyage-code-3"]
_CALIBRATION_DEFAULT_THRESHOLD: float = 0.55  # matches config.py's "default" key


def _calibration_factor_for_model(model: str) -> float:
    """Return the distance scale-correction factor for a RESOLVED
    embedding *model* (not a collection name or prefix).

    ``raw_distance * factor`` rescales onto voyage-code-3's absolute
    range. voyage-code-3 itself gets 1.0 (the baseline, no change); a
    model with a more lenient threshold gets a factor < 1.0, shrinking
    its distances proportionally. An unrecognized model (anything other
    than the two Voyage models this table knows) falls to the same
    "default" bucket ``search_engine._threshold_for_collection`` already
    uses for an unrecognized collection prefix.

    Callers MUST gate on :func:`_resolve_calibration_factors`'s
    multi-model check before relying on this — called directly and
    unconditionally, this function has no way to know whether the
    result set actually spans more than one model.
    """
    threshold = _CALIBRATION_THRESHOLDS_BY_MODEL.get(model, _CALIBRATION_DEFAULT_THRESHOLD)
    return _CALIBRATION_BASELINE / threshold


def _resolve_calibration_factors(results: list[SearchResult]) -> dict[str, float]:
    """Resolve a distance scale-correction factor per COLLECTION for one
    :func:`apply_hybrid_scoring` call, gated on whether the result set
    actually spans more than one embedding model.

    Returns ``{collection_name: factor}`` covering every distinct
    non-``rg__cache`` collection in *results*. When every collection
    resolves to the SAME model (the local-mode default, and any single-
    corpus cloud-mode search), every factor is exactly ``1.0`` — a true
    no-op, not a prefix-derived reshuffle. Only when two or more distinct
    models are actually present does a real per-model factor apply.
    """
    from nexus.corpus import embedding_model_for_collection  # noqa: PLC0415 — circular-dep avoidance (nexus.corpus)

    model_by_collection: dict[str, str] = {}

    def _model_for(collection: str) -> str:
        model = model_by_collection.get(collection)
        if model is None:
            model = embedding_model_for_collection(collection)
            model_by_collection[collection] = model
        return model

    collections = {r.collection for r in results if r.collection != "rg__cache"}
    models = {_model_for(c) for c in collections}

    if len(models) <= 1:
        return dict.fromkeys(collections, 1.0)

    return {c: _calibration_factor_for_model(_model_for(c)) for c in collections}


def min_max_normalize(value: float, window: list[float]) -> float:
    """Normalize *value* into [0, 1] using the min/max of *window*.

    Computed over the combined result window (not per-corpus). Returns 1.0
    when *window* has a single element (it is trivially the maximum). Returns
    0.0 when all values are identical (denominator collapses to ε).

    Raises ValueError if *window* is empty.
    """
    if not window:
        raise ValueError("min_max_normalize: window must be non-empty")
    if len(window) == 1:
        return 1.0  # single element is trivially the best; avoid collapsing to 0.0
    lo = min(window)
    hi = max(window)
    return (value - lo) / (hi - lo + _EPSILON)


def hybrid_score(
    vector_norm: float,
    frecency_norm: float,
    vector_weight: float = _VECTOR_WEIGHT,
    frecency_weight: float = _FRECENCY_WEIGHT,
) -> float:
    """Weighted combination of vector and frecency scores.

    Default weights (0.7 / 0.3) match the previous hard-coded values.
    Pass explicit weights from TuningConfig to override.
    """
    return vector_weight * vector_norm + frecency_weight * frecency_norm


def apply_hybrid_scoring(
    results: list[SearchResult],
    hybrid: bool,
    *,
    vector_weight: float = _VECTOR_WEIGHT,
    frecency_weight: float = _FRECENCY_WEIGHT,
    file_size_threshold: int = _FILE_SIZE_THRESHOLD,
    catalog: Any | None = None,
) -> list[SearchResult]:
    """Compute hybrid scores for *results*.

    For code__ corpora (hybrid=True): score = vector_weight * vector_norm + frecency_weight * frecency_norm.
    For docs__/knowledge__/rdr__ (hybrid=True): score = vector_weight * vector_norm
    (no frecency signal — frecency_norm is 0.0, not omitted; *vector_weight*
    still applies, so this stays comparable to code__'s score on the SAME
    scale rather than the uncapped 1.0 * vector_norm a non-code result got
    before nexus-tox2m — see "Normalization window" below for why that
    asymmetry mattered once distances are calibrated).
    Any collection (hybrid=False): score = 1.0 * vector_norm.

    Normalization window: ``vector_norm`` is computed via min-max
    normalization over ONE window pooled across every result in the call,
    after each result's raw distance is rescaled by a per-collection
    factor from :func:`_resolve_calibration_factors` (nexus-tox2m,
    2026-09-01) so distances from different embedding MODELS sit on a
    comparable absolute scale before they compete. Raw cosine distance is
    not comparable across models — a measured stable 0.135-0.203 scale
    gap between voyage-code-3 and voyage-context-3 on off-domain
    (irrelevant-to-both) queries is a model-scale artefact, not a
    relevance signal — so pooling one window over UNCALIBRATED distances
    let the tighter-scaled, larger code__ corpus dominate a merged top-N
    regardless of the prose corpus's actual relevance. Calibrating first
    and then pooling (rather than giving each corpus its OWN independent
    window) keeps a corpus with nothing relevant contributing nothing:
    its calibrated distances stay uniformly bad relative to the pool's
    true best, instead of a per-corpus window unconditionally crowning
    that corpus's own weak local-best to 1.0 (measured regression: an
    off-topic prose doc outranking an exact-text-match code file — code
    review Critical, first round, the design this replaced).

    Calibration only ever fires when the result set actually spans more
    than one RESOLVED EMBEDDING MODEL — see
    :func:`_resolve_calibration_factors`'s docstring. A local-mode result
    set (the default install path), where every collection shares one
    local embedder, gets factor 1.0 everywhere: a true no-op, never a
    prefix-derived reshuffle (code review Critical, second round — the
    first version of this fix keyed on collection PREFIX and fired
    regardless of the actual embedding model in play).

    File-size penalty: applied to all code__ results unconditionally after the
    initial score is computed: ``score *= _file_size_factor(chunk_count)``.
    Unrelated to and unaffected by the calibration above — RDR-006 (2026-
    02-28) governs this mechanism; see that RDR before changing it (R9:
    "penalty must be unconditional" — gating it on ``hybrid`` regresses
    RDR-006's validated single-corpus, non-hybrid code-search scenario,
    confirmed by code review Critical 1 against a prior draft of this fix
    that did exactly that).

    nexus-dxly: post-RDR-108 Phase 3 chunks no longer carry the
    ``chunk_count`` field in metadata. When *catalog* is provided, the
    penalty resolves ``chunk_count`` via a batch SQL lookup against
    ``documents.chunk_count`` keyed on the chunk's ``doc_id`` (== catalog
    tumbler post-RDR-101). Falls back to metadata then ``1`` (no penalty)
    when the catalog has no row or no catalog is supplied — preserves
    behaviour for pre-Phase-3 corpora.

    If *hybrid* is True but no code__ collections appear in results, a warning
    is logged and all results use 1.0 * vector_norm.

    *vector_weight*, *frecency_weight*, and *file_size_threshold* default to the
    module constants (backward-compatible).  Pass values from TuningConfig to
    honour per-repo configuration.

    Note: Mutates ``hybrid_score`` on each SearchResult in place before
    returning the sorted list.
    """
    if not results:
        return results

    has_code = any(r.collection.startswith("code__") for r in results)

    if hybrid and not has_code:
        _log.warning("--hybrid has no effect — no code corpus in scope")

    # Exclude rg__cache from normalization window — distance=0.0 from ripgrep
    # hits distorts the min-max range for real vector distances.
    #
    # nexus-tox2m: ONE pooled window across every result, computed over
    # CALIBRATED distances — see this function's docstring "Normalization
    # window" section. calibration_factors resolves to 1.0 everywhere when
    # the result set shares one embedding model (see
    # _resolve_calibration_factors docstring — this is the local-mode
    # no-op gate).
    calibration_factors = _resolve_calibration_factors(results)
    distances = [
        r.distance * calibration_factors.get(r.collection, 1.0)
        for r in results if r.collection != "rg__cache"
    ]
    frecencies = [
        r.metadata.get("frecency_score", 0.0)
        for r in results
        if r.collection.startswith("code__")
    ]

    # nexus-dxly: batch-resolve documents.chunk_count for code__ results
    # when a catalog is supplied; chunks lost the metadata field at
    # RDR-108 Phase 3.
    chunk_count_by_doc_id: dict[str, int] = {}
    if catalog is not None:
        code_doc_ids = {
            r.metadata.get("doc_id", "")
            for r in results
            if r.collection.startswith("code__")
        }
        code_doc_ids.discard("")
        if code_doc_ids:
            try:
                # nexus-qnp5s: chunk_counts_for_docs() is implemented on
                # both SQLite Catalog and HttpCatalogClient — no raw _db.
                chunk_count_by_doc_id = catalog.chunk_counts_for_docs(
                    list(code_doc_ids)
                )
            except Exception as exc:  # noqa: BLE001 — best-effort catalog lookup; failure logged, scoring proceeds without chunk counts
                _log.warning(
                    "scoring_chunk_count_lookup_failed",
                    error=str(exc),
                    doc_id_count=len(code_doc_ids),
                )

    for r in results:
        if r.collection == "rg__cache":
            r.hybrid_score = RG_FLOOR_SCORE
            continue
        # Invert: distances are dissimilarity (smaller = better), so best match → v_norm=1.0
        calibrated = r.distance * calibration_factors.get(r.collection, 1.0)
        v_norm = 1.0 - min_max_normalize(calibrated, distances) if distances else 1.0
        if hybrid:
            # nexus-tox2m follow-on: apply vector_weight to EVERY result
            # when hybrid=True, not just code__. Before calibration this
            # asymmetry (code__ compressed to vector_weight*v_norm,
            # everything else left at the full, unweighted v_norm) was
            # dormant — a pooled RAW window rarely let a non-code v_norm
            # rise anywhere near code's, so the gap never mattered. Once
            # distances are calibrated onto a genuinely comparable scale,
            # a merely-good prose match's now-honest v_norm can exceed
            # vector_weight (0.7 by default) and structurally outrank
            # even a PERFECT code match (v_norm=1.0, capped at 0.7*1.0) —
            # the same "fake winner" failure class Critical 2 named, just
            # surfacing through the weight formula instead of the window.
            # Non-code__ results have no frecency signal, so f_norm is
            # simply 0.0 for them; code__ keeps its existing frecency-
            # window blend.
            if r.collection.startswith("code__"):
                f_score = r.metadata.get("frecency_score", 0.0)
                f_norm = min_max_normalize(f_score, frecencies) if frecencies else 0.0
            else:
                f_norm = 0.0
            r.hybrid_score = hybrid_score(v_norm, f_norm, vector_weight, frecency_weight)
        else:
            r.hybrid_score = v_norm
        if r.collection.startswith("code__"):
            doc_id = r.metadata.get("doc_id", "")
            chunk_count = chunk_count_by_doc_id.get(doc_id) or int(
                r.metadata.get("chunk_count", 1)
            )
            r.hybrid_score *= _file_size_factor(chunk_count, file_size_threshold)

    return sorted(results, key=lambda r: r.hybrid_score, reverse=True)


def quality_score(
    citation_count: int,
    age_days: float = 0.0,
    alpha: float = 0.5,
    half_life: float = 730.0,
    c_max: float = 10_000.0,
) -> float:
    """Compute quality score from bibliographic metadata (RDR-055 E2).

    Returns 0.0 when *citation_count* is 0 (unenriched) to avoid bias.

    ``quality = α × log(count+1)/log(C+1) + (1-α) × 0.5^(age/half_life)``
    """
    if citation_count <= 0:
        return 0.0
    import math  # noqa: PLC0415 — branch-local; only reached when citation_count > 0
    citation_signal = min(1.0, math.log(citation_count + 1) / math.log(c_max + 1))
    age_signal = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
    return alpha * citation_signal + (1 - alpha) * age_signal


# Default boost weight — how much quality_score influences hybrid_score.
_QUALITY_BOOST_WEIGHT: float = 0.1

# Collections eligible for quality boost (bibliographic metadata expected).
_QUALITY_ELIGIBLE_PREFIXES = ("knowledge__", "docs__", "rdr__")


def apply_quality_boost(
    results: list[SearchResult],
    boost_weight: float = _QUALITY_BOOST_WEIGHT,
) -> list[SearchResult]:
    """Boost hybrid_score of results that have bibliographic quality metadata.

    Mutates ``hybrid_score`` in place: ``score += boost_weight × quality_score``.
    Only applies to knowledge__/docs__/rdr__ collections.  Results without
    ``bib_citation_count`` are untouched.
    """
    from datetime import date  # noqa: PLC0415 — branch-local helper import

    today = date.today()
    for r in results:
        if not r.collection.startswith(_QUALITY_ELIGIBLE_PREFIXES):
            continue
        count = int(r.metadata.get("bib_citation_count", 0))
        if count <= 0:
            continue
        bib_year = r.metadata.get("bib_year", "")
        age_days = 0.0
        if bib_year:
            try:
                pub_date = date(int(bib_year), 6, 15)  # mid-year estimate
                age_days = max(0.0, (today - pub_date).days)
            except (ValueError, TypeError):
                pass
        r.hybrid_score += boost_weight * quality_score(count, age_days=age_days)
    return results


# ── Link-aware boost (RDR-060 E3) ───────────────────────────────────────────

_LINK_BOOST_WEIGHTS: dict[str, float] = {
    "implements": 1.0,
    "relates": 0.5,
    "cites": 0.5,
    "supersedes": 0.0,
}
_DEFAULT_LINK_BOOST_WEIGHT: float = 0.15


def apply_link_boost(
    results: list[SearchResult],
    catalog: Any,
    boost_weight: float = _DEFAULT_LINK_BOOST_WEIGHT,
    type_weights: dict[str, float] | None = None,
) -> list[SearchResult]:
    """Boost hybrid_score for results whose source documents have outgoing links.

    nexus-1qed (RDR-101 Phase 4): keyed on the catalog ``doc_id`` rather
    than on the legacy ``source_path``. The catalog projects ``doc_id``
    onto ``tumbler`` directly, so the prune verb (.10.3) can drop
    ``source_path`` from chunk metadata without breaking the link boost.

    Only processes results that carry ``doc_id`` metadata. Chunks
    predating the doc_id backfill are skipped. Additive:
    ``score += boost_weight * min(signal, 1.0)``.
    """
    if not catalog:
        return results
    tw = type_weights if type_weights is not None else _LINK_BOOST_WEIGHTS

    # Collect unique doc_ids from results.
    doc_ids: set[str] = set()
    for r in results:
        did = r.metadata.get("doc_id", "")
        if did:
            doc_ids.add(did)

    if not doc_ids:
        return results

    # nexus-1qed: Phase 1 contract is ``doc_id == str(tumbler)`` (see
    # catalog/catalog.py:_DocumentRegisteredPayload). The links table
    # is keyed on tumbler, so doc_id can join links.from_tumbler
    # directly without the source_path → tumbler indirection the
    # legacy code carried. Phase 3 will mint UUID7 doc_ids distinct
    # from tumblers; that change reintroduces a metadata.doc_id →
    # tumbler projection step here.
    # nexus-qnp5s: links_from_batch() is implemented on both SQLite
    # Catalog and HttpCatalogClient — no raw _db access.
    try:
        links_by_tumbler = catalog.links_from_batch(list(doc_ids))
    except Exception as exc:  # noqa: BLE001 — best-effort catalog lookup; failure logged, scoring proceeds without link boost
        _log.warning(
            "scoring_link_boost_lookup_failed",
            error=str(exc),
            doc_id_count=len(doc_ids),
        )
        return results

    # Aggregate: tumbler -> total weighted signal
    tumbler_signal: dict[str, float] = {}
    for from_t, link_list in links_by_tumbler.items():
        for lnk in link_list:
            link_type = lnk.get("link_type", "")
            w = tw.get(link_type, 0.0)
            tumbler_signal[from_t] = tumbler_signal.get(from_t, 0.0) + w

    # Apply boost.
    for r in results:
        did = r.metadata.get("doc_id", "")
        if not did:
            continue
        signal = min(tumbler_signal.get(did, 0.0), 1.0)
        if signal > 0:
            r.hybrid_score += boost_weight * signal

    return results


# ── Topic boost (RDR-070, nexus-aym) ─────────────────────────────────────

_TOPIC_SAME_BOOST: float = 0.1
_TOPIC_LINKED_BOOST: float = 0.05


def apply_topic_boost(
    results: list[SearchResult],
    topic_assignments: dict[str, int],
    *,
    topic_links: dict[tuple[int, int], int] | None = None,
) -> list[SearchResult]:
    """Boost results that share or are linked by topic.

    Reduces ``distance`` (lower = better) rather than modifying
    ``hybrid_score``, because ``hybrid_score`` is populated later
    by the reranker and would be overwritten.

    For each result with a topic assignment:
    - If another result in the set shares the SAME topic: -_TOPIC_SAME_BOOST distance
    - If another result is in a LINKED topic: -_TOPIC_LINKED_BOOST distance

    Boost is applied once per relationship type (not per partner).
    """
    if not topic_assignments or len(results) < 2:
        return results

    links = topic_links or {}

    # Build topic_id → set of result indices
    topic_to_indices: dict[int, list[int]] = {}
    result_topics: dict[int, int] = {}  # result index → topic_id
    for i, r in enumerate(results):
        tid = topic_assignments.get(r.id)
        if tid is not None:
            result_topics[i] = tid
            topic_to_indices.setdefault(tid, []).append(i)

    # Build set of linked topic pairs (both directions)
    linked_pairs: set[tuple[int, int]] = set()
    for (a, b) in links:
        linked_pairs.add((a, b))
        linked_pairs.add((b, a))

    for i, r in enumerate(results):
        tid = result_topics.get(i)
        if tid is None:
            continue

        # Same-topic boost: at least one other result in the same topic
        same_topic_peers = topic_to_indices.get(tid, [])
        if len(same_topic_peers) > 1:
            r.distance = max(0.0, r.distance - _TOPIC_SAME_BOOST)

        # Linked-topic boost: at least one result in a linked topic
        has_linked = False
        for other_tid, indices in topic_to_indices.items():
            if other_tid == tid:
                continue
            if (tid, other_tid) in linked_pairs:
                has_linked = True
                break
        if has_linked:
            r.distance = max(0.0, r.distance - _TOPIC_LINKED_BOOST)

    return results


def round_robin_interleave(
    grouped: list[list[SearchResult]],
) -> list[SearchResult]:
    """Interleave multiple result lists in round-robin order."""
    merged: list[SearchResult] = []
    iterators = [iter(g) for g in grouped]
    while iterators:
        next_iters = []
        for it in iterators:
            try:
                merged.append(next(it))
                next_iters.append(it)
            except StopIteration:
                pass  # intentional: iterator exhaustion is normal control flow
        iterators = next_iters
    return merged
