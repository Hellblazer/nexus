# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import chromadb
import chromadb.errors
import httpx
import voyageai
from chromadb.errors import NotFoundError as _ChromaNotFoundError
import structlog

from nexus.corpus import embedding_model_for_collection, index_model_for_collection
from nexus.db.chroma_quotas import QUOTAS, QuotaValidator

_log = structlog.get_logger(__name__)

# The four ChromaDB Cloud databases, one per content type.
# ``chroma_database`` in config is the *base name*; each store is
# ``{base}_{type}``.  Example: base="nexus" → nexus_code, nexus_docs,
# nexus_rdr, nexus_knowledge.
_STORE_TYPES: tuple[str, ...] = ("code", "docs", "rdr", "knowledge")


# ── ChromaDB transient-error retry ───────────────────────────────────────────

_RETRYABLE_FRAGMENTS: frozenset[str] = frozenset({
    "502", "503", "504", "429",
    "bad gateway", "service unavailable", "gateway time-out", "too many requests",
})
_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})


def _is_retryable_chroma_error(exc: BaseException) -> bool:
    """Return True if *exc* represents a transient ChromaDB Cloud error worth retrying.

    Check order:
    1. Transport-level errors (ConnectError, ReadTimeout, RemoteProtocolError) — always retry.
    2. Chained httpx.HTTPStatusError — authoritative integer status code check.
    3. String fallback — plain Exception message body (gateway HTML or chroma JSON).
    """
    # 1. Transport-level errors — no HTTP response, but clearly transient.
    if isinstance(exc, httpx.TransportError):
        return True
    # 2. ChromaDB wraps HTTPStatusError as Exception(resp.text); original is __context__.
    ctx = exc.__context__
    if isinstance(ctx, httpx.HTTPStatusError):
        return ctx.response.status_code in _RETRYABLE_HTTP_STATUSES
    # 3. Fallback: scan the message body for retryable status tokens.
    msg = str(exc).lower()
    return any(fragment in msg for fragment in _RETRYABLE_FRAGMENTS)


def _chroma_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 5,
    **kwargs: Any,
) -> Any:
    """Call *fn* with exponential backoff on transient ChromaDB Cloud errors.

    Retries up to *max_attempts* times (default 5).  Backoff starts at 2 s,
    doubles each attempt, capped at 30 s.  Non-retryable errors raise immediately.
    """
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == max_attempts or not _is_retryable_chroma_error(exc):
                raise
            _log.warning(
                "chroma_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error=str(exc)[:120],
            )
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


