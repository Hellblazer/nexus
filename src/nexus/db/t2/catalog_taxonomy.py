# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CatalogTaxonomy — topics + topic_assignments domain (RDR-063, RDR-070).

Owns the ``topics`` and ``topic_assignments`` tables. Extracted from
the legacy ``nexus.taxonomy`` module in RDR-063; rewritten in RDR-070
(nexus-9k5) to use sklearn HDBSCAN for topic discovery with c-TF-IDF
labels and ChromaDB-backed centroid storage for incremental assignment.

Cross-domain dependency (RDR-063 §Cross-Domain Contracts):

- ``get_topic_docs`` JOINs ``topic_assignments.doc_id`` against
  ``memory.title`` via the shared SQLite file. The :class:`MemoryStore`
  reference injected at construction makes this coupling explicit.
- ``get_topic_docs`` carries the **Known Defect** (per RDR-063 Open
  Question 1, Option 3): when topics were clustered from a T3
  collection (``code__*``, ``knowledge__*``, …) the
  ``topics.collection`` value does not match any ``memory.project``,
  so the LEFT JOIN finds no row and the returned ``title`` falls back
  to the raw ``doc_id``.

Lock ownership convention (matches MemoryStore / PlanLibrary):

- Public methods acquire ``self._lock`` themselves.
- ``get_topic_tree`` deliberately acquires the lock once for the root
  fetch and once *per recursive child fetch* — see method docstring.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np
import structlog

if TYPE_CHECKING:
    from nexus.db.t2.memory_store import MemoryStore

# RDR-070 (nexus-9k5): scikit-learn>=1.3 is a core dep. sklearn.cluster.HDBSCAN
# HDBSCAN for topic discovery with c-TF-IDF labels via CountVectorizer.
from sklearn.cluster import HDBSCAN as SklearnHDBSCAN
from sklearn.feature_extraction.text import CountVectorizer, TfidfTransformer

_log = structlog.get_logger()


# Per-domain migration guard (RDR-063 Open Question 3 — Phase 2 resolution).
# Each store owns its own ``_migrated_paths`` set and ``_migrated_lock``.
# CatalogTaxonomy has a single migration (``_migrate_topics_if_needed``)
# for pre-RDR-061 databases that predate the ``topics`` tables.
_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()


# ── Schema SQL ────────────────────────────────────────────────────────────────

_TAXONOMY_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    parent_id     INTEGER REFERENCES topics(id),
    collection    TEXT NOT NULL,
    centroid_hash TEXT,
    doc_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending',
    terms         TEXT
);

CREATE TABLE IF NOT EXISTS taxonomy_meta (
    collection              TEXT PRIMARY KEY,
    last_discover_doc_count INTEGER NOT NULL DEFAULT 0,
    last_discover_at        TEXT
);

CREATE TABLE IF NOT EXISTS topic_assignments (
    doc_id      TEXT NOT NULL,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    assigned_by TEXT NOT NULL DEFAULT 'hdbscan',
    PRIMARY KEY (doc_id, topic_id)
);

