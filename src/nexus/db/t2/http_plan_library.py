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

from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-f2qvx.1: construction, credential/endpoint refresh-on-401, and the
# HTTP transport itself (_post/_get/_delete) are now inherited wholesale
# from RefreshableHttpStoreMixin — HttpPlanLibrary no longer bakes a
# ``self._headers`` dict or a ``httpx.Client(base_url=..., headers=...)``
# at construction time, which is what let a rotated bearer or a
# supervisor-restart port change go silently stale for the life of the
# instance. See ``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.
from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


# ── HttpPlanLibrary ────────────────────────────────────────────────────────────


class HttpPlanLibrary(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """PlanLibrary drop-in that delegates to the RDR-152 Java HTTP service.

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
        from nexus.db.t2.plan_library import (  # noqa: PLC0415 — deferred to avoid circular import (plan_library)
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

    @staticmethod
    def build_import_row(**kwargs: Any) -> dict[str, Any]:
        """Build one ``import_plans_batch`` row dict (same field shape as the
        :meth:`import_plan` payload). Accepts the same keyword args; optional
        ``last_used`` / ``disabled_at`` are omitted when ``None``."""
        row: dict[str, Any] = {
            "project":          kwargs.get("project") or "",
            "query":            kwargs["query"],
            "plan_json":        kwargs["plan_json"],
            "outcome":          kwargs.get("outcome") or "success",
            "tags":             kwargs.get("tags") or "",
            "created_at":       kwargs["created_at"],
            "ttl":              kwargs.get("ttl"),
            "name":             kwargs.get("name"),
            "verb":             kwargs.get("verb"),
            "scope":            kwargs.get("scope"),
            "dimensions":       kwargs.get("dimensions"),
            "default_bindings": kwargs.get("default_bindings"),
            "parent_dims":      kwargs.get("parent_dims"),
            "use_count":        kwargs.get("use_count", 0),
            "match_count":      kwargs.get("match_count", 0),
            "match_conf_sum":   kwargs.get("match_conf_sum", 0.0),
            "success_count":    kwargs.get("success_count", 0),
            "failure_count":    kwargs.get("failure_count", 0),
            "scope_tags":       kwargs.get("scope_tags") or "",
            "match_text":       kwargs.get("match_text") or "",
        }
        if kwargs.get("last_used") is not None:
            row["last_used"] = kwargs["last_used"]
        if kwargs.get("disabled_at") is not None:
            row["disabled_at"] = kwargs["disabled_at"]
        return row

    def import_plans_batch(self, rows: list[dict[str, Any]]) -> int:
        """RDR-176 P3 (bead nexus-t9rmg.18): fidelity-preserving BULK import.

        POSTs all *rows* (built via :meth:`build_import_row`) to
        ``/v1/plans/import_batch`` in ONE request — the service lands them under
        one tenant transaction. Collapses an N-row leg to ceil(N/batch). Caller
        keeps each batch within the per-write quota. Empty list is a no-op.
        """
        if not rows:
            return 0
        resp = self._post("/v1/plans/import_batch", {"rows": rows})
        return int(resp.get("imported", 0))

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        """Return the plan dict for *plan_id*, or ``None`` if absent.

        The mixin's ``_get`` raises ``httpx.HTTPStatusError`` on ANY non-2xx
        (including 404 — self-heal retry only applies to 401/connection
        errors). ``get_plan``'s contract is "not found -> None", not an
        exception, so catch specifically the 404 case here and re-raise
        anything else untouched (mirrors ``HttpMemoryStore.get``).
        """
        try:
            resp = self._get("/v1/plans/get", params={"id": plan_id})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return _normalize(resp)

    def get_plan_by_dimensions(
        self, *, project: str, dimensions: str,
    ) -> dict[str, Any] | None:
        """Return the plan with canonical *dimensions* JSON, or ``None``."""
        try:
            resp = self._get(
                "/v1/plans/get",
                params={"project": project, "dimensions": dimensions},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return _normalize(resp)

    # ── Delete ─────────────────────────────────────────────────────────────────

    def delete_plan(self, plan_id: int) -> int:
        """Delete plan by *plan_id*. Returns 1 if deleted, 0 if not found."""
        resp = self._delete("/v1/plans/delete", params={"id": plan_id})
        return 1 if resp.get("deleted") else 0

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
        resp = self._get("/v1/plans/list_active", params=params)
        return [_normalize(r) for r in resp]

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
        resp = self._get("/v1/plans/list", params=params)
        return [_normalize(r) for r in resp]

    def plan_exists(self, query: str, tag: str) -> bool:
        """Return True if any plan with *query* has *tag* as a comma-separated token."""
        resp = self._get("/v1/plans/exists", params={"query": query, "tag": tag})
        return bool(resp.get("exists"))


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

    # Nullable timestamp fields: defensive no-op now that the service includes
    # null fields (RDR-152 nexus-fjwxh flipped the handlers to JsonInclude.ALWAYS
    # for SQLite parity). Kept as belt-and-suspenders so callers can rely on dict
    # access, not .get(), regardless of serialization config.
    for nullable_field in ("disabled_at", "last_used"):
        if nullable_field not in row:
            row[nullable_field] = None

    return row
