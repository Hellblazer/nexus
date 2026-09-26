# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-assignments``: does the engine's LIVE cross-collection
("projection") ANN pick agree with an exact recompute, over the SAME live
foreign-centroid snapshot, at the SAME moment?

nexus-v4pj4, a substantive-critic follow-on to nexus-f3yxx/nexus-iygza
(T2 ``nexus/review-f3yxx-iygza-round2-substantive-critic-2026-09-25``).
Since engine-service-v0.1.132 the cross ("projection") pass of
``nexus.assign_from_chashes_<dim>`` picks each chunk's nearest
FOREIGN-collection centroid via a per-chunk ``CROSS JOIN LATERAL``
against the HNSW index (``taxonomy-018-assign-cross-lateral-hnsw.xml``),
tuned (``hnsw.iterative_scan=strict_order``, ``hnsw.ef_search=400``, plus
``enable_seqscan=off``/``enable_sort=off`` below pgvector's natural
Seq-Scan crossover -- ``taxonomy-020``) until it measured EQUAL to exact
recall. Production shows 0 wrong picks over ~49k decisions (2026-09-25),
but a wrong pick from a future pgvector upgrade or HNSW build-parameter
drift would be silent: nothing else in the audit surface re-derives the
pick and compares it.

ROUND-1 REVIEW FINDING (both reviewers, not-justified) AND THE ROUND-2 FIX
---------------------------------------------------------------------------
Round 1 compared a STORED historical pick against TODAY's live foreign
centroids -- two different MOMENTS -- so healthy taxonomy growth (a topic
discovered after the assignment) or an existing centroid revised via
rebuild/merge could read as ANN drift even though the ANN itself never
did anything wrong. An eligibility-cutoff mitigation (excluding foreign
topics created after the stored decision) closed the first half read-only
but could not touch the second (a revised centroid carries no
last-updated timestamp anywhere client-visible), and both reviewers'
actual demand was structural: "compare the engine's ANN answer and the
exact answer over the SAME live centroid set at the SAME moment, never
the stored row."

Round 2 (this version) does exactly that, via a new engine route,
``POST /v1/taxonomy/assignments/cross-preview`` (``nexus.
cross_preview_<dim>``, taxonomy-021, built in this same bead once
investigation confirmed no existing route qualified -- the one read-only
ANN route, ``/v1/taxonomy/centroids/query``, runs
``hnsw.iterative_scan=relaxed_order``/``hnsw.ef_search=n_results``, a
materially different and weaker recall configuration than the cross
pass's own settings, and ``assign_from_chashes`` itself has no dry-run
form). ``cross_preview_<dim>`` is a READ-ONLY twin of ``assign_from_
chashes_<dim>``'s cross branch: the identical ``batch``/``nearest`` CTE,
under the identical four transaction-local settings, but with no
``persisted`` INSERT CTE at all -- it can never write to
``topic_assignments``, and its Python-vs-SQL text stays drift-free by
construction (``CrossPreviewDriftTest`` on the engine side asserts a
byte-identical shared span).

This probe therefore: samples ordinary chunks from a source collection
(no longer restricted to chunks that already carry a stored projection
row -- ANY chunk with a live vector at this collection's dim is now
comparable, since the oracle no longer depends on assignment history at
all); asks the engine, RIGHT NOW, for its live ANN pick via
``cross_preview``; fetches every live foreign centroid for the collection,
in the SAME run; and independently recomputes the exact nearest ELIGIBLE
centroid in pure Python over that identical snapshot. A currently-stored
``assigned_by='projection'`` row, if one exists for a sampled chunk, is
fetched ONLY as CONTEXT for the report (did the live ANN pick agree with
what is actually persisted right now?) -- it is never consulted to decide
pass or fail, and the round-1 eligibility-cutoff timestamp filter this
module used to carry is GONE: it existed solely to compensate for reading
a possibly-stale stored row, which never happens in this design.