CREATE TABLE IF NOT EXISTS topic_links (
    from_topic_id INTEGER NOT NULL REFERENCES topics(id),
    to_topic_id   INTEGER NOT NULL REFERENCES topics(id),
    link_count    INTEGER NOT NULL DEFAULT 0,
    link_types    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (from_topic_id, to_topic_id)
);
"""

_TOPIC_COLUMNS = (
    "id",
    "label",
    "parent_id",
    "collection",
    "centroid_hash",
    "doc_count",
    "created_at",
    "review_status",
    "terms",
)

# ── Assignment result shape (RDR-077 nexus-uti) ─────────────────────────────


class AssignResult(NamedTuple):
    """Return shape for :meth:`CatalogTaxonomy.assign_single`.

    Carries the nearest ``topic_id`` and the raw cosine ``similarity``
    (``1.0 - distance``). ICF weighting is applied at query time, not here.
    """

    topic_id: int
    similarity: float


class HubRow(NamedTuple):
    """One hub row emitted by :meth:`CatalogTaxonomy.detect_hubs`.

    RDR-077 Phase 5 (nexus-84v).
    """

    topic_id: int
    label: str
    collection: str
    distinct_source_collections: int
    total_chunks: int
    icf: float
    score: float
    matched_stopwords: tuple[str, ...]
    source_collections: tuple[str, ...]
    last_assigned_at: str | None
    # --warn-stale output (populated only when requested; None otherwise)
    max_last_discover_at: str | None
    never_discovered_count: int
    is_stale: bool


class AuditReport(NamedTuple):
    """Summary of projection quality for a single source collection.

    Returned by :meth:`CatalogTaxonomy.audit_collection`. RDR-077 Phase 6
    (nexus-w4k).
    """

    collection: str
    total_assignments: int  # projection rows with source_collection = this
    p10: float | None
    p50: float | None
    p90: float | None
    below_threshold_count: int
    threshold: float
    top_receiving_hubs: list["AuditHub"]
    pattern_pollution: list["AuditHub"]


class AuditHub(NamedTuple):
    """One receiving-topic row inside an :class:`AuditReport`."""

    topic_id: int
    label: str
    chunk_count: int
    icf: float
    matched_stopwords: tuple[str, ...]


# RDR-077 Phase 5 PQ-3: default stopword tokens for generic-pattern detection.
# A hub's label that *contains* any of these (case-insensitive substring) is
# flagged. Ops can surface these in `--explain` so operators can accept or
# suppress. Extending this list is a future RDR (PQ-3 open).
DEFAULT_HUB_STOPWORDS: tuple[str, ...] = (
    "assert",
    "junit",
    "builder",
    "class",
    "import",
    "exception",
    "getter",
    "setter",
    "variable",
    "declaration",
    "operator",
)


# ── Centroid dimension guard (RDR-075 SC-10) ─────────────────────────────────


def _check_centroid_dimension(embedding: "np.ndarray", centroid_coll: Any) -> bool:
    """Return True if embedding dimension matches stored centroids.

    Logs a structured warning and returns False on mismatch.
    Returns True (optimistic) if centroid collection is empty or
    dimension cannot be determined.
    """
    try:
        peek = centroid_coll.peek(1)
        if not peek.get("embeddings") or peek["embeddings"][0] is None:
            return True
        stored_dim = len(peek["embeddings"][0])
        query_dim = len(embedding)
        if query_dim != stored_dim:
            _log.warning(
                "centroid_dimension_mismatch",
                query_dim=query_dim,
                stored_dim=stored_dim,
            )
            return False
    except Exception:
        return True
    return True


# ── CatalogTaxonomy ───────────────────────────────────────────────────────────


class CatalogTaxonomy:
    """Owns the ``topics`` and ``topic_assignments`` tables.

    RDR-070 (nexus-9k5): topic discovery uses sklearn HDBSCAN on
    pre-computed embeddings. Centroids stored in ChromaDB
    ``taxonomy__centroids`` collection. c-TF-IDF labels via
    CountVectorizer + TfidfTransformer for c-TF-IDF labels.

    Constructor takes a :class:`MemoryStore` reference for
    :meth:`get_topic_docs` JOIN resolution (RDR-063 §Cross-Domain
    Contracts).
    """

    def __init__(self, path: Path, memory: "MemoryStore") -> None:
        import math

        self._memory = memory
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
        # RDR-077 Phase 3 (nexus-qab): register `log2` as a SQLite scalar
        # function so the ICF aggregation query can use LOG2() inline.
        # Null-safe: returns NULL when x is NULL, 0, or negative.
        self.conn.create_function(
            "log2",
            1,
            lambda x: math.log2(x) if x is not None and x > 0 else None,
            deterministic=True,
        )
        # RDR-077 Phase 3: command-scoped ICF cache. Populated by callers
        # that want per-command reuse (bulk projection, hubs, audit). The
        # single-doc assignment hook path leaves this as None and recomputes.
        self._icf_cache: dict[int, float] | None = None
        try:
            canonical_key = str(path.resolve())
        except OSError:
            canonical_key = str(path)
        self._init_schema(canonical_key)

    def close(self) -> None:
        """Close the dedicated connection (idempotent under ``self._lock``)."""
        with self._lock:
            self.conn.close()

    # ── Schema / migrations ───────────────────────────────────────────────

    def _init_schema(self, path_key: str) -> None:
        """Create the topics tables and run migrations if needed."""
        with self._lock:
            self.conn.executescript(_TAXONOMY_SCHEMA_SQL)
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.commit()
            with _migrated_lock:
                if path_key in _migrated_paths:
                    return
                self._migrate_topics_if_needed()
                self._migrate_assigned_by_if_needed()
                self._migrate_review_columns_if_needed()
                _migrated_paths.add(path_key)

    def _migrate_topics_if_needed(self) -> None:
        """Add topics and topic_assignments tables if missing.

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        Delegates to module-level function in migrations.py (RDR-076).
        """
        from nexus.db.migrations import migrate_topics

        migrate_topics(self.conn)

    def _migrate_assigned_by_if_needed(self) -> None:
        """Add ``assigned_by`` column to ``topic_assignments`` if missing.

        RDR-070 (nexus-9k5). Lock-naive: caller must hold both locks.
        Delegates to module-level function in migrations.py (RDR-076).
        """
        from nexus.db.migrations import migrate_assigned_by

        migrate_assigned_by(self.conn)

    def _migrate_review_columns_if_needed(self) -> None:
        """Add ``review_status`` and ``terms`` columns if missing.

        RDR-070 (nexus-lbu). Lock-naive: caller must hold both locks.
        Delegates to module-level function in migrations.py (RDR-076).
        """
        from nexus.db.migrations import migrate_review_columns

        migrate_review_columns(self.conn)

    # ── Public API ────────────────────────────────────────────────────────

    # RDR-077 Phase 3 (nexus-qab): Inverse Collection Frequency for hub
    # suppression. Topics that appear across many source collections carry
    # less specific signal (generic patterns, stopword labels) and should
    # be down-weighted at query time. ICF never mutates the write path —
    # the `similarity` column holds raw cosine; ICF is applied by callers
    # that want weighted ranking.

    def compute_icf_map(
        self, *, use_cache: bool = False,
    ) -> dict[int, float]:
        """Return ``{topic_id: icf}`` where ``icf = log2(N_effective / DF)``.

        - ``N_effective`` — count of distinct ``source_collection`` values
          over projection rows (``assigned_by='projection'`` and
          ``source_collection IS NOT NULL``).
        - ``DF(topic)`` — count of distinct source collections that have
          assigned any chunk to this topic via projection.

        Guards:
        - ``N_effective < 2`` → ICF cannot discriminate; returns ``{}``.
          Callers should treat an empty map as "fall back to raw similarity".
        - ``DF = N_effective`` → ``log2(1) = 0`` — ubiquitous topics are
          suppressed to zero weight by design.
        - Legacy rows (NULL ``source_collection``, pre-RDR-077) are
          excluded from both numerator and denominator via ``IS NOT NULL``.

        The SQL uses the ``log2`` scalar registered in ``__init__``
        (SQLite has no native LOG/LOG2). ``log2`` returns NULL for
        non-positive inputs, so no runtime math error can escape.

        *use_cache*: when True, returns the cached map from
        ``self._icf_cache`` if present, else computes and caches. Callers
        driving a bulk command (``nx taxonomy project --persist``, hubs,
        audit) set this; the single-doc assignment hook leaves it False
        and recomputes per call.
        """
        if use_cache and self._icf_cache is not None:
            return self._icf_cache

        with self._lock:
            n_row = self.conn.execute(
                "SELECT COUNT(DISTINCT source_collection) "
                "FROM topic_assignments "
                "WHERE assigned_by = 'projection' "
                "AND source_collection IS NOT NULL"
            ).fetchone()
            n_effective = int(n_row[0]) if n_row and n_row[0] else 0

            if n_effective < 2:
                result: dict[int, float] = {}
                if use_cache:
                    self._icf_cache = result
                return result

            rows = self.conn.execute(
                """
                SELECT
                    ta.topic_id,
                    log2(CAST(? AS REAL)
                         / COUNT(DISTINCT ta.source_collection)) AS icf
                FROM topic_assignments ta
                WHERE ta.assigned_by = 'projection'
                  AND ta.source_collection IS NOT NULL
                GROUP BY ta.topic_id
                HAVING COUNT(DISTINCT ta.source_collection) > 0
                """,
                (n_effective,),
            ).fetchall()

        result = {int(tid): float(icf) for tid, icf in rows if icf is not None}
        if use_cache:
            self._icf_cache = result
        return result

    def clear_icf_cache(self) -> None:
        """Drop the cached ICF map so the next ``compute_icf_map(use_cache=True)``
        recomputes from current ``topic_assignments`` state."""
        self._icf_cache = None

    # RDR-077 Phase 5 (nexus-84v): generic-pattern hub detection. Surfaces
    # topics that span many source collections with low ICF and/or
    # stopword-containing labels — the practical signal that a topic is a
    # generic code/prose pattern rather than a specific domain concept.

    def detect_hubs(
        self,
        *,
        min_collections: int = 2,
        max_icf: float | None = None,
        stopwords: tuple[str, ...] = DEFAULT_HUB_STOPWORDS,
        warn_stale: bool = False,
    ) -> list[HubRow]:
        """Return candidate hub topics, sorted by ``chunks × (1 - ICF)`` desc.

        A hub is any projection topic that meets ALL configured filters:

        - ``DF(topic) >= min_collections`` (distinct source_collections).
        - ``ICF(topic) <= max_icf`` when ``max_icf`` is set.

        ``stopwords`` is an advisory list: matching tokens are reported in
        the row's ``matched_stopwords`` field for ``--explain`` output, but
        they do NOT filter rows — a topic with a clean label can still be a
        hub by DF/ICF alone.

        When *warn_stale* is True, the row carries ``max_last_discover_at``
        (the latest ``taxonomy_meta.last_discover_at`` across every source
        collection contributing to the hub — RDR-077 C-2 correctness fix),
        the count of never-discovered source collections, and
        ``is_stale`` set when any source collection's last discover predates
        the hub's latest assignment. NULL ``last_discover_at`` rows are
        excluded from MAX per SQLite aggregation semantics (RDR-077 O-3);
        never-discovered collections surface via ``never_discovered_count``.
        """
        icf_map = self.compute_icf_map()
        lowered_stopwords = tuple(s.lower() for s in stopwords)

        with self._lock:
            # Per-topic DF, total chunk count, label/collection, source set,
            # latest assigned_at — all from projection rows only.
            rows = self.conn.execute(
                """
                SELECT
                    t.id,
                    t.label,
                    t.collection,
                    COUNT(DISTINCT ta.source_collection) AS df,
                    COUNT(*)                              AS total,
                    MAX(ta.assigned_at)                   AS last_assigned_at
                FROM topic_assignments ta
                JOIN topics t ON t.id = ta.topic_id
                WHERE ta.assigned_by = 'projection'
                  AND ta.source_collection IS NOT NULL
                GROUP BY t.id, t.label, t.collection
                HAVING df >= ?
                ORDER BY total DESC
                """,
                (min_collections,),
            ).fetchall()

            # Storage review S-5: fetch the per-topic source collection
            # set in a single grouped query instead of N per-hub queries
            # under the lock. For a large hub count the old shape held
            # the taxonomy lock through N SELECTs, blocking every
            # concurrent writer.
            sources_rows = self.conn.execute(
                """
                SELECT topic_id, source_collection
                FROM topic_assignments
                WHERE assigned_by = 'projection'
                  AND source_collection IS NOT NULL
                ORDER BY topic_id, source_collection
                """
            ).fetchall()
            sources_by_topic: dict[int, list[str]] = {}
            for tid, sc in sources_rows:
                sources_by_topic.setdefault(int(tid), []).append(sc)

            hubs: list[HubRow] = []
            for topic_id, label, collection, df, total, last_at in rows:
                icf_value = float(icf_map.get(int(topic_id), 1.0))
                if max_icf is not None and icf_value > max_icf:
                    continue

                sources = tuple(
                    dict.fromkeys(sources_by_topic.get(int(topic_id), []))
                )

                lower_label = (label or "").lower()
                matched = tuple(
                    s for s in lowered_stopwords if s in lower_label
                )

                max_discover: str | None = None
                never_count = 0
                is_stale = False
                if warn_stale and sources:
                    placeholders = ",".join("?" * len(sources))
                    stale_row = self.conn.execute(
                        f"""
                        SELECT
                            MAX(last_discover_at),
                            SUM(CASE WHEN last_discover_at IS NULL
                                     THEN 1 ELSE 0 END),
                            COUNT(*)
                        FROM taxonomy_meta
                        WHERE collection IN ({placeholders})
                        """,
                        sources,
                    ).fetchone()
                    max_discover = stale_row[0]
                    seen = int(stale_row[2] or 0)
                    nulls = int(stale_row[1] or 0)
                    # Any contributing collection that has no taxonomy_meta
                    # row at all is also "never discovered" from the
                    # perspective of this command.
                    never_count = nulls + (len(sources) - seen)
                    # "Stale" when the hub has data newer than the latest
                    # discover (lexicographic ISO-8601 comparison).
                    if last_at and max_discover and last_at > max_discover:
                        is_stale = True
                    if never_count > 0:
                        is_stale = True

                score = float(total) * (1.0 - icf_value)
                hubs.append(HubRow(
                    topic_id=int(topic_id),
                    label=label or "",
                    collection=collection or "",
                    distinct_source_collections=int(df),
                    total_chunks=int(total),
                    icf=icf_value,
                    score=score,
                    matched_stopwords=matched,
                    source_collections=sources,
                    last_assigned_at=last_at,
                    max_last_discover_at=max_discover,
                    never_discovered_count=never_count,
                    is_stale=is_stale,
                ))

        hubs.sort(key=lambda h: h.score, reverse=True)
        return hubs

    # RDR-077 Phase 6 (nexus-w4k): per-collection projection-quality audit.
    # Reports the similarity distribution, the count below threshold,
    # receiving hubs (topics this collection's chunks project into), and
    # pattern-pollution — hubs flagged by the Phase 5 stopword heuristic.

    def audit_collection(
        self,
        collection: str,
        *,
        threshold: float | None = None,
        top_n: int = 5,
        stopwords: tuple[str, ...] = DEFAULT_HUB_STOPWORDS,
    ) -> AuditReport:
        """Summarise projection quality for *collection*.

        - ``total_assignments``: projection rows where
          ``source_collection = collection``.
        - ``p10`` / ``p50`` / ``p90``: quantiles of ``similarity`` on those
          rows (computed Python-side — SQLite has no
          ``percentile_cont``). ``None`` when the collection has no
          projection data.
        - ``below_threshold_count``: rows with ``similarity < threshold``.
          Defaults to the per-corpus-type threshold from
          :func:`nexus.corpus.default_projection_threshold` when not
          specified.
        - ``top_receiving_hubs``: topics this collection's chunks project
          into, sorted by chunk count descending, limited to *top_n*.
        - ``pattern_pollution``: subset of receiving hubs whose labels
          contain any of *stopwords* (case-insensitive substring match).

        Raw ``similarity`` column values are used; ICF is applied only for
        the receiving-hub ICF reporting, never to mutate stored rows.
        """
        from nexus.corpus import default_projection_threshold

        resolved_threshold = (
            threshold if threshold is not None
            else default_projection_threshold(collection)
        )
        icf_map = self.compute_icf_map()
        lowered_stopwords = tuple(s.lower() for s in stopwords)

        with self._lock:
            sims = [
                float(r[0]) for r in self.conn.execute(
                    "SELECT similarity FROM topic_assignments "
                    "WHERE assigned_by = 'projection' "
                    "AND source_collection = ? AND similarity IS NOT NULL "
                    "ORDER BY similarity ASC",
                    (collection,),
                ).fetchall()
            ]
            below_threshold = self.conn.execute(
                "SELECT COUNT(*) FROM topic_assignments "
                "WHERE assigned_by = 'projection' "
                "AND source_collection = ? "
                "AND similarity IS NOT NULL AND similarity < ?",
                (collection, resolved_threshold),
            ).fetchone()[0]

            hub_rows = self.conn.execute(
                """
                SELECT ta.topic_id, t.label, COUNT(*) AS chunks
                FROM topic_assignments ta
                JOIN topics t ON t.id = ta.topic_id
                WHERE ta.assigned_by = 'projection'
                  AND ta.source_collection = ?
                GROUP BY ta.topic_id, t.label
                ORDER BY chunks DESC
                LIMIT ?
                """,
                (collection, top_n),
            ).fetchall()

        total = len(sims)
        if total:
            def _quantile(q: float) -> float:
                # Nearest-rank index; matches numpy's `interpolation='nearest'`
                # for small samples and is deterministic for fixtures.
                idx = min(total - 1, max(0, int(round(q * (total - 1)))))
                return sims[idx]

            p10 = _quantile(0.10)
            p50 = _quantile(0.50)
            p90 = _quantile(0.90)
        else:
            p10 = p50 = p90 = None

        top_hubs: list[AuditHub] = []
        for topic_id, label, chunks in hub_rows:
            lower_label = (label or "").lower()
            matched = tuple(s for s in lowered_stopwords if s in lower_label)
            top_hubs.append(AuditHub(
                topic_id=int(topic_id),
                label=label or "",
                chunk_count=int(chunks),
                icf=float(icf_map.get(int(topic_id), 1.0)),
                matched_stopwords=matched,
            ))
        pattern_pollution = [h for h in top_hubs if h.matched_stopwords]

        return AuditReport(
            collection=collection,
            total_assignments=total,
            p10=p10,
            p50=p50,
            p90=p90,
            below_threshold_count=int(below_threshold or 0),
            threshold=resolved_threshold,
            top_receiving_hubs=top_hubs,
            pattern_pollution=pattern_pollution,
        )

    def get_topics(
        self,
        *,
        parent_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return topics filtered by parent.

        - ``parent_id=None`` (default): return root topics (parent_id IS NULL).
        - ``parent_id=<int>``: return children of that topic.

        Two SELECT branches (root vs. child) inside one lock acquisition,
        same as the pre-split implementation.
        """
        with self._lock:
            if parent_id is None:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at, review_status, terms "
                    "FROM topics WHERE parent_id IS NULL ORDER BY doc_count DESC"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at, review_status, terms "
                    "FROM topics WHERE parent_id = ? ORDER BY doc_count DESC",
                    (parent_id,),
                ).fetchall()
        return [dict(zip(_TOPIC_COLUMNS, row)) for row in rows]

    def assign_topic(
        self,
        doc_id: str,
        topic_id: int,
        *,
        assigned_by: str = "hdbscan",
        similarity: float | None = None,
        source_collection: str | None = None,
        assigned_at: str | None = None,
    ) -> None:
        """Assign a document to a topic.

        For ``assigned_by='projection'`` rows (RDR-077), emits a prefer-higher
        UPSERT: if a row already exists, ``similarity`` is set to the max of
        the stored and incoming values, and ``assigned_at`` /
        ``source_collection`` are refreshed only when the incoming similarity
        wins. ``COALESCE(-1.0)`` handles legacy NULL rows pre-migration.

        HDBSCAN / centroid / manual rows keep ``INSERT OR IGNORE`` idempotency
        — similarity is NULL for those paths (no ANN distance available).

        Args:
            doc_id: opaque per-collection document identifier.
            topic_id: target topic row id.
            assigned_by: provenance tag (``hdbscan`` / ``centroid`` /
                ``projection`` / ``manual``).
            similarity: raw cosine (``1.0 - distance``) when known;
                stored only for ``assigned_by='projection'``.
            source_collection: the collection the doc was fetched from
                (required for projection ICF aggregation in Phase 3).
            assigned_at: ISO-8601 timestamp; defaults to ``datetime.now(UTC)``
                when ``assigned_by='projection'``.
        """
        if assigned_by == "projection":
            ts = assigned_at or datetime.now(UTC).isoformat()
            with self._lock:
                self.conn.execute(
                    """
                    INSERT INTO topic_assignments
                        (doc_id, topic_id, assigned_by,
                         similarity, assigned_at, source_collection)
                    VALUES (?, ?, 'projection', ?, ?, ?)
                    ON CONFLICT(doc_id, topic_id) DO UPDATE SET
                        similarity =
                            MAX(COALESCE(topic_assignments.similarity, -1.0),
                                excluded.similarity),
                        assigned_at = CASE
                            WHEN excluded.similarity
                                 > COALESCE(topic_assignments.similarity, -1.0)
                            THEN excluded.assigned_at
                            ELSE topic_assignments.assigned_at
                        END,
                        source_collection = CASE
                            WHEN excluded.similarity
                                 > COALESCE(topic_assignments.similarity, -1.0)
                            THEN excluded.source_collection
                            ELSE topic_assignments.source_collection
                        END,
                        assigned_by = 'projection'
                    """,
                    (doc_id, topic_id, similarity, ts, source_collection),
                )
                self.conn.commit()
            return

        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO topic_assignments "
                "(doc_id, topic_id, assigned_by) VALUES (?, ?, ?)",
                (doc_id, topic_id, assigned_by),
            )
            self.conn.commit()

    def get_topic_docs(
        self,
        topic_id: int,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return doc_ids and titles assigned to a topic.

        KNOWN LIMITATION (RDR-063): The JOIN resolves ``doc_id`` via
        ``memory.title`` using ``topics.collection`` as the project scope.
        This only works when the taxonomy was built from T2 memory entries
        (where ``project == topics.collection``). Topics clustered from T3
        collections (``code__*``, ``knowledge__*``, etc.) will return
        ``title`` equal to ``doc_id`` (the JOIN finds no match and falls
        back to the raw identifier).

        Rationale: T3 chunk titles live in T3/catalog metadata, not in T2
        memory. If T3-origin title resolution is needed, route through the
        catalog (``CatalogEntry.title``), not through this function.

        See ``test_get_topic_docs_known_defect_project_collection_mismatch``
        for the mechanical documentation of this behavior.

        PHASE 3 FRAGILITY (RDR-063): This JOIN runs on the taxonomy
        connection and still finds the ``memory`` table because Phase 2
        keeps all four T2 domains in a single SQLite file — any
        connection can see any table. If RDR-063 Phase 3 (physical file
        split) ever proceeds, the taxonomy connection will no longer see
        the ``memory`` table, the JOIN will silently return empty rows,
        and every result will fall back to ``title=doc_id``. That
        failure mode is indistinguishable from the known-defect path
        above. Phase 3 must replace this JOIN with a two-step fetch:
        pull topic_assignments from ``self.conn``, then resolve titles
        via ``self._memory.get(...)`` on the MemoryStore reference. Do
        not touch this method in Phase 1/2 — it's correct for
        single-file T2. Flag it in the Phase 3 RDR.
        """
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT ta.doc_id, m.title, m.project
                FROM topic_assignments ta
                LEFT JOIN memory m ON m.title = ta.doc_id AND m.project = (
                    SELECT collection FROM topics WHERE id = ta.topic_id
                )
                WHERE ta.topic_id = ?
                LIMIT ?
                """,
                (topic_id, limit),
            ).fetchall()
        return [{"doc_id": r[0], "title": r[1] or r[0], "project": r[2] or ""} for r in rows]

    def get_topic_tree(
        self,
        collection: str = "",
        *,
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        """Return topics as a nested tree structure.

        Each node: ``{id, label, doc_count, children: [...]}``. Filtered
        by collection when provided.

        Lock pattern (preserved from the pre-split implementation):
          1. One lock acquisition fetches the root rows.
          2. The recursive ``_build_node`` walk runs WITH THE LOCK
             RELEASED. Each child fetch acquires the lock independently.
          3. ``threading.Lock`` is non-reentrant; the recursive structure
             is intentionally arranged so the lock is never re-entered.
             Concurrent writes between fetches are tolerated — callers
             accept that the returned tree is a multi-snapshot view, not
             a single-snapshot transaction.
        """
        with self._lock:
            if collection:
                roots = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at, review_status, terms "
                    "FROM topics WHERE parent_id IS NULL AND collection = ? ORDER BY doc_count DESC",
                    (collection,),
                ).fetchall()
            else:
                roots = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at, review_status, terms "
                    "FROM topics WHERE parent_id IS NULL ORDER BY doc_count DESC"
                ).fetchall()

        def _build_node(row: tuple, depth: int) -> dict[str, Any]:
            node = {"id": row[0], "label": row[1], "collection": row[3], "doc_count": row[5]}
            if depth < max_depth:
                with self._lock:
                    children = self.conn.execute(
                        "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at, review_status, terms "
                        "FROM topics WHERE parent_id = ? ORDER BY doc_count DESC",
                        (row[0],),
                    ).fetchall()
                # Recurse OUTSIDE the lock — see method docstring.
                node["children"] = [_build_node(c, depth + 1) for c in children]
            else:
                node["children"] = []
            return node

        return [_build_node(r, 0) for r in roots]

    def get_doc_ids_for_topic(self, label: str) -> list[str]:
        """Return doc_ids assigned to the topic with the given label."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT ta.doc_id FROM topic_assignments ta "
                "JOIN topics t ON t.id = ta.topic_id WHERE t.label = ?",
                (label,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_assignments_for_docs(self, doc_ids: list[str]) -> dict[str, int]:
        """Return {doc_id: topic_id} for docs that have topic assignments."""
        if not doc_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in doc_ids)
            rows = self.conn.execute(
                f"SELECT doc_id, topic_id FROM topic_assignments WHERE doc_id IN ({placeholders})",
                doc_ids,
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ── RDR-083: corpus-evidence helpers ─────────────────────────────────

    def top_topics_for_collection(
        self, collection: str, *, top_n: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the top-N projected topics for *collection* ordered by
        ``SUM(similarity) DESC``. Serves ``{{nx-anchor:<collection>|top=N}}``.

        Only projection-assigned rows (``assigned_by='projection'``) are
        counted — native-collection topics are already visible via the
        normal topic browser.
        """
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT t.label, COUNT(*) AS chunks, SUM(ta.similarity) AS sum_sim
                FROM topic_assignments ta
                JOIN topics t ON t.id = ta.topic_id
                WHERE ta.assigned_by = 'projection'
                  AND ta.source_collection = ?
                  AND ta.similarity IS NOT NULL
                GROUP BY ta.topic_id, t.label
                ORDER BY sum_sim DESC, chunks DESC
                LIMIT ?
                """,
                (collection, top_n),
            ).fetchall()
        return [{"label": r[0], "chunks": r[1], "sum_similarity": r[2]} for r in rows]

    def chunk_grounded_in(
        self, doc_id: str, source_collection: str, *, threshold: float,
    ) -> float | None:
        """Return the max similarity of *doc_id*'s chunks projecting into
        *source_collection*, or ``None`` when no projection data exists.

        Serves ``check-extensions``: a doc whose top projection similarity
        falls below the caller's threshold is an author-extension candidate.
        ``threshold`` is accepted for future use (e.g. prefilter); current
        impl returns the raw max.
        """
        with self._lock:
            row = self.conn.execute(
                """
                SELECT MAX(similarity)
                FROM topic_assignments
                WHERE assigned_by = 'projection'
                  AND doc_id = ?
                  AND source_collection = ?
                  AND similarity IS NOT NULL
                """,
                (doc_id, source_collection),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    # ── Review workflow (RDR-070, nexus-lbu) ─────────────────────────────

    def get_all_topics(
        self,
        collection: str = "",
    ) -> list[dict[str, Any]]:
        """Return every topic (roots + children), ordered by doc_count DESC.

        Contrast :meth:`get_topics` which returns only roots and
        :meth:`get_topics_for_collection` which requires a collection
        filter. This method is the "no tree structure, give me every
        row" helper that the CLI needs for label / relabel --all paths.

        GitHub #243 + bead nexus-kxez: previously ``label_cmd`` and
        ``relabel_topics``'s --all branch used ``get_topics()`` which
        hid every split sub-topic, so post-split pending children were
        never passed to the LLM labeler even though ``status`` counted
        them as pending.
        """
        with self._lock:
            if collection:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, "
                    "doc_count, created_at, review_status, terms "
                    "FROM topics WHERE collection = ? "
                    "ORDER BY doc_count DESC",
                    (collection,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, "
                    "doc_count, created_at, review_status, terms "
                    "FROM topics ORDER BY doc_count DESC"
                ).fetchall()
        return [dict(zip(_TOPIC_COLUMNS, row)) for row in rows]

    def get_projection_counts_by_collection(self) -> dict[str, int]:
        """Return ``{source_collection: projection_assignment_count}``.

        Counts rows in ``topic_assignments`` where ``assigned_by =
        'projection'`` grouped by ``source_collection``. Collections not
        present in the result have zero projection assignments; status
        callers can detect "has topics but no projection" by cross-
        referencing against the topics table.

        GitHub #239 + bead nexus-gwhy: ``status`` now surfaces
        zero-projection collections so the gap is visible without
        running ``audit -c`` per collection.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT source_collection, COUNT(*) FROM topic_assignments "
                "WHERE assigned_by = 'projection' "
                "AND source_collection IS NOT NULL AND source_collection != '' "
                "GROUP BY source_collection"
            ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def update_topic_label(self, topic_id: int, new_label: str) -> None:
        """Update a topic's label without touching ``review_status``.

        Used by the batch LLM relabeler so topics stay ``pending`` until
        a human runs ``nx taxonomy review``. Contrast :meth:`rename_topic`
        which also transitions to ``accepted`` (correct for the
        interactive review path where the human is acknowledging the new
        label as they rename).

        GitHub #241 Item 3 + bead nexus-kxez.
        """
        with self._lock:
            self.conn.execute(
                "UPDATE topics SET label = ? WHERE id = ?",
                (new_label, topic_id),
            )
            self.conn.commit()

    def get_unreviewed_topics(
        self,
        collection: str = "",
        *,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Return topics with ``review_status='pending'``, ordered by doc_count DESC."""
        with self._lock:
            if collection:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, "
                    "created_at, review_status, terms "
                    "FROM topics WHERE review_status = 'pending' AND collection = ? "
                    "ORDER BY doc_count DESC LIMIT ?",
                    (collection, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, "
                    "created_at, review_status, terms "
                    "FROM topics WHERE review_status = 'pending' "
                    "ORDER BY doc_count DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(zip(_TOPIC_COLUMNS, row)) for row in rows]

    def mark_topic_reviewed(self, topic_id: int, status: str) -> None:
        """Update ``review_status`` for a topic."""
        with self._lock:
            self.conn.execute(
                "UPDATE topics SET review_status = ? WHERE id = ?",
                (status, topic_id),
            )
            self.conn.commit()

    def rename_topic(self, topic_id: int, new_label: str) -> None:
        """Rename a topic's label and mark as accepted."""
        with self._lock:
            self.conn.execute(
                "UPDATE topics SET label = ?, review_status = 'accepted' WHERE id = ?",
                (new_label, topic_id),
            )
            self.conn.commit()

    def delete_topic(self, topic_id: int, *, chroma_client: Any = None) -> None:
        """Delete a topic, its assignments, and its centroid."""
        # Read collection before deleting the row
        with self._lock:
            row = self.conn.execute(
                "SELECT collection FROM topics WHERE id = ?", (topic_id,),
            ).fetchone()
            collection = row[0] if row else None
            self.conn.execute(
                "DELETE FROM topic_assignments WHERE topic_id = ?", (topic_id,),
            )
            self.conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
            self.conn.commit()
        # Clean up centroid outside the lock
        if chroma_client and collection:
            try:
                coll = chroma_client.get_collection(
                    "taxonomy__centroids", embedding_function=None,
                )
                coll.delete(ids=[f"{collection}:{topic_id}"])
            except Exception:
                pass

    def merge_topics(
        self, source_id: int, target_id: int, *, chroma_client: Any = None,
    ) -> None:
        """Move all assignments from source to target, delete source.

        Uses INSERT OR IGNORE to handle docs assigned to both topics
        (dedup). Updates target's doc_count to the actual assignment count.
        Cleans up source centroid from ChromaDB when chroma_client provided.
        """
        if source_id == target_id:
            return  # Self-merge is a no-op
        with self._lock:
            # Read collection before deleting
            row = self.conn.execute(
                "SELECT collection FROM topics WHERE id = ?", (source_id,),
            ).fetchone()
            collection = row[0] if row else None
            # Move assignments — storage review I-5: when the same doc_id
            # is assigned to both source and target topics, keep the row
            # with the higher similarity (best projection quality). The
            # previous INSERT OR IGNORE silently discarded the source row
            # even when it scored higher.
            self.conn.execute(
                "INSERT INTO topic_assignments "
                "    (doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection) "
                "SELECT doc_id, ?, assigned_by, similarity, assigned_at, source_collection "
                "FROM topic_assignments WHERE topic_id = ? "
                "ON CONFLICT(doc_id, topic_id) DO UPDATE SET "
                "    similarity = MAX("
                "        COALESCE(topic_assignments.similarity, -1.0), "
                "        COALESCE(excluded.similarity, -1.0)), "
                "    assigned_at = CASE "
                "        WHEN COALESCE(excluded.similarity, -1.0) > "
                "             COALESCE(topic_assignments.similarity, -1.0) "
                "        THEN excluded.assigned_at "
                "        ELSE topic_assignments.assigned_at END, "
                "    source_collection = CASE "
                "        WHEN COALESCE(excluded.similarity, -1.0) > "
                "             COALESCE(topic_assignments.similarity, -1.0) "
                "        THEN excluded.source_collection "
                "        ELSE topic_assignments.source_collection END",
                (target_id, source_id),
            )
            # Remove source assignments
            self.conn.execute(
                "DELETE FROM topic_assignments WHERE topic_id = ?", (source_id,),
            )
            # Update target doc_count from actual assignments
            new_count = self.conn.execute(
                "SELECT COUNT(*) FROM topic_assignments WHERE topic_id = ?",
                (target_id,),
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE topics SET doc_count = ? WHERE id = ?",
                (new_count, target_id),
            )
            # Delete source topic
            self.conn.execute("DELETE FROM topics WHERE id = ?", (source_id,))
            self.conn.commit()
        # Clean up source centroid outside the lock
        if chroma_client and collection:
            try:
                coll = chroma_client.get_collection(
                    "taxonomy__centroids", embedding_function=None,
                )
                coll.delete(ids=[f"{collection}:{source_id}"])
            except Exception:
                pass

    def get_topic_by_id(self, topic_id: int) -> dict[str, Any] | None:
        """Return a single topic dict by ID, or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT id, label, parent_id, collection, centroid_hash, doc_count, "
                "created_at, review_status, terms "
                "FROM topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(_TOPIC_COLUMNS, row))

    def get_topic_doc_ids(self, topic_id: int, *, limit: int = 3) -> list[str]:
        """Return doc_ids assigned to a topic (limited, for display)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT doc_id FROM topic_assignments WHERE topic_id = ? LIMIT ?",
                (topic_id, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def get_all_topic_doc_ids(self, topic_id: int) -> list[str]:
        """Return all doc_ids assigned to a topic (no limit)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT doc_id FROM topic_assignments WHERE topic_id = ?",
                (topic_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_labels_for_ids(self, topic_ids: list[int]) -> dict[int, str]:
        """Return {topic_id: label} for the given IDs (scoped query)."""
        if not topic_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in topic_ids)
            rows = self.conn.execute(
                f"SELECT id, label FROM topics WHERE id IN ({placeholders})",
                topic_ids,
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_distinct_collections(self) -> list[str]:
        """Return sorted list of collections that have at least one topic."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT collection FROM topics ORDER BY collection"
            ).fetchall()
        return [r[0] for r in rows]

    def get_topics_for_collection(
        self,
        collection: str,
        *,
        exclude_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all topics (root + children) for a collection."""
        with self._lock:
            if exclude_id is not None:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, "
                    "created_at, review_status, terms "
                    "FROM topics WHERE collection = ? AND id != ? ORDER BY doc_count DESC",
                    (collection, exclude_id),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, "
                    "created_at, review_status, terms "
                    "FROM topics WHERE collection = ? ORDER BY doc_count DESC",
                    (collection,),
                ).fetchall()
        return [dict(zip(_TOPIC_COLUMNS, row)) for row in rows]

    def get_topic_link_pairs(
        self, topic_ids: list[int],
    ) -> dict[tuple[int, int], int]:
        """Return {(from_id, to_id): link_count} for the given topic IDs.

        Used by ``apply_topic_boost`` at search time. Scoped to the
        provided topic IDs — returns only links where both endpoints
        are in the set.
        """
        if not topic_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in topic_ids)
            rows = self.conn.execute(
                f"SELECT from_topic_id, to_topic_id, link_count FROM topic_links "
                f"WHERE from_topic_id IN ({placeholders}) "
                f"AND to_topic_id IN ({placeholders})",
                topic_ids + topic_ids,
            ).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def refresh_projection_links(self) -> int:
        """Rebuild projection entries in ``topic_links`` from per-chunk assignments.

        Aggregates rows in ``topic_assignments`` where ``assigned_by =
        'projection'`` against the source-collection's own topic
        assignment for the same ``doc_id``, producing
        ``(source_topic_id, target_topic_id, count)`` tuples in
        canonical order. Each pair is upserted into ``topic_links``
        with ``'projection'`` merged into the existing ``link_types``
        set (so catalog-derived types like ``cites`` / ``implements``
        written by :func:`compute_topic_links` survive).

        GitHub #240 + bead nexus-gwhy: ``nx taxonomy project --persist``
        writes to ``topic_assignments`` but not to ``topic_links``, so
        the ``links`` view went stale after backfill while ``hubs``
        (which queries live) reflected the new state. Calling this
        helper at the end of the persist paths keeps the two views in
        sync.

        Returns the number of topic-pair rows written / updated.
        """
        # Single lock block: both the aggregate SELECT and the per-pair
        # merge+upsert run under the same acquisition. Splitting them
        # (earlier implementation) created a window where a concurrent
        # ``assign_topic`` or ``taxonomy_assign_hook`` could change
        # ``topic_assignments`` between the aggregate and the writes,
        # yielding stale ``link_count`` values. Code-review finding C-1.
        written = 0
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT src.topic_id, tgt.topic_id, COUNT(*) AS cnt
                FROM topic_assignments tgt
                JOIN topic_assignments src
                  ON src.doc_id = tgt.doc_id
                 AND src.topic_id != tgt.topic_id
                 AND src.assigned_by != 'projection'
                WHERE tgt.assigned_by = 'projection'
                GROUP BY src.topic_id, tgt.topic_id
                HAVING cnt > 0
                """
            ).fetchall()

            if not rows:
                return 0

            # Canonicalize pair ordering so (A, B) and (B, A) merge into one row.
            aggregated: dict[tuple[int, int], int] = {}
            for src_id, tgt_id, count in rows:
                pair = (int(src_id), int(tgt_id)) if src_id < tgt_id else (int(tgt_id), int(src_id))
                aggregated[pair] = aggregated.get(pair, 0) + int(count)

            for (from_id, to_id), count in aggregated.items():
                existing = self.conn.execute(
                    "SELECT link_types FROM topic_links "
                    "WHERE from_topic_id = ? AND to_topic_id = ?",
                    (from_id, to_id),
                ).fetchone()
                if existing and existing[0]:
                    try:
                        types_set = set(json.loads(existing[0]))
                    except json.JSONDecodeError:
                        types_set = set()
                else:
                    types_set = set()
                types_set.add("projection")

                self.conn.execute(
                    "INSERT OR REPLACE INTO topic_links "
                    "(from_topic_id, to_topic_id, link_count, link_types) "
                    "VALUES (?, ?, ?, ?)",
                    (from_id, to_id, count, json.dumps(sorted(types_set))),
                )
                written += 1
            self.conn.commit()
        return written

    def upsert_topic_links(
        self, links: list[dict[str, Any]],
    ) -> int:
        """Persist inter-topic link pairs from ``compute_topic_links``.

        Each dict has from_topic_id, to_topic_id, link_count, link_types.
        Uses INSERT OR REPLACE — preserves projection links written by
        ``_discover_cross_links`` (only overwrites matching PK pairs).
        Returns number of rows upserted.
        """
        if not links:
            return 0
        with self._lock:
            for link in links:
                self.conn.execute(
                    "INSERT OR REPLACE INTO topic_links "
                    "(from_topic_id, to_topic_id, link_count, link_types) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        link["from_topic_id"],
                        link["to_topic_id"],
                        link["link_count"],
                        json.dumps(link["link_types"]),
                    ),
                )
            self.conn.commit()
        return len(links)

    def resolve_label(
        self, label: str, *, collection: str = "",
    ) -> int | None:
        """Resolve a topic label to its ID. Returns None if not found."""
        with self._lock:
            if collection:
                row = self.conn.execute(
                    "SELECT id FROM topics WHERE label = ? AND collection = ? LIMIT 1",
                    (label, collection),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT id FROM topics WHERE label = ? LIMIT 1",
                    (label,),
                ).fetchone()
        return row[0] if row else None

    def split_topic(
        self,
        topic_id: int,
        k: int,
        chroma_client: Any,
    ) -> int:
        """Split a topic into k children via KMeans sub-clustering.

        Fetches doc texts from the T3 collection, re-embeds with local
        MiniLM, runs KMeans(n_clusters=k), creates child topics with
        c-TF-IDF labels, and reassigns docs. Returns number of children
        created, or 0 if too few docs.
        """
        from nexus.db.local_ef import LocalEmbeddingFunction

        if k < 2:
            return 0

        topic = self.get_topic_by_id(topic_id)
        if topic is None:
            return 0

        doc_ids = self.get_all_topic_doc_ids(topic_id)
        if len(doc_ids) < k:
            return 0

        # Fetch texts from T3 collection
        collection_name = topic["collection"]
        try:
            coll = chroma_client.get_collection(
                collection_name, embedding_function=None,
            )
        except Exception:
            _log.warning("split_collection_not_found", collection=collection_name)
            return 0

        # Paginate get() to respect cloud quota (limit 300)
        _PAGE = 250
        fetched_ids: list[str] = []
        texts: list[str] = []
        for i in range(0, len(doc_ids), _PAGE):
            batch = doc_ids[i : i + _PAGE]
            result = coll.get(ids=batch, include=["documents"])
            for fid, fdoc in zip(result.get("ids") or [], result.get("documents") or []):
                if fdoc:
                    fetched_ids.append(fid)
                    texts.append(fdoc)
        if len(texts) < k:
            return 0

        # Re-embed with MiniLM
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        embeddings = np.array(ef(texts), dtype=np.float32)

        # KMeans sub-clustering
        from sklearn.cluster import KMeans

        km = KMeans(n_clusters=k, n_init=10, random_state=42)
        labels = km.fit_predict(embeddings)

        # c-TF-IDF labels for children
        vectorizer = CountVectorizer(stop_words="english")
        tfidf_matrix = TfidfTransformer().fit_transform(
            vectorizer.fit_transform(texts),
        )
        feature_names = vectorizer.get_feature_names_out()

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        child_count = 0
        c_ids: list[str] = []
        c_embs: list[list[float]] = []
        c_metas: list[dict[str, Any]] = []

        with self._lock:
            # Remove parent assignments (docs move to children)
            self.conn.execute(
                "DELETE FROM topic_assignments WHERE topic_id = ?",
                (topic_id,),
            )

            for cid in range(k):
                mask = labels == cid
                if not mask.any():
                    continue

                cluster_tfidf = tfidf_matrix[mask].mean(axis=0).A1
                top_idx = cluster_tfidf.argsort()[-10:][::-1]
                top_terms = [str(feature_names[i]) for i in top_idx]
                label = " ".join(top_terms[:3])
                terms_json = json.dumps(top_terms)
                doc_count = int(mask.sum())

                self.conn.execute(
                    "INSERT INTO topics "
                    "(label, parent_id, collection, doc_count, created_at, terms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (label, topic_id, collection_name, doc_count, now, terms_json),
                )
                child_id = self.conn.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]

                for i in range(len(fetched_ids)):
                    if mask[i]:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO topic_assignments "
                            "(doc_id, topic_id, assigned_by) VALUES (?, ?, 'split')",
                            (fetched_ids[i], child_id),
                        )

                # Centroid for the child topic
                child_centroid = embeddings[mask].mean(axis=0)
                c_ids.append(f"{collection_name}:{child_id}")
                c_embs.append(child_centroid.tolist())
                c_metas.append({
                    "topic_id": child_id,
                    "label": label,
                    "collection": collection_name,
                    "doc_count": doc_count,
                })
                child_count += 1

            # Update parent doc_count to 0 (all docs moved to children)
            self.conn.execute(
                "UPDATE topics SET doc_count = 0 WHERE id = ?", (topic_id,),
            )
            self.conn.commit()

        # Update centroids: remove parent, add children
        centroid_coll = self._create_centroid_collection(chroma_client)
        parent_centroid_id = f"{collection_name}:{topic_id}"
        try:
            centroid_coll.delete(ids=[parent_centroid_id])
        except Exception:
            pass  # Parent centroid may not exist
        if c_ids:
            self._batched_upsert(centroid_coll, c_ids, c_embs, c_metas)

        return child_count

    # ── Rebalance trigger (RDR-070, nexus-1im) ──────────────────────────

    def needs_rebalance(self, collection: str, current_count: int) -> bool:
        """Return True if the corpus has grown >= 2x since last discovery."""
        with self._lock:
            row = self.conn.execute(
                "SELECT last_discover_doc_count FROM taxonomy_meta WHERE collection = ?",
                (collection,),
            ).fetchone()
        if row is None:
            return True  # No prior discovery
        return current_count >= 2 * row[0]

    def record_discover_count(self, collection: str, doc_count: int) -> None:
        """Record the doc count at discovery time."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self.conn.execute(
                "INSERT INTO taxonomy_meta (collection, last_discover_doc_count, last_discover_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(collection) DO UPDATE SET "
                "last_discover_doc_count = excluded.last_discover_doc_count, "
                "last_discover_at = excluded.last_discover_at",
                (collection, doc_count, now),
            )
            self.conn.commit()

    # ── Merge strategy (RDR-070, nexus-1im) ───────────────────────────

    @staticmethod
    def _merge_labels(
        old_centroids: np.ndarray,
        old_labels: list[str],
        old_review_statuses: list[str],
        new_centroids: np.ndarray,
        *,
        threshold: float = 0.8,
    ) -> list[dict[str, Any]]:
        """Match new centroids to old centroids, transfer operator labels.

        Returns a list of dicts (one per new centroid) with:
        - ``label``: transferred label or None (caller uses c-TF-IDF)
        - ``review_status``: 'accepted' if matched, 'pending' if new

        N:1 dedup: each old centroid claimed at most once. If two new
        centroids match the same old centroid above threshold, the
        higher-similarity claimant wins.
        """
        from sklearn.metrics.pairwise import cosine_similarity

        n_new = new_centroids.shape[0]
        result: list[dict[str, Any]] = [
            {"label": None, "review_status": "pending", "old_centroid_idx": -1}
            for _ in range(n_new)
        ]

        if old_centroids.shape[0] == 0:
            return result

        # Dimensionality mismatch guard (model upgrade scenario)
        if old_centroids.shape[1] != new_centroids.shape[1]:
            _log.warning(
                "centroid_dimension_mismatch",
                old_dim=old_centroids.shape[1],
                new_dim=new_centroids.shape[1],
            )
            return result

        # Cosine similarity matrix: (n_new, n_old)
        sims = cosine_similarity(new_centroids, old_centroids)

        # Greedy assignment: highest similarity first, each old used once
        claimed_old: set[int] = set()
        # Build (sim, new_idx, old_idx) sorted descending by sim
        candidates = []
        for new_idx in range(n_new):
            for old_idx in range(old_centroids.shape[0]):
                candidates.append((sims[new_idx, old_idx], new_idx, old_idx))
        candidates.sort(key=lambda x: x[0], reverse=True)

        claimed_new: set[int] = set()
        for sim, new_idx, old_idx in candidates:
            if sim < threshold:
                break  # No more above threshold
            if old_idx in claimed_old or new_idx in claimed_new:
                continue
            result[new_idx] = {
                "label": old_labels[old_idx],
                "review_status": old_review_statuses[old_idx],
                "old_centroid_idx": old_idx,
            }
            claimed_old.add(old_idx)
            claimed_new.add(new_idx)

        return result

    # ── HDBSCAN topic discovery (RDR-070, nexus-9k5) ────────────────────

    @staticmethod
    def _create_centroid_collection(client: Any) -> Any:
        """Create or retrieve the ``taxonomy__centroids`` ChromaDB collection.

        Uses ``embedding_function=None`` (pre-computed MiniLM 384d vectors)
        and ``hnsw:space=cosine`` (RF-070-11). Do NOT use
        ``t3.get_or_create_collection()`` — it injects the wrong EF + L2.
        """
        return client.get_or_create_collection(
            "taxonomy__centroids",
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )

    # Threshold for switching from HDBSCAN to MiniBatchKMeans.
    # HDBSCAN is O(n^2) on high-dimensional data; at 5K+ x 1024d it
    # takes minutes. MiniBatchKMeans is O(n) and produces good clusters.
    _LARGE_COLLECTION_THRESHOLD = 5000

    def _cluster(
        self,
        embeddings: np.ndarray,
        n: int,
        collection_name: str,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Cluster embeddings. Returns (labels, centroids).

        Uses HDBSCAN for small collections (density-based, automatic k).
        Switches to MiniBatchKMeans for large collections (O(n) speed).
        """
        if n <= self._LARGE_COLLECTION_THRESHOLD:
            _log.info("clustering_hdbscan", n=n, collection=collection_name)
            clusterer = SklearnHDBSCAN(
                min_cluster_size=5,
                store_centers="centroid",
                copy=True,
            )
            labels = clusterer.fit_predict(embeddings)
            # HDBSCAN centroids indexed by cluster label
            centroids = getattr(clusterer, "centroids_", np.empty((0, embeddings.shape[1])))
            return labels, centroids

        from sklearn.cluster import MiniBatchKMeans

        k = max(10, int(n ** 0.5 / 3))
        _log.info(
            "clustering_minibatch_kmeans",
            n=n, k=k, collection=collection_name,
        )
        km = MiniBatchKMeans(
            n_clusters=k,
            batch_size=min(1000, n),
            n_init=3,
            random_state=42,
        )
        labels = km.fit_predict(embeddings)
        return labels, km.cluster_centers_

    def discover_topics(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: np.ndarray,
        texts: list[str],
        chroma_client: Any,
    ) -> int:
        """Discover topics from pre-computed embeddings.

        Uses HDBSCAN for collections under 5K docs (density-based, finds
        natural cluster count). Switches to MiniBatchKMeans for larger
        collections (O(n) vs O(n^2), 100-300x faster at scale).

        Generates c-TF-IDF labels from ``texts``, persists topic rows +
        assignments to T2, and upserts cluster centroids to the
        ``taxonomy__centroids`` ChromaDB collection for incremental
        assignment via :meth:`assign_single`.

        Returns number of topics created, or 0 if topics already exist
        for this collection (use ``rebuild_taxonomy`` to re-discover).
        """
        n = len(doc_ids)
        if n < 5:
            return 0

        # Guard: skip if topics already exist for this collection.
        with self._lock:
            existing = self.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE collection = ?",
                (collection_name,),
            ).fetchone()[0]
        if existing > 0:
            _log.info(
                "discover_skip_existing",
                collection=collection_name,
                existing_topics=existing,
            )
            return 0

        labels, centroids = self._cluster(embeddings, n, collection_name)

        real_labels = sorted(set(int(lbl) for lbl in labels if lbl >= 0))
        if not real_labels:
            _log.warning(
                "cluster_all_noise",
                n_docs=n,
                collection=collection_name,
            )
            return 0

        # c-TF-IDF topic labels
        vectorizer = CountVectorizer(stop_words="english")
        tfidf_matrix = TfidfTransformer().fit_transform(
            vectorizer.fit_transform(texts),
        )
        feature_names = vectorizer.get_feature_names_out()

        centroid_coll = self._create_centroid_collection(chroma_client)

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = 0
        c_ids_out: list[str] = []
        c_embs: list[list[float]] = []
        c_metas: list[dict[str, Any]] = []

        with self._lock:
            for cid in real_labels:
                mask = labels == cid

                # Top c-TF-IDF terms: top-3 as label, top-10 stored for review
                cluster_tfidf = tfidf_matrix[mask].mean(axis=0).A1
                top_idx = cluster_tfidf.argsort()[-10:][::-1]
                top_terms = [str(feature_names[i]) for i in top_idx]
                label = " ".join(top_terms[:3])
                terms_json = json.dumps(top_terms)

                doc_count = int(mask.sum())
                self.conn.execute(
                    "INSERT INTO topics (label, collection, doc_count, created_at, terms) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (label, collection_name, doc_count, now, terms_json),
                )
                topic_id = self.conn.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]

                for i in range(n):
                    if mask[i]:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO topic_assignments "
                            "(doc_id, topic_id, assigned_by) VALUES (?, ?, 'hdbscan')",
                            (doc_ids[i], topic_id),
                        )

                c_ids_out.append(f"{collection_name}:{topic_id}")
                c_embs.append(centroids[cid].tolist())
                c_metas.append({
                    "topic_id": topic_id,
                    "label": label,
                    "collection": collection_name,
                    "doc_count": doc_count,
                })
                count += 1

            self.conn.commit()

        # Upsert centroids outside the lock (batched at 300 per write)
        if c_ids_out:
            self._batched_upsert(centroid_coll, c_ids_out, c_embs, c_metas)

        # Cross-collection post-pass: find topic links to other collections'
        # centroids (RDR-075 SC-6, Phase 3).  Lightweight: only new centroids
        # × existing centroids from other collections.
        if c_embs:
            try:
                self._discover_cross_links(
                    collection_name, c_embs, c_metas, centroid_coll,
                )
            except Exception:
                _log.debug("discover_cross_links_failed", exc_info=True)

        # Record doc count for rebalance tracking
        self.record_discover_count(collection_name, n)

        return count

    # ── Cross-collection co-occurrence links (RDR-075 Phase 4, SC-5) ─

    def generate_cooccurrence_links(self) -> int:
        """Generate topic_links from cross-collection projection co-occurrence.

        Uses a SQL self-join to find topic pairs sharing docs across
        different collections, avoiding loading the full assignment
        table into Python memory.

        Uses ``INSERT OR REPLACE`` to merge with existing projection
        links from ``_discover_cross_links``.

        Returns count of links generated.
        """
        with self._lock:
            pairs = self.conn.execute(
                "SELECT MIN(a.topic_id, b.topic_id), MAX(a.topic_id, b.topic_id), "
                "       COUNT(*) "
                "FROM topic_assignments a "
                "JOIN topic_assignments b ON a.doc_id = b.doc_id "
                "JOIN topics ta ON a.topic_id = ta.id "
                "JOIN topics tb ON b.topic_id = tb.id "
                "WHERE a.topic_id < b.topic_id AND ta.collection != tb.collection "
                "GROUP BY MIN(a.topic_id, b.topic_id), MAX(a.topic_id, b.topic_id)"
            ).fetchall()

        if not pairs:
            return 0

        # Two-lock split is intentional: the SQL fetch and the write are
        # separated to avoid holding the lock during the full self-join read.
        # Stale counts between the two windows are acceptable for this batch
        # analytics use case — co-occurrence links are regenerated on each run.
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO topic_links "
                "(from_topic_id, to_topic_id, link_count, link_types) "
                "VALUES (?, ?, ?, ?)",
                [(a, b, count, '["cooccurrence"]') for a, b, count in pairs],
            )
            self.conn.commit()

        _log.info("cooccurrence_links", count=len(pairs))
        return len(pairs)

    # ── Cross-collection centroid linking (RDR-075 Phase 3) ─────────

    _PROJECTION_THRESHOLD = 0.85

    @staticmethod
    def _batched_upsert(
        coll: Any,
        ids: list[str],
        embeddings: list,
        metadatas: list[dict],
        *,
        batch_size: int = 300,
    ) -> None:
        """Batched wrapper for ``coll.upsert()`` — caps at ChromaDB Cloud's
        300-record per-write limit (MAX_RECORDS_PER_WRITE).
        """
        for i in range(0, len(ids), batch_size):
            j = i + batch_size
            coll.upsert(
                ids=ids[i:j],
                embeddings=embeddings[i:j],
                metadatas=metadatas[i:j],
            )

    @staticmethod
    def _paginated_get(
        coll: Any,
        *,
        where: dict | None = None,
        include: list[str] | None = None,
        page_size: int = 300,
    ) -> dict[str, list]:
        """Paginated wrapper for ``coll.get()`` — caps at ChromaDB Cloud's
        300-record limit per call (MAX_QUERY_RESULTS).

        Returns a dict with the same shape as ``coll.get()`` (keys: ids,
        embeddings, metadatas, documents) but with ALL pages concatenated.
        """
        ids: list[str] = []
        embeddings: list[Any] = []
        metadatas: list[Any] = []
        documents: list[Any] = []
        offset = 0
        while True:
            kwargs: dict[str, Any] = {"limit": page_size, "offset": offset}
            if where is not None:
                kwargs["where"] = where
            if include is not None:
                kwargs["include"] = include
            page = coll.get(**kwargs)
            page_ids = page.get("ids") or []
            if not page_ids:
                break
            ids.extend(page_ids)
            if "embeddings" in (include or []):
                page_embs = page.get("embeddings")
                if page_embs is not None:
                    embeddings.extend(page_embs)
            if "metadatas" in (include or []):
                metadatas.extend(page.get("metadatas") or [])
            if "documents" in (include or []):
                documents.extend(page.get("documents") or [])
            if len(page_ids) < page_size:
                break
            offset += page_size

        result: dict[str, list] = {"ids": ids}
        if "embeddings" in (include or []):
            result["embeddings"] = embeddings
        if "metadatas" in (include or []):
            result["metadatas"] = metadatas
        if "documents" in (include or []):
            result["documents"] = documents
        return result

    def _discover_cross_links(
        self,
        collection_name: str,
        new_centroids: list[list[float]],
        new_metas: list[dict[str, Any]],
        centroid_coll: Any,
    ) -> None:
        """Find topic links between new centroids and other collections'.

        After discover creates centroids for *collection_name*, query
        existing centroids from all OTHER collections. Store matches
        above ``_PROJECTION_THRESHOLD`` as ``topic_links`` entries.
        """
        # Fetch all centroids NOT in this collection (paginated, ChromaDB cap = 300)
        try:
            other = self._paginated_get(
                centroid_coll,
                where={"collection": {"$ne": collection_name}},
                include=["embeddings", "metadatas"],
            )
        except Exception:
            return

        other_embs_raw = other.get("embeddings")
        other_metas = other.get("metadatas", [])
        if other_embs_raw is None or len(other_embs_raw) == 0:
            return

        other_embs = np.array(other_embs_raw, dtype=np.float32)
        new_embs = np.array(new_centroids, dtype=np.float32)

        if new_embs.shape[1] != other_embs.shape[1]:
            return

        # Cosine similarity: new centroids × other centroids
        n_norms = np.linalg.norm(new_embs, axis=1, keepdims=True)
        n_norms[n_norms == 0] = 1.0
        o_norms = np.linalg.norm(other_embs, axis=1, keepdims=True)
        o_norms[o_norms == 0] = 1.0
        sim = (new_embs / n_norms) @ (other_embs / o_norms).T

        # Collect pairs outside the lock (numpy work, no DB access)
        pairs: list[tuple[int, int]] = []
        for i, meta in enumerate(new_metas):
            new_tid = int(meta["topic_id"])
            for j in range(sim.shape[1]):
                if float(sim[i, j]) >= self._PROJECTION_THRESHOLD:
                    other_tid = int(other_metas[j]["topic_id"])
                    pairs.append((new_tid, other_tid))

        if pairs:
            with self._lock:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO topic_links "
                    "(from_topic_id, to_topic_id, link_count, link_types) "
                    "VALUES (?, ?, 1, ?)",
                    [(a, b, '["projection"]') for a, b in pairs],
                )
                self.conn.commit()
            _log.info(
                "discover_cross_links",
                collection=collection_name,
                links=len(pairs),
            )

    # ── Centroid dimension guard (RDR-075 SC-10) ─────────────────────

    def assign_single(
        self,
        collection_name: str,
        embedding: np.ndarray,
        chroma_client: Any,
        *,
        cross_collection: bool = False,
    ) -> AssignResult | None:
        """Return the nearest topic_id + raw cosine similarity for one embedding.

        When *cross_collection* is False (default), queries only centroids
        for *collection_name*.  When True, queries only FOREIGN centroids
        (``$ne collection_name``) — used for cross-collection projection
        (RDR-075 SC-6).

        The returned :class:`AssignResult` carries the raw cosine similarity
        (``1.0 - distance``) from ChromaDB. Callers apply ICF weighting at
        query time; the write path stores the raw value only (RDR-077 RF-8).

        Returns ``None`` when the centroid collection does not exist, is
        empty, or dimensions mismatch (SC-10).
        """
        try:
            centroid_coll = chroma_client.get_collection(
                "taxonomy__centroids",
                embedding_function=None,
            )
        except Exception:
            return None

        if centroid_coll.count() == 0:
            return None

        if not _check_centroid_dimension(embedding, centroid_coll):
            return None

        query_kwargs: dict[str, Any] = {
            "query_embeddings": [embedding.tolist()],
            "n_results": 1,
        }
        if cross_collection:
            query_kwargs["where"] = {"collection": {"$ne": collection_name}}
        else:
            query_kwargs["where"] = {"collection": collection_name}

        try:
            results = centroid_coll.query(**query_kwargs)
        except Exception:
            return None  # No centroids match the filter

        if not results["ids"] or not results["ids"][0]:
            return None

        topic_id = int(results["metadatas"][0][0]["topic_id"])
        # ChromaDB returns cosine *distance* as a list-of-lists (one row per
        # input embedding). We get a single row here (n_results=1).
        distance = float(results["distances"][0][0])
        return AssignResult(topic_id=topic_id, similarity=1.0 - distance)

    def assign_batch(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: list[list[float]],
        chroma_client: Any,
        *,
        cross_collection: bool = False,
    ) -> int:
        """Assign multiple docs to their nearest topics via centroid ANN.

        When *cross_collection* is False (default), queries only centroids
        for *collection_name*.  When True, queries only FOREIGN centroids
        (``$ne collection_name``) — used for cross-collection projection
        (RDR-075 SC-6).

        Returns the number of docs successfully assigned.  No-op (returns 0)
        when centroids don't exist or dimensions mismatch.
        """
        try:
            centroid_coll = chroma_client.get_collection(
                "taxonomy__centroids",
                embedding_function=None,
            )
        except Exception:
            return 0

        if centroid_coll.count() == 0:
            return 0

        # Dimension check once for the batch (all embeddings share dimension)
        if embeddings:
            first_emb = embeddings[0]
            sample = np.array(first_emb) if not isinstance(first_emb, np.ndarray) else first_emb
            if not _check_centroid_dimension(sample, centroid_coll):
                return 0

        base_kwargs: dict[str, Any] = {"n_results": 1}
        if cross_collection:
            base_kwargs["where"] = {"collection": {"$ne": collection_name}}
        else:
            base_kwargs["where"] = {"collection": collection_name}

        # Batch query — single ChromaDB round-trip for all embeddings
        emb_list = [
            emb if isinstance(emb, list) else emb.tolist()
            for emb in embeddings
        ]
        try:
            results = centroid_coll.query(
                query_embeddings=emb_list,
                **base_kwargs,
            )
        except Exception:
            return 0

        by = "projection" if cross_collection else "centroid"
        # RDR-077 C-1: capture per-row distance → similarity; source_collection
        # is the caller's *collection_name* (that's where the doc lives).
        assigned = 0
        for i, doc_id in enumerate(doc_ids):
            if not results["ids"][i]:
                continue
            topic_id = int(results["metadatas"][i][0]["topic_id"])
            if by == "projection":
                distance = float(results["distances"][i][0])
                self.assign_topic(
                    doc_id,
                    topic_id,
                    assigned_by=by,
                    similarity=1.0 - distance,
                    source_collection=collection_name,
                )
            else:
                self.assign_topic(doc_id, topic_id, assigned_by=by)
            assigned += 1

        return assigned

    # ── Cross-collection projection (RDR-075 Phase 2) ──────────────

    def project_against(
        self,
        source_collection: str,
        target_collections: list[str],
        chroma_client: Any,
        *,
        threshold: float = 0.85,
        top_k: int = 3,
        icf_map: dict[int, float] | None = None,
        progress: bool = False,
    ) -> dict[str, Any]:
        """Project source collection chunks against target collection centroids.

        Computes cosine similarity between all source chunk embeddings and
        all target centroid embeddings via a single matrix multiply.

        Returns a dict with ``matched_topics``, ``novel_chunks``,
        ``total_chunks``, and ``total_centroids``.

        When *icf_map* is provided (RDR-077 Phase 4a), each raw cosine is
        multiplied by ``icf_map[target_topic_id]`` (default 1.0 when the
        topic is missing — e.g., a freshly created topic not yet in the
        ICF map) before the threshold filter and top-K ranking. This
        suppresses ubiquitous "hub" topics that carry generic signal.

        **Raw-cosine-storage invariant**: the ``similarity`` component of
        each ``chunk_assignments`` tuple is ALWAYS the raw cosine — ICF is
        applied only for filtering and ranking, never stored. Callers may
        recompute adjusted scores at query time from the raw value plus a
        fresh ICF map.

        Raises ``ValueError`` on embedding dimension mismatch.

        When *progress* is True, key stages emit one-line progress messages
        to stderr (source fetch per page, centroid fetch, similarity
        compute, per-row aggregation, final counts). The CLI
        (``nx taxonomy project``) sets this to True by default so long-
        running projections are observable; test callers leave it False.
        """
        import sys
        import time

        def _emit(msg: str) -> None:
            if progress:
                print(f"  {msg}", file=sys.stderr, flush=True)

        # 1. Fetch source embeddings
        try:
            src_coll = chroma_client.get_collection(
                source_collection, embedding_function=None,
            )
        except Exception:
            return {
                "matched_topics": [],
                "novel_chunks": [],
                "total_chunks": 0,
                "total_centroids": 0,
            }

        # Paginated fetch — ChromaDB Cloud caps Get at 300 (MAX_QUERY_RESULTS)
        _PAGE = 300
        src_ids: list[str] = []
        src_emb_pages: list[np.ndarray] = []
        offset = 0
        t0 = time.monotonic()
        while True:
            page = src_coll.get(include=["embeddings"], limit=_PAGE, offset=offset)
            page_ids = page.get("ids", [])
            page_embs = page.get("embeddings")
            if not page_ids or page_embs is None:
                break
            src_ids.extend(page_ids)
            src_emb_pages.append(np.array(page_embs, dtype=np.float32))
            if len(src_ids) % 3000 == 0 or len(page_ids) < _PAGE:
                _emit(
                    f"fetching source {source_collection}: "
                    f"{len(src_ids)} chunks ({time.monotonic() - t0:.1f}s)"
                )
            if len(page_ids) < _PAGE:
                break
            offset += _PAGE

        if not src_ids:
            return {
                "matched_topics": [],
                "novel_chunks": [],
                "total_chunks": 0,
                "total_centroids": 0,
            }
        src_embs = np.concatenate(src_emb_pages)
        _emit(
            f"source fetch complete: {len(src_ids)} chunks, "
            f"{src_embs.shape[1]}d ({time.monotonic() - t0:.1f}s)"
        )

        # 2. Fetch target centroids
        try:
            centroid_coll = self._create_centroid_collection(chroma_client)
        except Exception:
            return {
                "matched_topics": [],
                "novel_chunks": list(src_ids),
                "total_chunks": len(src_ids),
                "total_centroids": 0,
            }

        if centroid_coll.count() == 0:
            return {
                "matched_topics": [],
                "novel_chunks": list(src_ids),
                "total_chunks": len(src_ids),
                "total_centroids": 0,
            }

        # Filter centroids to target collections (paginated, ChromaDB cap = 300)
        ctr_data = self._paginated_get(
            centroid_coll,
            where={"collection": {"$in": target_collections}},
            include=["embeddings", "metadatas"],
        )
        ctr_raw = ctr_data.get("embeddings")
        ctr_metas = ctr_data.get("metadatas", [])
        if ctr_raw is None or len(ctr_raw) == 0 or not ctr_metas:
            return {
                "matched_topics": [],
                "novel_chunks": list(src_ids),
                "total_chunks": len(src_ids),
                "total_centroids": 0,
            }
        ctr_embs = np.array(ctr_raw, dtype=np.float32)
        _emit(
            f"centroids fetched: {len(ctr_metas)} topics across "
            f"{len(target_collections)} target collection(s)"
        )

        # 3. Dimension check
        if src_embs.shape[1] != ctr_embs.shape[1]:
            raise ValueError(
                f"Dimension mismatch: source embeddings {src_embs.shape[1]}d, "
                f"centroids {ctr_embs.shape[1]}d"
            )

        # 4. Normalize for cosine similarity via dot product
        src_norms = np.linalg.norm(src_embs, axis=1, keepdims=True)
        src_norms[src_norms == 0] = 1.0
        src_norm = src_embs / src_norms

        ctr_norms = np.linalg.norm(ctr_embs, axis=1, keepdims=True)
        ctr_norms[ctr_norms == 0] = 1.0
        ctr_norm = ctr_embs / ctr_norms

        # 5. Similarity matrix: (N, M) — values in [-1, 1]
        t_sim = time.monotonic()
        sim = src_norm @ ctr_norm.T
        _emit(
            f"similarity matrix computed: ({len(src_ids)}x{len(ctr_metas)}) "
            f"in {time.monotonic() - t_sim:.1f}s"
        )

        # 5b. Adjusted matrix for filter + ranking (RDR-077 Phase 4a).
        # Raw `sim` is preserved for storage; `filter_sim` is what we
        # compare against *threshold* and use to pick top-K.
        if icf_map:
            # Build a (1, M) vector of per-centroid ICF weights, defaulting
            # to 1.0 for topics not present in the map (new topics or
            # N_effective<2 fallbacks).
            icf_weights = np.array(
                [icf_map.get(int(m["topic_id"]), 1.0) for m in ctr_metas],
                dtype=np.float32,
            )
            filter_sim = sim * icf_weights  # broadcasts over rows
        else:
            filter_sim = sim

        # 6. Aggregate matched topics and collect per-chunk assignments.
        topic_stats: dict[int, dict[str, Any]] = {}
        novel_chunks: list[str] = []
        # RDR-077 RF-3 + Phase 4a: 3-tuple stores RAW cosine similarity.
        chunk_assignments: list[tuple[str, int, float]] = []

        t_agg = time.monotonic()
        for i, doc_id in enumerate(src_ids):
            if progress and i and i % 5000 == 0:
                _emit(
                    f"aggregating: {i}/{len(src_ids)} chunks "
                    f"({len(chunk_assignments)} matched so far, "
                    f"{time.monotonic() - t_agg:.1f}s)"
                )
            row_max = float(filter_sim[i].max())
            if row_max < threshold:
                novel_chunks.append(doc_id)
                continue

            # Top-k centroids above threshold — ranked by adjusted score
            # when ICF is active, else raw.
            top_indices = np.argsort(-filter_sim[i])[:top_k]
            for idx in top_indices:
                if float(filter_sim[i, idx]) < threshold:
                    break
                meta = ctr_metas[idx]
                tid = int(meta["topic_id"])
                raw_sim = float(sim[i, idx])
                chunk_assignments.append((doc_id, tid, raw_sim))
                if tid not in topic_stats:
                    topic_stats[tid] = {
                        "topic_id": tid,
                        "label": meta.get("label", ""),
                        "collection": meta.get("collection", ""),
                        "chunk_count": 0,
                        "total_similarity": 0.0,
                    }
                topic_stats[tid]["chunk_count"] += 1
                topic_stats[tid]["total_similarity"] += raw_sim

        matched_topics = [
            {
                "topic_id": s["topic_id"],
                "label": s["label"],
                "collection": s["collection"],
                "chunk_count": s["chunk_count"],
                "avg_similarity": s["total_similarity"] / s["chunk_count"],
            }
            for s in sorted(topic_stats.values(), key=lambda x: x["chunk_count"], reverse=True)
        ]

        _log.info(
            "project_against",
            source=source_collection,
            targets=len(target_collections),
            chunks=len(src_ids),
            centroids=len(ctr_metas),
            matched_topics=len(matched_topics),
            novel=len(novel_chunks),
            assignments=len(chunk_assignments),
            threshold=threshold,
        )

        return {
            "matched_topics": matched_topics,
            "novel_chunks": novel_chunks,
            "chunk_assignments": chunk_assignments,
            "total_chunks": len(src_ids),
            "total_centroids": len(ctr_metas),
        }

    def purge_collection(self, collection: str) -> dict[str, int]:
        """Cascade-purge every row tied to *collection* across the four
        taxonomy tables. Transactional — any failure rolls back all
        deletes in this call.

        Returns a count dict: ``{"topics", "assignments", "links",
        "meta"}``. nexus-lub regression: `nx collection delete` was
        removing the Chroma collection but leaving these four tables
        orphaned. Call this after the Chroma delete.

        Order of operations matters:
          1. ``topic_links`` — drop every edge touching a doomed topic
             (in either direction). Must run before we drop the topics
             themselves to satisfy the FK reference.
          2. ``topic_assignments`` — drop every row whose ``topic_id``
             belongs to the collection OR whose ``source_collection``
             equals it (projection residue).
          3. ``topics`` — drop the collection's topic rows.
          4. ``taxonomy_meta`` — drop the collection's `last_discover_at`
             bookkeeping row.
        """
        out = {"topics": 0, "assignments": 0, "links": 0, "meta": 0}
        with self._lock:
            try:
                doomed_ids = [
                    r[0] for r in self.conn.execute(
                        "SELECT id FROM topics WHERE collection = ?",
                        (collection,),
                    ).fetchall()
                ]
                if doomed_ids:
                    placeholders = ",".join("?" for _ in doomed_ids)
                    # 1. links touching any doomed topic
                    cur = self.conn.execute(
                        f"DELETE FROM topic_links "
                        f"WHERE from_topic_id IN ({placeholders}) "
                        f"   OR to_topic_id IN ({placeholders})",
                        (*doomed_ids, *doomed_ids),
                    )
                    out["links"] = cur.rowcount or 0
                    # 2a. assignments by topic_id
                    cur = self.conn.execute(
                        f"DELETE FROM topic_assignments "
                        f"WHERE topic_id IN ({placeholders})",
                        tuple(doomed_ids),
                    )
                    out["assignments"] = cur.rowcount or 0
                # 2b. assignments by source_collection (projection residue
                #     whose target topic may live in a surviving collection)
                cur = self.conn.execute(
                    "DELETE FROM topic_assignments "
                    "WHERE source_collection = ?",
                    (collection,),
                )
                out["assignments"] += cur.rowcount or 0
                # 3. topics
                cur = self.conn.execute(
                    "DELETE FROM topics WHERE collection = ?",
                    (collection,),
                )
                out["topics"] = cur.rowcount or 0
                # 4. meta
                cur = self.conn.execute(
                    "DELETE FROM taxonomy_meta WHERE collection = ?",
                    (collection,),
                )
                out["meta"] = cur.rowcount or 0
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return out

    def rename_collection(self, old: str, new: str) -> dict[str, int]:
        """Re-point every taxonomy row from ``old`` → ``new``.

        nexus-1ccq: `nx collection rename` cascade. Updates three
        tables in one transaction:

          * ``topics.collection`` — where the cluster lives
          * ``topic_assignments.source_collection`` — projection origin
          * ``taxonomy_meta.collection`` — discovery bookkeeping

        ``topic_links`` is keyed by topic_id (FK), not by collection
        name, so links survive unchanged. Returns count dict mirroring
        ``purge_collection`` so the CLI can surface the row counts.
        Transactional — partial failure rolls back.
        """
        out = {"topics": 0, "assignments": 0, "meta": 0}
        with self._lock:
            try:
                cur = self.conn.execute(
                    "UPDATE topics SET collection = ? WHERE collection = ?",
                    (new, old),
                )
                out["topics"] = cur.rowcount or 0
                cur = self.conn.execute(
                    "UPDATE topic_assignments SET source_collection = ? "
                    "WHERE source_collection = ?",
                    (new, old),
                )
                out["assignments"] = cur.rowcount or 0
                cur = self.conn.execute(
                    "UPDATE taxonomy_meta SET collection = ? WHERE collection = ?",
                    (new, old),
                )
                out["meta"] = cur.rowcount or 0
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return out

    def purge_assignments_for_doc(self, project: str, title: str) -> int:
        """Remove topic_assignments for a deleted memory entry, empty topics.

        Called by ``T2Database.delete()`` after a successful memory row
        deletion to prevent orphan ``topic_assignments`` rows (v3.8.0
        shakeout finding). The ``topic_assignments.doc_id`` column
        references ``memory.title`` by value, not by SQL foreign key, so
        deleting a memory row by itself leaves dangling assignments that
        show up in ``nx taxonomy list`` and ``nx taxonomy show`` as
        ghost entries.

        This method is idempotent and scoped: it only affects topics in
        the given ``project`` (taxonomy's ``collection`` column) to
        avoid accidentally touching unrelated taxonomies that might
        reference the same title under a different project.

        Two-step operation under ``self._lock``:

        1. DELETE matching ``topic_assignments`` rows (``doc_id = title``
           AND parent topic's ``collection = project``).
        2. DELETE any topics in that collection whose ``doc_count``
           implicit count has dropped to zero — i.e. topics that now
           have no remaining assignments.

        Returns the number of ``topic_assignments`` rows removed. The
        count of cleaned-up topics is not returned because it is
        derivable by the caller if needed and the common case is
        "either 0 or exactly 1 assignment removed".
        """
        with self._lock:
            cursor = self.conn.execute(
                """
                DELETE FROM topic_assignments
                WHERE doc_id = ?
                  AND topic_id IN (
                      SELECT id FROM topics WHERE collection = ?
                  )
                """,
                (title, project),
            )
            removed = cursor.rowcount
            # Drop topics in this collection that no longer have any
            # assignments. Scoped by collection so we don't disturb
            # siblings from other projects.
            self.conn.execute(
                """
                DELETE FROM topics
                WHERE collection = ?
                  AND id NOT IN (
                      SELECT DISTINCT topic_id FROM topic_assignments
                  )
                """,
                (project,),
            )
            self.conn.commit()
        return removed

    def rebuild_taxonomy(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: np.ndarray,
        texts: list[str],
        chroma_client: Any,
    ) -> int:
        """Full rebuild with merge strategy for label preservation.

        1. Read old centroids + labels + manual assignments
        2. Clear old data
        3. Run HDBSCAN on new embeddings
        4. Match new centroids to old via ``_merge_labels``
        5. Transfer operator labels and manual assignments

        RACE WINDOW: Between Step 2 (T2 DELETE committed) and the final
        centroid upsert, concurrent ``assign_single`` calls may see stale
        or empty centroids. Centroid operations run outside ``self._lock``
        to avoid blocking T2 readers during ChromaDB I/O. This is an
        accepted trade-off — taxonomy rebuild is an infrequent operator
        action, not a hot path.
        6. Record doc count for rebalance tracking
        """
        # ── Step 1: read old state ────────────────────────────────────
        old_centroids = np.empty((0, 0), dtype=np.float32)
        old_labels: list[str] = []
        old_review_statuses: list[str] = []

        centroid_coll = self._create_centroid_collection(chroma_client)

        # Read old T2 topics (id -> label, review_status) — authoritative
        # for operator renames. ChromaDB metadata may still have the
        # original c-TF-IDF label.
        with self._lock:
            old_topic_rows = self.conn.execute(
                "SELECT id, label, review_status FROM topics WHERE collection = ?",
                (collection_name,),
            ).fetchall()
        old_topic_map: dict[int, tuple[str, str]] = {
            r[0]: (r[1], r[2]) for r in old_topic_rows
        }

        # Read old centroids from ChromaDB, resolve labels from T2
        # (paginated, ChromaDB cap = 300)
        old_centroid_topic_ids: list[int] = []  # old topic_id per centroid index
        try:
            old_data = self._paginated_get(
                centroid_coll,
                where={"collection": collection_name},
                include=["embeddings", "metadatas"],
            )
            if old_data["embeddings"] is not None and len(old_data["embeddings"]) > 0:
                old_centroids = np.array(old_data["embeddings"], dtype=np.float32)
                for m in old_data["metadatas"]:
                    tid = m.get("topic_id", -1)
                    old_centroid_topic_ids.append(tid)
                    if tid in old_topic_map:
                        old_labels.append(old_topic_map[tid][0])
                        old_review_statuses.append(old_topic_map[tid][1])
                    else:
                        old_labels.append(m.get("label", ""))
                        old_review_statuses.append("pending")
        except Exception:
            pass

        # Read manual assignments (doc_id -> old_topic_id)
        with self._lock:
            manual_rows = self.conn.execute(
                "SELECT ta.doc_id, ta.topic_id FROM topic_assignments ta "
                "JOIN topics t ON t.id = ta.topic_id "
                "WHERE ta.assigned_by = 'manual' AND t.collection = ?",
                (collection_name,),
            ).fetchall()
        manual_assignments: dict[str, int] = {r[0]: r[1] for r in manual_rows}

        # ── Step 2: clear old data ────────────────────────────────────
        with self._lock:
            self.conn.execute(
                "DELETE FROM topic_assignments WHERE topic_id IN "
                "(SELECT id FROM topics WHERE collection = ?)",
                (collection_name,),
            )
            self.conn.execute(
                "DELETE FROM topics WHERE collection = ?", (collection_name,),
            )
            self.conn.commit()

        # Clear old centroids from ChromaDB.  Paginated GET (cap 300) +
        # batched DELETE (MAX_RECORDS_PER_WRITE = 300).
        old_centroid_ids = self._paginated_get(
            centroid_coll,
            where={"collection": collection_name},
        ).get("ids", [])
        for i in range(0, len(old_centroid_ids), 300):
            centroid_coll.delete(ids=old_centroid_ids[i:i + 300])

        # ── Step 3: HDBSCAN ──────────────────────────────────────────
        n = len(doc_ids)
        if n < 5:
            self.record_discover_count(collection_name, n)
            return 0

        labels, centroids_arr = self._cluster(embeddings, n, collection_name)

        real_labels = sorted(set(int(lbl) for lbl in labels if lbl >= 0))
        if not real_labels:
            _log.warning(
                "rebuild_all_noise",
                collection=collection_name,
                n_docs=n,
            )
            self.record_discover_count(collection_name, n)
            return 0

        # c-TF-IDF
        vectorizer = CountVectorizer(stop_words="english")
        tfidf_matrix = TfidfTransformer().fit_transform(
            vectorizer.fit_transform(texts),
        )
        feature_names = vectorizer.get_feature_names_out()

        # ── Step 4: merge labels ──────────────────────────────────────
        new_centroids_arr = np.array(
            [centroids_arr[cid] for cid in real_labels], dtype=np.float32,
        )
        merged = self._merge_labels(
            old_centroids, old_labels, old_review_statuses, new_centroids_arr,
        )

        # ── Step 5: persist ───────────────────────────────────────────
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = 0
        c_ids: list[str] = []
        c_embs: list[list[float]] = []
        c_metas: list[dict[str, Any]] = []

        # Map new cluster index -> topic_id for manual assignment transfer
        new_topic_ids: list[int] = []

        with self._lock:
            for idx, cid in enumerate(real_labels):
                mask = labels == cid

                cluster_tfidf = tfidf_matrix[mask].mean(axis=0).A1
                top_idx = cluster_tfidf.argsort()[-10:][::-1]
                top_terms = [str(feature_names[i]) for i in top_idx]
                tfidf_label = " ".join(top_terms[:3])
                terms_json = json.dumps(top_terms)

                # Use merged label if available, else c-TF-IDF
                merged_info = merged[idx]
                label = merged_info["label"] or tfidf_label
                review_status = merged_info["review_status"]

                doc_count = int(mask.sum())
                self.conn.execute(
                    "INSERT INTO topics "
                    "(label, collection, doc_count, created_at, terms, review_status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (label, collection_name, doc_count, now, terms_json, review_status),
                )
                topic_id = self.conn.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]
                new_topic_ids.append(topic_id)

                for i in range(n):
                    if mask[i]:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO topic_assignments "
                            "(doc_id, topic_id, assigned_by) VALUES (?, ?, ?)",
                            (
                                doc_ids[i],
                                topic_id,
                                "auto-matched" if merged_info["label"] else "hdbscan",
                            ),
                        )

                c_ids.append(f"{collection_name}:{topic_id}")
                c_embs.append(centroids_arr[cid].tolist())
                c_metas.append({
                    "topic_id": topic_id,
                    "label": label,
                    "collection": collection_name,
                    "doc_count": doc_count,
                })
                count += 1

            # Transfer manual assignments.
            # Build old_topic_id -> new_topic_id map from _merge_labels output
            old_to_new_topic: dict[int, int] = {}
            if old_centroid_topic_ids and new_topic_ids:
                for new_idx, merge_info in enumerate(merged):
                    old_idx = merge_info.get("old_centroid_idx", -1)
                    if merge_info["label"] is not None and 0 <= old_idx < len(old_centroid_topic_ids):
                        old_tid = old_centroid_topic_ids[old_idx]
                        old_to_new_topic[old_tid] = new_topic_ids[new_idx]

            if manual_assignments:
                doc_id_to_idx = {did: i for i, did in enumerate(doc_ids)}
                for manual_doc, old_topic_id in manual_assignments.items():
                    # Route 1: old topic was matched to a new topic
                    if old_topic_id in old_to_new_topic:
                        target = old_to_new_topic[old_topic_id]
                        self.conn.execute(
                            "INSERT OR REPLACE INTO topic_assignments "
                            "(doc_id, topic_id, assigned_by) VALUES (?, ?, 'manual')",
                            (manual_doc, target),
                        )
                        continue

                    # Route 2: doc is in the current corpus — use embedding
                    if manual_doc in doc_id_to_idx:
                        from sklearn.metrics.pairwise import cosine_similarity as _cos_sim
                        doc_emb = embeddings[doc_id_to_idx[manual_doc] : doc_id_to_idx[manual_doc] + 1]
                        sims = _cos_sim(doc_emb, new_centroids_arr)[0]
                        best_idx = int(sims.argmax())
                        if float(sims[best_idx]) > 0.5:
                            self.conn.execute(
                                "INSERT OR REPLACE INTO topic_assignments "
                                "(doc_id, topic_id, assigned_by) VALUES (?, ?, 'manual')",
                                (manual_doc, new_topic_ids[best_idx]),
                            )
                            continue

                    # Route 3: manual assignment could not be placed
                    _log.warning(
                        "manual_assignment_lost",
                        doc_id=manual_doc,
                        old_topic_id=old_topic_id,
                        collection=collection_name,
                    )

            self.conn.commit()

        # Upsert new centroids (batched at 300 per write)
        if c_ids:
            self._batched_upsert(centroid_coll, c_ids, c_embs, c_metas)

        # Record doc count for rebalance tracking
        self.record_discover_count(collection_name, n)

        return count
