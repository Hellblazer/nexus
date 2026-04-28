# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document indexing pipeline: PDF and Markdown → T3 collections.

By default documents are stored in ``docs__`` collections.  Callers can
override the collection name for other prefixes (e.g. ``rdr__``).
"""
from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import structlog

_log = structlog.get_logger(__name__)

from nexus.checkpoint import (
    CHECKPOINT_DIR,
    CheckpointData,
    delete_checkpoint,
    read_checkpoint,
    write_checkpoint,
)
from nexus.corpus import index_model_for_collection
from nexus.db import make_t3
from nexus.retry import _chroma_with_retry, _voyage_with_retry
from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import PDFExtractor

# Type alias for the chunking callback used by _index_document.
# Receives (file_path, content_hash, target_model, now_iso, corpus) and returns
# a list of (chunk_id, document_text, metadata_dict) tuples, or an empty list.
ChunkFn = Callable[[Path, str, str, str, str], list[tuple[str, str, dict]]]

# Type alias for a local embedding function (replaces _embed_with_fallback).
# Receives (texts, model) and returns (embeddings, actual_model).
EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _has_credentials() -> bool:
    from nexus.config import get_credential
    return bool(get_credential("voyage_api_key") and get_credential("chroma_api_key"))


def _missing_credentials() -> list[str]:
    """Return the list of unset Voyage / Chroma credentials.

    Used to construct precise error messages — callers can tell the
    user EXACTLY which key is missing rather than the generic
    "credentials not configured" form. Empty list means both are set.
    """
    from nexus.config import get_credential
    missing: list[str] = []
    if not get_credential("voyage_api_key"):
        missing.append("voyage_api_key")
    if not get_credential("chroma_api_key"):
        missing.append("chroma_api_key")
    return missing


def _make_local_embed_fn() -> tuple[EmbedFn, str]:
    """Build an ``embed_fn`` backed by :class:`LocalEmbeddingFunction`,
    returned alongside the model name it will report.

    Used by ``_index_document`` and ``index_pdf`` as the credential-
    free fallback path: when the user is in local mode (``NX_LOCAL=1``
    or no Voyage/Chroma keys configured), ingestion uses the same
    ONNX MiniLM / fastembed model that ``store_put`` and local-mode
    ``nx search`` already use. The chunk metadata records the actual
    model that ran, not the requested ``target_model`` — staleness
    checks fire correctly when a later upgrade to cloud changes the
    target model.

    The caller overrides its ``target_model`` with the returned model
    name so the staleness check + chunk metadata are consistent: a
    re-index in local mode against unchanged content is a no-op
    instead of a silent re-embed.
    """
    from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415

    local_ef = LocalEmbeddingFunction()
    model_name = local_ef.model_name

    def _local_embed(texts: list[str], _target_model: str) -> tuple[list[list[float]], str]:
        # Honest about the actual model used. ``staleness_check`` in
        # ``_index_document`` compares ``stored_model == target_model``;
        # the caller overrides ``target_model`` with this name so
        # repeat-indexes against unchanged content are no-ops.
        embeddings = local_ef(texts)
        # Normalise to ``list[list[float]]`` regardless of which tier
        # ran underneath. ChromaDB's ONNXMiniLM_L6_V2 (TIER0) returns
        # ``np.ndarray`` per row; the chromadb upsert validator
        # accepts list[list[float]] or list[np.ndarray] but rejects
        # list[list[np.float32]] — so we convert all the way down to
        # native floats. fastembed (TIER1) is converted by
        # LocalEmbeddingFunction but ``np.float32`` scalars survive.
        normalized: list[list[float]] = []
        for vec in embeddings:
            if hasattr(vec, "tolist"):
                normalized.append(vec.tolist())  # numpy → list[float]
            else:
                normalized.append([float(x) for x in vec])
        return normalized, model_name

    return _local_embed, model_name


_CCE_TOKEN_LIMIT = 24_000  # 75% of Voyage's 32K to account for token estimation error
_CCE_TOTAL_TOKEN_LIMIT = 120_000  # Voyage API total token limit across all inputs
# Note: per-batch limit of 32K means we never hit 120K in a single call
_CCE_MAX_TOTAL_CHUNKS = 16_000  # Voyage API limit: max 16K chunks across all inputs
_EMBED_BATCH_SIZE = 128  # Voyage AI embed() limit is 1,000; use conservative batch size
_CCE_MAX_BATCH_CHUNKS = 1000  # Voyage API limit: max 1,000 inputs per request
_INCREMENTAL_BATCH_SIZE = 128  # Chunks per incremental embed/upsert batch
_INCREMENTAL_THRESHOLD = 128  # Use incremental path when chunk count exceeds this
_STREAMING_THRESHOLD = 0      # All PDFs use the streaming pipeline (resilient path)
_PARALLEL_WORKERS = 4  # Concurrent Voyage API calls for CCE embedding
_RATE_LIMIT_RPM = 250  # Target RPM for Voyage API (83% of 300 RPM limit)


class _TokenBucket:
    """Simple token-bucket rate limiter for API call throttling.

    Allows *burst* immediate calls, then throttles to *rpm* requests per minute.
    Thread-safe.
    """

    def __init__(self, rpm: int = _RATE_LIMIT_RPM, burst: int = 4) -> None:
        self._interval = 60.0 / rpm  # seconds between tokens
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._burst, self._tokens + elapsed / self._interval)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            time.sleep(self._interval * 0.5)


def _batch_chunks_for_cce(chunks: list[str]) -> list[list[str]]:
    """Split chunks into batches that each fit within the CCE token limit.

    Each batch must have >= 2 chunks (CCE requirement).  Single-leftover
    chunks are merged into the previous batch rather than dropped.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for chunk in chunks:
        chunk_tokens = len(chunk) // 2  # conservative: ~2 chars/token for academic text
        if current and (current_tokens + chunk_tokens > _CCE_TOKEN_LIMIT or len(current) >= _CCE_MAX_BATCH_CHUNKS):
            batches.append(current)
            current = [chunk]
            current_tokens = chunk_tokens
        else:
            current.append(chunk)
            current_tokens += chunk_tokens
    if current:
        # CCE requires >= 2 chunks per batch; merge singletons into previous batch
        # but only if that won't exceed the per-batch chunk limit
        if len(current) < 2 and batches and len(batches[-1]) < _CCE_MAX_BATCH_CHUNKS:
            batches[-1].extend(current)
        else:
            batches.append(current)
    return batches


