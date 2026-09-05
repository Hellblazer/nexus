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

ETL-only import methods (sole caller telemetry_etl.py was deleted with the
SQLite->PG ETL readers, nexus-i711w Stage 2 sub-stage A; these retire with
the 7.0.0 wave — see ``_ETL_DYING_WITH_THE_WAVE`` in
tests/db/test_http_t2_store_parity.py):
    import_relevance_row, import_search_row, import_tier_write,
    import_nx_answer_run, import_hook_failure, import_frecency_row

Route mapping (matches TelemetryHandler Java):
    POST /v1/telemetry/relevance/log    — log_relevance
    GET  /v1/telemetry/relevance/query  — get_relevance_log
    GET  /v1/telemetry/relevance/stats  — get_relevance_stats (nexus-v0x32)
    POST /v1/telemetry/relevance/expire — expire_relevance_log
    POST /v1/telemetry/search/batch     — log_search_batch
    GET  /v1/telemetry/search/stats     — query_collection_stats
    POST /v1/telemetry/search/trim      — trim_search_telemetry (dry_run=True
                                           previews the count without deleting,
                                           same WHERE predicate as the delete)
    POST /v1/telemetry/rename_collection — rename_collection
    POST /v1/telemetry/import           — import_* methods (ETL)
    POST /v1/telemetry/import_batch     — import_rows_batch (bulk ETL)
    POST /v1/telemetry/ids/probe        — probe_ids (verify-fill inner loop, RDR-178 wave-2 P1)
    GET  /v1/telemetry/nx_answer_runs/query — query_nx_answer_runs (nexus-eho3u: the read
                                               half of a formerly write-only instrument)
    GET  /v1/telemetry/tier_writes/list — list_tier_writes (nexus-onjvy gap 4: per-row
                                           target_title, the aggregate query route cannot
                                           carry it; capped page + exact total, same
                                           envelope discipline as list_hook_failures)
    POST /v1/telemetry/index_failures/record        — record_index_failure (nexus-nukn3)
    POST /v1/telemetry/index_failures/record_batch  — record_index_failures_batch
    GET  /v1/telemetry/index_failures/list          — list_index_failures
    POST /v1/telemetry/index_failures/trim          — trim_index_failures
    POST /v1/telemetry/index_failures/acknowledge   — acknowledge_index_failure
    GET  /v1/telemetry/index_failures/acks          — list_index_failure_acknowledgments
    POST /v1/telemetry/index_failures/unacknowledge — unacknowledge_index_failure
    POST /v1/telemetry/capability_census/record — record_capability_census (nexus-gjv9b
                                           PART 1: upsert on (tenant_id, session_id))
    GET  /v1/telemetry/capability_census/query  — query_capability_census
    POST /v1/telemetry/routing_events/record    — record_routing_event (nexus-gjv9b
                                           PART 2: NOT called by the routing hooks
                                           themselves, which POST via urllib directly —
                                           see that method's own docstring)
    GET  /v1/telemetry/routing_events/list      — list_routing_events
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from nexus.db.limits import QUOTAS

_log = structlog.get_logger(__name__)


def normalize_since_filter(since: str) -> str:
    """Normalize a caller-supplied ``since`` filter to full UTC ISO-8601.

    nexus-spbay: the engine's historical ``since`` parser (``parseTs``)
    fell back to ``now()`` on any form ``OffsetDateTime.parse`` cannot
    read — a bare date like ``2026-08-01`` silently became "since right
    now", so every date-filtered telemetry read returned a confirmatory
    zero (measured against 214 nx_answer_runs rows, T2 [23879]). Engines
    carrying the paired fix (``parseSinceFilter``) accept date-only and
    naive forms and 400 on garbage; THIS normalization is the client half
    of the [additive] pairing — it makes those forms work against every
    engine ALREADY deployed. Unrecognizable values raise ``ValueError``
    here: fail loud client-side rather than let an old engine manufacture
    an empty set.
    """
    try:
        dt = datetime.fromisoformat(since.strip())
    except ValueError as exc:
        raise ValueError(
            f"since is not a recognizable ISO-8601 date or datetime: {since!r}"
            " (accepted: 2026-08-01, 2026-08-01T12:00:00, 2026-08-01T12:00:00Z)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

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

    def query_tier_writes(
        self,
        *,
        session_id: str | None = None,
        since: str | None = None,
        last_n: int | None = None,
    ) -> list[tuple[str, str, str | None, str | None, int]]:
        """Aggregated tier-write counts — ``GET /v1/telemetry/tier_writes/query``.

        nexus-59wjj: the service-mode twin of ``tier_status._query`` (local
        SQLite). Same row shape ``(tool, tier, agent, project, count)`` and
        the same filter precedence (``last_n`` = last N distinct sessions >
        ``session_id`` > ``since``); the engine returns ``""`` for NULL
        agent/project, mapped back to ``None`` here for local parity.

        Requires engine >= the nexus-59wjj cut; older engines 404 — callers
        surface their honest "service-backed" message on failure.
        """
        params: dict[str, Any] = {}
        if last_n:
            params["last_n"] = last_n
        elif session_id:
            params["session_id"] = session_id
        elif since:
            params["since"] = normalize_since_filter(since)
        data = self._get("/v1/telemetry/tier_writes/query", params=params)
        return self._map_tier_write_rows(data)

    def list_tier_writes(
        self,
        *,
        session_id: str | None = None,
        since: str | None = None,
        last_n: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Per-row tier-write detail — ``GET /v1/telemetry/tier_writes/list``.

        nexus-onjvy gap 4: ``target_title`` is accepted and stored by
        :meth:`record_tier_write`, but until this route landed it was readable
        through NO route in service mode — :meth:`query_tier_writes` is an
        AGGREGATE grouped by (tool, tier, agent, project) with no target slot,
        and a per-row title carried on an aggregated group would be incoherent.
        This is a SEPARATE unaggregated read path, one row per write.

        Review finding (reviewer/critic convergence, 2026-08-08): this was the
        only tier_writes read path with no page cap — an unfiltered call
        returned every row ever recorded for the tenant, and callers had no
        kwarg to bound it even deliberately. Same capped-page-plus-exact-total
        envelope discipline as :meth:`list_hook_failures`:

        Returns ``{"rows": [(tool, tier, agent, project, target_title), ...],
        "total": int}``. ``rows`` is capped at ``limit``, most recent write
        first; ``total`` is the FULL count for the ``session_id``/``since``/
        ``last_n`` filter, independent of ``limit`` — a caller asking for the
        last 20 rows must not see a total of 20. Unlike
        :meth:`query_tier_writes`, NULL ``agent``/``project``/``target_title``
        map to ``None`` directly (the engine sends JSON ``null`` on this
        route, not ``""`` — there is no aggregation collapsing many NULLs
        into one bucket to disambiguate).

        Same filter precedence as :meth:`query_tier_writes` (``last_n`` >
        ``session_id`` > ``since``); requires an engine carrying the
        nexus-onjvy cut — older engines 404.

        Args:
            limit: max rows in the returned page (default 100, matching
                :meth:`list_hook_failures`). Does not affect ``total``.
        """
        params: dict[str, Any] = {"limit": limit}
        if last_n:
            params["last_n"] = last_n
        elif session_id:
            params["session_id"] = session_id
        elif since:
            params["since"] = normalize_since_filter(since)
        resp = self._get("/v1/telemetry/tier_writes/list", params=params)
        if not isinstance(resp, dict):  # defensive: a stripped proxy response
            return {"rows": [], "total": 0}
        rows = [
            (
                str(r.get("tool", "")),
                str(r.get("tier", "")),
                r.get("agent"),
                r.get("project"),
                r.get("target_title"),
            )
            for r in (resp.get("rows") or [])
        ]
        return {"rows": rows, "total": int(resp.get("total") or 0)}

    def query_tier_writes_once(
        self,
        *,
        session_id: str,
        timeout: float = 2.0,
    ) -> list[tuple[str, str, str | None, str | None, int]]:
        """Single-attempt ``tier_writes/query`` for latency-critical callers.

        nexus-ov13k (session-end summary): the normal ``_get`` path composes
        the mixin's gateway-retry sleeps (up to 17s) and the re-resolve leg's
        12s lease-wait — a 20-50s worst case that a per-request ``timeout``
        kwarg does NOT bound (both reviewers, independently). This variant
        issues exactly ONE raw request against the already-resolved endpoint
        with a hard per-request *timeout*: no gateway backoff, no re-resolve
        retry, no lease wait. Any failure raises to the caller, whose
        contract is best-effort. Not for general use — every other caller
        wants the self-healing transport.
        """
        import httpx  # noqa: PLC0415 — deferred import — only needed on this path

        resp = self._client.request(
            "GET",
            self._base_url + "/v1/telemetry/tier_writes/query",
            headers=self._auth_headers(),
            params={"session_id": session_id},
            timeout=httpx.Timeout(timeout),
        )
        resp.raise_for_status()
        return self._map_tier_write_rows(resp.json() if resp.content else [])

    @staticmethod
    def _map_tier_write_rows(
        data: Any,
    ) -> list[tuple[str, str, str | None, str | None, int]]:
        return [
            (
                str(r.get("tool", "")),
                str(r.get("tier", "")),
                r.get("agent") or None,
                r.get("project") or None,
                int(r.get("count", 0)),
            )
            for r in (data or [])
        ]

    @property
    def tenant(self) -> str:
        """The store's tenant. Public read-only: the verify-fill watermark
        keys per (service_url, tenant, table) — a shared cloud engine serves
        many tenants from one URL, and tenant B must never trust a rowid
        floor recorded against tenant A's counts/markers (critique
        68509ac8)."""
        return self._tenant or ""

    @property
    def base_url(self) -> str:
        """The resolved service base URL. Public read-only: the verify-fill
        watermark (nexus-te885.10) keys its per-target state on it, so a
        different target (fresh service after rollback + re-init) never
        inherits another target's watermark."""
        return self._base_url or ""

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

    def get_relevance_stats(self) -> dict[str, Any]:
        """Whole-tenant relevance_log aggregate (nexus-v0x32, playbook §4.5
        telemetry baseline): ``{"count": int, "oldest": str | None,
        "newest": str | None}``. Calls ``GET /v1/telemetry/relevance/stats``
        — no filters, no paging, unlike :meth:`get_relevance_log`'s capped
        300-row window (which cannot answer "row count" or "oldest
        surviving row" honestly at all, RDR proven 2026-08-27).

        Raises on transport/HTTP failure (including 404 on an engine that
        predates this route) — callers render the honest failure message
        via :func:`relevance_stats_read_failure_message`, same three-way
        split as :func:`nx_answer_runs_read_failure_message`.
        """
        data = self._get("/v1/telemetry/relevance/stats")
        if not isinstance(data, dict):  # defensive: a stripped proxy response
            return {"count": 0, "oldest": None, "newest": None}
        return {
            "count": int(data.get("count") or 0),
            "oldest": data.get("oldest"),
            "newest": data.get("newest"),
        }

    #: RDR-196 .p1d (nexus-nyry9.10) capability-probe cache. Class-level
    #: default; the first probe on THIS instance shadows it with an
    #: instance attribute — "cache per store instance" per the bead's own
    #: design note. Note this buys less than it sounds: ``T2Database.__init__``
    #: constructs a FRESH ``HttpTelemetryStore()`` on every ``t2_ctx()`` call
    #: (see ``mcp_infra.t2_ctx``'s own docstring — "fresh per call"), so in
    #: production this cache rarely outlives a single ``_nx_answer_record_run``
    #: call and every nx_answer invocation re-probes. That is a known,
    #: accepted cost (one extra GET on an already multi-second-plus
    #: operation), not a bug — see the class docstring for the full account.
    _nx_answer_steps_supported_cache: bool | None = None

    def _supports_nx_answer_steps(self) -> bool:
        """Capability probe (RDR-196 .p1d): does the engine THIS store talks
        to accept ``steps[]`` on ``POST /v1/telemetry/nx_answer_runs/record``?

        Reads ``GET /version``'s ``nx_answer_steps_supported`` field (added
        at ``.p1c`` / nexus-nyry9.9, unconditionally ``true`` whenever
        present — see ``TelemetryHandler``/``VersionHandler`` on the engine
        side). Cached per store INSTANCE, matching
        :func:`nexus.db.http_vector_client.get_http_vector_client`'s
        "the deployed engine does not change under a running client"
        reasoning for the ``INCOMPATIBLE`` probe class — simpler than that
        module's retry-window machinery because this probe is advisory
        (a `False` degrades to run-row-only telemetry, never a hard
        failure), not gating.

        NEVER raises. An absent field, a non-2xx response, or any
        transport failure (including hitting an engine that predates the
        ``/version`` route entirely, unlikely but not assumed) all read as
        unsupported — degrade to a run-row-only write.

        CORRECTION (code-review, T2 review-nexus-nyry9.10 [23091]): this
        probe does NOT exist to prevent a 400. ``TelemetryHandler``'s
        Jackson ``MAPPER`` has ``FAIL_ON_UNKNOWN_PROPERTIES=false`` and
        parses the request body into a generic ``Map`` — an engine that
        predates ``.p1c`` would silently IGNORE an unrecognized ``steps``
        key and still answer 200, probe or no probe. The probe's actual
        value is OBSERVABILITY: without it, a client talking to an old
        engine would believe its per-step telemetry landed (the run row
        writes fine either way) when it was actually dropped on the
        floor, unlogged. Checking first lets :meth:`record_nx_answer_run`
        log ``nx_answer_steps_unsupported_by_engine`` the moment it knows
        the data will be lost, instead of never finding out. Logged once
        per store instance so a genuinely-broken probe is still visible
        (nexus-bwulw class: the public edge has stubbed ``/version``
        before).
        """
        if self._nx_answer_steps_supported_cache is not None:
            return self._nx_answer_steps_supported_cache
        try:
            resp = self._get("/version")
        except Exception as exc:  # noqa: BLE001 — capability probe must never raise; caller degrades to run-row-only
            _log.warning("nx_answer_steps_probe_failed", error=str(exc))
            self._nx_answer_steps_supported_cache = False
            return False
        supported = bool(resp.get("nx_answer_steps_supported")) if isinstance(resp, dict) else False
        self._nx_answer_steps_supported_cache = supported
        return supported

    def record_nx_answer_run(
        self,
        *,
        question: str,
        plan_id: int | None,
        matched_confidence: float | None,
        step_count: int,
        final_text: str,
        cost_usd: float | None,
        duration_ms: int,
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record an nx_answer run. Calls ``POST /v1/telemetry/nx_answer_runs/record``.

        ``cost_usd`` is ``float | None`` (RDR-196 .p1d, nexus-nyry9.10):
        ``None`` means the caller could not determine a real cost (no step
        carried a known figure), never a fabricated ``0.0``; ``0.0`` means a
        measured free run (for example all-SQL steps). The parent
        ``nx_answer_runs.cost_usd`` column is nullable since changeset
        ``telemetry-007-3`` (nexus-lme1s, rides the same engine cut as the
        ``steps[]`` write), so the JSON ``null`` this method emits is stored
        as NULL server-side. Against an engine that predates 007-3 the
        column still carries ``NOT NULL DEFAULT 0.0`` and a ``null`` lands
        as ``0.0`` -- the same engine that also lacks the ``steps[]`` route,
        which the capability probe below reports.

        ``steps``: per-step telemetry rows (RDR-196 .p1b/.p1c/.p1d),
        included on the wire ONLY when :meth:`_supports_nx_answer_steps`
        reports the engine accepts them — an older engine never sees this
        key at all, matching the ``.p1c`` degradation contract ("absent/
        empty writes only the parent"). A `None`/empty *steps* never adds
        the key either way — nothing to gate, nothing lost, no warning.
        When *steps* IS non-empty but the probe reports unsupported, the
        run row still writes either way (this method always degrades,
        never refuses — see :meth:`_supports_nx_answer_steps`'s docstring
        for why the "never a 400" framing is about client-side visibility,
        not crash prevention: an old engine's lenient JSON parser would
        silently ignore an unrecognized ``steps`` key on its own and still
        answer 200). Gating on the probe instead of sending unconditionally
        is what lets a ``structlog`` warning fire the moment real per-step
        telemetry is about to be silently dropped, rather than the client
        wrongly believing it landed (RDR-196 .p1d DO: "probe says
        unsupported -> run-row-only payload + logged warning").
        """
        payload: dict[str, Any] = {
            "question":           question,
            "plan_id":            plan_id,
            "matched_confidence": matched_confidence,
            "step_count":         step_count,
            "final_text":         final_text,
            "cost_usd":           cost_usd,
            "duration_ms":        duration_ms,
        }
        if steps:
            if self._supports_nx_answer_steps():
                payload["steps"] = steps
            else:
                _log.warning(
                    "nx_answer_steps_unsupported_by_engine",
                    step_count=len(steps),
                    consequence="run row recorded without per-step telemetry",
                )
        self._post("/v1/telemetry/nx_answer_runs/record", payload)

    def query_nx_answer_runs(
        self,
        *,
        since: str | None = None,
        limit: int = 20,
        include_steps: bool = False,
    ) -> dict[str, Any]:
        """Read nx_answer_runs rows plus exact aggregates (nexus-eho3u).

        SERVICE-MODE ONLY — same shape as :meth:`list_hook_failures`: until
        engine-service carries the ``/nx_answer_runs/query`` route, every
        ``nx_answer`` call recorded a row here and nothing ever read one
        back (the ``import_nx_answer_run`` ETL path does not count — it is
        write-only in the other direction). Registered in
        ``T2_SUPPLEMENTAL_CONTRACT`` for the same reason as
        ``list_hook_failures``: no SQLite twin, so the parity tripwire
        cannot see it on its own.

        Calls ``GET /v1/telemetry/nx_answer_runs/query``.

        Args:
            since: only rows with ``created_at >= since``; ``None``/empty
                means no time bound.
            limit: max rows in the returned page; does not affect the
                aggregates.
            include_steps: RDR-196 .p1c-b (nexus-lme1s) — when ``True``,
                sends ``?include_steps=true`` and each row in the returned
                page gains a ``steps`` list (one dict per
                ``nx_answer_steps`` child row, ordered by ``step_index``,
                using the same field names the write side
                (``record_nx_answer_run``'s ``steps`` payload) accepts:
                ``step_index, operator, source, model, input_tokens,
                output_tokens, cost_usd, elapsed_ms, ok, bundled_steps``).
                Rows already pass through server JSON verbatim below, so no
                extra reconstruction is needed here — the ``steps`` key
                simply arrives already-present on each row dict when the
                server includes it. Requires an engine build that carries
                the .p1c-b read route (probe via ``GET /version``'s
                ``nx_answer_steps_supported`` field, same capability flag
                :class:`HttpTelemetryStore`'s write-side probe already
                uses, if a caller needs to know ahead of the call whether
                steps will actually come back) — an older engine simply
                ignores the unknown query param and returns rows with no
                ``steps`` key, not an error.

        Returns a dict with:
            rows: last *limit* runs, newest first — each
                ``{id, question, plan_id, matched_confidence, step_count,
                final_text, cost_usd, duration_ms, created_at}`` plus, when
                ``include_steps=True`` and the engine supports it, ``steps``.
            total: exact row count over the WHOLE *since*-filtered set, not
                the page — a caller asking for the last 5 runs must not see
                a total of 5.
            oldest_created_at: oldest row in the filtered set ("" if empty).
            hit_count / fallback_count: rows with a REAL matched plan
                (``plan_id`` non-null AND != 0) vs. not (``plan_id`` null,
                a genuine planner-error miss, OR ``plan_id == 0``, the
                synthetic ad-hoc ``Match`` sentinel every SUCCESSFUL
                inline-planner run carries — see
                ``core.py::_nx_answer_plan_miss``'s ``Match(plan_id=0,
                name="ad-hoc", ...)``; ``plans.id`` is BIGSERIAL so 0 can
                never be a real plan row) — the "plan-match hit rate versus
                inline-planner fallback" figure the shakedown playbook's
                §4.5 telemetry baseline snapshot wants. (Review fix,
                nexus-eho3u: an earlier version of this method treated
                ``plan_id IS NOT NULL`` alone as the hit predicate, which
                counted every successful ad-hoc run as a hit and inverted
                this metric.)
            avg_duration_ms / avg_cost_usd: averages over the filtered set
                (``None`` when there are no rows).
            latency_buckets: fixed-edge histogram matching the production
                distribution nx_answer's own docstring cites (under 5s,
                5s-30s, 30s-2min, 2min-5min, over 5min) — "SAME QUERIES,
                SAME BUCKETS, EVERY TIME" per the shakedown playbook, so the
                edges live here and in ``TelemetryRepository`` only, never
                re-derived per caller.
            steps_supported: RDR-196 .p1e (nexus-nyry9.11) — present ONLY
                when ``include_steps=True`` was requested. Reuses the SAME
                capability probe (:meth:`_supports_nx_answer_steps`) the
                write side already gates on, so a caller can distinguish
                "the engine ignored ``include_steps`` because it predates
                the read route" from "the engine supports it and these
                particular rows genuinely carry no steps". An older engine
                200s on an unrecognized query param and every row simply
                omits ``steps`` — without this signal that reads
                identically to "nothing was ever recorded", which is
                exactly the kind of silent-zero this arc exists to end.

        There is no session_id filter: the table has no session_id column
        (checked against the live schema before adding one — a session
        filter here would be a speculative field this table cannot back).
        """
        params: dict[str, Any] = {"limit": limit}
        if since:
            # nexus-spbay: normalize BEFORE the wire — see normalize_since_filter.
            params["since"] = normalize_since_filter(since)
        if include_steps:
            params["include_steps"] = "true"
        resp = self._get("/v1/telemetry/nx_answer_runs/query", params=params)
        if not isinstance(resp, dict):  # defensive: a stripped proxy response
            out: dict[str, Any] = {
                "rows": [], "total": 0, "oldest_created_at": "",
                "hit_count": 0, "fallback_count": 0,
                "avg_duration_ms": None, "avg_cost_usd": None,
                "latency_buckets": {
                    "under_5s": 0, "5s_to_30s": 0, "30s_to_2min": 0,
                    "2min_to_5min": 0, "over_5min": 0,
                },
            }
            if include_steps:
                out["steps_supported"] = self._supports_nx_answer_steps()
            return out
        out = {
            "rows": list(resp.get("rows") or []),
            "total": int(resp.get("total") or 0),
            "oldest_created_at": str(resp.get("oldest_created_at") or ""),
            "hit_count": int(resp.get("hit_count") or 0),
            "fallback_count": int(resp.get("fallback_count") or 0),
            "avg_duration_ms": resp.get("avg_duration_ms"),
            "avg_cost_usd": resp.get("avg_cost_usd"),
            "latency_buckets": dict(resp.get("latency_buckets") or {}),
        }
        if include_steps:
            out["steps_supported"] = self._supports_nx_answer_steps()
        return out

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

    @staticmethod
    def _batch_ack(resp: Any, sent: int) -> int:
        """The service's ``inserted`` ack, or 0 with a warning when absent.

        nexus-znwc2: the old ``resp.get("inserted", len(rows))`` fabricated a
        full-batch durable count on a stripped/stubbed response. Telemetry is
        advisory, so a missing ack degrades to a VISIBLE undercount (0 +
        warning) rather than aborting the caller.
        """
        acked = resp.get("inserted") if isinstance(resp, dict) else None
        if acked is None:
            _log.warning(
                "telemetry_batch_ack_missing", sent=sent,
                consequence="counting 0 inserted (never assuming the batch landed)",
            )
            return 0
        return int(acked)

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
        return self._batch_ack(resp, len(rows))

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
        return self._batch_ack(resp, len(rows))

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

    def trim_search_telemetry(self, days: int = 30, *, dry_run: bool = False) -> int:
        """Delete (or, with ``dry_run=True``, COUNT without deleting)
        ``search_telemetry`` rows older than *days* days.

        Calls ``POST /v1/telemetry/search/trim`` with
        ``{"days": days, "dry_run": dry_run}``.

        DESIGN NOTE (the search_telemetry trim-preview gap): ``dry_run=True``
        does NOT hit a separate count endpoint. The engine
        (``TelemetryRepository.trimSearchTelemetry``) computes the preview
        from the EXACT SAME ``ts < cutoff`` predicate the real delete uses —
        a ``SELECT count(*)`` substituted for the ``DELETE``. This is
        deliberate: a census computed by a *different* predicate than the
        action it authorises can drift from that action (the nexus-3rr3x
        class — ``purge-trash``'s dry-run once reported 340 against a live
        census of 11,156 because the two were computed by different
        queries). Sharing the predicate here makes that drift impossible by
        construction, not merely unlikely, so a caller can trust that the
        number this returns with ``dry_run=True`` is exactly what a
        subsequent ``dry_run=False`` call removes (barring rows that cross
        the cutoff in the interim).

        ``dry_run`` defaults to ``False`` — every pre-existing caller keeps
        its real-delete behavior unchanged.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        resp = self._post(
            "/v1/telemetry/search/trim", {"days": days, "dry_run": dry_run}
        )
        return int(resp.get("deleted", 0))

    def list_hook_failures(
        self,
        *,
        days: int = 0,
        hook_names: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read hook failures, newest first (nexus-onjvy).

        SERVICE-MODE ONLY — there is no SQLite twin, deliberately. The local
        store's readers were raw SELECTs in ``nx taxonomy status`` and
        ``nx doctor``, and they die with the SQLite stores in nexus-i711w; this
        is their replacement, not a port of them. Registered in
        ``T2_SUPPLEMENTAL_CONTRACT`` for exactly that reason: the
        SQLite-derived parity contract cannot see a method with no twin, so
        without the supplemental entry it would be silently deletable.

        Until this existed, ``hook_failures`` was WRITE-ONLY over HTTP —
        ``/record`` and ``/trim`` and nothing else — so the failure log that
        exists to surface SILENT hook failures could be written and never
        inspected.

        Returns ``{"rows": [...], "total": int, "oldest_occurred_at": str}``.
        ``total`` and ``oldest_occurred_at`` are computed server-side over the
        WHOLE filtered set, NOT the returned page: ``nx doctor`` reports a
        count, and deriving it from a limited page would under-report the
        moment failures exceeded ``limit``.

        Args:
            days: only rows within the last N days; ``0`` means no time bound.
            hook_names: restrict to these hook names; ``None``/empty means all.
            limit: max rows in the returned page. Does not affect the
                aggregates.
        """
        params: dict[str, Any] = {"days": days, "limit": limit}
        if hook_names:
            params["hook_name"] = ",".join(hook_names)
        resp = self._get("/v1/telemetry/hook_failures/list", params)
        if not isinstance(resp, dict):  # defensive: a stripped proxy response
            return {"rows": [], "total": 0, "oldest_occurred_at": ""}
        return {
            "rows": list(resp.get("rows") or []),
            "total": int(resp.get("total") or 0),
            "oldest_occurred_at": str(resp.get("oldest_occurred_at") or ""),
        }

    def trim_hook_failures(self, days: int = 30, *, dry_run: bool = False) -> int:
        """Delete (or, with ``dry_run=True``, COUNT without deleting)
        ``hook_failures`` rows older than *days* days (nexus-7365x).

        Calls ``POST /v1/telemetry/hook_failures/trim``. Same dry-run-reuses-
        the-delete's-own-predicate contract as :meth:`trim_search_telemetry`
        — added alongside it so a caller trimming both tables under one
        ``--dry-run`` cannot preview one while silently mutating the other.
        ``dry_run`` defaults to ``False``.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        resp = self._post(
            "/v1/telemetry/hook_failures/trim", {"days": days, "dry_run": dry_run}
        )
        return int(resp.get("deleted", 0))

    # ── index_failures (nexus-nukn3) ──────────────────────────────────────────
    #
    # Durable per-file failure record for the repo-index path (Sam's design:
    # "when a file fails, ENQUEUE the failure and move on"). Event-log shape,
    # like hook_failures — not aspect_extraction_queue's work-queue shape:
    # nothing ever claims or retries a row here (no retry worker in scope,
    # nexus-nukn3's explicit scope fence).

    def record_index_failure(
        self,
        *,
        run_id: str,
        file_path: str,
        error_class: str = "",
        error: str = "",
        occurred_at: str | None = None,
    ) -> None:
        """Record one durable index failure. Calls
        ``POST /v1/telemetry/index_failures/record``.
        """
        payload: dict[str, Any] = {
            "run_id":      run_id,
            "file_path":   file_path,
            "error_class": error_class,
            "error":       error,
        }
        if occurred_at is not None:
            payload["occurred_at"] = occurred_at
        self._post("/v1/telemetry/index_failures/record", payload)

    def record_index_failures_batch(
        self,
        rows: list[tuple[str, str, str, str]],
        *,
        run_id: str,
    ) -> int:
        """Record N index failures from one run in ONE transaction.

        *rows* is ``(file_path, error_class, error, occurred_at)`` tuples —
        ``occurred_at`` may be ``""`` to let the server stamp ``now()``.
        Mirrors :meth:`log_search_batch`'s row-tuple shape. Returns the
        service's ``inserted`` ack (0, with a warning, on a stripped/absent
        response — see :meth:`_batch_ack` — never a fabricated full count).
        """
        if not rows:
            return 0
        payload: dict[str, Any] = {
            "rows": [[run_id, file_path, error_class, error, occurred_at or None]
                     for file_path, error_class, error, occurred_at in rows]
        }
        resp = self._post("/v1/telemetry/index_failures/record_batch", payload)
        return self._batch_ack(resp, len(rows))

    def list_index_failures(
        self,
        *,
        run_id: str = "",
        days: int = 0,
        limit: int = 100,
        unacknowledged_only: bool = False,
        file_path: str = "",
    ) -> dict[str, Any]:
        """Read index failures, newest first, optionally scoped to one
        ``run_id`` (blank = every run).

        Calls ``GET /v1/telemetry/index_failures/list``.

        Returns ``{"rows": [...], "total": int, "oldest_occurred_at": str}``.
        ``total`` is computed server-side over the WHOLE filtered set, not
        the returned page — same non-vacuity shape as
        :meth:`list_hook_failures`: a caller reading a count (``nx doctor
        --check-index-failures``) must never under-report because the
        backlog exceeded ``limit``.

        Each row carries an ``acknowledged`` boolean (nexus-nukn3 fold-in,
        critic Critical finding: the doctor gate needs a durable
        adjudication that survives a fresh ``run_id`` every re-run --
        ``nx index failures --acknowledge``). ``unacknowledged_only=True``
        additionally excludes any covered row from both ``rows`` and
        ``total`` -- the DOCTOR GATE's input only. Deliberately NOT the
        deyd5 systemic-skip floor's input (code-review finding, T2
        code-review-nexus-nukn3-4d5520bf4 [24624]): ``nexus.indexer.
        _run_index`` calls this method for that floor WITHOUT
        ``unacknowledged_only`` — correctly, since acknowledging a failure
        records an operator's adjudication, not that the file actually
        indexed; the skip-ratio math must count it regardless.

        ``file_path`` (third fold-in, code-review finding [24624]) narrows
        to an exact file server-side -- the fix for ``nx index failures
        --acknowledge --file``'s error_class auto-resolve, which used to
        page 1000 rows tenant-wide and filter client-side.
        """
        params: dict[str, Any] = {"days": days, "limit": limit}
        if run_id:
            params["run_id"] = run_id
        if unacknowledged_only:
            params["unacknowledged_only"] = True
        if file_path:
            params["file_path"] = file_path
        resp = self._get("/v1/telemetry/index_failures/list", params)
        if not isinstance(resp, dict):  # defensive: a stripped proxy response
            return {"rows": [], "total": 0, "oldest_occurred_at": ""}
        return {
            "rows": list(resp.get("rows") or []),
            "total": int(resp.get("total") or 0),
            "oldest_occurred_at": str(resp.get("oldest_occurred_at") or ""),
        }

    def list_index_failure_acknowledgments(self) -> dict[str, Any]:
        """List every durable acknowledgment for the tenant, newest first
        (nexus-nukn3 third fold-in, critic Critical finding T2
        critique-nexus-nukn3-4d5520bf4 [24621]: the ack mechanism was
        write-only -- created via :meth:`acknowledge_index_failure` but
        never listable or revocable).

        Calls ``GET /v1/telemetry/index_failures/acks``.

        Returns ``{"rows": [{"id", "file_path", "error_class", "reason",
        "created_at"}, ...], "total": int}``. ``file_path`` is ``""`` for
        an error-class-scoped (corpus-wide) acknowledgment.
        """
        resp = self._get("/v1/telemetry/index_failures/acks", {})
        if not isinstance(resp, dict):  # defensive: a stripped proxy response
            return {"rows": [], "total": 0}
        return {
            "rows": list(resp.get("rows") or []),
            "total": int(resp.get("total") or 0),
        }

    def unacknowledge_index_failure(
        self, *, error_class: str, file_path: str = "",
    ) -> int:
        """Revoke a durable acknowledgment (nexus-nukn3 third fold-in,
        critic Critical finding [24621]: an ack that could be created but
        never undone).

        Calls ``POST /v1/telemetry/index_failures/unacknowledge``. Deletes
        ONLY the row matching the EXACT scope it was created under --
        ``error_class`` REQUIRED non-blank (mirrors
        :meth:`acknowledge_index_failure`'s own guard: revoking "every
        acknowledgment for a file regardless of class" is never the
        intended scope); ``file_path`` blank targets the error-class-scoped
        acknowledgment, mirroring how it was created.

        Returns the number of rows deleted (0 if no matching acknowledgment
        exists).
        """
        if not error_class:
            raise ValueError(
                "unacknowledge_index_failure requires a non-blank error_class"
            )
        payload: dict[str, Any] = {"error_class": error_class}
        if file_path:
            payload["file_path"] = file_path
        resp = self._post("/v1/telemetry/index_failures/unacknowledge", payload)
        return int(resp.get("deleted", 0))

    def acknowledge_index_failure(
        self,
        *,
        error_class: str,
        file_path: str = "",
        reason: str = "",
    ) -> None:
        """Durably acknowledge a recurring index failure (nexus-nukn3
        fold-in, critic Critical finding: a fresh ``run_id`` every run
        means ``--clear`` alone is undone by the very next index run for a
        PERMANENTLY unextractable file re-indexed on a cadence). Writes a
        ``kind='acknowledgment'`` row into the same ``nexus.index_failures``
        table -- an operator's adjudication that survives re-runs, unlike a
        one-time ``--clear``.

        Calls ``POST /v1/telemetry/index_failures/acknowledge``.

        Args:
            error_class: REQUIRED, non-blank. An acknowledgment with no
                error_class would cover every failure for its file (or,
                blank ``file_path`` too, every failure in the tenant),
                which is never the intended scope.
            file_path: file-scoped when non-blank (only that exact
                ``(file_path, error_class)`` pair is covered going
                forward); ``""`` (default) for an error-class-scoped
                acknowledgment covering ANY file with ``error_class``.
            reason: optional free-text note (shown in ``nx index
                failures``'s error column for the acknowledgment).
        """
        if not error_class:
            raise ValueError(
                "acknowledge_index_failure requires a non-blank error_class"
            )
        payload: dict[str, Any] = {"error_class": error_class}
        if file_path:
            payload["file_path"] = file_path
        if reason:
            payload["reason"] = reason
        self._post("/v1/telemetry/index_failures/acknowledge", payload)

    def trim_index_failures(
        self, *, run_id: str = "", days: int = 0, dry_run: bool = False,
    ) -> int:
        """Delete (or, with ``dry_run=True``, COUNT without deleting)
        index_failures rows by ``run_id`` and/or age (nexus-nukn3 fold-in,
        the critic's Critical finding on the original cut: an all-time,
        fail-first doctor check with no remedy is unfixable forever once a
        permanent extraction failure exists. This is the remedy -- ``nx
        index failures --clear``.

        Calls ``POST /v1/telemetry/index_failures/trim``. At least one of
        ``run_id``/``days`` is required -- refusing here (before the wire
        call) mirrors the engine's own 400 boundary, so a caller gets the
        same clear refusal message regardless of which side would have
        caught it first. Never deletes a ``kind='acknowledgment'`` row --
        only failures age out; an acknowledgment is a durable policy
        marker.
        """
        if not run_id and days < 1:
            raise ValueError(
                "trim_index_failures requires run_id and/or days >= 1 "
                "(refusing an unscoped delete of the entire tenant history)"
            )
        payload: dict[str, Any] = {}
        if run_id:
            payload["run_id"] = run_id
        if days > 0:
            payload["days"] = days
        if dry_run:
            payload["dry_run"] = True
        resp = self._post("/v1/telemetry/index_failures/trim", payload)
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
        ttl_days: int | None,
        frecency_score: float,
        miss_count: int,
        last_hit_at: str | None,
    ) -> None:
        """Fidelity-preserving import of one frecency row.

        Uses ``POST /v1/telemetry/import`` with ``table=frecency``.
        GREATEST for score/count/last_hit_at; LEAST for embedded_at.

        ``ttl_days`` type widened to ``int | None`` (code-review cosmetic
        finding, nexus-tk070.p6b fix-pass, 2026-08-20): stale relative to
        the None-sentinel convention (RDR-194 D5) — this is dead ETL-only
        code (sole caller already deleted, retirement overdue since
        7.0.0), so this is a type-hint-only correction, not a behavior
        change; left for whoever eventually deletes the method.
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
            resp = self._post(
                "/v1/telemetry/ids/probe", {"table": table, "keys": batch}, mutates=False
            )
            present.extend(resp.get("present") or [])
        return present

    # ── capability_census (nexus-gjv9b PART 1) ──────────────────────────────

    def record_capability_census(
        self,
        *,
        session_id: str,
        ts: str,
        blindspot: bool,
        unmeasurable_reason: str | None = None,
        capabilities: dict[str, int] | None = None,
        dispatches: int | None = None,
        total_calls: int | None = None,
        timeout: float = 2.0,
    ) -> None:
        """Upsert one session's capability census. Calls
        ``POST /v1/telemetry/capability_census/record``.

        Single-attempt with a hard *timeout* (default 2.0s, matching
        ``_print_service_tier_summary``'s own precedent): this method is
        called from the SessionEnd grandchild path
        (``_session_end_census.write_session_capability_census``), which
        has no retry budget to spend, so ``idempotent=False`` issues the
        request EXACTLY ONCE per credential — no gateway 502/503/504
        backoff loop — with the sole carve-out :meth:`_send` documents (a
        definitive 401 re-mints and retries once; a genuinely dead
        credential cannot silently wedge this path forever). ANY failure
        is the caller's (``_post_capability_census``'s) cue to degrade to
        a metered drop, never to retry itself on top of this.

        Routed through :meth:`_post` (nexus-a2qhz / nexus-onq1a review
        fix pass — a prior version bypassed the mixin's ``_client``
        entirely via a raw ``httpx`` call, which ALSO bypassed the
        production-write guard silently): this fires on every SessionEnd,
        so a dev checkout running as the operator's live ``nx`` must
        refuse it the same way every other T2 write does. ``mutates=True``
        (the default) means :meth:`_send` calls
        :func:`~nexus.db.service_endpoint.guard_production_write` BEFORE
        the first network attempt; an unopted-in dev checkout gets
        :class:`~nexus.db.service_endpoint.ProductionWriteGuardError`,
        which the caller counts as a metered drop, never silently loses.
        """
        payload: dict[str, Any] = {
            "session_id": session_id,
            "ts":         ts,
            "blindspot":  blindspot,
        }
        if blindspot:
            payload["unmeasurable_reason"] = unmeasurable_reason or ""
        else:
            payload["capabilities"] = capabilities or {}
            payload["dispatches"] = dispatches
            payload["total_calls"] = total_calls
        self._post(
            "/v1/telemetry/capability_census/record",
            payload,
            idempotent=False,
            timeout=timeout,
        )

    def query_capability_census(
        self,
        *,
        session_id: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read capability_census rows, newest first — the read half of
        ``nx census capability`` (nexus-gjv9b PART 1, S11 doctrine). Calls
        ``GET /v1/telemetry/capability_census/query``.
        """
        params: dict[str, Any] = {"limit": limit}
        if session_id:
            params["session_id"] = session_id
        elif since:
            params["since"] = normalize_since_filter(since)
        resp = self._get("/v1/telemetry/capability_census/query", params=params)
        if not isinstance(resp, dict):  # defensive: a stripped proxy response
            return []
        return list(resp.get("rows") or [])

    def trim_capability_census(self, days: int = 30, *, dry_run: bool = False) -> int:
        """Delete (or, with ``dry_run=True``, COUNT without deleting)
        ``capability_census`` rows older than *days* days (nexus-gjv9b
        review fold-in, critique Significant 4).

        Calls ``POST /v1/telemetry/capability_census/trim``. Same dry-run-
        reuses-the-delete's-own-predicate contract as
        :meth:`trim_hook_failures` — the preview and the real delete share
        one server-side ``ts < cutoff`` predicate, so the count returned
        with ``dry_run=True`` is exactly what a subsequent ``dry_run=False``
        call removes (barring rows that cross the cutoff in the interim).
        ``dry_run`` defaults to ``False``.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        resp = self._post(
            "/v1/telemetry/capability_census/trim", {"days": days, "dry_run": dry_run}
        )
        return int(resp.get("deleted", 0))

    # ── routing_events (nexus-gjv9b PART 2) ─────────────────────────────────

    def record_routing_event(
        self,
        *,
        rule: str,
        outcome: str,
        ts: str = "",
        session_id: str = "",
        tool_name: str = "",
        command_fragment: str = "",
        escape_reason: str = "",
        timeout: float = 0.25,
    ) -> None:
        """Append one routing-hook event. Calls
        ``POST /v1/telemetry/routing_events/record``.

        NOTE: this method exists for in-process callers that already hold
        an :class:`HttpTelemetryStore` (e.g. ``nx hook routing-stats``
        replaying events, or a future non-hook writer). The routing hooks
        THEMSELVES (``conexus/hooks/scripts/routing/_lib.py``) are
        standalone scripts with no ``nexus`` import (RDR-121 § Contract)
        and POST to the same route directly via ``urllib`` — they cannot
        call this method, so the endpoint-discovery/guard fix here does
        not reach them (see that module's own ``_engine_endpoint``).

        Single-attempt with a hard *timeout* (default 0.25s — the
        routing-hook latency budget): ``idempotent=False`` issues the
        request EXACTLY ONCE per credential (the sole carve-out is
        :meth:`_send`'s documented 401 re-mint-and-retry). ANY failure is
        the caller's cue to drop, never to retry itself on top of this —
        same discipline as :meth:`record_capability_census`.

        Routed through :meth:`_post` (nexus-a2qhz / nexus-onq1a review
        fix pass — a prior version bypassed the mixin's ``_client`` and
        its production-write guard via a raw ``httpx`` call); ``mutates=
        True`` (the default) means an unopted-in dev checkout is refused
        via :func:`~nexus.db.service_endpoint.guard_production_write`
        before any network attempt, same as every other T2 write.
        """
        payload: dict[str, Any] = {
            "rule":             rule,
            "outcome":          outcome,
            "ts":               ts,
            "session_id":       session_id,
            "tool_name":        tool_name,
            "command_fragment": command_fragment,
            "escape_reason":    escape_reason,
        }
        self._post(
            "/v1/telemetry/routing_events/record",
            payload,
            idempotent=False,
            timeout=timeout,
        )

    def list_routing_events(
        self,
        *,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read routing_events rows, newest first — the read half of
        :mod:`nexus.routing_stats` (nexus-gjv9b PART 2, S11 doctrine). Calls
        ``GET /v1/telemetry/routing_events/list``.
        """
        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = normalize_since_filter(since)
        resp = self._get("/v1/telemetry/routing_events/list", params=params)
        if not isinstance(resp, dict):  # defensive: a stripped proxy response
            return []
        return list(resp.get("rows") or [])

    def trim_routing_events(self, days: int = 30, *, dry_run: bool = False) -> int:
        """Delete (or, with ``dry_run=True``, COUNT without deleting)
        ``routing_events`` rows older than *days* days (nexus-gjv9b review
        fold-in, critique Significant 4).

        Calls ``POST /v1/telemetry/routing_events/trim``. Same dry-run-
        reuses-the-delete's-own-predicate contract as
        :meth:`trim_hook_failures`/:meth:`trim_capability_census`.
        ``dry_run`` defaults to ``False``.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        resp = self._post(
            "/v1/telemetry/routing_events/trim", {"days": days, "dry_run": dry_run}
        )
        return int(resp.get("deleted", 0))


def tier_writes_read_failure_message(exc: Exception) -> str:
    """One shared, honest diagnosis for a failed tier_writes/query read.

    Used by BOTH ``nx tier-status`` and doctor's tier-discipline check so
    the wording cannot drift (critique). Distinguishes the three cases
    (critique Significant-3 — a bare except collapsed them):

    - HTTP 404 → the engine predates the nexus-59wjj route (version skew,
      deploy a newer engine);
    - any other HTTP status → the route EXISTS and failed — a live engine
      returned an error; investigate before writing it off as version skew;
    - no HTTP status at all → the service is unreachable.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    prefix = (
        "tier_writes are recorded in the service-backed telemetry store "
        "(Postgres); "
    )
    if status == 404:
        return prefix + (
            "this engine predates the tier_writes/query route — "
            "deploy an engine carrying nexus-59wjj to see counts."
        )
    if status is not None:
        return prefix + (
            f"the tier_writes/query route returned HTTP {status} — the route "
            "exists but failed; investigate the engine before assuming "
            "version skew."
        )
    return prefix + (
        f"the service read failed ({type(exc).__name__}) — "
        "service unreachable."
    )


def relevance_stats_read_failure_message(exc: Exception) -> str:
    """One shared, honest diagnosis for a failed relevance/stats read.

    Same three-way split as :func:`tier_writes_read_failure_message` and
    :func:`nx_answer_runs_read_failure_message` (nexus-v0x32): a 404 means
    this engine predates the ``relevance/stats`` route (version skew, never
    reported as a fabricated 0), any other HTTP status means the route
    exists and failed on a live engine, and no status at all means the
    service is unreachable. Used by ``nx telemetry baseline`` so a caller
    cannot mistake "no route" for "no rows".
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    prefix = (
        "relevance_log is recorded in the service-backed telemetry store "
        "(Postgres); "
    )
    if status == 404:
        return prefix + (
            "this engine predates the relevance/stats route — "
            "deploy an engine carrying nexus-v0x32 to see counts."
        )
    if status is not None:
        return prefix + (
            f"the relevance/stats route returned HTTP {status} — the "
            "route exists but failed; investigate the engine before "
            "assuming version skew."
        )
    return prefix + (
        f"the service read failed ({type(exc).__name__}) — "
        "service unreachable."
    )


def nx_answer_runs_read_failure_message(exc: Exception) -> str:
    """One shared, honest diagnosis for a failed nx_answer_runs/query read.

    Same three-way split as :func:`tier_writes_read_failure_message`
    (nexus-eho3u — the read path is new, the honesty contract it must meet
    is not): a 404 means this engine predates the route (version skew, never
    reported as a silent zero), any other HTTP status means the route exists
    and failed on a live engine, and no status at all means the service is
    unreachable. Used by ``nx answer-runs`` so a caller cannot mistake
    "no route" for "no runs".
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    prefix = (
        "nx_answer_runs are recorded in the service-backed telemetry store "
        "(Postgres); "
    )
    if status == 404:
        return prefix + (
            "this engine predates the nx_answer_runs/query route — "
            "deploy an engine carrying nexus-eho3u to see counts."
        )
    if status is not None:
        return prefix + (
            f"the nx_answer_runs/query route returned HTTP {status} — the "
            "route exists but failed; investigate the engine before "
            "assuming version skew."
        )
    return prefix + (
        f"the service read failed ({type(exc).__name__}) — "
        "service unreachable."
    )
