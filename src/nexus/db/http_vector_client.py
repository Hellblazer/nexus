# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-152 bead nexus-gmiaf.20 — Seam B HTTP vector client.

Thin Python bridge that routes T3 vector operations (search, query,
upsert-chunks, store_put, store_get, store_list, store_delete) through
the Java nexus-service HTTP endpoints rather than hitting a vector
store / Voyage AI directly from Python.

Since the RDR-155 P4a.2 serving cutover (bead nexus-1k8s1) this is THE
production T3 handle: ``nexus.db.make_t3()`` returns the
:class:`HttpVectorClient` singleton whenever no test ``_client`` is
injected, in both local and cloud mode — the service stores vectors in
pgvector and embeds server-side. ``NX_STORAGE_BACKEND_VECTORS=service``
survives only as the indexer-side opt-in that skips Python-side
embedding (see :func:`is_vector_service_mode`).

Endpoint discovery (nexus-pebfx.1): ``{url, token}`` resolve from the
supervisor's ServiceRegistry lease (``storage_service_addr.<uid>``) by
default, with ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` env as per-half
overrides and a single re-resolve retry on 401/connection-refused so
clients ride through supervisor auto-restarts (the port churns on every
restart). No hardcoded fallback URL — unresolvable fails loud.

Chunking stays in Python; embed+write live in the JVM (Seam B contract —
CHUNKING STAYS PYTHON per the bead relay).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

#: Env var for the vector backend flag.
_VECTORS_BACKEND_ENV = "NX_STORAGE_BACKEND_VECTORS"


# ── Endpoint resolution (nexus-pebfx.1) ──────────────────────────────────────
#
# The supervisor (``nx daemon service start``) publishes ``{host, port,
# token}`` to the ServiceRegistry lease (``storage_service_addr.<uid>``)
# after a healthy ``/health`` — and allocates a NEW free port on every
# (re)start. Resolution order:
#
#   1. ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` env — each half overrides
#      independently (operator/test override; read fresh on every call).
#   2. The ServiceRegistry lease (cached; invalidated on 401 / connection
#      refused so clients ride through supervisor auto-restarts).
#   3. FAIL LOUD. The legacy hardcoded localhost default is retired — a
#      silent wrong-port fallback is a correctness hazard.

_endpoint_lock = threading.Lock()
#: Cached (base_url, token) from the LEASE only — env halves are read fresh.
#: Module-global: shared by every HttpVectorClient instance and thread in the
#: process (the client itself is a process-wide singleton). Populated only on
#: a SUCCESSFUL discovery — a missing lease is never cached, so a client
#: started before the supervisor picks the lease up as soon as it appears.
_lease_cache: tuple[str, str | None] | None = None


def _discover_lease() -> tuple[str | None, str | None]:
    """(url, token) from the supervisor's lease, or (None, None)."""
    try:
        from nexus.config import nexus_config_dir
        from nexus.daemon.service_registry import ServiceRegistry

        registry = ServiceRegistry(dir=nexus_config_dir(), tier="storage_service")
        lease = registry.discover(str(os.getuid()))
        if lease is not None:
            ep = lease.endpoint
            host = str(ep.get("host", "127.0.0.1"))
            port = int(ep.get("port", 0))
            token = str(ep.get("token", "")) or None
            if port > 0:
                return f"http://{host}:{port}", token
    except Exception as exc:  # discovery is best-effort; absence fails loud below
        _log.debug("vector_endpoint_lease_discover_failed", error=str(exc))
    return None, None


