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

from dataclasses import dataclass, field

import structlog

_log = structlog.get_logger(__name__)


@dataclass
class CascadeCounts:
    """Per-step outcome of :func:`purge_collection_cascade`.

    ``failures`` carries a human-readable message for each derived-state step
    that raised. The physical T3 delete still happens (or already happened),
    but a non-empty ``failures`` means orphan rows may remain; callers should
    surface it (``nx collection delete`` echoes to stderr, the migration folds
    it into its outcome). Silence here was the regression both P4-follow-up
    reviewers flagged.
    """

    t3_absent: bool = False
    taxonomy: dict[str, int] | None = None
    chash_deleted: int = 0
    pipeline_rows_deleted: int = 0
    catalog_docs_deleted: int = 0
    catalog_projection_deleted: int = 0
    failures: list[str] = field(default_factory=list)


def _purge_pipeline_db(name: str, counts: CascadeCounts) -> CascadeCounts:
    """Delete streaming-pipeline rows for *name* (client-side in BOTH modes — the
    pipeline buffer is local SQLite per RDR-164 CA-4). Best-effort; records the
    count on success and a failure message otherwise. Returns *counts*."""
    try:
        from nexus.pipeline_buffer import PIPELINE_DB_PATH, PipelineDB  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        counts.pipeline_rows_deleted = PipelineDB(
            PIPELINE_DB_PATH
        ).delete_pipeline_data_for_collection(name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("purge_cascade_pipeline_failed", collection=name, error=str(exc))
        counts.failures.append(f"pipeline-state cleanup failed: {exc}")
    return counts


def purge_collection_cascade(db: object, name: str) -> CascadeCounts:
    """Delete T3 collection *name* and cascade-purge all derived state.

    ``db`` is a ``T3Database`` (or any object exposing
    ``delete_collection(name)``). The T3 delete tolerates an already-absent
    collection (``t3_absent=True``) and still runs the cascade so a prior
    half-delete is cleaned up.
    """
    counts = CascadeCounts()

    from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if storage_backend_for("catalog") == StorageBackend.SERVICE:
        # RDR-164 P2: in service mode the entire in-Postgres cascade (T3 chunks,
        # chash index, taxonomy topics/assignments/centroids, aspect family, and
        # the catalog documents + registry row) is ONE atomic transaction on the
        # Java service. Fold it into a single call instead of fanning out to
        # per-store endpoints (which left orphans — nexus-tquoj/cugrk). Only
        # pipeline.db (below) stays client-side (CA-4).
        try:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

            client = make_catalog_reader()
            if client is None:  # service mode always returns a client; guard for a clear error
                raise RuntimeError("catalog service client unavailable")
            deleted = client.delete_collection(name)  # type: ignore[attr-defined]
            # Preserve the local fan-out's taxonomy dict shape ({topics, assignments,
            # links, meta}) so the CLI render (commands/collection.py) does not KeyError;
            # add centroids (purged here, absent from the local path).
            counts.taxonomy = {
                "topics": deleted.get("topics", 0),
                "assignments": deleted.get("topic_assignments", 0),
                "links": 0,
                "meta": deleted.get("taxonomy_meta", 0),
                "centroids": (
                    deleted.get("taxonomy_centroids_384", 0)
                    + deleted.get("taxonomy_centroids_768", 0)
                    + deleted.get("taxonomy_centroids_1024", 0)
                ),
            }
            counts.chash_deleted = deleted.get("chash_index", 0)
            counts.catalog_docs_deleted = deleted.get("catalog_documents", 0)
            counts.catalog_projection_deleted = deleted.get("catalog_collections", 0)
        except Exception as exc:  # noqa: BLE001 — best-effort, atomic on the service side
            _log.warning("purge_cascade_service_failed", collection=name, error=str(exc))
            counts.failures.append(f"service deleteCollection failed: {exc}")
        return _purge_pipeline_db(name, counts)

    # ── Local (sqlite/Chroma) mode: client-side fan-out (CA-5) ───────────────
    from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: PLC0415 — heavy/optional dep deferred

    try:
        db.delete_collection(name)  # type: ignore[attr-defined]
    except _ChromaNotFoundError:
        counts.t3_absent = True

    # Taxonomy + chash index, routed through the T2 daemon (single-writer).
    try:
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        def _cascade(store):
            return (
                store.taxonomy.purge_collection(name),
                store.chash_index.delete_collection(name),
            )

        counts.taxonomy, counts.chash_deleted = t2_index_write(_cascade)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        _log.warning("purge_cascade_t2_failed", collection=name, error=str(exc))
        counts.failures.append(f"taxonomy/chash cascade failed: {exc}")

    # Streaming-pipeline rows (otherwise the next index returns skip/0-chunks).
    _purge_pipeline_db(name, counts)

    # Catalog: document rows pointing at the gone collection + projection row.
    try:
        from nexus.catalog.catalog import Catalog  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.config import catalog_path  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

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
                except Exception:  # noqa: BLE001 — per-document purge failure logged at debug, cascade continues
                    _log.debug(
                        "purge_cascade_document_failed", tumbler=tumbler, exc_info=True
                    )
            if cat.delete_collection_projection(name, reason="collection purge"):
                counts.catalog_projection_deleted = 1
    except Exception as exc:  # noqa: BLE001
        _log.warning("purge_cascade_catalog_failed", collection=name, error=str(exc))
        counts.failures.append(f"catalog cascade failed: {exc}")

    return counts