RACE NOTE (round-2 review, critic Significant + code-review Minor): "the
same run" above is a client-side illusion unless it is enforced. A single
``get_foreign`` fetch taken AFTER the ``cross_preview`` call could observe
a foreign-centroid set that changed WHILE the engine's own LATERAL was
running -- in EITHER direction: a topic added or a centroid revised in
place after the engine computed its answer but before this probe reads
(the engine's answer would then be compared against a snapshot it never
saw, reading as a false disagreement), or, symmetrically, a topic removed
or revised in that same window (this probe's exact recompute would then
run over centroids the engine's answer legitimately never had a chance to
see either). Both directions are closed the same way: :func:`probe_collection`
fetches the foreign-centroid snapshot TWICE per attempt, immediately
before and immediately after the ``cross_preview`` call, bypassing
:class:`~nexus.db.t2.http_centroid_store.HttpCentroidStore`'s per-instance
cache for both reads (that cache is invalidated only by a mutation THIS
instance issues -- a different process's write is otherwise invisible to
it by documented design, which would make a naive before/after comparison
trivially always agree with itself). If the two snapshots differ, this
collection's batch is not compared this run; the whole before/call/after
sequence is retried ONCE; if it still differs, the collection is reported
CHANGED DURING PROBE -- reported, not a failure, and never folded into a
disagreement or an INCONCLUSIVE result. The comparison is SCOPED to the
sampled collection's own embedding dimension (round-3 review, Minor):
``get_foreign`` returns every OTHER collection's centroids across every
dim a tenant's collections use, so an unscoped before/after comparison
would spuriously flag this collection over a centroid change in a
completely unrelated dim, one this probe's own recompute never reads at
all.

Exit 0 when every sampled chunk's exact recompute agrees with the
engine's live ANN pick (within :data:`SIMILARITY_TIE_TOLERANCE`), 1 when
any disagrees, any collection could not be probed, or nothing was
compared. A source collection with no live foreign centroid at all (a
single-collection tenant, or one restricted via
``--assignments-collection`` that happens to have none) is reported "not
applicable", never a failure by itself. A collection this run's sample
failed to produce any comparable chunk for (a real chance event, not a
population question any more -- see the paragraph above) is reported
INCONCLUSIVE, likewise never a failure alone. A collection whose live
foreign-centroid snapshot would not stabilize across the before/after
reads, even after one retry, is reported CHANGED DURING PROBE (see the
RACE NOTE above), also never a failure alone. An engine older than the
one that shipped ``cross-preview`` 404s on the very first call; that is
reported as a single "not applicable: engine below ..." line, exit 0 --
an old engine is not a defect this check can observe anything about.

