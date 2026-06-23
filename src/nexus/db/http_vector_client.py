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
from datetime import UTC, datetime
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
    """(url, token) from the supervisor's lease, or (None, None).

    RDR-152 nexus-fjwxh: delegates to the centralized
    :func:`nexus.db.service_endpoint.discover_lease` so every storage client
    (T2 stores, catalog, T3) shares ONE discovery implementation. Kept as a
    module-local name because the catalog client and the discovery tests
    import ``_discover_lease`` from here.
    """
    from nexus.db.service_endpoint import discover_lease

    return discover_lease()


def _resolve_endpoint() -> tuple[str, str]:
    """Return ``(base_url, token)`` per the resolution order above."""
    global _lease_cache
    # env FIRST, then the persisted config.yml credential (RDR-166 nexus-v3p0x:
    # a greenfield managed user who ran `nx config set service_url/service_token`
    # must reach a resolvable endpoint with no env exported). get_credential
    # encodes env>config.yml precedence, so an exported env var still wins.
    from nexus.config import get_credential

    env_url = (get_credential("service_url") or "").strip().rstrip("/") or None
    env_token = (get_credential("service_token") or "").strip() or None
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
        # "credential" = env-or-config.yml (get_credential precedence); the
        # source here is "configured" vs "lease", not specifically env.
        if env_url is not None and token is lease_token and token is not None:
            _log.debug(
                "vector_endpoint_mixed_source", url_source="credential", token_source="lease"
            )
        elif env_token is not None and url is lease_url and url is not None:
            _log.debug(
                "vector_endpoint_mixed_source", url_source="lease", token_source="credential"
            )
    if url is None or token is None:
        raise RuntimeError(
            "nexus-service endpoint is not resolvable: T3 vector serving "
            "routes through the nexus-service HTTP API (RDR-155 Phase 4a — "
            "the direct Chroma serving paths are retired). Either start the "
            "supervisor with 'nx daemon service start' (publishes the "
            "endpoint lease this client auto-discovers), set the managed "
            "endpoint with 'nx config set service_url/service_token', or export "
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
        # TimeoutError is intentionally NOT in this retry classifier (it is not an
        # auto-restart signature); it propagates straight to the _get/_post handler,
        # which reframes it for managed endpoints (nexus-kf679).
        if not _is_retryable_endpoint_error(exc):
            raise
        _log.info(
            "vector_endpoint_reresolve",
            path=path,
            reason=type(exc).__name__,
        )
        _invalidate_endpoint()
        return _request_once(method, path, tenant=tenant, timeout=timeout, body=body)


def _managed_remedy() -> str | None:
    """Remedy text when the client is pointed at an EXPLICIT managed endpoint.

    RDR-001 (nexus-kf679): a misconfigured managed-cloud endpoint otherwise fails
    at the first /v1 call with a bare connection error / HTTP 401 and no guidance.
    When ``NX_SERVICE_URL`` is explicitly set we reframe that failure with an
    actionable remedy. Returns ``None`` for the local/lease topology
    (``NX_SERVICE_URL`` unset) so a local user's transient errors are NEVER
    reframed as a managed-service problem — and their error type/flow is unchanged.

    Note: a managed-cloud user with ``NX_SERVICE_URL`` UNSET is not a silent dead
    zone — there is no local supervisor lease to discover, so
    :func:`_resolve_endpoint` fails loud first ("export NX_SERVICE_URL / TOKEN")
    before any request reaches here. This reframing covers the set-but-wrong case.

    Exception-type note: for an explicit managed endpoint, connection-level errors
    (URLError/ConnectionError/TimeoutError) are surfaced by :func:`_get`/:func:`_post`
    as :class:`VectorServiceError` (``code=None``) rather than the raw urllib/OSError
    — callers that classify transient failures by raw type should catch
    ``VectorServiceError`` for the managed path. Local callers are unaffected.
    """
    from nexus.config import get_credential

    # env FIRST, then config.yml — so a config.yml-only greenfield user gets the
    # actionable managed remedy on a 401/connection error, not a bare error
    # (RDR-166 nexus-v3p0x).
    base = (get_credential("service_url") or "").strip()
    if not base:
        return None
    return (
        f"the managed nexus service at {base} could not be reached/authenticated "
        "— check NX_SERVICE_URL is reachable and NX_SERVICE_TOKEN is valid "
        "(verify with `nx service probe` or `nx doctor`)."
    )


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
        msg = f"POST {path} → HTTP {e.code}: {err.get('error', err)}"
        remedy = _managed_remedy() if e.code in (401, 403) else None
        if remedy:
            msg += f"\n{remedy}"
        raise VectorServiceError(msg, code=e.code) from e
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        # Connection-level failure (bad/unreachable endpoint). Reframe with a
        # remedy ONLY for an explicit managed endpoint; local/lease users keep
        # the original error and flow unchanged.
        remedy = _managed_remedy()
        if remedy is None:
            raise
        raise VectorServiceError(f"POST {path} failed: {e}\n{remedy}") from e


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
        msg = f"GET {path} → HTTP {e.code}: {err.get('error', err)}"
        remedy = _managed_remedy() if e.code in (401, 403) else None
        if remedy:
            msg += f"\n{remedy}"
        raise VectorServiceError(msg, code=e.code) from e
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        remedy = _managed_remedy()
        if remedy is None:
            raise
        raise VectorServiceError(f"GET {path} failed: {e}\n{remedy}") from e


class VectorServiceError(RuntimeError):
    """Raised when the vector service returns an error.

    ``code`` carries the HTTP status when the failure was an HTTP error
    response (404 from an older service JAR, 422 model-unavailable, ...);
    ``None`` for transport-level failures. Callers use it for
    deployment-skew fallbacks (RDR-156 P3: /stats absent on a pre-catalog-005
    JAR → fall back to /collections + /count).
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


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
        skip_existing: bool | None = None,
    ) -> None:
        """Embed + write via the Java service.

        Dedup + conflict-merge are SERVER-ENFORCED (nexus-57dh4): the service's
        ``PgVectorRepository.upsertChunksInternal`` does first-wins in-batch dedup
        and ``ON CONFLICT (tenant_id, collection, chash) DO UPDATE``. There is no
        client-side quota check or 300-record cap on this path — the whole id set
        is sent in one POST. (The old "quota-check" framing was a ChromaDB-Cloud
        leftover; Postgres has no such limit.)

        CHUNKING STAYS PYTHON — this method is called with pre-chunked text.
        Embeddings are computed server-side by default (Seam B). The ONE
        exception is the same-model migration PASSTHROUGH (nexus-hxry2): when
        ``embeddings`` is supplied, the vectors are sent and stored verbatim and
        the server skips the (billed) re-embed — used only when the source
        collection's model equals the target's wired model, so the vectors are
        already correct. Every non-migration caller leaves ``embeddings`` None.
        (Note: :meth:`upsert_chunks_with_embeddings` deliberately still DISCARDS
        its vectors — indexers re-embed server-side as the single authority.)

        ``skip_existing`` (or env ``NX_UPSERT_SKIP_EXISTING=1``): pre-filter
        ids through :meth:`existing_ids` and embed/upsert only the chunks
        the collection does not already hold. Chunk ids are content hashes,
        so re-indexing unchanged files re-sends byte-identical chunks whose
        server-side embedding cost is pure waste — the pre-filter makes a
        forced re-convergence run pay only for genuinely missing chunks
        (nexus-7zuzz orphan remediation). OPT-IN, not default: skipping an
        existing id also skips the ON CONFLICT DO UPDATE metadata refresh
        (line numbers can drift for identical chunk text after edits
        elsewhere in the file). ``existing_ids`` resolves to the EMPTY set
        on probe failure, so a degraded probe upserts everything — chunks
        are never silently dropped.
        """
        if not ids:
            return
        if skip_existing is None:
            skip_existing = os.environ.get("NX_UPSERT_SKIP_EXISTING", "") == "1"
        if skip_existing:
            present = self.existing_ids(collection, ids)
            if present:
                keep = [i for i, _id in enumerate(ids) if _id not in present]
                if not keep:
                    _log.debug(
                        "http_vector_upsert_all_present",
                        collection=collection, count=len(ids),
                    )
                    return
                metas = metadatas or [{}] * len(ids)
                ids = [ids[i] for i in keep]
                documents = [documents[i] for i in keep]
                metadatas = [metas[i] for i in keep]
                if embeddings is not None:
                    embeddings = [embeddings[i] for i in keep]
        body: dict[str, Any] = {
            "collection": collection,
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas or [{}] * len(ids),
        }
        # Same-model vector PASSTHROUGH (nexus-hxry2): when the caller supplies
        # precomputed vectors (source model == target wired model), send them so
        # the service stores them verbatim and skips the billed re-embed. Absent →
        # the default Seam B server-side embed. This is the ONE path where
        # ``embeddings`` is honoured; every other caller leaves it None.
        if embeddings is not None:
            body["embeddings"] = embeddings
        _post("/v1/vectors/upsert-chunks", body, tenant=self._tenant, timeout=600)
        _log.debug(
            "http_vector_upsert_chunks",
            collection=collection,
            count=len(ids),
        )

    def upsert_chunks_with_embeddings(
        self,
        collection_name: str,
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

        Param name ``collection_name`` (not ``collection``) matches
        ``T3Database.upsert_chunks_with_embeddings`` so callers using the kwarg
        form (code_indexer.py:470, prose_indexer.py:233, exporter.py:431,448)
        don't get a TypeError (nexus-7zuzz).
        """
        self.upsert_chunks(
            collection_name, ids, documents, metadatas=metadatas
        )

    def put(
        self,
        collection: str,
        content: str,
        title: str = "",
        tags: str = "",
        category: str = "",
        session_id: str = "",
        source_agent: str = "",
        store_type: str = "knowledge",
        ttl_days: int = 0,
        catalog_doc_id: str = "",
    ) -> str:
        """Upsert *content* into *collection*. Returns the document ID.

        Drop-in parity with ``T3Database.put`` (nexus-7zuzz): same parameter
        list, same doc_id derivation (sha256(content)[:32]), and metadata built
        via the SAME :func:`nexus.metadata_schema.make_chunk_metadata` factory
        that T3Database.put uses — parity by construction, not by duplication.

        ``store_type`` is accepted for API symmetry but intentionally not
        forwarded: T3Database also ignores it (RDR-101 Phase 5c dropped
        store_type from ALLOWED_TOP_LEVEL; content_type derives from the
        collection prefix, identical logic is applied here).

        ``catalog_doc_id`` is an HTTP-path superset: T3Database.put() accepts
        the param but normalize() strips it from the Chroma write (not in
        ALLOWED_TOP_LEVEL); on the T3 path catalog association is via the hook
        chain, not chunk metadata. HttpVectorClient stamps it into the service
        request body so the Java layer can persist the tumbler cross-reference
        if the service endpoint accepts it. This is a documented divergence, not
        a parity gap — see EXCLUSIONS comment in the parity test.

        Single-chunk: one HTTP call per put() call. T3Database.put uses
        ``fail_on_oversized=True``; the server is responsible for rejecting
        oversized content on the HTTP path.
        """
        from nexus.corpus import (  # noqa: PLC0415
            embedding_model_for_collection_name,
            index_model_for_collection,
        )
        from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        doc_id = content_hash[:32]
        now_iso = datetime.now(UTC).isoformat()

        # Derive content_type from collection prefix — mirrors T3Database.put
        # at t3.py:860-870 exactly.
        prefix_to_ct = {
            "code__": "code",
            "docs__": "prose",
            "rdr__": "markdown",
            "knowledge__": "prose",
        }
        content_type = "prose"
        for prefix, ct in prefix_to_ct.items():
            if collection.startswith(prefix):
                content_type = ct
                break

        metadata = make_chunk_metadata(
            content_type=content_type,
            chunk_text_hash=content_hash,
            content_hash=content_hash,
            chunk_start_char=0,
            chunk_end_char=len(content),
            indexed_at=now_iso,
            embedding_model=(
                embedding_model_for_collection_name(collection)
                or index_model_for_collection(collection)
            ),
            title=title,
            tags=tags,
            category=category,
            ttl_days=ttl_days,
            source_agent=source_agent,
            session_id=session_id,
        )

        # catalog_doc_id: HTTP-path superset (see docstring). Stamp when present;
        # omit when empty to keep the body clean for the legacy/no-catalog path.
        if catalog_doc_id:
            metadata["catalog_doc_id"] = catalog_doc_id

        body: dict[str, Any] = {
            "collection": collection,
            "doc_id": doc_id,
            "content": content,
            "metadata": metadata,
        }
        result = _post("/v1/vectors/store-put", body, tenant=self._tenant)
        return result.get("id", doc_id)

    # ── Read path ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        collection_names: list[str],
        n_results: int = 10,
        where: dict | None = None,
        *,
        cluster_by: str = "",
        threshold: float | None = None,
        structured: bool = False,
    ) -> list[dict] | dict:
        """Semantic search via the Java service.

        Param name ``collection_names`` (not ``collections``) matches
        ``T3Database.search`` (nexus-7zuzz). The HTTP body key stays
        ``"collections"`` — that is what the Java VectorHandler reads.

        The service embeds the query server-side and returns ranked results.
        Returns the same list-of-dicts shape as ``T3Database.search()``
        when ``structured=False``, or a ``{ids, tumblers, distances, collections}``
        dict when ``structured=True``.
        """
        body: dict[str, Any] = {
            "query": query,
            "collections": collection_names,
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

    def search_metadata_scoped(
        self,
        query: str,
        collection_names: list[str],
        *,
        content_type: str | None = None,
        author: str | None = None,
        year: int | None = None,
        corpus: str | None = None,
        subtree: str | None = None,
        where: dict | None = None,
        n_results: int = 10,
    ) -> list[dict]:
        """Metadata-scoped combined search (RDR-156 P4, Decision 5; catalog-008).

        Routes to ``POST /v1/vectors/search-metadata-scoped`` —
        ``nexus.search_metadata_scoped_<dim>``, which joins the chunk table to
        the catalog manifest + documents and filters by the catalog dimensions
        in ONE statement (the unification of the ``query`` tool's app-side
        catalog-routing dance). A ``None``/empty filter is omitted (no filter on
        that dimension). ``author`` is matched case-insensitively as a SUBSTRING
        (ILIKE), ``subtree`` is a tumbler-prefix scope, ``where`` is a
        chunk-metadata equality map (JSONB containment). Returns the flat
        ``{id, content, distance, collection, chash}`` row list; ``id`` is the
        document tumbler (de-dup per id is the caller's job); ``chash`` is the
        matched chunk's hash (RDR-086 ``chunk_text_hash`` source).
        """
        body: dict[str, Any] = {
            "query": query,
            "collections": collection_names,
            "n_results": n_results,
        }
        if content_type is not None:
            body["content_type"] = content_type
        if author is not None:
            body["author"] = author
        if year is not None:
            body["year"] = year
        if corpus is not None:
            body["corpus"] = corpus
        if subtree is not None:
            body["subtree"] = subtree
        if where:
            body["where"] = where
        return _post("/v1/vectors/search-metadata-scoped", body, tenant=self._tenant)

    def search_topic_scoped(
        self,
        query: str,
        topic: str,
        collection: str,
        *,
        n_results: int = 10,
    ) -> list[dict]:
        """Topic-scoped combined search (RDR-156 P4, Decision 5).

        Routes to ``POST /v1/vectors/search-topic-scoped`` —
        ``nexus.search_topic_scoped_<dim>`` (catalog-006). Topic membership is
        chunk-level (``topic_assignments.doc_id`` is a chunk chash, nexus-sa14p),
        so results are chunk-level (``id`` is the chunk chash). Returns the flat
        ``{id, content, distance, collection}`` row list.
        """
        body: dict[str, Any] = {
            "query": query,
            "topic": topic,
            "collection": collection,
            "n_results": n_results,
        }
        return _post("/v1/vectors/search-topic-scoped", body, tenant=self._tenant)

    def search_graph_hop(
        self,
        query: str,
        seeds: list[str],
        collection_names: list[str],
        *,
        link_type: str | None = None,
        depth: int = 1,
        direction: str = "both",
        n_results: int = 10,
    ) -> list[dict]:
        """Graph-hop combined search (RDR-156 P4 follow-on, Decision 5, bead nexus-houg9).

        Routes to ``POST /v1/vectors/search-graph-hop`` —
        ``nexus.search_graph_hop_<dim>`` (catalog-007): a ``WITH RECURSIVE`` BFS over
        ``catalog_links`` from ``seeds`` to ``depth`` hops collects the reachable
        document set, joins ``chunks_<dim>``, and vector-ranks. The single-statement
        unification of the ``query`` tool's ``follow_links`` app-side graphBFS dance.
        ``link_type=None`` follows all edge types; ``direction`` is ``"out"``/``"in"``/
        ``"both"`` (default ``"both"``, matching ``Catalog.graph``); ``depth`` is clamped
        to [1,3] service-side. Returns the flat ``{id, content, distance, collection,
        chash}`` row list; ``id`` is the document tumbler, ``chash`` the MATCHED chunk's
        content hash (the repoint populates the RDR-086 ``chunk_text_hash`` from it).
        """
        body: dict[str, Any] = {
            "query": query,
            "seeds": seeds,
            "collections": collection_names,
            "depth": depth,
            "direction": direction,
            "n_results": n_results,
        }
        if link_type is not None:
            body["link_type"] = link_type
        return _post("/v1/vectors/search-graph-hop", body, tenant=self._tenant)

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        """Fetch a single chunk by ID.

        Returns a FLAT dict of ``id`` + ``content`` + all metadata fields, to
        match ``T3Database.get_by_id`` (the drop-in oracle). nexus-ij9hg: the
        prior shape (``id``/``document``/nested ``metadata``) diverged from the
        SQLite oracle, so MCP ``store_get`` / ``store_get_many`` and
        ``nx store get`` — which read ``entry["content"]`` / ``entry["title"]``
        etc. — silently rendered EMPTY content in service mode (the
        post-P4a default). That is the nexus-7zuzz behavioural-divergence class
        signature parity cannot catch.
        """
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
        meta = metas[0] if metas else {}
        return {
            "id": ids[0],
            "content": docs[0] if docs else "",
            **(meta if isinstance(meta, dict) else {}),
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

    def collection_stats(self) -> list[dict]:
        """Per-collection live statistics via ``GET /v1/vectors/stats``.

        RDR-156 P3 (nexus-70r3c.12): served from the
        ``nexus.collection_vector_stats`` SECURITY INVOKER view — one
        round-trip for all of the tenant's collections, TOMBSTONE-FILTERED
        (chunks whose only manifest rows point to trashed documents are not
        counted; manifest-less note chunks are).

        Returns ``[{"name": ..., "dim": 384, "count": N,
        "last_write": "2026-..."}, ...]``, name ascending. Collections with
        zero live chunks do not appear. ``last_write`` may be absent.

        Raises :class:`VectorServiceError` on failure — including ``code=404``
        from a pre-catalog-005 service JAR (deployment skew); callers that
        must work across the skew use :meth:`list_collections`, which falls
        back automatically.
        """
        result = _get("/v1/vectors/stats", tenant=self._tenant)
        return result if isinstance(result, list) else []

    def list_collections(self) -> list[dict]:
        """List the tenant's vector collections with live chunk counts.

        T3Database parity: returns ``[{"name": ..., "count": N}, ...]`` —
        ``nx collection list`` and friends index both keys (the missing
        ``count`` was a live KeyError on every service-mode box, RDR-156 P3).

        Primary path is ONE ``/v1/vectors/stats`` round-trip
        (tombstone-filtered live counts, replacing T3Database's N-way
        threadpooled ``col.count()`` fan-out). On a pre-catalog-005 service
        JAR the route 404s; fall back to ``/collections`` + per-collection
        ``/count`` so the surface keeps working across the deployment skew
        (raw counts — tombstones do not exist on a pre-catalog-005 schema).

        Multi-dim collections (same name in two ``chunks_<dim>`` tables —
        cross-dim re-indexing residue) collapse to one entry, counts summed.
        """
        try:
            stats = self.collection_stats()
        except VectorServiceError as e:
            if e.code != 404:
                _log.warning("http_vector_list_collections_failed", error=str(e))
                return []
            _log.info("http_vector_stats_unavailable_fallback", error=str(e))
            return self._list_collections_via_count()
        merged: dict[str, int] = {}
        for row in stats:
            name = row.get("name", "")
            if name:
                # `or 0` guards an explicit null count, not just an absent key
                merged[name] = merged.get(name, 0) + int(row.get("count") or 0)
        return [{"name": n, "count": c} for n, c in sorted(merged.items())]

    def _list_collections_via_count(self) -> list[dict]:
        """Deployment-skew fallback: ``/collections`` names + N ``/count`` calls.

        Pre-catalog-005 JARs have no ``/stats`` route. Counts here are RAW
        (the old endpoint's semantics); a failing per-collection count is
        reported as -1 rather than dropping the collection from the listing.
        """
        try:
            result = _get("/v1/vectors/collections", tenant=self._tenant)
        except VectorServiceError as e:
            _log.warning("http_vector_fallback_collections_failed", error=str(e))
            return []
        names = [c.get("name", "") for c in result] if isinstance(result, list) else []
        out: list[dict] = []
        for name in names:
            if not name:
                continue
            try:
                out.append({"name": name, "count": self.count(name)})
            except VectorServiceError as e:
                _log.warning(
                    "http_vector_collection_count_failed",
                    collection=name,
                    error=str(e),
                )
                out.append({"name": name, "count": -1})
        return out

    def collection_exists(self, name: str) -> bool:
        """True if *name* holds at least one LIVE chunk (no create side-effect).

        T3Database parity (RDR-155 P4a.2): on the pgvector path a collection
        is a column value, so existence == "has rows for this tenant".
        Since RDR-156 P3 this reads the tombstone-filtered stats view via
        :meth:`list_collections`: a collection whose every chunk belongs to
        trashed documents reads as absent — the Decision 6 single-enforcement
        -point semantics (consumers see live state only).
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

        Sends in request-sized batches of 300 ids. NOTE (nexus-57dh4): this is a
        pragmatic HTTP request-size chunk, NOT a backend quota — the pgvector
        service has no 300-record limit (300 was a ChromaDB-Cloud free-tier quota,
        inapplicable to Postgres). Dedup + conflict-merge are server-enforced in
        ``PgVectorRepository.upsertChunksInternal`` (first-wins in-batch dedup +
        ``ON CONFLICT DO UPDATE``); clients need not pre-dedup or quota-check.
        The constant is reused only to keep a sane per-request size.
        """
        if not ids:
            return
        from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415
        # Request-size chunk only (see docstring) — not a backend quota.
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

    def get_embeddings(self, collection_name: str, ids: list[str]):
        """Fetch stored embeddings for *ids* via the service (nexus-pebfx.7).

        Param name ``collection_name`` matches ``T3Database.get_embeddings``
        (nexus-7zuzz). The HTTP body key stays ``"collection"`` — that is what
        the Java VectorHandler reads.

        Mirrors ``T3Database.get_embeddings``: returns an ``(N, D)`` float32
        ndarray with rows in request order; ids the service does not find
        are DROPPED (``N < len(ids)``), which the search-engine caller
        already treats as a per-collection shape-mismatch failure —
        identical to the Chroma path's semantics.
        """
        import numpy as np

        result = _post(
            "/v1/vectors/get-embeddings",
            {"collection": collection_name, "ids": ids},
            tenant=self._tenant,
        )
        return np.array(result.get("embeddings", []), dtype=np.float32)

    # ── Stubs for T3Database surface not used by Seam B ─────────────────────

    def delete_collection(self, name: str) -> None:
        raise NotImplementedError("delete_collection not implemented in HttpVectorClient")

    def ids_for_source(self, collection_name: str, source_path: str) -> list[str]:
        """Return all chunk IDs for a given source path. Does not fetch content.

        Mirrors ``T3Database.ids_for_source``: paginates the service's
        ``/v1/vectors/get`` where-filter endpoint at the 300-record quota and
        returns an empty list when the collection does not exist (the service
        returns no ids). Param name ``collection_name`` matches the oracle
        (nexus-7zuzz).
        """
        from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415

        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        ids: list[str] = []
        offset = 0
        while True:
            try:
                result = _post(
                    "/v1/vectors/get",
                    {
                        "collection": collection_name,
                        "where": {"source_path": source_path},
                        "include": [],
                        "limit": page_limit,
                        "offset": offset,
                    },
                    tenant=self._tenant,
                )
            except VectorServiceError as exc:
                # Match T3Database, which suppresses ONLY collection-not-found
                # (404) and returns []. A 5xx / 422 / transport failure — or ANY
                # error mid-pagination after ids were already collected — must
                # NOT be masked as "no chunks": delete_by_source would then
                # under-delete and report success, silently orphaning the
                # unread chunks (review: over-broad catch). Re-raise so the
                # prune-stale call site's except-clause reports SKIP loudly.
                if exc.code == 404 and offset == 0:
                    return []
                raise
            page = result.get("ids", []) or []
            ids.extend(page)
            if len(page) < page_limit:
                break
            offset += len(page)  # match T3Database oracle (not += page_limit)
        return ids

    def delete_by_source(self, collection_name: str, source_path: str) -> int:
        """Delete all chunks for a given source path; return the count deleted.

        nexus-vhyua: previously a NotImplementedError stub, which made
        ``nx t3 prune-stale --no-dry-run`` print 'delete failed' per path and
        silently do nothing in service mode (the post-P4a default). Now built
        from existing primitives — ``ids_for_source`` (``/v1/vectors/get``
        where-filter) + ``/v1/vectors/store-delete`` — so no new Java endpoint
        is required. Param name ``collection_name`` matches
        ``T3Database.delete_by_source`` (nexus-7zuzz).

        Count semantics differ slightly from the oracle by design: T3Database
        returns ``len(ids)`` (ids it asked to delete); this returns the sum of
        the service's CONFIRMED ``deleted`` counts. They match unless a
        concurrent delete already removed some — in which case the prune-stale
        caller's ``deleted != len(ids)`` WARN correctly fires.
        """
        from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415

        ids = self.ids_for_source(collection_name, source_path)
        if not ids:
            return 0
        # Batch at the 300-record write quota — a source with many chunks would
        # otherwise exceed MAX_RECORDS_PER_WRITE in a single store-delete.
        batch = QUOTAS.MAX_RECORDS_PER_WRITE
        deleted = 0
        for i in range(0, len(ids), batch):
            result = _post(
                "/v1/vectors/store-delete",
                {"collection": collection_name, "ids": ids[i:i + batch]},
                tenant=self._tenant,
            )
            deleted += int(result.get("deleted", 0))
        return deleted

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
    """Return True unless NX_STORAGE_BACKEND_VECTORS explicitly opts out.

    nexus-tawx0: since the RDR-155 P4a.2 serving cutover, ``make_t3()``
    returns the service-backed client UNCONDITIONALLY — service mode is
    the default reality, so this defaults True. The opt-in era left the
    no-Python-embed stubs (doc/prose/code indexers) inert in default
    environments: every indexing run client-embedded via Voyage, the
    client discarded the vectors, and the server embedded again — double
    spend per chunk, empirically proven by voyageai tracebacks in
    production hook runs (2026-06-11).

    The env var survives as an explicit OPT-OUT (any value other than
    ``service``/empty, conventionally ``chroma``) for test setups that
    inject a chroma-backed ``T3Database``. For "can this HANDLE do
    chroma-client things?" decisions use :func:`is_service_backed` on the
    handle instead: env state and handle type diverge in those tests.
    """
    value = os.environ.get(_VECTORS_BACKEND_ENV, "").strip().lower()
    return value in ("", "service")


def is_service_backed(db: object) -> bool:
    """True when *db* routes T3 ops through the nexus-service HTTP API.

    The instance-based capability guard (RDR-155 P4a.2, nexus-1k8s1):
    service-backed handles have no raw ``._client`` and no chroma-coupled
    surface. Prefer this over :func:`is_vector_service_mode` wherever the
    handle is in hand — injected chroma-backed ``T3Database`` test fixtures
    must keep taking the legacy branches regardless of env state.
    """
    return isinstance(db, HttpVectorClient)
