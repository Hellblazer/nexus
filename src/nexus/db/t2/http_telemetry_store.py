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
    POST /v1/telemetry/import_batch     — import_rows_batch (bulk ETL)
    POST /v1/telemetry/ids/probe        — probe_ids (verify-fill inner loop, RDR-178 wave-2 P1)
"""

from __future__ import annotations

from typing import Any

import structlog

from nexus.db.limits import QUOTAS

_log = structlog.get_logger(__name__)

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-f2qvx.1: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpTelemetryStore no longer bakes a
# ``self._headers`` dict or a ``httpx.Client(base_url=..., headers=...)``
# at construction time, which is what let a rotated bearer or a
# supervisor-restart port change go silently stale for the life of the
# instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


class HttpTelemetryStore(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """Telemetry drop-in that delegates to the RDR-152 Java HTTP service.

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
        base_url: Optional override for the service base URL
            (``http://<host>:<port>``). When supplied without ``_token``,
            only the token half is re-resolved (host/port need not also be
            independently resolvable).
        tenant:   Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
    """

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

    def record_tier_write(
        self,
        *,
        session_id: str,
        ts: str,
        tool: str,
        tier: str,
        agent: str | None = None,
        project: str | None = None,
        target_title: str | None = None,
    ) -> None:
        """Record a tier-write event. Calls ``POST /v1/telemetry/tier_writes/record``.

        nexus-pyzk7: the service-side table + endpoint already exist; this routes
        the MCP consumer there instead of a raw SQLite conn the service has not.
        """
        self._post("/v1/telemetry/tier_writes/record", {
            "session_id":   session_id,
            "ts":           ts,
            "tool":         tool,
            "tier":         tier,
            "agent":        agent,
            "project":      project,
            "target_title": target_title,
        })

    @property
    def base_url(self) -> str:
        """The resolved service base URL. Public read-only: the verify-fill
        watermark (nexus-te885.10) keys its per-target state on it, so a
        different target (fresh service after rollback + re-init) never
        inherits another target's watermark."""
        return self._base_url or ""

    def record_consent(self, *, scope: str, ts: str, granted: bool) -> None:
        """Record a consent grant/revoke. Calls ``POST /v1/telemetry/consents/record``.

        RDR-182 nexus-ng2sy: the service-mode twin of ``Telemetry.record_consent``
        — the consent audit the ``remediate`` release records at layer 5.
        Append-only; ``granted`` distinguishes a grant from a revoke.
        """
        self._post("/v1/telemetry/consents/record", {
            "scope":   scope,
            "ts":      ts,
            "granted": granted,
        })

    def list_consents(self) -> list[dict[str, Any]]:
        """Read the tenant's consent-audit trail (grants and revokes, in
        insertion order). Calls ``GET /v1/telemetry/consents/list``.

        The service-mode twin of ``Telemetry.list_consents`` — the read
        surface behind ``nx remediate --history``.
        """
        data = self._get("/v1/telemetry/consents/list")
        return data if isinstance(data, list) else []

    def get_retention_markers(self, relations: list[str]) -> dict[str, int]:
        """Cumulative-deletes retention markers for *relations* (nexus-24p05)
        — the verify-fill watermark's rollback detector. Calls
        ``GET /v1/telemetry/retention/markers``. Relations never swept (or on
        a fresh post-rollback schema) are absent; callers treat absent as 0.
        """
        from urllib.parse import quote  # noqa: PLC0415 — stdlib, branch-local

        data = self._get(
            "/v1/telemetry/retention/markers?relations=" + quote(",".join(relations))
        )
        markers = data.get("markers") if isinstance(data, dict) else None
        if not isinstance(markers, dict):
            return {}
        return {k: int(v) for k, v in markers.items() if isinstance(v, (int, float))}

    def record_nx_answer_run(
        self,
        *,
        question: str,
        plan_id: int | None,
        matched_confidence: float | None,
        step_count: int,
        final_text: str,
        cost_usd: float,
        duration_ms: int,
    ) -> None:
        """Record an nx_answer run. Calls ``POST /v1/telemetry/nx_answer_runs/record``."""
        self._post("/v1/telemetry/nx_answer_runs/record", {
            "question":           question,
            "plan_id":            plan_id,
            "matched_confidence": matched_confidence,
            "step_count":         step_count,
            "final_text":         final_text,
            "cost_usd":           cost_usd,
            "duration_ms":        duration_ms,
        })

    def record_hook_failure(
        self,
        *,
        doc_id: str,
        collection: str,
        hook_name: str,
        error: str,
        chain: str,
        batch_doc_ids: str | None = None,
        is_batch: bool = False,
        occurred_at: str | None = None,
    ) -> None:
        """Record a hook failure. Calls ``POST /v1/telemetry/hook_failures/record``.

        nexus-9613q.3: the service-side table + endpoint already exist; this
        routes the hook_registry consumer there instead of a raw SQLite conn
        the service-backed store has not (every row was silently dropped).
        """
        payload: dict[str, Any] = {
            "doc_id":      doc_id,
            "collection":  collection,
            "hook_name":   hook_name,
            "error":       error,
            "chain":       chain,
            "is_batch":    is_batch,
        }
        if batch_doc_ids is not None:
            payload["batch_doc_ids"] = batch_doc_ids
        if occurred_at is not None:
            payload["occurred_at"] = occurred_at
        self._post("/v1/telemetry/hook_failures/record", payload)

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
        data = self._get("/v1/telemetry/relevance/query", params=params)
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
        return self._get(
            "/v1/telemetry/search/stats",
            params={"collection": collection, "days": days},
        )

    def trim_search_telemetry(self, days: int = 30) -> int:
        """Delete ``search_telemetry`` rows older than *days* days.

        Calls ``POST /v1/telemetry/search/trim``.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        resp = self._post("/v1/telemetry/search/trim", {"days": days})
        return int(resp.get("deleted", 0))

    def trim_hook_failures(self, days: int = 30) -> int:
        """Delete ``hook_failures`` rows older than *days* days (nexus-7365x).

        Calls ``POST /v1/telemetry/hook_failures/trim``.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        resp = self._post("/v1/telemetry/hook_failures/trim", {"days": days})
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
        plan_id: int | None,
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

    def import_rows_batch(self, table: str, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): fidelity-preserving BULK import for
        one telemetry *table*.

        POSTs ``{"table": table, "rows": rows}`` to ``/v1/telemetry/import_batch``
        in ONE request — the service lands the whole batch under one tenant
        transaction (GUC set once). Each row dict carries the same fields the
        per-row ``import_*`` method for *table* sends (minus ``table``). Collapses
        an N-row leg to ceil(N/batch). Empty list is a no-op; returns the number
        of rows imported.
        """
        if not rows:
            return 0
        resp = self._post("/v1/telemetry/import_batch", {"table": table, "rows": rows})
        return int(resp.get("imported", 0))

    # ── ids probe (RDR-178 wave-2 P1, bead nexus-s3dd4.3) ──────────────────────

    def probe_ids(self, table: str, keys: list[list[Any]]) -> list[list[Any]]:
        """Membership-probe for the verify-fill inner loop: given candidate
        conflict-key tuples for one of the six telemetry tables, return the
        subset already present in the target.

        Each element of *keys* is the table's conflict-key tuple IN COLUMN
        ORDER (``tenant_id`` is implicit via RLS; see
        ``TelemetryRepository.probeIds`` for the authoritative per-table
        column order, transcribed verbatim from the UNIQUE indexes / PK in
        ``telemetry-001-baseline.xml``):

        - ``relevance_log``:    ``[query, chunk_id, action, session_id, timestamp]``
        - ``search_telemetry``: ``[ts, query_hash, collection]``
        - ``tier_writes``:      ``[session_id, ts, tool, tier]``
        - ``nx_answer_runs``:   ``[question, created_at]``
        - ``hook_failures``:    ``[doc_id, hook_name, occurred_at]``
        - ``frecency``:         ``[chunk_id]``

        Paged transparently at ``QUOTAS.MAX_RECORDS_PER_WRITE`` (300)
        candidates per request — mirrors the batch discipline of
        ``HttpVectorClient.existing_ids``. Calls
        ``POST /v1/telemetry/ids/probe`` once per page.

        Returned tuples are echoed back VERBATIM from *keys* (the service
        never reconstructs them from stored values — see
        ``TelemetryRepository.probeIds``), so a caller computing
        ``set(map(tuple, source_keys)) - set(map(tuple, present))`` cannot
        false-negative on timestamp string-formatting drift (e.g. a stored
        ``+00:00`` offset vs. a source ``Z`` suffix).

        FAIL-CLOSED CONTRACT (nexus-te885.6): unlike
        ``HttpVectorClient.existing_ids`` — which swallows transport errors
        and degrades to ``set()`` — this method does NOT catch exceptions.
        An unreachable/erroring service propagates as an ``httpx`` exception
        (via :meth:`_post`'s ``_raise_for_status``) rather than silently
        reading as "nothing exists", which would otherwise make a
        verify-fill caller believe every candidate is missing and trigger a
        needless (if harmless, since ``importBatch`` is idempotent) full
        re-send. Callers building ``IdentitySource``-style tri-state
        semantics should catch at the call site, not expect this method to
        degrade quietly.
        """
        if not keys:
            return []
        page = QUOTAS.MAX_RECORDS_PER_WRITE
        present: list[list[Any]] = []
        for start in range(0, len(keys), page):
            batch = keys[start : start + page]
            resp = self._post("/v1/telemetry/ids/probe", {"table": table, "keys": batch})
            present.extend(resp.get("present") or [])
        return present
