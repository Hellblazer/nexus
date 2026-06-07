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

import json
import os
import threading
from typing import Any

import httpx
import structlog

from nexus.db.t2.aspect_extraction_queue import QueueRow

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


def _resolve_config() -> tuple[str, int, str]:
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_ASPECT_QUEUE=service."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(f"NX_SERVICE_PORT must be an integer, got: {port_str!r}") from exc
    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_ASPECT_QUEUE=service."
        )
    return host, port, token


def _body_to_queue_row(body: dict[str, Any]) -> QueueRow:
    return QueueRow(
        collection=body.get("collection", ""),
        source_path=body.get("source_path", ""),
        content_hash=body.get("content_hash", ""),
        content=body.get("content", ""),
        retry_count=int(body.get("retry_count", 0)),
        doc_id=body.get("doc_id", "") or "",
    )


class HttpAspectQueue:
    """AspectExtractionQueue drop-in that delegates to the RDR-152 Java HTTP service.

    The ``rename_lock`` parameter is accepted to match AspectExtractionQueue's
    constructor signature (T2Database injects it). It is ignored — no
    Python-side threading guards are needed when the Java service owns all
    queue state.

    Args:
        base_url:    Optional override for the service base URL.
        tenant:      Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
        rename_lock: Accepted for constructor parity with AspectExtractionQueue;
                     NOT used (no-op).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        rename_lock: "threading.RLock | None" = None,
        _token: str | None = None,
    ) -> None:
        if base_url is not None:
            if _token is None:
                _token = os.environ.get("NX_SERVICE_TOKEN", "")
                if not _token:
                    raise RuntimeError(
                        "NX_SERVICE_TOKEN is required when "
                        "NX_STORAGE_BACKEND_ASPECT_QUEUE=service."
                    )
            self._base_url = base_url.rstrip("/")
        else:
            host, port, token = _resolve_config()
            self._base_url = f"http://{host}:{port}"
            _token = token

        self._tenant = tenant
        # rename_lock accepted for constructor parity but ignored over HTTP.
        self.rename_lock: threading.RLock = (
            rename_lock if rename_lock is not None else threading.RLock()
        )
        self._headers = {
            "Authorization": f"Bearer {_token}",
            "X-Nexus-Tenant": tenant,
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        _log.info(
            "http_aspect_queue.init",
            base_url=self._base_url,
            tenant=tenant,
        )

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        resp = self._client.post(f"/v1/aspects/queue{path}", content=json.dumps(body))
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(f"/v1/aspects/queue{path}", params={
            k: str(v) for k, v in (params or {}).items() if v is not None
        })
        resp.raise_for_status()
        return resp.json()

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

    def claim_next(self) -> QueueRow | None:
        """Atomically claim the oldest pending row via SELECT ... FOR UPDATE SKIP LOCKED.

        Returns the claimed row as a QueueRow, or None when no pending row exists.
        The Java service uses FOR UPDATE SKIP LOCKED — no CAS retry loop needed.
        """
        r = self._post("/claim_next", {})
        if not r or r.get("claimed") is False or not r.get("row"):
            return None
        return _body_to_queue_row(r["row"])

    def claim_batch(self, limit: int) -> list[QueueRow]:
        """Claim up to *limit* pending rows in FIFO order."""
        if limit <= 0:
            return []
        r = self._post("/claim_batch", {"limit": limit})
        rows = r.get("rows", [])
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

    def mark_retry(self, collection: str, source_path: str) -> None:
        """Reset the row to 'pending' and increment retry_count."""
        self._post("/mark_retry", {
            "collection": collection,
            "source_path": source_path,
        })

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

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's collection from *old* to *new*."""
        r = self._post("/rename_collection", {"old": old, "new": new})
        return int(r.get("updated", 0))

    # ── ETL import ────────────────────────────────────────────────────────────

    def import_queue_row(self, body: dict[str, Any]) -> int:
        """Fidelity-preserving import for ETL. Returns 1 on success."""
        r = self._post("/import", body)
        return int(r.get("imported", 0))