class T3Database:
    """T3 ChromaDB CloudClient permanent knowledge store.

    Uses four separate ``chromadb.CloudClient`` instances — one per content
    type — derived from the ``chroma_database`` base name:

    - ``{base}_code``      for ``code__*`` collections
    - ``{base}_docs``      for ``docs__*`` collections
    - ``{base}_rdr``       for ``rdr__*`` collections
    - ``{base}_knowledge`` for ``knowledge__*`` collections and fallback

    All routing is internal to this class.  Every public caller
    (``search_cmd``, ``indexer``, etc.) remains unchanged.

    The ``_client`` and ``_ef_override`` keyword arguments are injection
    points for testing — pass an ``EphemeralClient`` and
    ``DefaultEmbeddingFunction`` to run the full code path without any API
    keys.  When ``_client`` is provided, all four store types are mapped to
    the same client (single-mock backward compatibility).
    """

    def __init__(
        self,
        tenant: str = "",
        database: str = "",
        api_key: str = "",
        voyage_api_key: str = "",
        *,
        _client=None,
        _ef_override=None,
    ) -> None:
        self._voyage_api_key = voyage_api_key
        self._ef_override = _ef_override
        self._ef_cache: dict[str, object] = {}
        self._ef_lock = threading.Lock()
        self._write_sems: dict[str, threading.BoundedSemaphore] = {}
        self._read_sems: dict[str, threading.BoundedSemaphore] = {}
        self._sems_lock = threading.Lock()
        self._quota_validator = QuotaValidator()
        self._voyage_client: voyageai.Client | None = (
            voyageai.Client(api_key=voyage_api_key) if voyage_api_key else None
        )
        if _client is not None:
            # Test injection: single client serves all store types.
            self._clients: dict[str, object] = {t: _client for t in _STORE_TYPES}
        else:
            _clients: dict[str, object] = {}
            for t in _STORE_TYPES:
                db_name = f"{database}_{t}"
                try:
                    _clients[t] = chromadb.CloudClient(
                        tenant=tenant or None, database=db_name, api_key=api_key
                    )
                except Exception as exc:
                    _log.debug("cloud_client_connect_failed", database=db_name, error=str(exc))
                    raise RuntimeError(
                        f"Failed to connect to ChromaDB Cloud database {db_name!r}.\n"
                        f"Ensure these four databases exist in your ChromaDB Cloud dashboard:\n"
                        + "\n".join(f"  - {database}_{t2}" for t2 in _STORE_TYPES)
                    ) from exc
            self._clients = _clients

    # ── Context manager (no-op: CloudClient is stateless REST) ───────────────

    def __enter__(self) -> "T3Database":
        return self

    def __exit__(self, *_) -> None:
        pass  # ChromaDB CloudClient is HTTP-based; no persistent connection to close.

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _client_for(self, collection_name: str) -> object:
        """Route a collection name to the correct ChromaDB client.

        Routing is by prefix (the part before ``__``):
        - ``code__*``      → code client
        - ``docs__*``      → docs client
        - ``rdr__*``       → rdr client
        - ``knowledge__*`` → knowledge client
        - no ``__`` or unknown prefix → knowledge client (with a warning)
        """
        if "__" in collection_name:
            prefix = collection_name.split("__")[0]
        else:
            _log.warning("collection_no_prefix", collection=collection_name)
            prefix = "knowledge"
        client = self._clients.get(prefix)
        if client is None:
            _log.warning(
                "unknown_collection_prefix",
                prefix=prefix,
                collection=collection_name,
            )
            client = self._clients["knowledge"]
        return client

    def _embedding_fn(self, collection_name: str):
        """Return a VoyageAI EF (always voyage-4) for collection creation.

        The voyage-4 EF is attached to collections for structural compatibility
        but is NOT called for CCE collections at write or query time:
        - Write: ``upsert_chunks_with_embeddings()`` passes pre-computed CCE
          embeddings; ``put()`` calls ``_cce_embed()`` directly and passes
          ``embeddings=`` to bypass the EF.
        - Query: ``search()`` calls ``_cce_embed()`` and passes
          ``query_embeddings=`` to bypass the EF.
        Caching is per-collection-name to match the existing test contract.
        """
        if self._ef_override is not None:
            return self._ef_override
        with self._ef_lock:
            if collection_name not in self._ef_cache:
                self._ef_cache[collection_name] = (
                    chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction(
                        model_name="voyage-4", api_key=self._voyage_api_key
                    )
                )
            return self._ef_cache[collection_name]

    def _cce_embed(
        self, text: str, input_type: Literal["query", "document"] = "document"
    ) -> list[float]:
        """Embed *text* via the Voyage AI Contextualized Chunk Embedding API.

        CCE collections (docs__, knowledge__, rdr__) use voyage-context-3 at
        both index and query time.  voyage-4 is **not** compatible with CCE
        vector spaces (cross-model cosine similarity ≈ 0.05, i.e. random noise).

        ``inputs=[[text]]`` — one inner list — embeds the text independently,
        with no cross-chunk context propagation.  Use ``input_type="document"``
        (default) for documents being stored and ``input_type="query"`` for
        search queries.  Using the wrong subtype is the same class of bug as
        the original CCE/voyage-4 mismatch.
        """
        assert self._voyage_client is not None, "_cce_embed called without voyage_api_key"
        result = self._voyage_client.contextualized_embed(
            inputs=[[text]],
            model="voyage-context-3",
            input_type=input_type,
        )
        assert result.results, "voyageai CCE returned empty results"
        return result.results[0].embeddings[0]

    def _write_sem(self, name: str) -> threading.BoundedSemaphore:
        """Return the per-collection write semaphore, lazily initialised."""
        with self._sems_lock:
            if name not in self._write_sems:
                self._write_sems[name] = threading.BoundedSemaphore(QUOTAS.MAX_CONCURRENT_WRITES)
            return self._write_sems[name]

    def _read_sem(self, name: str) -> threading.BoundedSemaphore:
        """Return the per-collection read semaphore, lazily initialised."""
        with self._sems_lock:
            if name not in self._read_sems:
                self._read_sems[name] = threading.BoundedSemaphore(QUOTAS.MAX_CONCURRENT_READS)
            return self._read_sems[name]

    def _validate_record(
        self,
        id: str,
        document: str,
        embedding: list[float] | None,
        metadata: dict,
        uri: str | None = None,
    ) -> None:
        """Validate a single record against ChromaDB Cloud quota limits."""
        self._quota_validator.validate_record(
            id=id, document=document, embedding=embedding, metadata=metadata, uri=uri
        )

    def _write_batch(
        self,
        col,
        collection_name: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        """Split into ≤300-record chunks and upsert each.

        Acquires the per-collection write semaphore for the duration of all chunks.
        """
        # Defense-in-depth: drop any document that exceeds the hard ChromaDB limit.
        # The chunker-level SAFE_CHUNK_BYTES cap should prevent this from ever firing.
        max_bytes = QUOTAS.MAX_DOCUMENT_BYTES
        valid = [
            i for i, doc in enumerate(documents)
            if len(doc.encode()) <= max_bytes
        ]
        if len(valid) < len(documents):
            for i, doc in enumerate(documents):
                if len(doc.encode()) > max_bytes:
                    source = metadatas[i].get("source_path", "<unknown>") if i < len(metadatas) else "<unknown>"
                    _log.warning(
                        "write_batch_oversized_document_dropped",
                        source_path=source,
                        doc_bytes=len(doc.encode()),
                        max_bytes=max_bytes,
                        collection=collection_name,
                    )
            if not valid:
                return
            ids = [ids[i] for i in valid]
            documents = [documents[i] for i in valid]
            metadatas = [metadatas[i] for i in valid]
            if embeddings is not None:
                embeddings = [embeddings[i] for i in valid]

        size = QUOTAS.MAX_RECORDS_PER_WRITE
        with self._write_sem(collection_name):
            for start in range(0, len(ids), size):
                chunk_ids = ids[start : start + size]
                chunk_docs = documents[start : start + size]
                chunk_metas = metadatas[start : start + size]
                if embeddings is not None:
                    col.upsert(
                        ids=chunk_ids,
                        documents=chunk_docs,
                        embeddings=embeddings[start : start + size],
                        metadatas=chunk_metas,
                    )
                else:
                    col.upsert(ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)

    def _delete_batch(self, col, collection_name: str, ids: list[str]) -> None:
        """Split *ids* into ≤300-record chunks and delete each.

        Acquires the per-collection write semaphore for the duration of all chunks.
        """
        size = QUOTAS.MAX_RECORDS_PER_WRITE
        with self._write_sem(collection_name):
            for start in range(0, len(ids), size):
                col.delete(ids=ids[start : start + size])

    # ── Collection access ─────────────────────────────────────────────────────

    def get_or_create_collection(self, name: str) -> chromadb.Collection:
        """Get or create a T3 collection with the appropriate embedding function."""
        from nexus.corpus import validate_collection_name
        validate_collection_name(name)
        return self._client_for(name).get_or_create_collection(
            name, embedding_function=self._embedding_fn(name)
        )

    # ── Write ─────────────────────────────────────────────────────────────────

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
        expires_at: str = "",
    ) -> str:
        """Upsert *content* into *collection*. Returns the document ID.

        *ttl_days* = 0 means permanent (``expires_at=""``).

        *expires_at* may be supplied by the caller (e.g. ``promote_cmd`` when
        carrying over an existing T2 TTL from a known base timestamp).  When
        omitted and *ttl_days* > 0, ``expires_at`` is computed from
        ``datetime.now(UTC)``.

        Note: The document ID is derived from ``collection:title``. Calling put()
        with an empty title will overwrite any previous empty-title document in the
        same collection. Always provide a meaningful title to avoid unintentional
        overwrites.
        """
        doc_id = hashlib.sha256(f"{collection}:{title}".encode()).hexdigest()[:16]
        now_iso = datetime.now(UTC).isoformat()

        if not expires_at:
            if ttl_days > 0:
                expires_at = (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()
            else:
                expires_at = ""

        # Determine whether this collection uses CCE.  When a voyage_api_key
        # is available, CCE collections (docs__, knowledge__, rdr__) are embedded
        # via _cce_embed() so that put()-stored entries are in the same vector
        # space as the CCE-indexed chunks and can be found by search().
        is_cce = (
            bool(self._voyage_api_key)
            and index_model_for_collection(collection) == "voyage-context-3"
        )

        metadata: dict = {
            "title": title,
            "tags": tags,
            "category": category,
            "session_id": session_id,
            "source_agent": source_agent,
            "store_type": store_type,
            "indexed_at": now_iso,
            "expires_at": expires_at,
            "ttl_days": ttl_days,
            "embedding_model": "voyage-context-3" if is_cce else "voyage-4",
        }

        col = self.get_or_create_collection(collection)
        if is_cce:
            vec = self._cce_embed(content)
            self._write_batch(col, collection, [doc_id], [content], [metadata], embeddings=[vec])
        else:
            self._write_batch(col, collection, [doc_id], [content], [metadata])
        return doc_id

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        """Upsert a batch of pre-chunked documents into *collection*.

        Validates each record against ChromaDB Cloud quota limits before any
        network call.  Splits the batch into ≤300-record chunks automatically.
        All metadata fields are passed through verbatim.
        """
        for doc_id, doc, meta in zip(ids, documents, metadatas):
            self._validate_record(id=doc_id, document=doc, embedding=None, metadata=meta)
        col = self.get_or_create_collection(collection)
        self._write_batch(col, collection, ids, documents, metadatas)

    def upsert_chunks_with_embeddings(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
    ) -> None:
        """Upsert chunks with pre-computed embeddings (bypasses ChromaDB's EF).

        Use this when the caller has already obtained embeddings via the Voyage AI
        Contextualized Chunk Embedding (CCE) API — for example, ``voyage-context-3``
        for ``docs__`` and ``knowledge__`` collections — and wishes to store them
        without triggering the collection's own embedding function.

        ChromaDB accepts pre-computed embeddings when ``embeddings=`` is supplied
        to ``col.upsert()``, even when the collection was created with an EF attached.

        Note: Per-record quota validation is intentionally skipped — callers are
        responsible for compliance (source data, e.g. from migration, is presumed valid).
        """
        col = self.get_or_create_collection(collection_name)
        self._write_batch(col, collection_name, ids, documents, metadatas, embeddings=embeddings)

    def update_chunks(
        self,
        collection: str,
        ids: list[str],
        metadatas: list[dict],
    ) -> None:
        """Update chunk metadata without re-embedding.

        Preserves original document text and embedding vectors.
        Use for frecency-only reindex: update frecency_score without
        triggering expensive re-embedding.
        """
        col = self.get_or_create_collection(collection)
        size = QUOTAS.MAX_RECORDS_PER_WRITE
        with self._write_sem(collection):
            for start in range(0, len(ids), size):
                col.update(
                    ids=ids[start : start + size],
                    metadatas=metadatas[start : start + size],
                )

    # ── Read ──────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        collection_names: list[str],
        n_results: int = 10,
        where: dict | None = None,
    ) -> list[dict]:
        """Semantic search over the given collections.

        Each collection is queried with its appropriate embedding model.
        Results are returned sorted by distance (closest first).
        Empty collections are skipped.

        *where* is an optional ChromaDB metadata filter applied to every collection.
        """
        results: list[dict] = []
        for name in collection_names:
            # CCE collections must be queried with voyage-context-3 via
            # contextualized_embed(); using query_texts would invoke the voyage-4
            # EF, producing vectors in an incompatible space (cosine sim ≈ 0.05).
            # Skip CCE path when voyage_api_key is absent (test / offline mode).
            is_cce = (
                bool(self._voyage_api_key)
                and index_model_for_collection(name) == "voyage-context-3"
            )
            try:
                if is_cce:
                    col = self._client_for(name).get_collection(name)
                else:
                    col = self._client_for(name).get_collection(
                        name, embedding_function=self._embedding_fn(name)
                    )
            except _ChromaNotFoundError:
                continue  # collection doesn't exist, skip it
            count = col.count()
            if count == 0:
                continue
            actual_n = min(n_results, count, QUOTAS.MAX_QUERY_RESULTS)
            if n_results > QUOTAS.MAX_QUERY_RESULTS:
                _log.warning(
                    "search_n_results_clamped",
                    requested=n_results,
                    actual=actual_n,
                    collection=name,
                )
            if is_cce:
                query_kwargs: dict = {
                    "query_embeddings": [self._cce_embed(query, input_type="query")],
                    "n_results": actual_n,
                    "include": ["documents", "metadatas", "distances"],
                }
            else:
                query_kwargs = {
                    "query_texts": [query],
                    "n_results": actual_n,
                    "include": ["documents", "metadatas", "distances"],
                }
            if where is not None:
                query_kwargs["where"] = where
            qr = col.query(**query_kwargs)
            for doc_id, doc, meta, dist in zip(
                qr["ids"][0],
                qr["documents"][0],
                qr["metadatas"][0],
                qr["distances"][0],
            ):
                results.append({"id": doc_id, "content": doc, "distance": dist, **meta})
        return sorted(results, key=lambda r: r["distance"])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def expire(self) -> int:
        """Delete all expired entries from ``knowledge__*`` collections.

        Only queries the knowledge store — TTL-managed entries are written
        via ``nx store put`` which routes to the knowledge store.

        **Precondition**: the ``{base}_knowledge`` ChromaDB Cloud database must
        exist before calling this method.  If the database has not yet been
        created (e.g. the user upgraded but has not run ``nx migrate t3``),
        the method logs a warning and returns 0 rather than raising.

        Only removes entries where ``ttl_days > 0`` AND ``expires_at != ""``
        AND ``expires_at < now``. Permanent entries (``ttl_days=0``,
        ``expires_at=""``) are always preserved.

        Returns the total number of deleted documents.
        """
        # ChromaDB only supports numeric $lt/$gt, so we filter by ttl_days > 0
        # (int comparison) then check expires_at in Python (ISO 8601 strings are
        # lexicographically ordered, so string comparison is correct).
        now_iso = datetime.now(UTC).isoformat()
        ttl_where: dict = {"ttl_days": {"$gt": 0}}
        total = 0
        kc = self._clients["knowledge"]
        try:
            collections = kc.list_collections()
        except _ChromaNotFoundError:
            _log.warning("expire_skipped_knowledge_store_not_found")
            return 0
        for col_or_name in collections:
            name = col_or_name if isinstance(col_or_name, str) else col_or_name.name
            if not name.startswith("knowledge__"):
                continue
            col = kc.get_collection(name)
            # Paginated accumulation: gather all expired IDs before deleting.
            # A single col.get() without limit silently truncates beyond 300
            # (ChromaDB Cloud hard limit).  Pagination stops when the returned
            # page is shorter than the limit (i.e. it was the last page).
            expired_ids: list[str] = []
            offset = 0
            page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
            while True:
                result = col.get(
                    where=ttl_where,
                    include=["metadatas"],
                    limit=page_limit,
                    offset=offset,
                )
                page_ids = result["ids"]
                for doc_id, meta in zip(page_ids, result["metadatas"]):
                    if meta.get("expires_at", "") and meta["expires_at"] < now_iso:
                        expired_ids.append(doc_id)
                offset += len(page_ids)
                if len(page_ids) < page_limit:
                    break  # last page (short or empty)
            if expired_ids:
                self._delete_batch(col, name, expired_ids)
            total += len(expired_ids)
        return total

    # ── Collection management ─────────────────────────────────────────────────

    def list_store(self, collection: str, limit: int = 200) -> list[dict]:
        """Return metadata for entries in a single knowledge__ collection.

        Each entry is a dict with at minimum ``id``, ``title``, ``tags``,
        ``ttl_days``, ``expires_at``, and ``indexed_at``.  Returns an empty
        list when the collection does not exist.
        """
        try:
            col = self._client_for(collection).get_collection(collection)
        except _ChromaNotFoundError:
            return []
        clamped = min(limit, QUOTAS.MAX_QUERY_RESULTS)
        with self._read_sem(collection):
            result = col.get(include=["metadatas"], limit=clamped)
        return [
            {"id": doc_id, **meta}
            for doc_id, meta in zip(result["ids"], result["metadatas"])
        ]

    def list_collections(self) -> list[dict]:
        """Return all T3 collections with their document counts.

        Fans out across all four store clients, deduplicates by collection
        name (relevant when all four clients are the same mock in tests), and
        parallelizes count queries up to 8 concurrent requests.

        Note: The enumeration phase makes four sequential HTTP calls (one per
        store client) before the parallel count phase begins.  This is
        acceptable because ``list_collections`` is only called from
        non-hot-path commands (``nx collections``, ``nx store list``).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        seen: set[str] = set()
        names: list[str] = []
        for client in self._clients.values():
            try:
                for col_or_name in client.list_collections():
                    n = col_or_name if isinstance(col_or_name, str) else col_or_name.name
                    if n not in seen:
                        names.append(n)
                        seen.add(n)
            except _ChromaNotFoundError:
                continue  # store not yet created, skip gracefully

        if not names:
            return []

        def _count(name: str) -> dict:
            col = self._client_for(name).get_collection(name)
            return {"name": name, "count": col.count()}

        result: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
            futures = {pool.submit(_count, n): n for n in names}
            for future in as_completed(futures):
                try:
                    result.append(future.result())
                except Exception as exc:
                    name = futures[future]
                    _log.warning("list_collections_count_failed", collection=name, error=str(exc))
        return sorted(result, key=lambda r: r["name"])

    def collection_exists(self, name: str) -> bool:
        """Return True if the collection already exists in T3 (no create side-effect)."""
        try:
            self._client_for(name).get_collection(name)
            return True
        except _ChromaNotFoundError:
            return False

    def delete_collection(self, name: str) -> None:
        """Delete a T3 collection entirely."""
        self._client_for(name).delete_collection(name)

    def delete_by_source(self, collection_name: str, source_path: str) -> int:
        """Delete all chunks for a given source path. Returns count deleted.

        Uses paginated ``col.get()`` to avoid the ChromaDB Cloud 300-record
        truncation limit.  Same short-page termination pattern as ``expire()``.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return 0
        ids: list[str] = []
        offset = 0
        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        while True:
            result = col.get(
                where={"source_path": source_path},
                include=[],
                limit=page_limit,
                offset=offset,
            )
            page_ids = result["ids"]
            ids.extend(page_ids)
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break  # last page (short or empty)
        if ids:
            self._delete_batch(col, collection_name, ids)
        return len(ids)

    def collection_info(self, name: str) -> dict:
        """Return metadata for a collection (count, metadata dict).

        Raises KeyError if the collection does not exist.
        """
        try:
            col = self._client_for(name).get_collection(name)
        except _ChromaNotFoundError:
            raise KeyError(f"Collection not found: {name!r}") from None
        return {"count": col.count(), "metadata": col.metadata or {}}

    def collection_metadata(self, collection_name: str) -> dict:
        """Return metadata dict for a collection.

        Keys returned: ``name``, ``count``, ``embedding_model`` (query-time model),
        ``index_model`` (index-time model, may differ for CCE collections).

        Raises KeyError if the collection does not exist.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            raise KeyError(f"Collection not found: {collection_name!r}") from None
        return {
            "name": collection_name,
            "count": col.count(),
            "embedding_model": embedding_model_for_collection(collection_name),
            "index_model": index_model_for_collection(collection_name),
        }
