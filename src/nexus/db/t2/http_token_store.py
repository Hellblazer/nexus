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


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from the environment.

    Raises:
        RuntimeError: if NX_SERVICE_PORT or NX_SERVICE_TOKEN are not set.
    """
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required for token administration. "
            "Set it to the port where nexus-service is listening."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(f"NX_SERVICE_PORT must be an integer, got: {port_str!r}") from exc
    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required for token administration (the bootstrap "
            "credential that authenticates the admin call)."
        )
    return host, port, token


class HttpTokenStore:
    """Client for the token lifecycle admin endpoints.

    Args:
        base_url: Optional override for ``http://<host>:<port>``. When supplied the
            host/port env-vars are ignored; the token env-var is still required unless
            ``_token`` is passed explicitly.
        tenant:   Tenant header to stamp (the wildcard bootstrap token requires one).
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
                    raise RuntimeError("NX_SERVICE_TOKEN is required for token administration.")
            self._base_url = base_url.rstrip("/")
        else:
            host, port, token = _resolve_config()
            self._base_url = f"http://{host}:{port}"
            _token = token
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {_token}",
                "X-Nexus-Tenant": tenant,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        _log.info("http_token_store.init", base_url=self._base_url)

    def close(self) -> None:
        """Close the connection pool (idempotent)."""
        self._client.close()

    def __enter__(self) -> HttpTokenStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    # ── Lifecycle verbs ─────────────────────────────────────────────────────────

    def create_tenant(self, name: str) -> dict[str, Any]:
        """Mint a new tenant's first token. Returns {tenant, token, token_hash}."""
        return self._post("/v1/tenants/create", {"name": name})

    def issue_token(
        self, tenant: str, label: str | None = None, ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        """Issue a bound token. Returns {tenant, token, token_hash}."""
        body: dict[str, Any] = {"tenant": tenant}
        if label is not None:
            body["label"] = label
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
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
        return self._post("/v1/service-tokens/list", body).get("tokens", [])
