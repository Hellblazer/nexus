# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-assignments``: does each SAMPLED cross-collection
("projection") topic assignment agree with an exact recompute?

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

This probe samples chunks that carry a stored ``assigned_by='projection'``
row, re-fetches their stored vector (``HttpVectorClient.
get_embeddings_by_id``) and every live foreign centroid
(``HttpCentroidStore.get_foreign``), and recomputes the exact nearest
centroid with the SAME tie-break the engine's own LATERAL uses
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
"not applicable", never as a failure by itself -- there is nothing wrong
to detect when there is no cross-collection assignment to have gotten
wrong.

CONFLICT-KEY NOTE (read before touching the "stored pick" logic): the
engine's ``topic_assignments`` unique constraint is
``(tenant_id, doc_id, topic_id)``, NOT ``(tenant_id, doc_id)`` -- a chunk
reassigned to a DIFFERENT topic across two ``assign_from_chashes`` runs
(e.g. after a centroid rebuild) accumulates a SECOND 'projection' row
rather than overwriting the first; the old row is not implicitly
retracted (see ``HttpTaxonomyStore.prune_projection_below``, the
explicit remedy for exactly this). This probe therefore treats the
HIGHEST-similarity 'projection' row per sampled doc_id as the "current"
stored pick -- the same GREATEST(similarity)-wins rule the engine's own
upsert applies on a same-topic conflict, generalised across topics.
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

#: Similarity gap above which a disagreement is real rather than float
#: noise between this probe's pure-Python cosine and the engine's pgvector
#: computation of the SAME vector pair. The two independently compute the
#: same dot products in a different summation order/precision over
#: (already float32-precision) stored embeddings, so a genuine tie can
#: read as a hairline difference of either sign; a real ANN/exact
#: divergence (a different topic actually chosen) shows a gap many orders
#: of magnitude larger than float64 roundoff on a few hundred to a
#: thousand summed terms. Exact float ties are not disagreements.
SIMILARITY_TIE_TOLERANCE = 1e-6
#: Chunks sampled per collection when the caller names no size.
DEFAULT_SAMPLE = 20
#: Candidate chunk pool multiplier: not every sampled chunk carries a
#: projection assignment (a below-similarity-floor chunk, or a
#: single-collection tenant, gets none), so the candidate pool is
#: oversampled before filtering down to `sample` actual projection rows.
_POOL_FACTOR = 5
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
    """One sampled chunk whose exact-recomputed nearest foreign topic
    differs from its stored ``assigned_by='projection'`` pick by more
    than :data:`SIMILARITY_TIE_TOLERANCE`."""

    doc_id: str
    stored_topic_id: int
    stored_similarity: float
    exact_topic_id: int
    exact_similarity: float
    gap: float


@dataclass
class CollectionAssignmentDrift:
    """One source collection's probe result. ``error`` set means not probed."""

    collection: str
    #: Total chunk count in this T3 collection (context only).
    size: int
    #: Known population size: projection rows sourced from this collection
    #: (``HttpTaxonomyStore.get_projection_counts_by_collection``).
    projection_count: int
    #: Sampled rows actually compared against a live foreign centroid.
    compared: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    #: Sampled candidate ids that carried no stored vector at this
    #: collection's dim (a re-embed in progress, or deleted mid-probe).
    no_vector: int = 0
    #: Sampled projection rows whose stored topic id has no live foreign
    #: centroid any more (a deleted or rebuilt topic) -- excluded from
    #: `compared`, never counted as a disagreement.
    no_foreign_centroids: int = 0
    error: str | None = None


def _stored_pick(rows: list[dict[str, Any]]) -> tuple[int, float]:
    """The CANONICAL stored projection pick among *rows* for one doc_id:
    highest similarity, ties broken by the LOWER topic id (mirrors the
    engine's own ``ORDER BY distance, topic_id`` preference). See the
    module docstring's CONFLICT-KEY NOTE for why more than one row can
    exist for the same doc_id.
    """
    best = max(rows, key=lambda r: (float(r.get("similarity") or -1.0), -int(r["topic_id"])))
    return int(best["topic_id"]), float(best.get("similarity") or -1.0)


