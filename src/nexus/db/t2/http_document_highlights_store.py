# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpDocumentHighlightsStore — thin HTTP client over the RDR-152 Java aspects service.

Drop-in replacement for :class:`~nexus.db.t2.document_highlights.DocumentHighlights`.
Activated when ``NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS=service``.

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

Interface parity (bead nexus-gmiaf.15, RDR-152 P2.5; extended nexus-gmiaf.16):
    upsert, get, get_by_source_uri, list, delete, rename_collection, close
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from nexus.db.t2.document_highlights import HighlightRecord

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-f2qvx.2: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpDocumentHighlightsStore no longer
# bakes a ``self._headers`` dict or a ``httpx.Client(base_url=...,
# headers=...)`` at construction time, which is what let a rotated bearer
# or a supervisor-restart port change go silently stale for the life of
# the instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


def _record_to_body(record: HighlightRecord) -> dict[str, Any]:
    return {
        "doc_id": record.doc_id,
        "source_uri": record.source_uri,
        "collection": record.collection,
        "highlights_md": record.highlights_md,
        "mentions_md": record.mentions_md,
        "ingested_at": record.ingested_at,
    }


def _body_to_record(body: dict[str, Any]) -> HighlightRecord:
    return HighlightRecord(
        doc_id=body.get("doc_id", ""),
        source_uri=body.get("source_uri", ""),
        collection=body.get("collection", ""),
        highlights_md=body.get("highlights_md", ""),
        mentions_md=body.get("mentions_md", ""),
        ingested_at=body.get("ingested_at", ""),
    )


class HttpDocumentHighlightsStore(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """DocumentHighlights drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order. ``__init__`` is inherited unchanged (this
    class's constructor signature matches the mixin's pinned contract
    exactly, so no override is needed).

    Args:
        base_url: Optional override for the service base URL.
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
    """

    # ── Internal helpers ───────────────────────────────────────────────────────
    #
    # These stay LOCAL overrides (not a straight inherit) because every method
    # in this class calls self._post/self._get with a SHORT path suffix
    # (e.g. "/upsert") — the "/v1/aspects/highlights" prefix is store-specific
    # routing, not part of the mixin's shared contract. Every actual HTTP
    # round-trip still goes through the inherited, self-healing
    # super()._post/_get (RefreshableHttpStoreMixin._send), never
    # self._client directly.

    def _post(self, path: str, body: dict[str, Any], *, idempotent: bool = True) -> Any:
        return super()._post(f"/v1/aspects/highlights{path}", body, idempotent=idempotent)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, idempotent: bool = True) -> Any:
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return super()._get(f"/v1/aspects/highlights{path}", q, idempotent=idempotent)

    # ── Public API — mirrors DocumentHighlights ───────────────────────────────

    def upsert(self, record: HighlightRecord) -> bool:
        """Persist *record* — complete overwrite if doc_id already exists.

        Returns True when written; False when empty highlights + mentions.
        Raises ValueError on empty doc_id or ingested_at.
        """
        if not record.doc_id:
            raise ValueError("doc_id must not be empty")
        if not record.ingested_at:
            raise ValueError("ingested_at must not be empty")
        if not (record.highlights_md or record.mentions_md):
            return False
        r = self._post("/upsert", _record_to_body(record))
        return bool(r.get("written", False))

    def get(self, doc_id: str) -> HighlightRecord | None:
        """Return the row matching doc_id, or None."""
        try:
            body = self._get("/get", {"doc_id": doc_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if not body:
            return None
        return _body_to_record(body)

    def get_by_source_uri(self, source_uri: str) -> HighlightRecord | None:
        """Return the row matching source_uri, or None."""
        try:
            body = self._get("/get_by_source_uri", {"source_uri": source_uri})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if not body:
            return None
        return _body_to_record(body)

    def list(self, *, limit: int = 50, offset: int = 0) -> list[HighlightRecord]:
        """Return highlight records ordered by ingested_at DESC."""
        rows: list[dict] = self._get("/list", {"limit": limit, "offset": offset})
        return [_body_to_record(r) for r in rows]

    def delete(self, doc_id: str) -> bool:
        """Delete row by doc_id. Returns True if a row was deleted."""
        r = self._post("/delete", {"doc_id": doc_id})
        return bool(r.get("deleted", False))

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's collection from *old* to *new*.

        Calls ``POST /v1/aspects/highlights/rename_collection``.
        Returns the number of rows updated.
        """
        if not old or not new:
            raise ValueError("old and new must not be empty")
        r = self._post("/rename_collection", {"old": old, "new": new})
        return int(r.get("updated", 0))

    # ── ETL import ────────────────────────────────────────────────────────────

    def import_highlight(self, body: dict[str, Any]) -> int:
        """Fidelity-preserving import for ETL. Returns 1 on success."""
        r = self._post("/import", body)
        return int(r.get("imported", 0))

    def import_highlights_batch(self, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): GUC-once bulk highlight import — POST
        all *rows* to /v1/aspects/highlights/import in ONE request."""
        if not rows:
            return 0
        r = self._post("/import", {"rows": rows})
        return int(r.get("imported", 0))
