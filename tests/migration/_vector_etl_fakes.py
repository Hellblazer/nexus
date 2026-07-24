# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared vector-ETL test fakes (RDR-155 P4b salvage).

Extracted verbatim from the retired ``tests/migration/test_vector_etl.py``
(deleted with the migration machinery) — the surviving pg-source reconcile
suite (``test_vector_etl_pg_source.py``) still needs the FakeVectorClient
surface + collection-naming helper.
"""
from __future__ import annotations

import hashlib

# ── embeds on the service's bundled ONNX fallback — no cloud credentials) ──

_MODEL_384 = "minilm-l6-v2-384"
_MODEL_768 = "bge-base-en-v15-768"


def _coll(owner: str, *, model: str = _MODEL_384, version: int = 1) -> str:
    return f"knowledge__{owner}__{model}__v{version}"


def _chash(text: str) -> str:
    """Chunk natural ID: the FULL sha256(text) hexdigest (RDR-180; the
    repo-wide chash convention post-truncation-retirement)."""
    return hashlib.sha256(text.encode()).hexdigest()


# ── Fake vector client (HttpVectorClient surface subset) ─────────────────────


class _FakeCollectionHandle:
    """Collection-handle stub mirroring ``_ServiceCollectionStub``."""

    def __init__(self, client: "FakeVectorClient", name: str) -> None:
        self._client = client
        self._name = name

    def delete(self, ids: list[str]) -> None:
        col = self._client.store.get(self._name, {})
        for chunk_id in ids:
            col.pop(chunk_id, None)

    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict:
        col = self._client.store.get(self._name, {})
        keys = [i for i in (ids or list(col)) if i in col]
        return {
            "ids": keys,
            "documents": [col[k][0] for k in keys],
            "metadatas": [col[k][1] for k in keys],
        }


class FakeVectorClient:
    """Hermetic stand-in for ``HttpVectorClient`` (same surface subset).

    ``upsert_chunks`` accepts the optional ``embeddings`` kwarg and RECORDS it
    (``upsert_embeddings``): post-nexus-hxry2, source vectors cross the ETL ONLY
    for the same-model voyage passthrough; every other path leaves it None. Tests
    assert the recorded value to pin which path ran.

    ``count_delta`` simulates a lossy target (service wrote fewer rows than
    sent) so the ETL's post-write count verification can be proven
    non-vacuous.
    """

    def __init__(self, *, count_delta: dict[str, int] | None = None) -> None:
        # collection -> {chash: (document, metadata)}
        self.store: dict[str, dict[str, tuple[str, dict]]] = {}
        # (collection, [ids]) per upsert call, in call order
        self.upsert_calls: list[tuple[str, list[str]]] = []
        # embeddings arg per upsert call, in call order (None = re-embed path)
        self.upsert_embeddings: list[list[list[float]] | None] = []
        self._count_delta = count_delta or {}

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict] | None = None,
        *,
        embeddings: list[list[float]] | None = None,
    ) -> None:
        metas = metadatas or [{}] * len(ids)
        self.upsert_calls.append((collection, list(ids)))
        self.upsert_embeddings.append(embeddings)
        col = self.store.setdefault(collection, {})
        for chunk_id, doc, meta in zip(ids, documents, metas):
            col[chunk_id] = (doc, dict(meta or {}))

    def count(self, collection: str) -> int:
        return len(self.store.get(collection, {})) + self._count_delta.get(
            collection, 0
        )

    def existing_ids(self, collection: str, ids: list[str]) -> set[str]:
        """Membership probe (mirrors ``HttpVectorClient.existing_ids``):
        the subset of *ids* actually present in *collection*."""
        col = self.store.get(collection, {})
        return {i for i in ids if i in col}

    def list_collections(self) -> list[dict]:
        return [
            {"name": name, "count": len(col)}
            for name, col in sorted(self.store.items())
            if col
        ]

    def collection_exists(self, name: str) -> bool:
        return bool(self.store.get(name))

    def delete_by_id(self, collection: str, doc_id: str) -> bool:
        col = self.store.get(collection, {})
        return col.pop(doc_id, None) is not None

    def get_collection(self, name: str) -> _FakeCollectionHandle:
        from chromadb.errors import NotFoundError

        if name not in self.store:
            raise NotFoundError(f"collection {name!r} not found")
        return _FakeCollectionHandle(self, name)

    def get_or_create_collection(self, name: str) -> _FakeCollectionHandle:
        self.store.setdefault(name, {})
        return _FakeCollectionHandle(self, name)
