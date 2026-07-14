# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chunk quarantine — soft delete for the orphan GC (nexus-xukbj).

Instead of hard-deleting orphan chunks (or refusing over-floor sweeps with
a recurring warning — the nexus-mr89x nag), the GC MOVES orphans to a
sibling collection named ``quarantine__<owner>__<model>__v<n>``. The
``quarantine`` prefix is in NO search corpus, so quarantined chunks are
excluded from every retrieval surface by construction — no filters, no
metadata-update primitive, no schema change.

Lifecycle per GC pass (wired in ``indexer._prune_deleted_files``):

1. **Restore** — quarantined chashes that are referenced by the manifest
   again (a heal re-referenced them, or content returned) copy back to the
   origin collection and leave quarantine. Chash-keyed upsert = idempotent.
2. **Quarantine** — this pass's orphans move over with their embeddings
   (no re-embed), stamped ``quarantined_at`` + ``origin_collection`` at add
   time. NO safety floor here: the move is recoverable, so mass supersede
   churn from a big ``git pull`` proceeds silently instead of warning
   forever (the nexus-mr89x refusal nag this module retires).
3. **Expire** — quarantine rows older than ``NX_GC_QUARANTINE_DAYS``
   (default 14) hard-delete. The mr89x safety floor applies HERE only: a
   mass hard-delete surviving a full grace window means a manifest defect
   persisted for weeks — the one case that should still be loud.

First concrete piece of the RDR-156 soft-delete theme (nexus-70r3c).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

QUARANTINE_PREFIX = "quarantine"

#: Days a quarantined chunk survives before the expiry pass hard-deletes it.
QUARANTINE_DAYS_DEFAULT = 14

_WRITE_BATCH = 300  # ChromaCloud MAX_RECORDS_PER_WRITE; safe everywhere


def quarantine_collection_name(origin: str) -> str:
    """``code__nexus-1-1__voyage-code-3__v1`` -> its quarantine sibling.

    Swaps the content-type segment for ``quarantine`` — stays a conformant
    4-part name, and the prefix keeps it out of every search corpus.
    """
    parts = origin.split("__", 1)
    return f"{QUARANTINE_PREFIX}__{parts[1]}" if len(parts) == 2 else (
        f"{QUARANTINE_PREFIX}__{origin}"
    )


def quarantine_days() -> int:
    raw = os.environ.get("NX_GC_QUARANTINE_DAYS", "")
    if not raw:
        return QUARANTINE_DAYS_DEFAULT
    try:
        val = int(raw)
    except ValueError:
        val = -1
    if val < 0:
        _log.warning(
            "gc_quarantine_days_invalid",
            raw=raw, using=QUARANTINE_DAYS_DEFAULT,
        )
        return QUARANTINE_DAYS_DEFAULT
    return val


def _fetch_full(db: Any, col: Any, collection_name: str, ids: list[str]):
    """(ids, embeddings, metadatas, documents) for *ids*, both modes.

    Local Chroma returns embeddings on ``include``; the service stub does
    not carry them on ``get`` — its client exposes ``get_embeddings``.
    """
    got = col.get(ids=ids, include=["metadatas", "documents", "embeddings"])
    embs = got.get("embeddings")
    if embs is None or (hasattr(embs, "__len__") and len(embs) == 0):
        client = getattr(db, "_vector_client", None) or getattr(db, "client", None)
        if client is not None and hasattr(client, "get_embeddings"):
            embs = client.get_embeddings(collection_name, got["ids"])
    return got["ids"], embs, got.get("metadatas") or [], got.get("documents") or []


def _upsert_full(db: Any, name: str, ids, embeddings, metadatas, documents) -> None:
    qcol = db.get_or_create_collection(name)
    for i in range(0, len(ids), _WRITE_BATCH):
        sl = slice(i, i + _WRITE_BATCH)
        qcol.upsert(
            ids=ids[sl],
            embeddings=embeddings[sl] if embeddings is not None else None,
            metadatas=metadatas[sl],
            documents=documents[sl],
        )


def quarantine_orphans(
    db: Any,
    col: Any,
    collection_name: str,
    orphan_ids: list[str],
    metadatas_by_id: dict[str, dict] | None = None,
) -> int:
    """Move *orphan_ids* from *col* into the quarantine sibling.

    Returns the number of chunks quarantined. Failure to move leaves the
    chunks in place (never delete-before-copy) and logs — the next pass
    retries.
    """
    if not orphan_ids:
        return 0
    qname = quarantine_collection_name(collection_name)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    moved = 0
    for i in range(0, len(orphan_ids), _WRITE_BATCH):
        batch = orphan_ids[i:i + _WRITE_BATCH]
        try:
            ids, embs, metas, docs = _fetch_full(db, col, collection_name, batch)
            stamped = []
            for m in metas:
                m = dict(m or {})
                m["quarantined_at"] = now
                m["origin_collection"] = collection_name
                stamped.append(m)
            _upsert_full(db, qname, ids, embs, stamped, docs)
            col.delete(ids=ids)  # copy-then-delete: never lossy
            moved += len(ids)
        except Exception as exc:  # noqa: BLE001 — boundary catch; a failed batch stays in place and retries next pass
            _log.warning(
                "gc_quarantine_move_failed",
                collection=collection_name, batch=len(batch), error=str(exc),
            )
    if moved:
        _log.info(
            "gc_quarantined_orphans",
            collection=collection_name, quarantine=qname, count=moved,
        )
    return moved


