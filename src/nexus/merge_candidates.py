# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx collection merge-candidates`` — RDR-087 Phase 4.3.

Pair-wise cross-collection overlap via ``topic_assignments``. Ranks
(source_collection, topics.collection) pairs by
``shared_topics * mean_similarity``.

Null ``source_collection`` rows are excluded (legacy pre-RDR-077
rows). Self-pairs (``source_collection == topics.collection``) are
excluded — those are same-collection assignments, not cross-collection
merge candidates. ``--exclude-hubs`` subtracts the top-N hub topics
(widest ``source_collection`` spread) from the shared-topic count to
mitigate false positives from generic hubs like "API rate limiting"
or "schema evolution".

Catalog link creation (``--create-link``) is explicitly deferred per
RDR §bridge-link workflow. This bead surfaces the candidates; the
human / agent decides.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class MergeCandidate:
    a: str                       # source_collection
    b: str                       # topic's collection
    shared_topics: int
    mean_sim: float
    sample_chunks: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.shared_topics * self.mean_sim


# ── Core query ──────────────────────────────────────────────────────────────


_DEFAULT_HUB_TOP_N = 10


def compute_merge_candidates(
    taxonomy,
    *,
    min_shared: int = 3,
    min_similarity: float = 0.5,
    exclude_hubs: bool = False,
    hub_top_n: int = _DEFAULT_HUB_TOP_N,
    limit: int = 50,
    sample_k: int = 3,
) -> list[MergeCandidate]:
    """Return ranked (a, b) cross-collection merge candidates.

    Accepts a ``CatalogTaxonomy`` store (was the raw connection in
    pre-RDR-112-P0-gate code). The pair-pruning + sample-fetch SQL is
    encapsulated in
    :meth:`CatalogTaxonomy.query_merge_candidate_pairs` and
    :meth:`CatalogTaxonomy.sample_merge_candidate_doc_ids`.

    Symmetric-pair normalisation (review I-3): the underlying query
    enforces ``source_collection < t.collection`` so each symmetric
    pair appears exactly once, regardless of which projection direction
    was populated first.
    """
    excluded: tuple[int, ...] = ()
    if exclude_hubs:
        excluded = tuple(
            tid for tid, _ in taxonomy.query_hub_topic_ids(limit=hub_top_n)
        )

    rows = taxonomy.query_merge_candidate_pairs(
        min_shared=min_shared,
        min_similarity=min_similarity,
        excluded_topic_ids=excluded,
        limit=limit,
    )
    out: list[MergeCandidate] = []
    for a, b, shared, mean_sim in rows:
        samples = taxonomy.sample_merge_candidate_doc_ids(
            source_collection=a,
            target_collection=b,
            excluded_topic_ids=excluded,
            sample_k=sample_k,
        )
        out.append(
            MergeCandidate(
                a=a, b=b,
                shared_topics=shared,
                mean_sim=mean_sim,
                sample_chunks=samples,
            )
        )
    return out


# ── Default production runners ──────────────────────────────────────────────


def _open_t2():
    from nexus.config import default_db_path
    from nexus.mcp_infra import t2_ctx

    db_path = default_db_path()
    if not db_path.exists():
        return None
    return t2_ctx()


# ── CLI entry point ─────────────────────────────────────────────────────────


def run_merge_candidates(
    *,
    min_shared: int,
    min_similarity: float,
    exclude_hubs: bool,
    hub_top_n: int,
    limit: int,
    fmt: str,
) -> str:
    t2 = _open_t2()
    if t2 is None:
        return "T2 database not initialised."
    try:
        pairs = compute_merge_candidates(
            t2.taxonomy,
            min_shared=min_shared,
            min_similarity=min_similarity,
            exclude_hubs=exclude_hubs,
            hub_top_n=hub_top_n,
            limit=limit,
        )
    finally:
        t2.close()
    if fmt == "json":
        # Schema review I-3: omit hub_top_n entirely when exclude_hubs
        # is False rather than emitting `null`. Agents parsing the
        # schema no longer need to special-case a null sentinel.
        filters: dict = {
            "min_shared": min_shared,
            "min_similarity": min_similarity,
            "exclude_hubs": exclude_hubs,
            "limit": limit,
        }
        if exclude_hubs:
            filters["hub_top_n"] = hub_top_n
        return json.dumps(
            {
                "candidates": [asdict(p) for p in pairs],
                "filters": filters,
            },
            indent=2,
        )
    return _format_human(pairs, exclude_hubs=exclude_hubs)


def _format_human(
    pairs: list[MergeCandidate], *, exclude_hubs: bool,
) -> str:
    if not pairs:
        return "No merge candidates above the configured thresholds."
    lines = ["Merge candidates — pair-wise cross-collection overlap"]
    if exclude_hubs:
        lines.append("  (top-N hub topics excluded)")
    lines.append("")
    lines.append(
        f"  {'a':<35}  {'b':<35}  {'shared':>7}  "
        f"{'mean_sim':>9}  {'score':>7}  samples"
    )
    for p in pairs:
        samples_str = (",".join(p.sample_chunks[:3]))[:45]
        lines.append(
            f"  {p.a:<35}  {p.b:<35}  {p.shared_topics:>7}  "
            f"{p.mean_sim:>9.3f}  {p.score:>7.2f}  {samples_str}"
        )
    return "\n".join(lines)
