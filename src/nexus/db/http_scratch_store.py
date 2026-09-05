# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpScratchStore — thin HTTP client for the RDR-152 Java T1 scratch service.

Drop-in replacement for :class:`~nexus.db.t1.T1Database`.

Activated when ``NX_STORAGE_BACKEND_T1=service`` (or global
``NX_STORAGE_BACKEND=service``).  Requires ``NX_T1_SESSION`` to be set; that
env var replaces the old ``NX_T1_HOST`` / ``NX_T1_PORT`` Chroma discovery
mechanism (RDR-152 bead nexus-gmiaf.13).

Config
------
NX_SERVICE_HOST  — service host (default: 127.0.0.1)
NX_SERVICE_PORT  — service port (required when using service backend)
NX_SERVICE_TOKEN — bearer token (required)
NX_T1_SESSION    — session identifier (required; replaces NX_T1_HOST/PORT)
NX_NEXUS_TENANT  — tenant to stamp on every request (default: "default")

SEARCH BEHAVIOR CHANGE
----------------------
``T1Database.search()`` was **semantic** (ChromaDB ONNX cosine similarity).
``HttpScratchStore.search()`` is **FTS** (Postgres tsvector, OR-query:
``plainto_tsquery('english', q)`` for prose stemming and
``plainto_tsquery('simple', q)`` for exact identifier/tag matching).

This is an intentional upgrade (see 152-FTS-tokenizer-DECISION in T2 project
memory ``rdr``).  Short exact-identifier queries still work via the ``simple``
branch.  Ranking is ts_rank (BM25-like), not cosine; results are still
ordered best-first.

