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

import structlog

_log = structlog.get_logger(__name__)


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
class AuditReport:
    collection: str
    distance_histogram: DistanceHistogram
    cross_projections: list[ProjectionPair]
    orphans: list[OrphanChunk]
    hub_assignments: list[HubAssignment]
    #: nexus-kmo9h (critic item 2): False when the orphan leg never ran —
    #: service mode (the local .catalog.db is a frozen migration source) or
    #: no local catalog. Distinguishes "checked, clean" from "couldn't
    #: check" in the render; the DistanceHistogram source="empty" idiom.
    orphans_checked: bool = True


# ── Section 1: distance histogram (telemetry primary, live fallback) ────────


_HIST_BIN_WIDTH = 0.2
_HIST_BINS = 10  # covers [0.0, 2.0]
_LIVE_PROBE_DEFAULT_N = 25


def _bucketize(distances: list[float], source: str) -> DistanceHistogram:
    """Pack *distances* into the 10-bin 0.0-2.0 histogram."""
    buckets = [0] * _HIST_BINS
    for d in distances:
        idx = min(max(int(d / _HIST_BIN_WIDTH), 0), _HIST_BINS - 1)
        buckets[idx] += 1
    return DistanceHistogram(
        buckets=buckets, source=source, sample_size=len(distances),
    )


def sample_live_distances(
    collection: str, t3: Any, *, n: int = _LIVE_PROBE_DEFAULT_N,
) -> list[float]:
    """Run up to *n* self-queries against *collection* and return the
    nearest-other distances (nexus-fx2d).

    Samples N chunk embeddings directly from the collection via
    ``col.get(include=["embeddings"])``, then for each runs
    ``col.query(query_embeddings=[emb], n_results=2)`` and records the
    distance at position ``[1]`` — position ``[0]`` is the chunk itself.
    No re-embedding, no Voyage API roundtrips: we reuse the vectors
    already in ChromaDB. Budget: ~10 s for N=25 against cloud under
    light contention.

    Returns a possibly-shorter list if the collection has fewer than
    *n* chunks, or if individual probes return < 2 neighbours (solo
    chunks). Caller decides what to do with a sparse sample.
    """
    col = t3.get_or_create_collection(collection)
    try:
        got = col.get(limit=n, include=["embeddings"])
    except Exception as _exc:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
        # nexus-ou4tb walk: a degraded vector service gets a WARNING (the
        # empty histogram it produces is otherwise identical to a genuinely
        # empty collection); other sampling failures keep the original
        # DEBUG posture (Reviewer B/S-1 + C/S-2).
        from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred import; rare/branch-local path

        _emit = _log.warning if isinstance(_exc, VectorServiceError) else _log.debug
        _emit(
            "sample_live_distances_failed",
            collection=collection, n=n, exc_info=True,
        )
        return []
    # ChromaDB returns embeddings as a numpy ndarray; guard the
    # "truth-value ambiguous" trap with an explicit ``is None`` + length
    # check, not a boolean collapse.
    embeddings_raw = got.get("embeddings")
    if embeddings_raw is None or len(embeddings_raw) == 0:
        return []
    embeddings = embeddings_raw

    distances: list[float] = []
    # nexus-8g79.8: pre-fix the bare ``continue`` silently dropped any
    # query failure — operators saw a sparse/short histogram that
    # looked identical to "healthy sparse collection". Count failures
    # so they surface in the structured log + audit-result summary.
    failed = 0
    for emb in embeddings:
        try:
            res = col.query(
                query_embeddings=[emb], n_results=2, include=["distances"],
            )
        except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
            failed += 1
            _log.debug(
                "sample_live_distances_query_failed",
                collection=collection, exc_info=True,
            )
            continue
        d_rows = res.get("distances") or [[]]
        if not d_rows or not d_rows[0] or len(d_rows[0]) < 2:
            continue
        distances.append(float(d_rows[0][1]))
    if failed > 0:
        _log.warning(
            "sample_live_distances_partial_failure",
            collection=collection,
            failed=failed,
            sampled=len(embeddings),
            surviving_distances=len(distances),
        )
    return distances