def _resolve_endpoint() -> tuple[str, str]:
    """Return ``(base_url, token)`` per the resolution order above."""
    global _lease_cache
    env_url = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/") or None
    env_token = os.environ.get("NX_SERVICE_TOKEN", "").strip() or None
    url, token = env_url, env_token
    if url is None or token is None:
        with _endpoint_lock:
            if _lease_cache is None:
                discovered = _discover_lease()
                if discovered[0] is not None:
                    # Cache ONLY on success: a (None, None) miss must not
                    # stick, or a client started before the supervisor would
                    # never discover it (dual-review S1).
                    _lease_cache = discovered  # type: ignore[assignment]
            lease_url, lease_token = _lease_cache or (None, None)
        url = url or lease_url
        token = token or lease_token
        if env_url is not None and token is lease_token and token is not None:
            _log.debug(
                "vector_endpoint_mixed_source", url_source="env", token_source="lease"
            )
        elif env_token is not None and url is lease_url and url is not None:
            _log.debug(
                "vector_endpoint_mixed_source", url_source="lease", token_source="env"
            )
    if url is None or token is None:
        raise RuntimeError(
            "nexus-service endpoint is not resolvable: T3 vector serving "
            "routes through the nexus-service HTTP API (RDR-155 Phase 4a — "
            "the direct Chroma serving paths are retired). Either start the "
            "supervisor with 'nx daemon service start' (publishes the "
            "endpoint lease this client auto-discovers), or export "
            "NX_SERVICE_URL / NX_SERVICE_TOKEN explicitly."
        )
    return url, token


def _invalidate_endpoint() -> None:
    """Drop the cached lease so the next call re-discovers (port churn)."""
    global _lease_cache
    with _endpoint_lock:
        _lease_cache = None


def _is_retryable_endpoint_error(exc: Exception) -> bool:
    """The three auto-restart signatures (dual-review S2 added RST):

    - 401: token rotated + republished with the lease.
    - connection refused: supervisor restarted; old port is dead.
    - connection reset (incl. ``http.client.RemoteDisconnected``): the
      supervisor SIGTERMs the JVM process group on restart, so a request
      IN FLIGHT at restart time gets a TCP RST, not a refusal. Every
      operation this client issues is idempotent (upsert on
      (tenant, collection, chash) ON CONFLICT; deletes; reads), so a
      single retry after a mid-flight reset is safe.
    """
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 401
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        return isinstance(reason, (ConnectionRefusedError, ConnectionResetError))
    return isinstance(exc, (ConnectionRefusedError, ConnectionResetError))


# ── HTTP transport ────────────────────────────────────────────────────────────


def _request_once(
    method: str, path: str, *, tenant: str, timeout: int, body: dict | None
) -> Any:
    """One HTTP round-trip against the currently-resolved endpoint.

    Raises the raw ``urllib.error`` exceptions — the retry wrapper below
    classifies them; the public ``_post``/``_get`` wrap HTTP errors into
    :class:`VectorServiceError`.
    """
    import urllib.request

    base_url, token = _resolve_endpoint()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Nexus-Tenant": tenant,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _request(
    method: str, path: str, *, tenant: str, timeout: int, body: dict | None
) -> Any:
    """Round-trip with ONE re-resolve retry on the auto-restart signatures.

    The supervisor allocates a new port (and republishes the lease, token
    included) on every restart; a 401 or connection-refused against the
    cached endpoint therefore means "re-read the lease and try once more"
    (nexus-pebfx.1), not "give up". A second failure surfaces normally —
    no retry loops.
    """
    import urllib.error

    try:
        return _request_once(method, path, tenant=tenant, timeout=timeout, body=body)
    # Narrow catch (dual-review H1): only the transport/auth error families
    # participate in retry classification. RuntimeError from an unresolvable
    # endpoint propagates untouched — fail-loud must never become a retry.
    except (urllib.error.URLError, ConnectionRefusedError, ConnectionResetError) as exc:
        if not _is_retryable_endpoint_error(exc):
            raise
        _log.info(
            "vector_endpoint_reresolve",
            path=path,
            reason=type(exc).__name__,
        )
        _invalidate_endpoint()
        return _request_once(method, path, tenant=tenant, timeout=timeout, body=body)


def _post(path: str, body: dict, *, tenant: str = "default", timeout: int = 120) -> Any:
    """POST JSON to the service endpoint, return parsed response body.

    ``timeout`` defaults to 120s for read/search/delete paths. The upsert-chunks
    call site passes 600s: a 300-chunk CCE (voyage-context-3) upsert batch
    routinely exceeds 120s server-side (embed is synchronous in the request);
    the RDR-155 production migration false-timed-out on exactly this until
    raised (bead nexus-rvfwj, 2026-06-10 — docs__1-16 + docs__1-1 evidence).
    Per dual-review S2 the raise is deliberately NOT global — a slow search
    should still fail fast.
    """
    import urllib.error

    try:
        return _request("POST", path, tenant=tenant, timeout=timeout, body=body)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:
            err = {"error": body_bytes.decode(errors="replace")}
        raise VectorServiceError(
            f"POST {path} → HTTP {e.code}: {err.get('error', err)}"
        ) from e


