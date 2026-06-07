# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpTaxonomyStore — thin HTTP client over the RDR-152 Java taxonomy service.

Drop-in replacement for :class:`~nexus.db.t2.catalog_taxonomy.CatalogTaxonomy`.
Activated when ``NX_STORAGE_BACKEND=service`` (or ``NX_STORAGE_BACKEND_TAXONOMY=service``).

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

CHROMA INTERACTION NOTE (RDR-152 P2.4):
    The taxonomy PG migration handles only the *relational* tables: topics,
    taxonomy_meta, topic_assignments, topic_links.

    Chroma operations remain Python-side:
    - The ``taxonomy__centroids`` ChromaDB collection (centroid vectors) is
      NOT migrated to PG in this bead.  Phase 3 (Seam B) will address the
      vector-store surface.
    - ``delete_topic`` and ``merge_topics`` return the collection name so the
      *caller* (CatalogTaxonomy or the orchestrator) can still call
      ``chroma_client.get_collection(name).delete(...)`` against the centroid
      collection locally.
    - ``assign_topic`` never touches Chroma — centroid assignment is purely
      relational (doc_id ↔ topic_id + similarity score).
    - All callers that need to clean Chroma centroid rows after a delete/merge
      must continue to do so from Python.  This store does NOT suppress the
      Chroma half; it simply does not duplicate it.

