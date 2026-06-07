# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-152 bead nexus-gmiaf.20 — Seam B HTTP vector client.

Thin Python bridge that routes T3 vector operations (search, query,
upsert-chunks, store_put, store_get, store_list, store_delete) through
the Java nexus-service HTTP endpoints rather than hitting ChromaDB /
Voyage AI directly from Python.

Activated by setting ``NX_STORAGE_BACKEND_VECTORS=service`` in the
process environment. The default (flag unset) routes through the
existing ``T3Database`` path unchanged.

Chunking stays in Python; only embed+quota+Chroma-write move to the JVM
(Seam B contract — CHUNKING STAYS PYTHON per the bead relay).
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

#: Default service URL. Override via NX_SERVICE_URL env var.
_DEFAULT_SERVICE_URL = "http://127.0.0.1:8080"

#: Env var for the vector backend flag.
_VECTORS_BACKEND_ENV = "NX_STORAGE_BACKEND_VECTORS"


def _service_url() -> str:
    return os.environ.get("NX_SERVICE_URL", _DEFAULT_SERVICE_URL).rstrip("/")


def _service_token() -> str:
    tok = os.environ.get("NX_SERVICE_TOKEN", "")
    if not tok:
        raise RuntimeError(
            "NX_SERVICE_TOKEN must be set when NX_STORAGE_BACKEND_VECTORS=service"
        )
    return tok


# ── HTTP transport ────────────────────────────────────────────────────────────


def _post(path: str, body: dict, *, tenant: str = "default") -> Any:
    """POST JSON to the service endpoint, return parsed response body."""
    import urllib.error
    import urllib.request

    url = _service_url() + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_service_token()}",
            "X-Nexus-Tenant": tenant,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
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
    import urllib.request

    url = _service_url() + path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {_service_token()}",
            "X-Nexus-Tenant": tenant,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
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
        _post("/v1/vectors/upsert-chunks", body, tenant=self._tenant)
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
        """List all Chroma collections managed by the service."""
        try:
            result = _get("/v1/vectors/collections", tenant=self._tenant)
            return result if isinstance(result, list) else []
        except VectorServiceError as e:
            _log.warning("http_vector_list_collections_failed", error=str(e))
            return []

    # ── Stubs for T3Database surface not used by Seam B ─────────────────────

    def delete_collection(self, name: str) -> None:
        raise NotImplementedError("delete_collection not implemented in HttpVectorClient")

    def delete_by_source(self, collection: str, source_path: str) -> int:
        raise NotImplementedError("delete_by_source not implemented in HttpVectorClient")

    def get_embeddings(self, collection: str, ids: list[str]):  # type: ignore[return]
        raise NotImplementedError("get_embeddings not implemented in HttpVectorClient")

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
    """Return True when NX_STORAGE_BACKEND_VECTORS=service."""
    return os.environ.get(_VECTORS_BACKEND_ENV, "").strip().lower() == "service"
