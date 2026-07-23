# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""In-process, in-memory vector store — the dependency-free successor to
``chromadb.EphemeralClient`` for every ephemeral consumer (RDR-155 P4b
Phase 0a; Hal decision 1, 2026-07-23).

Consumers: the test-suite substrate (``tests/conftest``), the plan-match
session cache, ``nx index --dry-run``, and (pending the ephemeral-usage
answer) the T1-isolated leg. NOT a storage engine: nothing persists, by
contract — persistent data lives in PG via the engine, in every mode.

Semantics are pinned by ``tests/test_vector_substrate_contract.py`` (the
differential harness): this store and chromadb run the SAME contract
suite until the dependency leaves at P3, after which the suite is this
store's permanent conformance pin. Deliberate properties:

* **Cosine, only.** Production always pinned ``hnsw:space=cosine`` over
  chroma's L2 default; this store simply *is* cosine — no metric config
  to get wrong. Distance = 1 - cosine_similarity (identical=0,
  orthogonal=1, opposite=2), brute-force over normalized vectors — at
  ephemeral scale (tens to low thousands of rows) brute force beats an
  index on every axis including correctness.
* **Real instance isolation.** Unlike ``EphemeralClient`` (whose
  ``SharedSystemClient`` shares process state by settings hash — the
  documented gotcha), two ``InMemoryVectorClient`` instances share
  nothing.
* **The where-grammar is the USED subset, fail-loud.** Implicit ``$eq``,
  explicit ``$eq``/``$ne``/``$in``, comparisons
  ``$gt``/``$gte``/``$lt``/``$lte``, and ``$and``/``$or`` — anything
  else raises ``ValueError`` rather than silently half-matching.