NO SIMILARITY THRESHOLD: the cross branch of ``assign_from_chashes_<dim>``
(and therefore ``cross_preview_<dim>``, its byte-identical twin) has no
distance/similarity floor at all -- every chunk gets its unconditional
nearest foreign centroid (``LIMIT 1``, no ``WHERE`` on the distance)
whenever at least one foreign centroid exists at this collection's dim.
"""
from __future__ import annotations

import math
import random
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import click
import structlog

from nexus.doctor_embeddings import default_seed, window_offsets

_log = structlog.get_logger(__name__)

#: Similarity gap above which a disagreement is real rather than a near-tie.
#: Both similarities compared here (the exact best pick and this probe's
#: OWN recompute of the engine's ANN-picked topic's similarity) are
#: produced by THIS module's own pure-Python cosine against the SAME two
#: live centroid embeddings -- never a comparison against the engine's
#: reported similarity number (that value is carried in the report only
#: as context). The tolerance separates a genuine wrong pick from two
#: eligible centroids that are, to this vector, essentially equidistant:
#: when the gap between "exact best" and "engine's pick" is this small,
#: which one an approximate search happens to land on is not a
#: correctness question the ef_search=400/strict_order tuning was ever
#: meant to resolve, and calling it a defect would flag noise, not drift.
SIMILARITY_TIE_TOLERANCE = 1e-6
#: Chunks sampled per collection when the caller names no size.
DEFAULT_SAMPLE = 20
#: Detection power at the default sample, assuming independent draws (the
#: windowed sampling in :func:`nexus.doctor_embeddings.window_offsets`
#: approximates this well enough for this purpose) and a systemic
#: wrong-pick RATE p across a collection's chunk population: the
#: probability this run catches AT LEAST ONE wrong pick is
#: ``1 - (1 - p) ** DEFAULT_SAMPLE``. At the default sample of 20: a 5%
#: wrong-pick rate is caught ~64% of the time, 10% ~88%, 20% ~99%. A rare,
#: isolated bad pick well under 1% of a collection's population is
#: unlikely to land in any single day's sample -- rerun with a larger
#: ``--assignments-sample`` (up to 300) or a different
#: ``--assignments-seed`` for higher-confidence coverage of a suspected
#: narrow defect, the same tradeoff ``--check-embeddings`` documents for
#: its own sample.
#: Collections probed at once.
_WORKERS = 4
#: Worst rows named per collection.
_MAX_NAMED = 5
#: Substring an engine's 404 (route predates this bead) reliably carries
#: in httpx's own exception text -- used to fold an old-engine population
#: into one "not applicable" line instead of N identical per-collection
#: failures. A coarse text match, not a status-code inspection, because
#: the failure is captured generically in :func:`probe_collection`'s
#: broad ``except Exception`` (every OTHER error must still surface with
#: its own detail there) -- accepted for the same reason a stray "404" in
#: an unrelated error message is vanishingly unlikely in this call's own
#: failure surface (connection/timeout/HTTP-status text).
_ENGINE_404_MARKER = "404"
#: Before/after foreign-centroid snapshot attempts per collection (round-2
#: review, critic Significant): the first attempt plus ONE retry, per the
#: module docstring's RACE NOTE. Not a tunable -- a coordinator decision,
#: not a population/detection-power knob like `DEFAULT_SAMPLE`.
_MAX_SNAPSHOT_ATTEMPTS = 2


def _live_foreign(taxo: Any, name: str) -> dict[str, list[Any]]:
    """A GENUINELY live ``get_foreign`` read, bypassing
    :class:`~nexus.db.t2.http_centroid_store.HttpCentroidStore`'s
    per-instance ``get_foreign``/``get_by_collection`` cache -- that cache
    is invalidated only by a mutation THIS SAME instance issues
    (``upsert``/``delete_ids``/``purge``); a different process's write
    between two calls on this instance is invisible to it by documented
    design. The before/after comparison in :func:`probe_collection` exists
    specifically to detect a cross-process change DURING the probe
    window, so both reads must bypass the cache -- otherwise the "after"
    read would just return the "before" read's cached value and the
    comparison would trivially always agree with itself, closing nothing.

    Fake ``_centroid`` test doubles (this module's own pure-part tests)
    carry no such cache and therefore no invalidation method at all --
    skipped via ``getattr`` rather than required, so those fakes need no
    changes to keep working.
    """
    invalidate = getattr(taxo._centroid, "_invalidate_centroid_cache", None)
    if invalidate is not None:
        invalidate()
    return taxo._centroid.get_foreign(name)


def _foreign_snapshot_map(
    foreign: dict[str, list[Any]], dim: int | None = None,
) -> dict[int, list[float]]:
    """Collapse a ``get_foreign()`` envelope into ``{topic_id: embedding}``,
    order-independent, so two snapshots can be compared for equality
    regardless of what order the engine happened to return rows in. Catches
    an added, removed, OR revised-in-place centroid alike -- any of the
    three changes this dict's equality with an earlier snapshot.

    *dim*, when given, SCOPES the map to centroids at that embedding
    dimension only (round-3 review, Minor): ``get_foreign`` returns every
    OTHER collection's centroids across every embedding dim a tenant's
    collections use, not just the sampled source collection's own dim.
    Without scoping, a centroid change in a completely UNRELATED dim (a
    different collection's own rebuild, nothing to do with the one being
    probed) would still change this dict and spuriously flag a
    ``changed_during_probe`` for a collection nothing actually raced
    against. ``dim=None`` (the caller could not determine the sampled
    dim up front -- see :func:`probe_collection`) falls back to comparing
    every dim, the original unscoped behavior.
    """
    return {
        int(meta["topic_id"]): list(emb)
        for emb, meta in zip(foreign.get("embeddings", []), foreign.get("metadatas", []))
        if dim is None or len(emb) == dim
    }


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine in plain Python (mirrors :func:`nexus.doctor_embeddings._cosine`
    -- kept local rather than imported so this module has no dependency on
    that module's private surface). A zero-length vector gives NaN.
    """
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(y * y for y in b))
    return dot / norm if norm else math.nan


