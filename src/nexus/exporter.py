# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Collection export/import for T3 ChromaDB backup and migration.

Format: ``.nxexp`` (Nexus Export)
- Line 1: JSON header (newline-terminated) containing format metadata
- Remainder: gzip-compressed msgpack stream of records

Each record is a dict:
    {"id": str, "document": str, "metadata": dict, "embedding": bytes}

Embeddings are stored as little-endian float32 bytes (numpy tobytes).
"""
from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import msgpack
import numpy as np
import structlog

from nexus.corpus import embedding_model_for_collection_name, index_model_for_collection
from nexus.db.limits import QUOTAS
from nexus.db.local_ef import _MODEL_DIMS as _LOCAL_RAW_MODEL_DIMS
from nexus.db.local_ef import _MODEL_TOKENS as _LOCAL_MODEL_TOKENS
from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415 — same cross-module reuse pattern as commands/catalog_cmds/doctor.py
from nexus.errors import (
    EmbeddingDimensionMismatch,
    EmbeddingModelMismatch,
    FormatVersionError,
    NexusError,
)
from nexus.retry import _chroma_with_retry

if TYPE_CHECKING:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.db.t3 import T3Database
    from nexus.hook_registry import HookRegistry

_log = structlog.get_logger(__name__)

#: The format version written by this implementation.
FORMAT_VERSION: int = 1

#: The maximum format version this importer can read.
#: If an export file's format_version > this, import MUST abort.
MAX_SUPPORTED_FORMAT_VERSION: int = 1

#: Pipeline version tag embedded in every export header.
_PIPELINE_VERSION: str = "nexus-1"

#: Content-addressed chunk id length (``chunk_text_hash[:32]``, RDR-108 D1).
#: The Postgres ``chunks_<dim>`` tables enforce
#: ``CHECK (length(chash) = 32)`` -- any export record whose id doesn't
#: satisfy this on import must be re-derived (GH #1370 D1).
_CHASH_LEN: int = 32

#: Known embedding-model -> vector-dimension table (GH #1370 D2). Reused
#: from the local-mode table (``nexus.db.local_ef``) rather than
#: duplicated, keyed by the RDR-109 collection-name token; Voyage models
#: aren't in that table (cloud-only) so they're listed explicitly here.
_MODEL_DIMENSIONS: dict[str, int] = {
    "voyage-3": 1024,
    "voyage-code-3": 1024,
    "voyage-context-3": 1024,
    **{
        token: _LOCAL_RAW_MODEL_DIMS[raw]
        for raw, token in _LOCAL_MODEL_TOKENS.items()
    },
}


def _rehash_nonconformant_id(rec_id: str, doc: str) -> tuple[str, str]:
    """Return ``(new_id, full_hash)`` for an export record id that isn't
    the conformant ``_CHASH_LEN``-char content hash (GH #1370 D1).

    Content-addressed derivation matches production indexing
    (``chunk_text_hash[:32]``): hash the document text when present.
    Vector-only entries (``document == ""``) have no meaningful content
    to hash, so the *old* id is hashed instead -- deterministic and
    stable across repeated re-imports of the same legacy file.
    """
    basis = doc if doc else rec_id
    full_hash = hashlib.sha256(basis.encode()).hexdigest()
    return full_hash[:_CHASH_LEN], full_hash


#: Substrings that indicate an upsert failure is a chash/constraint
#: conflict rather than an unrelated error (GH #1370 D3 cheap-win UX).
_CONSTRAINT_HINT_KEYWORDS: tuple[str, ...] = (
    "constraint", "integrity", "duplicate", "chash", "length(",
)


def _upsert_with_hint(
    db: "T3Database | HttpVectorClient",
    collection_name: str,
    ids: list[str],
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
    hooks: "HookRegistry",
) -> None:
    """Upsert a batch and fire post-store hook chains, wrapping
    constraint-violation errors with an actionable hint (chash length /
    --skip-existing) instead of the raw opaque backend error (GH #1370 D3).

    Hook-firing lives in this function (rather than the caller) so the
    nexus-9099 drift guard (``test_every_cli_t3_write_function_fires_
    store_chains``) sees both the T3 write and the hook-chain fire in
    the same function body.
    """
    try:
        db.upsert_chunks_with_embeddings(
            collection_name=collection_name,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if any(keyword in msg for keyword in _CONSTRAINT_HINT_KEYWORDS):
            raise NexusError(
                f"{exc}\nHint: this looks like a chunk-id constraint "
                f"conflict in collection {collection_name!r} -- a "
                "non-conformant legacy chunk id or a duplicate key. If "
                "you're re-running a partial import, retry with "
                "--skip-existing."
            ) from exc
        raise
    _fire_store_chains_grouped_by_doc(
        ids, collection_name, documents, embeddings, metadatas, hooks,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _apply_filter(
    source_path: str | None,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
) -> bool:
    """Return True if this record should be included in the export.

    Entries without a source_path (e.g. nx store put entries) pass
    unconditionally regardless of include/exclude patterns.

    Include logic (OR): if any pattern matches, the entry is included.
    Exclude logic (AND): if any pattern matches, the entry is excluded.
    Excludes are evaluated after includes.
    """
    if source_path is None:
        # No source_path — pass through unconditionally.
        return True

    if includes:
        if not any(fnmatch.fnmatch(source_path, p) for p in includes):
            return False

    if excludes:
        if any(fnmatch.fnmatch(source_path, p) for p in excludes):
            return False

    return True


def _apply_remap(source_path: str, remaps: list[tuple[str, str]]) -> str:
    """Apply the first matching prefix remap to *source_path*.

    Each element of *remaps* is a ``(old_prefix, new_prefix)`` pair.
    The first matching pair wins; subsequent pairs are not evaluated.
    """
    for old, new in remaps:
        if source_path.startswith(old):
            return new + source_path[len(old):]
    return source_path


def _fire_store_chains_grouped_by_doc(
    ids: list[str],
    collection_name: str,
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
    hooks: "HookRegistry",
) -> None:
    """nexus-8g79.1: fire post-store chains per-doc so the manifest
    hook can attribute chunks to the right catalog tumbler.

    Pre-RDR-108-Phase-3 exports carry ``meta["doc_id"]`` per chunk
    (the tumbler string was stored in chunk metadata at write-time).
    Post-Phase-3 exports do NOT carry it; for those chunks the group
    key is the empty string and the manifest hook short-circuits —
    accepted limitation until export-format extension carries the
    catalog manifest sidecar. The grouping handles the legacy path
    correctly and degrades to the existing no-doc_id behaviour for
    Phase-3 exports.

    Records are partitioned by ``meta.get("doc_id", "")``; each group
    fires its own ``HookRegistry.fire_store_chains`` call with that
    group key as ``catalog_doc_id`` so the manifest hook attributes
    the chunks correctly. Insertion order within each group is
    preserved so ``chunk_index`` re-injection sees a stable position.
    """
    from collections import defaultdict  # noqa: PLC0415 — stdlib import kept branch-local

    groups: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(metadatas):
        groups[(m or {}).get("doc_id", "")].append(i)

    for doc_id_key, indices in groups.items():
        sub_ids = [ids[i] for i in indices]
        sub_docs = [documents[i] for i in indices]
        sub_embs = [embeddings[i] for i in indices] if embeddings else None
        sub_metas = [metadatas[i] for i in indices]
        sub_paths = [(metadatas[i] or {}).get("source_path", "") for i in indices]
        hooks.fire_store_chains(
            sub_ids, collection_name, sub_docs,
            source_paths=sub_paths,
            embeddings=sub_embs,
            metadatas=sub_metas,
            catalog_doc_id=doc_id_key,
        )


def export_collection(
    db: "T3Database | HttpVectorClient",
    collection_name: str,
    output_path: Path,
    includes: tuple[str, ...] = (),
    excludes: tuple[str, ...] = (),
) -> dict:
    """Export *collection_name* to *output_path* in ``.nxexp`` format.

    Parameters
    ----------
    db:
        A connected T3Database or HttpVectorClient instance (GH #1373:
        production's ``make_t3()`` returns ``HttpVectorClient`` in both
        local and cloud mode -- this must work against either backend's
        ``get_collection`` / ``get_embeddings`` surface, never a
        backend-private attribute).
    collection_name:
        Fully-qualified collection name (e.g. ``code__myrepo``).
    output_path:
        Destination file path.  Parent directories must exist.
    includes:
        Glob patterns matched against ``source_path`` metadata.
        If non-empty, only entries whose source_path matches at least one
        pattern are exported.  Entries without source_path pass through.
    excludes:
        Glob patterns matched against ``source_path`` metadata.
        Entries whose source_path matches any pattern are excluded.
        Entries without source_path are never excluded.

    Returns
    -------
    dict with keys: collection_name, record_count, exported_count,
    file_bytes, elapsed_seconds, output_path.
    """
    t0 = time.monotonic()

    # Backend-neutral collection handle (GH #1373): db.get_collection()
    # resolves to a real chromadb.Collection on T3Database or a
    # _ServiceCollectionStub on HttpVectorClient -- never reach for the
    # Chroma-only db._client_for() private method here.
    col = db.get_collection(collection_name)
    total_count = _chroma_with_retry(col.count)

    embedding_model = index_model_for_collection(collection_name)

    _log.info(
        "export_start",
        collection=collection_name,
        total_count=total_count,
        embedding_model=embedding_model,
        output=str(output_path),
    )

    # Determine database_type from collection prefix.
    prefix = collection_name.split("__")[0] if "__" in collection_name else "knowledge"

    # Write header (record_count/embedding_dim filled after streaming).
    # The header is written first so the file is valid even during writing.
    # record_count and embedding_dim are informational metadata (not validated
    # on import) and are updated in a final rewrite pass after streaming.
    header: dict = {
        "format_version": FORMAT_VERSION,
        "collection_name": collection_name,
        "database_type": prefix,
        "embedding_model": embedding_model,
        "record_count": total_count,  # estimate; refined below
        "embedding_dim": 0,           # informational only; not validated on import
        "exported_at": _now_iso(),
        "pipeline_version": _PIPELINE_VERSION,  # informational; not checked on import
    }
    header_line = json.dumps(header).encode() + b"\n"

    # Stream records page-by-page: paginate ChromaDB, filter, and write each
    # page directly to the gzip stream.  This avoids accumulating all records
    # in memory (031-I1).
    page_size = QUOTAS.MAX_RECORDS_PER_WRITE
    exported_count = 0
    embedding_dim = 0

    with open(output_path, "wb") as f:
        f.write(header_line)
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            offset = 0
            while True:
                result = _chroma_with_retry(
                    col.get,
                    include=["documents", "metadatas"],
                    limit=page_size,
                    offset=offset,
                )
                page_ids = result["ids"]
                if not page_ids:
                    break

                # Embeddings are fetched via the backend-neutral
                # get_embeddings() surface rather than
                # col.get(include=["embeddings"]): the service-mode
                # collection stub (_ServiceCollectionStub) never returns
                # embeddings through its get() envelope regardless of
                # `include` (GH #1373), so requesting them there would
                # silently export zero-length vectors on the HttpVectorClient
                # path. get_embeddings() reorders its response to match
                # request order on both backends.
                emb_array = db.get_embeddings(collection_name, page_ids)
                if emb_array.shape[0] != len(page_ids):
                    raise NexusError(
                        f"Export failed: collection {collection_name!r} page "
                        f"at offset {offset} returned {len(page_ids)} chunk "
                        f"ids but only {emb_array.shape[0]} stored "
                        "embeddings -- a chunk without a vector indicates a "
                        "data-integrity issue; re-index the collection "
                        "before exporting."
                    )

                for rec_id, doc, meta, emb in zip(
                    page_ids,
                    result["documents"],
                    result["metadatas"],
                    emb_array,
                ):
                    source_path = (meta or {}).get("source_path")
                    if not _apply_filter(source_path, includes, excludes):
                        continue
                    emb_bytes: bytes = np.asarray(emb, dtype=np.float32).tobytes()
                    if embedding_dim == 0 and emb_bytes:
                        embedding_dim = len(emb_bytes) // 4
                    gz.write(msgpack.packb(
                        {
                            "id": rec_id,
                            "document": doc,
                            "metadata": meta or {},
                            "embedding": emb_bytes,
                        },
                        use_bin_type=True,
                    ))
                    exported_count += 1

                offset += len(page_ids)
                if len(page_ids) < page_size:
                    break  # last page

    file_bytes = output_path.stat().st_size
    elapsed = time.monotonic() - t0

    _log.info(
        "export_complete",
        collection=collection_name,
        exported_count=exported_count,
        file_bytes=file_bytes,
        elapsed_seconds=round(elapsed, 2),
    )

    return {
        "collection_name": collection_name,
        "record_count": total_count,
        "exported_count": exported_count,
        "file_bytes": file_bytes,
        "elapsed_seconds": round(elapsed, 2),
        "output_path": str(output_path),
    }


def import_collection(
    db: "T3Database | HttpVectorClient",
    input_path: Path,
    target_collection: str | None = None,
    remaps: list[tuple[str, str]] | None = None,
    *,
    hooks: "HookRegistry | None" = None,
    assume_model: str | None = None,
    skip_existing: bool = False,
) -> dict:
    """Import a ``.nxexp`` file into T3.

    Parameters
    ----------
    db:
        A connected T3Database or HttpVectorClient instance.
    input_path:
        Path to the ``.nxexp`` file to import.
    target_collection:
        Override the collection name from the export header.  Useful for
        renaming on import (e.g. ``code__newname``).
    remaps:
        List of ``(old_prefix, new_prefix)`` pairs applied to the
        ``source_path`` metadata field during import.
    assume_model:
        Override the export header's declared ``embedding_model`` for both
        the model-mismatch gate and the dimension sanity check (GH #1370
        D2). Pre-migration exports can carry a wrong header label; this
        lets the caller supply the true model instead of trusting it.
    skip_existing:
        If True, records whose id already exists in the target collection
        are skipped rather than overwritten (GH #1370 D3). Useful for
        resuming a partially-completed import.

    Returns
    -------
    dict with keys: collection_name, imported_count, skipped_count,
    rehashed_count, elapsed_seconds.

    Raises
    ------
    FormatVersionError:
        If the export file's format_version exceeds MAX_SUPPORTED_FORMAT_VERSION.
    EmbeddingModelMismatch:
        If the export's embedding_model does not match the target collection's
        expected index model.
    EmbeddingDimensionMismatch:
        If the declared model's expected dimensionality doesn't match the
        actual vectors found in the file (a mislabeled pre-migration export).
    """
    t0 = time.monotonic()
    remaps = remaps or []
    if hooks is None:
        from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        hooks = HookRegistry()
        install_default_hooks(hooks)

    # Phase 1: read and validate header.
    with open(input_path, "rb") as f:
        header_line = f.readline()

    header: dict = json.loads(header_line.decode())

    file_format_version: int = header.get("format_version", 0)
    if file_format_version > MAX_SUPPORTED_FORMAT_VERSION:
        raise FormatVersionError(
            f"Export file format_version={file_format_version} exceeds "
            f"MAX_SUPPORTED_FORMAT_VERSION={MAX_SUPPORTED_FORMAT_VERSION}. "
            "Upgrade Nexus to import this file."
        )

    try:
        source_collection: str = header["collection_name"]
    except KeyError:
        raise FormatVersionError(
            f"Export file {input_path!r} is missing required header key 'collection_name'. "
            "The file may be corrupt or was produced by an incompatible version."
        )
    collection_name: str = target_collection or source_collection
    try:
        export_model: str = header["embedding_model"]
    except KeyError:
        raise FormatVersionError(
            f"Export file {input_path!r} is missing required header key 'embedding_model'. "
            "The file may be corrupt or was produced by an incompatible version."
        )
    expected_model: str = index_model_for_collection(collection_name)

    # GH #1370 D2: --assume-model overrides the header's (possibly wrong)
    # declared model for this gate. It corrects a mislabeled export; it does
    # NOT bypass the safety check for a genuinely incompatible collection.
    effective_model: str = assume_model if assume_model is not None else export_model

    if effective_model != expected_model:
        label = "assumed model" if assume_model is not None else "export uses"
        raise EmbeddingModelMismatch(
            f"Embedding model mismatch — {label} '{effective_model}' but "
            f"target collection '{collection_name}' requires '{expected_model}'. "
            "Import aborted. Re-index from source or export to a compatible "
            "collection prefix."
        )

    _log.info(
        "import_start",
        source_collection=source_collection,
        target_collection=collection_name,
        embedding_model=export_model,
        assume_model=assume_model,
        skip_existing=skip_existing,
        input=str(input_path),
    )

    # GH #1370 D2: the dims sanity check only applies when the declared
    # model is authoritative -- either the target name is RDR-103
    # conformant (four-segment names embed the model token directly,
    # per ``embedding_model_for_collection_name``) or the caller
    # explicitly opted in via ``--assume-model`` (which must still be
    # validated, per spec, even for a legacy target). For a legacy
    # two-segment target, ``expected_model`` is only ever a prefix-based
    # *guess* (``voyage_model_for_collection``) -- local-mode installs
    # legitimately write bge/minilm vectors under 2-segment names, and
    # the guess has never been reliable there. Enforcing the dims check
    # unconditionally would block that pre-existing, unaffected
    # workflow; scope it to the cases where GH #1370 D2 actually bites
    # (migrating into a properly self-declaring conformant collection).
    enforce_dims_check: bool = (
        assume_model is not None
        or embedding_model_for_collection_name(collection_name) is not None
    )

    # Stream records from gzip-compressed msgpack body and upsert in batches.
    # Single file open: read header, then gzip body from the same handle
    # (eliminates the TOCTOU window of opening the file twice — 031-S8).
    # Records are upserted as each batch fills, avoiding accumulating all
    # records in memory (031-I1).
    page_size = QUOTAS.MAX_RECORDS_PER_WRITE
    imported_count = 0
    skipped_count = 0
    rehashed_count = 0
    ids: list[str] = []
    documents: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict] = []

    # GH #1370 D1: legacy (pre-migration) exports carry non-conformant
    # chunk ids that fail the Postgres ``chash`` length constraint on
    # import. Bypass-schema collections (``taxonomy__*``) use their own
    # programmatic id scheme (not content-derived) and must NOT be
    # rehashed -- that would break their intentional stable identifiers.
    rehash_ids = not collection_name.startswith(_BYPASS_SCHEMA_PREFIXES)

    # CLI review: infer the expected embedding byte-size from the first
    # record and reject any subsequent record whose embedding doesn't
    # match. This catches truncation/corruption mid-file without
    # hard-coding a model-specific dim (tests use 384-dim MiniLM;
    # production uses 1024-dim Voyage). Also sanity-checks that the
    # byte-size is a multiple of 4 (float32).
    expected_emb_bytes: int | None = None

    def _filter_existing(
        batch_ids: list[str],
        batch_docs: list[str],
        batch_embs: list[list[float]],
        batch_metas: list[dict],
    ) -> tuple[list[str], list[str], list[list[float]], list[dict], int]:
        """Drop records whose id already exists in the target collection
        (GH #1370 D3, ``--skip-existing``). No-op unless requested."""
        if not skip_existing:
            return batch_ids, batch_docs, batch_embs, batch_metas, 0
        existing = db.existing_ids(collection_name, batch_ids)
        if not existing:
            return batch_ids, batch_docs, batch_embs, batch_metas, 0
        keep = [i for i, rid in enumerate(batch_ids) if rid not in existing]
        return (
            [batch_ids[i] for i in keep],
            [batch_docs[i] for i in keep],
            [batch_embs[i] for i in keep],
            [batch_metas[i] for i in keep],
            len(batch_ids) - len(keep),
        )

    with open(input_path, "rb") as f:
        f.readline()  # skip header (already parsed above)
        with gzip.GzipFile(fileobj=f, mode="rb") as gz:
            unpacker = msgpack.Unpacker(gz, raw=False, max_buffer_size=10 * 1024 * 1024)
            for record in unpacker:
                rec_id: str = record["id"]
                # Vector-only entries (e.g. ``taxonomy__centroids``) round-trip
                # ``document=None``. Coerce to empty string so the downstream
                # write path's byte-length checks don't trip on ``None.encode()``
                # (nexus-fxc1).
                doc: str = record["document"] or ""
                meta: dict = dict(record["metadata"])
                emb_bytes: bytes = record["embedding"]

                if expected_emb_bytes is None:
                    if len(emb_bytes) == 0 or len(emb_bytes) % 4 != 0:
                        raise FormatVersionError(
                            f"Export file {input_path!r} has a malformed "
                            f"embedding for record {rec_id!r}: "
                            f"{len(emb_bytes)} bytes is not a multiple of 4 "
                            "(float32). File may be corrupt."
                        )
                    expected_emb_bytes = len(emb_bytes)

                    # GH #1370 D2: sanity-check the declared model's dims
                    # against the actual first-record vector (scope: see
                    # ``enforce_dims_check`` above). Unknown models (not
                    # in _MODEL_DIMENSIONS) skip silently -- can't
                    # validate what we don't have a table entry for.
                    if enforce_dims_check:
                        actual_dims = expected_emb_bytes // 4
                        declared_dims = _MODEL_DIMENSIONS.get(effective_model)
                        if declared_dims is not None and declared_dims != actual_dims:
                            raise EmbeddingDimensionMismatch(
                                declared_model=effective_model,
                                declared_dims=declared_dims,
                                actual_dims=actual_dims,
                                collection=collection_name,
                                assumed=assume_model is not None,
                            )
                elif len(emb_bytes) != expected_emb_bytes:
                    raise FormatVersionError(
                        f"Export file {input_path!r} contains an embedding "
                        f"of {len(emb_bytes)} bytes for record {rec_id!r}, "
                        f"expected {expected_emb_bytes} bytes (same as the "
                        "first record). File may be truncated or corrupt."
                    )

                if rehash_ids and len(rec_id) != _CHASH_LEN:
                    new_id, full_hash = _rehash_nonconformant_id(rec_id, doc)
                    if "chunk_text_hash" in meta:
                        meta["chunk_text_hash"] = full_hash
                    rec_id = new_id
                    rehashed_count += 1

                if remaps and "source_path" in meta:
                    meta["source_path"] = _apply_remap(meta["source_path"], remaps)

                emb: list[float] = np.frombuffer(emb_bytes, dtype=np.float32).tolist()

                ids.append(rec_id)
                documents.append(doc)
                embeddings.append(emb)
                metadatas.append(meta)

                # Flush batch when page_size reached.
                if len(ids) >= page_size:
                    f_ids, f_docs, f_embs, f_metas, skipped = _filter_existing(
                        ids, documents, embeddings, metadatas,
                    )
                    skipped_count += skipped
                    if f_ids:
                        _upsert_with_hint(
                            db, collection_name, f_ids, f_docs, f_embs, f_metas, hooks,
                        )
                    imported_count += len(f_ids)
                    _log.debug("import_batch_written", count=len(f_ids), total_so_far=imported_count)
                    ids, documents, embeddings, metadatas = [], [], [], []

    # Flush remaining records.
    if ids:
        f_ids, f_docs, f_embs, f_metas, skipped = _filter_existing(
            ids, documents, embeddings, metadatas,
        )
        skipped_count += skipped
        if f_ids:
            _upsert_with_hint(db, collection_name, f_ids, f_docs, f_embs, f_metas, hooks)
        imported_count += len(f_ids)

    elapsed = time.monotonic() - t0

    if rehashed_count:
        _log.info(
            "import_rehashed_nonconformant_ids",
            collection=collection_name,
            rehashed_count=rehashed_count,
        )

    _log.info(
        "import_complete",
        collection=collection_name,
        imported_count=imported_count,
        skipped_count=skipped_count,
        rehashed_count=rehashed_count,
        elapsed_seconds=round(elapsed, 2),
    )

    return {
        "collection_name": collection_name,
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "rehashed_count": rehashed_count,
        "elapsed_seconds": round(elapsed, 2),
    }
