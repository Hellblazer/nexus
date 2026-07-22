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

    Centroid vectors live in the service-backed centroid store (the pgvector
    ``/v1/taxonomy/centroids`` routes, RDR-156 nexus-t1hnc), reached lazily via
    ``self._centroid``:
    - ``delete_topic`` and ``merge_topics`` self-clean the affected topic's
      centroid via ``self._centroid.delete_ids(collection, [topic_id])`` after
      the relational write (nexus-cugrk). This replaces the earlier "return the
      collection name so the caller removes the centroid" contract, which leaked
      orphan centroids that kept attracting chunks (ghost assignments to deleted
      topics). The collection name is still returned for compatibility.
    - ``assign_topic`` never touches the centroid store — centroid assignment is
      purely relational (doc_id ↔ topic_id + similarity score).

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
from datetime import UTC, datetime
from typing import Any

import httpx
import numpy as np
import structlog

from nexus.db.t2.catalog_taxonomy import (
    AssignResult,
    AuditHub,
    AuditReport,
    CatalogTaxonomy,
    DEFAULT_HUB_STOPWORDS,
    HubRow,
)

_log = structlog.get_logger(__name__)

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-f2qvx.1: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpTaxonomyStore no longer bakes a
# ``self._headers`` dict or a ``httpx.Client(base_url=..., headers=...)``
# at construction time, which is what let a rotated bearer or a
# supervisor-restart port change go silently stale for the life of the
# instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


def _cosine_matrix(a: "np.ndarray", b: "np.ndarray") -> "np.ndarray":
    """Row-wise cosine-similarity matrix (a_rows × b_rows), i.e. ``1 - cosine
    distance``.

    Mirrors the normalized-dot the oracle's compute_assignments /
    compute_cross_links run over chroma results, so service-mode similarities
    match the chroma path to float precision. Zero-norm rows are guarded to 1.0
    (same as the oracle).
    """
    an = np.linalg.norm(a, axis=1, keepdims=True)
    an[an == 0] = 1.0
    bn = np.linalg.norm(b, axis=1, keepdims=True)
    bn[bn == 0] = 1.0
    return (a / an) @ (b / bn).T


# ── HttpTaxonomyStore ──────────────────────────────────────────────────────────