@dataclass
class Disagreement:
    """One sampled chunk whose exact-recomputed nearest live foreign topic
    differs from the engine's LIVE ``cross-preview`` ANN pick, over the
    SAME centroid snapshot, by more than :data:`SIMILARITY_TIE_TOLERANCE`.
    """

    doc_id: str
    ann_topic_id: int
    #: Verbatim from the engine's cross-preview response -- context only.
    ann_reported_similarity: float
    #: This probe's OWN recompute of cosine(vector, ann_topic_id's live
    #: centroid); used for the gap, never ``ann_reported_similarity``.
    ann_recomputed_similarity: float
    exact_topic_id: int
    exact_similarity: float
    gap: float
    #: The topic a CURRENTLY-stored ``assigned_by='projection'`` row names
    #: for this chunk, if any -- CONTEXT ONLY (did the live ANN pick agree
    #: with what production has actually persisted?), never consulted to
    #: decide pass or fail. ``None`` when no such row exists or it could
    #: not be read (a best-effort fetch; see :func:`probe_collection`).
    stored_topic_id: int | None = None


@dataclass
class CollectionAssignmentDrift:
    """One source collection's probe result. ``error`` set means not probed."""

    collection: str
    #: Total chunk count in this T3 collection (context only).
    size: int
    #: Sampled chunks actually compared (had a live vector at this dim
    #: AND a foreign centroid both the engine and this probe could see).
    compared: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    #: Sampled candidate ids the engine's cross-preview answered for, but
    #: whose vector this probe could not itself re-fetch (a re-embed in
    #: progress, or deleted between the two calls).
    no_vector: int = 0
    #: Sampled chunks whose engine-reported ANN topic id was not present
    #: in the SAME foreign-centroid snapshot this probe fetched (a rebuild
    #: or deletion landed between the two calls) -- excluded from
    #: `compared`, never counted as a disagreement.
    no_foreign_centroids: int = 0
    #: True when this collection has no live foreign centroid at all (no
    #: candidate chunk got a cross-preview answer) -- nothing wrong to
    #: detect when there is no cross-collection candidate to project onto.
    not_applicable: bool = False
    #: True when sampling produced candidate chunks but none turned out
    #: comparable (a real chance event now, not a population question --
    #: see the module docstring) -- distinct from both a clean pass and a
    #: failure.
    inconclusive: bool = False
    #: True when the live foreign-centroid snapshot this probe fetched
    #: immediately BEFORE and immediately AFTER its ``cross_preview`` call
    #: differed -- on both this attempt and its one retry (round-2 review,
    #: critic Significant + code-review Minor; see the module docstring's
    #: RACE NOTE). A centroid added, removed, or revised in place during
    #: that window makes it impossible to know which snapshot the engine's
    #: own answer actually saw, so this collection's batch is not compared
    #: this run rather than risk a false disagreement OR a false pass
    #: against the wrong snapshot. Distinct from both a clean pass and a
    #: failure, like `inconclusive` -- and mutually exclusive with it,
    #: since a collection that hits this never reaches the point where
    #: `inconclusive` would otherwise apply.
    changed_during_probe: bool = False
    error: str | None = None


