# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-assignments``: does each SAMPLED cross-collection
("projection") topic assignment agree with an exact recompute over the
SAME candidate topics the engine had when it made that decision?

nexus-v4pj4, a substantive-critic follow-on to nexus-f3yxx/nexus-iygza
(T2 ``nexus/review-f3yxx-iygza-round2-substantive-critic-2026-09-25``).
Since engine-service-v0.1.132 the cross ("projection") pass of
``nexus.assign_from_chashes_<dim>`` picks each chunk's nearest
FOREIGN-collection centroid via a per-chunk ``CROSS JOIN LATERAL``
against the HNSW index (``taxonomy-018-assign-cross-lateral-hnsw.xml``),
not an exact join -- an approximate-nearest-neighbor search traded for a
40x-plus speedup (11.8-13.8s exact vs 0.26-0.35s per chunk, measured on
that changeset). It was tuned (``hnsw.iterative_scan=strict_order``,
``hnsw.ef_search=400``) until it measured EQUAL to exact recall on the
authoring collections, and production shows 0 wrong picks over ~49k
decisions (2026-09-25) -- but a wrong pick from a future pgvector upgrade
or HNSW build-parameter drift would be silent: nothing else in the audit
surface re-derives the pick and compares it.

ROUND-1 REVIEW FINDING (both reviewers, not-justified) AND THE FIX
-------------------------------------------------------------------
The first cut compared a STORED pick against TODAY's live foreign
centroids, so healthy taxonomy growth (a topic discovered/rediscovered
AFTER the stored assignment was made) read as ANN drift: the stored
pick could not possibly have chosen a topic that did not exist yet, but
an unrestricted "exact over everything live today" recompute would
happily call that a disagreement.

The fully correct fix compares the engine's ANN answer and the exact
answer over the IDENTICAL live centroid snapshot at the IDENTICAL
moment -- which needs either a read-only engine route that runs the
cross pass's exact HNSW settings (``ef_search=400``,
``iterative_scan=strict_order``) without persisting, or accepting a
real (idempotent) re-write via ``assign_from_chashes`` itself. NEITHER
exists client-side today (investigated 2026-09-26, see the module's own
`docs/cli-reference.md` entry and the nexus-v4pj4 handoff note for the
detail): the one read-only ANN route, ``POST /v1/taxonomy/centroids/
query`` (``HttpCentroidStore.ann_query`` -> ``TaxonomyCentroidRepository
.annQuery``), sets ``hnsw.iterative_scan='relaxed_order'`` and
``hnsw.ef_search=n_results`` -- NOT the cross pass's ``strict_order``/
``400`` -- so it tests a materially different, weaker recall
configuration, not the one this probe exists to audit; and
``assign_from_chashes_<dim>`` has no dry-run form, it always computes
AND persists in the same statement. Recommendation on record for a
follow-on: an engine-side read-only route (or a query-parameter on the
existing one) that accepts the SAME HNSW settings the cross pass uses,
so a client-only audit can compare same-moment ANN vs exact without a
write. Filed as the open half of nexus-v4pj4 pending that engine work.

Given that, THIS probe closes the specific, provable false-positive
class the reviewers demonstrated (healthy taxonomy growth) with a
read-only mitigation that needs no engine change: rather than trusting
the stored pick's recorded SIMILARITY VALUE at all, it (1) picks the
CANONICAL stored row per chunk by RECENCY (``assigned_at``), since the
``topic_assignments`` conflict key is ``(tenant_id, doc_id, topic_id)``
-- see the CONFLICT-KEY NOTE below -- so a chunk reassigned to a
different topic can carry a STALE, higher-recorded-similarity row that
GREATEST-wins would otherwise treat as canonical; and (2) recomputes
the exact nearest centroid restricted to foreign topics whose
``created_at`` is at or before that stored decision's ``assigned_at``
-- a topic discovered or rediscovered AFTER the assignment was made is
excluded from its candidate set, exactly mirroring what the engine's
own LATERAL saw at decision time. This does NOT close the "an EXISTING
topic's centroid vector was later revised (rebuild/merge, same
topic_id)" half of the reviewers' finding -- there is no
centroid-last-updated timestamp exposed client-side to detect that; a
genuinely revised centroid still reads as a same-moment disagreement
here, which is the accepted residual gap pending the engine route above.

