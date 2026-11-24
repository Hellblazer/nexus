# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical collection-delete cascade (RDR-144 P4 follow-up nexus-prgf4).

Deleting a T3 collection leaves derived state behind unless every dependent
projection is purged in lockstep: taxonomy topics/assignments/links, the
chash index, streaming-pipeline rows, and catalog document + projection rows.
``nx collection delete`` (commands/collection.py) has always run this cascade;
the RDR-144 384->768 migration originally called bare ``db.delete_collection``
and so orphaned the old collection's catalog rows (doctor FAILs:
t3-vs-catalog + collections-drift).

This module is the single source of truth for that cascade; its callers are
``nx collection delete``, ``prune`` and ``reindex``. Pure (no ``click``):
returns counts; callers render. Every step is best-effort so a partial
environment (no catalog, no daemon) still deletes what it can.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

_log = structlog.get_logger(__name__)


@dataclass
class CascadeCounts:
    """Per-step outcome of :func:`purge_collection_cascade`.

    ``failures`` carries a human-readable message for each derived-state step
    that raised. The physical T3 delete still happens (or already happened),
    but a non-empty ``failures`` means orphan rows may remain; callers should
    surface it (``nx collection delete``, ``prune`` and ``reindex`` echo it to
    stderr). Silence here was the regression both P4-follow-up
    reviewers flagged.
    """

    t3_absent: bool = False
    taxonomy: dict[str, int] | None = None
    chash_deleted: int = 0
    pipeline_rows_deleted: int = 0
    catalog_docs_deleted: int = 0
    catalog_projection_deleted: int = 0
    #: Rows the engine took from the origin's ``quarantine-`` siblings in the same
    #: transaction (nexus-wbfpw.68/.71; one ``collection_delete_quarantine``
    #: gc_audit row per sibling), or, when the deleted name is itself a
    #: ``quarantine-*`` collection, the rows that collection held (one
    #: ``quarantine_collection_delete`` gc_audit row). 0 from an engine that
    #: predates the key, and 0 under ``keep_quarantine``.
    quarantine_chunks_deleted: int = 0
    #: T3 chunk rows the engine deleted from the collection itself (its ``chunks`` count).
    chunks_deleted: int = 0
    #: True when the engine's single-transaction deleteCollection raised (or
    #: timed out): the delete is UNCONFIRMED, not merely missing derived rows.
    #: Callers must not report the collection as deleted (nexus-oq5gd).
    service_delete_failed: bool = False
    #: ``{"collection", "job_id", "message", "state"}`` (``state`` is "" when the engine's text did not name one) when the engine refused the delete with 409 ``collection_busy``:
    #: a re-embed job in a non-terminal state holds the collection, and nothing was removed (RDR-229 P3.5a row 14,
    #: engine 0.1.202). Set with ``service_delete_failed``; a caller that can say why reads this one.
    collection_busy: dict[str, str] | None = None
    failures: list[str] = field(default_factory=list)


def collection_busy_of(exc: BaseException, name: str) -> dict[str, str] | None:
    """The engine's 409 ``collection_busy`` answer to a delete, as ``{collection, job_id, message, state}``, else None.

    The catalog client raises ``httpx.HTTPStatusError`` and the vector client ``VectorServiceError``; both carry the
    decoded body, so the answer is read by its ``reason``, never by the status alone (a 409 is also the
    label-ambiguous and superseded refusals)."""
    from nexus.db.engine_reasons import COLLECTION_BUSY_REASON, error_reason  # noqa: PLC0415 — deferred import

    body: object = getattr(exc, "engine_body", None)
    status: object = getattr(exc, "code", None)
    response = getattr(exc, "response", None)
    if body is None and response is not None:
        status = getattr(response, "status_code", None)
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 — an unreadable body is not the typed answer
            body = None
    if status != 409 or not isinstance(body, dict) or error_reason(body) != COLLECTION_BUSY_REASON:
        return None
    job_id = body.get("job_id")
    message = str(body.get("error") or "")
    # The body has no state field (engine 0.1.202); its text reads "...: re-embed job <id> (<state>) is moving documents
    # from A to B. Abort or finish the job first." The state is read from that only when it is one the fence holds.
    from nexus.reembed_job import LOCK_STATES  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    state = body.get("state")
    if not isinstance(state, str) or state not in LOCK_STATES:
        found = re.search(r"re-embed job \S+ \((\w+)\) is moving", message)
        state = found.group(1) if found and found.group(1) in LOCK_STATES else ""
    return {
        "collection": str(body.get("collection") or name),
        "job_id": str(job_id) if job_id else "",
        "message": message,
        "state": state,
    }


