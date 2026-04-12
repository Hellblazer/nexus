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
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog

if TYPE_CHECKING:
    from nexus.db.t2.memory_store import MemoryStore

# RDR-070 (nexus-9k5): scikit-learn>=1.3 is a core dep. sklearn.cluster.HDBSCAN
# replaces BERTopic for topic discovery — same algorithm, no 500MB torch chain.
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

CREATE TABLE IF NOT EXISTS topic_assignments (
    doc_id      TEXT NOT NULL,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    assigned_by TEXT NOT NULL DEFAULT 'hdbscan',
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
    "review_status",
    "terms",
)

# ── CatalogTaxonomy ───────────────────────────────────────────────────────────


class CatalogTaxonomy:
    """Owns the ``topics`` and ``topic_assignments`` tables.

    RDR-070 (nexus-9k5): topic discovery uses sklearn HDBSCAN on
    pre-computed embeddings. Centroids stored in ChromaDB
    ``taxonomy__centroids`` collection. c-TF-IDF labels via
    CountVectorizer + TfidfTransformer. No BERTopic, no PyTorch.

    Constructor takes a :class:`MemoryStore` reference for
    :meth:`get_topic_docs` JOIN resolution (RDR-063 §Cross-Domain
    Contracts).
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

    def _migrate_assigned_by_if_needed(self) -> None:
        """Add ``assigned_by`` column to ``topic_assignments`` if missing.

        RDR-070 (nexus-9k5). Lock-naive: caller must hold both locks.
        """
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
        }
        if "assigned_by" in cols:
            return
        _log.info("Migrating topic_assignments: adding assigned_by column")
        self.conn.execute(
            "ALTER TABLE topic_assignments ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'hdbscan'"
        )
        self.conn.commit()

    def _migrate_review_columns_if_needed(self) -> None:
        """Add ``review_status`` and ``terms`` columns if missing.

        RDR-070 (nexus-lbu). Lock-naive: caller must hold both locks.
        """
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        if "review_status" not in cols:
            _log.info("Migrating topics: adding review_status column")
            self.conn.execute(
                "ALTER TABLE topics ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "terms" not in cols:
            _log.info("Migrating topics: adding terms column")
            self.conn.execute("ALTER TABLE topics ADD COLUMN terms TEXT")
        if "review_status" not in cols or "terms" not in cols:
            self.conn.commit()

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
        self, doc_id: str, topic_id: int, *, assigned_by: str = "hdbscan",
    ) -> None:
        """Assign a document to a topic (idempotent via INSERT OR IGNORE)."""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO topic_assignments (doc_id, topic_id, assigned_by) "
                "VALUES (?, ?, ?)",
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

    # ── Review workflow (RDR-070, nexus-lbu) ─────────────────────────────

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

    def delete_topic(self, topic_id: int) -> None:
        """Delete a topic and all its assignments."""
        with self._lock:
            self.conn.execute(
                "DELETE FROM topic_assignments WHERE topic_id = ?", (topic_id,),
            )
            self.conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
            self.conn.commit()

    def merge_topics(self, source_id: int, target_id: int) -> None:
        """Move all assignments from source to target, delete source.

        Uses INSERT OR IGNORE to handle docs assigned to both topics
        (dedup). Updates target's doc_count to the actual assignment count.
        """
        with self._lock:
            # Move assignments (ignore duplicates)
            self.conn.execute(
                "INSERT OR IGNORE INTO topic_assignments (doc_id, topic_id, assigned_by) "
                "SELECT doc_id, ?, assigned_by FROM topic_assignments WHERE topic_id = ?",
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

        result = coll.get(ids=doc_ids, include=["documents"])
        texts = result.get("documents") or []
        fetched_ids = result.get("ids") or []
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
                child_count += 1

            # Update parent doc_count to 0 (all docs moved to children)
            self.conn.execute(
                "UPDATE topics SET doc_count = 0 WHERE id = ?", (topic_id,),
            )
            self.conn.commit()

        return child_count

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

    def discover_topics(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: np.ndarray,
        texts: list[str],
        chroma_client: Any,
    ) -> int:
        """Discover topics from pre-computed embeddings via sklearn HDBSCAN.

        Clusters ``embeddings`` (N × D float32), generates c-TF-IDF labels
        from ``texts``, persists topic rows + assignments to T2, and
        upserts cluster centroids to the ``taxonomy__centroids`` ChromaDB
        collection for incremental assignment via :meth:`assign_single`.

        Returns number of topics created. Returns 0 if HDBSCAN assigns
        all documents to noise (-1).
        """
        n = len(doc_ids)
        if n < 5:
            return 0

        min_cluster_size = max(5, n // 15)
        clusterer = SklearnHDBSCAN(
            min_cluster_size=min_cluster_size,
            store_centers="centroid",
            copy=True,
        )
        labels = clusterer.fit_predict(embeddings)

        real_labels = sorted(set(int(lbl) for lbl in labels if lbl >= 0))
        if not real_labels:
            _log.warning(
                "hdbscan_all_noise",
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
        c_ids: list[str] = []
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

                c_ids.append(f"{collection_name}:{topic_id}")
                c_embs.append(clusterer.centroids_[cid].tolist())
                c_metas.append({
                    "topic_id": topic_id,
                    "label": label,
                    "collection": collection_name,
                    "doc_count": doc_count,
                })
                count += 1

            self.conn.commit()

        # Upsert centroids outside the lock
        if c_ids:
            centroid_coll.upsert(ids=c_ids, embeddings=c_embs, metadatas=c_metas)

        return count

    def assign_single(
        self,
        collection_name: str,
        embedding: np.ndarray,
        chroma_client: Any,
    ) -> int | None:
        """Return the nearest topic_id for a single embedding.

        Queries ``taxonomy__centroids`` with unconditional nearest-centroid
        assignment (RF-070-7). Returns ``None`` when the centroid collection
        does not exist or is empty.
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

        results = centroid_coll.query(
            query_embeddings=[embedding.tolist()],
            n_results=1,
            where={"collection": collection_name},
        )

        if not results["ids"] or not results["ids"][0]:
            return None

        return int(results["metadatas"][0][0]["topic_id"])

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
        """Full rebuild: delete existing topics for collection, re-discover.

        DELETE topic_assignments and topics for the collection in one
        transaction (single lock acquisition + commit), then call
        :meth:`discover_topics` AFTER the lock is released.
        """
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
        return self.discover_topics(
            collection_name, doc_ids, embeddings, texts, chroma_client,
        )
