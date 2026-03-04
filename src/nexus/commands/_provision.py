# SPDX-License-Identifier: AGPL-3.0-or-later
"""ChromaDB Cloud database provisioning helpers."""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from nexus.db.t3 import _STORE_TYPES

if TYPE_CHECKING:
    import chromadb

_log = structlog.get_logger(__name__)


def _cloud_admin_client(api_key: str) -> "chromadb.AdminClient":
    """Return a ChromaDB AdminClient pointed at Chroma Cloud.

    Mirrors the same Settings wiring used internally by ``chromadb.CloudClient``
    (verified against chromadb 0.6.x; review if upgrading chromadb major version).
    """
    import chromadb
    from chromadb import Settings
    from chromadb.auth.token_authn import TokenTransportHeader

    settings = Settings()
    settings.chroma_api_impl = "chromadb.api.fastapi.FastAPI"
    settings.chroma_server_host = "api.trychroma.com"
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
    """Create the four T3 databases if they do not already exist.

    ``tenant`` defaults to ``"default_tenant"``; when the AdminClient is built
    via :func:`_cloud_admin_client` the
    ``chroma_overwrite_singleton_tenant_database_access_from_auth`` flag causes
    Chroma Cloud to substitute the API-key-derived tenant, so the value passed
    here is effectively ignored for cloud accounts.

    Returns a mapping of ``{db_name: created}`` where ``created=True`` means the
    database was freshly created and ``False`` means it already existed.

    ``UniqueConstraintError`` (HTTP 409) is silently swallowed — it means the
    database already exists, which is the desired end state.  For any other
    ``ChromaError`` (e.g. 403 from some Chroma Cloud plans where
    ``create_database``, ``get_database``, and ``list_databases`` all return
    403), a ``CloudClient`` heartbeat is used to verify actual reachability
    before re-raising.
    """
    from chromadb.errors import ChromaError, UniqueConstraintError

    result: dict[str, bool] = {}
    for t in _STORE_TYPES:
        db_name = f"{base}_{t}"
        try:
            admin.create_database(db_name, tenant=tenant)
            result[db_name] = True
        except UniqueConstraintError:
            result[db_name] = False
        except ChromaError as exc:
            # Chroma Cloud sometimes returns a generic ChromaError (e.g. 403
            # "Permission denied") instead of UniqueConstraintError (409) when
            # create_database is called on an existing database.  AdminClient
            # get_database / list_databases also return 403 on the same plans,
            # so we verify existence with a CloudClient heartbeat instead.
            try:
                import chromadb as _chromadb
                api_key = admin.get_chroma_cloud_api_key_from_clients()
                probe = _chromadb.CloudClient(
                    tenant=None, database=db_name, api_key=api_key
                )
                probe.heartbeat()
                result[db_name] = False
            except Exception:
                raise exc
    return result
