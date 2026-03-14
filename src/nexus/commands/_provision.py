# SPDX-License-Identifier: AGPL-3.0-or-later
"""ChromaDB Cloud database provisioning helpers."""
from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import chromadb

_log = structlog.get_logger(__name__)

_CHROMA_CLOUD_HOST = "api.trychroma.com"


def _resolve_cloud_tenant(api_key: str) -> str:
    """Return the real tenant UUID for a Chroma Cloud API key.

    Calls ``GET https://api.trychroma.com/api/v2/auth/identity`` which returns
    the authoritative tenant UUID for the key.  The literal string
    ``"default_tenant"`` is rejected with 403 by Chroma Cloud; the UUID
    returned here must be used in all admin API calls.
    """
    req = urllib.request.Request(
        f"https://{_CHROMA_CLOUD_HOST}/api/v2/auth/identity",
        headers={"x-chroma-token": api_key},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["tenant"]


def _cloud_admin_client(api_key: str) -> "chromadb.AdminClient":
    """Return a ChromaDB AdminClient pointed at Chroma Cloud.

    Mirrors the same Settings wiring used internally by ``chromadb.CloudClient``
    (verified against chromadb 0.6.x; review if upgrading chromadb major version).

    Note: ``chroma_overwrite_singleton_tenant_database_access_from_auth`` does
    **not** rewrite the tenant in AdminClient requests — callers must supply the
    real tenant UUID (via :func:`_resolve_cloud_tenant`) when calling admin
    methods such as ``create_database`` or ``get_database``.
    """
    import chromadb
    from chromadb import Settings
    from chromadb.auth.token_authn import TokenTransportHeader

    settings = Settings()
    settings.chroma_api_impl = "chromadb.api.fastapi.FastAPI"
    settings.chroma_server_host = _CHROMA_CLOUD_HOST
    settings.chroma_server_http_port = 443
    settings.chroma_server_ssl_enabled = True
    settings.chroma_client_auth_provider = (
        "chromadb.auth.token_authn.TokenAuthClientProvider"
    )
    settings.chroma_client_auth_credentials = api_key
    settings.chroma_auth_token_transport_header = TokenTransportHeader.X_CHROMA_TOKEN
    settings.chroma_overwrite_singleton_tenant_database_access_from_auth = True
    return chromadb.AdminClient(settings)


def ensure_databases(
    admin: "chromadb.AdminClient",
    *,
    base: str,
    tenant: str = "default_tenant",
) -> dict[str, bool]:
    """Create the T3 database if it does not already exist.

    The ``tenant`` parameter is resolved to the real Chroma Cloud tenant UUID
    via :func:`_resolve_cloud_tenant` before any API calls are made.  The
    literal string ``"default_tenant"`` is rejected with 403 by Chroma Cloud;
    passing the UUID causes ``create_database`` and ``get_database`` to behave
    correctly.

    Returns a mapping of ``{base: created}`` where ``created=True`` means the
    database was freshly created and ``False`` means it already existed.

    ``UniqueConstraintError`` (HTTP 409) is silently swallowed.  For any other
    ``ChromaError``, ``get_database`` is called to verify actual existence
    before re-raising — this correctly handles cases where Chroma Cloud returns
    a non-409 error for create-on-existing.
    """
    from chromadb.errors import ChromaError, UniqueConstraintError

    # Resolve the real tenant UUID so all admin operations use the correct path.
    try:
        api_key = admin.get_chroma_cloud_api_key_from_clients()
        tenant = _resolve_cloud_tenant(api_key)
    except Exception as exc:
        _log.warning("provision.tenant_resolve_failed", error=str(exc))
        # Fall through with the provided tenant (works for self-hosted setups).

    try:
        admin.create_database(base, tenant=tenant)
        return {base: True}
    except UniqueConstraintError:
        return {base: False}
    except ChromaError as exc:
        # Verify actual existence before deciding whether to re-raise.
        try:
            admin.get_database(base, tenant=tenant)
            return {base: False}
        except Exception:
            raise exc
