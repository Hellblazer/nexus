# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CatalogTaxonomy — topics + topic_assignments domain (RDR-063).

Owns the ``topics`` and ``topic_assignments`` tables. Extracted from
the legacy ``nexus.taxonomy`` module + the monolithic ``T2Database``
in RDR-063 Phase 1 steps 4-5 (bead ``nexus-u29l``); promoted to own
its dedicated ``sqlite3.Connection`` and ``threading.Lock`` in Phase 2
(bead ``nexus-3d3k``).

Why the Phase 1 extraction was non-mechanical: the old
``nexus.taxonomy`` module reached through the monolithic T2's lock and
connection directly across 7 top-level functions (22 grep hits, 7
lock acquisitions, 12 execute calls, 3 commits). Each function had
its own transaction shape — one or two reads under one lock,
multi-statement writes with explicit ``commit()``, etc. The
translation onto ``self._lock`` / ``self.conn`` preserves each
function's shape exactly; do not collapse "two queries under one lock"
into one query.

Cross-domain dependency (RDR-063 §Cross-Domain Contracts):

- ``cluster_and_persist`` reads memory entries via :class:`MemoryStore`
  to build word-frequency vectors. The dependency is made explicit by
  injecting a ``MemoryStore`` reference at construction; the JOIN that
  binds ``topic_assignments.doc_id`` to ``memory.title`` continues to
  live in SQL inside :meth:`get_topic_docs`. After the per-store
  connection split the JOIN still works — it runs on this store's own
  connection against the single SQLite file shared by all domains.
- ``get_topic_docs`` carries the **Known Defect** (per RDR-063 Open
  Question 1, Option 3): when topics were clustered from a T3
  collection (``code__*``, ``knowledge__*``, …) the
  ``topics.collection`` value does not match any ``memory.project``,
  so the LEFT JOIN finds no row and the returned ``title`` falls back
  to the raw ``doc_id``. This is documented, not fixed — the docstring
  on :meth:`get_topic_docs` is the contract and
  ``test_get_topic_docs_known_defect_project_collection_mismatch``
  pins it.

Lock ownership convention (matches MemoryStore / PlanLibrary):

- Public methods acquire ``self._lock`` themselves.
- ``_init_schema`` runs under ``self._lock`` during ``__init__`` and
  runs ``_migrate_topics_if_needed`` under the per-domain
  ``_migrated_lock`` guard.
- ``get_topic_tree`` deliberately acquires the lock once for the root
  fetch and once *per recursive child fetch*. This matches the
  pre-split behavior: a snapshot per query, with the lock released
  between queries so concurrent writers (e.g. another agent calling
  ``cluster_and_persist``) are not blocked for the duration of the
  walk. The recursive ``_build_node`` calls are made with the lock
  released, so there is no re-entry — ``threading.Lock`` is
  non-reentrant, and any nesting would have deadlocked the existing
  test suite.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog

if TYPE_CHECKING:
    from nexus.db.t2.memory_store import MemoryStore

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
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_assignments (
    doc_id    TEXT NOT NULL,
    topic_id  INTEGER NOT NULL REFERENCES topics(id),
    PRIMARY KEY (doc_id, topic_id)
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
)

# Stopword list distinct from MemoryStore._STOPWORDS — taxonomy clustering
# operates on natural-language word vectors and benefits from a longer
# stop list. MemoryStore's list (22 words) targets short title-fragment
# matching for find_overlapping_memories.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "in", "of", "for", "to", "and", "or", "is", "are", "was",
        "it", "that", "this", "with", "on", "at", "by", "from", "as", "be", "not",
        "but", "have", "has", "had", "will", "would", "can", "could", "should",
        "may", "might", "must", "shall", "do", "does", "did", "been", "being",
        "if", "then", "else", "when", "where", "which", "who", "what", "how",
    }
)


# ── CatalogTaxonomy ───────────────────────────────────────────────────────────