def _get(path: str, *, tenant: str = "default") -> Any:
    """GET from the service endpoint, return parsed response body."""
    import urllib.error

    try:
        return _request("GET", path, tenant=tenant, timeout=30, body=None)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:
            err = {"error": body_bytes.decode(errors="replace")}
        raise VectorServiceError(
            f"GET {path} → HTTP {e.code}: {err.get('error', err)}"
        ) from e


class VectorServiceError(RuntimeError):
    """Raised when the vector service returns an error."""


# ── Collection-handle stub ────────────────────────────────────────────────────


class _ServiceCollectionStub:
    """Minimal Chroma-collection-like handle for doc_indexer staleness + prune.

    doc_indexer._index_document uses the collection handle for:
      - Incremental staleness check: ``col.get(where=..., include=[...], limit=N)``
      - Stale-chunk prune: ``col.delete(ids=[...])``

    Both are forwarded to the service's HTTP API so the Python indexer
    stays consistent with the service's Chroma view.

    RDR-152 Seam B (nexus-gmiaf.22): this stub is the minimal surface
    required to satisfy doc_indexer's incremental-sync protocol without
    adding a full Chroma collection client to the service mode.
    """

    def __init__(self, name: str, tenant: str = "default") -> None:
        self._name = name
        self._tenant = tenant

    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict:
        """Query chunks from the service. Returns Chroma-style result dict.

        RDR-152 nexus-enehl: added ``ids`` parameter to support the
        frecency manifest-based lookup path (``col.get(ids=natural_ids,
        include=["metadatas"])``). When ``ids`` is provided the request is
        routed to ``/v1/vectors/store-get``; when ``where`` is provided it
        is routed to ``/v1/vectors/get`` (staleness-check path).
        """
        try:
            if ids is not None:
                # Manifest-based lookup: fetch specific chunk IDs
                body: dict[str, Any] = {
                    "collection": self._name,
                    "ids": ids,
                    "limit": limit,
                    "offset": offset,
                }
                result = _post("/v1/vectors/store-get", body, tenant=self._tenant)
            else:
                # Where-filter lookup (incremental-sync staleness check)
                body = {
                    "collection": self._name,
                    "limit": limit,
                    "offset": offset,
                }
                if where:
                    body["where"] = where
                if include:
                    body["include"] = include
                result = _post("/v1/vectors/get", body, tenant=self._tenant)
            # Normalise to Chroma shape: {ids, documents, metadatas}
            return {
                "ids":       result.get("ids", []),
                "documents": result.get("documents", []),
                "metadatas": result.get("metadatas", []),
            }
        except VectorServiceError as exc:
            _log.warning(
                "service_collection_get_failed",
                collection=self._name,
                error=str(exc),
            )
            return {"ids": [], "documents": [], "metadatas": []}

    def delete(self, ids: list[str]) -> None:
        """Delete chunks by ID from the service."""
        if not ids:
            return
        try:
            _post(
                "/v1/vectors/store-delete",
                {"collection": self._name, "ids": ids},
                tenant=self._tenant,
            )
        except VectorServiceError as exc:
            _log.warning(
                "service_collection_delete_failed",
                collection=self._name,
                count=len(ids),
                error=str(exc),
            )


# ── HttpVectorClient ─────────────────────────────────────────────────────────


