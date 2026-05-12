# SPDX-License-Identifier: AGPL-3.0-or-later
"""ChromaDB Cloud database provisioning helpers."""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
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

    nexus-8g79.22: replaced ``urllib.request.urlopen`` with ``httpx`` for
    consistency with the rest of the codebase (every other outbound HTTP
    call goes through httpx). ``httpx`` uses the system CA bundle by
    default, same as urllib; the explicit timeout is preserved.
    """
    response = httpx.get(
        f"https://{_CHROMA_CLOUD_HOST}/api/v2/auth/identity",
        headers={"x-chroma-token": api_key},
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()["tenant"]


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

    # nexus-8g79.22: constructor kwargs instead of attribute mutation —
    # the attribute-set form was deprecated in chromadb 0.4.x and the
    # deprecation timeline keeps quietly advancing. All values are
    # static here; pre-fix the only reason for the multi-line setter
    # form was historical (RDR-099 D1 first draft).
    settings = Settings(
        chroma_api_impl="chromadb.api.fastapi.FastAPI",
        chroma_server_host=_CHROMA_CLOUD_HOST,
        chroma_server_http_port=443,
        chroma_server_ssl_enabled=True,
        chroma_client_auth_provider=(
            "chromadb.auth.token_authn.TokenAuthClientProvider"
        ),
        chroma_client_auth_credentials=api_key,
        chroma_auth_token_transport_header=TokenTransportHeader.X_CHROMA_TOKEN,
        chroma_overwrite_singleton_tenant_database_access_from_auth=True,
    )
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
        # nexus-8g79.33: ChromaDB error bodies sometimes echo the
        # offending token in the message (e.g. "invalid token: sk-...").
        # Truncate to 120 chars matching retry.py's safety bound so
        # tokens cannot leak verbatim into structured logs.
        _log.warning(
            "provision.tenant_resolve_failed",
            error=str(exc)[:120],
        )
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