def compute_live_distance_histogram(
    collection: str, t3: Any, *, n: int = _LIVE_PROBE_DEFAULT_N,
) -> DistanceHistogram:
    """Build a histogram from :func:`sample_live_distances` (nexus-fx2d).

    Distinct ``source="live"`` marker so downstream consumers can see
    the audit hit ChromaDB, not search_telemetry. Cold collection (no
    chunks, all probes failed) → ``source="empty"`` with sample_size=0
    — identical to the telemetry-empty case so formatters only need
    one branch.
    """
    distances = sample_live_distances(collection, t3, n=n)
    if not distances:
        return DistanceHistogram(
            buckets=[0] * _HIST_BINS, source="empty", sample_size=0,
        )
    return _bucketize(distances, "live")


# ── Section 2: top-N cross-projections ──────────────────────────────────────


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


def _open_catalog_conn() -> sqlite3.Connection | None:
    """Return ``None`` — the local catalog cache DB no longer exists.

    nexus-e9ru2 / nexus-i711w: the catalog is service-owned; the audit's
    catalog legs DEGRADE (orphans=[]) rather than read a local substrate,
    and the degradation is REPORTED, not silent (the item-17 contract,
    tests/test_i711w_gap_item17_orphan_audit.py). Tests monkeypatch this
    module-level function to point at a seeded fixture — that seam is why
    the function survives the local catalog's deletion.
    """
    return None


# ── Orchestrator ────────────────────────────────────────────────────────────


def run_collection_audit(
    collection: str,
    *,
    live: bool = False,
    t3: Any = None,
    live_n: int = _LIVE_PROBE_DEFAULT_N,
) -> AuditReport:
    """Assemble the full audit report for *collection*.

    Sections tolerate absent backing stores (empty-telemetry / uninit
    catalog) — each falls back to a neutral empty value.

    nexus-fx2d: when *live* is True and the telemetry histogram is
    ``source="empty"``, run :func:`compute_live_distance_histogram`
    against *t3* (resolved via :func:`nexus.db.make_t3` when ``None``).
    Budget: ~10 s for N=25 probes against cloud T3.

    The telemetry-histogram / cross-projection / hub-assignment sections
    are always their neutral empty values now: they were raw SQL over the
    local SQLite taxonomy/telemetry tables, which were deleted in the
    RDR-158 P4 retirement, and the engine does not expose those aggregates
    yet (nexus-i711w.1 GAP; degrade-to-absent per nexus-9613q.4 — these
    are diagnostic enrichment sections, and the live-probe fallback below
    still produces a real histogram on request).
    """
    cat_conn = _open_catalog_conn()
    try:
        hist = DistanceHistogram(buckets=[0] * _HIST_BINS, source="empty", sample_size=0)
        projections = []
        hubs = []
        if cat_conn is not None:
            orphans = compute_orphan_chunks(cat_conn, collection)
            orphans_checked = True
        else:
            orphans = []
            orphans_checked = False
    finally:
        if cat_conn is not None:
            cat_conn.close()

    # Live-probe fallback — only when telemetry came back empty AND
    # caller opted in. Own error boundary: a T3 hiccup shouldn't blank
    # the telemetry/projection/hub results.
    if live and hist.source == "empty":
        try:
            if t3 is None:
                from nexus.db import make_t3  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
                t3 = make_t3()
            hist = compute_live_distance_histogram(collection, t3, n=live_n)
        except Exception:  # noqa: BLE001 — best-effort path; error surfaced via log, must not crash caller
            # Review remediation (Reviewer B/S-1): log so a missing `hist`
            # on a --live run isn't invisible. DEBUG keeps normal runs
            # quiet; the operator can re-run with verbose logging.
            _log.debug(
                "live_histogram_failed",
                collection=collection, live_n=live_n, exc_info=True,
            )
    return AuditReport(
        collection=collection,
        distance_histogram=hist,
        cross_projections=projections,
        orphans=orphans,
        hub_assignments=hubs,
        orphans_checked=orphans_checked,
    )


# ── Formatters ──────────────────────────────────────────────────────────────


def format_audit_human(report: AuditReport) -> str:
    lines: list[str] = [f"Audit: {report.collection}", ""]
    # Section 1
    lines.append("=== distance histogram (30d) ===")
    h = report.distance_histogram
    if h.sample_size == 0:
        lines.append(
            "  (no telemetry rows; pass --live to sample from ChromaDB)"
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
    if not report.orphans_checked:
        # nexus-kmo9h: never render "couldn't check" as "checked, clean".
        lines.append(
            "  (skipped — no local catalog to audit: service mode's local "
            ".catalog.db is a frozen migration source; service-side orphan "
            "audit lands with P5 catalog-collapse)"
        )
    elif not report.orphans:
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
