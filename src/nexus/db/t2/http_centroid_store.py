# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpCentroidStore — service-backed taxonomy-centroid port (RDR-156 nexus-t1hnc).

The chroma-free replacement for the ``taxonomy__centroids`` ChromaDB collection
the oracle (:class:`~nexus.db.t2.catalog_taxonomy.CatalogTaxonomy`) reached via a
``chroma_client``. Backs the centroid-ANN reads (``assign_single`` /
``compute_assignments`` / ``compute_cross_links`` / ``project_against``) and the
``discover_topics`` centroid upsert when taxonomy runs on the PG service backend.

Talks to the RDR-156 ``/v1/taxonomy/centroids/*`` endpoints (bead nexus-t1hnc.3).
Endpoint discovery is the SAME centralized resolver
(:func:`nexus.db.service_endpoint.resolve_service_endpoint`) that HttpTaxonomyStore
uses — no per-store env handling.

ERROR-TRANSLATION CONTRACT (Phase-1 gate O2):
    The oracle's ``assign_single`` returns ``None`` (and ``compute_assignments``
    skips) on: collection-absent, count==0, dim-mismatch, empty-filter. In service
    mode the table always exists, so:
    - dim-mismatch / bad request -> HTTP 400 -> :meth:`ann_query` returns ``[]``
      (so :meth:`nearest` returns ``None``), matching the oracle's best-effort
      "don't assign" on a dimension mismatch.
    - empty result -> ``[]`` / ``None`` (no centroids yet).
    - transport / 5xx errors are RAISED, NOT swallowed to ``None``. This is a
      DELIBERATE divergence from the oracle's blanket ``except Exception: return
      None``: silently treating a service outage as "no centroid" during a full
      reindex would produce an untaxonomized corpus — the silent-wrong class the
      project forbids. Callers that want best-effort parity catch explicitly.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.db.t2.catalog_taxonomy import AssignResult
from nexus.db.t2.http_taxonomy_store import DEFAULT_TENANT

_log = structlog.get_logger(__name__)

#: Matches RefreshableHttpStoreMixin's own default (kept local so the
#: ``_transport`` test-seam rebuild below does not import a private
#: constant across modules).
_DEFAULT_TIMEOUT_S = 30.0


class HttpCentroidStore(RefreshableHttpStoreMixin):
    """Service-backed centroid port mirroring the chroma centroid contract.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order.

    Args:
        base_url: Optional ``http://<host>:<port>`` override. When supplied
            without ``_token``, only the token half is re-resolved
            (host/port need not also be independently resolvable).
        tenant:   Tenant stamped on every request (default: ``DEFAULT_TENANT``).
        _token:   Optional bearer token (test seam / explicit override).
        _transport: Optional ``httpx`` transport (test seam for ``MockTransport``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(base_url, tenant, _token=_token)
        if _transport is not None:
            # Test seam (MockTransport): the mixin's __init__ already built
            # a plain (transport-less) httpx.Client; swap it for one wired
            # to the fake transport, keeping the same timeout.
            self._client.close()
            self._client = httpx.Client(timeout=_DEFAULT_TIMEOUT_S, transport=_transport)

    # ── Internal helpers ────────────────────────────────────────────────────────
    #
    # LOCAL overrides (not a straight inherit): every method in this class
    # calls self._post/self._get with a SHORT path suffix (e.g. "/upsert") —
    # the "/v1/taxonomy/centroids" prefix is store-specific routing, not
    # part of the mixin's shared contract. Every actual HTTP round-trip
    # still goes through the inherited, self-healing super()._post/_get
    # (RefreshableHttpStoreMixin._send), never self._client directly.

    def _post(self, path: str, body: dict[str, Any], *, idempotent: bool = True) -> Any:
        return super()._post(f"/v1/taxonomy/centroids{path}", body, idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return super()._get(f"/v1/taxonomy/centroids{path}", q, idempotent=idempotent)

    # ── Writes ────────────────────────────────────────────────────────────────────

    def upsert(self, records: list[dict[str, Any]]) -> None:
        """Upsert centroids. Each record: ``{collection, topic_id, embedding,
        label, doc_count}``. Embeddings route to the per-dim table by length
        service-side. No-op on empty input."""
        if not records:
            return
        self._post("/upsert", {"records": records})

    def delete_ids(self, collection: str, topic_ids: list[int]) -> int:
        """Delete centroids by topic_id within a collection. Returns rows deleted."""
        if not topic_ids:
            return 0
        r = self._post("/delete", {"collection": collection, "topic_ids": topic_ids})
        return int(r.get("deleted", 0))

    def purge(self, collection: str) -> int:
        """Remove every centroid for a collection. Returns rows deleted."""
        r = self._post("/purge", {"collection": collection})
        return int(r.get("deleted", 0))

    # ── ANN reads ─────────────────────────────────────────────────────────────────

    def ann_query(
        self,
        embedding: list[float],
        collection: str,
        *,
        cross_collection: bool = False,
        n_results: int = 1,
    ) -> list[AssignResult]:
        """Nearest centroids for one embedding, distance-ascending.

        Returns a list of :class:`AssignResult` (``topic_id``, ``similarity = 1 -
        distance``). On HTTP 400 (dimension mismatch / bad request) returns ``[]``
        — the oracle's best-effort "don't assign" on a dim mismatch. Transport /
        5xx errors propagate (see the module error-translation contract).
        """
        try:
            rows = self._post("/query", {
                "embedding": list(embedding),
                "collection": collection,
                "cross_collection": cross_collection,
                "n_results": n_results,
            })
        except httpx.HTTPStatusError as e:
            # Swallow to [] ONLY for the dimension-mismatch 400 — the oracle's
            # best-effort "don't assign" when the query vector's space does not
            # match the stored centroids (catalog_taxonomy._check_centroid_dimension).
            # Any OTHER 400 (malformed body, n_results<1, ...) is a CALLER BUG and
            # re-raises — never silently empty (fail-loud, M1/S3).
            detail = e.response.text[:300]
            if e.response.status_code == 400 and "taxonomy_centroids" in detail:
                _log.warning(
                    "centroid_dimension_mismatch",
                    collection=collection,
                    detail=detail,
                )
                return []
            raise
        return [AssignResult(topic_id=int(r["topic_id"]), similarity=float(r["similarity"]))
                for r in rows]

    def nearest(
        self,
        embedding: list[float],
        collection: str,
        *,
        cross_collection: bool = False,
    ) -> AssignResult | None:
        """The ``assign_single`` equivalent: nearest single centroid or ``None``.

        Returns ``None`` when there are no centroids (or a dim mismatch yields an
        empty result), matching :meth:`CatalogTaxonomy.assign_single`'s contract.
        """
        hits = self.ann_query(
            embedding, collection, cross_collection=cross_collection, n_results=1,
        )
        return hits[0] if hits else None

    # ── Bulk / metadata reads ─────────────────────────────────────────────────────

    def count(self, collection: str | None = None) -> int:
        """Count centroids (optionally for one collection) across all per-dim tables."""
        r = self._get("/count", {"collection": collection})
        return int(r.get("count", 0))

    def dimension(self) -> int:
        """The active centroid dimension for this tenant, or ``-1`` when empty.

        Mirrors :func:`catalog_taxonomy._check_centroid_dimension`'s role: resolves
        the deployment's single centroid space for collection-keyed ops.
        """
        r = self._get("/dimension")
        return int(r.get("dimension", -1))

    def get_by_collection(self, collection: str) -> dict[str, list[Any]]:
        """All centroids for ``collection`` in the chroma ``get()`` envelope shape
        the rebuild/project paths index into: ``{ids, embeddings, metadatas}``.

        ``ids`` are ``"{collection}:{topic_id}"`` (the oracle centroid id at
        ``catalog_taxonomy.py:_centroid_records_for``); ``metadatas`` carry
        ``{topic_id, label, collection, doc_count}``.
        """
        return self._envelope(self._get("/by_collection", {"collection": collection}))

    def get_foreign(self, collection: str) -> dict[str, list[Any]]:
        """All centroids in collections OTHER than ``collection`` (cross-collection
        projection source set), in the same envelope shape as
        :meth:`get_by_collection`.

        Serves ``compute_cross_links`` (``$ne`` directly) and ``project_against``
        (``$in`` by filtering this foreign set to the target collections).
        """
        return self._envelope(self._get("/foreign", {"collection": collection}))

    @staticmethod
    def _envelope(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
        """Adapt service centroid rows to the chroma ``get()`` envelope."""
        ids: list[str] = []
        embeddings: list[list[float]] = []
        metadatas: list[dict[str, Any]] = []
        for r in rows:
            collection = r["collection"]
            topic_id = int(r["topic_id"])
            ids.append(f"{collection}:{topic_id}")
            embeddings.append(r["embedding"])
            metadatas.append({
                "topic_id": topic_id,
                "label": r.get("label"),
                "collection": collection,
                "doc_count": r.get("doc_count"),
            })
        return {"ids": ids, "embeddings": embeddings, "metadatas": metadatas}
