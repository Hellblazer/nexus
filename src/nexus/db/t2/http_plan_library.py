# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpPlanLibrary — thin HTTP client over the RDR-152 Java plans service.

Drop-in replacement for :class:`~nexus.db.t2.plan_library.PlanLibrary`.
Activated by setting ``NX_STORAGE_BACKEND=service`` (or
``NX_STORAGE_BACKEND_PLANS=service``).

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

All methods send ``Authorization: Bearer <token>`` and
``X-Nexus-Tenant: default`` (``DEFAULT_TENANT``) on every request.

Interface parity (bead nexus-gmiaf.11, RDR-152 P2.1):
    save_plan, get_plan, get_plan_by_dimensions,
    delete_plan, set_plan_disabled, set_plan_enabled,
    set_scope_tags, list_active_plans, increment_match_metrics,
    increment_run_started, increment_run_outcome,
    search_plans, list_plans, plan_exists, close
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

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
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_PLANS=service. "
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
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_PLANS=service. "
            "Set it to the bearer token configured in the nexus-service."
        )

    return host, port, token


# ── HttpPlanLibrary ────────────────────────────────────────────────────────────


class HttpPlanLibrary:
    """PlanLibrary drop-in that delegates to the RDR-152 Java HTTP service.

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
                        "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_PLANS=service."
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
        # Keep-alive connection pool.
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        _log.info("http_plan_library.init", base_url=self._base_url, tenant=tenant)

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()
        _log.debug("http_plan_library.closed")

    # ── Write ──────────────────────────────────────────────────────────────────

    def save_plan(
        self,
        query: str,
        plan_json: str,
        outcome: str = "success",
        tags: str = "",
        project: str = "",
        ttl: int | None = None,
        *,
        name: str | None = None,
        verb: str | None = None,
        scope: str | None = None,
        dimensions: str | None = None,
        default_bindings: str | None = None,
        parent_dims: str | None = None,
        scope_tags: str | None = None,
    ) -> int:
        """Upsert a plan. Returns the row id (BIGSERIAL, always positive).

        Matches the PlanLibrary.save_plan signature exactly.
        The ``match_text`` synthesis and ``scope_tags`` normalization that
        happen in PlanLibrary.save_plan before the SQL call are NOT
        replicated here — the Java service's /v1/plans/save endpoint
        receives the raw caller-supplied values and performs its own
        normalisation (scope_tags inference is Python-side since the
        Java service has no inference logic).

        Unlike the direct SQLite path (which synthesizes match_text from
        verb/name/scope in Python), this client sends ``match_text`` built
        here so the service stores the correct FTS payload.
        """
        from nexus.db.t2.plan_library import (
            _synthesize_match_text,
            _infer_scope_tags,
            _normalize_scope_string,
            _SCOPE_AGNOSTIC_SENTINELS,
        )

        match_text = _synthesize_match_text(
            description=query, verb=verb, name=name, scope=scope,
        )

        # Scope-tag normalization mirrors PlanLibrary.save_plan exactly.
        if scope_tags:
            parts = [
                _normalize_scope_string(p.strip())
                for p in scope_tags.split(",")
                if p.strip() and p.strip() not in _SCOPE_AGNOSTIC_SENTINELS
            ]
            stored_scope_tags = ",".join(sorted({p for p in parts if p}))
        elif scope_tags is None:
            stored_scope_tags = _infer_scope_tags(plan_json)
            if not stored_scope_tags and project:
                candidate = _normalize_scope_string(project.strip())
                if candidate and candidate not in _SCOPE_AGNOSTIC_SENTINELS:
                    stored_scope_tags = candidate
        else:
            stored_scope_tags = ""

        payload: dict[str, Any] = {
            "query":            query,
            "plan_json":        plan_json,
            "outcome":          outcome,
            "tags":             tags or "",
            "project":          project or "",
            "ttl":              ttl,
            "name":             name,
            "verb":             verb,
            "scope":            scope,
            "dimensions":       dimensions,
            "default_bindings": default_bindings,
            "parent_dims":      parent_dims,
            "scope_tags":       stored_scope_tags,
            "match_text":       match_text,
        }
        resp = self._post("/v1/plans/save", payload)
        return int(resp["id"])

    def import_plan(
        self,
        *,
        project: str,
        query: str,
        plan_json: str,
        outcome: str,
        tags: str,
        created_at: str,
        ttl: int | None = None,
        name: str | None = None,
        verb: str | None = None,
        scope: str | None = None,
        dimensions: str | None = None,
        default_bindings: str | None = None,
        parent_dims: str | None = None,
        use_count: int = 0,
        last_used: str | None = None,
        match_count: int = 0,
        match_conf_sum: float = 0.0,
        success_count: int = 0,
        failure_count: int = 0,
        scope_tags: str = "",
        match_text: str = "",
        disabled_at: str | None = None,
    ) -> int:
        """Fidelity-preserving import (bead nexus-gmiaf.11, RDR-152 P2.1).

        Unlike :meth:`save_plan` (which routes through ``/v1/plans/save`` and
        resets counters to 0 and stamps ``created_at=now()``), this method
        calls ``POST /v1/plans/import`` which writes ``created_at``,
        ``use_count``, ``last_used``, ``match_count``, ``match_conf_sum``,
        ``success_count``, ``failure_count``, and ``disabled_at`` verbatim
        from the source row. Re-runs are idempotent.

        Args:
            created_at: Required ISO-8601 UTC string, e.g. ``"2026-05-15T08:30:00Z"``.
            last_used:  Optional ISO-8601 UTC string or ``None``.
            disabled_at: Optional ISO-8601 UTC string or ``None``.

        Returns:
            The Postgres row id (BIGSERIAL, always positive).
        """
        payload: dict[str, Any] = {
            "project":          project or "",
            "query":            query,
            "plan_json":        plan_json,
            "outcome":          outcome,
            "tags":             tags or "",
            "created_at":       created_at,
            "ttl":              ttl,
            "name":             name,
            "verb":             verb,
            "scope":            scope,
            "dimensions":       dimensions,
            "default_bindings": default_bindings,
            "parent_dims":      parent_dims,
            "use_count":        use_count,
            "match_count":      match_count,
            "match_conf_sum":   match_conf_sum,
            "success_count":    success_count,
            "failure_count":    failure_count,
            "scope_tags":       scope_tags or "",
            "match_text":       match_text or "",
        }
        if last_used is not None:
            payload["last_used"] = last_used
        if disabled_at is not None:
            payload["disabled_at"] = disabled_at

        resp = self._post("/v1/plans/import", payload)
        return int(resp["id"])

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        """Return the plan dict for *plan_id*, or ``None`` if absent."""
        resp = self._client.get("/v1/plans/get", params={"id": plan_id})
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp, "get_plan")
        return _normalize(resp.json())

    def get_plan_by_dimensions(
        self, *, project: str, dimensions: str,
    ) -> dict[str, Any] | None:
        """Return the plan with canonical *dimensions* JSON, or ``None``."""
        resp = self._client.get(
            "/v1/plans/get",
            params={"project": project, "dimensions": dimensions},
        )
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp, "get_plan_by_dimensions")
        return _normalize(resp.json())

    # ── Delete ─────────────────────────────────────────────────────────────────

    def delete_plan(self, plan_id: int) -> int:
        """Delete plan by *plan_id*. Returns 1 if deleted, 0 if not found."""
        resp = self._client.delete("/v1/plans/delete", params={"id": plan_id})
        self._raise_for_status(resp, "delete_plan")
        return 1 if resp.json().get("deleted") else 0

    # ── Disable / enable ───────────────────────────────────────────────────────

    def set_plan_disabled(self, plan_id: int, *, reason: str = "") -> bool:
        """Soft-disable the plan. Returns True if updated, False if not found.

        When *reason* is non-empty the Java endpoint appends
        ``disable-reason:<reason>`` to the ``tags`` column (removing any
        existing ``disable-reason:*`` tag first), mirroring
        ``PlanLibrary.set_plan_disabled`` exactly.
        """
        payload: dict[str, Any] = {"id": plan_id}
        if reason:
            payload["reason"] = reason
        resp = self._post("/v1/plans/disable", payload)
        return bool(resp.get("updated"))

    def set_plan_enabled(self, plan_id: int) -> bool:
        """Re-enable a disabled plan. Returns True if updated, False if not found."""
        resp = self._post("/v1/plans/enable", {"id": plan_id})
        return bool(resp.get("updated"))

    # ── Scope tags ─────────────────────────────────────────────────────────────

    def set_scope_tags(self, plan_id: int, scope_tags: str) -> bool:
        """Write explicit *scope_tags*. Returns True if updated."""
        resp = self._post("/v1/plans/set_scope_tags", {"id": plan_id, "scope_tags": scope_tags})
        return bool(resp.get("updated"))

    # ── List / search ──────────────────────────────────────────────────────────

    def list_active_plans(
        self,
        *,
        outcome: str = "success",
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Return every non-expired, non-disabled plan for the given *outcome*."""
        params: dict[str, Any] = {"outcome": outcome}
        if project:
            params["project"] = project
        resp = self._client.get("/v1/plans/list_active", params=params)
        self._raise_for_status(resp, "list_active_plans")
        return [_normalize(r) for r in resp.json()]

    def increment_match_metrics(
        self, plan_id: int, *, confidence: float | None,
    ) -> None:
        """Bump ``match_count`` and (when scored) ``match_conf_sum``."""
        payload: dict[str, Any] = {"id": plan_id}
        if confidence is not None:
            payload["confidence"] = confidence
        self._post("/v1/plans/metrics/match", payload)

    def increment_run_started(self, plan_id: int) -> None:
        """Bump ``use_count`` and stamp ``last_used``."""
        self._post("/v1/plans/metrics/run_start", {"id": plan_id})

    def increment_run_outcome(self, plan_id: int, *, success: bool) -> None:
        """Bump ``success_count`` or ``failure_count``."""
        self._post("/v1/plans/metrics/run_outcome", {"id": plan_id, "success": success})

    def search_plans(
        self,
        query: str,
        limit: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """FTS search over plans. Returns plans ordered by ts_rank relevance."""
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if project:
            payload["project"] = project
        resp = self._post("/v1/plans/search", payload)
        if isinstance(resp, list):
            return [_normalize(r) for r in resp]
        return []

    def list_plans(
        self,
        limit: int = 20,
        project: str = "",
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        """Return most recent non-expired plans, ordered by created_at DESC."""
        params: dict[str, Any] = {
            "limit":            limit,
            "include_disabled": "true" if include_disabled else "false",
        }
        if project:
            params["project"] = project
        resp = self._client.get("/v1/plans/list", params=params)
        self._raise_for_status(resp, "list_plans")
        return [_normalize(r) for r in resp.json()]

    def plan_exists(self, query: str, tag: str) -> bool:
        """Return True if any plan with *query* has *tag* as a comma-separated token."""
        resp = self._client.get(
            "/v1/plans/exists", params={"query": query, "tag": tag},
        )
        self._raise_for_status(resp, "plan_exists")
        return bool(resp.json().get("exists"))

    # ── Internal helpers ───────────────────────────────────────────────────────

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
            f"HttpPlanLibrary.{op} failed: HTTP {resp.status_code}: {detail}",
            request=resp.request,
            response=resp,
        )


# ── Normalisation helper ───────────────────────────────────────────────────────

def _normalize(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert a service response row to PlanLibrary-compatible dict.

    Normalization rules matching ``_row_to_dict(row)`` from SQLite PlanLibrary:

    - ``id``, ``use_count``, ``match_count``, ``success_count``,
      ``failure_count``, ``ttl``: cast to ``int`` (JSON may send as float).
    - ``match_conf_sum``: cast to ``float``.
    - ``tags``, ``scope_tags``, ``match_text``, ``project``: guaranteed to be
      strings; fallback to ``""`` for defence-in-depth.
    - ``outcome``: fallback to ``"success"`` if absent/None.
    - ``last_used``, ``disabled_at``: pass through as-is (string or None).
    - ``created_at``: pass through as UTC second-precision ISO string.
    """
    if row is None:
        return None

    # Integer fields
    for field in ("id", "use_count", "match_count", "success_count", "failure_count"):
        if row.get(field) is not None:
            row[field] = int(row[field])

    # Float fields
    if row.get("match_conf_sum") is not None:
        row["match_conf_sum"] = float(row["match_conf_sum"])
    else:
        row["match_conf_sum"] = 0.0

    # TTL: int or None
    if row.get("ttl") is not None:
        row["ttl"] = int(row["ttl"])

    # String fields with defaults
    for field, default in (
        ("tags", ""),
        ("scope_tags", ""),
        ("match_text", ""),
        ("project", ""),
    ):
        if row.get(field) is None:
            row[field] = default

    # Outcome default
    if row.get("outcome") is None:
        row["outcome"] = "success"

    return row
