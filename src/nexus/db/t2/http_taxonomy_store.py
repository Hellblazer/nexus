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
import math
import os
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from nexus.db.t2.catalog_taxonomy import (
    AuditHub,
    AuditReport,
    DEFAULT_HUB_STOPWORDS,
    HubRow,
)

_log = structlog.get_logger(__name__)

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
from nexus.db.service_endpoint import resolve_service_config as _resolve_config


def _not_on_service(method: str) -> "Any":
    """Fail loud: this taxonomy method has no parity-correct service-backend
    implementation yet (RDR-152 nexus-fjwxh / nexus-1di3r).

    The HttpTaxonomyStore is an incomplete port of CatalogTaxonomy: the
    BERTopic/HDBSCAN compute pipeline and several raw-cursor read methods
    (topic-tree assembly, exclude-id merge-target listing, link upserts) are
    not yet exposed by the Java service. Rather than crash with a TypeError on
    a drifted signature (or, worse, return silently-wrong results), the method
    raises a clear, actionable error. Run taxonomy in local mode meanwhile.
    """
    raise NotImplementedError(
        f"taxonomy.{method} is not available on the service storage backend yet "
        f"(tracked in nexus-1di3r). Run taxonomy commands in local mode: set "
        f"NX_STORAGE_BACKEND_TAXONOMY=sqlite (the migrated data is still in the "
        f"local SQLite store)."
    )


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

    def get_topics(
        self,
        *,
        parent_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return topics filtered by parent (mirrors CatalogTaxonomy.get_topics).

        - ``parent_id=None`` (default): root topics (parent_id IS NULL), ordered
          by doc_count DESC.
        - ``parent_id=<int>``: children of that topic.

        RDR-152 nexus-1di3r.5: reconciled to the oracle's parent_id-keyed
        signature (was ``collection``) so the param-prefix tripwire goes strict.
        Collection-scoped reads use :meth:`get_all_topics` /
        :meth:`get_topics_for_collection`, which already hit ``/topics`` directly.
        """
        if parent_id is None:
            return self._get("/topics/root")
        return self._get("/topics/children", {"parent_id": parent_id})

    def get_all_topics(
        self,
        *,
        collection: str | None = None,
        include_children: bool = False,
    ) -> list[dict[str, Any]]:
        """Return all topics (mirrors CatalogTaxonomy.get_all_topics)."""
        # Hit /topics directly rather than via get_topics() — the latter's
        # signature differs from the oracle (collection vs parent_id) and is
        # excluded from the parity tripwire; this keeps get_all_topics, the
        # method the working read commands actually use, decoupled from it.
        params = {"collection": collection} if collection else {}
        return self._get("/topics", params)

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
        exclude_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all topics (root + children) for a collection, ordered by
        doc_count DESC (mirrors CatalogTaxonomy.get_topics_for_collection).

        Backed by GET /topics?collection= (getAllTopics) with ``exclude_id``
        applied client-side — a single-row predicate on an already-fetched list,
        so no dedicated Java route is needed (RDR-152 nexus-1di3r.4).
        """
        rows = self._get("/topics", {"collection": collection})
        if exclude_id is not None:
            rows = [r for r in rows if r.get("id") != exclude_id]
        return rows

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
        collection: str = "",
        *,
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        """Collection-scoped, depth-bounded topic tree (mirrors
        CatalogTaxonomy.get_topic_tree).

        Each node: ``{id, label, collection, doc_count, children:[...]}``. Roots
        are the parent_id-IS-NULL topics (filtered to ``collection`` when given,
        matching the oracle's root-only collection filter); children are fetched
        per node down to ``max_depth``. Client-side recursion over the existing
        GET /topics/root + GET /topics/children routes (RDR-152 nexus-1di3r.3):
        round-trips are bounded by node count within ``max_depth`` — acceptable
        for this infrequent operator read — and avoids a new server-side tree
        route. Like the oracle, the returned tree is a multi-snapshot view (each
        fetch is an independent read), not a single-transaction snapshot.
        """
        roots = self._get("/topics/root")
        if collection:
            roots = [r for r in roots if r.get("collection") == collection]

        def _build(row: dict[str, Any], depth: int) -> dict[str, Any]:
            node: dict[str, Any] = {
                "id": row["id"],
                "label": row["label"],
                "collection": row["collection"],
                "doc_count": row["doc_count"],
            }
            if depth < max_depth:
                children = self._get("/topics/children", {"parent_id": row["id"]})
                node["children"] = [_build(c, depth + 1) for c in children]
            else:
                node["children"] = []
            return node

        return [_build(r, 0) for r in roots]

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
        links: list[dict[str, Any]],
    ) -> int:
        """Upsert inter-topic link pairs (mirrors CatalogTaxonomy.upsert_topic_links).

        Each dict carries from_topic_id, to_topic_id, link_count, link_types.
        ``link_types`` is JSON-serialized (list -> string) before sending, exactly
        matching the oracle's ``json.dumps(link["link_types"])``; the Java
        upsertTopicLink stores it verbatim as a string. The per-link POST loop is
        parity-correct: INSERT OR REPLACE is per-PK idempotent with no cross-set
        atomicity need, so projection links written by ``_discover_cross_links``
        survive (only matching PK pairs are overwritten). Returns the number of
        links upserted (RDR-152 nexus-1di3r.4).
        """
        if not links:
            return 0
        for link in links:
            self._post("/links/upsert", {
                "from_topic_id": link["from_topic_id"],
                "to_topic_id": link["to_topic_id"],
                "link_count": link["link_count"],
                "link_types": json.dumps(link["link_types"]),
            })
        return len(links)

    # ── ICF / analytics ────────────────────────────────────────────────────────

    def compute_icf_map(
        self,
        *,
        use_cache: bool = False,
        force_recompute: bool = False,
    ) -> dict[int, float]:
        """Compute ICF map {topic_id: icf_score} via atomic /icf/map endpoint.

        No local cache over HTTP (race-free single round-trip replaces
        the 2-call n_effective + rows pattern from the original ICF map).
        """
        r = self._get("/icf/map")
        n_effective: int = r.get("n_effective", 0)
        if n_effective < 2:
            return {}
        rows: list[dict[str, Any]] = r.get("rows", [])
        result: dict[int, float] = {}
        for row in rows:
            df = int(row.get("df", 0))
            if df > 0:
                icf = math.log2(n_effective / df)
                result[int(row["topic_id"])] = icf
        return result

    def detect_hubs(
        self,
        *,
        min_collections: int = 2,
        max_icf: float | None = None,
        stopwords: tuple[str, ...] = DEFAULT_HUB_STOPWORDS,
        warn_stale: bool = False,
    ) -> list[HubRow]:
        """Return candidate hub topics, sorted by chunks * (1 - ICF) desc.

        Delegates DF/chunk aggregation to the service (/hubs); computes
        ICF, stopword matching, and score Python-side for exact parity
        with CatalogTaxonomy.detect_hubs.
        """
        rows: list[dict[str, Any]] = self._get("/hubs", {"min_collections": min_collections})
        icf_map = self.compute_icf_map()
        lowered_stopwords = tuple(s.lower() for s in stopwords)

        hubs: list[HubRow] = []
        for r in rows:
            topic_id = int(r["topic_id"])
            icf_value = float(icf_map.get(topic_id, 1.0))
            if max_icf is not None and icf_value > max_icf:
                continue

            label = r.get("label") or ""
            lower_label = label.lower()
            matched = tuple(s for s in lowered_stopwords if s in lower_label)

            sources = tuple(dict.fromkeys(r.get("source_collections") or []))
            total = int(r.get("total_chunks", 0))
            score = float(total) * (1.0 - icf_value)

            last_at = r.get("last_assigned_at")
            if last_at:
                last_at = str(last_at)

            hubs.append(HubRow(
                topic_id=topic_id,
                label=label,
                collection=r.get("collection") or "",
                distinct_source_collections=int(r.get("df", 0)),
                total_chunks=total,
                icf=icf_value,
                score=score,
                matched_stopwords=matched,
                source_collections=sources,
                last_assigned_at=last_at,
                max_last_discover_at=None,   # warn_stale not implemented over HTTP
                never_discovered_count=0,
                is_stale=False,
            ))

        hubs.sort(key=lambda h: h.score, reverse=True)
        return hubs

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
        *,
        threshold: float = 0.0,
    ) -> float | None:
        """Return max projection similarity for a doc into source_collection.

        ``threshold`` mirrors CatalogTaxonomy's signature; like the oracle it is
        accepted-for-future-use (the caller applies the threshold to the
        returned raw max — see nexus.doc.citations.extensions_report).
        """
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
    ) -> bool:
        """Fidelity-preserving import for a topic_assignments row.

        Always returns ``True``. doc_id is a chunk chash with no catalog FK
        (``fk_ta_catalog_doc`` was never registered — nexus-sa14p), so there is no
        catalog-existence guard and nothing to skip. The ``bool`` return is retained
        for caller-API stability with the generic ``_migrate_table`` loop.
        """
        r = self._post("/import/assignment", {
            "doc_id": doc_id,
            "topic_id": topic_id,
            "assigned_by": assigned_by,
            "similarity": similarity,
            "assigned_at": assigned_at,
            "source_collection": source_collection,
        })
        # Older services returned only {"ok": true}; treat missing "applied" as True.
        return bool(r.get("applied", True)) if isinstance(r, dict) else True

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

    def audit_collection(
        self,
        collection: str,
        *,
        threshold: float | None = None,
        top_n: int = 5,
        stopwords: tuple[str, ...] = DEFAULT_HUB_STOPWORDS,
    ) -> AuditReport:
        """Summarise projection quality for *collection*.

        Delegates raw similarity values and hub rows to the service (/audit);
        computes quantiles and stopword matching Python-side for exact parity
        with CatalogTaxonomy.audit_collection.
        """
        from nexus.corpus import default_projection_threshold

        resolved_threshold = (
            threshold if threshold is not None
            else default_projection_threshold(collection)
        )

        r = self._get("/audit", {"collection": collection, "top_n": top_n})
        sims: list[float] = [float(v) for v in r.get("similarities", [])]
        hub_rows_raw: list[dict[str, Any]] = r.get("hub_rows", [])

        icf_map = self.compute_icf_map()
        lowered_stopwords = tuple(s.lower() for s in stopwords)

        total = len(sims)
        if total:
            def _quantile(q: float) -> float:
                idx = min(total - 1, max(0, int(round(q * (total - 1)))))
                return sims[idx]
            p10: float | None = _quantile(0.10)
            p50: float | None = _quantile(0.50)
            p90: float | None = _quantile(0.90)
        else:
            p10 = p50 = p90 = None

        below_threshold_count = sum(1 for s in sims if s < resolved_threshold)

        top_hubs: list[AuditHub] = []
        for h in hub_rows_raw:
            topic_id = int(h["topic_id"])
            label = h.get("label") or ""
            lower_label = label.lower()
            matched = tuple(s for s in lowered_stopwords if s in lower_label)
            top_hubs.append(AuditHub(
                topic_id=topic_id,
                label=label,
                chunk_count=int(h.get("chunk_count", 0)),
                icf=float(icf_map.get(topic_id, 1.0)),
                matched_stopwords=matched,
            ))

        pattern_pollution = [h for h in top_hubs if h.matched_stopwords]

        return AuditReport(
            collection=collection,
            total_assignments=total,
            p10=p10,
            p50=p50,
            p90=p90,
            below_threshold_count=below_threshold_count,
            threshold=resolved_threshold,
            top_receiving_hubs=top_hubs,
            pattern_pollution=pattern_pollution,
        )

    def clear_icf_cache(self) -> None:
        """No-op: ICF is computed on-demand over HTTP, no local cache."""

    def generate_cooccurrence_links(self) -> int:
        """Generate topic_links from cross-collection projection co-occurrence.

        Delegates to the service (/links/generate_cooccurrence).
        Returns count of links generated.
        """
        r = self._post("/links/generate_cooccurrence", {})
        return int(r.get("count", 0))

    def refresh_projection_links(self) -> int:
        """Rebuild projection entries in topic_links from per-chunk assignments.

        Delegates to the service (/links/refresh_projection).
        Returns the number of topic-pair rows written/updated.
        """
        r = self._post("/links/refresh_projection", {})
        return int(r.get("count", 0))

    def persist_split(
        self,
        split_result: dict[str, Any],
    ) -> list[int]:
        """Persist the split: DELETE parent assignments, INSERT children.

        Delegates to the service (/topics/persist_split).
        Returns the list of new child topic_id values.
        """
        r = self._post("/topics/persist_split", {
            "topic_id": split_result["topic_id"],
            "collection_name": split_result["collection_name"],
            "child_specs": split_result.get("child_specs", []),
        })
        return [int(i) for i in r.get("child_ids", [])]

    def rename_collection(self, old: str, new: str) -> dict[str, int]:
        """Re-point every taxonomy row from old -> new collection name.

        Delegates to the service (/rename_collection).
        Returns count dict {topics, assignments, meta}.
        """
        r = self._post("/rename_collection", {"old": old, "new": new})
        return {
            "topics": int(r.get("topics", 0)),
            "assignments": int(r.get("assignments", 0)),
            "meta": int(r.get("meta", 0)),
        }

    def get_labels_for_ids(self, topic_ids: list[int]) -> dict[int, str]:
        """Return {topic_id: label} for given ids."""
        result = {}
        for tid in topic_ids:
            topic = self.get_topic_by_id(tid)
            if topic:
                result[tid] = topic["label"]
        return result