def _purge_pipeline_db(name: str, counts: CascadeCounts) -> CascadeCounts:
    """Delete streaming-pipeline rows for *name* via the engine's
    ``/v1/pipeline/delete_collection`` (RDR-186 .16: the pipeline buffer is
    ``nexus.pdf_pipeline`` in Postgres in BOTH modes — the RDR-164 CA-4
    "stays client-side" note described the retired SQLite buffer). Best-effort;
    records the count on success and a failure message otherwise. Returns
    *counts*."""
    try:
        from nexus.db.http_pipeline_client import HttpPipelineDB  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        with HttpPipelineDB() as pipeline_db:
            counts.pipeline_rows_deleted = pipeline_db.delete_pipeline_data_for_collection(name)
    except Exception as exc:  # noqa: BLE001 — best-effort; failure logged via _log.warning and recorded in counts.failures, cascade continues
        _log.warning("purge_cascade_pipeline_failed", collection=name, error=str(exc))
        counts.failures.append(f"pipeline-state cleanup failed: {exc}")
    return counts


def purge_collection_cascade(
    db: object, name: str, *, keep_quarantine: bool = False,
) -> CascadeCounts:
    """Delete T3 collection *name* and cascade-purge all derived state.

    ``db`` is a ``T3Database`` (or any object exposing
    ``delete_collection(name)``). The engine answers an already-absent
    collection with zero counts, so the cascade still runs and a prior
    half-delete (or an unconfirmed one that committed) is cleaned up.

    The engine deletes the origin's rows in its ``quarantine-`` siblings with
    it (audited, nexus-wbfpw.71). ``keep_quarantine=True`` asks it not to: for
    a caller that re-registers the SAME name straight away
    (``nx collection reindex``), where taking the rows would destroy
    restorable data for nothing. It also keeps the empty retired rows that
    name the collection as successor (rename redirects, nexus-s80u9).
    """
    counts = CascadeCounts()

    # RDR-164 P2: the entire in-Postgres cascade (T3 chunks,
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
        # The field is sent only when true (additive: an engine or client that predates it is unchanged).
        if keep_quarantine:
            deleted = client.delete_collection(name, keep_quarantine=True)  # type: ignore[attr-defined]
        else:
            deleted = client.delete_collection(name)  # type: ignore[attr-defined]
        # Preserve the local fan-out's taxonomy dict shape ({topics, assignments,
        # links, meta}) so the CLI render (commands/collection.py) does not KeyError;
        # add centroids (purged here, absent from the local path).
        # RDR-191 Phase 4 (nexus-o8dil.19/.47): unified nexus.taxonomy_centroids
        # -- see the matching comment in collection_rename.py for why this
        # reads the unified key with a legacy-per-dim-key fallback rather
        # than requiring the Java repoint to have landed first.
        _unified_centroids = deleted.get("taxonomy_centroids")
        if _unified_centroids is None:
            _unified_centroids = (
                deleted.get("taxonomy_centroids_384", 0)
                + deleted.get("taxonomy_centroids_768", 0)
                + deleted.get("taxonomy_centroids_1024", 0)
            )
        counts.taxonomy = {
            "topics": deleted.get("topics", 0),
            "assignments": deleted.get("topic_assignments", 0),
            "links": 0,
            "meta": deleted.get("taxonomy_meta", 0),
            "centroids": _unified_centroids,
        }
        counts.chash_deleted = deleted.get("chash_index", 0)
        counts.catalog_docs_deleted = deleted.get("catalog_documents", 0)
        counts.catalog_projection_deleted = deleted.get("catalog_collections", 0)
        counts.quarantine_chunks_deleted = deleted.get("quarantine_chunks", 0)
        counts.chunks_deleted = deleted.get("chunks", 0)
    except Exception as exc:  # noqa: BLE001 — best-effort, atomic on the service side
        _log.warning("purge_cascade_service_failed", collection=name, error=str(exc))
        counts.failures.append(f"service deleteCollection failed: {exc}")
        counts.collection_busy = collection_busy_of(exc, name)
        counts.service_delete_failed = True
        # The delete is unconfirmed (it may not have committed), so the pipeline buffer of what may still be a
        # live collection is left alone; a re-run of the delete takes it once the service confirms (nexus-69n4q).
        return counts
    return _purge_pipeline_db(name, counts)