def restore_rereferenced(
    db: Any, collection_name: str, referenced: set[str],
) -> int:
    """Copy quarantined chunks whose chash is referenced again back to the
    origin collection; returns the number restored."""
    qname = quarantine_collection_name(collection_name)
    try:
        qcol = db.get_collection(qname)
    except Exception:  # noqa: BLE001 — no quarantine sibling yet = nothing to restore
        return 0
    from nexus.indexer import _paginated_get  # noqa: PLC0415 — deferred: heavy module, circular-dep avoidance

    rows = _paginated_get(qcol, include=["metadatas"])
    back_ids = [
        cid for cid, m in zip(rows.get("ids") or [], rows.get("metadatas") or [])
        if ((m or {}).get("chunk_text_hash") or cid)[:32] in referenced
        and (m or {}).get("origin_collection", collection_name) == collection_name
    ]
    if not back_ids:
        return 0
    restored = 0
    origin = db.get_or_create_collection(collection_name)
    for i in range(0, len(back_ids), _WRITE_BATCH):
        batch = back_ids[i:i + _WRITE_BATCH]
        try:
            ids, embs, metas, docs = _fetch_full(db, qcol, qname, batch)
            cleaned = []
            for m in metas:
                m = dict(m or {})
                m.pop("quarantined_at", None)
                m.pop("origin_collection", None)
                cleaned.append(m)
            _upsert_full(db, collection_name, ids, embs, cleaned, docs)
            qcol.delete(ids=ids)
            restored += len(ids)
        except Exception as exc:  # noqa: BLE001 — boundary catch; a failed batch stays quarantined and retries next pass
            _log.warning(
                "gc_quarantine_restore_failed",
                collection=collection_name, batch=len(batch), error=str(exc),
            )
    if restored:
        _log.info(
            "gc_restored_from_quarantine",
            collection=collection_name, count=restored,
        )
    return restored


def expire_quarantine(
    db: Any,
    collection_name: str,
    *,
    floor_fraction: float,
    floor_min_chunks: int,
) -> tuple[int, int]:
    """Hard-delete quarantine rows older than the grace window.

    Returns ``(expired, refused)``. The mr89x safety floor applies here —
    a mass hard-delete after a FULL grace window means a manifest defect
    persisted for weeks, the one case that should still be loud.
    """
    qname = quarantine_collection_name(collection_name)
    try:
        qcol = db.get_collection(qname)
    except Exception:  # noqa: BLE001 — no quarantine sibling = nothing to expire
        return 0, 0
    from nexus.indexer import _batched_delete, _paginated_get  # noqa: PLC0415 — deferred: heavy module, circular-dep avoidance

    rows = _paginated_get(qcol, include=["metadatas"])
    all_ids = rows.get("ids") or []
    if not all_ids:
        return 0, 0
    cutoff = datetime.now(UTC) - timedelta(days=quarantine_days())
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    expired_ids = [
        cid for cid, m in zip(all_ids, rows.get("metadatas") or [])
        if (m or {}).get("quarantined_at", "9999") <= cutoff_s
    ]
    if not expired_ids:
        return 0, 0
    frac = len(expired_ids) / len(all_ids) if all_ids else 0.0
    if (
        len(expired_ids) >= floor_min_chunks
        and frac > floor_fraction
        and os.environ.get("NX_GC_FORCE", "") != "1"
    ):
        _log.warning(
            "gc_quarantine_expiry_floor_refused",
            collection=collection_name, expiring=len(expired_ids),
            quarantined=len(all_ids), fraction=round(frac, 3),
            note=(
                "a mass hard-delete surviving the full grace window means a "
                "manifest defect persisted for weeks — verify with "
                "`nx catalog doctor --t3-vs-catalog` + `nx catalog "
                "reconcile`; override with NX_GC_FORCE=1."
            ),
        )
        return 0, len(expired_ids)
    _batched_delete(qcol, expired_ids)
    _log.info(
        "gc_quarantine_expired",
        collection=collection_name, count=len(expired_ids),
    )
    return len(expired_ids), 0