"""
from __future__ import annotations

import math
import threading
from typing import Any, Callable

__all__ = ["InMemoryVectorClient", "InMemoryCollection"]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return list(vec)
    return [v / norm for v in vec]


def _cosine_distance(a_normed: list[float], b_normed: list[float]) -> float:
    # Clamp to the documented [0, 2] cosine-distance range: float32 EF
    # vectors can dot to 1.0000001 on exact matches, and a -1.2e-07
    # distance trips downstream >= 0.0 assertions (verify-deep probe).
    d = 1.0 - sum(x * y for x, y in zip(a_normed, b_normed, strict=True))
    return min(2.0, max(0.0, d))


def _matches(meta: dict[str, Any], where: dict[str, Any]) -> bool:
    """The Mongo-style subset the consumers use; unknown operators raise."""
    for key, cond in where.items():
        if key == "$and":
            if not all(_matches(meta, sub) for sub in cond):
                return False
        elif key == "$or":
            if not any(_matches(meta, sub) for sub in cond):
                return False
        elif key.startswith("$"):
            raise ValueError(
                f"unsupported where operator {key!r} — the in-memory store "
                "implements the used subset ($eq/$ne/$in/$and/$or) and fails "
                "loud on anything else (P4b contract)"
            )
        elif isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$eq":
                    if meta.get(key) != operand:
                        return False
                elif op == "$ne":
                    if meta.get(key) == operand:
                        return False
                elif op == "$in":
                    if meta.get(key) not in operand:
                        return False
                elif op in ("$gt", "$gte", "$lt", "$lte"):
                    # Oracle-verified: a row missing the field never
                    # matches a comparison operator.
                    value = meta.get(key)
                    if value is None:
                        return False
                    if op == "$gt" and not value > operand:
                        return False
                    if op == "$gte" and not value >= operand:
                        return False
                    if op == "$lt" and not value < operand:
                        return False
                    if op == "$lte" and not value <= operand:
                        return False
                else:
                    raise ValueError(
                        f"unsupported where operator {op!r} on field {key!r}"
                    )
        else:
            if meta.get(key) != cond:  # implicit $eq
                return False
    return True


class _Row:
    __slots__ = ("id", "document", "metadata", "embedding_normed")

    def __init__(self, id_: str, document: str | None,
                 metadata: dict[str, Any], embedding_normed: list[float]):
        self.id = id_
        self.document = document
        self.metadata = metadata
        self.embedding_normed = embedding_normed


class InMemoryCollection:
    """Chroma-shaped collection over a dict — the consumed API subset only."""

    def __init__(self, name: str, *, embedding_function: Any = None,
                 metadata: dict[str, Any] | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}
        self._ef = embedding_function
        self._rows: dict[str, _Row] = {}
        self._order: list[str] = []  # insertion order for get() paging
        self._lock = threading.Lock()

    # ── embedding resolution ────────────────────────────────────────────

    def _embed_documents(self, documents: list[str]) -> list[list[float]]:
        if self._ef is None:
            raise ValueError(
                f"collection {self.name!r} has no embedding function; pass "
                "embeddings explicitly"
            )
        return self._ef(input=documents)

    def _embed_query(self, texts: list[str]) -> list[list[float]]:
        if self._ef is None:
            raise ValueError(
                f"collection {self.name!r} has no embedding function; pass "
                "query_embeddings explicitly"
            )
        embed_query: Callable | None = getattr(self._ef, "embed_query", None)
        if callable(embed_query):
            return embed_query(input=texts)
        return self._ef(input=texts)

    # ── writes ──────────────────────────────────────────────────────────

    def add(self, *, ids: list[str], embeddings: list[list[float]] | None = None,
            documents: list[str] | None = None,
            metadatas: list[dict[str, Any]] | None = None) -> None:
        self.upsert(ids=ids, embeddings=embeddings, documents=documents,
                    metadatas=metadatas)

    def upsert(self, *, ids: list[str],
               embeddings: list[list[float]] | None = None,
               documents: list[str] | None = None,
               metadatas: list[dict[str, Any]] | None = None) -> None:
        if embeddings is None:
            if documents is None:
                raise ValueError("upsert needs embeddings or documents")
            embeddings = self._embed_documents(documents)
        docs = documents if documents is not None else [None] * len(ids)
        metas = metadatas if metadatas is not None else [{}] * len(ids)
        if not (len(ids) == len(embeddings) == len(docs) == len(metas)):
            raise ValueError("ids/embeddings/documents/metadatas length mismatch")
        with self._lock:
            for id_, emb, doc, meta in zip(ids, embeddings, docs, metas,
                                           strict=True):
                if id_ not in self._rows:
                    self._order.append(id_)
                self._rows[id_] = _Row(id_, doc, dict(meta or {}),
                                       _normalize(list(emb)))

    def update(self, *, ids: list[str],
               embeddings: list[list[float]] | None = None,
               documents: list[str] | None = None,
               metadatas: list[dict[str, Any]] | None = None) -> None:
        """Chroma-parity partial update (differentially verified):
        unknown ids are silently skipped; metadata MERGES at key level;
        unsupplied fields are preserved; a document update without
        explicit embeddings re-embeds via the collection EF."""
        if embeddings is None and documents is not None:
            embeddings = self._embed_documents(documents)
        with self._lock:
            for i, id_ in enumerate(ids):
                row = self._rows.get(id_)
                if row is None:
                    continue
                if embeddings is not None:
                    row.embedding_normed = _normalize(list(embeddings[i]))
                if documents is not None:
                    row.document = documents[i]
                if metadatas is not None:
                    row.metadata.update(metadatas[i])

    def delete(self, *, ids: list[str] | None = None,
               where: dict[str, Any] | None = None) -> None:
        with self._lock:
            if ids is not None:
                victims = [i for i in ids if i in self._rows]
            elif where is not None:
                victims = [r.id for r in self._rows.values()
                           if _matches(r.metadata, where)]
            else:
                raise ValueError("delete needs ids or where")
            for id_ in victims:
                del self._rows[id_]
            if victims:
                gone = set(victims)
                self._order = [i for i in self._order if i not in gone]

    # ── reads ───────────────────────────────────────────────────────────

    def count(self) -> int:
        with self._lock:
            return len(self._rows)

    def peek(self, limit: int = 10) -> dict[str, Any]:
        """First *limit* rows in insertion order — ids, embeddings,
        documents, metadatas (chroma-parity, oracle-verified)."""
        with self._lock:
            rows = [self._rows[i] for i in self._order[:limit]]
            return {
                "ids": [r.id for r in rows],
                "embeddings": [list(r.embedding_normed) for r in rows],
                "documents": [r.document for r in rows],
                "metadatas": [dict(r.metadata) for r in rows],
            }

    def get(self, *, ids: list[str] | None = None,
            where: dict[str, Any] | None = None,
            limit: int | None = None, offset: int = 0,
            include: list[str] | None = None) -> dict[str, Any]:
        include = include if include is not None else ["documents", "metadatas"]
        with self._lock:
            if ids is not None:
                rows = [self._rows[i] for i in ids if i in self._rows]
            else:
                rows = [self._rows[i] for i in self._order]
            if where is not None:
                rows = [r for r in rows if _matches(r.metadata, where)]
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            out: dict[str, Any] = {"ids": [r.id for r in rows]}
            if "documents" in include:
                out["documents"] = [r.document for r in rows]
            if "metadatas" in include:
                out["metadatas"] = [dict(r.metadata) for r in rows]
            if "embeddings" in include:
                out["embeddings"] = [list(r.embedding_normed) for r in rows]
            return out

    def query(self, *, query_embeddings: list[list[float]] | None = None,
              query_texts: list[str] | None = None, n_results: int = 10,
              where: dict[str, Any] | None = None,
              include: list[str] | None = None) -> dict[str, Any]:
        if query_embeddings is None:
            if query_texts is None:
                raise ValueError("query needs query_embeddings or query_texts")
            query_embeddings = self._embed_query(query_texts)
        include = include if include is not None else ["documents", "metadatas",
                                                       "distances"]
        with self._lock:
            candidates = [r for r in self._rows.values()
                          if where is None or _matches(r.metadata, where)]
            ids_out, docs_out, metas_out, dists_out = [], [], [], []
            for q in query_embeddings:
                q_normed = _normalize(list(q))
                scored = sorted(
                    ((_cosine_distance(q_normed, r.embedding_normed), r)
                     for r in candidates),
                    key=lambda pair: pair[0],
                )[:n_results]
                ids_out.append([r.id for _, r in scored])
                docs_out.append([r.document for _, r in scored])
                metas_out.append([dict(r.metadata) for _, r in scored])
                dists_out.append([d for d, _ in scored])
        out: dict[str, Any] = {"ids": ids_out}
        if "documents" in include:
            out["documents"] = docs_out
        if "metadatas" in include:
            out["metadatas"] = metas_out
        if "distances" in include:
            out["distances"] = dists_out
        return out


class InMemoryVectorClient:
    """Chroma-client-shaped registry of :class:`InMemoryCollection`.

    Per-instance state, genuinely — two clients share nothing.

    ``default_embedding_function`` (when given) attaches to collections
    created without an explicit EF — the analogue of chroma's implicit
    default EF, but injected rather than ambient. The T1-isolated leg
    relies on this: :class:`~nexus.db.t1.T1Database` creates its
    ``scratch`` collection without passing an EF and then adds
    documents-only rows.
    """

    def __init__(self, *, default_embedding_function: Any = None) -> None:
        self._collections: dict[str, InMemoryCollection] = {}
        self._default_ef = default_embedding_function
        self._lock = threading.Lock()

    def create_collection(self, name: str, *,
                          metadata: dict[str, Any] | None = None,
                          embedding_function: Any = None) -> InMemoryCollection:
        with self._lock:
            if name in self._collections:
                raise ValueError(f"collection {name!r} already exists")
            col = InMemoryCollection(
                name,
                embedding_function=embedding_function or self._default_ef,
                metadata=metadata,
            )
            self._collections[name] = col
            return col

    def get_collection(self, name: str, *,
                       embedding_function: Any = None) -> InMemoryCollection:
        with self._lock:
            try:
                col = self._collections[name]
            except KeyError:
                from nexus.errors import CollectionNotFoundError  # noqa: PLC0415 — circular-dep avoidance

                raise CollectionNotFoundError(
                    f"collection {name!r} does not exist"
                ) from None
            if embedding_function is not None and col._ef is None:
                col._ef = embedding_function
            return col

    def get_or_create_collection(self, name: str, *,
                                 metadata: dict[str, Any] | None = None,
                                 embedding_function: Any = None,
                                 ) -> InMemoryCollection:
        with self._lock:
            col = self._collections.get(name)
            if col is None:
                col = InMemoryCollection(
                    name,
                    embedding_function=embedding_function or self._default_ef,
                    metadata=metadata,
                )
                self._collections[name] = col
            elif embedding_function is not None and col._ef is None:
                col._ef = embedding_function
            return col

    def delete_collection(self, name: str) -> None:
        with self._lock:
            try:
                del self._collections[name]
            except KeyError:
                from nexus.errors import CollectionNotFoundError  # noqa: PLC0415 — circular-dep avoidance

                raise CollectionNotFoundError(
                    f"collection {name!r} does not exist"
                ) from None

    def list_collections(self) -> list[InMemoryCollection]:
        with self._lock:
            return list(self._collections.values())
