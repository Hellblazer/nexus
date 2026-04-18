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
import sqlite3
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


def _top_hub_topic_ids(
    conn: sqlite3.Connection, hub_top_n: int,
) -> tuple[int, ...]:
    rows = conn.execute(
        "SELECT ta.topic_id "
        "FROM topic_assignments ta "
        "GROUP BY ta.topic_id "
        "ORDER BY COUNT(DISTINCT ta.source_collection) DESC, ta.topic_id ASC "
        "LIMIT ?",
        (hub_top_n,),
    ).fetchall()
    return tuple(int(r[0]) for r in rows)


def compute_merge_candidates(
    conn: sqlite3.Connection,
    *,
    min_shared: int = 3,
    min_similarity: float = 0.5,
    exclude_hubs: bool = False,
    hub_top_n: int = _DEFAULT_HUB_TOP_N,
    limit: int = 50,
    sample_k: int = 3,
) -> list[MergeCandidate]:
    """Return ranked (a, b) cross-collection merge candidates."""
    hub_filter = ""
    hub_params: tuple[int, ...] = ()
    if exclude_hubs:
        hub_ids = _top_hub_topic_ids(conn, hub_top_n)
        if hub_ids:
            placeholders = ",".join("?" * len(hub_ids))
            hub_filter = f" AND ta.topic_id NOT IN ({placeholders})"
            hub_params = hub_ids

    sql = f"""
        SELECT ta.source_collection AS a,
               t.collection AS b,
               COUNT(DISTINCT ta.topic_id) AS shared,
               AVG(ta.similarity) AS mean_sim
        FROM topic_assignments ta
        JOIN topics t ON ta.topic_id = t.id
        WHERE ta.source_collection IS NOT NULL
          AND ta.source_collection <> t.collection
          AND ta.similarity IS NOT NULL
          {hub_filter}
        GROUP BY ta.source_collection, t.collection
        HAVING shared >= ? AND mean_sim >= ?
        ORDER BY shared * mean_sim DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (*hub_params, min_shared, min_similarity, limit)).fetchall()
    out: list[MergeCandidate] = []
    for a, b, shared, mean_sim in rows:
        samples = [
            r[0] for r in conn.execute(
                "SELECT ta.doc_id "
                "FROM topic_assignments ta "
                "JOIN topics t ON ta.topic_id = t.id "
                "WHERE ta.source_collection = ? AND t.collection = ? "
                "  AND ta.similarity IS NOT NULL "
                f"  {hub_filter} "
                "ORDER BY ta.similarity DESC "
                "LIMIT ?",
                (a, b, *hub_params, sample_k),
            ).fetchall()
        ]
        out.append(
            MergeCandidate(
                a=a, b=b,
                shared_topics=int(shared),
                mean_sim=float(mean_sim),
                sample_chunks=samples,
            )
        )
    return out


# ── Default production runners ──────────────────────────────────────────────


def _open_t2():
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    db_path = default_db_path()
    if not db_path.exists():
        return None
    return T2Database(db_path)


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
            t2.taxonomy.conn,
            min_shared=min_shared,
            min_similarity=min_similarity,
            exclude_hubs=exclude_hubs,
            hub_top_n=hub_top_n,
            limit=limit,
        )
    finally:
        t2.close()
    if fmt == "json":
        return json.dumps(
            {
                "candidates": [asdict(p) for p in pairs],
                "filters": {
                    "min_shared": min_shared,
                    "min_similarity": min_similarity,
                    "exclude_hubs": exclude_hubs,
                    "hub_top_n": hub_top_n if exclude_hubs else None,
                    "limit": limit,
                },
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
