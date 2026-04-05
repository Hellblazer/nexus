# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Collection consolidation: merge per-paper collections into corpus-level collections."""

from __future__ import annotations

from typing import Any

import structlog

from nexus.catalog.catalog import Catalog

_log = structlog.get_logger()


def merge_corpus(
    cat: Catalog,
    t3: Any,
    corpus: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Merge all collections for a corpus into a single target collection.

    Returns: {"merged": N, "would_merge": M, "errors": [...]}
    """
    source_entries = cat.by_corpus(corpus)
    if not source_entries:
        return {"merged": 0, "would_merge": 0, "errors": [f"No entries with corpus={corpus!r}"]}

    target_col_name = f"docs__{corpus}"

    if dry_run:
        for entry in source_entries:
            _log.info(
                "consolidation_dry_run",
                source=entry.physical_collection,
                target=target_col_name,
                tumbler=str(entry.tumbler),
            )
        return {"merged": 0, "would_merge": len(source_entries), "errors": []}

    # Create target collection
    target_col = t3.get_or_create_collection(target_col_name)

    merged = 0
    errors: list[str] = []

    for entry in source_entries:
        if entry.physical_collection == target_col_name:
            # Already in target — skip
            merged += 1
            continue

        try:
            src_col = t3.get_or_create_collection(entry.physical_collection)

            # Read all chunks with embeddings
            result = src_col.get(include=["documents", "metadatas", "embeddings"])
            actual_count = len(result["ids"])

            if actual_count == 0:
                _log.warning("consolidation_empty_source", collection=entry.physical_collection)
                cat.update(entry.tumbler, physical_collection=target_col_name)
                merged += 1
                continue

            # Chunk count sanity check
            if entry.chunk_count > 0 and abs(actual_count - entry.chunk_count) > entry.chunk_count * 0.1:
                _log.warning(
                    "consolidation_chunk_mismatch",
                    collection=entry.physical_collection,
                    catalog=entry.chunk_count,
                    actual=actual_count,
                )

            # Upsert into target (preserve original IDs)
            target_col.upsert(
                ids=result["ids"],
                documents=result["documents"],
                metadatas=result["metadatas"],
                embeddings=result["embeddings"],
            )

            # Update catalog pointer
            cat.update(entry.tumbler, physical_collection=target_col_name)

            # Delete source collection
            try:
                t3.delete_collection(entry.physical_collection)
            except Exception:
                _log.warning("consolidation_delete_failed", collection=entry.physical_collection, exc_info=True)

            merged += 1
            _log.info(
                "consolidation_merged",
                source=entry.physical_collection,
                target=target_col_name,
                chunks=actual_count,
            )

        except Exception as exc:
            errors.append(f"{entry.physical_collection}: {exc}")
            _log.error(
                "consolidation_failed",
                collection=entry.physical_collection,
                error=str(exc),
            )

    return {"merged": merged, "would_merge": 0, "errors": errors}