class HttpVectorClient:
    """Drop-in subset of ``T3Database`` that routes to the Java service.

    Implements only the methods exercised by the MCP tools and the
    doc_indexer upsert path:

    - :meth:`upsert_chunks` / :meth:`upsert_chunks_with_embeddings`
    - :meth:`search`
    - :meth:`put`
    - :meth:`get_by_id`
    - :meth:`delete_by_id`
    - :meth:`list_collections`

    Methods NOT implemented here (not needed for Seam B or stubbed
    as no-ops) will raise ``NotImplementedError`` or return safe defaults.
    Taxonomy hooks and the ``_client`` attribute are also excluded — the
    Python code that uses them still routes through T3Database (flag unset).

    Thread-safe: all state is in the HTTP request payload.
    """

    # Exposed so mcp_infra.get_collection_names() and taxonomy hooks can
    # skip the expensive list call. Set to None to force a real fetch.
    # Tests may patch this.
    _tenant: str

    def __init__(self, *, tenant: str = "default") -> None:
        self._tenant = tenant

    # ── Context manager (no-op: stateless HTTP, parity with T3Database) ──────

    def __enter__(self) -> "HttpVectorClient":
        return self

    def __exit__(self, *_: object) -> None:
        pass  # No persistent connection to close.

    # NOTE — no ``_client`` attribute, deliberately (pinned by
    # tests/db/test_http_vector_client.py): chroma-client-coupled features
    # (taxonomy-via-chroma, catalog span/link embedding probes, raw collection
    # surgery) retire with the Chroma serving paths (RDR-155 P4a.2,
    # nexus-1k8s1). Accessing ``._client`` raises AttributeError — callers
    # guard with :func:`is_service_backed`; pg-side equivalents are tracked
    # follow-ons (taxonomy: nexus-gmiaf.21+).

    # ── Seam B write path ────────────────────────────────────────────────────

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
        *,
        embeddings: list[list[float]] | None = None,
    ) -> None:
        """Embed + quota-check + write via the Java service.

        CHUNKING STAYS PYTHON — this method is called with pre-chunked text.
        Embeddings are computed server-side; any ``embeddings`` argument is
        ignored (Seam B contract).
        """
        if not ids:
            return
        body: dict[str, Any] = {
            "collection": collection,
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas or [{}] * len(ids),
        }
        _post("/v1/vectors/upsert-chunks", body, tenant=self._tenant, timeout=600)
        _log.debug(
            "http_vector_upsert_chunks",
            collection=collection,
            count=len(ids),
        )

    def upsert_chunks_with_embeddings(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict] | None = None,
    ) -> None:
        """Server-side embed path: forward chunk text, ignore caller's embeddings.

        The Java service embeds server-side; the Python-side embeddings are
        discarded (Seam B: embed moves to JVM). This method signature matches
        ``T3Database.upsert_chunks_with_embeddings`` so it works transparently
        as a drop-in.
        """
        self.upsert_chunks(
            collection, ids, documents, metadatas=metadatas
        )

    def put(
        self,
        collection: str,
        doc_id: str,
        content: str,
        metadata: dict | None = None,
        *,
        embedding: list[float] | None = None,
    ) -> str:
        """Single-chunk put (MCP store_put path)."""
        body: dict[str, Any] = {
            "collection": collection,
            "doc_id": doc_id,
            "content": content,
            "metadata": metadata or {},
        }
        result = _post("/v1/vectors/store-put", body, tenant=self._tenant)
        return result.get("id", doc_id)

    # ── Read path ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        collections: list[str],
        n_results: int = 10,
        *,
        where: dict | None = None,
        cluster_by: str = "",
        threshold: float | None = None,
        structured: bool = False,
    ) -> list[dict] | dict:
        """Semantic search via the Java service.

        The service embeds the query server-side and returns ranked results.
        Returns the same list-of-dicts shape as ``T3Database.search()``
        when ``structured=False``, or a ``{ids, tumblers, distances, collections}``
        dict when ``structured=True``.
        """
        body: dict[str, Any] = {
            "query": query,
            "collections": collections,
            "n_results": n_results,
        }
        if where:
            body["where"] = where

        results = _post("/v1/vectors/search", body, tenant=self._tenant)
        # results is a list of {id, content, distance, collection, ...}

        if structured:
            # Return the plan-runner compatible structured form
            return {
                "ids":         [r.get("id", "")         for r in results],
                "tumblers":    [r.get("tumbler", "")    for r in results],
                "distances":   [r.get("distance", 0.0)  for r in results],
                "collections": [r.get("collection", "") for r in results],
            }
        return results

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        """Fetch a single chunk by ID."""
        try:
            result = _post(
                "/v1/vectors/store-get",
                {"collection": collection, "ids": [doc_id]},
                tenant=self._tenant,
            )
        except VectorServiceError:
            return None

        ids = result.get("ids") or []
        if not ids:
            return None
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        return {
            "id": ids[0],
            "document": docs[0] if docs else "",
            "metadata": metas[0] if metas else {},
        }

    def delete_by_id(self, collection: str, doc_id: str) -> bool:
        """Delete a chunk by ID. Returns True if the chunk existed."""
        try:
            result = _post(
                "/v1/vectors/store-delete",
                {"collection": collection, "ids": [doc_id]},
                tenant=self._tenant,
            )
            return result.get("deleted", 0) > 0
        except VectorServiceError:
            return False

    def list_collections(self) -> list[dict]:
        """List the tenant's vector collections via the service."""
        try:
            result = _get("/v1/vectors/collections", tenant=self._tenant)
            return result if isinstance(result, list) else []
        except VectorServiceError as e:
            _log.warning("http_vector_list_collections_failed", error=str(e))
            return []

    def collection_exists(self, name: str) -> bool:
        """True if *name* holds at least one chunk (no create side-effect).

        T3Database parity (RDR-155 P4a.2): on the pgvector path a collection
        is a column value, so existence == "has rows for this tenant".
        """
        return any(c.get("name") == name for c in self.list_collections())

    def count(self, collection: str) -> int:
        """Number of chunks in *collection* visible to this tenant."""
        from urllib.parse import quote  # noqa: PLC0415

        result = _get(
            "/v1/vectors/count?collection=" + quote(collection),
            tenant=self._tenant,
        )
        return int(result.get("count", 0))

    def existing_ids(self, collection: str, ids: list[str]) -> set[str]:
        """Return the subset of *ids* present in *collection*.

        T3Database parity (``nx catalog verify`` / gc paths). Pages at 300
        ids per request to mirror the historical batch shape; a missing or
        unreachable collection resolves to the empty set, matching
        ``T3Database.existing_ids``.
        """
        if not ids:
            return set()
        found: set[str] = set()
        page = 300
        try:
            for start in range(0, len(ids), page):
                batch = ids[start : start + page]
                result = _post(
                    "/v1/vectors/store-get",
                    {"collection": collection, "ids": batch, "limit": len(batch)},
                    tenant=self._tenant,
                )
                found.update(result.get("ids") or [])
        except VectorServiceError as exc:
            _log.warning(
                "http_vector_existing_ids_failed",
                collection=collection,
                error=str(exc),
            )
            return set()
        return found

    def update_chunks(
        self,
        collection: str,
        ids: list[str],
        metadatas: list[dict],
    ) -> None:
        """Metadata-only update on existing chunks — no re-embedding.

        RDR-152 bead nexus-enehl: the frecency-only reindex path calls
        ``db.update_chunks(collection=..., ids=..., metadatas=...)`` on the
        db object.  In service mode ``db`` is an :class:`HttpVectorClient`;
        this method routes the update through the service's
        ``/v1/vectors/update-metadata`` endpoint so the frecency_score lands
        in the service's Chroma (the one search reads) — not daemon-Chroma.

        Batches at MAX_RECORDS_PER_WRITE (300) to match the service's quota
        validator and to mirror :meth:`T3Database.update_chunks` parity.
        """
        if not ids:
            return
        from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415
        size = QUOTAS.MAX_RECORDS_PER_WRITE
        for start in range(0, len(ids), size):
            batch_ids  = ids[start : start + size]
            batch_meta = metadatas[start : start + size]
            _post(
                "/v1/vectors/update-metadata",
                {"collection": collection, "ids": batch_ids, "metadatas": batch_meta},
                tenant=self._tenant,
            )
        _log.debug(
            "http_vector_update_chunks",
            collection=collection,
            count=len(ids),
        )

    # ── Collection-handle stub for doc_indexer staleness + prune paths ─────────

    def get_collection(self, name: str) -> "_ServiceCollectionStub":
        """Return a collection stub, raising ChromaNotFoundError if the collection does not exist.

        RDR-152 bead nexus-enehl: mirrors T3Database.get_collection() semantics
        for the frecency-only loop.  The loop catches ChromaNotFoundError and
        skips collections that have not yet been indexed.

        Checks existence via the service's ``/v1/vectors/collections`` list.
        A missing collection raises ``chromadb.errors.NotFoundError`` rather than
        creating a zombie collection (contrast with
        :meth:`get_or_create_collection`).
        """
        from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: PLC0415
        try:
            cols = self.list_collections()
            if not any(c.get("name") == name for c in cols):
                raise _ChromaNotFoundError(f"collection {name!r} not found in service")
        except VectorServiceError as exc:
            raise _ChromaNotFoundError(
                f"service unavailable checking collection {name!r}"
            ) from exc
        return _ServiceCollectionStub(name=name, tenant=self._tenant)

    def get_or_create_collection(self, name: str) -> "_ServiceCollectionStub":
        """Return a stub collection handle for staleness checks.

        doc_indexer._index_document / _index_pdf_incremental use the
        returned handle for:
          - ``col.get(where=..., ...)`` incremental staleness check
          - ``col.delete(ids=...)`` stale-chunk pruning

        The stub routes the staleness check through the service's
        ``/v1/vectors/get`` endpoint and routes deletes through
        ``/v1/vectors/store-delete``, making both paths work end-to-end
        against the Java service.
        """
        return _ServiceCollectionStub(name=name, tenant=self._tenant)

    def get_embeddings(self, collection: str, ids: list[str]):
        """Fetch stored embeddings for *ids* via the service (nexus-pebfx.7).

        Mirrors ``T3Database.get_embeddings``: returns an ``(N, D)`` float32
        ndarray with rows in request order; ids the service does not find
        are DROPPED (``N < len(ids)``), which the search-engine caller
        already treats as a per-collection shape-mismatch failure —
        identical to the Chroma path's semantics.
        """
        import numpy as np

        result = _post(
            "/v1/vectors/get-embeddings",
            {"collection": collection, "ids": ids},
            tenant=self._tenant,
        )
        return np.array(result.get("embeddings", []), dtype=np.float32)

    # ── Stubs for T3Database surface not used by Seam B ─────────────────────

    def delete_collection(self, name: str) -> None:
        raise NotImplementedError("delete_collection not implemented in HttpVectorClient")

    def delete_by_source(self, collection: str, source_path: str) -> int:
        raise NotImplementedError("delete_by_source not implemented in HttpVectorClient")

    # ── Utility ──────────────────────────────────────────────────────────────

    @staticmethod
    def chunk_id(text: str) -> str:
        """Compute the canonical chunk natural ID: sha256(text)[:32]."""
        return hashlib.sha256(text.encode()).hexdigest()[:32]


