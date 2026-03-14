# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nexus.commands._provision — cloud database provisioning."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from nexus.commands._provision import (
    _CHROMA_CLOUD_HOST,
    _cloud_admin_client,
    _resolve_cloud_tenant,
    ensure_databases,
)


# ── _resolve_cloud_tenant ─────────────────────────────────────────────────────

def _mock_urlopen(tenant_uuid: str):
    """Return a context-manager mock that yields a response with the given UUID."""
    body = json.dumps({"tenant": tenant_uuid, "user_id": "42", "databases": []}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_resolve_cloud_tenant_returns_uuid() -> None:
    """Returns the 'tenant' field from the auth/identity response."""
    uuid = "c749e1f8-2c59-43fc-8e44-19d534e1404a"
    cm = _mock_urlopen(uuid)
    with patch("nexus.commands._provision.urllib.request.urlopen", return_value=cm):
        result = _resolve_cloud_tenant("ck-test-key")
    assert result == uuid


def test_resolve_cloud_tenant_uses_correct_url_and_header() -> None:
    """Requests the correct endpoint with the API key in x-chroma-token."""
    cm = _mock_urlopen("some-uuid")
    with patch("nexus.commands._provision.urllib.request.Request") as mock_req, \
         patch("nexus.commands._provision.urllib.request.urlopen", return_value=cm):
        _resolve_cloud_tenant("ck-my-key")

    mock_req.assert_called_once()
    url_arg, = mock_req.call_args.args
    assert f"https://{_CHROMA_CLOUD_HOST}/api/v2/auth/identity" == url_arg
    assert mock_req.call_args.kwargs["headers"] == {"x-chroma-token": "ck-my-key"}


def test_resolve_cloud_tenant_propagates_network_error() -> None:
    """Network failures propagate to the caller (not swallowed)."""
    with patch("nexus.commands._provision.urllib.request.urlopen",
               side_effect=OSError("connection refused")):
        with pytest.raises(OSError):
            _resolve_cloud_tenant("ck-bad")


# ── _cloud_admin_client ───────────────────────────────────────────────────────

def test_cloud_admin_client_settings_wiring() -> None:
    """AdminClient is built with the correct Chroma Cloud settings."""
    import chromadb as _chromadb_real
    from chromadb.auth.token_authn import TokenTransportHeader

    mock_admin = MagicMock()
    with patch.object(_chromadb_real, "AdminClient", return_value=mock_admin) as mock_ctor:
        result = _cloud_admin_client("ck-secret")

    assert result is mock_admin
    mock_ctor.assert_called_once()
    settings = mock_ctor.call_args.args[0]
    assert settings.chroma_api_impl == "chromadb.api.fastapi.FastAPI"
    assert settings.chroma_server_host == _CHROMA_CLOUD_HOST
    assert settings.chroma_server_http_port == 443
    assert settings.chroma_server_ssl_enabled is True
    assert "TokenAuthClientProvider" in settings.chroma_client_auth_provider
    assert settings.chroma_client_auth_credentials == "ck-secret"
    assert settings.chroma_auth_token_transport_header == TokenTransportHeader.X_CHROMA_TOKEN
    assert settings.chroma_overwrite_singleton_tenant_database_access_from_auth is True


# ── ensure_databases ──────────────────────────────────────────────────────────

def _make_admin(api_key: str = "ck-key", tenant_uuid: str = "uuid-123") -> MagicMock:
    """Return a mock AdminClient that resolves correctly."""
    admin = MagicMock()
    admin.get_chroma_cloud_api_key_from_clients.return_value = api_key
    return admin


def _patch_resolve(tenant_uuid: str = "uuid-123"):
    return patch(
        "nexus.commands._provision._resolve_cloud_tenant",
        return_value=tenant_uuid,
    )


def test_ensure_databases_creates_single_db() -> None:
    """Fresh install: single database is created, result is {base: True}."""
    admin = _make_admin()
    with _patch_resolve("t-uuid"):
        result = ensure_databases(admin, base="mynexus")

    assert result == {"mynexus": True}
    admin.create_database.assert_called_once_with("mynexus", tenant="t-uuid")


def test_ensure_databases_idempotent_via_unique_constraint() -> None:
    """Second call: UniqueConstraintError → {base: False} (already existed)."""
    from chromadb.errors import UniqueConstraintError

    admin = _make_admin()
    admin.create_database.side_effect = UniqueConstraintError("already exists")
    with _patch_resolve():
        result = ensure_databases(admin, base="mynexus")

    assert result == {"mynexus": False}
    admin.get_database.assert_not_called()


def test_ensure_databases_idempotent_via_chroma_error_get_succeeds() -> None:
    """Non-409 ChromaError + successful get_database → False (database exists)."""
    from chromadb.errors import InternalError

    admin = _make_admin()
    admin.create_database.side_effect = InternalError("Permission denied.")
    admin.get_database.return_value = MagicMock()  # exists
    with _patch_resolve():
        result = ensure_databases(admin, base="mynexus")

    assert result == {"mynexus": False}
    admin.get_database.assert_called_once_with("mynexus", tenant="uuid-123")


def test_ensure_databases_reraises_when_get_also_fails() -> None:
    """ChromaError on create + failure on get → original error re-raised."""
    from chromadb.errors import ChromaError, InternalError

    admin = _make_admin()
    original_exc = InternalError("some real error")
    admin.create_database.side_effect = original_exc
    admin.get_database.side_effect = InternalError("also failed")
    with _patch_resolve():
        with pytest.raises(ChromaError, match="some real error"):
            ensure_databases(admin, base="mynexus")


def test_ensure_databases_tenant_resolve_failure_falls_through() -> None:
    """If tenant resolution fails, the provided tenant default is used."""
    admin = _make_admin()
    admin.get_chroma_cloud_api_key_from_clients.side_effect = RuntimeError("no key")
    with _patch_resolve() as mock_resolve:
        mock_resolve.side_effect = RuntimeError("network error")
        result = ensure_databases(admin, base="nexus", tenant="my-fallback-tenant")

    admin.create_database.assert_called_once_with("nexus", tenant="my-fallback-tenant")
    assert result == {"nexus": True}
