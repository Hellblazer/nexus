# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx collection audit <name>`` — RDR-087 Phase 4.2.

Four sections:

1. **Distance histogram** — 10-bucket histogram of ``top_distance`` over
   the last 30 days from ``search_telemetry``. Live-probe fallback
   (N=25 queries against ChromaDB when telemetry is cold) is deferred
   to bead ``nexus-fx2d``; this module ships the telemetry-only path
   and reports ``source="empty"`` when cold.

2. **Top-5 cross-projections** — collections this one projects INTO.
   Aggregates ``topic_assignments`` WHERE ``source_collection=<name>``
   AND ``topics.collection != <name>``, ranks by
   ``shared_topics * avg_similarity``.

3. **Orphan chunks** — catalog documents in this collection with no
   incoming links AND ``indexed_at < now - 30d``.

4. **Hub-topic assignments** — top-10 cross-collection hubs (topics
   whose assignments span the most distinct source collections) and
   this collection's contribution to each.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DistanceHistogram:
    buckets: list[int]              # 10 counts over [0.0, 2.0] in 0.2 steps
    source: str                     # "telemetry" | "live" | "empty"
    sample_size: int


@dataclass(frozen=True)
class ProjectionPair:
    other_collection: str
    shared_topics: int
    avg_similarity: float

    @property
    def score(self) -> float:
        return self.shared_topics * self.avg_similarity


@dataclass(frozen=True)
class OrphanChunk:
    tumbler: str
    title: str
    indexed_at: str


@dataclass(frozen=True)
class HubAssignment:
    topic_id: int
    topic_label: str
    topic_collection: str
    source_collection_count: int    # # distinct source_collections across the hub
    chunks_in_hub: int              # this collection's chunks assigned to the hub


@dataclass(frozen=True)
class ChashCoverage:
    """RDR-087 Phase 4.6: chash_index coverage for *collection*.

    ``None`` fields distinguish "backfill needed" from "schema absent".
    The ``missing_sample`` is a best-effort list of up to 5 T3 chunk IDs
    whose ``chunk_text_hash`` metadata is not present in ``chash_index``
    — populated when 0 < ratio < 1.0. Empty when ratio is 1.0 or None.
    """
    total_chunks: int | None
    indexed_rows: int
    ratio: float | None
    missing_sample: list[str]


@dataclass(frozen=True)
class AuditReport:
    collection: str
    distance_histogram: DistanceHistogram
    cross_projections: list[ProjectionPair]
    orphans: list[OrphanChunk]
    hub_assignments: list[HubAssignment]
    chash_coverage: ChashCoverage | None = None


# ── Section 1: distance histogram (telemetry-only; live deferred) ───────────


_HIST_BIN_WIDTH = 0.2
_HIST_BINS = 10  # covers [0.0, 2.0]