# ── Module-level routing helper ───────────────────────────────────────────────

_vector_client_lock = threading.Lock()
_vector_client_instance: HttpVectorClient | None = None


def get_http_vector_client() -> HttpVectorClient:
    """Return the process-local HttpVectorClient singleton."""
    global _vector_client_instance
    if _vector_client_instance is None:
        with _vector_client_lock:
            if _vector_client_instance is None:
                _vector_client_instance = HttpVectorClient()
    return _vector_client_instance


def reset_http_vector_client_for_tests() -> None:
    """Test helper: reset the singleton."""
    global _vector_client_instance
    with _vector_client_lock:
        _vector_client_instance = None


def is_vector_service_mode() -> bool:
    """Return True when NX_STORAGE_BACKEND_VECTORS=service.

    RDR-155 P4a.2 note: since the serving cutover, ``make_t3()`` returns the
    service-backed client unconditionally — this env flag survives only as
    the explicit indexer-side opt-in (skip Python-side embedding). For
    "can this HANDLE do chroma-client things?" decisions use
    :func:`is_service_backed` on the handle instead: env state and handle
    type diverge in tests that inject a chroma-backed ``T3Database``.
    """
    return os.environ.get(_VECTORS_BACKEND_ENV, "").strip().lower() == "service"


def is_service_backed(db: object) -> bool:
    """True when *db* routes T3 ops through the nexus-service HTTP API.

    The instance-based capability guard (RDR-155 P4a.2, nexus-1k8s1):
    service-backed handles have no raw ``._client`` and no chroma-coupled
    surface. Prefer this over :func:`is_vector_service_mode` wherever the
    handle is in hand — injected chroma-backed ``T3Database`` test fixtures
    must keep taking the legacy branches regardless of env state.
    """
    return isinstance(db, HttpVectorClient)