This probe samples chunks that carry a stored ``assigned_by='projection'``
row, re-fetches their stored vector (``HttpVectorClient.
get_embeddings_by_id``) and every live foreign centroid
(``HttpCentroidStore.get_foreign``), and recomputes the exact nearest
ELIGIBLE centroid with the SAME tie-break the engine's own LATERAL uses
(``ORDER BY distance, topic_id`` -- ascending distance, i.e. descending
cosine similarity, ties broken by the lower topic id). Same shape as
``nx doctor --check-embeddings`` (:mod:`nexus.doctor_embeddings`): opt-in
flag, a sample size, a reproducible seed, windowed sampling, NaN-safe
pure-Python cosine, and a non-vacuity exit code.

Exit 0 when every sampled row's exact recompute agrees with the stored
pick (within :data:`SIMILARITY_TIE_TOLERANCE`), 1 when any disagrees, any
collection could not be probed, or nothing was compared. A collection
with NO cross-collection projection population at all (a single-
collection tenant, or a collection restricted via
``--assignments-collection`` that happens to carry none) is reported as
"not applicable", never as a failure by itself. A collection WITH a known
projection population that this run's sample failed to reach (a thin,
low-density population the row-page cap could not realistically cover --
see the density-sized pool in :func:`probe_collection`) is
reported as INCONCLUSIVE, distinct from both a clean pass and a failure.