def probe_collection(
    taxo: Any, t3: Any, name: str, size: int, sample: int, rng: random.Random,
) -> CollectionAssignmentDrift:
    """Sample *name*'s chunk ids, ask the engine for its LIVE cross-pass ANN
    pick on each (``HttpTaxonomyStore.cross_preview``, never persisted),
    fetch every live foreign centroid for *name* in this SAME run, and
    recompute the exact nearest one in pure Python -- comparing the
    engine's answer and the exact answer over the IDENTICAL snapshot.

    Any failure is recorded in ``error``; the collection then counts as
    not probed (a 404, meaning the deployed engine predates this route, is
    handled by the caller across every collection at once -- see the
    module docstring). A collection with no live foreign centroid at this
    dim sets ``not_applicable=True``. A sample that turns up candidates
    but nothing comparable sets ``inconclusive=True`` with no error. A
    live foreign-centroid snapshot that will not stabilize across a
    before/after read, even after one retry, sets
    ``changed_during_probe=True`` (see the module docstring's RACE NOTE).
    """
    result = CollectionAssignmentDrift(collection=name, size=size)

    def _finish() -> CollectionAssignmentDrift:
        if (
            result.error is None
            and not result.not_applicable
            and not result.changed_during_probe
            and result.compared == 0
        ):
            result.inconclusive = True
        return result

    try:
        col = t3.get_or_create_collection(name)
        candidate_ids: list[str] = []
        for offset, limit in window_offsets(size, sample, rng):
            page = col.get(limit=limit, offset=offset)
            candidate_ids.extend(page.get("ids") or [])
        if not candidate_ids:
            return _finish()

        # Fetched EARLY (round-3 review, Minor), before the ann/snapshot
        # loop below, purely so `dim` (this SOURCE collection's own chunk
        # embedding dimension) is known in time to SCOPE the before/after
        # foreign-centroid comparison to it -- see `_foreign_snapshot_map`'s
        # own docstring for why an unscoped comparison can spuriously flag
        # a collection over an unrelated dim's centroid changing elsewhere.
        # `vecs` covers every candidate (a superset of whatever `ann`
        # answers for below), so it is reused directly further down instead
        # of a second, separate fetch for `matched`. `dim=None` when NO
        # candidate has a stored vector at all -- the before/after
        # comparison then falls back to comparing every dim (this run's
        # `ann` will end up empty regardless, since the engine cannot
        # answer for a chunk with no vector either, so `not_applicable`
        # already covers this case).
        vecs = t3.get_embeddings_by_id(name, candidate_ids)
        dim = len(next(iter(vecs.values()))) if vecs else None

        # The engine's LIVE answer, right now -- never a stored historical
        # row. An id absent from `ann` had no live chunk vector at this
        # dim, or `name` has no foreign centroid at all for THAT id's dim
        # (both benign; see the "no similarity threshold" module note for
        # why an absence is never a below-floor signal).
        #
        # Bracketed by a before/after live foreign-centroid snapshot (round-2
        # review, critic Significant + code-review Minor; module docstring's
        # RACE NOTE), scoped to `dim` (round-3 review, Minor): if the
        # snapshot changed WHILE cross_preview ran, this probe cannot know
        # which snapshot the engine's own answer actually saw, so the whole
        # sequence is retried once before giving up.
        ann: dict[str, tuple[int, float]] = {}
        foreign: dict[str, list[Any]] = {}
        for _attempt in range(_MAX_SNAPSHOT_ATTEMPTS):
            foreign_before = _live_foreign(taxo, name)
            ann = taxo.cross_preview(name, candidate_ids)
            if not ann:
                result.not_applicable = True
                return result
            foreign_after = _live_foreign(taxo, name)
            if _foreign_snapshot_map(foreign_before, dim) == _foreign_snapshot_map(foreign_after, dim):
                foreign = foreign_after
                break
        else:
            result.changed_during_probe = True
            return _finish()

        matched = sorted(ann)[:sample]
        found = [i for i in matched if i in vecs]
        result.no_vector = len(matched) - len(found)
        if not found:
            return _finish()

        centroids: dict[int, list[float]] = {}
        for emb, meta in zip(foreign.get("embeddings", []), foreign.get("metadatas", [])):
            if len(emb) == dim:
                centroids[int(meta["topic_id"])] = emb
        if not centroids:
            # The engine answered (a foreign centroid existed when IT ran),
            # but by the time THIS call landed nothing at this dim remained
            # live -- a genuine race, not a defect; report as not probed
            # rather than guessing at a comparison with no candidate set.
            result.error = f"no live foreign centroid at dim {dim} for {name} (raced with cross-preview)"
            return _finish()

        # Best-effort CONTEXT only (module docstring): never affects pass/
        # fail, so a failure here is swallowed, not surfaced as `error`.
        stored_context: dict[str, int] = {}
        try:
            for r in taxo.get_assignment_details(found):
                if r.get("assigned_by") == "projection" and r.get("source_collection") == name:
                    tid = int(r["topic_id"])
                    prev = stored_context.get(r["doc_id"])
                    if prev is None or str(r.get("assigned_at") or "") >= str(prev):
                        stored_context[r["doc_id"]] = tid
        except Exception as exc:  # noqa: BLE001 — context only, never load-bearing
            _log.debug("doctor_assignments_stored_context_failed", collection=name, error=str(exc))

        for doc_id in found:
            vec = vecs[doc_id]
            ann_topic_id, ann_reported_sim = ann[doc_id]
            sims = {tid: _cosine(vec, emb) for tid, emb in centroids.items()}
            exact_topic_id = min(sims, key=lambda tid: (-sims[tid], tid))
            exact_sim = sims[exact_topic_id]
            ann_recomputed_sim = sims.get(ann_topic_id)
            if ann_recomputed_sim is None:
                result.no_foreign_centroids += 1
                continue
            result.compared += 1
            gap = exact_sim - ann_recomputed_sim
            if exact_topic_id != ann_topic_id and gap > SIMILARITY_TIE_TOLERANCE:
                result.disagreements.append(Disagreement(
                    doc_id=doc_id, ann_topic_id=ann_topic_id, ann_reported_similarity=ann_reported_sim,
                    ann_recomputed_similarity=ann_recomputed_sim,
                    exact_topic_id=exact_topic_id, exact_similarity=exact_sim, gap=gap,
                    stored_topic_id=stored_context.get(doc_id),
                ))
    except Exception as exc:  # noqa: BLE001 — one collection's failure is reported, never hides the rest
        _log.debug("doctor_assignments_probe_failed", collection=name, error=str(exc))
        result.error = f"{type(exc).__name__}: {exc}"
    return _finish()


