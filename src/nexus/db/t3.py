# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Literal

import chromadb
import chromadb.errors
import httpx
from chromadb.errors import NotFoundError as _ChromaNotFoundError
import structlog

try:
    import voyageai
except Exception:  # Pydantic v1 crashes on Python ≥ 3.14
    voyageai = None  # type: ignore[assignment]

from nexus.config import get_credential
from nexus.corpus import embedding_model_for_collection, index_model_for_collection
from nexus.db.chroma_quotas import QUOTAS, QuotaValidator

_log = structlog.get_logger(__name__)


class OldLayoutDetected(RuntimeError):
    """Raised when the old four-database ChromaDB layout is detected during init."""


# Deprecated: no internal callers remain after RDR-037. Kept for one release
# cycle in case external scripts import it. Will be removed in next major version.
_STORE_TYPES: tuple[str, ...] = ("code", "docs", "rdr", "knowledge")

from nexus.retry import (
    _chroma_with_retry,
    _is_retryable_chroma_error,
    _is_retryable_voyage_error,
    _voyage_with_retry,
)

class T3Database:
    """T3 ChromaDB permanent knowledge store.

    Supports two modes:
    - **Cloud mode** (default): ``chromadb.CloudClient`` connected to the configured
      ChromaDB Cloud database.
    - **Local mode** (``local_mode=True``): ``chromadb.PersistentClient`` using a
      local directory — zero API keys required.

    On first cloud connection (no ``NX_MIGRATED`` flag), the constructor probes for
    the old four-database layout by attempting to connect to ``{base}_code``.
    If that database exists, ``OldLayoutDetected`` is raised.

    The ``_client`` and ``_ef_override`` keyword arguments are injection
    points for testing — pass an ``EphemeralClient`` and
    ``DefaultEmbeddingFunction`` to run the full code path without any API
    keys.
    """

    def __init__(
        self,
        tenant: str = "",
        database: str = "",
        api_key: str = "",
        voyage_api_key: str = "",
        *,
        local_mode: bool = False,
        local_path: str = "",
        read_timeout_seconds: float = 120.0,
        _client=None,
        _ef_override=None,
    ) -> None:
        self._local_mode = local_mode
        self._voyage_api_key = voyage_api_key
        self._ef_override = _ef_override
        self._ef_cache: dict[str, object] = {}
        self._ef_lock = threading.Lock()
        self._write_sems: dict[str, threading.BoundedSemaphore] = {}
        self._read_sems: dict[str, threading.BoundedSemaphore] = {}
        self._sems_lock = threading.Lock()
        self._quota_validator = QuotaValidator()

        # ── Local mode: PersistentClient, no cloud, no Voyage ────────────
        if local_mode:
            from pathlib import Path
            p = Path(local_path)
            p.mkdir(parents=True, exist_ok=True)
            if _client is not None:
                self._client = _client
            else:
                self._client = chromadb.PersistentClient(path=str(p))
            self._voyage_client = None
            return

        # ── Cloud mode ───────────────────────────────────────────────────
        if voyageai is None:
            raise ImportError(
                "voyageai is required for cloud mode but is not installed. "
                "Install with: uv tool install conexus --with 'conexus[cloud]' --force"
            )
        self._voyage_client: voyageai.Client | None = (
            voyageai.Client(api_key=voyage_api_key, timeout=read_timeout_seconds, max_retries=3)
            if voyage_api_key else None
        )
        if _client is not None:
            self._client = _client
        else:
            migrated = get_credential("migrated")
            if migrated:
                self._client = chromadb.CloudClient(
                    tenant=tenant or None, database=database, api_key=api_key
                )
            else:
                try:
                    chromadb.CloudClient(
                        tenant=tenant or None, database=f"{database}_code", api_key=api_key
                    )
                except _ChromaNotFoundError:
                    # Old layout absent — connect to single database
                    self._client = chromadb.CloudClient(
                        tenant=tenant or None, database=database, api_key=api_key
                    )
                except Exception as probe_exc:
                    # Auth errors, network errors, etc. during probe — wrap so
                    # CLI callers (except RuntimeError) surface a clean message.
                    raise RuntimeError(
                        f"Failed to connect to ChromaDB Cloud (probe for '{database}_code').\n"
                        f"Check CHROMA_API_KEY and network connectivity."
                    ) from probe_exc
                else:
                    _log.warning(
                        "old_layout_detected",
                        database=database,
                        msg="Old four-database layout detected. Set NX_MIGRATED=1 after migration.",
                    )
                    raise OldLayoutDetected(
                        f"Old four-database layout detected: '{database}_code' exists.\n"
                        f"Export data with the pre-upgrade version first, then set "
                        f"NX_MIGRATED=1 or run 'nx config set migrated 1'."
                    )

    # ── Context manager (no-op: CloudClient is stateless REST) ───────────────

    def __enter__(self) -> "T3Database":
        return self

    def __exit__(self, *_) -> None:
        pass  # ChromaDB CloudClient is HTTP-based; no persistent connection to close.

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _client_for(self, collection_name: str) -> object:
        """Return the ChromaDB client (single-database; routing removed per RDR-037)."""
        return self._client

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
        result = _voyage_with_retry(
            self._voyage_client.contextualized_embed,
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
                    _chroma_with_retry(
                        col.upsert,
                        ids=chunk_ids,
                        documents=chunk_docs,
                        embeddings=embeddings[start : start + size],
                        metadatas=chunk_metas,
                    )
                else:
                    _chroma_with_retry(col.upsert, ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)

    def _delete_batch(self, col, collection_name: str, ids: list[str]) -> None:
        """Split *ids* into ≤300-record chunks and delete each.

        Acquires the per-collection write semaphore for the duration of all chunks.
        """
        size = QUOTAS.MAX_RECORDS_PER_WRITE
        with self._write_sem(collection_name):
            for start in range(0, len(ids), size):
                _chroma_with_retry(col.delete, ids=ids[start : start + size])

    # ── Collection access ─────────────────────────────────────────────────────

    def get_or_create_collection(self, name: str) -> chromadb.Collection:
        """Get or create a T3 collection with the appropriate embedding function."""
        from nexus.corpus import validate_collection_name
        validate_collection_name(name)
        return _chroma_with_retry(
            self._client_for(name).get_or_create_collection,
            name, embedding_function=self._embedding_fn(name),
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
        # is available and we're not in local mode, CCE collections (docs__,
        # knowledge__, rdr__) are embedded via _cce_embed() so that put()-stored
        # entries are in the same vector space as the CCE-indexed chunks.
        is_cce = (
            not self._local_mode
            and bool(self._voyage_api_key)
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
                _chroma_with_retry(
                    col.update,
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
            # Skip CCE path in local mode or when voyage_api_key is absent.
            is_cce = (
                not self._local_mode
                and bool(self._voyage_api_key)
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
            count = _chroma_with_retry(col.count)
            if count == 0:
                continue
            if self._local_mode:
                actual_n = min(n_results, count)
            else:
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
            qr = _chroma_with_retry(col.query, **query_kwargs)
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
        kc = self._client
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
                result = _chroma_with_retry(
                    col.get,
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
        clamped = limit if self._local_mode else min(limit, QUOTAS.MAX_QUERY_RESULTS)
        with self._read_sem(collection):
            result = _chroma_with_retry(col.get, include=["metadatas"], limit=clamped)
        return [
            {"id": doc_id, **meta}
            for doc_id, meta in zip(result["ids"], result["metadatas"])
        ]

    def list_collections(self) -> list[dict]:
        """Return all T3 collections with their document counts.

        Queries the single ChromaDB client and parallelizes count queries
        up to 8 concurrent requests.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            raw = self._client.list_collections()
        except _ChromaNotFoundError:
            return []

        names = [c if isinstance(c, str) else c.name for c in raw]
        if not names:
            return []

        def _count(name: str) -> dict:
            col = self._client.get_collection(name)
            return {"name": name, "count": _chroma_with_retry(col.count)}

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
            result = _chroma_with_retry(
                col.get,
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

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        """Retrieve a single entry by its exact document ID.

        Returns a dict with ``id``, ``content``, and all metadata fields,
        or ``None`` if the entry does not exist.
        """
        try:
            col = self._client_for(collection).get_collection(collection)
        except _ChromaNotFoundError:
            return None
        with self._read_sem(collection):
            result = _chroma_with_retry(
                col.get, ids=[doc_id], include=["documents", "metadatas"]
            )
        if not result["ids"]:
            return None
        return {
            "id": result["ids"][0],
            "content": result["documents"][0],
            **result["metadatas"][0],
        }

    def delete_by_id(self, collection: str, doc_id: str) -> bool:
        """Delete a single entry by its exact document ID. Returns True if found and deleted."""
        try:
            col = self._client_for(collection).get_collection(collection)
        except _ChromaNotFoundError:
            return False
        result = _chroma_with_retry(col.get, ids=[doc_id], include=[])
        if not result["ids"]:
            return False
        _chroma_with_retry(col.delete, ids=[doc_id])
        return True

    def find_ids_by_title(self, collection: str, title: str) -> list[str]:
        """Return all document IDs whose title metadata exactly matches *title*.

        Uses paginated ``col.get()`` to avoid the ChromaDB Cloud 300-record
        truncation limit.
        """
        try:
            col = self._client_for(collection).get_collection(collection)
        except _ChromaNotFoundError:
            return []
        ids: list[str] = []
        offset = 0
        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        while True:
            result = _chroma_with_retry(
                col.get,
                where={"title": title},
                include=[],
                limit=page_limit,
                offset=offset,
            )
            page_ids = result["ids"]
            ids.extend(page_ids)
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break
        return ids

    def batch_delete(self, collection: str, ids: list[str]) -> None:
        """Delete *ids* from *collection* in write-semaphore-bounded batches."""
        if not ids:
            return
        try:
            col = self._client_for(collection).get_collection(collection)
        except _ChromaNotFoundError:
            return
        self._delete_batch(col, collection, ids)

    def collection_info(self, name: str) -> dict:
        """Return metadata for a collection (count, metadata dict).

        Raises KeyError if the collection does not exist.
        """
        try:
            col = self._client_for(name).get_collection(name)
        except _ChromaNotFoundError:
            raise KeyError(f"Collection not found: {name!r}") from None
        return {"count": _chroma_with_retry(col.count), "metadata": col.metadata or {}}

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
            "count": _chroma_with_retry(col.count),
            "embedding_model": embedding_model_for_collection(collection_name),
            "index_model": index_model_for_collection(collection_name),
        }