CONFLICT-KEY NOTE (read before touching the "stored pick" logic): the
engine's ``topic_assignments`` unique constraint is
``(tenant_id, doc_id, topic_id)``, NOT ``(tenant_id, doc_id)`` -- a chunk
reassigned to a DIFFERENT topic across two ``assign_from_chashes`` runs
(e.g. after a centroid rebuild) accumulates a SECOND 'projection' row
rather than overwriting the first; the old row is not implicitly
retracted (see ``HttpTaxonomyStore.prune_projection_below``, the
explicit remedy for exactly this). This probe therefore treats the
MOST-RECENTLY-DECIDED (max ``assigned_at``) 'projection' row per sampled
doc_id as the "current" stored pick -- NOT the highest-similarity row
(that was the round-1 design, and it is exactly backwards: a STALE row
from an earlier, weaker candidate set can carry a numerically HIGHER
recorded similarity than a later, correct reassignment onto a newly
discovered, genuinely-closer topic; GREATEST-wins is the right rule for
the ENGINE's own same-topic-id conflict resolution, but the wrong rule
for THIS probe's "which historical decision is current" question).

NO SIMILARITY THRESHOLD (round-1 finding (b), corrected): the cross
branch of ``assign_from_chashes_<dim>`` has no distance/similarity floor
at all -- every chunk gets its unconditional nearest foreign centroid
(``LIMIT 1``, no ``WHERE`` on the distance) whenever at least one foreign
centroid exists at this collection's dim. A sampled candidate chunk can
still lack a 'projection' row, but only because no foreign centroid
existed yet when it was indexed, or the cross pass was never invoked for
it at all (a drain/backlog gap, see ``HttpTaxonomyStore.
unassigned_chashes``/``mcp_infra.drain_unassigned_chunks``) -- never
because its best match scored too low.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import click
import structlog

from nexus.doctor_embeddings import default_seed, window_offsets

_log = structlog.get_logger(__name__)

#: Similarity gap above which a disagreement is real rather than a near-tie.
#: Both similarities compared here (the exact best pick and the stored
#: pick's own recompute) are produced by THIS module's own pure-Python
#: cosine against the SAME two live centroid embeddings -- this is never a
#: comparison against the engine's recorded number (that value is used only
#: to help pick the CANONICAL stored row among duplicates; see the
#: CONFLICT-KEY NOTE). The tolerance instead separates a genuine wrong pick
#: from two eligible centroids that are, to this vector, essentially
#: equidistant: when the gap between "best" and "stored" is this small,
#: which one an approximate search happens to land on is not a
#: correctness question the ef_search=400/strict_order tuning was ever
#: meant to resolve, and calling it a defect would flag noise, not drift.
SIMILARITY_TIE_TOLERANCE = 1e-6
#: Chunks sampled per collection when the caller names no size.
DEFAULT_SAMPLE = 20
#: Detection power at the default sample, assuming independent draws (the
#: windowed sampling in :func:`nexus.doctor_embeddings.window_offsets`
#: approximates this well enough for this purpose) and a systemic
#: wrong-pick RATE p across the collection's projection population: the
#: probability this run catches AT LEAST ONE wrong pick is
#: ``1 - (1 - p) ** DEFAULT_SAMPLE``. At the default sample of 20: a 5%
#: wrong-pick rate is caught ~64% of the time, 10% ~88%, 20% ~99%. A rare,
#: isolated bad pick well under 1% of a collection's population is
#: unlikely to land in any single day's sample -- rerun with a larger
#: ``--assignments-sample`` (up to 300) or a different
#: ``--assignments-seed`` for higher-confidence coverage of a suspected
#: narrow defect, the same tradeoff ``--check-embeddings`` documents for
#: its own sample.
#: Floor on the density-sized candidate pool (see :func:`probe_collection`):
#: never smaller than ``sample * _MIN_POOL_MULTIPLE``, even when the known
#: projection density alone would suggest a smaller pool -- real
#: populations cluster (a batch of chunks indexed together tends to share
#: assignment fate), so a pool sized to the EXPECTED yield with no margin
#: would systematically undershoot on an unlucky draw.
_MIN_POOL_MULTIPLE = 3
#: Collections probed at once.
_WORKERS = 4
#: Worst rows named per collection.
_MAX_NAMED = 5


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
    """One sampled chunk whose exact-recomputed nearest ELIGIBLE foreign
    topic (see the module docstring's eligibility-cutoff design) differs
    from its stored, most-recently-decided 'projection' pick by more than
    :data:`SIMILARITY_TIE_TOLERANCE`."""

    doc_id: str
    stored_topic_id: int
    #: This probe's OWN recompute of cosine(vector, stored topic's live
    #: centroid) -- never the engine's recorded number; see
    #: :data:`SIMILARITY_TIE_TOLERANCE`'s docstring.
    stored_topic_similarity: float
    exact_topic_id: int
    exact_similarity: float
    gap: float
    #: The stored pick's own ``assigned_at`` (context for the report: is
    #: this a fresh decision or an old one that predates recent topics?).
    stored_assigned_at: str


@dataclass
class CollectionAssignmentDrift:
    """One source collection's probe result. ``error`` set means not probed."""

    collection: str
    #: Total chunk count in this T3 collection (context only).
    size: int
    #: Known population size: projection rows sourced from this collection
    #: (``HttpTaxonomyStore.get_projection_counts_by_collection``).
    projection_count: int
    #: Sampled rows actually compared against an eligible live foreign centroid.
    compared: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    #: Sampled candidate ids that carried no stored vector at this
    #: collection's dim (a re-embed in progress, or deleted mid-probe).
    no_vector: int = 0
    #: Sampled projection rows whose stored topic id has no live foreign
    #: centroid any more (a deleted topic) -- excluded from `compared`,
    #: never counted as a disagreement.
    no_foreign_centroids: int = 0
    #: True when this collection has a known projection population
    #: (``projection_count > 0``, the only reason it was probed at all)
    #: but this run's sample reached NONE of it -- a thin/low-density
    #: population the row-page cap could not realistically cover, or
    #: simple bad luck. Distinct from a clean pass AND from a failure:
    #: reported as INCONCLUSIVE, never folded into either.
    inconclusive: bool = False
    error: str | None = None


def _stored_pick(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The row representing the engine's MOST RECENTLY DECIDED 'projection'
    assignment among *rows* for one doc_id: max ``assigned_at`` (ties
    broken by higher recorded similarity, then lower topic id, purely for
    determinism -- true ties on ``assigned_at`` at one-second resolution
    are possible but rare).

    Deliberately NOT the highest-similarity row (see the module
    docstring's CONFLICT-KEY NOTE for why that rule is backwards for this
    question, even though it is the right rule for the ENGINE's own
    same-topic-id upsert conflict).
    """
    def _key(r: dict[str, Any]) -> tuple[str, float, int]:
        return (str(r.get("assigned_at") or ""), float(r.get("similarity") or -1.0), -int(r["topic_id"]))
    return max(rows, key=_key)


def probe_collection(
    taxo: Any, t3: Any, name: str, size: int, projection_count: int,
    sample: int, rng: random.Random, topic_created_at: dict[int, str],
) -> CollectionAssignmentDrift:
    """Sample *name*'s chunk ids, filter to those carrying a stored
    ``assigned_by='projection'`` row for THIS collection, recompute the
    exact nearest ELIGIBLE foreign centroid for each (eligible = the
    foreign topic already existed, per *topic_created_at*, at or before
    the stored pick's own ``assigned_at``), and compare.

    Any failure is recorded in ``error``; the collection then counts as
    not probed. A sample that turns up no projection row at all --
    despite a known nonzero *projection_count* (the only reason this
    function is ever called for *name*) -- sets ``inconclusive=True``
    with no error: this run's sample simply did not reach the known
    population, never a failure by itself.

    *topic_created_at* maps EVERY topic id tenant-wide (not just this
    collection's foreign set) to its ``created_at`` (UTC ISO-8601,
    explicit seconds -- the exact string shape ``HttpTaxonomyStore.
    get_all_topics``/``get_assignment_details`` both emit via the
    engine's shared ``UTC_SECOND`` formatter, so a plain string compare
    is a valid chronological compare). A topic id absent from the map
    (should not normally happen for a live centroid) is treated as
    ELIGIBLE by default -- the safer failure mode is to include an
    unknown-provenance candidate rather than silently exclude it and
    hide a real disagreement.
    """
    result = CollectionAssignmentDrift(collection=name, size=size, projection_count=projection_count)

    def _finish() -> CollectionAssignmentDrift:
        if result.error is None and result.compared == 0:
            result.inconclusive = True
        return result

    try:
        col = t3.get_or_create_collection(name)
        from nexus.db.limits import MAX_QUERY_RESULTS  # noqa: PLC0415 — deferred (db.limits)
        # Density-sized pool (round-1 finding (a)): draw enough candidate
        # chunk ids that, at this collection's KNOWN projection density,
        # we expect to land on `sample` actual projection rows -- not a
        # flat oversample factor blind to how sparse the population is.
        density = (projection_count / size) if size > 0 else 0.0
        target_pool = math.ceil(sample / density) if density > 0 else size
        pool = min(size, max(target_pool, sample * _MIN_POOL_MULTIPLE), MAX_QUERY_RESULTS)
        candidate_ids: list[str] = []
        for offset, limit in window_offsets(size, pool, rng):
            page = col.get(limit=limit, offset=offset)
            candidate_ids.extend(page.get("ids") or [])
        if not candidate_ids:
            return _finish()

        rows = taxo.get_assignment_details(candidate_ids)
        by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            if r.get("assigned_by") == "projection" and r.get("source_collection") == name:
                by_doc[r["doc_id"]].append(r)
        if not by_doc:
            return _finish()
        stored: dict[str, dict[str, Any]] = {doc_id: _stored_pick(rs) for doc_id, rs in by_doc.items()}

        chosen = sorted(stored)[:sample]
        vecs = t3.get_embeddings_by_id(name, chosen)
        found = [i for i in chosen if i in vecs]
        result.no_vector = len(chosen) - len(found)
        if not found:
            return _finish()

        dim = len(vecs[found[0]])
        foreign = taxo._centroid.get_foreign(name)
        centroids: dict[int, list[float]] = {}
        for emb, meta in zip(foreign.get("embeddings", []), foreign.get("metadatas", [])):
            if len(emb) == dim:
                centroids[int(meta["topic_id"])] = emb
        if not centroids:
            result.error = f"no live foreign centroid at dim {dim} for {name}"
            return _finish()

        for doc_id in found:
            vec = vecs[doc_id]
            stored_row = stored[doc_id]
            stored_topic_id = int(stored_row["topic_id"])
            cutoff = str(stored_row.get("assigned_at") or "")
            # Eligibility: a foreign topic discovered/rediscovered AFTER
            # this decision was made could not have been a candidate when
            # the engine's own LATERAL ran -- excluding it is what makes
            # this a same-moment comparison instead of "exact today vs
            # decided yesterday". The stored topic itself is ALWAYS
            # eligible (it was chosen, so it existed) even if its
            # created_at is missing from the map for some reason.
            eligible_ids = {tid for tid in centroids if topic_created_at.get(tid, "") <= cutoff}
            eligible_ids.add(stored_topic_id)
            sims = {tid: _cosine(vec, centroids[tid]) for tid in eligible_ids if tid in centroids}
            best_topic_id = min(sims, key=lambda tid: (-sims[tid], tid))
            best_sim = sims[best_topic_id]
            stored_sim = sims.get(stored_topic_id)
            if stored_sim is None:
                result.no_foreign_centroids += 1
                continue
            result.compared += 1
            gap = best_sim - stored_sim
            if best_topic_id != stored_topic_id and gap > SIMILARITY_TIE_TOLERANCE:
                result.disagreements.append(Disagreement(
                    doc_id=doc_id, stored_topic_id=stored_topic_id, stored_topic_similarity=stored_sim,
                    exact_topic_id=best_topic_id, exact_similarity=best_sim, gap=gap,
                    stored_assigned_at=cutoff,
                ))
    except Exception as exc:  # noqa: BLE001 — one collection's failure is reported, never hides the rest
        _log.debug("doctor_assignments_probe_failed", collection=name, error=str(exc))
        result.error = f"{type(exc).__name__}: {exc}"
    return _finish()


def probe_collections(
    taxo: Any, t3: Any, sizes: dict[str, int], projection_counts: dict[str, int],
    topic_created_at: dict[int, str], *, sample: int, seed: int,
) -> list[CollectionAssignmentDrift]:
    """Probe every collection in *sizes*, in name order.

    Each collection gets its own RNG derived from the seed and its name,
    so its sample does not depend on which other collections were probed.
    """
    names = sorted(sizes)

    def _one(name: str) -> CollectionAssignmentDrift:
        return probe_collection(
            taxo, t3, name, sizes[name], projection_counts.get(name, 0),
            sample, random.Random(f"{seed}:{name}"), topic_created_at,
        )

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        return list(pool.map(_one, names))


def format_report(
    results: list[CollectionAssignmentDrift], *, sample: int, seed: int,
    not_applicable: list[str] | None = None,
) -> tuple[list[str], bool]:
    """Human lines and whether the run is clean."""
    not_applicable = not_applicable or []
    lines: list[str] = []
    disagreed = [r for r in results if r.error is None and r.disagreements]
    failed = [r for r in results if r.error is not None]
    probed = [r for r in results if r.error is None and r.compared > 0]
    inconclusive = [r for r in results if r.error is None and r.inconclusive]
    total = sum(r.compared for r in probed)
    # A run that compared nothing has not shown anything is healthy.
    ok = not disagreed and not failed and total > 0

    mark = "✓" if ok else "✗"
    lines.append(
        f"[{mark}] Assignment drift: {total} assignment(s) compared in {len(probed)} "
        f"collection(s), tie tolerance {SIMILARITY_TIE_TOLERANCE}, sample {sample}, "
        f"seed {seed}; {len(disagreed)} collection(s) with a disagreement, "
        f"{len(failed)} not probed"
    )
    for r in disagreed:
        worst = ", ".join(
            f"{d.doc_id[:12]} stored={d.stored_topic_id} exact={d.exact_topic_id} gap={d.gap:.4f}"
            for d in sorted(r.disagreements, key=lambda d: -d.gap)[:_MAX_NAMED]
        )
        lines.append(
            f"      ✗ {r.collection}: {len(r.disagreements)}/{r.compared} disagree "
            f"({r.size} chunks, {r.projection_count} known projection row(s)); worst: {worst}"
        )
    for r in failed:
        lines.append(f"      ✗ {r.collection}: NOT PROBED ({r.error})")
    if inconclusive:
        lines.append(
            f"      INCONCLUSIVE: {len(inconclusive)} collection(s) have a known "
            "cross-collection projection population this sample did not reach (a thin "
            "population past the row-page cap, or the sampled candidates simply carried "
            "none) -- not a failure, not a clean pass: "
            + ", ".join(f"{r.collection} ({r.projection_count} known)" for r in inconclusive[:5])
            + (f" (+{len(inconclusive) - 5} more)" if len(inconclusive) > 5 else "")
        )
    no_vector = [r for r in results if r.error is None and r.no_vector]
    if no_vector:
        lines.append(
            f"      {sum(r.no_vector for r in no_vector)} sampled assignment(s) have no "
            "stored vector at the collection's dim (a re-embed in progress, or deleted "
            "mid-probe), not compared: "
            + ", ".join(f"{r.collection} ({r.no_vector})" for r in no_vector[:5])
        )
    missing_topic = [r for r in results if r.error is None and r.no_foreign_centroids]
    if missing_topic:
        lines.append(
            f"      {sum(r.no_foreign_centroids for r in missing_topic)} sampled "
            "assignment(s) reference a topic with no live foreign centroid at the "
            "sampled dim (a deleted topic), not compared: "
            + ", ".join(f"{r.collection} ({r.no_foreign_centroids})" for r in missing_topic[:5])
        )
    if not_applicable:
        lines.append(
            f"      not applicable: {len(not_applicable)} collection(s) have no "
            "cross-collection projection assignment to audit: "
            + ", ".join(not_applicable[:5])
            + (f" (+{len(not_applicable) - 5} more)" if len(not_applicable) > 5 else "")
        )
    if total == 0 and not failed:
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

    taxo = HttpTaxonomyStore()
    try:
        try:
            proj_counts = taxo.get_projection_counts_by_collection()
            # Fetched ONCE, tenant-wide: the eligibility cutoff (module
            # docstring) needs every topic's created_at, not just the
            # collections this run happens to probe -- a foreign topic
            # can live in ANY other collection.
            topic_created_at = {
                int(t["id"]): str(t.get("created_at") or "")
                for t in taxo.get_all_topics()
            }
        except Exception as exc:  # noqa: BLE001 — boundary: an unreadable taxonomy engine is a hard failure here
            click.echo(f"[✗] Assignment drift: taxonomy engine UNREADABLE ({type(exc).__name__}: {exc})")
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

        applicable = {n: listed[n] for n in names if proj_counts.get(n, 0) > 0}
        not_applicable = sorted(n for n in names if proj_counts.get(n, 0) == 0)

        if not applicable:
            click.echo(
                "[✓] Assignment drift: not applicable (no collection has a "
                "cross-collection projection assignment to audit)"
            )
            return

        results = probe_collections(
            taxo, t3, applicable, proj_counts, topic_created_at, sample=sample, seed=run_seed,
        )
        lines, ok = format_report(results, sample=sample, seed=run_seed, not_applicable=not_applicable)
        for line in lines:
            click.echo(line)
        if not ok:
            raise SystemExit(1)
    finally:
        taxo.close()