def probe_collections(
    taxo: Any, t3: Any, sizes: dict[str, int], *, sample: int, seed: int,
) -> list[CollectionAssignmentDrift]:
    """Probe every collection in *sizes*, in name order.

    Each collection gets its own RNG derived from the seed and its name,
    so its sample does not depend on which other collections were probed.
    """
    names = sorted(sizes)

    def _one(name: str) -> CollectionAssignmentDrift:
        return probe_collection(taxo, t3, name, sizes[name], sample, random.Random(f"{seed}:{name}"))

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        return list(pool.map(_one, names))


def format_report(
    results: list[CollectionAssignmentDrift], *, sample: int, seed: int,
) -> tuple[list[str], bool]:
    """Human lines and whether the run is clean."""
    lines: list[str] = []
    disagreed = [r for r in results if r.error is None and r.disagreements]
    failed = [r for r in results if r.error is not None]
    probed = [r for r in results if r.error is None and r.compared > 0]
    not_applicable = [r for r in results if r.error is None and r.not_applicable]
    inconclusive = [r for r in results if r.error is None and r.inconclusive]
    changed = [r for r in results if r.error is None and r.changed_during_probe]
    total = sum(r.compared for r in probed)
    # A run that compared nothing has not shown anything is healthy --
    # UNLESS every result is genuinely not_applicable (a single-collection
    # tenant, or every named collection lacks a live foreign centroid):
    # then there was nothing TO compare, which is a clean outcome, not an
    # unproven one. Mirrors the same distinction --check-embeddings draws
    # between "sampled nothing comparable" (not clean) and "no collection
    # holds chunks at all" (an explicit not-applicable, exit 0) one level
    # up in run_check_assignments; here it can only be decided per-result,
    # since not_applicable is discovered per collection during probing.
    all_not_applicable = bool(results) and len(not_applicable) == len(results)
    ok = not disagreed and not failed and (total > 0 or all_not_applicable)

    mark = "✓" if ok else "✗"
    lines.append(
        f"[{mark}] Assignment drift: {total} chunk(s) compared in {len(probed)} "
        f"collection(s), tie tolerance {SIMILARITY_TIE_TOLERANCE}, sample {sample}, "
        f"seed {seed}; {len(disagreed)} collection(s) with a disagreement, "
        f"{len(failed)} not probed"
    )
    for r in disagreed:
        worst = ", ".join(
            f"{d.doc_id[:12]} ann={d.ann_topic_id} exact={d.exact_topic_id} gap={d.gap:.4f}"
            f"{'' if d.stored_topic_id is None else f' (stored={d.stored_topic_id})'}"
            for d in sorted(r.disagreements, key=lambda d: -d.gap)[:_MAX_NAMED]
        )
        lines.append(
            f"      ✗ {r.collection}: {len(r.disagreements)}/{r.compared} disagree "
            f"({r.size} chunks); worst: {worst}"
        )
    for r in failed:
        lines.append(f"      ✗ {r.collection}: NOT PROBED ({r.error})")
    if inconclusive:
        lines.append(
            f"      INCONCLUSIVE: {len(inconclusive)} collection(s) sampled candidate "
            "chunks but none turned out comparable this run (a re-embed in progress, "
            "or a live foreign-centroid race) -- not a failure, not a clean pass: "
            + ", ".join(r.collection for r in inconclusive[:5])
            + (f" (+{len(inconclusive) - 5} more)" if len(inconclusive) > 5 else "")
        )
    if changed:
        lines.append(
            f"      CHANGED DURING PROBE: {len(changed)} collection(s) had their live "
            "foreign-centroid snapshot change between this probe's own before/after "
            "reads, even after one retry (a topic added, removed, or revised in place "
            "mid-probe) -- not compared this run, not a failure: "
            + ", ".join(r.collection for r in changed[:5])
            + (f" (+{len(changed) - 5} more)" if len(changed) > 5 else "")
        )
    no_vector = [r for r in results if r.error is None and r.no_vector]
    if no_vector:
        lines.append(
            f"      {sum(r.no_vector for r in no_vector)} sampled chunk(s) have no "
            "stored vector at the collection's dim (a re-embed in progress, or deleted "
            "mid-probe), not compared: "
            + ", ".join(f"{r.collection} ({r.no_vector})" for r in no_vector[:5])
        )
    missing_topic = [r for r in results if r.error is None and r.no_foreign_centroids]
    if missing_topic:
        lines.append(
            f"      {sum(r.no_foreign_centroids for r in missing_topic)} sampled "
            "chunk(s) had an engine ANN pick this probe's own foreign-centroid fetch "
            "no longer carried (raced with a rebuild/deletion), not compared: "
            + ", ".join(f"{r.collection} ({r.no_foreign_centroids})" for r in missing_topic[:5])
        )
    if not_applicable:
        lines.append(
            f"      not applicable: {len(not_applicable)} collection(s) have no live "
            "cross-collection foreign centroid to project onto: "
            + ", ".join(r.collection for r in not_applicable[:5])
            + (f" (+{len(not_applicable) - 5} more)" if len(not_applicable) > 5 else "")
        )
    if total == 0 and not failed and not ok:
        lines.append("      nothing was compared, so this is not a clean result")
    return lines, ok