def probe_collection(
    taxo: Any, t3: Any, name: str, size: int, projection_count: int,
    sample: int, rng: random.Random,
) -> CollectionAssignmentDrift:
    """Sample *name*'s chunk ids, filter to those carrying a stored
    ``assigned_by='projection'`` row for THIS collection, recompute the
    exact nearest foreign centroid for each, and compare.

    Any failure is recorded in ``error``; the collection then counts as
    not probed. A sample that turns up no projection row at all (an
    unlucky window, or a collection whose population is thinner than the
    sample) returns with ``compared == 0`` and no error -- reported as an
    empty probe, not a failure, by :func:`format_report`.
    """
    result = CollectionAssignmentDrift(collection=name, size=size, projection_count=projection_count)
    try:
        col = t3.get_or_create_collection(name)
        from nexus.db.limits import MAX_QUERY_RESULTS  # noqa: PLC0415 — deferred (db.limits)
        pool = min(size, max(sample * _POOL_FACTOR, sample), MAX_QUERY_RESULTS)
        candidate_ids: list[str] = []
        for offset, limit in window_offsets(size, pool, rng):
            page = col.get(limit=limit, offset=offset)
            candidate_ids.extend(page.get("ids") or [])
        if not candidate_ids:
            return result

        rows = taxo.get_assignment_details(candidate_ids)
        by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            if r.get("assigned_by") == "projection" and r.get("source_collection") == name:
                by_doc[r["doc_id"]].append(r)
        if not by_doc:
            return result
        stored: dict[str, tuple[int, float]] = {doc_id: _stored_pick(rs) for doc_id, rs in by_doc.items()}

        chosen = sorted(stored)[:sample]
        vecs = t3.get_embeddings_by_id(name, chosen)
        found = [i for i in chosen if i in vecs]
        result.no_vector = len(chosen) - len(found)
        if not found:
            return result

        dim = len(vecs[found[0]])
        foreign = taxo._centroid.get_foreign(name)
        centroids: dict[int, list[float]] = {}
        for emb, meta in zip(foreign.get("embeddings", []), foreign.get("metadatas", [])):
            if len(emb) == dim:
                centroids[int(meta["topic_id"])] = emb
        if not centroids:
            result.error = f"no live foreign centroid at dim {dim} for {name}"
            return result

        for doc_id in found:
            vec = vecs[doc_id]
            stored_topic_id, _stored_recorded_similarity = stored[doc_id]
            sims = {tid: _cosine(vec, emb) for tid, emb in centroids.items()}
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
                    doc_id=doc_id, stored_topic_id=stored_topic_id, stored_similarity=stored_sim,
                    exact_topic_id=best_topic_id, exact_similarity=best_sim, gap=gap,
                ))
    except Exception as exc:  # noqa: BLE001 — one collection's failure is reported, never hides the rest
        _log.debug("doctor_assignments_probe_failed", collection=name, error=str(exc))
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def probe_collections(
    taxo: Any, t3: Any, sizes: dict[str, int], projection_counts: dict[str, int],
    *, sample: int, seed: int,
) -> list[CollectionAssignmentDrift]:
    """Probe every collection in *sizes*, in name order.

    Each collection gets its own RNG derived from the seed and its name,
    so its sample does not depend on which other collections were probed.
    """
    names = sorted(sizes)

    def _one(name: str) -> CollectionAssignmentDrift:
        return probe_collection(
            taxo, t3, name, sizes[name], projection_counts.get(name, 0),
            sample, random.Random(f"{seed}:{name}"),
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
    empty = [r for r in results if r.error is None and r.compared == 0]
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
    if empty:
        lines.append(
            f"      NOT CHECKED: {len(empty)} collection(s) gave no comparable projection "
            "row in the sample (no stored vector, a deleted foreign topic, or the sampled "
            "candidates simply carried none): "
            + ", ".join(r.collection for r in empty[:5])
            + (f" (+{len(empty) - 5} more)" if len(empty) > 5 else "")
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
            "sampled dim (a deleted or rebuilt topic), not compared: "
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

        results = probe_collections(taxo, t3, applicable, proj_counts, sample=sample, seed=run_seed)
        lines, ok = format_report(results, sample=sample, seed=run_seed, not_applicable=not_applicable)
        for line in lines:
            click.echo(line)
        if not ok:
            raise SystemExit(1)
    finally:
        taxo.close()
