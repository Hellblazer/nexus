# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical collection-delete cascade (RDR-144 P4 follow-up nexus-prgf4).

Deleting a T3 collection leaves derived state behind unless every dependent
projection is purged in lockstep: taxonomy topics/assignments/links, the
chash index, streaming-pipeline rows, and catalog document + projection rows.
``nx collection delete`` (commands/collection.py) has always run this cascade;
the RDR-144 384->768 migration originally called bare ``db.delete_collection``
and so orphaned the old collection's catalog rows (doctor FAILs:
t3-vs-catalog + collections-drift).

This module is the single source of truth for that cascade so both the delete
verb and the migration leave the same clean state. Pure (no ``click``):
returns counts; callers render. Every step is best-effort so a partial
environment (no catalog, no daemon) still deletes what it can.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

_log = structlog.get_logger(__name__)


@dataclass
class CascadeCounts:
    """Per-step outcome of :func:`purge_collection_cascade`."""

    t3_absent: bool = False
    taxonomy: dict[str, int] | None = None
    chash_deleted: int = 0
    pipeline_rows_deleted: int = 0
    catalog_docs_deleted: int = 0
    catalog_projection_deleted: int = 0


def purge_collection_cascade(db: object, name: str) -> CascadeCounts:
    """Delete T3 collection *name* and cascade-purge all derived state.

    ``db`` is a ``T3Database`` (or any object exposing
    ``delete_collection(name)``). The T3 delete tolerates an already-absent
    collection (``t3_absent=True``) and still runs the cascade so a prior
    half-delete is cleaned up.
    """
    from chromadb.errors import NotFoundError as _ChromaNotFoundError

    counts = CascadeCounts()

    try:
        db.delete_collection(name)  # type: ignore[attr-defined]
    except _ChromaNotFoundError:
        counts.t3_absent = True

    # Taxonomy + chash index, routed through the T2 daemon (single-writer).
    try:
        from nexus.mcp_infra import t2_index_write

        def _cascade(store):
            return (
                store.taxonomy.purge_collection(name),
                store.chash_index.delete_collection(name),
            )

        counts.taxonomy, counts.chash_deleted = t2_index_write(_cascade)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        _log.warning("purge_cascade_t2_failed", collection=name, error=str(exc))

    # Streaming-pipeline rows (otherwise the next index returns skip/0-chunks).
    try:
        from nexus.pipeline_buffer import PIPELINE_DB_PATH, PipelineDB

        counts.pipeline_rows_deleted = PipelineDB(
            PIPELINE_DB_PATH
        ).delete_pipeline_data_for_collection(name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("purge_cascade_pipeline_failed", collection=name, error=str(exc))

    # Catalog: document rows pointing at the gone collection + projection row.
    try:
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if Catalog.is_initialized(cat_path):
            cat = Catalog(cat_path, cat_path / ".catalog.db")
            orphan_tumblers = [
                row[0]
                for row in cat._db.execute(
                    "SELECT tumbler FROM documents WHERE physical_collection = ?",
                    (name,),
                ).fetchall()
            ]
            for tumbler in orphan_tumblers:
                try:
                    if cat.delete_document(tumbler):
                        counts.catalog_docs_deleted += 1
                except Exception:
                    _log.debug(
                        "purge_cascade_document_failed", tumbler=tumbler, exc_info=True
                    )
            if cat.delete_collection_projection(name, reason="collection purge"):
                counts.catalog_projection_deleted = 1
    except Exception as exc:  # noqa: BLE001
        _log.warning("purge_cascade_catalog_failed", collection=name, error=str(exc))

    return counts
