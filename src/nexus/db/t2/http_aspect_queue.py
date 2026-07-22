# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpAspectQueue — thin HTTP client over the RDR-152 Java aspects/queue service.

Drop-in replacement for :class:`~nexus.db.t2.aspect_extraction_queue.AspectExtractionQueue`.
Activated when ``NX_STORAGE_BACKEND_ASPECT_QUEUE=service``.

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

Interface parity (bead nexus-gmiaf.15, RDR-152 P2.5):
    enqueue, claim_next, claim_batch, mark_done, mark_failed,
    mark_retry, reclaim_stale, pending_count, is_drained,
    list_pending, rename_collection, close

Queue claim strategy: the Java service uses
``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` — eliminates WAL contention
vs the SQLite CAS loop and is the primary motivation for this migration.

The ``rename_lock`` constructor parameter accepted by AspectExtractionQueue
is accepted but ignored here (no Python-side threading; the Java service owns
all serialization).
"""
from __future__ import annotations

import threading
from typing import Any

import structlog

from nexus.db.t2.aspect_extraction_queue import QueueRow

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-f2qvx.2: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpAspectQueue no longer bakes a
# ``self._headers`` dict or a ``httpx.Client(base_url=..., headers=...)``
# at construction time, which is what let a rotated bearer or a
# supervisor-restart port change go silently stale for the life of the
# instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
# This store is the one with a public ``timeout`` kwarg on its own
# constructor (the mixin's ``_DEFAULT_TIMEOUT_S`` docstring flags it by
# name) — threaded through to ``super().__init__(..., timeout=timeout)``
# below, which required the mixin's own ``__init__`` to grow a matching
# optional ``timeout`` kwarg (additive; every other adopter keeps the
# 30.0s default unchanged).
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


def _body_to_queue_row(body: dict[str, Any]) -> QueueRow:
    return QueueRow(
        collection=body.get("collection", ""),
        source_path=body.get("source_path", ""),
        content_hash=body.get("content_hash", ""),
        content=body.get("content", ""),
        retry_count=int(body.get("retry_count", 0)),
        doc_id=body.get("doc_id", "") or "",
    )


class HttpAspectQueue(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """AspectExtractionQueue drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order.

    The ``rename_lock`` parameter is accepted to match AspectExtractionQueue's
    constructor signature (T2Database injects it). It is ignored — no
    Python-side threading guards are needed when the Java service owns all
    queue state. ``timeout`` is this store's own public constructor kwarg
    (pre-dating the mixin); it is threaded through to the mixin's
    ``__init__`` unchanged so callers that pin a non-default HTTP timeout
    keep working identically post-adoption.

    Args:
        base_url:    Optional override for the service base URL.
        tenant:      Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
        rename_lock: Accepted for constructor parity with AspectExtractionQueue;
                     NOT used (no-op).
        timeout:     HTTP client timeout in seconds (default: 30.0).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        rename_lock: "threading.RLock | None" = None,
        _token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(base_url, tenant, _token=_token, timeout=timeout)
        # rename_lock accepted for constructor parity but ignored over HTTP
        # (verbatim behavior preserved from the pre-mixin constructor).
        self.rename_lock: threading.RLock = (
            rename_lock if rename_lock is not None else threading.RLock()
        )

    # ── Internal helpers ───────────────────────────────────────────────────────
    #
    # These stay LOCAL overrides (not a straight inherit) because every method
    # in this class calls self._post/self._get with a SHORT path suffix
    # (e.g. "/enqueue") — the "/v1/aspects/queue" prefix is store-specific
    # routing, not part of the mixin's shared contract. Every actual HTTP
    # round-trip still goes through the inherited, self-healing
    # super()._post/_get (RefreshableHttpStoreMixin._send), never
    # self._client directly.

    def _post(self, path: str, body: dict[str, Any], *, idempotent: bool = True) -> Any:
        return super()._post(f"/v1/aspects/queue{path}", body, idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return super()._get(f"/v1/aspects/queue{path}", q)

    # ── Public API — mirrors AspectExtractionQueue ────────────────────────────

    def enqueue(
        self,
        collection: str,
        source_path: str,
        content_hash: str = "",
        content: str = "",
        *,
        doc_id: str = "",
    ) -> None:
        """Persist a new pending row for (collection, source_path).

        INSERT OR REPLACE semantics: re-enqueue at the same key resets
        status='pending' and retry_count=0.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        if not source_path:
            raise ValueError("source_path must not be empty")
        self._post("/enqueue", {
            "collection": collection,
            "source_path": source_path,
            "content_hash": content_hash,
            "content": content,
            "doc_id": doc_id,
        })

    def enqueue_many(self, rows: list[dict]) -> int:
        """Batch-enqueue N documents in ONE round trip (nexus-nj4ch,
        nexus-duoak follow-up: replaces the indexer's per-document
        ``enqueue()`` call under the ChunkBatcher's flush-grain hook,
        measured at ~34.7s across ~250 real inserts in this repo's own
        shakeout).

        Each dict carries the same fields as :meth:`enqueue`'s keyword
        arguments (``collection``, ``source_path``, and optionally
        ``content_hash``/``content``/``doc_id``). Returns the count of
        rows actually enqueued (malformed rows are skipped server-side,
        not counted).

        The whole batch shares ONE Postgres transaction server-side
        (``ctx.batch(...)`` inside one ``tenantScope.withTenant``), so a
        single row's constraint violation (e.g. a ``doc_id`` FK to a
        catalog document that hasn't landed yet) fails the WHOLE batch,
        not just that row — unlike the per-row isolation
        ``updateDocumentsMany``/``register_many`` get from their looser
        constraints. On any batch failure this falls back to per-row
        :meth:`enqueue` calls, isolating the genuinely bad row from its
        batch-mates (mirrors :meth:`nexus.catalog.http_catalog_client.
        HttpCatalogClient.register_many`'s page-failure fallback).
        """
        if not rows:
            return 0
        try:
            result = self._post("/enqueue_many", {"rows": rows})
            return int((result or {}).get("enqueued", 0))
        except Exception:  # noqa: BLE001 — batch unrecoverable (e.g. one row's constraint violation); per-row isolation fallback
            _log.warning(
                "aspect_enqueue_many_failed_falling_back_per_row",
                row_count=len(rows),
                exc_info=True,
            )
            enqueued = 0
            for row in rows:
                collection = row.get("collection", "")
                source_path = row.get("source_path", "")
                if not collection or not source_path:
                    continue
                try:
                    self.enqueue(
                        collection, source_path,
                        content_hash=row.get("content_hash", ""),
                        content=row.get("content", ""),
                        doc_id=row.get("doc_id", ""),
                    )
                    enqueued += 1
                except Exception:  # noqa: BLE001 — per-row fallback failure isolation (mirrors register_many's per-doc try/except upstream)
                    _log.warning(
                        "aspect_enqueue_fallback_row_failed",
                        collection=collection,
                        source_path=source_path,
                        exc_info=True,
                    )
            return enqueued

    def claim_next(self) -> QueueRow | None:
        """Atomically claim the oldest pending row via SELECT ... FOR UPDATE SKIP LOCKED.

        Returns the claimed row as a QueueRow, or None when no pending row exists.
        The Java service uses FOR UPDATE SKIP LOCKED — no CAS retry loop needed.
        """
        # nexus-tjvgf: claiming is NOT retry-safe — a lost response
        # after a successful server-side claim orphans the row
        # in_progress until reclaim_stale. Single attempt; the worker
        # loop owns recovery.
        r = self._post("/claim_next", {}, idempotent=False)
        if not r or r.get("claimed") is False or not r.get("row"):
            return None
        return _body_to_queue_row(r["row"])

    def claim_batch(self, limit: int) -> list[QueueRow]:
        """Claim up to *limit* pending rows in FIFO order."""
        if limit <= 0:
            return []
        # nexus-tjvgf: see claim_next — single attempt.
        r = self._post("/claim_batch", {"limit": limit}, idempotent=False)
        # nexus-575kd: the Java service sends a BARE JSON ARRAY here
        # (AspectHandler.handleQueueClaimBatch -> writeValueAsString(rows)),
        # unlike claim_next which is enveloped. Accept the array directly;
        # tolerate a future {"rows":[...]} envelope defensively. The prior
        # ``r.get("rows", [])`` raised AttributeError on the list every poll,
        # so the service-mode worker claimed nothing and document_aspects never
        # populated (all service-mode aspect extraction was dead).
        rows = r if isinstance(r, list) else r.get("rows", [])
        return [_body_to_queue_row(row) for row in rows]

    def mark_done(
        self,
        collection: str = "",
        source_path: str = "",
        *,
        doc_id: str = "",
    ) -> int:
        """DELETE the row at (doc_id) or (collection, source_path) — success path.

        Returns deleted row count.
        """
        r = self._post("/mark_done", {
            "collection": collection,
            "source_path": source_path,
            "doc_id": doc_id,
        })
        return int(r.get("deleted", 0))

    def mark_failed(
        self,
        collection: str,
        source_path: str,
        error: str,
    ) -> None:
        """Mark the row as 'failed' (terminal until re-enqueued)."""
        self._post("/mark_failed", {
            "collection": collection,
            "source_path": source_path,
            "error": error[:2000],
        })

    def mark_retry(self, collection: str, source_path: str,
                   interval_seconds: int = 0) -> None:
        """Reset the row to 'pending', increment retry_count, and back it off.

        ``interval_seconds`` is the worker-chosen backoff; the service stamps
        ``next_retry_at = now() + interval_seconds`` server-side (RDR-163 P1,
        nexus-ztpt6). Default 0 = ready immediately.
        """
        # nexus-tjvgf: retry_count = retry_count + 1 server-side — a
        # retried request double-increments the budget toward premature
        # terminal mark_failed. Single attempt.
        self._post("/mark_retry", {
            "collection": collection,
            "source_path": source_path,
            "interval_seconds": interval_seconds,
        }, idempotent=False)

    def reclaim_stale(self, timeout_seconds: int = 300) -> int:
        """Reset in_progress rows older than timeout back to 'pending'.

        Returns the number of rows reclaimed.
        """
        r = self._post("/reclaim_stale", {"timeout_seconds": timeout_seconds})
        return int(r.get("reclaimed", 0))

    def pending_count(self) -> int:
        """Return the number of rows currently in 'pending' status."""
        r = self._get("/pending_count")
        return int(r.get("count", 0))

    def is_drained(self) -> bool:
        """Return True iff no actionable rows remain (count of non-failed rows is 0)."""
        r = self._get("/is_drained")
        return bool(r.get("drained", False))

    def list_pending(self, limit: int | None = None) -> list[QueueRow]:
        """Return pending rows in claim order (FIFO by enqueued_at)."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        rows: list[dict] = self._get("/list_pending", params)
        return [_body_to_queue_row(r) for r in rows]

    def list_failed(self, collection: str | None = None) -> list[QueueRow]:
        """Return terminal-failed rows, optionally scoped to one collection."""
        params: dict[str, Any] = {}
        if collection:
            params["collection"] = collection
        rows: list[dict] = self._get("/list_failed", params)
        return [_body_to_queue_row(r) for r in rows]

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's collection from *old* to *new*."""
        r = self._post("/rename_collection", {"old": old, "new": new})
        return int(r.get("updated", 0))

    # ── ETL import ────────────────────────────────────────────────────────────

    def import_queue_row(self, body: dict[str, Any]) -> int:
        """Fidelity-preserving import for ETL. Returns 1 on success."""
        r = self._post("/import", body)
        return int(r.get("imported", 0))

    def import_queue_batch(self, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): GUC-once bulk queue import — POST all
        *rows* to /v1/aspects/queue/import in ONE request."""
        if not rows:
            return 0
        r = self._post("/import", {"rows": rows})
        return int(r.get("imported", 0))