class CatalogTaxonomy:
    """Owns the ``topics`` and ``topic_assignments`` tables.

    Constructor takes a :class:`MemoryStore` reference because
    :meth:`cluster_and_persist` reads memory entries to build the
    word-frequency vectors that drive Ward clustering. This makes the
    Taxonomy → Memory coupling visible in the constructor signature
    (RDR-063 §Cross-Domain Contracts).
    """

    def __init__(self, path: Path, memory: "MemoryStore") -> None:
        self._memory = memory
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
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
        """Create the topics tables and run the topics migration if needed."""
        with self._lock:
            self.conn.executescript(_TAXONOMY_SCHEMA_SQL)
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.commit()
            with _migrated_lock:
                if path_key in _migrated_paths:
                    return
                self._migrate_topics_if_needed()
                _migrated_paths.add(path_key)

    def _migrate_topics_if_needed(self) -> None:
        """Add topics and topic_assignments tables if missing.

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        Kept as a separate migration path distinct from the base
        ``_TAXONOMY_SCHEMA_SQL`` script because some pre-RDR-061
        databases predate the topics tables and the migration log
        emits a structured event when it fires.
        """
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topics'"
        ).fetchone()
        if row is not None:
            return
        _log.info("Migrating T2 schema to add topics tables")
        self.conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS topics (
                id            INTEGER PRIMARY KEY,
                label         TEXT NOT NULL,
                parent_id     INTEGER REFERENCES topics(id),
                collection    TEXT NOT NULL,
                centroid_hash TEXT,
                doc_count     INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_assignments (
                doc_id    TEXT NOT NULL,
                topic_id  INTEGER NOT NULL REFERENCES topics(id),
                PRIMARY KEY (doc_id, topic_id)
            );
        """
        )
        self.conn.commit()
        _log.info("topics migration complete")

    # ── Public API ────────────────────────────────────────────────────────

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
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                    "FROM topics WHERE parent_id IS NULL ORDER BY doc_count DESC"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                    "FROM topics WHERE parent_id = ? ORDER BY doc_count DESC",
                    (parent_id,),
                ).fetchall()
        return [dict(zip(_TOPIC_COLUMNS, row)) for row in rows]

    def assign_topic(self, doc_id: str, topic_id: int) -> None:
        """Assign a document to a topic (idempotent via INSERT OR IGNORE)."""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                (doc_id, topic_id),
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
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                    "FROM topics WHERE parent_id IS NULL AND collection = ? ORDER BY doc_count DESC",
                    (collection,),
                ).fetchall()
            else:
                roots = self.conn.execute(
                    "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                    "FROM topics WHERE parent_id IS NULL ORDER BY doc_count DESC"
                ).fetchall()

        def _build_node(row: tuple, depth: int) -> dict[str, Any]:
            node = {"id": row[0], "label": row[1], "collection": row[3], "doc_count": row[5]}
            if depth < max_depth:
                with self._lock:
                    children = self.conn.execute(
                        "SELECT id, label, parent_id, collection, centroid_hash, doc_count, created_at "
                        "FROM topics WHERE parent_id = ? ORDER BY doc_count DESC",
                        (row[0],),
                    ).fetchall()
                # Recurse OUTSIDE the lock — see method docstring.
                node["children"] = [_build_node(c, depth + 1) for c in children]
            else:
                node["children"] = []
            return node

        return [_build_node(r, 0) for r in roots]

    def cluster_and_persist(
        self,
        project: str,
        *,
        k: int | None = None,
    ) -> int:
        """Cluster memory entries by content word vectors, persist topics to T2.

        Uses simple word-frequency vectors (no external embeddings required)
        and the existing :func:`nexus.search_clusterer.cluster_results`
        engine.

        Cross-domain read: pulls memory entries via the injected
        :class:`MemoryStore` reference. The taxonomy → memory coupling is
        explicit at the constructor signature.

        Returns number of topics created.
        """
        from nexus.search_clusterer import cluster_results

        entries = self._memory.get_all(project)
        if len(entries) < 3:
            return 0

        # Build word-frequency vectors capped to top-N most frequent words.
        # Without a cap, vocab grows unboundedly with content size (1000 entries
        # × 500 words ≈ 2GB float32 matrix).
        MAX_VOCAB = 2000
        word_counts: dict[str, int] = {}
        for e in entries:
            for word in e.get("content", "").lower().split():
                if len(word) > 2 and word not in _STOPWORDS:
                    word_counts[word] = word_counts.get(word, 0) + 1

        if not word_counts:
            return 0

        # Keep top-N by frequency, then assign stable indices
        top_words = sorted(word_counts.items(), key=lambda kv: -kv[1])[:MAX_VOCAB]
        vocab: dict[str, int] = {word: i for i, (word, _) in enumerate(top_words)}

        dim = len(vocab)
        embeddings = np.zeros((len(entries), dim), dtype=np.float32)
        for i, e in enumerate(entries):
            for word in e.get("content", "").lower().split():
                idx = vocab.get(word)
                if idx is not None:
                    embeddings[i, idx] += 1.0

        # Normalize rows
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.maximum(norms, 1e-9)

        # Build result dicts for cluster_results API.
        # IMPORTANT: "id" must equal memory.title — get_topic_docs() JOINs
        # topic_assignments.doc_id against memory.title to resolve titles.
        result_dicts = [
            {
                "id": e["title"],
                "content": e.get("content", ""),
                "distance": 0.0,
                "metadata": {"title": e["title"]},
            }
            for e in entries
        ]

        if k is None:
            k = max(2, math.ceil(len(entries) / 5))

        clusters = cluster_results(result_dicts, embeddings, k=k)

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = 0
        with self._lock:
            for cluster in clusters:
                if not cluster:
                    continue
                label = cluster[0].get("_cluster_label", f"topic-{count}")
                self.conn.execute(
                    "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
                    (label, project, len(cluster), now),
                )
                topic_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for r in cluster:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                        (r["id"], topic_id),
                    )
                count += 1
            self.conn.commit()

        return count

    def rebuild_taxonomy(
        self,
        project: str,
        *,
        k: int | None = None,
    ) -> int:
        """Full rebuild: delete existing topics for project, recluster.

        DELETE topic_assignments and topics for the project in one
        transaction (single lock acquisition + commit), then call
        :meth:`cluster_and_persist` AFTER the lock is released. The
        re-cluster takes its own lock for the new INSERTs — never nested
        with the DELETE lock.
        """
        with self._lock:
            self.conn.execute(
                "DELETE FROM topic_assignments WHERE topic_id IN "
                "(SELECT id FROM topics WHERE collection = ?)",
                (project,),
            )
            self.conn.execute("DELETE FROM topics WHERE collection = ?", (project,))
            self.conn.commit()
        return self.cluster_and_persist(project, k=k)
