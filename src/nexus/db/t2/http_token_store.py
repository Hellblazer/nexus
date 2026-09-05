# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpTokenStore — thin HTTP client over the RDR-152 token lifecycle endpoints.

The consumer of the ``/v1/tenants/*`` and ``/v1/service-tokens/*`` admin endpoints
(bead nexus-gmiaf.32.3). All SQL lives in the Java service; this client only marshals
requests and parses responses. Used by the ``nx tenant`` and ``nx service token`` CLI.

Config (same contract as the other service clients):
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required)
    NX_SERVICE_TOKEN — bearer token used to authenticate the admin call. During the
        bootstrap window this is the shared NX_SERVICE_TOKEN the storage-service
        supervisor publishes; per-tenant minted tokens replace it once provisioned.

The raw minted token is returned ONLY in the issue/rotate/create response and is shown
once by the CLI; the service stores only its hash.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

#: Default tenant header value (matches TenantConstants.DEFAULT_TENANT in the service).
DEFAULT_TENANT: str = "default"


# RDR-152 nexus-fjwxh: env-only resolution replaced by the centralized
# resolver (env halves -> ServiceRegistry lease -> fail loud), so the
# T2 service-mode default works wherever the supervisor is running.
# nexus-bgh2j: construction-time resolution gets the SAME evidence-gated
# bounded wait as the nine mixin adopters (call sites unchanged — only
# the alias target moved to the gated resolver).
from nexus.db.service_endpoint import (
    resolve_service_endpoint_with_evidence_gate as _resolve_endpoint,
)


class HttpTokenStore:
    """Client for the token lifecycle admin endpoints.

    Args:
        base_url: Optional override for ``http://<host>:<port>``. When supplied the
            host/port env-vars are ignored; the token env-var is still required unless
            ``_token`` is passed explicitly.
        tenant:   Tenant header to stamp (the wildcard bootstrap token requires one).
        prefer_data_token: nexus-maf9l — the SESSION-lifecycle call sites
            (T1 session start/close in ``db/t1.py``, ``mcp/core.py``,
            ``operators/dispatch.py``) pass True so an ARMED pass-through
            box presents its minted DATA token instead of the static
            credential: there the static token is scope=mint-locked and the
            engine rejects it outside the mint surface, which killed every
            T1 session mint on the first armed box (RDR-005 step (d),
            2026-08-30) the moment its pre-armed leases expired. Mirrors
            the ``bearer_for``-beats-static contract every other store
            carries. The default False is LOAD-BEARING: the admin surface
            (``nx tenant`` / service-token verbs) 403s mint- and
            data-scoped bearers wholesale, so preferring a data token
            there would break admin flows. Unarmed boxes see no change
            (``bearer_for`` returns ``None``); a mint failure raises
            ``DataTokenMintError`` — the designed no-fallback path.
    """

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        *,
        _token: str | None = None,
        prefer_data_token: bool = False,
    ) -> None:
        if base_url is not None:
            if _token is None:
                _token = os.environ.get("NX_SERVICE_TOKEN", "")
                if not _token:
                    raise RuntimeError("NX_SERVICE_TOKEN is required for token administration.")
            self._base_url = base_url.rstrip("/")
        else:
            self._base_url, token = _resolve_endpoint()
            _token = token
        self._tenant = tenant
        self._prefer_data_token = prefer_data_token
        self._using_data_token = False
        self._auth_token = _token or ""
        self._apply_data_token_preference()
        self._client = self._build_client()
        _log.debug("http_token_store.init", base_url=self._base_url)

    def _apply_data_token_preference(self) -> None:
        """Replace the static token with the manager's DATA token when
        ``prefer_data_token`` and minting is configured (``bearer_for``
        returns ``None`` otherwise — the unarmed no-op). Re-run after any
        event that overwrites ``_auth_token`` (endpoint rebind), so the
        preference survives it."""
        if not self._prefer_data_token:
            return
        from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred to avoid circular import

        token = get_data_token_manager().bearer_for(self._base_url, self._tenant)
        if token is not None:
            self._auth_token = token
            self._using_data_token = True

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
        return httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._auth_token}",
                "X-Nexus-Tenant": self._tenant,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def _rebind_from_lease(self) -> bool:
        """nexus-om64x: on connection-refused, re-resolve the endpoint from the
        ServiceRegistry lease (bypassing the stale env port) and rebuild the
        client. Returns True if rebound to a NEW endpoint, False otherwise.

        nexus-7dsgp (GH #1405 defect 1): passes
        ``DEFAULT_LEASE_WAIT_BUDGET_S`` so a retry landing in the
        supervisor-respawn gap polls for up to 12s instead of giving up on
        the first miss — the only caller of ``recover_endpoint_from_lease``
        on this store, so the wait applies exactly once per ``_post`` call.
        """
        from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
            DEFAULT_LEASE_WAIT_BUDGET_S,
            recover_endpoint_from_lease,
        )

        recovered = recover_endpoint_from_lease(
            self._base_url, wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S
        )
        if recovered is None:
            return False
        new_url, new_token = recovered
        _log.warning("http_token_store.rebind", old=self._base_url, new=new_url)
        self._base_url = new_url
        if new_token:
            self._auth_token = new_token
            self._using_data_token = False
        self._apply_data_token_preference()
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — best-effort close of stale client during reset
            pass
        self._client = self._build_client()
        return True

    def close(self) -> None:
        """Close the connection pool (idempotent)."""
        self._client.close()

    def __enter__(self) -> HttpTokenStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=body)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            # nexus-om64x: stale endpoint (supervisor restarted on a new port; our
            # env port is dead). ConnectError = connect-refused (store reconnects
            # post-restart); RemoteProtocolError/ReadError = TCP RST on a pooled
            # keep-alive connection that was in flight when the JVM was SIGTERM'd
            # (mirrors http_vector_client's reset handling). Re-resolve from the
            # lease and retry ONCE.
            if not self._rebind_from_lease():
                raise
            resp = self._client.post(path, json=body)
        if (
            resp.status_code == 401
            and self._prefer_data_token
            and self._using_data_token
        ):
            # nexus-maf9l, the adopters' invalidate-and-reresolve contract:
            # one 401 on a DATA token means the cached token went stale
            # server-side (expiry, rotation); invalidate, re-mint via
            # bearer_for, rebuild the baked client, retry ONCE. A second
            # 401 raises. Static-token 401s stay loud immediately.
            from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred to avoid circular import

            manager = get_data_token_manager()
            manager.invalidate(self._base_url, self._tenant)
            self._using_data_token = False
            self._apply_data_token_preference()
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — best-effort close of the stale client before rebuild
                pass
            self._client = self._build_client()
            resp = self._client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    # ── Lifecycle verbs ─────────────────────────────────────────────────────────

    def create_tenant(self, name: str) -> dict[str, Any]:
        """Mint a new tenant's first token. Returns {tenant, token, token_hash}."""
        return self._post("/v1/tenants/create", {"name": name})

    def issue_token(
        self, tenant: str, label: str | None = None, ttl_seconds: int | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Issue a bound token. Returns {tenant, token, token_hash}.

        ``scope`` (nexus-868dq): ``None`` preserves the server default
        (``tenant``); ``"mint"`` issues the data-token mint credential
        (operator-only server-side — the conexus-edge provisioning surface).
        """
        body: dict[str, Any] = {"tenant": tenant}
        if label is not None:
            body["label"] = label
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if scope is not None:
            body["scope"] = scope
        return self._post("/v1/service-tokens/issue", body)

    def rotate_token(self, tenant: str, grace_seconds: int | None = None) -> dict[str, Any]:
        """Zero-downtime rotate: issue a new token, grace-expire the old. Returns the new token."""
        body: dict[str, Any] = {"tenant": tenant}
        if grace_seconds is not None:
            body["grace_seconds"] = grace_seconds
        return self._post("/v1/service-tokens/rotate", body)

    def revoke_token(self, selector: str) -> dict[str, Any]:
        """Revoke a token by hash or unique prefix. Returns {revoked, token_hash?}."""
        return self._post("/v1/service-tokens/revoke", {"selector": selector})

    def list_tokens(self, tenant: str | None = None) -> list[dict[str, Any]]:
        """List token rows (never plaintext). Returns the tokens array."""
        body: dict[str, Any] = {}
        if tenant is not None:
            body["tenant"] = tenant
        return self._post("/v1/service-tokens/list", body, mutates=False).get("tokens", [])

    # ── Session tokens (bead nexus-gmiaf.32.4) ────────────────────────────────

    def start_session(self, session_id: str, ttl_seconds: int | None = None) -> dict[str, Any]:
        """Mint the per-session token for SESSION_ID (tenant from the bearer). Returns
        {session_token, session_id, expires_in_seconds}; the raw token is set into
        NX_T1_SESSION by the caller and shown to no one."""
        body: dict[str, Any] = {"session_id": session_id}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return self._post("/v1/sessions/start", body)

    def close_session(self, session_id: str) -> dict[str, Any]:
        """Delete the per-session token for SESSION_ID. Returns {closed: <count>}."""
        return self._post("/v1/sessions/close", {"session_id": session_id})