def _embed_with_fallback(
    chunks: list[str],
    model: str,
    api_key: str,
    input_type: str = "document",
    timeout: float = 120.0,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[list[float]], str]:
    """Embed chunks using CCE when possible, falling back to voyage-4 on failure.

    Large documents are automatically batched into groups that fit within the
    CCE token limit.  Returns ``(embeddings, actual_model_used)`` so callers
    can record the model that produced the stored vectors in metadata.

    On CCE batch failure (token limit exceeded), the batch is split in half
    and retried with the same model. Never falls back to a different model —
    all vectors in a collection must come from the same embedding space.
    Single-chunk failures that cannot be split further raise immediately.

    We rely on Voyage's default truncation=True. Our chunker keeps chunks well
    under model context limits, so truncation should never activate. If it does,
    the embedding is still usable (just based on truncated text).
    """
    # Filter out empty strings — Voyage AI rejects them
    chunks = [c for c in chunks if c and c.strip()]
    if not chunks:
        return [], model
    if len(chunks) >= _CCE_MAX_TOTAL_CHUNKS:
        _log.warning(
            "chunk count exceeds Voyage API limit",
            chunk_count=len(chunks),
            limit=_CCE_MAX_TOTAL_CHUNKS,
        )
    import voyageai
    client = voyageai.Client(api_key=api_key, timeout=timeout, max_retries=0)
    if model == "voyage-context-3":
        # CCE API accepts single-element inputs — use it for all chunk counts.
        # The old >=2 requirement was our incorrect assumption; removing it ensures
        # single-chunk docs are indexed in the same embedding space as CCE queries.
        batches = _batch_chunks_for_cce(chunks) if len(chunks) >= 2 else [[chunks[0]]]
        all_embeddings: list[list[float]] = []

        def _embed_one_batch(batch: list[str]) -> list[list[float]]:
            """Embed a single CCE batch, splitting on failure.

            When a batch exceeds the 32K token limit, halves it recursively.
            When a single chunk is too large, truncates it to ~60K chars
            (~30K tokens) and retries once before skipping with a zero vector.
            """
            try:
                r = _voyage_with_retry(
                    client.contextualized_embed,
                    inputs=[batch], model=model, input_type=input_type,
                )
                return r.results[0].embeddings
            except Exception as exc:
                if len(batch) <= 1:
                    # Single oversized chunk — truncate and retry
                    _CCE_CHAR_LIMIT = 60_000  # ~30K tokens, under 32K context window
                    original_len = len(batch[0])
                    if original_len > _CCE_CHAR_LIMIT:
                        truncated = batch[0][:_CCE_CHAR_LIMIT]
                        _log.warning("cce_chunk_truncated",
                                     original_chars=original_len,
                                     truncated_chars=_CCE_CHAR_LIMIT)
                        try:
                            r = _voyage_with_retry(
                                client.contextualized_embed,
                                inputs=[[truncated]], model=model, input_type=input_type,
                            )
                            return r.results[0].embeddings
                        except Exception as exc2:
                            _log.warning("cce_chunk_skip_after_truncate",
                                         error=str(exc2), chars=_CCE_CHAR_LIMIT)
                            # Return zero vector — chunk is indexed but with degraded embedding
                            dim = len(all_embeddings[0]) if all_embeddings else 1024
                            return [[0.0] * dim]
                    # Not a length issue — re-raise
                    raise
                _log.warning("cce_batch_too_large_splitting",
                             error=str(exc), batch_size=len(batch))
                mid = len(batch) // 2
                result_embs: list[list[float]] = []
                for half in (batch[:mid], batch[mid:]):
                    result_embs.extend(_embed_one_batch(half))
                return result_embs

        if len(batches) >= 2:
            # Parallel CCE embedding with rate limiting (nexus-cmcp)
            bucket = _TokenBucket(rpm=_RATE_LIMIT_RPM, burst=_PARALLEL_WORKERS)
            batch_results: list[list[list[float]] | None] = [None] * len(batches)

            def _rate_limited_embed(idx: int, batch: list[str]) -> None:
                bucket.acquire()
                batch_results[idx] = _embed_one_batch(batch)

            # Indexing review C3: drain every future before re-raising.
            # The previous shape collected results in submission order via
            # future.result() — the first raising future aborted the loop
            # and the executor's __exit__ discarded the remaining results,
            # so a later batch's 429 was silently swallowed. We now wait
            # for *all* futures to complete, record the first exception,
            # then re-raise it — callers that retry see the full failure
            # surface, not just the first one.
            with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
                futures = [
                    pool.submit(_rate_limited_embed, i, b)
                    for i, b in enumerate(batches)
                ]
                first_exc: BaseException | None = None
                for future in futures:
                    try:
                        future.result()
                    except BaseException as exc:
                        if first_exc is None:
                            first_exc = exc
                if first_exc is not None:
                    raise first_exc

                # All batches completed successfully — extend in submission
                # order to preserve embedding alignment with the input chunks.
                done_count = 0
                for i, _ in enumerate(futures):
                    embs = batch_results[i]
                    if embs is None:
                        raise RuntimeError(
                            f"Batch {i} embedding result missing after future completed"
                        )
                    all_embeddings.extend(embs)
                    done_count += len(embs)
                    if on_progress:
                        on_progress(done_count, len(chunks))
        else:
            # Single batch — no parallelism overhead
            embs = _embed_one_batch(batches[0])
            all_embeddings.extend(embs)
            if on_progress:
                on_progress(len(all_embeddings), len(chunks))

        if all_embeddings:
            return all_embeddings, model
        raise RuntimeError(
            f"CCE embedding returned no vectors for {len(chunks)} chunks — "
            "refusing to fall through to voyage-4 (would corrupt vector space)"
        )
    # Standard embedding path (voyage-4 or any non-CCE model)
    all_emb: list[list[float]] = []
    for i in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[i:i + _EMBED_BATCH_SIZE]
        result = _voyage_with_retry(client.embed, texts=batch, model=model, input_type=input_type)
        all_emb.extend(result.embeddings)
        if on_progress:
            on_progress(len(all_emb), len(chunks))
    return all_emb, model