def run_check_assignments(*, sample: int, collections: tuple[str, ...], seed: int | None) -> None:
    """CLI entry for ``nx doctor --check-assignments``."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore  # noqa: PLC0415 — deferred: CLI startup cost

    run_seed = default_seed() if seed is None else seed
    try:
        t3 = make_t3()
        listed = {str(c.get("name", "")): int(c.get("count", 0) or 0) for c in t3.list_collections()}
    except Exception as exc:  # noqa: BLE001 — boundary: an unreadable tenant is a hard failure here
        click.echo(f"[✗] Assignment drift: T3 UNREADABLE ({type(exc).__name__}: {exc})")
        raise SystemExit(1) from exc

    if collections:
        unknown = [c for c in collections if c not in listed]
        if unknown:
            click.echo(f"[✗] Assignment drift: no such collection(s): {', '.join(unknown)}")
            raise SystemExit(1)
        names = list(collections)
    else:
        names = sorted(n for n, s in listed.items() if n and s > 0)

    if not names:
        click.echo("[✓] Assignment drift: not applicable (no collection holds chunks)")
        return

    taxo = HttpTaxonomyStore()
    try:
        results = probe_collections(taxo, t3, {n: listed[n] for n in names}, sample=sample, seed=run_seed)

        # An engine below the version that shipped POST .../cross-preview
        # 404s on every collection identically -- fold that into ONE line
        # instead of N indistinguishable "NOT PROBED" rows (see
        # `_ENGINE_404_MARKER`'s own docstring for the detection caveat).
        if results and all(r.error is not None and _ENGINE_404_MARKER in r.error for r in results):
            click.echo(
                "[✓] Assignment drift: not applicable (the deployed engine is older "
                "than the version that added POST /v1/taxonomy/assignments/"
                "cross-preview, nexus-v4pj4)"
            )
            return

        lines, ok = format_report(results, sample=sample, seed=run_seed)
        for line in lines:
            click.echo(line)
        if not ok:
            raise SystemExit(1)
    finally:
        taxo.close()
