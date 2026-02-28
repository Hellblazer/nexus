# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta

import chromadb
import chromadb.errors
from chromadb.errors import NotFoundError as _ChromaNotFoundError
import structlog

from nexus.corpus import embedding_model_for_collection, index_model_for_collection

_log = structlog.get_logger(__name__)

# The four ChromaDB Cloud databases, one per content type.
# ``chroma_database`` in config is the *base name*; each store is
# ``{base}_{type}``.  Example: base="nexus" → nexus_code, nexus_docs,
# nexus_rdr, nexus_knowledge.
_STORE_TYPES: tuple[str, ...] = ("code", "docs", "rdr", "knowledge")


class T3Database:
    """T3 ChromaDB CloudClient permanent knowledge store.

    Uses four separate ``chromadb.CloudClient`` instances — one per content
    type — derived from the ``chroma_database`` base name:

    - ``{base}_code``      for ``code__*`` collections
    - ``{base}_docs``      for ``docs__*`` collections
    - ``{base}_rdr``       for ``rdr__*`` collections
    - ``{base}_knowledge`` for ``knowledge__*`` collections and fallback

    All routing is internal to this class.  Every public caller
    (``search_cmd``, ``indexer``, ``pm``, etc.) remains unchanged.

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
        if _client is not None:
            # Test injection: single client serves all store types.
            self._clients: dict[str, object] = {t: _client for t in _STORE_TYPES}
        else:
            _clients: dict[str, object] = {}
            for t in _STORE_TYPES:
                db_name = f"{database}_{t}"
                try:
                    _clients[t] = chromadb.CloudClient(
                        tenant=tenant, database=db_name, api_key=api_key
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to connect to ChromaDB Cloud database {db_name!r}: {exc}\n"
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
        prefix = collection_name.split("__")[0] if "__" in collection_name else "knowledge"
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
        if self._ef_override is not None:
            return self._ef_override
        with self._ef_lock:
            if collection_name not in self._ef_cache:
                model = embedding_model_for_collection(collection_name)
                self._ef_cache[collection_name] = (
                    chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction(
                        model_name=model, api_key=self._voyage_api_key
                    )
                )
            return self._ef_cache[collection_name]

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
            # voyage-4 is the query-time model for all collection types; nx store put
            # intentionally uses it (via the collection EF) rather than CCE because
            # agent-stored knowledge chunks are typically single entries (CCE requires 2+).
            "embedding_model": embedding_model_for_collection(collection),
        }

        col = self.get_or_create_collection(collection)
        col.upsert(ids=[doc_id], documents=[content], metadatas=[metadata])
        return doc_id

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        """Upsert a batch of pre-chunked documents into *collection*.

        All metadata fields are passed through verbatim — nothing is added,
        removed, or truncated.  This preserves any atomicity semantics
        (delete-then-add) applied by the caller and passes through the full
        metadata schema emitted by the indexing pipeline.
        """
        col = self.get_or_create_collection(collection)
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)

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
        """
        col = self.get_or_create_collection(collection_name)
        col.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

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
        col.update(ids=ids, metadatas=metadatas)

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
            try:
                col = self._client_for(name).get_collection(
                    name, embedding_function=self._embedding_fn(name)
                )
            except _ChromaNotFoundError:
                continue  # collection doesn't exist, skip it
            count = col.count()
            if count == 0:
                continue
            actual_n = min(n_results, count)
            query_kwargs: dict = {
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
            result = col.get(where=ttl_where, include=["metadatas"])
            expired_ids = [
                doc_id
                for doc_id, meta in zip(result["ids"], result["metadatas"])
                if meta.get("expires_at", "") and meta["expires_at"] < now_iso
            ]
            if expired_ids:
                col.delete(ids=expired_ids)
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
        result = col.get(include=["metadatas"], limit=limit)
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
                result.append(future.result())
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
        """Delete all chunks for a given source path. Returns count deleted."""
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return 0
        existing = col.get(where={"source_path": source_path}, include=[])
        ids = existing["ids"]
        if ids:
            col.delete(ids=ids)
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