def _index_document(
    file_path: Path,
    corpus: str,
    chunk_fn: ChunkFn,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    source_key: str | None = None,
) -> int | list[dict]:
    """Shared indexing pipeline: credential check, staleness, embed, upsert, prune.

    *chunk_fn(file_path, content_hash, target_model, now_iso)* produces the
    per-format (chunk_id, document_text, metadata_dict) tuples.  Returns the
    number of chunks indexed, or 0 if skipped.

    When *collection_name* is provided it is used as the T3 collection name
    directly, bypassing the default ``docs__{corpus}`` derivation.  This is
    used for RDR collections (``rdr__<repo>-<hash8>``).

    When *embed_fn* is provided it replaces ``_embed_with_fallback`` and the
    Voyage AI credential check is skipped.  This supports local dry-run mode
    (ONNX / DefaultEmbeddingFunction) without requiring any API keys.

    When *return_metadata* is True, returns the prepared chunk metadatas list
    instead of a bare int.  Callers (index_pdf, index_markdown) use it to
    build format-specific summary dicts.  Default False preserves the existing
    int return type with zero overhead.

    When *source_key* is provided it overrides ``str(file_path)`` as the
    ``source_path`` value used in the staleness check and stale-chunk pruning.
    Callers pass a relative path here so that T3 metadata lookups match the
    relative ``source_path`` stored in chunk metadata (RDR-060).
    """
    # GH #336: when ``nx index md/pdf`` runs in local mode we want
    # the local ONNX/fastembed embedder rather than a hard fail. The
    # local model name overrides ``target_model`` so the staleness
    # check + chunk metadata are consistent.
    local_target_model: str | None = None
    if embed_fn is None:
        from nexus.config import is_local_mode  # noqa: PLC0415

        if is_local_mode():
            embed_fn, local_target_model = _make_local_embed_fn()
        elif not _has_credentials():
            from nexus.errors import CredentialsMissingError  # noqa: PLC0415

            missing = _missing_credentials()
            raise CredentialsMissingError(
                f"cannot index in cloud mode without {', '.join(missing)}. "
                f"Either set the missing key(s) via 'nx config set <key> "
                f"<value>' (or env var), or unset NX_LOCAL to fall back "
                f"to local-mode ingestion (no API keys needed)."
            )

    # Normalize to absolute so staleness checks are path-form-independent.
    file_path = file_path.resolve()

    sp = source_key if source_key is not None else str(file_path)
    content_hash = _sha256(file_path)
    if collection_name is None:
        collection_name = f"docs__{corpus}"
    db = t3 if t3 is not None else make_t3()
    col = db.get_or_create_collection(collection_name)

    target_model = index_model_for_collection(collection_name)
    if local_target_model is not None:
        # Local-mode override: chunk metadata records the local model
        # name so re-indexes against unchanged content skip cleanly,
        # and a later upgrade to cloud mode triggers re-embed (the
        # cloud target_model differs from the locally-stored name).
        target_model = local_target_model

    # Incremental sync: skip if file is already indexed with the same hash AND model
    existing = _chroma_with_retry(
        col.get,
        where={"source_path": sp},
        include=["metadatas"],
        limit=1,
    )
    if not force and existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        if stored_hash == content_hash and stored_model == target_model:
            return 0

    now_iso = datetime.now(UTC).isoformat()
    prepared = chunk_fn(file_path, content_hash, target_model, now_iso, corpus)
    if not prepared:
        return 0

    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas = [p[2] for p in prepared]

    if embed_fn is not None:
        embeddings, actual_model = embed_fn(documents, target_model)
    else:
        from nexus.config import get_credential, load_config
        voyage_key = get_credential("voyage_api_key")
        if not voyage_key:
            raise RuntimeError("voyage_api_key must be set — unreachable if _has_credentials() passed")
        timeout = load_config().get("voyageai", {}).get("read_timeout_seconds", 120.0)
        embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key, timeout=timeout, on_progress=on_progress)
    if actual_model != target_model:
        for m in metadatas:
            m["embedding_model"] = actual_model
    db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)

    # Post-store hook chains (RDR-095). Both single-doc and batch chains
    # fire from every storage event; the per-doc loop covers single-shape
    # consumers on CLI ingest.
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        fire_post_store_batch_hooks,
        fire_post_store_hooks,
    )
    fire_post_store_batch_hooks(
        ids, collection_name, documents, embeddings, metadatas,
    )
    for _did, _doc in zip(ids, documents):
        fire_post_store_hooks(_did, collection_name, _doc)
    # RDR-089 document-grain chain — fires once per file boundary.
    # content="" because only chunk text is in scope here; the hook
    # reads source_path itself per the P0.1 content-sourcing contract.
    fire_post_document_hooks(sp, collection_name, "")

    # Prune stale chunks from a previous (larger) version of this file.
    # Paginate: ChromaDB Cloud returns at most 300 records per get() call.
    current_ids_set = set(ids)
    stale_ids: list[str] = []
    offset = 0
    while True:
        batch = _chroma_with_retry(
            col.get,
            where={"source_path": sp},
            include=[],
            limit=300,
            offset=offset,
        )
        batch_ids = batch.get("ids", [])
        stale_ids.extend(eid for eid in batch_ids if eid not in current_ids_set)
        if len(batch_ids) < 300:
            break
        offset += 300
    if stale_ids:
        # Batch deletes at MAX_RECORDS_PER_WRITE=300 (indexing review I4).
        for i in range(0, len(stale_ids), 300):
            _chroma_with_retry(col.delete, ids=stale_ids[i:i + 300])

    if return_metadata:
        return metadatas
    return len(prepared)


