# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpDocumentAspectsStore — thin HTTP client over the RDR-152 Java aspects service.

Drop-in replacement for :class:`~nexus.db.t2.document_aspects.DocumentAspects`.
Activated when ``NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service``.

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

Interface parity (bead nexus-gmiaf.15, RDR-152 P2.5):
    upsert, get, get_by_doc_id, list_by_collection, delete,
    delete_orphans, rename_collection, list_by_extractor_version,
    set_salient_sentences, set_salient_sentences_by_key,
    get_salient_sentences, close
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import structlog

from nexus.db.t2.document_aspects import AspectRecord, _safe_json_dict, _safe_json_list

_log = structlog.get_logger(__name__)

DEFAULT_TENANT: str = "default"


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from environment."""
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(f"NX_SERVICE_PORT must be an integer, got: {port_str!r}") from exc
    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service."
        )
    return host, port, token


def _record_to_body(record: AspectRecord) -> dict[str, Any]:
    """Serialize AspectRecord to JSON body for HTTP POST."""
    return {
        "collection": record.collection,
        "source_path": record.source_path,
        "problem_formulation": record.problem_formulation,
        "proposed_method": record.proposed_method,
        "experimental_datasets": list(record.experimental_datasets),
        "experimental_baselines": list(record.experimental_baselines),
        "experimental_results": record.experimental_results,
        "extras": dict(record.extras),
        "confidence": record.confidence,
        "extracted_at": record.extracted_at,
        "model_version": record.model_version,
        "extractor_name": record.extractor_name,
        "source_uri": record.source_uri,
        "doc_id": record.doc_id,
        "salient_sentences": list(record.salient_sentences),
    }


def _body_to_record(body: dict[str, Any]) -> AspectRecord:
    """Deserialize HTTP response dict into an AspectRecord."""
    def _json_list(v: Any) -> list:
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            return _safe_json_list(v)
        return []

    def _json_dict(v: Any) -> dict:
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            return _safe_json_dict(v)
        return {}

    return AspectRecord(
        collection=body.get("collection", ""),
        source_path=body.get("source_path", ""),
        problem_formulation=body.get("problem_formulation"),
        proposed_method=body.get("proposed_method"),
        experimental_datasets=_json_list(body.get("experimental_datasets", [])),
        experimental_baselines=_json_list(body.get("experimental_baselines", [])),
        experimental_results=body.get("experimental_results"),
        extras=_json_dict(body.get("extras", {})),
        confidence=body.get("confidence"),
        extracted_at=body.get("extracted_at", ""),
        model_version=body.get("model_version", ""),
        extractor_name=body.get("extractor_name", ""),
        source_uri=body.get("source_uri"),
        doc_id=body.get("doc_id", ""),
        salient_sentences=_json_list(body.get("salient_sentences", [])),
    )


class HttpDocumentAspectsStore:
    """DocumentAspects drop-in that delegates to the RDR-152 Java HTTP service.

    NOTE: ``promote_extras_field`` and ``list_promotions`` in
    ``aspect_promotion.py`` reach into ``db.document_aspects.conn`` and
    ``db.document_aspects._lock`` for raw SQLite access. When this store is
    active those attributes raise ``AttributeError`` — callers using the
    promotion ETL against the service backend must use the
    ``/v1/aspects/promotion/record`` endpoint directly. The ``aspect_promotion``
    module is SQLite-specific and is intentionally NOT forwarded over HTTP
    (the Postgres tier owns the promotion_log table via AspectRepository).

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
                        "NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service."
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
            "http_document_aspects_store.init",
            base_url=self._base_url,
            tenant=tenant,
        )

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        resp = self._client.post(f"/v1/aspects{path}", content=json.dumps(body))
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._client.get(f"/v1/aspects{path}", params={
            k: str(v) for k, v in (params or {}).items() if v is not None
        })
        resp.raise_for_status()
        return resp.json()

    # ── Public API — mirrors DocumentAspects ──────────────────────────────────

    def upsert(self, record: AspectRecord) -> bool:
        """Persist *record* — complete overwrite if the key already exists.

        Returns True when the row was written; False when rejected by the
        confidence gate (confidence < 0.3).
        """
        if not record.extracted_at:
            raise ValueError("extracted_at must not be empty")
        if not record.model_version:
            raise ValueError("model_version must not be empty")
        if not record.extractor_name:
            raise ValueError("extractor_name must not be empty")
        r = self._post("/upsert", _record_to_body(record))
        return bool(r.get("written", False))

    def get(self, collection: str, source_path: str) -> AspectRecord | None:
        """Return the row matching (collection, source_path), or None."""
        try:
            body = self._get("/get", {
                "collection": collection,
                "source_path": source_path,
            })
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if not body:
            return None
        return _body_to_record(body)

    def get_by_doc_id(self, doc_id: str) -> AspectRecord | None:
        """Return the row matching doc_id, or None."""
        try:
            body = self._get("/get_by_doc_id", {"doc_id": doc_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if not body:
            return None
        return _body_to_record(body)

    def list_by_collection(
        self,
        collection: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AspectRecord]:
        """Return all rows in *collection*, paginated."""
        params: dict[str, Any] = {"collection": collection, "offset": offset}
        if limit is not None:
            params["limit"] = limit
        rows: list[dict] = self._get("/list_by_collection", params)
        return [_body_to_record(r) for r in rows]

    def delete(self, collection: str, source_path: str) -> int:
        """Drop the row at (collection, source_path). Returns deleted count."""
        r = self._post("/delete", {
            "collection": collection,
            "source_path": source_path,
        })
        return int(r.get("deleted", 0))

    def delete_orphans(
        self,
        catalog_db_path: Path | None,
        *,
        dry_run: bool = True,
    ) -> tuple[int, int]:
        """Orphan deletion is SQLite-specific (ATTACH DATABASE).

        Over HTTP the service does not have catalog path access. Returns
        (0, 0) to preserve the call signature without silently deleting
        rows on a backend that cannot confirm orphan status.
        """
        _log.info(
            "http_document_aspects_store.delete_orphans_noop",
            catalog_db_path=str(catalog_db_path),
            dry_run=dry_run,
            reason="orphan deletion requires catalog ATTACH which is SQLite-specific",
        )
        return (0, 0)

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's collection from *old* to *new*."""
        r = self._post("/rename_collection", {"old": old, "new": new})
        return int(r.get("updated", 0))

    def list_by_extractor_version(
        self,
        extractor_name: str,
        max_version: str,
    ) -> list[AspectRecord]:
        """Return rows whose extractor_name matches and model_version < max_version."""
        rows: list[dict] = self._get("/list_by_extractor_version", {
            "extractor": extractor_name,
            "max_version": max_version,
        })
        return [_body_to_record(r) for r in rows]

    def set_salient_sentences(self, doc_id: str, sentences: list[str]) -> bool:
        """Write salient_sentences for doc_id. Returns True on update."""
        r = self._post("/salient_sentences/set", {
            "doc_id": doc_id,
            "sentences": sentences,
        })
        return bool(r.get("updated", False))

    def set_salient_sentences_by_key(
        self,
        collection: str,
        source_path: str,
        sentences: list[str],
    ) -> bool:
        """Pre-PK-migration fallback: target rows by (collection, source_path)."""
        r = self._post("/salient_sentences/set_by_key", {
            "collection": collection,
            "source_path": source_path,
            "sentences": sentences,
        })
        return bool(r.get("updated", False))

    def get_salient_sentences(self, doc_id: str) -> list[str]:
        """Return the salient sentences for doc_id, or []."""
        try:
            r = self._get("/salient_sentences/get", {"doc_id": doc_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return []
            raise
        sentences = r.get("sentences", [])
        if not isinstance(sentences, list):
            return []
        return [str(s) for s in sentences if s]

    # ── ETL import ────────────────────────────────────────────────────────────

    def import_aspect(self, body: dict[str, Any]) -> int:
        """Fidelity-preserving import for ETL. Returns 1 on success."""
        r = self._post("/import", body)
        return int(r.get("imported", 0))