Interface parity (bead nexus-gmiaf.14, RDR-152 P2.4):
    get_topics, get_all_topics, get_topic_by_id, resolve_label,
    get_distinct_collections, get_topics_for_collection, get_unreviewed_topics,
    assign_topic, get_topic_docs, get_topic_tree, get_doc_ids_for_topic,
    get_assignments_for_docs, top_topics_for_collection, chunk_grounded_in,
    get_projection_counts_by_collection, update_topic_label, rename_topic,
    mark_topic_reviewed, delete_topic, merge_topics, get_topic_doc_ids,
    get_all_topic_doc_ids, get_topic_link_pairs, upsert_topic_links,
    compute_icf_map, detect_hubs, needs_rebalance, record_discover_count,
    purge_assignments_for_doc, close
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from environment.

    Raises:
        RuntimeError: if NX_SERVICE_PORT or NX_SERVICE_TOKEN are not set.
    """
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")

    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_TAXONOMY=service. "
            "Set it to the port where the nexus-service is listening."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(
            f"NX_SERVICE_PORT must be an integer, got: {port_str!r}"
        ) from exc

    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_TAXONOMY=service. "
            "Set it to the bearer token configured in the nexus-service."
        )

    return host, port, token


# ── HttpTaxonomyStore ──────────────────────────────────────────────────────────


class HttpTaxonomyStore:
    """CatalogTaxonomy drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool.  Reads
    ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and ``NX_SERVICE_TOKEN``
    from the environment at construction time.

    Args:
        base_url: Optional override for the service base URL
            (``http://<host>:<port>``).  When supplied, the host/port
            env-vars are ignored; the token env-var is still required.
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
    ) -> None:
        if base_url is not None:
            if _token is None:
                _token = os.environ.get("NX_SERVICE_TOKEN", "")
                if not _token:
                    raise RuntimeError(
                        "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_TAXONOMY=service."
                    )
            self._base_url = base_url.rstrip("/")
        else:
            host, port, token = _resolve_config()
            self._base_url = f"http://{host}:{port}"
            _token = token

        self._tenant = tenant
        self._headers = {
            "Authorization": f"Bearer {_token}",
            "X-Nexus-Tenant": tenant,
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        _log.info("http_taxonomy_store.init", base_url=self._base_url, tenant=tenant)

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()
        _log.debug("http_taxonomy_store.closed")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        resp = self._client.post(f"/v1/taxonomy{path}", content=json.dumps(body))
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(f"/v1/taxonomy{path}", params={
            k: str(v) for k, v in (params or {}).items() if v is not None
        })
        resp.raise_for_status()
        return resp.json()

    # ── Topics ─────────────────────────────────────────────────────────────────

    def get_topics(self, collection: str | None = None) -> list[dict[str, Any]]:
        """Return all topics, optionally filtered by collection."""
        params = {"collection": collection} if collection else {}
        return self._get("/topics", params)

    def get_all_topics(
        self,
        *,
        collection: str | None = None,
        include_children: bool = False,
    ) -> list[dict[str, Any]]:
        """Return all topics (mirrors CatalogTaxonomy.get_all_topics)."""
        return self.get_topics(collection)

    def get_topic_by_id(self, topic_id: int) -> dict[str, Any] | None:
        """Return a single topic by id, or None."""
        try:
            return self._get("/topics/by_id", {"id": topic_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def resolve_label(
        self,
        label: str,
        collection: str | None = None,
    ) -> int | None:
        """Resolve topic label to id. Returns None if not found."""
        try:
            r = self._get("/topics/resolve", {"label": label, "collection": collection})
            return r.get("id")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def get_distinct_collections(self) -> list[str]:
        """Return distinct collection names."""
        return self._get("/topics/collections")

    def get_topics_for_collection(
        self,
        collection: str,
        *,
        limit: int = 100,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return topics for a collection."""
        topics = self.get_topics(collection)
        if status:
            topics = [t for t in topics if t.get("review_status") == status]
        return topics[:limit]

    def get_unreviewed_topics(
        self,
        collection: str | None = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return topics with review_status='pending'."""
        params: dict[str, Any] = {"limit": limit}
        if collection:
            params["collection"] = collection
        return self._get("/topics/unreviewed", params)

    def update_topic_label(self, topic_id: int, new_label: str) -> None:
        """Update topic label without changing review_status."""
        self._post("/topics/update_label", {"topic_id": topic_id, "label": new_label})

    def rename_topic(self, topic_id: int, new_label: str) -> None:
        """Rename topic and mark as accepted."""
        self._post("/topics/rename", {"topic_id": topic_id, "label": new_label})

    def mark_topic_reviewed(self, topic_id: int, status: str) -> None:
        """Update review_status."""
        self._post("/topics/mark_reviewed", {"topic_id": topic_id, "status": status})

    def delete_topic(self, topic_id: int, *, chroma_client: Any = None) -> str | None:
        """Delete a topic (relational tables only).

        Returns the collection name for chroma centroid cleanup.

        CHROMA BOUNDARY: the caller is responsible for removing centroid
        vectors from the ``taxonomy__centroids`` ChromaDB collection using
        the returned collection name.  This store only handles the PG side.
        """
        try:
            r = self._post("/topics/delete", {"topic_id": topic_id})
            collection = r.get("collection")
            _log.debug(
                "http_taxonomy_store.delete_topic",
                topic_id=topic_id,
                collection=collection,
                chroma_cleanup_required=chroma_client is not None,
            )
            return collection
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def merge_topics(
        self,
        source_id: int,
        target_id: int,
        *,
        chroma_client: Any = None,
    ) -> str | None:
        """Merge source topic into target (relational tables only).

        Returns the source topic's collection name for chroma centroid cleanup.

        CHROMA BOUNDARY: same as ``delete_topic``.
        """
        try:
            r = self._post("/topics/merge", {"source_id": source_id, "target_id": target_id})
            return r.get("collection")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    # ── Assignments ────────────────────────────────────────────────────────────

    def assign_topic(
        self,
        doc_id: str,
        topic_id: int,
        assigned_by: str,
        similarity: float | None = None,
        source_collection: str | None = None,
        assigned_at: str | None = None,
    ) -> None:
        """Upsert a topic assignment.

        Projection rows use GREATEST(similarity) conflict resolution.
        Non-projection rows use INSERT OR IGNORE semantics.
        """
        self._post("/assignments/assign", {
            "doc_id": doc_id,
            "topic_id": topic_id,
            "assigned_by": assigned_by,
            "similarity": similarity,
            "source_collection": source_collection,
            "assigned_at": assigned_at,
        })

    def get_topic_doc_ids(self, topic_id: int, *, limit: int = 3) -> list[str]:
        """Return up to ``limit`` doc_ids assigned to a topic."""
        return self._get("/assignments/docs", {"topic_id": topic_id, "limit": limit})

    def get_all_topic_doc_ids(self, topic_id: int) -> list[str]:
        """Return ALL doc_ids assigned to a topic."""
        return self._get("/assignments/docs", {"topic_id": topic_id, "limit": 0})

    def get_topic_docs(
        self,
        topic_id: int,
        *,
        limit: int = 3,
        memory: Any = None,
    ) -> list[dict[str, Any]]:
        """Return topic doc_ids as dicts with title info (limited CatalogTaxonomy compat).

        The ``memory`` reference for JOIN-based title enrichment is not
        available over HTTP; this implementation returns dicts with
        ``doc_id`` set and ``title`` set to ``doc_id`` as a fallback.
        """
        doc_ids = self.get_topic_doc_ids(topic_id, limit=limit)
        return [{"doc_id": d, "title": d} for d in doc_ids]

    def get_doc_ids_for_topic(self, label: str) -> list[str]:
        """Return doc_ids labeled with a given topic label."""
        return self._get("/assignments/by_label", {"label": label})

    def get_assignments_for_docs(self, doc_ids: list[str]) -> dict[str, int]:
        """Return {doc_id: topic_id} mapping for given doc_ids."""
        result = self._post("/assignments/for_docs", {"doc_ids": doc_ids})
        return {r["doc_id"]: r["topic_id"] for r in result}

    def purge_assignments_for_doc(self, project: str, title: str) -> int:
        """Remove assignments for a deleted doc."""
        r = self._post("/assignments/purge_doc", {"project": project, "title": title})
        return r.get("removed", 0)

    # ── Topic tree ─────────────────────────────────────────────────────────────

    def get_topic_tree(
        self,
        parent_id: int | None = None,
        *,
        depth: int = -1,
    ) -> list[dict[str, Any]]:
        """Return topic tree structure (roots if parent_id is None)."""
        if parent_id is None:
            roots = self._get("/topics/root")
        else:
            roots = self._get("/topics/children", {"parent_id": parent_id})
        return roots

    # ── Links ──────────────────────────────────────────────────────────────────

    def get_topic_link_pairs(
        self,
        topic_ids: list[int],
    ) -> list[tuple[int, int, int]]:
        """Return (from_id, to_id, link_count) triples."""
        result = self._post("/links/pairs", {"topic_ids": topic_ids})
        return [(r["from_topic_id"], r["to_topic_id"], r["link_count"]) for r in result]

    def upsert_topic_links(
        self,
        pairs: list[tuple[int, int, int]],
        *,
        link_types: str = "[]",
    ) -> int:
        """Upsert topic link pairs. Returns count of pairs processed."""
        for from_id, to_id, link_count in pairs:
            self._post("/links/upsert", {
                "from_topic_id": from_id,
                "to_topic_id": to_id,
                "link_count": link_count,
                "link_types": link_types,
            })
        return len(pairs)

    # ── ICF / analytics ────────────────────────────────────────────────────────

    def compute_icf_map(
        self,
        *,
        force_recompute: bool = False,
    ) -> dict[int, float]:
        """Compute ICF map {topic_id: icf_score}. No local cache over HTTP."""
        n_effective = self._get("/icf/source_count").get("count", 1)
        if n_effective < 2:
            return {}
        rows = self._get("/icf/rows", {"n_effective": n_effective})
        import math
        return {
            r["topic_id"]: math.log2(r["icf_raw"]) if r["icf_raw"] > 0 else 0.0
            for r in rows
        }

    def detect_hubs(
        self,
        *,
        top_n: int = 10,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return top topics by projection volume for a collection."""
        if collection is None:
            return []
        return self._get("/top_topics", {"collection": collection, "top_n": top_n})

    def top_topics_for_collection(
        self,
        collection: str,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """Return top projection topics for a collection."""
        return self._get("/top_topics", {"collection": collection, "top_n": top_n})

    def chunk_grounded_in(
        self,
        doc_id: str,
        source_collection: str,
    ) -> float | None:
        """Return max projection similarity for a doc into source_collection."""
        try:
            r = self._get("/chunk_grounded", {
                "doc_id": doc_id,
                "source_collection": source_collection,
            })
            return r.get("similarity")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def get_projection_counts_by_collection(self) -> dict[str, int]:
        """Return {source_collection: count} for projection assignments."""
        result = self._get("/projection_counts")
        return {r["source_collection"]: r["count"] for r in result}

    # ── Discover bookkeeping ───────────────────────────────────────────────────

    def record_discover_count(self, collection: str, doc_count: int) -> None:
        """Record discover doc_count for rebalance check."""
        self._post("/meta/record", {
            "collection": collection,
            "doc_count": doc_count,
            "discovered_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    def needs_rebalance(self, collection: str, current_count: int) -> bool:
        """Check if collection needs rebalancing (5% growth threshold)."""
        try:
            r = self._get("/meta/last_count", {"collection": collection})
            last = r.get("count", 0)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return True
            raise
        if last == 0:
            return True
        return abs(current_count - last) / last > 0.05

    # ── ETL import (used by nx storage migrate) ────────────────────────────────

    def import_topic(
        self,
        *,
        src_id: int,
        label: str,
        parent_id: int | None,
        collection: str,
        centroid_hash: str | None,
        doc_count: int,
        created_at: str,
        review_status: str,
        terms: str | None,
    ) -> int:
        """Fidelity-preserving import for a topics row. Returns preserved id."""
        r = self._post("/import/topic", {
            "id": src_id,
            "label": label,
            "parent_id": parent_id,
            "collection": collection,
            "centroid_hash": centroid_hash,
            "doc_count": doc_count,
            "created_at": created_at,
            "review_status": review_status,
            "terms": terms,
        })
        return r["id"]

    def import_assignment(
        self,
        *,
        doc_id: str,
        topic_id: int,
        assigned_by: str,
        similarity: float | None,
        assigned_at: str | None,
        source_collection: str | None,
    ) -> None:
        """Fidelity-preserving import for a topic_assignments row."""
        self._post("/import/assignment", {
            "doc_id": doc_id,
            "topic_id": topic_id,
            "assigned_by": assigned_by,
            "similarity": similarity,
            "assigned_at": assigned_at,
            "source_collection": source_collection,
        })

    def import_topic_link(
        self,
        *,
        from_topic_id: int,
        to_topic_id: int,
        link_count: int,
        link_types: str,
    ) -> None:
        """Fidelity-preserving import for a topic_links row."""
        self._post("/import/link", {
            "from_topic_id": from_topic_id,
            "to_topic_id": to_topic_id,
            "link_count": link_count,
            "link_types": link_types,
        })

    def import_taxonomy_meta(
        self,
        *,
        collection: str,
        last_discover_doc_count: int,
        last_discover_at: str | None,
    ) -> None:
        """Fidelity-preserving import for a taxonomy_meta row."""
        self._post("/import/meta", {
            "collection": collection,
            "last_discover_doc_count": last_discover_doc_count,
            "last_discover_at": last_discover_at,
        })

    # ── Stub methods for CatalogTaxonomy compat ────────────────────────────────
    # These methods involve complex local computation (HDBSCAN clustering,
    # cross-collection discovery, etc.) that is not yet migrated to the service.
    # The seam only routes the relational query/write surface.

    def audit_collection(self, collection: str) -> dict[str, Any]:
        """Stub: not implemented over HTTP in this phase."""
        _log.warning("http_taxonomy_store.audit_collection_not_implemented")
        return {}

    def clear_icf_cache(self) -> None:
        """No-op: ICF is computed on-demand over HTTP, no local cache."""

    def get_labels_for_ids(self, topic_ids: list[int]) -> dict[int, str]:
        """Return {topic_id: label} for given ids."""
        result = {}
        for tid in topic_ids:
            topic = self.get_topic_by_id(tid)
            if topic:
                result[tid] = topic["label"]
        return result
