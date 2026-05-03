# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import chromadb
import chromadb.errors
import httpx
from chromadb.errors import (
    InvalidArgumentError as _ChromaInvalidArgumentError,
    NotFoundError as _ChromaNotFoundError,
)
import structlog

# voyageai is imported lazily inside ``__init__`` only when cloud mode
# is selected. The eager import here was pulling
# voyageai -> langchain_text_splitters -> transformers -> torch
# (multi-second cold start) into every CLI invocation through the
# nexus.commands.store -> nexus.db.t3 chain. Type annotations using
# ``voyageai.Client`` are stringified by ``from __future__ import
# annotations`` at the top of this file so the lazy import does not
# break static type checkers.

from nexus.config import get_credential
from nexus.corpus import embedding_model_for_collection, index_model_for_collection
from nexus.db.chroma_quotas import QUOTAS, QuotaValidator
from nexus.metadata_schema import CONTENT_TYPES, normalize, validate

_log = structlog.get_logger(__name__)


# ── Chroma HTTP timeout override (nexus-jgjw) ────────────────────────────────
#
# chromadb >=1.5 hardcodes ``httpx.Client(timeout=None, ...)`` at
# ``chromadb/api/fastapi.py:86,91``. With ``timeout=None``, a Chroma op blocks
# indefinitely on any read where the server has closed the connection — the
# failure mode observed during the 2026-05-03 orphan recovery (10+ min CPU=0%
# process state, one TCP socket in CLOSE_WAIT, no recovery without SIGTERM).
#
# Until chromadb exposes a ``Settings.chroma_http_request_timeout_seconds``
# field that propagates to the underlying httpx.Client (track upstream), we
# override the timeout once after construction. After this patch, a stalled
# read raises ``httpx.ReadTimeout`` — already classified retryable by
# ``nexus.retry._is_retryable_chroma_error`` — so the existing retry helper
# converts the hang into a bounded retry-then-fail loop.
#
# Defensive on shape: the override only fires for clients that expose
# ``client._server._session`` (CloudClient + HttpClient via FastAPI backend).
# PersistentClient and EphemeralClient have no HTTP session and are skipped.

#: Connect timeout (s) — TCP handshake. Generous; cloud handshakes are sub-sec.
CHROMA_HTTP_CONNECT_TIMEOUT_S: float = 10.0
#: Read timeout (s) — single response read window. Sized for ``col.get`` of
#: a 300-id batch with full metadata; longer than typical (~1s) to absorb
#: cloud-side jitter without false-positive retries.
CHROMA_HTTP_READ_TIMEOUT_S: float = 120.0
#: Write timeout (s) — request-body upload. ``col.update`` payloads are small
#: enough that 60 s is generous.
CHROMA_HTTP_WRITE_TIMEOUT_S: float = 60.0
#: Pool timeout (s) — wait time to acquire a connection from the pool.
CHROMA_HTTP_POOL_TIMEOUT_S: float = 10.0


