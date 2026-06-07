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

import json
import os
from typing import Any

import httpx
import structlog

from nexus.db.t2.document_highlights import HighlightRecord

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


def _resolve_config() -> tuple[str, int, str]:
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS=service."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(f"NX_SERVICE_PORT must be an integer, got: {port_str!r}") from exc
    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when "
            "NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS=service."
        )
    return host, port, token


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


class HttpDocumentHighlightsStore:
    """DocumentHighlights drop-in that delegates to the RDR-152 Java HTTP service.

    Args:
        base_url: Optional override for the service base URL.
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
    ) -> None:
        if base_url is not None:
            if _token is None:
                _token = os.environ.get("NX_SERVICE_TOKEN", "")
                if not _token:
                    raise RuntimeError(
                        "NX_SERVICE_TOKEN is required when "
                        "NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS=service."
                    )
            self._base_url = base_url.rstrip("/")
        else:
            host, port, token = _resolve_config()
            self._base_url = f"http://{host}:{port}"
            _token = token

        self._tenant = tenant
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
            "http_document_highlights_store.init",
            base_url=self._base_url,
            tenant=tenant,
        )

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        resp = self._client.post(f"/v1/aspects/highlights{path}", content=json.dumps(body))
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(f"/v1/aspects/highlights{path}", params={
            k: str(v) for k, v in (params or {}).items() if v is not None
        })
        resp.raise_for_status()
        return resp.json()

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
