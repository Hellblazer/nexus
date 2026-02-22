# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import chromadb

from nexus.corpus import embedding_model_for_collection


class T3Database:
    """T3 ChromaDB CloudClient permanent knowledge store.

    Uses ``chromadb.CloudClient`` + ``VoyageAIEmbeddingFunction`` (voyage-code-3
    for ``code__*`` collections; voyage-4 for all others).

    Each collection is namespaced by type: ``code__{repo}``, ``docs__{corpus}``,
    ``knowledge__{topic}``.
    """

    def __init__(
        self,
        tenant: str,
        database: str,
        api_key: str,
        voyage_api_key: str = "",
    ) -> None:
        self._voyage_api_key = voyage_api_key
        self._client = chromadb.CloudClient(
            tenant=tenant, database=database, api_key=api_key
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _embedding_fn(self, collection_name: str) -> chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction:
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
        doc_id = str(uuid4())
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
        self, query: str, collection_names: list[str], n_results: int = 10
    ) -> list[dict]:
        """Semantic search over the given collections.

        Each collection is queried with its appropriate embedding model.
        Results are returned sorted by distance (closest first).
        Empty collections are skipped.
        """
        results: list[dict] = []
        for name in collection_names:
            col = self.get_or_create_collection(name)
            count = col.count()
            if count == 0:
                continue
            actual_n = min(n_results, count)
            qr = col.query(
                query_texts=[query],
                n_results=actual_n,
                include=["documents", "metadatas", "distances"],
            )
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
        now_iso = datetime.now(UTC).isoformat()
        where: dict = {
            "$and": [
                {"ttl_days": {"$gt": 0}},
                {"expires_at": {"$ne": ""}},
                {"expires_at": {"$lt": now_iso}},
            ]
        }
        total = 0
        for name in self._client.list_collections():
            if not name.startswith("knowledge__"):
                continue
            col = self._client.get_collection(name)
            result = col.get(where=where)
            ids = result["ids"]
            if ids:
                col.delete(ids=ids)
            total += len(ids)
        return total

    # ── Collection management ─────────────────────────────────────────────────

    def list_collections(self) -> list[dict]:
        """Return all T3 collections with their document counts.

        Note: makes N+1 API calls (1 list + 1 count per collection).
        Optimize if the ChromaDB CloudClient exposes batched counts.
        """
        result: list[dict] = []
        for name in self._client.list_collections():
            col = self._client.get_collection(name)
            result.append({"name": name, "count": col.count()})
        return result

    def delete_collection(self, name: str) -> None:
        """Delete a T3 collection entirely."""
        self._client.delete_collection(name)