``promote()`` is implemented: it fetches the T1 entry from the service, runs
the same Jaccard overlap detection used by the Chroma path (via the shared
:func:`~nexus.db.t1._find_promote_overlap_candidates` helper), writes to T2,
and returns a :class:`~nexus.types.PromotionReport`.
"""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

import httpx
import structlog

if TYPE_CHECKING:  # nexus-b8a5a: return-shape parity — the annotation was
    # `object` while this method's own docstring said PromotionReport.
    from nexus.types import PromotionReport

_log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"

#: Env var carrying the per-session TOKEN sent in the X-Nexus-T1-Session header
#: (RDR-152 bead nexus-gmiaf.32.4). In the transitional bootstrap (no minted token)
#: this is the bare session id, used as both the header value and the body session_id.
_SESSION_ENV: str = "NX_T1_SESSION"

#: Env var carrying the session IDENTIFIER (body session_id + flush-title). Distinct from
#: the token once minting is active; falls back to NX_T1_SESSION when unset (bootstrap).
_SESSION_ID_ENV: str = "NX_T1_SESSION_ID"

#: Header carrying the per-session token; the service hashes it to resolve (tenant, session).
_HEADER_T1_SESSION: str = "X-Nexus-T1-Session"

#: Sentinel prefix raised for HTTP 401 (the require-minted-session gate,
#: nexus-h8rf6 T1-401 finding). ``nexus.commands.scratch._clean_service_errors``
#: keys its actionable minted-token guidance on THIS marker — never on ad-hoc
#: substring matching (wave review: a wording change would silently degrade the
#: guidance to the generic branch). Change the marker, the raise sites below,
#: and the detection together; tests/test_scratch_cmd_service_errors.py pins
#: the coupling end-to-end.
SESSION_UNAUTHORIZED_MARKER: str = "HttpScratchStore: unauthorized T1 session (HTTP 401)"

#: Appended to SESSION_UNAUTHORIZED_MARKER when the post-401 self-heal was
#: attempted but found nothing fresher to adopt (no lease, expired, or
#: unchanged) -- the ADOPTED/DECLINED distinction the owner-death guidance in
#: ``mcp.core._mcp_tool_error`` branches on (nexus-fe96p: that guidance used
#: to assert owner death unconditionally, which was measured false when the
#: heal actually succeeded and the retry failed for an unrelated reason).
HEAL_DECLINED_SUFFIX: str = "(heal: declined -- no fresher token found)"

#: Appended to SESSION_UNAUTHORIZED_MARKER when the g5hzk SESSION-LEASE heal
#: (specifically -- the wrwb7 re-mint case carries HEAL_REMINT_SUFFIX) DID
#: adopt a fresh token and retried, but the retry itself still came back
#: unauthorized -- a materially different failure than HEAL_DECLINED_SUFFIX:
#: the token was live at the top of the retry, so the owner/refresh-loop are
#: not implicated (nexus-fe96p).
HEAL_ADOPTED_SUFFIX: str = "(heal: adopted a fresh token, retry still unauthorized)"

#: Appended when the g5hzk lease heal DECLINED but the nexus-wrwb7 data-token
#: re-mint healed the BEARER -- and the retry still came back unauthorized.
#: Distinct from HEAL_ADOPTED_SUFFIX so the guidance names the mechanism that
#: actually fired: both reviewers flagged that a shared ``healed`` bool let
#: the message credit g5hzk for a re-mint heal (the inference-as-fact class
#: this bead exists to fix, one level down).
HEAL_REMINT_SUFFIX: str = "(heal: re-minted the data-token bearer, retry still unauthorized)"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-bgh2j: construction-time resolution gets the SAME evidence-gated
# bounded wait as the nine mixin adopters (call sites unchanged — only
# the alias target moved to the gated resolver).
from nexus.db.service_endpoint import (
    resolve_service_endpoint_with_evidence_gate as _resolve_endpoint,
)


# ── HttpScratchStore ───────────────────────────────────────────────────────────


class HttpScratchStore:
    """T1Database drop-in that delegates to the RDR-152 Java HTTP service.

    SEARCH BEHAVIOR CHANGE: ``search()`` uses FTS (Postgres tsvector) rather
    than vector/cosine (ChromaDB ONNX).  See module docstring for details.

    Uses a keep-alive :class:`httpx.Client` connection pool.

    Args:
        base_url:   Optional URL override (``http://<host>:<port>``).
                    When supplied, host/port env-vars are ignored; token
                    env-var is still required.
        tenant:     Tenant to stamp on every request (default: ``DEFAULT_TENANT``).
        session_id: Optional session identifier override.  When ``None``,
                    resolved from ``NX_T1_SESSION`` env var.
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        session_id: str | None = None,
        _token: str | None = None,
        _session_token: str | None = None,
    ) -> None:
        # nexus-wrwb7: whether the CALLER explicitly pinned the bearer (a
        # test fixture pointing at a fake server) -- tracked BEFORE the
        # env/lease-resolution fallback below overwrites the local, same
        # contract as RefreshableHttpStoreMixin's _token_pinned. A pinned
        # token is never silently swapped for a self-minted data token.
        self._token_pinned = _token is not None

        if base_url is not None:
            if _token is None:
                _token = os.environ.get("NX_SERVICE_TOKEN", "")
                if not _token:
                    raise RuntimeError(
                        "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_T1=service."
                    )
            self._base_url = base_url.rstrip("/")
        else:
            self._base_url, token = _resolve_endpoint()
            _token = token

        self._tenant = tenant
        # session_id (body + flush-title) and session_token (header) are distinct once
        # minting is active (Phase D). Back-compat: with only NX_T1_SESSION set, both
        # collapse to the bare session id (the pre-minting bootstrap posture).
        env_id = os.environ.get(_SESSION_ID_ENV, "").strip()
        env_token = os.environ.get(_SESSION_ENV, "").strip()
        self._session_id: str = session_id or env_id or env_token
        if not self._session_id:
            raise RuntimeError(
                f"{_SESSION_ENV} (or {_SESSION_ID_ENV}) is required when "
                "NX_STORAGE_BACKEND_T1=service. Set it to the session shared across siblings."
            )
        # The minted session token (resolves to a live session_tokens row) is sent
        # as the X-Nexus-T1-Session header; the AuthFilter require-minted gate 401s a
        # header that does not resolve. Production mints via the MCP lifespan and
        # exports NX_T1_SESSION; a direct caller (or a test) injects via _session_token.
        # Falls back to the bare session id only in the pre-minting bootstrap posture.
        self._session_token: str = _session_token or env_token or self._session_id

        self._headers = {
            "Authorization": f"Bearer {_token}",
            "X-Nexus-Tenant": tenant,
            _HEADER_T1_SESSION: self._session_token,
            "Content-Type": "application/json",
        }
        # nexus-wrwb7 (RDR-005 2a self-minting): substitute a self-minted
        # data token for the Authorization header above when a mint_token
        # credential is configured. No-op (byte-identical to pre-existing
        # behavior) when unconfigured or the bearer was explicitly pinned.
        self._apply_data_token_override()
        self._client = self._build_client()
        # nexus-g5hzk review H1: guards the 401 self-heal's read-check-
        # mutate-rebuild sequence. Latent under the current single-event-loop
        # MCP dispatch (sync tool bodies never interleave), but the RDR-105
        # contract invites concurrent subagent fan-out through one store, and
        # the sibling T2 singleton already carries the same guard
        # (_service_t2_lock) for exactly this close()-under-a-live-caller
        # class.
        import threading  # noqa: PLC0415 — stdlib, deferred; ctor-only

        self._refresh_lock = threading.Lock()
        _log.info(
            "http_scratch_store.init",
            base_url=self._base_url,
            tenant=tenant,
            session_id=self._session_id,
        )

    # ── Session ────────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """The session identifier used to scope all scratch entries."""
        return self._session_id

    def close(self) -> None:
        """Close the keep-alive connection pool (idempotent)."""
        self._client.close()
        _log.debug("http_scratch_store.closed")

    def _apply_data_token_override(self) -> None:
        """nexus-wrwb7: substitute a self-minted data token for
        ``self._headers["Authorization"]`` when a ``mint_token`` credential
        is configured. See ``RefreshableHttpStoreMixin._apply_data_token_override``
        (the T2 twin of this method) for the full rationale; this store does
        not ride that mixin (bespoke bearer-header caching), so the same
        treatment is applied directly here.

        Skipped when the bearer was explicitly pinned at construction. A
        mint failure (``DataTokenMintError``) propagates uncaught -- a
        half-provisioned install must surface, never silently keep the
        static/lease token as though nothing were configured.
        """
        # getattr default True (not False): an instance constructed via
        # __new__() bypassing __init__ (a real test pattern in this
        # codebase, e.g. tests/test_scratch_cmd_service_errors.py) has no
        # _token_pinned attribute at all -- treat that as "pinned" (do
        # nothing) rather than crash with AttributeError or, worse, silently
        # assume unpinned and attempt a mint against whatever partial state
        # such an instance happens to carry.
        if getattr(self, "_token_pinned", True):
            return
        from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred to avoid circular import

        token = get_data_token_manager().bearer_for(self._base_url, self._tenant)
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"

    def _current_authorization_header(self) -> str:
        """Return the CURRENT ``Authorization`` header value, resolved
        FRESH on every call (critic S4, nexus-ssqk9) rather than only the
        construction-time-cached ``self._headers["Authorization"]``.

        Mirrors ``http_vector_client._request_once``'s per-request
        ``bearer_for()`` call (T3) -- the design of record's <20%-TTL
        PROACTIVE refresh is otherwise vestigial for this store: without
        this, a live long-running ``HttpScratchStore`` only ever refreshes
        REACTIVELY, after a 401 (:meth:`_remint_data_token_and_rebuild`),
        which stays as a fallback below for the case a token is rejected
        between two calls to this method (an out-of-band revoke).

        Cheap when unconfigured or pinned: ``DataTokenManager.bearer_for``
        returns ``None`` immediately with zero network call in that case
        (or is never invoked at all here), so this adds no per-request cost
        for the ~every install that has not opted into self-minting.
        """
        # getattr defaults (not direct attribute access): an instance built
        # via HttpScratchStore.__new__(HttpScratchStore) bypassing __init__
        # (the exact pattern tests/test_scratch_cmd_service_errors.py uses,
        # setting ONLY self._client) has neither _token_pinned NOR _headers
        # -- default to pinned/no-op (True) and an empty header dict rather
        # than crash with AttributeError, mirroring
        # _apply_data_token_override's identical defensive contract.
        static_headers = getattr(self, "_headers", {})
        if getattr(self, "_token_pinned", True):
            return static_headers.get("Authorization", "")
        from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred to avoid circular import

        token = get_data_token_manager().bearer_for(self._base_url, self._tenant)
        if token is not None:
            return f"Bearer {token}"
        return static_headers.get("Authorization", "")

    def _remint_data_token_and_rebuild(self) -> bool:
        """On a 401 that the session-token refresh did not resolve, try
        invalidating + re-minting the self-minted data token (RDR-005 2a)
        and rebuild the client with the fresh header.

        Returns ``False`` (nothing to do, the original 401 stands) when no
        ``mint_token`` credential is configured or the bearer was pinned.
        A configured-but-failing mint raises ``DataTokenMintError`` --
        fail loud, never leave the caller with a misleading
        ``SESSION_UNAUTHORIZED_MARKER`` when the real problem is a bad or
        revoked mint credential.
        """
        # See _apply_data_token_override's matching getattr for why the
        # default is True (pinned/no-op), not False.
        if getattr(self, "_token_pinned", True):
            return False
        from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred to avoid circular import

        manager = get_data_token_manager()
        if not manager.is_configured():
            return False
        manager.invalidate(self._base_url, self._tenant)
        token = manager.bearer_for(self._base_url, self._tenant)
        if token is None:
            return False
        self._headers["Authorization"] = f"Bearer {token}"
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
            pass
        self._client = self._build_client()
        _log.warning("http_scratch_store.data_token_remint_on_401")
        return True

    def _rebind_from_lease(self) -> bool:
        """nexus-om64x: on connection-refused (supervisor restarted on a new
        port; our env port is stale), re-resolve the endpoint from the
        ServiceRegistry lease and rebuild the client. Returns True if rebound to
        a NEW endpoint, False otherwise (genuine outage — let the error stand).

        nexus-7dsgp (GH #1405 defect 1): passes
        ``DEFAULT_LEASE_WAIT_BUDGET_S`` so a retry landing in the
        supervisor-respawn gap (old lease expired, new lease not yet
        published) polls for up to 12s instead of giving up on the first
        miss — this is the ONLY caller of ``recover_endpoint_from_lease``
        on this store, so the bounded wait applies exactly once per
        ``_post``/``_post_raw`` call, never stacked.
        """
        from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred to avoid circular import
            DEFAULT_LEASE_WAIT_BUDGET_S,
            recover_endpoint_from_lease,
        )

        recovered = recover_endpoint_from_lease(
            self._base_url, wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S
        )
        if recovered is None:
            return False
        new_url, new_token = recovered
        _log.warning("http_scratch_store.rebind", old=self._base_url, new=new_url)
        self._base_url = new_url
        if new_token:
            self._headers["Authorization"] = f"Bearer {new_token}"
        # nexus-wrwb7: re-apply AFTER the static-token half above -- when a
        # mint_token credential is configured, the data token takes
        # precedence over whatever the lease republished, exactly as at
        # construction.
        self._apply_data_token_override()
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
            pass
        self._client = self._build_client()
        return True

    def _refresh_session_token_from_lease(self, sent_token: str) -> bool:
        """nexus-g5hzk: on a 401, re-read the session lease republished by the
        token OWNER and adopt its token if it differs from ours.

        The borrow path (``t1_session_inherited_no_mint``) caches the owner's
        token string once at startup; the owner's refresh loop re-mints on a
        half-TTL cadence and ``start_session`` is ON CONFLICT DO UPDATE — every
        re-mint ROTATES the server-side token, stranding every borrower on a
        string that no longer resolves (live incident 2026-07-14: a resumed
        session 401'd four minutes after borrowing and stayed dark all day
        while its elder incarnation kept rotating). The owner republishes the
        lease file with each rotation, so the fresh token is on disk — re-read
        and retry once. Never MINTS: the nexus-ngcpo single-minter ownership
        rule stands; when the owner is genuinely gone the lease ages out and
        this returns False, letting the 401 stand.

        *sent_token* is the token the failed request actually carried: under
        concurrent 401s (review H1) the losing thread finds the winner already
        healed the shared store — current token != sent — and retries on the
        healed client instead of spuriously raising.

        Returns True when a retry is worthwhile (a differing token was adopted,
        or another thread already adopted one); False on no lease / unreadable
        / expired / unchanged.
        """
        session_id = getattr(self, "_session_id", "")
        if not session_id:
            return False
        with self._refresh_lock:
            if self._session_token != sent_token:
                # Another thread healed the store while we waited on the
                # lock (or mid-request) — just retry with its work.
                return True
            try:
                from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
                from nexus.db.t1 import read_t1_session_lease  # noqa: PLC0415 — deferred to avoid circular import

                fresh = read_t1_session_lease(session_id, nexus_config_dir())
            except Exception as exc:  # noqa: BLE001 — best-effort self-heal; the original 401 stands
                # read_t1_session_lease handles "no lease" internally (None) —
                # reaching here means the self-heal ITSELF broke; leave a trail.
                _log.warning(
                    "http_scratch_store.session_token_refresh_failed",
                    session_id=session_id, error=str(exc),
                )
                return False
            if not fresh or fresh == self._session_token:
                return False
            _log.warning(
                "http_scratch_store.session_token_refreshed_from_lease",
                session_id=session_id,
            )
            self._session_token = fresh
            self._headers[_HEADER_T1_SESSION] = fresh
            # Subprocess children resolve NX_T1_SESSION at spawn — hand them
            # the live token, not the stranded one. Review M1: export the id
            # alongside when absent, so a later ctor's fallback chain
            # (session_id or NX_T1_SESSION_ID or NX_T1_SESSION) can never
            # collapse the TOKEN into a session id.
            os.environ[_SESSION_ENV] = fresh
            os.environ.setdefault(_SESSION_ID_ENV, session_id)
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
                pass
            self._client = self._build_client()
            return True

    def _build_client(self) -> httpx.Client:
        # RDR-198 (closed, declined): this client carries a BAKED
        # Authorization header, so it must NEVER be shared with another
        # store — doing so sends this domain's credential on the other
        # domain's requests. Twelve of the fourteen httpx stores build
        # auth per call and ARE safe to share; these two are the
        # exception. Guarded by
        # tests/test_constructor_baked_auth_clients_not_shared.py.
        # Converting to per-call auth was measured and declined: the
        # shared-pool benefit was 0.07% of an nx_answer call and the
        # indexer turned out not to construct stores per file at all
        # (T2 nexus_rdr/198-research-2, 198-research-3). Convert BEFORE
        # sharing, never after.
        return httpx.Client(base_url=self._base_url, headers=self._headers, timeout=30.0)

    def close_session(self) -> int:
        """Delete all scratch entries for this session. Returns count deleted.

        Called from MCP lifespan on exit for promptness.  The service also
        runs a periodic TTL sweep (default 24 h) as a crash-safety backstop.
        Idempotent: double-close returns 0, not an error.
        """
        resp = self._post("/v1/t1/session/close", {"session_id": self._session_id})
        return int(resp.get("deleted", 0))

    # ── Write ──────────────────────────────────────────────────────────────────

    def put(
        self,
        content: str,
        tags: str = "",
        persist: bool = False,
        flush_project: str = "",
        flush_title: str = "",
        agent: str = "",
    ) -> str:
        """Store *content* in T1 scratch. Returns the entry UUID.

        Interface matches :meth:`T1Database.put` exactly.

        If *persist* is ``True`` the entry is pre-flagged for SessionEnd
        flush.  Auto-destination when no explicit project/title:
        ``scratch_sessions`` / ``{session_id}_{id}``.
        """
        import uuid as _uuid_mod  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
        doc_id = str(_uuid_mod.uuid4())
        if persist:
            flush_project = flush_project or "scratch_sessions"
            flush_title = flush_title or f"{self._session_id}_{doc_id}"
        if not agent:
            agent = os.environ.get("NX_AGENT", "")

        payload: dict[str, Any] = {
            "id": doc_id,
            "session_id": self._session_id,
            "content": content,
            "tags": tags,
            "agent": agent or None,
            "flagged": persist,
            "flush_project": flush_project or None,
            "flush_title": flush_title or None,
        }
        resp = self._post("/v1/t1/put", payload)
        return str(resp["id"])

    # ── Read ───────────────────────────────────────────────────────────────────

    def get(self, id: str) -> dict | None:
        """Return the entry dict for *id*, or None if not found / wrong session.

        *id* may be the full UUID or a unique session-owned prefix (uses
        resolve_prefix_candidates internally when a full-UUID miss occurs).

        BEHAVIOR CHANGE: semantics shift from ChromaDB cosine to column-filter.
        The entry is scoped to ``(tenant, session_id)`` via Postgres RLS +
        WHERE; cross-session access returns None.
        """
        # Try exact id first
        resp_data = self._post_raw("/v1/t1/get", {"id": id, "session_id": self._session_id})
        if resp_data.get("found") is False:
            # Prefix fallback: find full id
            candidates = self.resolve_prefix_candidates(id)
            if len(candidates) == 1:
                resp_data = self._post_raw(
                    "/v1/t1/get", {"id": candidates[0], "session_id": self._session_id}
                )
            elif candidates:
                _log.warning(
                    "t1_http_get_ambiguous_prefix",
                    requested_id=id,
                    candidates=candidates,
                    session_id=self._session_id,
                )
                return None
            else:
                return None
        if resp_data.get("found") is False:
            return None
        return resp_data if resp_data else None

    def search(self, query: str, n_results: int = 10) -> list[dict]:
        """FTS search over content + tags, scoped to this session.

        BEHAVIOR CHANGE: was vector/cosine (ChromaDB ONNX); now FTS
        (Postgres tsvector, OR: English stemmer + simple identifier config).
        Results are ordered by ts_rank descending (best first).
        """
        resp = self._post(
            "/v1/t1/search",
            {"query": query, "session_id": self._session_id, "limit": n_results},
            mutates=False,
        )
        return resp.get("results", [])

    def list_entries(self) -> list[dict]:
        """Return all entries for this session (ordered ts desc)."""
        resp = self._post("/v1/t1/list", {"session_id": self._session_id}, mutates=False)
        return resp.get("entries", [])

    def flagged_entries(self) -> list[dict]:
        """Return all flagged entries for this session."""
        resp = self._post("/v1/t1/flagged", {"session_id": self._session_id}, mutates=False)
        return resp.get("entries", [])

    # ── Flag / unflag ──────────────────────────────────────────────────────────

    def flag(self, id: str, project: str = "", title: str = "") -> None:
        """Mark *id* for SessionEnd flush to T2.

        Auto-destination when *project*/*title* omitted:
        ``scratch_sessions`` / ``{session_id}_{id}``.

        Raises KeyError when the entry is not found.
        """
        flush_project = project or "scratch_sessions"
        flush_title = title or f"{self._session_id}_{id}"
        resp = self._post(
            "/v1/t1/flag",
            {
                "id": id,
                "session_id": self._session_id,
                "flush_project": flush_project,
                "flush_title": flush_title,
            },
        )
        if not resp.get("ok"):
            raise KeyError(f"No scratch entry: {id!r}")

    def unflag(self, id: str) -> None:
        """Remove the flush-on-SessionEnd marking from *id*.

        Raises KeyError when the entry is not found.
        """
        resp = self._post(
            "/v1/t1/unflag",
            {"id": id, "session_id": self._session_id},
        )
        if not resp.get("ok"):
            raise KeyError(f"No scratch entry: {id!r}")

    # ── Promote ────────────────────────────────────────────────────────────────

    def promote(
        self, id: str, project: str, title: str, t2: object,
    ) -> "PromotionReport":
        """Copy T1 entry *id* to T2 immediately. Returns a PromotionReport.

        *t2* is a ``T2Database`` (or any object exposing ``.put()`` and
        ``.memory`` — the shape the retired daemon-backed ``T2Client``
        also had, RDR-128 P3; the daemon died in RDR-158 P4).

        Overlap detection mirrors :meth:`T1Database.promote`: fetches the entry
        from the T1 service, runs :func:`~nexus.db.t1._find_promote_overlap_candidates`
        (same Jaccard threshold), writes to T2 regardless of overlap, and returns
        a ``PromotionReport`` with ``action="overlap_detected"`` or ``action="new"``.

        This path is valid on the service backend because promote is a write-through
        to T2 (not a T1 read-path), so there is no session-scoping concern unique to
        the service backend.

        Raises ``KeyError`` when the entry is not found in this session.
        """
        from nexus.db.t1 import _find_promote_overlap_candidates  # noqa: PLC0415 — deferred to avoid circular import
        from nexus.types import PromotionReport  # noqa: PLC0415 — deferred to avoid circular import

        entry = self.get(id)
        if entry is None:
            raise KeyError(f"No scratch entry: {id!r}")

        matches = _find_promote_overlap_candidates(entry["content"], project, t2)  # type: ignore[arg-type]
        if matches:
            best = matches[0]
            report = PromotionReport(
                action="overlap_detected",
                existing_title=best["title"],
                merged=False,
            )
        else:
            report = PromotionReport(action="new")
        t2.put(project=project, title=title, content=entry["content"], tags=entry.get("tags", ""))  # type: ignore[union-attr]
        return report

    # ── Delete / clear ─────────────────────────────────────────────────────────

    def delete(self, id: str) -> bool:
        """Delete a scratch entry by full UUID or unique session-owned prefix.

        Returns True when deleted, False when not found or not in this session.
        """
        # Prefix resolution for ergonomics (mirrors T1Database.delete)
        resolved = id
        if "-" not in id:
            # Looks like a short prefix; attempt resolution
            candidates = self.resolve_prefix_candidates(id)
            if len(candidates) == 1:
                resolved = candidates[0]
            elif candidates:
                _log.warning(
                    "t1_http_delete_ambiguous_prefix",
                    requested_id=id,
                    candidates=candidates,
                    session_id=self._session_id,
                )
                return False
            else:
                return False

        resp = self._post(
            "/v1/t1/delete",
            {"id": resolved, "session_id": self._session_id},
        )
        return bool(resp.get("deleted", False))

    def clear(self) -> int:
        """Remove all session entries. Returns the count deleted.

        Implemented via session-close + count.  Note: this ALSO invalidates
        the current session on the service side (all entries gone).  Callers
        that need to continue using the scratch store after clear() should
        create a fresh ``HttpScratchStore`` with a new session_id.
        """
        return self.close_session()

    def resolve_prefix_candidates(self, id: str) -> list[str]:
        """Return session-owned ids matching *id* as exact or prefix.

        Empty list when nothing matches; one-element list when a unique
        resolution exists; multi-element when ambiguous.
        """
        resp = self._post(
            "/v1/t1/resolve_prefix",
            {"prefix": id, "session_id": self._session_id},
            mutates=False,
        )
        return resp.get("ids", [])

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any], *, mutates: bool = True) -> dict[str, Any]:
        """POST *payload* to *path* and return the parsed JSON body.

        Raises RuntimeError on non-2xx responses.

        ``mutates`` (nexus-a2qhz) defaults to ``True`` — most callers here
        (``put``, ``flag``, ``unflag``, ``delete``, ``clear``,
        ``close_session``) really are writes against T1. ``search``,
        ``list_entries``, ``flagged_entries``, and
        ``resolve_prefix_candidates`` (round-2 review fix — a pure
        disambiguation LOOKUP the MCP ``scratch`` tool's ``get``/``delete``
        fall back to on an ambiguous or missing exact id; misclassifying
        it as a write turned an ordinary "not found" UX into an uncaught
        ``ProductionWriteGuardError`` from a dev checkout) send a query
        over POST (the filter body doesn't fit a GET query string) and
        pass ``mutates=False``. ``_post_raw`` (``get``) never guards at
        all — it is read-only. This class is not a
        ``RefreshableHttpStoreMixin`` adopter (bespoke bearer-header-baked
        transport, same family as ``HttpTokenStore``), so it calls
        :func:`~nexus.db.service_endpoint.guard_production_write` directly.
        """
        if mutates:
            from nexus.db.service_endpoint import guard_production_write  # noqa: PLC0415 — deferred: only on the mutates=True branch, no circular import (service_endpoint imports no nexus.* modules)

            guard_production_write(self._base_url)
        sent_token = getattr(self, "_session_token", "")
        # critic S4 (nexus-ssqk9): resolve the Authorization header FRESH
        # for this request rather than the client's construction-time
        # default -- see _current_authorization_header's docstring.
        request_headers = {"Authorization": self._current_authorization_header()}
        try:
            resp = self._client.post(path, json=payload, headers=request_headers)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            # nexus-om64x: stale endpoint after a supervisor restart (connect-refused
            # OR TCP RST on a pooled in-flight connection) — re-resolve from the lease
            # and retry ONCE before failing.
            if not self._rebind_from_lease():
                raise RuntimeError(f"HttpScratchStore: connect failed on {path}")
            try:
                resp = self._client.post(path, json=payload, headers={"Authorization": self._current_authorization_header()})
            except httpx.HTTPError as exc:
                raise RuntimeError(f"HttpScratchStore: connect failed on retry {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HttpScratchStore: network error on {path}: {exc}") from exc
        heal_suffix = ""  # nexus-fe96p: only ever set inside the 401 branch below
        if resp.status_code == 401:
            # nexus-cmzib (critic kopmj): an EDGE-authored 401 short-circuits
            # BEFORE the self-heal chain — lease refresh / re-mint cannot fix
            # a rejection the application never saw, and the heal's retry
            # would just re-hit the edge. (conexus's edge audit says their
            # edge never authors a post-forward 401; this guards the class,
            # not the current topology.)
            edge = self._edge_refusal(path, resp)
            if edge is not None:
                raise RuntimeError(edge)
            # nexus-g5hzk: the owner rotated the SESSION token; ours went
            # stale. Try that self-heal FIRST (unchanged precedence).
            healed = self._refresh_session_token_from_lease(sent_token)
            if healed:
                heal_suffix = f" {HEAL_ADOPTED_SUFFIX}"
            else:
                # nexus-wrwb7: the session-token refresh didn't resolve it --
                # if a mint_token credential is configured, the AUTHORIZATION
                # bearer (a self-minted data token) may be what actually went
                # stale. Try re-minting before giving up. The suffix records
                # WHICH mechanism healed (reviewer fold on nexus-fe96p).
                healed = self._remint_data_token_and_rebuild()
                heal_suffix = f" {HEAL_REMINT_SUFFIX}" if healed else f" {HEAL_DECLINED_SUFFIX}"
            if healed:
                try:
                    # nexus-fe96p: resolve the Authorization header FRESH on
                    # the retry too -- the heal above may have rebuilt the
                    # client (re-baking self._headers), but self._headers
                    # only ever carries the Authorization value as of the
                    # LAST bearer-touching heal/ctor; a since-rotated bearer
                    # (RDR-005 self-minted data token) is only visible via
                    # _current_authorization_header(), same as the initial
                    # request above.
                    resp = self._client.post(
                        path, json=payload,
                        headers={"Authorization": self._current_authorization_header()},
                    )
                except httpx.HTTPError as exc:
                    raise RuntimeError(
                        f"HttpScratchStore: network error on token-refresh retry {path}: {exc}"
                    ) from exc
        if not resp.is_success:
            edge = self._edge_refusal(path, resp)
            if edge is not None:
                raise RuntimeError(edge)
            if resp.status_code == 401:
                raise RuntimeError(
                    f"{SESSION_UNAUTHORIZED_MARKER} on {path}{heal_suffix}: {resp.text[:200]}"
                )
            raise RuntimeError(
                f"HttpScratchStore: {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    @staticmethod
    def _edge_refusal(path: str, resp: httpx.Response) -> str | None:
        """nexus-cmzib: structured message when the EDGE (WAF) refused the
        request — never the raw HTML error page, and with the
        shell-substitution defang hint when the body carries the trigger.
        Checked BEFORE the 401 session-marker branch: an edge-authored
        refusal says nothing about the session token, and the marker's
        remint guidance would send the caller after the wrong cause."""
        from nexus.db.edge_refusal import edge_refusal_message  # noqa: PLC0415 — deferred to avoid import-cycle risk at module load

        try:
            request_body = resp.request.content.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — request-body access is diagnostics-only; never fail the error path over it
            request_body = ""
        return edge_refusal_message(
            f"HttpScratchStore {path}", resp.status_code, resp.headers, request_body,
        )

    def _post_raw(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST *payload* to *path* and return parsed JSON without raising on 404-class."""
        sent_token = getattr(self, "_session_token", "")
        # critic S4 (nexus-ssqk9): see _post's matching comment.
        request_headers = {"Authorization": self._current_authorization_header()}
        try:
            resp = self._client.post(path, json=payload, headers=request_headers)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            # nexus-om64x: stale endpoint after a supervisor restart (connect-refused
            # OR TCP RST on a pooled in-flight connection) — re-resolve + retry once.
            if not self._rebind_from_lease():
                raise RuntimeError(f"HttpScratchStore: connect failed on {path}")
            try:
                resp = self._client.post(path, json=payload, headers={"Authorization": self._current_authorization_header()})
            except httpx.HTTPError as exc:
                raise RuntimeError(f"HttpScratchStore: connect failed on retry {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HttpScratchStore: network error on {path}: {exc}") from exc
        heal_suffix = ""  # nexus-fe96p: only ever set inside the 401 branch below
        if resp.status_code == 401:
            # nexus-cmzib (critic kopmj): edge-authored 401 short-circuits the
            # heal chain — see _post's matching comment.
            edge = self._edge_refusal(path, resp)
            if edge is not None:
                raise RuntimeError(edge)
            # nexus-g5hzk: rotated SESSION token — try that self-heal first.
            healed = self._refresh_session_token_from_lease(sent_token)
            if healed:
                heal_suffix = f" {HEAL_ADOPTED_SUFFIX}"
            else:
                # nexus-wrwb7: fall back to a data-token re-mint (see _post's
                # matching comment for the full rationale + suffix split).
                healed = self._remint_data_token_and_rebuild()
                heal_suffix = f" {HEAL_REMINT_SUFFIX}" if healed else f" {HEAL_DECLINED_SUFFIX}"
            if healed:
                try:
                    # nexus-fe96p: see _post's matching comment -- resolve
                    # the Authorization header FRESH on the retry too.
                    resp = self._client.post(
                        path, json=payload,
                        headers={"Authorization": self._current_authorization_header()},
                    )
                except httpx.HTTPError as exc:
                    raise RuntimeError(
                        f"HttpScratchStore: network error on token-refresh retry {path}: {exc}"
                    ) from exc
        if resp.status_code == 404:
            return {"found": False}
        if not resp.is_success:
            edge = self._edge_refusal(path, resp)
            if edge is not None:
                raise RuntimeError(edge)
            if resp.status_code == 401:
                raise RuntimeError(
                    f"{SESSION_UNAUTHORIZED_MARKER} on {path}{heal_suffix}: {resp.text[:200]}"
                )
            raise RuntimeError(
                f"HttpScratchStore: {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()