def compute_distance_histogram(
    taxonomy_conn: sqlite3.Connection, collection: str, *, days: int = 30,
) -> DistanceHistogram:
    """Histogram of ``top_distance`` from search_telemetry for *collection*.

    10 fixed bins over [0.0, 2.0]. Empty table or <1 in-window rows →
    ``DistanceHistogram(source="empty", sample_size=0)``. Live-probe
    fallback is deferred to bead ``nexus-fx2d``.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = taxonomy_conn.execute(
        "SELECT top_distance FROM search_telemetry "
        "WHERE collection = ? AND ts >= ? "
        "AND raw_count > 0 AND top_distance IS NOT NULL",
        (collection, cutoff),
    ).fetchall()
    distances = [float(r[0]) for r in rows]
    if not distances:
        return DistanceHistogram(
            buckets=[0] * _HIST_BINS, source="empty", sample_size=0,
        )
    buckets = [0] * _HIST_BINS
    for d in distances:
        idx = min(int(d / _HIST_BIN_WIDTH), _HIST_BINS - 1)
        buckets[idx] += 1
    return DistanceHistogram(
        buckets=buckets, source="telemetry", sample_size=len(distances),
    )


# ── Section 2: top-N cross-projections ──────────────────────────────────────


def compute_cross_projections(
    taxonomy_conn: sqlite3.Connection, collection: str, *, top_n: int = 5,
) -> list[ProjectionPair]:
    """Top-*top_n* collections this one projects INTO.

    Ranked by ``shared_topics * avg_similarity``. Requires
    ``assigned_by='projection'`` rows with non-NULL ``similarity`` and
    ``source_collection``.
    """
    rows = taxonomy_conn.execute(
        "SELECT t.collection AS other, "
        "       COUNT(DISTINCT ta.topic_id) AS shared, "
        "       AVG(ta.similarity) AS avg_sim "
        "FROM topic_assignments ta "
        "JOIN topics t ON ta.topic_id = t.id "
        "WHERE ta.source_collection = ? "
        "  AND t.collection != ? "
        "  AND ta.similarity IS NOT NULL "
        "GROUP BY t.collection "
        "ORDER BY shared * AVG(ta.similarity) DESC "
        "LIMIT ?",
        (collection, collection, top_n),
    ).fetchall()
    return [
        ProjectionPair(
            other_collection=r[0],
            shared_topics=int(r[1]),
            avg_similarity=float(r[2]),
        )
        for r in rows
    ]


# ── Section 3: orphan chunks ────────────────────────────────────────────────


def compute_orphan_chunks(
    catalog_conn: sqlite3.Connection,
    collection: str,
    *,
    age_days: int = 30,
    limit: int = 20,
) -> list[OrphanChunk]:
    """Catalog documents with no incoming links older than *age_days*."""
    cutoff = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    rows = catalog_conn.execute(
        "SELECT d.tumbler, d.title, d.indexed_at "
        "FROM documents d "
        "LEFT JOIN links l ON d.tumbler = l.to_tumbler "
        "WHERE d.physical_collection = ? "
        "  AND l.id IS NULL "
        "  AND d.indexed_at IS NOT NULL "
        "  AND d.indexed_at < ? "
        "ORDER BY d.indexed_at ASC "
        "LIMIT ?",
        (collection, cutoff, limit),
    ).fetchall()
    return [
        OrphanChunk(tumbler=r[0], title=r[1] or "", indexed_at=r[2] or "")
        for r in rows
    ]


# ── Section 4: hub-topic assignments ────────────────────────────────────────


def compute_hub_assignments(
    taxonomy_conn: sqlite3.Connection, collection: str, *, top_n: int = 10,
) -> list[HubAssignment]:
    """Top-*top_n* cross-collection hub topics and this collection's share."""
    hubs = taxonomy_conn.execute(
        "SELECT ta.topic_id, COUNT(DISTINCT ta.source_collection) AS src_count "
        "FROM topic_assignments ta "
        "GROUP BY ta.topic_id "
        "ORDER BY src_count DESC, ta.topic_id ASC "
        "LIMIT ?",
        (top_n,),
    ).fetchall()
    if not hubs:
        return []
    out: list[HubAssignment] = []
    for topic_id, src_count in hubs:
        meta = taxonomy_conn.execute(
            "SELECT label, collection FROM topics WHERE id = ?",
            (topic_id,),
        ).fetchone()
        if meta is None:
            continue
        label, topic_collection = meta
        chunk_count = taxonomy_conn.execute(
            "SELECT COUNT(*) FROM topic_assignments "
            "WHERE topic_id = ? AND source_collection = ?",
            (topic_id, collection),
        ).fetchone()[0]
        out.append(
            HubAssignment(
                topic_id=int(topic_id),
                topic_label=label or "",
                topic_collection=topic_collection or "",
                source_collection_count=int(src_count),
                chunks_in_hub=int(chunk_count or 0),
            )
        )
    return out


# ── Default production runners (dep-injected) ───────────────────────────────


def _open_t2():
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    db_path = default_db_path()
    if not db_path.exists():
        return None
    return T2Database(db_path)


