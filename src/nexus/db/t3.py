# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta

import chromadb

from nexus.corpus import embedding_model_for_collection


class T3Database:
    """T3 ChromaDB CloudClient permanent knowledge store.

    Uses ``chromadb.CloudClient`` + ``VoyageAIEmbeddingFunction`` (voyage-code-3
    for ``code__*`` collections; voyage-4 for all others).

    Each collection is namespaced by type: ``code__{repo}``, ``docs__{corpus}``,
    ``knowledge__{topic}``.

    The ``_client`` and ``_ef_override`` keyword arguments are injection points
    for testing — pass an ``EphemeralClient`` and ``DefaultEmbeddingFunction``
    to run the full code path without any API keys.
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
        if _client is not None:
            self._client = _client
        else:
            self._client = chromadb.CloudClient(
                tenant=tenant, database=database, api_key=api_key
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _embedding_fn(self, collection_name: str):
        if self._ef_override is not None:
            return self._ef_override
        model = embedding_model_for_collection(collection_name)
        return chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction(
            model_name=model, api_key=self._voyage_api_key
        )

    # ── Collection access ─────────────────────────────────────────────────────

    def get_or_create_collection(self, name: str) -> chromadb.Collection:
        """Get or create a T3 collection with the appropriate embedding function."""
        return self._client.get_or_create_collection(
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
    ) -> str:
        """Upsert *content* into *collection*. Returns the document ID.

        *ttl_days* = 0 means permanent (``expires_at=""``).
        """
        doc_id = hashlib.sha256(f"{collection}:{title}".encode()).hexdigest()[:16]
        now_iso = datetime.now(UTC).isoformat()

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
        }

        col = self.get_or_create_collection(collection)
        col.upsert(ids=[doc_id], documents=[content], metadatas=[metadata])
        return doc_id

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
            col = self.get_or_create_collection(name)
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
        for col_or_name in self._client.list_collections():
            name = col_or_name if isinstance(col_or_name, str) else col_or_name.name
            if not name.startswith("knowledge__"):
                continue
            col = self._client.get_collection(name)
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

    def list_collections(self) -> list[dict]:
        """Return all T3 collections with their document counts.

        Note: makes N+1 API calls (1 list + 1 count per collection).
        Optimize if the ChromaDB CloudClient exposes batched counts.
        """
        result: list[dict] = []
        for col_or_name in self._client.list_collections():
            name = col_or_name if isinstance(col_or_name, str) else col_or_name.name
            col = self._client.get_collection(name)
            result.append({"name": name, "count": col.count()})
        return result

    def delete_collection(self, name: str) -> None:
        """Delete a T3 collection entirely."""
        self._client.delete_collection(name)