def _index_pdf_incremental(
    file_path: Path,
    corpus: str,
    prepared: list[tuple[str, str, dict]],
    content_hash: str,
    collection_name: str,
    t3: Any,
    *,
    embed_fn: EmbedFn | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Embed and upsert chunks in batches with checkpoint support.

    Designed for large PDFs where the embed/upsert phase can take many minutes.
    Writes a checkpoint after each batch so a crash loses at most one batch
    of work (~128 chunks).

    The full document has already been extracted and chunked — this function
    only handles the embed → upsert → checkpoint loop.

    Returns the total number of chunks indexed.
    """
    target_model = prepared[0][2]["embedding_model"] if prepared else "voyage-context-3"
    total = len(prepared)

    # Check for existing checkpoint — resume from where we left off.
    #
    # Indexing review I1: if the extractor/chunker produced fewer chunks
    # this run than the checkpoint claims (e.g. Docling vs MinerU version
    # mismatch or PDF re-chunked under a new chunk_chars setting), the
    # naive ``min(ckpt.chunks_upserted, total)`` would skip the whole
    # loop and leave the T3 collection with stale chunks beyond index
    # ``total``. Detect the mismatch and discard the checkpoint so we
    # re-index from 0 — slower but correct.
    ckpt = read_checkpoint(content_hash, collection_name)
    start_offset = 0
    if ckpt is not None and ckpt.chunks_upserted > total:
        _log.warning(
            "checkpoint_count_shrunk_discarding",
            stored=ckpt.chunks_upserted,
            current=total,
            pdf=str(file_path),
        )
        delete_checkpoint(content_hash, collection_name)
        ckpt = None
    if ckpt is not None:
        start_offset = min(ckpt.chunks_upserted, total)
        _log.info(
            "checkpoint_resume",
            pdf=str(file_path),
            chunks_done=start_offset,
            total=total,
        )

    ids_all = [p[0] for p in prepared]
    documents_all = [p[1] for p in prepared]
    metadatas_all = [p[2] for p in prepared]

    for batch_start in range(start_offset, total, _INCREMENTAL_BATCH_SIZE):
        batch_end = min(batch_start + _INCREMENTAL_BATCH_SIZE, total)
        batch_docs = documents_all[batch_start:batch_end]
        batch_ids = ids_all[batch_start:batch_end]
        batch_metas = metadatas_all[batch_start:batch_end]

        # Embed
        if embed_fn is not None:
            embeddings, actual_model = embed_fn(batch_docs, target_model)
        else:
            from nexus.config import get_credential, load_config
            voyage_key = get_credential("voyage_api_key")
            if not voyage_key:
                raise RuntimeError("voyage_api_key required")
            timeout = load_config().get("voyageai", {}).get("read_timeout_seconds", 120.0)
            embeddings, actual_model = _embed_with_fallback(
                batch_docs, target_model, voyage_key, timeout=timeout,
            )

        if actual_model != target_model:
            for m in batch_metas:
                m["embedding_model"] = actual_model

        # Upsert
        t3.upsert_chunks_with_embeddings(collection_name, batch_ids, batch_docs, embeddings, batch_metas)

        # Post-store hook chains (RDR-095). Both single-doc and batch
        # chains fire from every storage event; the per-doc loop covers
        # single-shape consumers on CLI ingest.
        from nexus.mcp_infra import (
            fire_post_store_batch_hooks,
            fire_post_store_hooks,
        )
        fire_post_store_batch_hooks(
            batch_ids, collection_name, batch_docs, embeddings, batch_metas,
        )
        for _did, _doc in zip(batch_ids, batch_docs):
            fire_post_store_hooks(_did, collection_name, _doc)

        # Checkpoint
        write_checkpoint(CheckpointData(
            pdf=str(file_path),
            collection=collection_name,
            content_hash=content_hash,
            chunks_upserted=batch_end,
            total_chunks=total,
            embedding_model=target_model,
        ))

        if on_progress:
            on_progress(batch_end, total)

    # Prune stale chunks from a previous (larger) version of this file.
    col = t3.get_or_create_collection(collection_name)
    current_ids_set = set(ids_all)
    stale_ids: list[str] = []
    offset = 0
    while True:
        batch = _chroma_with_retry(
            col.get,
            where={"source_path": str(file_path)},
            include=[],
            limit=300,
            offset=offset,
        )
        batch_ids = batch.get("ids", [])
        stale_ids.extend(eid for eid in batch_ids if eid not in current_ids_set)
        if len(batch_ids) < 300:
            break
        offset += 300
    if stale_ids:
        # Batch deletes at MAX_RECORDS_PER_WRITE=300 (indexing review I4).
        for i in range(0, len(stale_ids), 300):
            _chroma_with_retry(col.delete, ids=stale_ids[i:i + 300])

    # Clean up checkpoint on success
    delete_checkpoint(content_hash, collection_name)
    return total


def _pdf_chunks(
    pdf_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
    corpus: str,
    *,
    chunk_chars: int | None = None,
    bib_enrich_enabled: bool = False,
    extractor: str = "auto",
    git_meta: dict | None = None,
) -> list[tuple[str, str, dict]]:
    """Chunk a PDF and return (id, text, metadata) tuples.

    *chunk_chars* overrides the default chunk size (1500 chars).  When None
    the PDFChunker default is used.  Pass ``tuning.pdf_chunk_chars`` from
    TuningConfig to honour per-repo configuration.

    *bib_enrich_enabled* controls whether Semantic Scholar is queried for
    bibliographic metadata (year, venue, authors, citation count).  Disable
    for offline/air-gapped environments or bulk indexing.

    *extractor* selects the PDF extraction backend (``"auto"``, ``"docling"``,
    or ``"mineru"``).

    *git_meta* — flat ``git_*`` provenance dict. When ``None`` the function
    auto-detects via :func:`nexus.indexer_utils.detect_git_metadata` from
    ``pdf_path``. Pass an explicit value when the caller has already
    resolved it (the repo-walk path does this once per repo). Empty dict
    when *pdf_path* is not in a git repository — :func:`normalize` then
    omits ``git_meta`` per the empty-set rule (nexus-2my fix #3).
    """
    if git_meta is None:
        from nexus.indexer_utils import detect_git_metadata
        git_meta = detect_git_metadata(pdf_path)
    result = PDFExtractor().extract(pdf_path, extractor=extractor)
    chunker = PDFChunker(chunk_chars=chunk_chars) if chunk_chars is not None else PDFChunker()
    chunks = chunker.chunk(result.text, result.metadata)
    if not chunks:
        return []

    # Heuristic: fewer than 20 chars per page suggests a scanned/image-only PDF.
    # Per-page normalisation avoids false positives on short-but-real documents.
    _page_count = result.metadata.get("page_count", 1) or 1
    is_image_pdf = (len(result.text) / _page_count) < 20
    has_formulas = result.metadata.get("formula_count", 0) > 0

    # Compute source_title once before the loop so bib lookup uses the same value.
    # nexus-8l6 fallback: extractor metadata wins; otherwise derive from
    # first H1 or normalised filename (preserves initialisms like RDR, API).
    from nexus.indexer_utils import derive_title
    source_title = (
        str(result.metadata.get("docling_title") or "").strip()
        or str(result.metadata.get("pdf_title") or "").strip()
        or derive_title(pdf_path, body=None)
    )
    bib: dict = {}
    if bib_enrich_enabled:
        from nexus.bib_enricher import enrich as bib_enrich
        bib = bib_enrich(source_title)

    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415

    prepared: list[tuple[str, str, dict]] = []
    for chunk in chunks:
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta = make_chunk_metadata(
            content_type="pdf",
            source_path=str(pdf_path),
            chunk_index=chunk.chunk_index,
            chunk_count=len(chunks),
            chunk_text_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
            content_hash=content_hash,
            chunk_start_char=chunk.metadata.get("chunk_start_char", 0),
            chunk_end_char=chunk.metadata.get("chunk_end_char", 0),
            page_number=chunk.metadata.get("page_number", 0),
            indexed_at=now_iso,
            embedding_model=target_model,
            store_type="pdf",
            corpus=corpus,
            title=source_title,
            source_author=result.metadata.get("pdf_author", ""),
            section_title=chunk.metadata.get("section_title", ""),
            section_type=chunk.metadata.get("section_type", ""),
            tags="pdf",
            category="paper",
            bib_year=bib.get("year", 0),
            bib_authors=bib.get("authors", ""),
            bib_venue=bib.get("venue", ""),
            bib_citation_count=bib.get("citation_count", 0),
            git_meta=git_meta,
        )
        prepared.append((chunk_id, chunk.text, meta))
    return prepared


def _markdown_chunks(
    md_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
    corpus: str,
    *,
    base_path: Path | None = None,
    git_meta: dict | None = None,
) -> list[tuple[str, str, dict]]:
    """Chunk a Markdown file and return (id, text, metadata) tuples.

    *git_meta* — flat ``git_*`` provenance dict. ``None`` triggers
    auto-detection from *md_path* via
    :func:`nexus.indexer_utils.detect_git_metadata`. Empty dict outside
    a git repo (nexus-2my fix #3).
    """
    from nexus.catalog.catalog import make_relative

    if git_meta is None:
        from nexus.indexer_utils import detect_git_metadata
        git_meta = detect_git_metadata(md_path)

    raw_text = md_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw_text)
    frontmatter_len = len(raw_text) - len(body)

    sp = make_relative(md_path, base_path) if base_path else str(md_path)
    base_meta: dict = {
        "source_path": sp,
        "corpus": corpus,
    }
    chunks = SemanticMarkdownChunker().chunk(body, base_meta)
    if not chunks:
        return []

    # nexus-8l6: source_title fallback chain. Frontmatter ``title:`` wins;
    # otherwise derive from the first H1 or the normalised filename so
    # ``nx store list`` never displays ``untitled``.
    from nexus.indexer_utils import derive_title
    source_title = (
        str(frontmatter.get("title") or "").strip()
        or derive_title(md_path, body)
    )

    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415

    prepared: list[tuple[str, str, dict]] = []
    for chunk in chunks:
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta = make_chunk_metadata(
            content_type="markdown",
            source_path=sp,
            chunk_index=chunk.chunk_index,
            chunk_count=len(chunks),
            chunk_text_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
            content_hash=content_hash,
            chunk_start_char=chunk.metadata.get("chunk_start_char", 0) + frontmatter_len,
            chunk_end_char=chunk.metadata.get("chunk_end_char", 0) + frontmatter_len,
            page_number=chunk.metadata.get("page_number", 0),
            indexed_at=now_iso,
            embedding_model=target_model,
            store_type="markdown",
            corpus=corpus,
            title=source_title,
            source_author=str(frontmatter.get("author", "")),
            section_title=chunk.metadata.get("header_path", ""),
            section_type=chunk.metadata.get("section_type", ""),
            tags="markdown",
            category="prose",
            git_meta=git_meta,
        )
        prepared.append((chunk_id, chunk.text, meta))
    return prepared


def index_pdf(
    pdf_path: Path,
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    enrich: bool = False,
    extractor: str = "auto",
    streaming: str = "auto",
) -> int | dict:
    """Index *pdf_path* into a T3 collection.

    By default the collection is ``docs__{corpus}``.  Pass *collection_name*
    to override (e.g. ``knowledge__delos`` for external reference corpora).

    Returns the number of chunks indexed, or 0 if skipped (no credentials or
    content unchanged since last index with the same embedding model).

    Pass *embed_fn* to override the default Voyage AI embedding (e.g. a local
    ONNX function for dry-run mode).  When *embed_fn* is provided the Voyage
    credential check is bypassed.

    Pass *force=True* to bypass the staleness check and always re-index.

    When *return_metadata* is True, returns a dict instead of an int::

        {"chunks": int, "pages": list[int], "title": str, "author": str}

    Metadata is derived from chunk metadatas produced during extraction
    (no additional T3 query).  Default False preserves existing int behavior.

    Pass *enrich=True* to enable Semantic Scholar bibliographic metadata
    lookup (year, venue, authors, citations).  Default is False (opt-in)
    to avoid network calls in offline/air-gapped environments.  Use
    ``nx enrich <collection>`` for deliberate backfill.
    """
    from functools import partial

    _empty_meta = {"chunks": 0, "pages": [], "title": "", "author": ""}
    # GH #336 mirror: same local-fallback semantics as ``_index_document``.
    local_target_model: str | None = None
    if embed_fn is None:
        from nexus.config import is_local_mode  # noqa: PLC0415

        if is_local_mode():
            embed_fn, local_target_model = _make_local_embed_fn()
        elif not _has_credentials():
            from nexus.errors import CredentialsMissingError  # noqa: PLC0415

            missing = _missing_credentials()
            raise CredentialsMissingError(
                f"cannot index in cloud mode without {', '.join(missing)}. "
                f"Either set the missing key(s) via 'nx config set <key> "
                f"<value>' (or env var), or unset NX_LOCAL to fall back "
                f"to local-mode ingestion (no API keys needed)."
            )

    # Normalize to absolute so staleness checks are path-form-independent.
    pdf_path = pdf_path.resolve()

    content_hash = _sha256(pdf_path)
    col_name = collection_name if collection_name is not None else f"docs__{corpus}"
    db = t3 if t3 is not None else make_t3()  # T3Database instance (not PipelineDB)
    col = db.get_or_create_collection(col_name)
    target_model = index_model_for_collection(col_name)
    if local_target_model is not None:
        # See _index_document for rationale: keep the staleness check
        # + chunk metadata aligned with the local embedder's actual
        # model so repeat-indexes are no-ops.
        target_model = local_target_model

    # Incremental sync: skip if file is already indexed with the same hash AND model
    existing = _chroma_with_retry(
        col.get,
        where={"source_path": str(pdf_path)},
        include=["metadatas"],
        limit=1,
    )
    if not force and existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        if stored_hash == content_hash and stored_model == target_model:
            if return_metadata:
                return {"chunks": 0, "pages": [], "title": "", "author": ""}
            return 0

    # Streaming pipeline routing: check page count before full extraction.
    if streaming in ("auto", "always"):
        try:
            import pymupdf
            with pymupdf.open(str(pdf_path)) as _doc:
                page_count = len(_doc)
        except Exception:
            page_count = -1  # can't open PDF — fall through to batch path
        use_streaming = streaming == "always" or (page_count >= 0 and page_count >= _STREAMING_THRESHOLD)
        if use_streaming:
            from nexus.pipeline_stages import pipeline_index_pdf
            # Returns 0 if skipped (already running or completed by another process).
            # The staleness check above (line 638-644) handles the "unchanged" case;
            # a 0 here means a concurrent pipeline is active on this content_hash.
            count = pipeline_index_pdf(
                pdf_path, content_hash, col_name, db,
                embed_fn=embed_fn, extractor=extractor,
                corpus=corpus, target_model=target_model,
                force=force,
            )
            if return_metadata:
                # Query T3 for metadata after streaming upload.
                all_meta: list[dict] = []
                offset = 0
                while True:
                    batch = _chroma_with_retry(
                        col.get,
                        where={"source_path": str(pdf_path)},
                        include=["metadatas"],
                        limit=300,
                        offset=offset,
                    )
                    all_meta.extend(batch.get("metadatas", []))
                    if len(batch.get("ids", [])) < 300:
                        break
                    offset += 300
                return {
                    "chunks": count,
                    "pages": sorted({m.get("page_number", 0) for m in all_meta}),
                    "title": all_meta[0].get("title", "") if all_meta else "",
                    "author": all_meta[0].get("source_author", "") if all_meta else "",
                }
            return count

    # Catalog registration helper for batch paths (streaming has its own hook)
    def _register_in_catalog(meta_list: list[dict], chunk_count: int) -> None:
        try:
            from nexus.pipeline_stages import _catalog_pdf_hook
            _catalog_pdf_hook(
                pdf_path, col_name,
                title=meta_list[0].get("title", "") if meta_list else "",
                author=meta_list[0].get("source_author", "") if meta_list else "",
                year=int(meta_list[0].get("year", 0)) if meta_list else 0,
                corpus=corpus,
                chunk_count=chunk_count,
            )
        except Exception:
            pass  # catalog registration is non-fatal

    # Extract and chunk the entire document
    now_iso = datetime.now(UTC).isoformat()
    chunk_fn = partial(_pdf_chunks, bib_enrich_enabled=enrich, extractor=extractor)
    prepared = chunk_fn(pdf_path, content_hash, target_model, now_iso, corpus)
    if not prepared:
        return _empty_meta if return_metadata else 0

    # Route: incremental for large documents, original path for small ones
    if len(prepared) > _INCREMENTAL_THRESHOLD:
        count = _index_pdf_incremental(
            pdf_path, corpus, prepared, content_hash, col_name, db,
            embed_fn=embed_fn, on_progress=on_progress,
        )
        metadatas = [p[2] for p in prepared]
        _register_in_catalog(metadatas, len(metadatas))
        # RDR-089 document-grain chain — fires once per PDF boundary at the
        # incremental-branch tail. content="" (chunks already paginated
        # through T3); the hook reads source_path itself per the P0.1
        # content-sourcing contract.
        from nexus.mcp_infra import fire_post_document_hooks
        fire_post_document_hooks(str(pdf_path), col_name, "")
        if return_metadata:
            return {
                "chunks": len(metadatas),
                "pages": sorted({m.get("page_number", 0) for m in metadatas}),
                "title": metadatas[0].get("title", "") if metadatas else "",
                "author": metadatas[0].get("source_author", "") if metadatas else "",
            }
        return count

    # Small document: use the original all-at-once path
    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas_list = [p[2] for p in prepared]

    if embed_fn is not None:
        embeddings, actual_model = embed_fn(documents, target_model)
    else:
        from nexus.config import get_credential, load_config
        voyage_key = get_credential("voyage_api_key")
        if not voyage_key:
            raise RuntimeError("voyage_api_key must be set — unreachable if _has_credentials() passed")
        timeout = load_config().get("voyageai", {}).get("read_timeout_seconds", 120.0)
        embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key, timeout=timeout, on_progress=on_progress)
    if actual_model != target_model:
        for m in metadatas_list:
            m["embedding_model"] = actual_model
    db.upsert_chunks_with_embeddings(col_name, ids, documents, embeddings, metadatas_list)

    # Post-store hook chains (RDR-095). Both single-doc and batch chains
    # fire from every storage event; the per-doc loop covers single-shape
    # consumers on CLI ingest.
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        fire_post_store_batch_hooks,
        fire_post_store_hooks,
    )
    fire_post_store_batch_hooks(
        ids, col_name, documents, embeddings, metadatas_list,
    )
    for _did, _doc in zip(ids, documents):
        fire_post_store_hooks(_did, col_name, _doc)
    # RDR-089 document-grain chain — fires once per small-doc PDF boundary.
    # content="" (full document text not retained in this path); the hook
    # reads source_path itself.
    fire_post_document_hooks(str(pdf_path), col_name, "")

    # Prune stale chunks
    current_ids_set = set(ids)
    stale_ids: list[str] = []
    offset = 0
    while True:
        batch = _chroma_with_retry(
            col.get,
            where={"source_path": str(pdf_path)},
            include=[],
            limit=300,
            offset=offset,
        )
        batch_ids = batch.get("ids", [])
        stale_ids.extend(eid for eid in batch_ids if eid not in current_ids_set)
        if len(batch_ids) < 300:
            break
        offset += 300
    if stale_ids:
        # Batch deletes at MAX_RECORDS_PER_WRITE=300 (indexing review I4).
        for i in range(0, len(stale_ids), 300):
            _chroma_with_retry(col.delete, ids=stale_ids[i:i + 300])

    _register_in_catalog(metadatas_list, len(metadatas_list))

    if return_metadata:
        return {
            "chunks": len(metadatas_list),
            "pages": sorted({m.get("page_number", 0) for m in metadatas_list}),
            "title": metadatas_list[0].get("source_title", "") if metadatas_list else "",
            "author": metadatas_list[0].get("source_author", "") if metadatas_list else "",
        }
    return len(prepared)


def _catalog_markdown_hook(
    md_path: Path, collection_name: str, content_type: str, corpus: str, chunk_count: int,
    *, base_path: Path | None = None,
) -> None:
    """Register markdown document in catalog after indexing. Silently skipped if absent."""
    try:
        from nexus.catalog import Catalog
        from nexus.catalog.catalog import make_relative
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        # Derive title and year from frontmatter or filename
        title = md_path.stem
        year = 0
        try:
            text = md_path.read_text(encoding="utf-8")
            if text.startswith("---"):
                import re
                m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
                if m:
                    title = m.group(1).strip().strip('"').strip("'")
                # Extract year from created/date/accepted_date frontmatter
                for field in ("created", "date", "accepted_date"):
                    ym = re.search(rf"^{field}:\s*(.+)$", text, re.MULTILINE)
                    if ym:
                        dm = re.search(r"(\d{4})", ym.group(1))
                        if dm:
                            year = int(dm.group(1))
                            break
        except Exception:
            pass

        owner_name = corpus if corpus else "standalone-docs"
        rows = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE name = ?", (owner_name,)
        ).fetchone()
        if rows:
            from nexus.catalog.tumbler import Tumbler
            owner = Tumbler.parse(rows[0])
        else:
            owner = cat.register_owner(owner_name, "curator")

        fp = make_relative(md_path, base_path) if base_path else str(md_path)
        # Known TOCTOU window (Reviewer B/I-3): this stat happens AFTER the
        # markdown content was read for chunking earlier in the pipeline.
        # A concurrent write between content-read and this stat stores an
        # mtime newer than the indexed content, suppressing a subsequent
        # staleness flag. Proper fix requires threading source_mtime from
        # ``index_markdown``'s content-read point down to this hook. Filed
        # as a follow-up (nexus-vatx was scoped to ingest observability
        # surfaces, not data-consistency reorders).
        try:
            source_mtime = md_path.stat().st_mtime
        except OSError:
            source_mtime = 0.0
        cat.register(
            owner=owner, title=title, content_type=content_type,
            file_path=fp, physical_collection=collection_name,
            chunk_count=chunk_count, year=year,
            source_mtime=source_mtime,
        )
    except Exception:
        _log.debug("catalog_markdown_hook_failed", exc_info=True)


def index_markdown(
    md_path: Path,
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    content_type: str = "prose",
    base_path: Path | None = None,
) -> int | dict:
    """Index *md_path* into a T3 collection.

    By default the collection is ``docs__{corpus}``.  Pass *collection_name*
    to override (e.g. ``rdr__<repo>-<hash8>`` for RDR documents).

    YAML frontmatter fields (title, author, date) are stored as metadata.
    Returns the number of chunks indexed, or 0 if skipped.

    Pass *embed_fn* to override the default Voyage AI embedding (e.g. a local
    ONNX function for dry-run mode).  When *embed_fn* is provided the Voyage
    credential check is bypassed.

    Pass *force=True* to bypass the staleness check and always re-index.

    When *return_metadata* is True, returns a dict instead of an int::

        {"chunks": int, "sections": int}

    *sections* is the count of chunks with a non-empty ``section_title``
    (i.e. produced under a heading).  Default False preserves existing int behavior.

    When *base_path* is provided, ``source_path`` in T3 chunk metadata is
    stored relative to *base_path* instead of absolute (RDR-060).
    """
    from functools import partial

    from nexus.catalog.catalog import make_relative

    # Normalize to absolute so staleness checks are path-form-independent.
    md_path = md_path.resolve()

    col_name = collection_name if collection_name is not None else f"docs__{corpus}"
    chunk_fn = partial(_markdown_chunks, base_path=base_path) if base_path else _markdown_chunks
    source_key = make_relative(md_path, base_path) if base_path else None
    raw = _index_document(
        md_path, corpus, chunk_fn, t3=t3,
        collection_name=collection_name, embed_fn=embed_fn,
        force=force, return_metadata=return_metadata, on_progress=on_progress,
        source_key=source_key,
    )
    if not return_metadata:
        assert isinstance(raw, int)
        count = raw
        if count > 0:
            _catalog_markdown_hook(md_path, col_name, content_type, corpus, count, base_path=base_path)
        return count
    if not isinstance(raw, list):
        return {"chunks": 0, "sections": 0}
    metadatas: list[dict] = raw
    sections = sum(1 for m in metadatas if m.get("section_title", ""))
    if metadatas:
        _catalog_markdown_hook(md_path, col_name, content_type, corpus, len(metadatas), base_path=base_path)
    return {"chunks": len(metadatas), "sections": sections}


def batch_index_pdfs(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    *,
    force: bool = False,
    on_file: Callable[[Path, int, float], None] | None = None,
    extractor: str = "auto",
) -> dict[str, str]:
    """Index multiple PDFs sequentially, returning per-file status.

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"skipped"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.

    Pass *force=True* to bypass the staleness check on every file.

    *on_file*, if provided, is called after each file as
    ``on_file(path, chunks, elapsed_s)`` where *chunks* is the number of
    chunks upserted (0 for skipped/failed) and *elapsed_s* is wall time.
    """
    results: dict[str, str] = {}
    for path in paths:
        count: int = 0
        t0 = time.monotonic()
        try:
            raw = index_pdf(path, corpus, t3=t3, force=force, extractor=extractor)
            count = raw if isinstance(raw, int) else 0
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:
            _log.warning("batch_index_pdfs: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
        if on_file:
            on_file(path, count, time.monotonic() - t0)
    return results


def batch_index_markdowns(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    content_type: str = "prose",
    force: bool = False,
    on_file: Callable[[Path, int, float], None] | None = None,
    base_path: Path | None = None,
    embed_fn: EmbedFn | None = None,
) -> dict[str, str]:
    """Index multiple Markdown files sequentially, returning per-file status.

    Pass *collection_name* to override the default ``docs__{corpus}`` target
    (used for RDR collections).

    Pass *content_type* to set the catalog content type (default: "prose",
    use "rdr" for RDR documents).

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"skipped"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.

    Pass *force=True* to bypass the staleness check on every file.

    *on_file*, if provided, is called after each file as
    ``on_file(path, chunks, elapsed_s)`` where *chunks* is the number of
    chunks upserted (0 for skipped/failed) and *elapsed_s* is wall time.

    When *base_path* is provided, ``source_path`` in T3 chunk metadata and
    catalog ``file_path`` are stored relative to *base_path* (RDR-060).
    """
    results: dict[str, str] = {}
    for path in paths:
        count: int = 0
        t0 = time.monotonic()
        try:
            raw = index_markdown(path, corpus, t3=t3, collection_name=collection_name,
                                 content_type=content_type, force=force,
                                 base_path=base_path, embed_fn=embed_fn)
            count = raw if isinstance(raw, int) else 0
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:
            _log.warning("batch_index_markdowns: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
        if on_file:
            on_file(path, count, time.monotonic() - t0)
    return results