def _open_catalog_conn() -> sqlite3.Connection | None:
    """Return a sqlite3 connection to the catalog cache DB.

    Tests monkeypatch this module-level function to point at a seeded
    fixture; production reaches to ``~/.config/nexus`` by default.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        return None
    return sqlite3.connect(str(path / ".catalog.db"))


# ── Section 5: chash_index coverage (RDR-087 Phase 4.6 / nexus-c2op) ────────


def compute_chash_coverage(collection: str) -> ChashCoverage | None:
    """Report chash_index coverage for *collection*.

    Composes (1) ``COUNT(*) FROM chash_index WHERE physical_collection = ?``
    for indexed rows, (2) ``col.count()`` on the T3 collection for total
    chunks, (3) a best-effort sampled T3 ``get(where={'chunk_text_hash':
    ...})`` walk to produce up to 5 IDs whose hash is not registered in
    T2 — observable "run backfill" evidence without chunk-by-chunk
    inspection.

    Returns ``None`` when either side of the ratio is unreachable
    (T2 file missing, T3 unavailable); calling code treats this
    the same as ratio=None (schema absent vs backfill needed).
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db import make_t3
    from nexus.db.t2.chash_index import ChashIndex

    db_path = default_db_path()
    if not db_path.exists():
        return None

    idx = ChashIndex(db_path)
    try:
        with idx._lock:
            row = idx.conn.execute(
                "SELECT COUNT(*) FROM chash_index "
                "WHERE physical_collection = ?",
                (collection,),
            ).fetchone()
        indexed_rows = int(row[0] or 0)
    finally:
        idx.close()

    try:
        t3 = make_t3()
        col = t3.get_or_create_collection(collection)
        total_chunks = col.count()
    except Exception:
        return ChashCoverage(
            total_chunks=None,
            indexed_rows=indexed_rows,
            ratio=None,
            missing_sample=[],
        )

    if total_chunks == 0:
        return ChashCoverage(
            total_chunks=0, indexed_rows=indexed_rows,
            ratio=None, missing_sample=[],
        )

    ratio = min(1.0, indexed_rows / total_chunks)

    # Sample missing chunks only when there's actually a gap. Bounded at
    # 5 to keep the audit cheap; the operator uses nx collection
    # backfill-hash for the real fix.
    missing: list[str] = []
    if ratio < 1.0:
        try:
            # Pull up to MAX_QUERY_RESULTS=300 chunks' ids + hashes from
            # T3 and cross-check against the chash_index. One get() is
            # bounded by the ChromaDB quota.
            page = col.get(limit=300, include=["metadatas"])
            ids = page.get("ids") or []
            metadatas = page.get("metadatas") or []
            if ids and metadatas:
                # Re-open chash_index for the lookup pass.
                probe = ChashIndex(db_path)
                try:
                    with probe._lock:
                        placeholders = ",".join("?" * len(ids))
                        indexed = {
                            r[0] for r in probe.conn.execute(
                                f"SELECT doc_id FROM chash_index "
                                f"WHERE physical_collection = ? "
                                f"AND doc_id IN ({placeholders})",
                                (collection, *ids),
                            ).fetchall()
                        }
                finally:
                    probe.close()
                for cid in ids:
                    if cid not in indexed:
                        missing.append(cid)
                    if len(missing) >= 5:
                        break
        except Exception:
            missing = []  # best-effort: ratio still meaningful

    return ChashCoverage(
        total_chunks=total_chunks,
        indexed_rows=indexed_rows,
        ratio=ratio,
        missing_sample=missing,
    )


# ── Orchestrator ────────────────────────────────────────────────────────────


def run_collection_audit(collection: str) -> AuditReport:
    """Assemble the full audit report for *collection*.

    Sections tolerate absent backing stores (empty-telemetry / uninit
    catalog) — each falls back to a neutral empty value.
    """
    t2 = _open_t2()
    cat_conn = _open_catalog_conn()
    try:
        if t2 is not None:
            hist = compute_distance_histogram(t2.taxonomy.conn, collection)
            projections = compute_cross_projections(t2.taxonomy.conn, collection)
            hubs = compute_hub_assignments(t2.taxonomy.conn, collection)
        else:
            hist = DistanceHistogram(buckets=[0] * _HIST_BINS, source="empty", sample_size=0)
            projections = []
            hubs = []
        if cat_conn is not None:
            orphans = compute_orphan_chunks(cat_conn, collection)
        else:
            orphans = []
    finally:
        if t2 is not None:
            t2.close()
        if cat_conn is not None:
            cat_conn.close()
    # RDR-087 Phase 4.6 (nexus-c2op): chash coverage section. Own error
    # boundary — the rest of the audit is purely T2, chash coverage hits
    # T3, so failures (missing collection, network) shouldn't lose the
    # other sections.
    try:
        chash = compute_chash_coverage(collection)
    except Exception:
        chash = None
    return AuditReport(
        collection=collection,
        distance_histogram=hist,
        cross_projections=projections,
        orphans=orphans,
        hub_assignments=hubs,
        chash_coverage=chash,
    )


