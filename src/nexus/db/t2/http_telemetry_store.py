# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpTelemetryStore — thin HTTP client over the RDR-152 Java telemetry service.

Drop-in replacement for :class:`~nexus.db.t2.telemetry.Telemetry`.
Activated by setting ``NX_STORAGE_BACKEND=service`` (or
``NX_STORAGE_BACKEND_TELEMETRY=service``).

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

All methods send ``Authorization: Bearer <token>`` and
``X-Nexus-Tenant: default`` (``DEFAULT_TENANT``) on every request.

Interface parity (bead nexus-gmiaf.12, RDR-152 P2.2):
    log_relevance, log_relevance_batch,
    get_relevance_log, expire_relevance_log,
    log_search_batch, query_collection_stats,
    trim_search_telemetry, rename_collection, close

ETL-only import methods (used by telemetry_etl.py):
    import_relevance_row, import_search_row, import_tier_write,
    import_nx_answer_run, import_hook_failure, import_frecency_row

Route mapping (matches TelemetryHandler Java):
    POST /v1/telemetry/relevance/log    — log_relevance
    GET  /v1/telemetry/relevance/query  — get_relevance_log
    POST /v1/telemetry/relevance/expire — expire_relevance_log
    POST /v1/telemetry/search/batch     — log_search_batch
    GET  /v1/telemetry/search/stats     — query_collection_stats
    POST /v1/telemetry/search/trim      — trim_search_telemetry
    POST /v1/telemetry/rename_collection — rename_collection
    POST /v1/telemetry/import           — import_* methods (ETL)
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from environment.

    Raises:
        RuntimeError: if NX_SERVICE_PORT or NX_SERVICE_TOKEN are not set.
    """
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")

    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_TELEMETRY=service. "
            "Set it to the port where the nexus-service is listening."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(
            f"NX_SERVICE_PORT must be an integer, got: {port_str!r}"
        ) from exc

    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_TELEMETRY=service. "
            "Set it to the bearer token configured in the nexus-service."
        )

    return host, port, token


class HttpTelemetryStore:
    """Telemetry drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool. Reads
    ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and ``NX_SERVICE_TOKEN``
    from the environment at construction time.

    Args:
        base_url: Optional override for the service base URL
            (``http://<host>:<port>``). When supplied, ``host``/``port``
            env-vars are ignored; the token env-var is still required.
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
                        "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_TELEMETRY=service."
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
        _log.info("http_telemetry_store.init", base_url=self._base_url, tenant=tenant)

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()
        _log.debug("http_telemetry_store.closed")

    # ── relevance_log ─────────────────────────────────────────────────────────

    def log_relevance(
        self,
        query: str,
        chunk_id: str,
        action: str,
        session_id: str = "",
        collection: str = "",
    ) -> int:
        """Record a single (query, chunk_id, action) triple in the relevance log.

        Returns the new row id. Calls ``POST /v1/telemetry/relevance/log``.
        """
        payload: dict[str, Any] = {
            "query":      query,
            "chunk_id":   chunk_id,
            "action":     action,
            "session_id": session_id or "",
            "collection": collection or "",
        }
        resp = self._post("/v1/telemetry/relevance/log", payload)
        return int(resp.get("id", 0))

    def log_relevance_batch(
        self,
        rows: list[tuple[str, str, str, str, str]],
    ) -> int:
        """Insert multiple (query, chunk_id, collection, action, session_id) rows.

        Single transaction on the service side. Returns number of rows inserted.
        Calls ``POST /v1/telemetry/relevance/batch``.
        """
        if not rows:
            return 0
        payload: dict[str, Any] = {
            "rows": [list(r) for r in rows]
        }
        resp = self._post("/v1/telemetry/relevance/batch", payload)
        return int(resp.get("inserted", len(rows)))

    def get_relevance_log(
        self,
        query: str = "",
        chunk_id: str = "",
        action: str = "",
        session_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query the relevance log by filters. All filters optional.

        Returns rows as dicts ordered by most recent first.
        Calls ``GET /v1/telemetry/relevance/query``.
        """
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["query"] = query
        if chunk_id:
            params["chunk_id"] = chunk_id
        if action:
            params["action"] = action
        if session_id:
            params["session_id"] = session_id
        resp = self._client.get("/v1/telemetry/relevance/query", params=params)
        self._raise_for_status(resp, "get_relevance_log")
        data = resp.json()
        return data if isinstance(data, list) else []

    def expire_relevance_log(self, days: int = 90) -> int:
        """Delete relevance_log entries older than *days* days.

        Calls ``POST /v1/telemetry/relevance/expire``.
        """
        resp = self._post("/v1/telemetry/relevance/expire", {"days": days})
        return int(resp.get("deleted", 0))

    # ── search_telemetry ──────────────────────────────────────────────────────

    def log_search_batch(
        self,
        rows: list[tuple[str, str, str, int, int, float | None, float | None]],
    ) -> int:
        """Insert per-call threshold-filter telemetry in a single transaction.

        Row tuple layout: ``(ts, query_hash, collection, raw_count,
        kept_count, top_distance, threshold)``.
        Calls ``POST /v1/telemetry/search/batch``.
        """
        if not rows:
            return 0
        payload: dict[str, Any] = {
            "rows": [list(r) for r in rows]
        }
        resp = self._post("/v1/telemetry/search/batch", payload)
        return int(resp.get("inserted", len(rows)))

    def query_collection_stats(
        self, collection: str, *, days: int = 30,
    ) -> dict[str, Any]:
        """Return retrieval-health stats for *collection* over the last *days*.

        Calls ``GET /v1/telemetry/search/stats``.
        """
        resp = self._client.get(
            "/v1/telemetry/search/stats",
            params={"collection": collection, "days": days},
        )
        self._raise_for_status(resp, "query_collection_stats")
        return resp.json()

    def trim_search_telemetry(self, days: int = 30) -> int:
        """Delete ``search_telemetry`` rows older than *days* days.

        Calls ``POST /v1/telemetry/search/trim``.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        resp = self._post("/v1/telemetry/search/trim", {"days": days})
        return int(resp.get("deleted", 0))

    def rename_collection(self, *, old: str, new: str) -> dict[str, int]:
        """Re-point collection columns from ``old`` to ``new`` in all telemetry tables.

        Calls ``POST /v1/telemetry/rename_collection``.
        """
        resp = self._post("/v1/telemetry/rename_collection", {"old": old, "new": new})
        return {
            "search_telemetry": int(resp.get("search_telemetry", 0)),
            "hook_failures":    int(resp.get("hook_failures", 0)),
        }

    # ── ETL import methods (fidelity-preserving, timestamps verbatim) ─────────

    def import_relevance_row(
        self,
        *,
        query: str,
        chunk_id: str,
        collection: str,
        action: str,
        session_id: str,
        timestamp: str,
    ) -> None:
        """Fidelity-preserving import of one relevance_log row.

        Uses ``POST /v1/telemetry/import`` with ``table=relevance_log``.
        The ``timestamp`` is written VERBATIM (DO NOTHING on ETL dedup conflict).
        """
        self._post("/v1/telemetry/import", {
            "table":      "relevance_log",
            "query":      query,
            "chunk_id":   chunk_id,
            "collection": collection or "",
            "action":     action,
            "session_id": session_id or "",
            "timestamp":  timestamp,
        })

    def import_search_row(
        self,
        *,
        ts: str,
        query_hash: str,
        collection: str,
        raw_count: int,
        kept_count: int,
        top_distance: float | None,
        threshold: float | None,
    ) -> None:
        """Fidelity-preserving import of one search_telemetry row.

        Uses ``POST /v1/telemetry/import`` with ``table=search_telemetry``.
        DO NOTHING on composite PK conflict.
        """
        payload: dict[str, Any] = {
            "table":       "search_telemetry",
            "ts":          ts,
            "query_hash":  query_hash,
            "collection":  collection,
            "raw_count":   raw_count,
            "kept_count":  kept_count,
        }
        if top_distance is not None:
            payload["top_distance"] = top_distance
        if threshold is not None:
            payload["threshold"] = threshold
        self._post("/v1/telemetry/import", payload)

    def import_tier_write(
        self,
        *,
        session_id: str,
        ts: str,
        tool: str,
        tier: str,
        agent: str | None,
        project: str | None,
        target_title: str | None,
    ) -> None:
        """Fidelity-preserving import of one tier_writes row.

        Uses ``POST /v1/telemetry/import`` with ``table=tier_writes``.
        DO NOTHING on ETL dedup conflict.
        """
        payload: dict[str, Any] = {
            "table":      "tier_writes",
            "session_id": session_id or "",
            "ts":         ts,
            "tool":       tool or "",
            "tier":       tier or "",
        }
        if agent is not None:
            payload["agent"] = agent
        if project is not None:
            payload["project"] = project
        if target_title is not None:
            payload["target_title"] = target_title
        self._post("/v1/telemetry/import", payload)

    def import_nx_answer_run(
        self,
        *,
        question: str,
        plan_id: str | None,
        matched_confidence: float | None,
        step_count: int,
        final_text: str,
        cost_usd: float | None,
        duration_ms: int,
        created_at: str,
    ) -> None:
        """Fidelity-preserving import of one nx_answer_runs row.

        Uses ``POST /v1/telemetry/import`` with ``table=nx_answer_runs``.
        DO NOTHING on ETL dedup conflict.
        """
        payload: dict[str, Any] = {
            "table":      "nx_answer_runs",
            "question":   question,
            "step_count": step_count,
            "final_text": final_text or "",
            "duration_ms": duration_ms,
            "created_at": created_at,
        }
        if plan_id is not None:
            payload["plan_id"] = plan_id
        if matched_confidence is not None:
            payload["matched_confidence"] = matched_confidence
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        self._post("/v1/telemetry/import", payload)

    def import_hook_failure(
        self,
        *,
        doc_id: str,
        collection: str,
        hook_name: str,
        error: str,
        occurred_at: str,
        batch_doc_ids: str | None,
        is_batch: bool,
        chain: str | None,
    ) -> None:
        """Fidelity-preserving import of one hook_failures row.

        Uses ``POST /v1/telemetry/import`` with ``table=hook_failures``.
        DO NOTHING on ETL dedup conflict.
        """
        payload: dict[str, Any] = {
            "table":       "hook_failures",
            "doc_id":      doc_id or "",
            "collection":  collection or "",
            "hook_name":   hook_name,
            "error":       error or "",
            "occurred_at": occurred_at,
            "is_batch":    is_batch,
            "chain":       chain or "",
        }
        if batch_doc_ids is not None:
            payload["batch_doc_ids"] = batch_doc_ids
        self._post("/v1/telemetry/import", payload)

    def import_frecency_row(
        self,
        *,
        chunk_id: str,
        embedded_at: str | None,
        ttl_days: int,
        frecency_score: float,
        miss_count: int,
        last_hit_at: str | None,
    ) -> None:
        """Fidelity-preserving import of one frecency row.

        Uses ``POST /v1/telemetry/import`` with ``table=frecency``.
        GREATEST for score/count/last_hit_at; LEAST for embedded_at.
        """
        payload: dict[str, Any] = {
            "table":          "frecency",
            "chunk_id":       chunk_id,
            "ttl_days":       ttl_days,
            "frecency_score": frecency_score,
            "miss_count":     miss_count,
        }
        if embedded_at is not None:
            payload["embedded_at"] = embedded_at
        if last_hit_at is not None:
            payload["last_hit_at"] = last_hit_at
        self._post("/v1/telemetry/import", payload)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST JSON payload, raise on error, return parsed JSON."""
        resp = self._client.post(path, json=payload)
        self._raise_for_status(resp, path)
        return resp.json()

    def _raise_for_status(self, resp: httpx.Response, op: str) -> None:
        """Raise a descriptive exception on non-2xx responses."""
        if resp.is_success:
            return
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise httpx.HTTPStatusError(
            f"HttpTelemetryStore.{op} failed: HTTP {resp.status_code}: {detail}",
            request=resp.request,
            response=resp,
        )