def _apply_chroma_http_timeout(client: object) -> None:
    """Override chromadb's hardcoded ``httpx.Client(timeout=None)``.

    Called once after ``T3Database`` constructs (or is handed) a chroma
    client. No-op for clients that don't carry an HTTP session
    (PersistentClient, EphemeralClient, test mocks shaped without
    ``_server._session``).
    """
    server = getattr(client, "_server", None)
    if server is None:
        return
    session = getattr(server, "_session", None)
    if session is None:
        return
    try:
        session.timeout = httpx.Timeout(
            connect=CHROMA_HTTP_CONNECT_TIMEOUT_S,
            read=CHROMA_HTTP_READ_TIMEOUT_S,
            write=CHROMA_HTTP_WRITE_TIMEOUT_S,
            pool=CHROMA_HTTP_POOL_TIMEOUT_S,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("chroma_http_timeout_override_failed", error=str(exc))


# Legacy store_type values accepted on input (nexus-40t). All map to a
# :data:`nexus.metadata_schema.CONTENT_TYPES` value so the canonical
# schema stays compact.
_STORE_TYPE_TO_CONTENT_TYPE: dict[str, str] = {
    "code": "code",
    "pdf": "pdf",
    "markdown": "markdown",
    "prose": "prose",
    "knowledge": "prose",
    "rdr": "prose",
    "docs": "prose",
}


def _infer_content_type(metadata: dict, collection_name: str) -> str:
    """Pick a canonical ``content_type`` for a record (nexus-40t).

    Priority:
      1. Explicit ``content_type`` in the payload.
      2. Legacy ``store_type`` → mapped via
         :data:`_STORE_TYPE_TO_CONTENT_TYPE`.
      3. Collection prefix (``code__`` → ``code``; anything else → ``prose``).
    """
    content_type = metadata.get("content_type")
    if content_type in CONTENT_TYPES:
        return content_type  # type: ignore[return-value]

    store_type = metadata.get("store_type")
    if store_type in _STORE_TYPE_TO_CONTENT_TYPE:
        return _STORE_TYPE_TO_CONTENT_TYPE[store_type]

    if collection_name.startswith("code__"):
        return "code"
    return "prose"


def _normalize_for_write(metadata: dict, collection_name: str) -> dict:
    """Normalise *metadata* before any T3 upsert/update (nexus-40t)."""
    content_type = _infer_content_type(metadata, collection_name)
    return normalize(metadata, content_type=content_type)


# nexus-o6aa.9.16: collection prefixes whose writes bypass the canonical
# chunk schema. These are programmatically-populated collections that
# carry their own metadata vocabulary — applying the canonical
# normalize/validate would strip every collection-specific key. The
# production writers (e.g. ``catalog_taxonomy._batched_upsert``) already
# bypass by calling ``coll.upsert()`` directly; this set keeps the
# T3-public write path symmetric so .nxexp round-trips don't silently
# strip the collection's metadata at import time.
_BYPASS_SCHEMA_PREFIXES: tuple[str, ...] = ("taxonomy__",)


def _bypass_canonical_schema(collection_name: str) -> bool:
    """Return ``True`` if *collection_name* should skip canonical schema
    normalisation/validation (nexus-o6aa.9.16).
    """
    return collection_name.startswith(_BYPASS_SCHEMA_PREFIXES)


def _rewrite_collection_metadata(
    t3_db: T3Database,
    collection_name: str,
    *,
    source_path: str | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Rewrite every chunk's metadata to the canonical schema (nexus-2my).

    Paginates ``col.get(limit=300)`` over the collection, normalises each
    record's metadata via :func:`_normalize_for_write`, and writes back via
    :meth:`T3Database.update_chunks` (which validates the canonical
    output a second time as defense-in-depth).

    Returns ``(updated, skipped, total)``:

      * ``updated`` — chunks whose canonicalised metadata differs from
        the stored value (``dry_run`` reports what *would* be written).
      * ``skipped`` — chunks already in canonical shape; no write issued.
      * ``total`` — chunks scanned.

    Required follow-up to nexus-40t (PR #164): the original fix only
    constrains *new* writes. Already-indexed corpora keep their pre-4.3.1
    metadata until this command is run. Unblocks ART by sidestepping the
    pipeline-state staleness short-circuit on ``--force``.
    """
    page = QUOTAS.MAX_RECORDS_PER_WRITE  # 300, dual-purpose: read page + write batch

    # ChromaDB's ``where=`` is the cleanest filter — falls back to a
    # post-loop equality check when no source_path is requested.
    where_clause: dict | None = (
        {"source_path": source_path} if source_path else None
    )

    col = t3_db.get_or_create_collection(collection_name)
    updated = skipped = total = 0
    offset = 0

    while True:
        if where_clause is not None:
            batch = _chroma_with_retry(
                col.get, where=where_clause, include=["metadatas"],
                limit=page, offset=offset,
            )
        else:
            batch = _chroma_with_retry(
                col.get, include=["metadatas"], limit=page, offset=offset,
            )
        ids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        if not ids:
            break

        rewrite_ids: list[str] = []
        rewrite_metas: list[dict] = []
        for chunk_id, meta in zip(ids, metas):
            total += 1
            current = dict(meta or {})
            canonical = _normalize_for_write(current, collection_name)
            if canonical == current:
                skipped += 1
                continue
            updated += 1
            rewrite_ids.append(chunk_id)
            rewrite_metas.append(canonical)

        if rewrite_ids and not dry_run:
            t3_db.update_chunks(collection_name, rewrite_ids, rewrite_metas)

        if len(ids) < page:
            break
        offset += len(ids)

    return updated, skipped, total


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

    Single-database architecture (RDR-037, 2026-03-14): all collection
    prefixes (``code__``, ``docs__``, ``rdr__``, ``knowledge__``) coexist
    in one cloud database identified by the ``chroma_database`` config
    value.

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
        # Credential fallback (nexus-9ji/086 follow-up): when a caller passes
        # the bare constructor (scripts, research probes, quick repls), empty
        # args default to the configured credentials via nexus.config so the
        # constructor is not a footgun. Callers that supply explicit values
        # still win. Skipped entirely when ``_client`` is injected (tests).
        if _client is None and not local_mode:
            tenant = tenant or get_credential("chroma_tenant")
            database = database or get_credential("chroma_database")
            api_key = api_key or get_credential("chroma_api_key")
            voyage_api_key = voyage_api_key or get_credential("voyage_api_key")

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
        # Lazy voyageai import: only loaded when cloud mode is actually
        # used (the eager top-level import was multi-second cold-start
        # cost on every CLI invocation through the indirect import chain).
        if voyage_api_key:
            import voyageai  # noqa: PLC0415
            self._voyage_client = voyageai.Client(
                api_key=voyage_api_key,
                timeout=read_timeout_seconds,
                max_retries=0,
            )
        else:
            self._voyage_client = None
        if _client is not None:
            self._client = _client
        else:
            # Single-database architecture (RDR-037 consolidation,
            # 2026-03-14). All collection prefixes (code__, docs__,
            # rdr__, knowledge__) coexist in one cloud database.
            self._client = chromadb.CloudClient(
                tenant=tenant or None, database=database, api_key=api_key
            )
        _apply_chroma_http_timeout(self._client)

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
        """Return the embedding function for *collection_name*.

        Local mode: returns the bundled ONNX MiniLM-L6-v2 (384-dim) so the
        full pipeline runs without a Voyage API key.  Cloud mode: returns a
        VoyageAI EF whose model matches the one used at index time
        (per ``embedding_model_for_collection``) so query-time dimensions
        match the collection's indexed dimensions.  For CCE collections the
        EF is structural only (bypassed via ``_cce_embed()``).

        Caching is per-collection-name to match the existing test contract.
        """
        if self._ef_override is not None:
            return self._ef_override
        with self._ef_lock:
            if collection_name not in self._ef_cache:
                if self._local_mode:
                    from nexus.db.local_ef import LocalEmbeddingFunction
                    self._ef_cache[collection_name] = LocalEmbeddingFunction()
                else:
                    model = embedding_model_for_collection(collection_name)
                    self._ef_cache[collection_name] = (
                        chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction(
                            model_name=model, api_key=self._voyage_api_key
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
        *,
        fail_on_oversized: bool = False,
    ) -> None:
        """Split into ≤300-record chunks and upsert each.

        Acquires the per-collection write semaphore for the duration of all chunks.

        *fail_on_oversized* selects between two contracts:

        * ``False`` (default, indexer pipelines): drop-and-warn any
          record over ``QUOTAS.MAX_DOCUMENT_BYTES``. The chunker should
          never produce an oversized record; this is defense-in-depth
          for pipeline bugs and we'd rather keep running.
        * ``True`` (put path): raise :class:`PutOversizedError` for the
          first oversized record encountered. The put path has no
          chunker upstream, so silent-drop leaves the caller thinking
          the write succeeded while producing a catalog ghost
          (no row in ChromaDB despite a registered doc_id). GitHub
          #244 + bead ``nexus-akof``.
        """
        # Defense-in-depth: drop any document that exceeds the hard ChromaDB limit.
        # The chunker-level SAFE_CHUNK_BYTES cap should prevent this from ever firing.
        # Vector-only entries (``taxonomy__centroids``) legitimately have
        # ``document=None`` — treat as zero-byte rather than crashing on
        # ``None.encode()`` (nexus-fxc1).
        max_bytes = QUOTAS.MAX_DOCUMENT_BYTES

        def _doc_bytes(d: str | None) -> int:
            return 0 if d is None else len(d.encode())

        valid = [
            i for i, doc in enumerate(documents)
            if _doc_bytes(doc) <= max_bytes
        ]
        if len(valid) < len(documents):
            for i, doc in enumerate(documents):
                if _doc_bytes(doc) > max_bytes:
                    source = metadatas[i].get("source_path", "<unknown>") if i < len(metadatas) else "<unknown>"
                    if fail_on_oversized:
                        from nexus.errors import PutOversizedError

                        raise PutOversizedError(
                            doc_id=ids[i],
                            doc_bytes=_doc_bytes(doc),
                            max_bytes=max_bytes,
                            collection=collection_name,
                        )
                    _log.warning(
                        "write_batch_oversized_document_dropped",
                        source_path=source,
                        doc_bytes=_doc_bytes(doc),
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

        # Funnel every write through the canonical metadata schema
        # (nexus-40t). ``normalize()`` drops cargo keys and packs
        # ``git_*`` into ``git_meta``; ``validate()`` then fails loud if
        # anything still violates the 30-key ceiling or schema rules —
        # never silently drops fields by insertion order.
        #
        # nexus-o6aa.9.16: programmatic vector-only collections
        # (``taxonomy__*``) carry collection-specific metadata
        # (``topic_id``, ``label``, ``doc_count``, ``collection``) that
        # is not part of the canonical chunk schema. Production writes
        # bypass this funnel entirely (``catalog_taxonomy._batched_upsert``
        # calls ``coll.upsert()`` directly); the import path must do the
        # same or .nxexp round-trips for taxonomy collections silently
        # strip every taxonomy-specific key, breaking export-as-backup.
        if not _bypass_canonical_schema(collection_name):
            metadatas = [
                _normalize_for_write(m, collection_name) for m in metadatas
            ]
            for m in metadatas:
                validate(m)

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

    def get_or_create_collection(
        self, name: str, *, strict: bool | None = None,
    ) -> chromadb.Collection:
        """Get or create a T3 collection with the appropriate embedding function.

        In local mode, the collection is created with ``hnsw:search_ef`` set to
        the configured value (default 256) so HNSW query recall is tuned at
        collection-creation time.  Cloud SPANN collections do not use this key.

        nexus-18wz: programmatic vector-only collections matching
        :data:`_BYPASS_SCHEMA_PREFIXES` (``taxonomy__*``) are created with
        ``embedding_function=None`` and ``metadata={'hnsw:space': 'cosine'}``
        regardless of local_mode — mirroring
        :meth:`CatalogTaxonomy._create_centroid_collection`. This makes
        ``nx store import`` of a ``.nxexp`` for a taxonomy collection
        recreate the right shape; without it the import would silently
        default to L2 and break cosine queries against the imported centroids.

        RDR-101 Phase 6 strict mode (nexus-o6aa.14):
        ``strict=True`` rejects NEW collection names that fail
        :func:`nexus.corpus.is_conformant_collection_name`. Existing
        collections (any name) are always allowed; the validator only
        gates first-time creation. ``strict=None`` (default) reads
        ``[catalog].strict_collection_naming`` from config; absent or
        false keeps the existing permissive behavior so indexers and
        tests do not break before the irreversible flip ships.
        Operators wanting to opt out of strict mode in a config-on
        environment can pass ``strict=False`` explicitly (the
        backfill / migration verbs do this).
        """
        from nexus.corpus import (
            is_conformant_collection_name, validate_collection_name,
        )
        validate_collection_name(name)

        if strict is None:
            from nexus.config import load_config
            strict = bool(
                load_config().get("catalog", {}).get(
                    "strict_collection_naming", False,
                )
            )

        if (
            strict
            and not _bypass_canonical_schema(name)
            and not self.collection_exists(name)
            and not is_conformant_collection_name(name)
        ):
            raise ValueError(
                f"Collection name {name!r} is not conformant. Expected "
                f"<content_type>__<owner_id>__<embedding_model>__v<n>. "
                f"Pass strict=False (or unset [catalog].strict_collection_naming) "
                f"to allow grandfathered creation; pre-existing legacy "
                f"collections continue to be readable regardless of strict mode."
            )

        if _bypass_canonical_schema(name):
            metadata: dict = {"hnsw:space": "cosine"}
            if self._local_mode:
                from nexus.config import load_config
                cfg = load_config()
                metadata["hnsw:search_ef"] = cfg.get("search", {}).get("hnsw_ef", 256)
            kwargs: dict = {"embedding_function": None, "metadata": metadata}
        else:
            kwargs = {"embedding_function": self._embedding_fn(name)}
            if self._local_mode:
                from nexus.config import load_config
                cfg = load_config()
                hnsw_ef = cfg.get("search", {}).get("hnsw_ef", 256)
                kwargs["metadata"] = {"hnsw:search_ef": hnsw_ef}
        return _chroma_with_retry(
            self._client_for(name).get_or_create_collection,
            name, **kwargs,
        )

    def get_collection(self, name: str) -> chromadb.Collection:
        """Read-only collection access. Raises on missing collection
        (CloudClient surface; EphemeralClient + PersistentClient also
        raise). Used by read paths that should NOT auto-create
        collections — most importantly the ``chroma://`` reader in
        :mod:`nexus.aspect_readers`, where a missing collection is a
        signal to surface ``ReadFail(reason='unreachable')`` rather
        than create an empty side-effect collection.
        """
        return _chroma_with_retry(
            self._client_for(name).get_collection, name,
        )

    def get_embeddings(self, collection_name: str, ids: list[str]) -> "np.ndarray":
        """Fetch embeddings for specific document IDs.

        Returns an ``(N, D)`` float32 ndarray, one row per ID (in order).
        Used by the clustering pipeline to avoid including embeddings in
        every search response.
        """
        import numpy as np

        col = _chroma_with_retry(
            self._client_for(collection_name).get_collection, collection_name,
        )
        result = _chroma_with_retry(col.get, ids=ids, include=["embeddings"])
        return np.array(result["embeddings"], dtype=np.float32)

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
        catalog_doc_id: str = "",
    ) -> str:
        """Upsert *content* into *collection*. Returns the document ID.

        *ttl_days* = 0 means permanent. Expiry is no longer stored as a
        separate ``expires_at`` field — it's computed Python-side via
        :func:`nexus.metadata_schema.is_expired` from
        ``indexed_at + ttl_days``.

        Note: The document ID is derived from ``collection:title``. Calling put()
        with an empty title will overwrite any previous empty-title document in the
        same collection. Always provide a meaningful title to avoid unintentional
        overwrites.

        MCP-stored docs are single-chunk by definition; this routes through
        :func:`nexus.metadata_schema.make_chunk_metadata` so every
        ALLOWED_TOP_LEVEL field is populated and the chash dual-write hook
        gets the chunk_text_hash it needs (closes the RDR-086 coverage hole
        for MCP-stored docs).

        ``catalog_doc_id`` (RDR-101 Phase 3 PR δ Stage B.4) is the
        catalog ``Document.doc_id`` (Tumbler string) for the
        single-chunk doc. Caller registers the catalog entry FIRST and
        passes the resulting tumbler so the T3 chunk lands with a
        cross-reference back to the catalog at write-time. Empty
        string is the legacy / no-catalog path; ``normalize`` Step 4c
        drops the field on the way to T3.
        """
        from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415

        doc_id = hashlib.sha256(f"{collection}:{title}".encode()).hexdigest()[:16]
        now_iso = datetime.now(UTC).isoformat()

        # Determine whether this collection uses CCE.  When a voyage_api_key
        # is available and we're not in local mode, CCE collections (docs__,
        # knowledge__, rdr__) are embedded via _cce_embed() so that put()-stored
        # entries are in the same vector space as the CCE-indexed chunks.
        is_cce = (
            not self._local_mode
            and bool(self._voyage_api_key)
            and index_model_for_collection(collection) == "voyage-context-3"
        )

        # Derive content_type from the collection prefix so the factory
        # can stamp it through normalize().
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

        # MCP-stored docs are single-chunk: chunk_index=0, chunk_count=1,
        # chunk_text_hash matches content_hash because content == chunk text.
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        # RDR-101 Phase 5c dropped store_type, corpus, git_meta. Title kept
        # — find_ids_by_title is the load-bearing reader for nx store
        # delete --title and MCP store_get title-fallback.
        metadata = make_chunk_metadata(
            content_type=content_type,
            chunk_index=0,
            chunk_count=1,
            chunk_text_hash=content_hash,
            content_hash=content_hash,
            chunk_start_char=0,
            chunk_end_char=len(content),
            indexed_at=now_iso,
            embedding_model=index_model_for_collection(collection),
            title=title,
            tags=tags,
            category=category,
            ttl_days=ttl_days,
            source_agent=source_agent,
            session_id=session_id,
            doc_id=catalog_doc_id,
        )

        col = self.get_or_create_collection(collection)
        if is_cce:
            vec = self._cce_embed(content)
            self._write_batch(
                col, collection, [doc_id], [content], [metadata],
                embeddings=[vec], fail_on_oversized=True,
            )
        else:
            self._write_batch(
                col, collection, [doc_id], [content], [metadata],
                fail_on_oversized=True,
            )
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

        Every record is funnelled through the canonical metadata schema
        (nexus-40t) so enrichment post-passes can't overrun Chroma's
        32-key cap by merging fields on top of an already-full row.

        nexus-o6aa.9.16: programmatic vector-only collections
        (``taxonomy__*``) bypass the canonical schema — see
        :func:`_bypass_canonical_schema`.
        """
        if not _bypass_canonical_schema(collection):
            metadatas = [_normalize_for_write(m, collection) for m in metadatas]
            for m in metadatas:
                validate(m)
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
            # contextualized_embed(); using query_texts would invoke the
            # collection EF, which is not CCE-aware.
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
            try:
                qr = _chroma_with_retry(col.query, **query_kwargs)
            except _ChromaInvalidArgumentError as exc:
                # Dimension mismatch = collection was indexed with a
                # different embedding model than the one currently
                # configured. Crashes the whole multi-collection search
                # if we let it bubble. Skip this collection, warn, and
                # continue — issue #190 follow-up. Other
                # ``InvalidArgumentError`` subtypes (bad where clause,
                # malformed query, etc.) point at a caller bug and
                # must still surface.
                if "dimension" in str(exc).lower():
                    _log.warning(
                        "collection_dimension_mismatch_skipped",
                        collection=name,
                        error=str(exc),
                    )
                    continue
                raise
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

        Expiry is computed from ``indexed_at + ttl_days`` Python-side
        (see :func:`nexus.metadata_schema.is_expired`). ``ttl_days == 0``
        is the permanent sentinel — never expires regardless of
        indexed_at.

        Returns the total number of deleted documents.
        """
        from nexus.metadata_schema import is_expired  # noqa: PLC0415

        # ChromaDB only supports numeric $lt/$gt, so pre-filter by
        # ttl_days > 0 (eliminates permanent entries) then check
        # indexed_at + ttl_days Python-side via is_expired().
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
                    if is_expired(meta, now_iso=now_iso):
                        expired_ids.append(doc_id)
                offset += len(page_ids)
                if len(page_ids) < page_limit:
                    break  # last page (short or empty)
            if expired_ids:
                self._delete_batch(col, name, expired_ids)
            total += len(expired_ids)
        return total

    # ── Collection management ─────────────────────────────────────────────────

    def list_store(
        self, collection: str, limit: int = 200, offset: int = 0,
    ) -> list[dict]:
        """Return metadata for entries in a single knowledge__ collection.

        Each entry is a dict with at minimum ``id``, ``title``, ``tags``,
        ``ttl_days``, and ``indexed_at``.  Returns an empty list when the
        collection does not exist.

        Supports offset-based pagination via ChromaDB's native offset param.
        """
        try:
            col = self._client_for(collection).get_collection(collection)
        except _ChromaNotFoundError:
            return []
        clamped = limit if self._local_mode else min(limit, QUOTAS.MAX_QUERY_RESULTS)
        with self._read_sem(collection):
            result = _chroma_with_retry(
                col.get, include=["metadatas"], limit=clamped, offset=offset,
            )
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

    def rename_collection(self, old: str, new: str) -> None:
        """Rename a T3 collection via ``collection.modify(name=new)``.

        ChromaDB exposes ``modify(name=...)`` as an O(1) metadata-only
        rename — no embedding re-upload, no data movement. nexus-1ccq
        exposes this primitive as ``nx collection rename``. Caller
        guarantees ``new`` does not collide (we raise here if it does).
        """
        from nexus.corpus import validate_collection_name
        validate_collection_name(new)
        client = self._client_for(old)
        if self.collection_exists(new):
            raise ValueError(
                f"cannot rename to {new!r}: a collection with that name already exists",
            )
        col = client.get_collection(old)
        col.modify(name=new)
        # Bust the embedding-function cache under the old key so the next
        # caller fetching ``new`` does not reuse an unrelated EF.
        with self._ef_lock:
            self._ef_cache.pop(old, None)

    def list_unique_source_paths(self, collection_name: str) -> list[str]:
        """Return every distinct ``source_path`` value present in *collection_name*.

        Paginates ``col.get()`` to respect the ChromaDB Cloud 300-record
        limit and dedupes locally. Empty / missing source_path values
        are skipped — those are MCP-put chunks that have no on-disk
        source by design (the prune-stale CLI must not flag them).

        nexus-u7r0 (P1.4 / RDR-090): the staleness sweep needs to
        iterate the unique source paths in a collection so it can
        ``Path(...).exists()`` each one and ``delete_by_source`` the
        misses. There was no batched accessor for this before.
        Returns empty list if the collection does not exist.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return []
        seen: set[str] = set()
        offset = 0
        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        while True:
            result = _chroma_with_retry(
                col.get,
                include=["metadatas"],
                limit=page_limit,
                offset=offset,
            )
            page_metas = result.get("metadatas") or []
            page_ids = result.get("ids") or []
            if not page_ids:
                break
            for meta in page_metas:
                if not isinstance(meta, dict):
                    continue
                src = meta.get("source_path") or ""
                if src:
                    seen.add(src)
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break
        return sorted(seen)

    def ids_for_source(self, collection_name: str, source_path: str) -> list[str]:
        """Return all chunk IDs for a given source path. Does not fetch content.

        Paginates ``col.get()`` to respect the ChromaDB Cloud 300-record limit.
        Returns empty list if the collection does not exist.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return []
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
                break
        return ids

    def delete_by_source(self, collection_name: str, source_path: str) -> int:
        """Delete all chunks for a given source path. Returns count deleted.

        Uses paginated ``col.get()`` to avoid the ChromaDB Cloud 300-record
        truncation limit.  Same short-page termination pattern as ``expire()``.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return 0
        ids = self.ids_for_source(collection_name, source_path)
        if ids:
            self._delete_batch(col, collection_name, ids)
        return len(ids)

    def ids_for_doc_id(self, collection_name: str, doc_id: str) -> list[str]:
        """Return all chunk IDs for a given catalog ``doc_id``. No content fetch.

        Companion to :meth:`ids_for_source`; switches the chunk-lookup
        identity field from ``source_path`` to ``doc_id`` (RDR-101 Phase 4
        reader migration). Paginates ``col.get()`` to respect the ChromaDB
        Cloud 300-record limit. Returns empty list if the collection does
        not exist.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return []
        ids: list[str] = []
        offset = 0
        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        while True:
            result = _chroma_with_retry(
                col.get,
                where={"doc_id": doc_id},
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

    def delete_by_doc_id(self, collection_name: str, doc_id: str) -> int:
        """Delete all chunks for a given catalog ``doc_id``. Returns count.

        Companion to :meth:`delete_by_source` (RDR-101 Phase 4 reader
        migration). Uses paginated ``col.get()`` keyed on ``doc_id`` to
        avoid the ChromaDB Cloud 300-record truncation limit.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return 0
        ids = self.ids_for_doc_id(collection_name, doc_id)
        if ids:
            self._delete_batch(col, collection_name, ids)
        return len(ids)

    def list_chunks_with_metadata(
        self,
        collection_name: str,
        *,
        fields: tuple[str, ...] = ("doc_id", "indexed_at"),
    ) -> Iterator[tuple[str, dict[str, str]]]:
        """Yield ``(chunk_id, metadata_subset)`` for every chunk in a collection.

        Paginates ``col.get()`` to respect the ChromaDB Cloud 300-record
        limit. ``metadata_subset`` contains only the requested ``fields``,
        with empty strings for missing keys, so callers do not need to
        guard each key access. The default fields support RDR-101 Phase 6
        ``nx t3 gc`` (``doc_id`` for orphan detection, ``indexed_at`` for
        the orphan-window filter).
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return
        offset = 0
        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        while True:
            result = _chroma_with_retry(
                col.get,
                include=["metadatas"],
                limit=page_limit,
                offset=offset,
            )
            page_ids = result.get("ids") or []
            page_metas = result.get("metadatas") or []
            if not page_ids:
                break
            for cid, meta in zip(page_ids, page_metas):
                if not isinstance(meta, dict):
                    meta = {}
                yield cid, {f: str(meta.get(f, "")) for f in fields}
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break

    def delete_by_chunk_ids(
        self, collection_name: str, chunk_ids: list[str],
    ) -> int:
        """Delete chunks by explicit Chroma id. Returns count deleted.

        The per-chunk-id deletion primitive used by ``nx t3 gc`` (RDR-101
        Phase 6) and any future maintenance verb that selects orphan
        candidates outside the ``source_path``/``doc_id`` join paths.
        Empty ``chunk_ids`` is a no-op (returns 0); missing collection
        returns 0 without raising. Same paginated batching as
        :meth:`delete_by_source` via :meth:`_delete_batch`.
        """
        if not chunk_ids:
            return 0
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return 0
        self._delete_batch(col, collection_name, chunk_ids)
        return len(chunk_ids)

    def update_source_path(
        self, collection_name: str, old_path: str, new_path: str
    ) -> int:
        """Rewrite source_path metadata for all chunks matching old_path.

        Paginates via col.get() to respect Cloud's 300-record batch limit.
        Returns count of chunks updated. Idempotent.
        """
        try:
            col = self._client_for(collection_name).get_collection(collection_name)
        except _ChromaNotFoundError:
            return 0
        ids: list[str] = []
        metadatas: list[dict] = []
        offset = 0
        page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
        while True:
            result = _chroma_with_retry(
                col.get,
                where={"source_path": old_path},
                include=["metadatas"],
                limit=page_limit,
                offset=offset,
            )
            page_ids = result["ids"]
            page_metas = result["metadatas"]
            for i, mid in enumerate(page_ids):
                ids.append(mid)
                updated = dict(page_metas[i])
                updated["source_path"] = new_path
                metadatas.append(updated)
            offset += len(page_ids)
            if len(page_ids) < page_limit:
                break
        if not ids:
            return 0
        size = QUOTAS.MAX_RECORDS_PER_WRITE
        with self._write_sem(collection_name):
            for start in range(0, len(ids), size):
                _chroma_with_retry(
                    col.update,
                    ids=ids[start:start + size],
                    metadatas=metadatas[start:start + size],
                )
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

    def existing_ids(self, collection: str, ids: list[str]) -> set[str]:
        """Return the subset of *ids* that exist in *collection*.

        Batched at ``QUOTAS.MAX_RECORDS_PER_WRITE`` (300) per page to stay
        inside ChromaDB Cloud's free-tier per-call cap. Missing collections
        resolve to an empty set rather than raising — the caller ``nx
        catalog verify`` (GH #249) treats a missing collection the same as
        missing ids.

        Cheap: ``include=[]`` skips documents/embeddings/metadatas entirely,
        so the server-side work is a pure ANN-less presence check.
        """
        if not ids:
            return set()
        try:
            col = self._client_for(collection).get_collection(collection)
        except _ChromaNotFoundError:
            return set()
        found: set[str] = set()
        page = QUOTAS.MAX_RECORDS_PER_WRITE
        with self._read_sem(collection):
            for i in range(0, len(ids), page):
                batch = ids[i : i + page]
                result = _chroma_with_retry(col.get, ids=batch, include=[])
                found.update(result["ids"])
        return found

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


@dataclass
class VerifyResult:
    """Result of a collection health verification probe."""

    status: str  # "healthy", "degraded", "broken", "skipped"
    doc_count: int
    probe_doc_id: str | None = None
    distance: float | None = None
    metric: str = "unknown"
    probe_hit_rate: float | None = None


def verify_collection_deep(db: "T3Database", collection_name: str) -> VerifyResult:
    """Verify retrieval health by probing up to 5 known documents.

    Peeks at up to 5 stored documents, queries each, and checks if the
    original document appears in top-10 results.  Reports ``probe_hit_rate``
    as a crude Robustness-delta@K proxy.

    Raises KeyError if the collection does not exist.
    """
    info = db.collection_info(collection_name)
    count = info["count"]

    if count < 2:
        return VerifyResult(status="skipped", doc_count=count)

    client = db._client_for(collection_name)
    col = client.get_collection(collection_name)
    peek = col.peek(limit=5)

    if not peek["ids"]:
        return VerifyResult(status="skipped", doc_count=0)

    probe_ids = peek["ids"]
    probe_docs = peek.get("documents") or [""] * len(probe_ids)

    meta = col.metadata or {}
    if db._local_mode:
        metric = meta.get("hnsw:space", "l2")
    else:
        metric = "cosine"  # Cloud SPANN; hnsw:space not populated

    found_count = 0
    last_distance: float | None = None
    probed_count = 0

    for pid, pdoc in zip(probe_ids, probe_docs):
        words = pdoc.split()[:50]
        query = " ".join(words).strip()
        if not query:
            continue
        probed_count += 1
        results = db.search(query=query, collection_names=[collection_name], n_results=10)
        found = [r for r in results if r["id"] == pid]
        if found:
            found_count += 1
            last_distance = found[0]["distance"]

    if probed_count == 0:
        return VerifyResult(status="skipped", doc_count=count, probe_doc_id=probe_ids[0])

    probe_hit_rate = found_count / probed_count
    first_probe_id = probe_ids[0]

    if probe_hit_rate == 1.0:
        status = "healthy"
    elif probe_hit_rate > 0:
        status = "degraded"
    else:
        status = "broken"

    return VerifyResult(
        status=status,
        doc_count=count,
        probe_doc_id=first_probe_id,
        distance=last_distance,
        metric=metric,
        probe_hit_rate=probe_hit_rate,
    )


def apply_hnsw_ef(db: "T3Database") -> int:
    """Apply HNSW search_ef tuning to all local-mode collections.

    Iterates all collections in *db* and calls ``col.modify()`` to set
    ``hnsw:search_ef`` to the value from config (default 256).

    Returns the number of collections updated, or 0 if *db* is in cloud mode
    (SPANN does not use HNSW tuning parameters).
    """
    if not db._local_mode:
        return 0

    from nexus.config import load_config
    cfg = load_config()
    hnsw_ef: int = cfg.get("search", {}).get("hnsw_ef", 256)

    collections = _chroma_with_retry(db._client.list_collections)
    count = 0
    for col in collections:
        _chroma_with_retry(col.modify, metadata={"hnsw:search_ef": hnsw_ef})
        count += 1
    return count
