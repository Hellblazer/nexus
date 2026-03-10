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
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import msgpack
import numpy as np
import structlog

from nexus.corpus import index_model_for_collection
from nexus.db.chroma_quotas import QUOTAS
from nexus.errors import EmbeddingModelMismatch, FormatVersionError
from nexus.retry import _chroma_with_retry

if TYPE_CHECKING:
    from nexus.db.t3 import T3Database

_log = structlog.get_logger(__name__)

#: The format version written by this implementation.
FORMAT_VERSION: int = 1

#: The maximum format version this importer can read.
#: If an export file's format_version > this, import MUST abort.
MAX_SUPPORTED_FORMAT_VERSION: int = 1

#: Pipeline version tag embedded in every export header.
_PIPELINE_VERSION: str = "nexus-1"


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


def export_collection(
    db: "T3Database",
    collection_name: str,
    output_path: Path,
    includes: tuple[str, ...] = (),
    excludes: tuple[str, ...] = (),
) -> dict:
    """Export *collection_name* to *output_path* in ``.nxexp`` format.

    Parameters
    ----------
    db:
        A connected T3Database instance.
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

    # Access the underlying ChromaDB collection directly to retrieve embeddings.
    col = db._client_for(collection_name).get_collection(collection_name)
    total_count = _chroma_with_retry(col.count)

    embedding_model = index_model_for_collection(collection_name)

    _log.info(
        "export_start",
        collection=collection_name,
        total_count=total_count,
        embedding_model=embedding_model,
        output=str(output_path),
    )

    # Phase 1: paginated retrieval of all records.
    page_size = QUOTAS.MAX_RECORDS_PER_WRITE
    all_records: list[dict] = []
    offset = 0

    while True:
        result = _chroma_with_retry(
            col.get,
            include=["documents", "metadatas", "embeddings"],
            limit=page_size,
            offset=offset,
        )
        page_ids = result["ids"]
        if not page_ids:
            break

        for rec_id, doc, meta, emb in zip(
            page_ids,
            result["documents"],
            result["metadatas"],
            result["embeddings"],
        ):
            source_path = (meta or {}).get("source_path")
            if not _apply_filter(source_path, includes, excludes):
                continue
            # Store embedding as bytes (float32 little-endian)
            emb_bytes: bytes = np.array(emb, dtype=np.float32).tobytes()
            all_records.append(
                {
                    "id": rec_id,
                    "document": doc,
                    "metadata": meta or {},
                    "embedding": emb_bytes,
                }
            )

        offset += len(page_ids)
        if len(page_ids) < page_size:
            break  # last page

    exported_count = len(all_records)
    embedding_dim = 0
    if all_records:
        first_emb = all_records[0]["embedding"]
        embedding_dim = len(first_emb) // 4  # float32 = 4 bytes each

    # Determine database_type from collection prefix.
    prefix = collection_name.split("__")[0] if "__" in collection_name else "knowledge"

    header: dict = {
        "format_version": FORMAT_VERSION,
        "collection_name": collection_name,
        "database_type": prefix,
        "embedding_model": embedding_model,
        "record_count": exported_count,
        "embedding_dim": embedding_dim,
        "exported_at": _now_iso(),
        "pipeline_version": _PIPELINE_VERSION,
    }

    # Phase 2: write header + gzip-compressed msgpack body.
    header_line = json.dumps(header).encode() + b"\n"

    with open(output_path, "wb") as f:
        f.write(header_line)
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            for record in all_records:
                gz.write(msgpack.packb(record, use_bin_type=True))

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
    db: "T3Database",
    input_path: Path,
    target_collection: str | None = None,
    remaps: list[tuple[str, str]] | None = None,
) -> dict:
    """Import a ``.nxexp`` file into T3.

    Parameters
    ----------
    db:
        A connected T3Database instance.
    input_path:
        Path to the ``.nxexp`` file to import.
    target_collection:
        Override the collection name from the export header.  Useful for
        renaming on import (e.g. ``code__newname``).
    remaps:
        List of ``(old_prefix, new_prefix)`` pairs applied to the
        ``source_path`` metadata field during import.

    Returns
    -------
    dict with keys: collection_name, imported_count, elapsed_seconds.

    Raises
    ------
    FormatVersionError:
        If the export file's format_version exceeds MAX_SUPPORTED_FORMAT_VERSION.
    EmbeddingModelMismatch:
        If the export's embedding_model does not match the target collection's
        expected index model.
    """
    t0 = time.monotonic()
    remaps = remaps or []

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

    if export_model != expected_model:
        raise EmbeddingModelMismatch(
            f"Embedding model mismatch — export uses '{export_model}' but "
            f"target collection '{collection_name}' requires '{expected_model}'. "
            "Import aborted. Re-index from source or export to a compatible "
            "collection prefix."
        )

    _log.info(
        "import_start",
        source_collection=source_collection,
        target_collection=collection_name,
        embedding_model=export_model,
        input=str(input_path),
    )

    # Phase 2: read records from gzip-compressed msgpack body.
    header_len = len(header_line)
    ids: list[str] = []
    documents: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict] = []

    with open(input_path, "rb") as f:
        f.seek(header_len)
        with gzip.GzipFile(fileobj=f, mode="rb") as gz:
            unpacker = msgpack.Unpacker(gz, raw=False, max_buffer_size=10 * 1024 * 1024)
            for record in unpacker:
                rec_id: str = record["id"]
                doc: str = record["document"]
                meta: dict = record["metadata"]
                emb_bytes: bytes = record["embedding"]

                # Apply path remapping to source_path if present.
                if remaps and "source_path" in meta:
                    meta = dict(meta)
                    meta["source_path"] = _apply_remap(meta["source_path"], remaps)

                # Reconstruct embedding from float32 bytes.
                emb: list[float] = np.frombuffer(emb_bytes, dtype=np.float32).tolist()

                ids.append(rec_id)
                documents.append(doc)
                embeddings.append(emb)
                metadatas.append(meta)

    imported_count = len(ids)

    # Phase 3: upsert in batches using the pre-computed embedding path.
    page_size = QUOTAS.MAX_RECORDS_PER_WRITE
    for start in range(0, imported_count, page_size):
        end = start + page_size
        db.upsert_chunks_with_embeddings(
            collection_name=collection_name,
            ids=ids[start:end],
            documents=documents[start:end],
            embeddings=embeddings[start:end],
            metadatas=metadatas[start:end],
        )
        _log.debug(
            "import_batch_written",
            start=start,
            end=min(end, imported_count),
            total=imported_count,
        )

    elapsed = time.monotonic() - t0

    _log.info(
        "import_complete",
        collection=collection_name,
        imported_count=imported_count,
        elapsed_seconds=round(elapsed, 2),
    )

    return {
        "collection_name": collection_name,
        "imported_count": imported_count,
        "elapsed_seconds": round(elapsed, 2),
    }