# ── Formatters ──────────────────────────────────────────────────────────────


def format_audit_human(report: AuditReport) -> str:
    lines: list[str] = [f"Audit: {report.collection}", ""]
    # Section 1
    lines.append("=== distance histogram (30d) ===")
    h = report.distance_histogram
    if h.sample_size == 0:
        lines.append(
            f"  (no telemetry rows; live-probe fallback deferred — nexus-fx2d)"
        )
    else:
        lines.append(f"  source={h.source} samples={h.sample_size}")
        for i, count in enumerate(h.buckets):
            lo = i * _HIST_BIN_WIDTH
            hi = lo + _HIST_BIN_WIDTH
            bar = "▇" * count if count else ""
            lines.append(f"  [{lo:.1f}, {hi:.1f})  {count:>5}  {bar}")
    lines.append("")
    # Section 2
    lines.append("=== top-5 cross-projections ===")
    if not report.cross_projections:
        lines.append("  (no projection rows for this collection)")
    else:
        for p in report.cross_projections:
            lines.append(
                f"  → {p.other_collection:<40}  "
                f"shared={p.shared_topics:>4}  "
                f"avg_sim={p.avg_similarity:.3f}  "
                f"score={p.score:.3f}"
            )
    lines.append("")
    # Section 3
    lines.append("=== orphan chunks (>30d, no incoming links) ===")
    if not report.orphans:
        lines.append("  (none)")
    else:
        for o in report.orphans:
            lines.append(f"  {o.tumbler:<10}  {o.indexed_at:<25}  {o.title}")
    lines.append("")
    # Section 4
    lines.append("=== top-10 cross-collection hubs ===")
    if not report.hub_assignments:
        lines.append("  (no hub signals)")
    else:
        for h_ in report.hub_assignments:
            lines.append(
                f"  topic#{h_.topic_id:<5} {h_.topic_label:<30} "
                f"({h_.topic_collection})  "
                f"srcs={h_.source_collection_count:>3}  "
                f"this_col_chunks={h_.chunks_in_hub:>4}"
            )
    lines.append("")
    # Section 5: chash_index coverage (RDR-087 Phase 4.6 / nexus-c2op)
    lines.append("=== chash_index coverage ===")
    cov = report.chash_coverage
    if cov is None:
        lines.append(
            "  (chash_index unavailable — T2 missing or T3 unreachable)"
        )
    elif cov.total_chunks == 0 or cov.total_chunks is None:
        lines.append(
            f"  indexed_rows={cov.indexed_rows}  (collection has no T3 chunks)"
        )
    else:
        ratio_pct = 100.0 * (cov.ratio or 0.0)
        lines.append(
            f"  total_chunks={cov.total_chunks}  "
            f"indexed_rows={cov.indexed_rows}  "
            f"ratio={cov.ratio:.3f} ({ratio_pct:.1f}%)"
        )
        if cov.ratio is not None and cov.ratio < 1.0:
            lines.append(
                "  Run `nx collection backfill-hash "
                f"{report.collection}` to close the gap."
            )
            if cov.missing_sample:
                lines.append("  Sample unindexed chunk IDs:")
                for cid in cov.missing_sample:
                    lines.append(f"    - {cid}")
    return "\n".join(lines)


def format_audit_json(report: AuditReport) -> str:
    """Serialise the audit report as JSON.

    Schema review I-2: the ``distance_histogram.buckets`` field is a
    bare list of counts; the bin edges aren't recoverable from the
    payload. Add an explicit ``bin_edges`` sibling so downstream
    consumers (dashboards, agent tools) can reconstruct bucket
    boundaries unambiguously. Edges are left-closed / right-open
    except for the last bucket which is inclusive at the upper bound.
    """
    data = asdict(report)
    hist = data.get("distance_histogram")
    if isinstance(hist, dict) and isinstance(hist.get("buckets"), list):
        n = len(hist["buckets"])
        hist["bin_edges"] = [
            [round(i * _HIST_BIN_WIDTH, 4),
             round((i + 1) * _HIST_BIN_WIDTH, 4)]
            for i in range(n)
        ]
        hist["bin_inclusivity"] = "left-closed, right-open (last bucket inclusive)"
    return json.dumps(data, indent=2)
