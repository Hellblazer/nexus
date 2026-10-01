# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Model/dimension routing and the substrate-neutral chunk pagers.

What lives here:

* :func:`dim_for_model_token` — THE canonical model-segment ->
  pgvector-table-dimension routing table (nexus-h1zu0), consumed by
  ``nexus.health`` and the substrate test helpers.
* :func:`iter_collection_chunks` / :func:`list_collection_names` — the
  pagers over any Chroma-SHAPED client, including the surviving
  :class:`~nexus.migration.pg_read.PgReadClient`.

The verify-fill (delta reconcile) machinery that used to live here
(``verify_fill_collections``, ``verify_fill_pg_source`` and their worker,
classification and report types) had no caller in ``src``, ``scripts``,
``tests/e2e`` or ``conexus`` and wrote chunks through the ownerless
``upsert-chunks`` route; RDR-223 Phase 3 (nexus-z0o2p.25) deleted it with the
other dead chunk-write path, ``db/embed_migrate``.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from nexus.db.limits import QUOTAS


def list_collection_names(client: Any) -> list[str]:
    """All collection names visible to *client*, sorted."""
    return sorted(c.name for c in client.list_collections())


def iter_collection_chunks(
    client: Any,
    collection_name: str,
    *,
    page_size: int | None = None,
    include_embeddings: bool = False,
    start_offset: int = 0,
) -> Iterator[dict[str, Any]]:
    """Yield every chunk of *collection_name* as ``{id, document, metadata}``.

    Substrate-neutral: pages any Chroma-SHAPED client (``get_collection``
    + ``col.get(include=..., limit=..., offset=...)``), which
    :class:`~nexus.migration.pg_read.PgReadClient` implements too. Pages
    at ``QUOTAS.MAX_QUERY_RESULTS`` (300) per call.

    By default the embedding vectors are NOT fetched: the pgvector side
    re-embeds server-side (Seam B), so dragging legacy vectors through a
    cross-model migration would invite contamination (RDR-109 hazard class).
    The SAME-MODEL passthrough (nexus-hxry2) is the deliberate exception: when
    the source model equals the target's wired model the stored vectors are
    already correct, so ``include_embeddings=True`` fetches them and each yielded
    chunk additionally carries ``"embedding"`` (a ``list[float]``, or ``None`` if
    the source row has no vector). The caller is responsible for only enabling
    this on the verified same-model branch.
    """
    page = page_size or QUOTAS.MAX_QUERY_RESULTS
    if page > QUOTAS.MAX_QUERY_RESULTS:
        raise ValueError(
            f"page_size {page} exceeds the per-call read cap "
            f"{QUOTAS.MAX_QUERY_RESULTS} (nexus.db.limits governs this read leg)"
        )
    include = ["documents", "metadatas"]
    if include_embeddings:
        include.append("embeddings")
    col = client.get_collection(collection_name)
    offset = start_offset
    while True:
        batch = col.get(include=include, limit=page, offset=offset)
        ids = batch.get("ids") or []
        if not ids:
            return
        docs = batch.get("documents") or [None] * len(ids)
        metas = batch.get("metadatas") or [None] * len(ids)
        embs = batch.get("embeddings") if include_embeddings else None
        if embs is None:
            embs = [None] * len(ids)
        for chunk_id, doc, meta, emb in zip(ids, docs, metas, embs):
            chunk = {"id": chunk_id, "document": doc, "metadata": dict(meta or {})}
            if include_embeddings:
                chunk["embedding"] = list(emb) if emb is not None else None
            yield chunk
        if len(ids) < page:
            return
        offset += len(ids)


#: Model-segment → pgvector table dimension. MIRRORS the Java authority
#: ``PgVectorRepository.MODEL_DIMS`` (service/src/main/java/dev/nexus/
#: service/vectors/PgVectorRepository.java) — the server fails loud on any
#: token not in this registry.
_MODEL_DIMS: dict[str, int] = {
    "voyage-code-3": 1024,
    "voyage-context-3": 1024,
    "voyage-3": 1024,
    "bge-base-en-v15-768": 768,
    "minilm-l6-v2-384": 384,
}


def dim_for_model_token(token: str) -> int | None:
    """Public accessor for :data:`_MODEL_DIMS` (nexus-h1zu0).

    THE canonical model-segment -> pgvector-table-dimension routing table,
    mirroring the Java authority ``PgVectorRepository.MODEL_DIMS`` (and, by
    construction, the per-dim ``split_part`` IN-lists used throughout
    rdr180-002-hex-boundary-functions.xml — formerly also
    ``nexus.manifest_orphans(dim)``'s, retired RDR-191 Phase 6
    nexus-o8dil.33; ``chash_conformance_report(dim)`` uses the same
    routing today). Returns ``None`` for an unrecognized token rather than
    guessing.

    Deliberately NOT the same registry as ``nexus.corpus.
    CANONICAL_EMBEDDING_MODELS``/``LOCAL_EMBEDDING_MODELS`` (consulted by
    ``commands.collection._dim_for_model_token`` and ``commands.
    catalog_cmds.doctor._expected_dim_for_model_token``): those answer a
    COLLECTION-NAMING-POLICY question (which models are canonical to mint
    a NEW collection name with) and omit the legacy ``voyage-3`` token on
    purpose (RDR-103 canonical-set guard). This function answers a
    STORAGE-ROUTING question (which physical ``chunks_<dim>`` table does
    an EXISTING collection's data live in) and must include every token
    the engine could have routed a live collection to — ``voyage-3``
    included. Use this one for anything that talks to
    ``nexus.chash_conformance_report(dim)`` or the ``embedding_<dim>``
    columns of the unified ``nexus.chunks`` table directly; use the
    corpus.py pair for collection-name minting/validation.
    """
    return _MODEL_DIMS.get(token)