class HttpTaxonomyStore(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """CatalogTaxonomy drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order.

    Args:
        base_url: Optional override for the service base URL
            (``http://<host>:<port>``).  When supplied without ``_token``,
            only the token half is re-resolved (host/port need not also be
            independently resolvable).
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
        centroid_store: Optional pre-constructed centroid port (test seam);
            when omitted, lazily constructed from this store's own resolved
            base_url/tenant/token (see :attr:`_centroid`).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
        centroid_store: Any | None = None,
    ) -> None:
        super().__init__(base_url, tenant, _token=_token)
        # Centroid R/W routes through the pgvector centroid-port (nexus-t1hnc),
        # NOT chroma. Constructed lazily from the SAME resolved service config so
        # both stores share one base_url/token/tenant; injectable for tests.
        self._centroid_store = centroid_store

    @property
    def _centroid(self) -> Any:
        """The service-backed centroid port (lazy; shares this store's config).

        nexus-gcx2r (decision-surface audit, 2026-07-12): the child inherits
        the PARENT's pin state per half, not a hard pin of both. Passing
        ``base_url=self._base_url, _token=self._token`` unconditionally
        marked the child fully pinned, and the mixin's
        ``_invalidate_and_reresolve`` refuses to self-heal a fully pinned
        instance — since this property is the ONLY production construction
        site of ``HttpCentroidStore``, every centroid-backed operation lost
        self-heal entirely: any supervisor restart or token rotation turned
        the first retryable failure into a permanent
        ``RuntimeError: cannot self-heal`` for the life of the instance.
        A half the parent resolved itself (unpinned) is passed as ``None``
        so the child resolves — and later RE-resolves — that half through
        the same resolver; a half the caller deliberately pinned on the
        parent (fake-server tests) stays pinned on the child. Consequence
        (code-review, non-blocking): an unpinned half is resolved
        INDEPENDENTLY by the child at first access, not copied from the
        parent — if the credential rotated between parent construction and
        the lazy child's first centroid op, the two can transiently hold
        different (both individually valid) tokens, each self-healing on
        its own. Deliberate: copying would re-pin and re-create the dead
        self-heal this fix removes.
        """
        if self._centroid_store is None:
            from nexus.db.t2.http_centroid_store import HttpCentroidStore  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

            self._centroid_store = HttpCentroidStore(
                base_url=self._base_url if self._base_url_pinned else None,
                tenant=self._tenant,
                _token=self._token if self._token_pinned else None,
            )
        return self._centroid_store

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        super().close()
        if self._centroid_store is not None:
            self._centroid_store.close()

    # ── Internal helpers ───────────────────────────────────────────────────────
    #
    # These stay LOCAL overrides (not a straight inherit) because every method
    # in this class calls self._post/self._get with a SHORT path suffix
    # (e.g. "/topics/root") — the "/v1/taxonomy" prefix is store-specific
    # routing, not part of the mixin's shared contract. Every actual HTTP
    # round-trip still goes through the inherited, self-healing
    # super()._post/_get (RefreshableHttpStoreMixin._send), never
    # self._client directly.

    def _post(self, path: str, body: dict[str, Any], *, idempotent: bool = True) -> Any:
        return super()._post(f"/v1/taxonomy{path}", body, idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return super()._get(f"/v1/taxonomy{path}", q, idempotent=idempotent)

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

    def _delete_centroid(self, collection: str, topic_id: int) -> None:
        """Best-effort removal of a topic's centroid from the service-backed
        centroid store (nexus-cugrk).

        Leaving the orphan centroid behind keeps it attracting chunks in
        ``project_against`` / ``assign_single`` until the next full rebuild —
        persistent ghost assignments to a topic the user just deleted/merged.
        Self-cleaning here closes that leak for every caller, replacing the
        Chroma-era "caller removes the centroid" contract.

        Failures are logged, never raised: the relational delete/merge has
        already committed, and raising post-commit would surface a confusing
        error for a non-authoritative side effect. The trade-off is that a
        centroid-store outage during this window silently leaks the orphan —
        it is cleared only by a subsequent full rebuild that purges-then-
        recomputes the collection's centroids (not guaranteed to run soon), so
        the failure is logged at WARNING with a traceback to keep the leak
        detectable via log scraping.
        """
        try:
            deleted = self._centroid.delete_ids(collection, [topic_id])
            _log.debug(
                "http_taxonomy_store.centroid_cleanup",
                collection=collection, topic_id=topic_id, deleted=deleted,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort side effect
            _log.warning(
                "http_taxonomy_store.centroid_cleanup_failed",
                collection=collection, topic_id=topic_id, error=str(exc),
                exc_info=True,
            )

    def delete_topic(self, topic_id: int, *, chroma_client: Any = None) -> str | None:
        """Delete a topic, its assignments, and its centroid.

        Deletes the relational rows via the service, then removes the topic's
        centroid from the service-backed centroid store (nexus-cugrk) so it
        stops attracting chunks. Returns the collection name.

        The ``chroma_client`` parameter is retained for signature parity with
        :class:`CatalogTaxonomy` but is unused on the service path — the
        centroid lives in the pgvector centroid store, not Chroma.
        """
        try:
            r = self._post("/topics/delete", {"topic_id": topic_id})
            collection = r.get("collection")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        _log.debug("http_taxonomy_store.delete_topic", topic_id=topic_id, collection=collection)
        if collection:
            self._delete_centroid(collection, topic_id)
        return collection

    def merge_topics(
        self,
        source_id: int,
        target_id: int,
        *,
        chroma_client: Any = None,
    ) -> str | None:
        """Merge source topic into target, deleting the source and its centroid.

        Reassigns the source's docs to the target via the service, then removes
        the SOURCE topic's centroid from the service-backed centroid store
        (nexus-cugrk) — the source no longer exists, so its centroid must not
        keep attracting chunks. The target's centroid is left untouched,
        matching the local-store behaviour: it still reflects the target's
        pre-merge doc set and is only recomputed on the next full rebuild, so
        the merged target is in a stale attract-state until then. Not
        recomputing here is the accepted trade-off (cheap, consistent with the
        existing design) — an immediate recompute would need the merged
        embeddings this relational-only path does not have.

        Returns the source topic's collection name. ``chroma_client`` is unused
        on the service path (see :meth:`delete_topic`).
        """
        try:
            r = self._post("/topics/merge", {"source_id": source_id, "target_id": target_id})
            collection = r.get("collection")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if collection:
            self._delete_centroid(collection, source_id)
        return collection

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

    # ── Compute (delegate-thin) + centroid ANN (RDR-152 nexus-1di3r.7) ──────────
    #
    # Per design Approach A: the heavy compute statics (cluster / c-TF-IDF /
    # KMeans / rebuild-plan) are backend-agnostic and delegated VERBATIM to
    # CatalogTaxonomy — zero reimplementation. Only the chroma-coupled centroid
    # ANN reads are adapted to the pgvector centroid-port: assign_single (single
    # doc) uses the port's server-side nearest; compute_assignments /
    # compute_cross_links (batch) fetch the centroid set ONCE via get_by_collection
    # / get_foreign and run the cosine nearest-search in numpy LOCALLY (S2
    # decision: never loop ann_query N times, never add a Java batch endpoint).

    @staticmethod
    def compute_discovered_topics(
        collection_name: str,
        doc_ids: list[str],
        embeddings: "np.ndarray",
        texts: list[str],
    ) -> list[dict[str, Any]]:
        """Delegate verbatim to the backend-agnostic oracle static."""
        return CatalogTaxonomy.compute_discovered_topics(
            collection_name, doc_ids, embeddings, texts,
        )

    @staticmethod
    def compute_rebuild_plan(
        collection_name: str,
        doc_ids: list[str],
        embeddings: "np.ndarray",
        texts: list[str],
        *,
        old_centroids: "np.ndarray",
        old_labels: list[str],
        old_review_statuses: list[str],
        old_centroid_topic_ids: list[int],
        manual_assignments: dict[str, int],
    ) -> dict[str, Any]:
        """Delegate verbatim to the backend-agnostic oracle static."""
        return CatalogTaxonomy.compute_rebuild_plan(
            collection_name, doc_ids, embeddings, texts,
            old_centroids=old_centroids,
            old_labels=old_labels,
            old_review_statuses=old_review_statuses,
            old_centroid_topic_ids=old_centroid_topic_ids,
            manual_assignments=manual_assignments,
        )

    @staticmethod
    def compute_split(
        topic_id: int,
        doc_ids: list[str],
        texts: list[str],
        fetched_ids: list[str],
        embeddings: "np.ndarray",
        collection_name: str,
        k: int,
    ) -> dict[str, Any]:
        """Delegate verbatim to the backend-agnostic oracle static."""
        return CatalogTaxonomy.compute_split(
            topic_id, doc_ids, texts, fetched_ids, embeddings, collection_name, k,
        )

    def assign_single(
        self,
        collection_name: str,
        embedding: "np.ndarray",
        chroma_client: Any = None,
        *,
        cross_collection: bool = False,
    ) -> "AssignResult | None":
        """Nearest topic_id + raw cosine similarity for one embedding, via the
        centroid-port (mirrors CatalogTaxonomy.assign_single).

        ``chroma_client`` is accepted for signature-prefix parity but unused —
        centroid R/W routes through the pgvector port. The port's ``nearest``
        already reproduces the oracle's None-on-empty / dim-mismatch short-circuit.
        """
        emb = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        return self._centroid.nearest(
            emb, collection_name, cross_collection=cross_collection,
        )

    def compute_assignments(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: list[list[float]],
        chroma_client: Any = None,
        *,
        cross_collection: bool = False,
    ) -> list[dict[str, Any]]:
        """Batch nearest-topic assignments via the centroid-port (mirrors
        CatalogTaxonomy.compute_assignments).

        S2 batch path: fetch the centroid set ONCE (get_foreign for the
        cross-collection projection slice, else get_by_collection) and run the
        cosine nearest-search in numpy locally. Preserves the oracle's
        projection/centroid branch, raw-cosine similarity (``1 - distance``), and
        empty-on-no-op short-circuits (no centroids / dim mismatch) exactly.
        ``chroma_client`` is accepted for prefix parity but unused.
        """
        env = (
            self._centroid.get_foreign(collection_name)
            if cross_collection
            else self._centroid.get_by_collection(collection_name)
        )
        c_embs = env.get("embeddings") or []
        c_metas = env.get("metadatas") or []
        if not c_embs or not embeddings:
            return []

        cent = np.array(c_embs, dtype=np.float32)
        q = np.array(
            [e if isinstance(e, list) else (e.tolist() if hasattr(e, "tolist") else list(e))
             for e in embeddings],
            dtype=np.float32,
        )
        if q.size == 0 or q.shape[1] != cent.shape[1]:
            return []  # dimension mismatch — oracle short-circuit (SC-10)

        sim = _cosine_matrix(q, cent)  # docs × centroids; 1 - cosine_distance
        nearest_idx = sim.argmax(axis=1)

        by = "projection" if cross_collection else "centroid"
        out: list[dict[str, Any]] = []
        for i, doc_id in enumerate(doc_ids):
            j = int(nearest_idx[i])
            topic_id = int(c_metas[j]["topic_id"])
            if by == "projection":
                out.append({
                    "doc_id": doc_id,
                    "topic_id": topic_id,
                    "assigned_by": by,
                    "similarity": float(sim[i, j]),
                    "source_collection": collection_name,
                })
            else:
                out.append({
                    "doc_id": doc_id,
                    "topic_id": topic_id,
                    "assigned_by": by,
                    "similarity": None,
                    "source_collection": None,
                })
        return out

    def compute_cross_links(
        self,
        collection_name: str,
        new_centroids: list[list[float]],
        new_metas: list[dict[str, Any]],
        centroid_coll: Any = None,
    ) -> list[tuple[int, int]]:
        """Cross-collection centroid matching via the centroid-port (mirrors
        CatalogTaxonomy.compute_cross_links).

        Fetches the foreign centroid set ONCE via get_foreign and runs the SAME
        cosine threshold match as the oracle. Returns ``(new_topic_id,
        other_topic_id)`` pairs above ``_PROJECTION_THRESHOLD``. ``centroid_coll``
        accepted for prefix parity but unused.
        """
        env = self._centroid.get_foreign(collection_name)
        other_embs_raw = env.get("embeddings")
        other_metas = env.get("metadatas", [])
        if other_embs_raw is None or len(other_embs_raw) == 0:
            return []

        other_embs = np.array(other_embs_raw, dtype=np.float32)
        new_embs = np.array(new_centroids, dtype=np.float32)
        if new_embs.size == 0 or new_embs.shape[1] != other_embs.shape[1]:
            return []

        sim = _cosine_matrix(new_embs, other_embs)
        pairs: list[tuple[int, int]] = []
        for i, meta in enumerate(new_metas):
            new_tid = int(meta["topic_id"])
            for j in range(sim.shape[1]):
                if float(sim[i, j]) >= CatalogTaxonomy._PROJECTION_THRESHOLD:
                    pairs.append((new_tid, int(other_metas[j]["topic_id"])))
        return pairs

    # ── Persist (relational -> Java; centroids -> port) (nexus-1di3r.8) ─────────

    def persist_discovered_topics(
        self,
        collection_name: str,
        specs: list[dict[str, Any]],
    ) -> list[int]:
        """Persist discovered topic specs (mirrors CatalogTaxonomy.persist_discovered_topics).

        Routes to the atomic /topics/persist_discovered endpoint (existing-topics
        guard + INSERT specs + INSERT-OR-IGNORE assignments in one txn). Returns
        the generated topic_ids aligned to ``specs`` order (``[]`` when the guard
        fires or specs is empty). The per-spec ``centroid`` is ignored here — the
        orchestrator upserts centroids through the port from the returned ids.

        A UNIQUE-violation 409 is treated as a benign skip (``[]``), same as
        the guard firing: pre-fix engines (< the nexus-n2ls1 advisory-lock
        cut) map a concurrent guard-then-insert race to SQLSTATE 23505 → HTTP
        409 — the topics were persisted by the concurrent winner, so there is
        nothing to retry and nothing was lost. The skip is SQLSTATE-scoped
        (critique HIGH): the engine's typed error ladder maps EVERY class-23
        integrity violation to 409 with the sqlstate in the body, and a
        23502/23503/23514 here would be a real defect that must propagate. A
        409 body WITHOUT a readable sqlstate is treated as the race (older
        engines predating the typed ladder — the only known 409 producer on
        this endpoint there). Fixed engines never return 409 here.
        Deliberately scoped to THIS endpoint only: rebuild/split are
        REPLACE-semantics, where a conflict would mean a genuinely lost
        write — those must keep propagating.
        """
        try:
            r = self._post(
                "/topics/persist_discovered",
                {"collection": collection_name, "specs": specs},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                try:
                    sqlstate = exc.response.json().get("sqlstate")
                except Exception:  # noqa: BLE001 — body may be empty/non-JSON on older engines; absent sqlstate handled below
                    sqlstate = None
                if sqlstate in (None, "23505"):
                    _log.info(
                        "persist_discovered_conflict_benign_skip",
                        collection=collection_name,
                        sqlstate=sqlstate,
                        hint=(
                            "a concurrent discovery already persisted this "
                            "collection's topics (pre-n2ls1 engine race shape); "
                            "nothing to retry"
                        ),
                    )
                    return []
            raise
        return r.get("topic_ids", [])

    def persist_rebuild_topics(
        self,
        collection_name: str,
        plan: dict[str, Any],
    ) -> list[int]:
        """Apply a rebuild plan (mirrors CatalogTaxonomy.persist_rebuild_topics).

        Routes to the atomic /topics/persist_rebuild endpoint (REPLACE: DELETE
        old + INSERT new specs + manual_transfers in one txn — clears old rows
        even when specs is empty). Returns topic_ids aligned to ``plan["specs"]``.
        """
        r = self._post(
            "/topics/persist_rebuild",
            {
                "collection": collection_name,
                "specs": plan["specs"],
                "manual_transfers": plan.get("manual_transfers", {}),
            },
        )
        return r.get("topic_ids", [])

    def persist_assignments(self, assignments: list[dict[str, Any]]) -> int:
        """Persist pre-computed assignments (mirrors CatalogTaxonomy.persist_assignments).

        nexus-71988: one ``/assignments/assign_many`` POST per <=1000 rows —
        the per-row ``assign_topic`` loop cost ~29s per 300-chunk flush batch
        (up to ~600 sequential POSTs, 2026-07-04 attrib run). The engine
        endpoint (v0.1.24+) preserves the per-row semantics verbatim
        (projection GREATEST/CASE upsert, centroid DO NOTHING,
        trigger-maintained doc_count). A 404 (older engine) falls back to
        the legacy per-row loop. Returns the number persisted.
        """
        if not assignments:
            return 0
        _PAGE = 1000  # engine cap (MAX_BATCH parity)
        try:
            for start in range(0, len(assignments), _PAGE):
                batch = assignments[start : start + _PAGE]
                self._post("/assignments/assign_many", {"assignments": batch})
            return len(assignments)
        except Exception as exc:  # noqa: BLE001 — 404-classify only; anything else re-raises below
            status = getattr(
                getattr(exc, "response", None), "status_code", None
            ) or getattr(exc, "code", None)
            if status != 404:
                raise
            _log.info("assign_many_unavailable_fallback_per_row")
        for a in assignments:
            self.assign_topic(
                a["doc_id"],
                a["topic_id"],
                a["assigned_by"],
                a.get("similarity"),
                a.get("source_collection"),
                a.get("assigned_at"),
            )
        return len(assignments)

    def persist_cross_links(self, pairs: list[tuple[int, int]]) -> int:
        """Persist projection topic_links (mirrors CatalogTaxonomy.persist_cross_links).

        Each pair -> INSERT OR REPLACE link_count=1 link_types='["projection"]'
        via the /links/upsert endpoint (EXCLUDED overwrite, per-PK idempotent —
        the per-link loop has no cross-set atomicity need). Returns ``len(pairs)``.
        """
        if not pairs:
            return 0
        for a, b in pairs:
            self._post("/links/upsert", {
                "from_topic_id": a,
                "to_topic_id": b,
                "link_count": 1,
                "link_types": json.dumps(["projection"]),
            })
        return len(pairs)

    def read_rebuild_old_state(
        self,
        collection_name: str,
        centroid_coll: Any = None,
    ) -> dict[str, Any]:
        """Read the pre-rebuild state (mirrors CatalogTaxonomy.read_rebuild_old_state).

        COMPOSES the two halves: the T2 read via GET /rebuild/old_state and the
        centroid read via the centroid-port get_by_collection. Reconstructs the
        oracle's dict forms from the endpoint's JSON-friendly lists (the
        nexus-1di3r.1 reshape contract): old_topic_map list -> {id:(label,status)},
        manual_assignments list -> {doc_id:topic_id}. Returns the EXACT 6-key dict
        compute_rebuild_plan consumes. ``centroid_coll`` kept for prefix parity,
        unused (centroids route through the port).
        """
        t2 = self._get("/rebuild/old_state", {"collection": collection_name})
        old_topic_map: dict[int, tuple[str, str]] = {
            row["id"]: (row["label"], row["review_status"])
            for row in t2.get("old_topic_map", [])
        }
        manual_assignments: dict[str, int] = {
            row["doc_id"]: row["topic_id"] for row in t2.get("manual_assignments", [])
        }

        env = self._centroid.get_by_collection(collection_name)
        embeddings = env.get("embeddings") or []
        metadatas = env.get("metadatas") or []
        old_centroid_ids = env.get("ids") or []

        old_centroids = (
            np.array(embeddings, dtype=np.float32)
            if embeddings
            else np.empty((0, 0), dtype=np.float32)
        )
        old_labels: list[str] = []
        old_review_statuses: list[str] = []
        old_centroid_topic_ids: list[int] = []
        for m in metadatas:
            tid = m.get("topic_id", -1)
            old_centroid_topic_ids.append(tid)
            if tid in old_topic_map:
                old_labels.append(old_topic_map[tid][0])
                old_review_statuses.append(old_topic_map[tid][1])
            else:
                old_labels.append(m.get("label") or "")
                old_review_statuses.append("pending")

        return {
            "old_centroids": old_centroids,
            "old_labels": old_labels,
            "old_review_statuses": old_review_statuses,
            "old_centroid_topic_ids": old_centroid_topic_ids,
            "manual_assignments": manual_assignments,
            "old_centroid_ids": old_centroid_ids,
        }

    def purge_collection(self, collection: str) -> dict[str, int]:
        """Cascade-purge the four taxonomy tables for a collection (mirrors
        CatalogTaxonomy.purge_collection).

        Routes to the transactional /purge_collection endpoint. Returns the
        4-key count dict ``{topics, assignments, links, meta}``. The centroid
        cleanup is the caller's responsibility (centroid-port purge), matching
        the oracle's "call this after the Chroma delete" contract.
        """
        return self._post("/purge_collection", {"collection": collection})

    # ── Orchestrators (thin compose-glue) (RDR-152 nexus-1di3r.9) ──────────────
    #
    # compute_* (delegate) -> persist_* (Java) -> centroid R/W (port). The three
    # T3-free orchestrators (discover/rebuild/assign_batch) take pre-computed
    # embeddings as args and only touch the centroid-port. project_against and
    # split_topic are HYBRID: chunk reads stay on the passed chroma_client (T3 is
    # still chroma-served until RDR-155 pgvector-T3), centroid R/W routes through
    # the port. chroma_client is retained on every signature for prefix parity.

    @staticmethod
    def _centroid_records_for_port(
        collection_name: str,
        specs: list[dict[str, Any]],
        topic_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Build centroid-port upsert records from compute specs + persisted ids."""
        records: list[dict[str, Any]] = []
        for spec, tid in zip(specs, topic_ids):
            if spec.get("centroid") is None:
                continue
            records.append({
                "collection": collection_name,
                "topic_id": int(tid),
                "embedding": spec["centroid"],
                "label": spec["label"],
                "doc_count": spec["doc_count"],
            })
        return records

    def discover_topics(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: "np.ndarray",
        texts: list[str],
        chroma_client: Any = None,
    ) -> int:
        """Discover topics (mirrors CatalogTaxonomy.discover_topics): compute ->
        persist -> centroid-port upsert -> cross-links -> record count.

        Returns the number of topics created (0 if specs empty or the
        existing-topics guard fires). chroma_client unused (centroids via port).
        """
        specs = self.compute_discovered_topics(collection_name, doc_ids, embeddings, texts)
        if not specs:
            return 0
        topic_ids = self.persist_discovered_topics(collection_name, specs)
        if not topic_ids:
            return 0

        records = self._centroid_records_for_port(collection_name, specs, topic_ids)
        if records:
            self._centroid.upsert(records)

        # Cross-collection post-pass (best-effort, like the oracle). Build the
        # centroid + meta lists from ONE aligned pass so a None-centroid spec can
        # never desync their lengths (compute_cross_links indexes sim rows by the
        # meta list — a length skew would IndexError inside this try/except).
        centroid_pairs = [
            (spec["centroid"], int(tid))
            for spec, tid in zip(specs, topic_ids)
            if spec.get("centroid") is not None
        ]
        if centroid_pairs:
            try:
                new_centroids = [c for c, _ in centroid_pairs]
                new_metas = [{"topic_id": tid} for _, tid in centroid_pairs]
                self.persist_cross_links(
                    self.compute_cross_links(collection_name, new_centroids, new_metas)
                )
            except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
                _log.debug("discover_cross_links_failed", exc_info=True)

        self.record_discover_count(collection_name, len(doc_ids))
        return len(topic_ids)

    def assign_batch(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: list[list[float]],
        chroma_client: Any = None,
        *,
        cross_collection: bool = False,
    ) -> int:
        """Assign a batch to nearest topics (mirrors CatalogTaxonomy.assign_batch):
        compute_assignments -> persist_assignments. Returns the number assigned."""
        return self.persist_assignments(
            self.compute_assignments(
                collection_name, doc_ids, embeddings,
                cross_collection=cross_collection,
            )
        )

    def rebuild_taxonomy(
        self,
        collection_name: str,
        doc_ids: list[str],
        embeddings: "np.ndarray",
        texts: list[str],
        chroma_client: Any = None,
    ) -> int:
        """Full rebuild with label preservation (mirrors CatalogTaxonomy.rebuild_taxonomy):
        read old state -> delete old centroids (port) -> compute plan -> persist
        (REPLACE) -> upsert new centroids (port) -> record count.

        Returns the number of topics after rebuild. chroma_client unused.
        """
        old = self.read_rebuild_old_state(collection_name)
        old_tids = [int(t) for t in old["old_centroid_topic_ids"] if int(t) >= 0]
        if old_tids:
            self._centroid.delete_ids(collection_name, old_tids)

        plan = self.compute_rebuild_plan(
            collection_name, doc_ids, embeddings, texts,
            old_centroids=old["old_centroids"],
            old_labels=old["old_labels"],
            old_review_statuses=old["old_review_statuses"],
            old_centroid_topic_ids=old["old_centroid_topic_ids"],
            manual_assignments=old["manual_assignments"],
        )
        topic_ids = self.persist_rebuild_topics(collection_name, plan)
        if topic_ids:
            records = self._centroid_records_for_port(
                collection_name, plan["specs"], topic_ids,
            )
            if records:
                self._centroid.upsert(records)

        self.record_discover_count(collection_name, len(doc_ids))
        return len(topic_ids)

    @staticmethod
    def _svc_fetch_by_ids(t3: Any, collection: str, doc_ids: list[str]):
        """Fetch (ids, texts, embeddings) for specific doc_ids via the service.

        nexus-9pqoj. Texts come from the service store-get (`stub.get(ids=...)`),
        embeddings server-side via `t3.get_embeddings` (the collection's native
        space). Returns the aligned subset that resolved; ``embeddings`` is
        ``None`` when the service could not align vectors to the fetched ids
        (count skew → refuse rather than mis-pair).
        """
        stub = t3.get_or_create_collection(collection)
        ids: list[str] = []
        texts: list[str] = []
        _PAGE = 250
        for i in range(0, len(doc_ids), _PAGE):
            batch = doc_ids[i:i + _PAGE]
            # The service store-get path ignores `include` and always returns
            # the full {ids, documents, metadatas} envelope (VectorHandler P4a.2,
            # nexus-1k8s1); we pass include for intent only.
            res = stub.get(ids=batch, include=["documents"])
            for fid, fdoc in zip(res.get("ids") or [], res.get("documents") or []):
                if fdoc:
                    ids.append(fid)
                    texts.append(fdoc)
        if not ids:
            return [], [], None
        embs = t3.get_embeddings(collection, ids)
        if embs is None or len(embs) != len(ids):
            _log.warning(
                "taxonomy_svc_fetch_by_ids_misalign",
                collection=collection, want=len(ids),
                got=0 if embs is None else len(embs),
            )
            return ids, texts, None
        return ids, texts, np.asarray(embs, dtype=np.float32)

    @staticmethod
    def _svc_fetch_all_embeddings(t3: Any, collection: str):
        """Fetch (ids, embeddings) for ALL chunks in a collection via the service.

        nexus-9pqoj — the project source-side equivalent of
        :meth:`_svc_fetch_by_ids`. Returns ``(ids, None)`` on count skew.
        """
        try:
            n = t3.count(collection)
        except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
            return [], None
        if n == 0:
            return [], np.empty((0, 0), dtype=np.float32)
        stub = t3.get_or_create_collection(collection)
        ids: list[str] = []
        offset = 0
        _PAGE = 300
        while offset < n:
            page = stub.get(include=[], limit=_PAGE, offset=offset)
            pids = page.get("ids") or []
            if not pids:
                break
            ids.extend(pids)
            offset += len(pids)
            if len(pids) < _PAGE:
                break
        if not ids:
            return [], np.empty((0, 0), dtype=np.float32)
        embs = t3.get_embeddings(collection, ids)
        if embs is None or len(embs) != len(ids):
            _log.warning(
                "taxonomy_svc_fetch_all_misalign",
                collection=collection, want=len(ids),
                got=0 if embs is None else len(embs),
            )
            return ids, None
        return ids, np.asarray(embs, dtype=np.float32)

    def split_topic(
        self,
        topic_id: int,
        k: int,
        chroma_client: Any,
    ) -> int:
        """Split a topic into k children (mirrors CatalogTaxonomy.split_topic):
        fetch texts from T3 (via chroma_client) + re-embed -> compute_split ->
        persist_split (Java) -> centroid-port delete parent + upsert children.

        HYBRID: chunk text reads stay on chroma_client (T3); centroids via port.
        Returns the number of children created (0 on any short-circuit).
        """
        from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

        if k < 2:
            return 0
        topic = self.get_topic_by_id(topic_id)
        if topic is None:
            return 0
        doc_ids = self.get_all_topic_doc_ids(topic_id)
        if len(doc_ids) < k:
            return 0

        collection_name = topic["collection"]

        # nexus-9pqoj: service-backed source reads. When the handle is the
        # HttpVectorClient (service-mode CLI), pull the topic's STORED vectors
        # via the service — NOT a MiniLM-384 re-embed, because parent and child
        # centroids must share the collection's bge-768 / voyage space for ANN
        # assignment to work. Raw chroma handles keep the legacy re-embed path.
        from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

        if is_service_backed(chroma_client):
            fetched_ids, texts, embeddings = self._svc_fetch_by_ids(
                chroma_client, collection_name, doc_ids,
            )
            if not fetched_ids or len(texts) < k or embeddings is None:
                return 0
        else:
            try:
                coll = chroma_client.get_collection(collection_name, embedding_function=None)
            except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
                _log.warning("split_collection_not_found", collection=collection_name)
                return 0

            _PAGE = 250
            fetched_ids = []
            texts = []
            for i in range(0, len(doc_ids), _PAGE):
                batch = doc_ids[i:i + _PAGE]
                result = coll.get(ids=batch, include=["documents"])
                for fid, fdoc in zip(result.get("ids") or [], result.get("documents") or []):
                    if fdoc:
                        fetched_ids.append(fid)
                        texts.append(fdoc)
            if len(texts) < k:
                return 0

            ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
            embeddings = np.array(ef(texts), dtype=np.float32)

        split_result = self.compute_split(
            topic_id, doc_ids, texts, fetched_ids, embeddings, collection_name, k,
        )
        child_specs = split_result.get("child_specs", [])
        if not child_specs:
            return 0

        child_ids = self.persist_split(split_result)

        # Centroid port: drop the parent centroid, upsert the children.
        self._centroid.delete_ids(collection_name, [topic_id])
        records: list[dict[str, Any]] = []
        for spec, cid in zip(child_specs, child_ids):
            records.append({
                "collection": collection_name,
                "topic_id": int(cid),
                "embedding": spec["centroid"],
                "label": spec["label"],
                "doc_count": spec["doc_count"],
            })
        if records:
            self._centroid.upsert(records)
        return len(child_ids)

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
        """Project source chunks against target centroids (mirrors
        CatalogTaxonomy.project_against).

        HYBRID: source CHUNK embeddings come from T3 via chroma_client (chunks are
        not centroids); TARGET centroids come from the centroid-port. The cosine
        matmul + top-K aggregation mirror the oracle exactly (raw-cosine-storage
        invariant: stored similarity is the raw cosine; ICF only filters/ranks).
        Raises ValueError on dimension mismatch.
        """
        _empty = {
            "matched_topics": [], "novel_chunks": [],
            "total_chunks": 0, "total_centroids": 0,
        }
        # 1. Source chunk embeddings. nexus-9pqoj: via the service when the
        # handle is the HttpVectorClient (the stub's get() drops embeddings, so
        # we must use get_embeddings); raw chroma keeps the legacy include path.
        from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

        if is_service_backed(chroma_client):
            src_ids, src_embs = self._svc_fetch_all_embeddings(
                chroma_client, source_collection,
            )
            # nexus-9pqoj S1: distinguish an INCOMPLETE fetch (service could not
            # align embeddings to ids) from a legitimately empty collection.
            # Silent-zero on a fetch failure looks like 'no matches' to the user;
            # flag it so the CLI surfaces it (feedback_no_silent_fallbacks).
            if src_embs is None:
                return {**_empty, "incomplete_fetch": True}
            if not src_ids or src_embs.size == 0:
                return dict(_empty)
        else:
            try:
                src_coll = chroma_client.get_collection(source_collection, embedding_function=None)
            except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
                return dict(_empty)
            _PAGE = 300
            src_ids = []
            src_emb_pages: list[np.ndarray] = []
            offset = 0
            while True:
                page = src_coll.get(include=["embeddings"], limit=_PAGE, offset=offset)
                page_ids = page.get("ids", [])
                page_embs = page.get("embeddings")
                if not page_ids or page_embs is None:
                    break
                src_ids.extend(page_ids)
                src_emb_pages.append(np.array(page_embs, dtype=np.float32))
                if len(page_ids) < _PAGE:
                    break
                offset += _PAGE
            if not src_ids:
                return dict(_empty)
            src_embs = np.concatenate(src_emb_pages)

        # 2. Target centroids from the centroid-port ($in target_collections).
        ctr_raw: list[list[float]] = []
        ctr_metas: list[dict[str, Any]] = []
        for tc in target_collections:
            env = self._centroid.get_by_collection(tc)
            ctr_raw.extend(env.get("embeddings") or [])
            ctr_metas.extend(env.get("metadatas") or [])
        if not ctr_raw or not ctr_metas:
            return {
                "matched_topics": [], "novel_chunks": list(src_ids),
                "total_chunks": len(src_ids), "total_centroids": 0,
            }
        ctr_embs = np.array(ctr_raw, dtype=np.float32)

        # 3. Dimension check (oracle raises ValueError).
        if src_embs.shape[1] != ctr_embs.shape[1]:
            raise ValueError(
                f"Dimension mismatch: source embeddings {src_embs.shape[1]}d, "
                f"centroids {ctr_embs.shape[1]}d"
            )

        # 4-5. Cosine similarity matrix (raw) + ICF-adjusted filter matrix.
        sim = _cosine_matrix(src_embs, ctr_embs)
        if icf_map:
            icf_weights = np.array(
                [icf_map.get(int(m["topic_id"]), 1.0) for m in ctr_metas],
                dtype=np.float32,
            )
            filter_sim = sim * icf_weights
        else:
            filter_sim = sim

        # 6. Aggregate matched topics + per-chunk assignments (raw cosine stored).
        topic_stats: dict[int, dict[str, Any]] = {}
        novel_chunks: list[str] = []
        chunk_assignments: list[tuple[str, int, float]] = []
        for i, doc_id in enumerate(src_ids):
            if float(filter_sim[i].max()) < threshold:
                novel_chunks.append(doc_id)
                continue
            for idx in np.argsort(-filter_sim[i])[:top_k]:
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
        return {
            "matched_topics": matched_topics,
            "novel_chunks": novel_chunks,
            "chunk_assignments": chunk_assignments,
            "total_chunks": len(src_ids),
            "total_centroids": len(ctr_metas),
        }

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

    def import_rows_batch(self, kind: str, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): fidelity-preserving BULK import for
        one taxonomy *kind* (``topic`` | ``assignment`` | ``link`` | ``meta``).

        POSTs ``{"kind": kind, "rows": payloads}`` to ``/v1/taxonomy/import_batch``
        in ONE request — the service lands the whole batch under one tenant
        transaction (GUC set once). *rows* are the ETL transform kwargs; only the
        ``topic`` kind renames ``src_id`` → ``id`` to match the per-row import
        payload (the other three already use matching keys). Collapses the
        topic_assignments leg from N round-trips to ceil(N/batch) — the 190k-row
        dogfood fix. Empty list is a no-op; returns the number of rows imported.
        """
        if not rows:
            return 0
        if kind == "topic":
            payloads = [
                {**{k: v for k, v in r.items() if k != "src_id"}, "id": r["src_id"]}
                for r in rows
            ]
        else:
            payloads = rows
        resp = self._post("/import_batch", {"kind": kind, "rows": payloads})
        return int(resp.get("imported", 0)) if isinstance(resp, dict) else 0

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
        from nexus.corpus import default_projection_threshold  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
