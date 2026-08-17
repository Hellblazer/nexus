# SPDX-License-Identifier: AGPL-3.0-or-later
"""make_catalog_client_for_migration data-token treatment (nexus-ssqk9 fix
pass, code-review WORTH-TRACKING finding on nexus-wrwb7).

Two branches, two different (documented) treatments:
  - explicit base_url= -> stays fully pinned, mint_token is NEVER applied
    (a caller naming a non-default target must not have a default-scoped
    mint credential silently presented there).
  - no base_url (the common/default-endpoint path) -> gets the SAME
    data-token override treatment as every other T2 store construction.
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.catalog.factory import make_catalog_client_for_migration


class _CapturingClient:
    def __init__(self, *, base_url: str | None = None, _token: str | None = None) -> None:
        self.base_url = base_url
        self.token = _token


def test_explicit_base_url_stays_pinned_never_applies_mint_token() -> None:
    class _MintedManager:
        def bearer_for(self, base_url: str, tenant: str) -> str | None:
            raise AssertionError("must not be called when base_url is explicit")

    with (
        patch("nexus.catalog.http_catalog_client.HttpCatalogClient", _CapturingClient),
        patch("nexus.db.data_token.get_data_token_manager", return_value=_MintedManager()),
    ):
        client = make_catalog_client_for_migration(
            base_url="http://staging.example:9999", token="explicit-token",
        )

    assert client.base_url == "http://staging.example:9999"
    assert client.token == "explicit-token"


def test_no_base_url_applies_data_token_override() -> None:
    class _MintedManager:
        def bearer_for(self, base_url: str, tenant: str) -> str | None:
            return "self-minted-migration-token"

    with (
        patch("nexus.catalog.http_catalog_client.HttpCatalogClient", _CapturingClient),
        patch("nexus.db.data_token.get_data_token_manager", return_value=_MintedManager()),
        patch(
            "nexus.db.service_endpoint.resolve_service_endpoint",
            return_value=("http://127.0.0.1:9999", "resolved-static-token"),
        ),
    ):
        client = make_catalog_client_for_migration(token="static-token")

    assert client.token == "self-minted-migration-token"
    assert client.base_url == "http://127.0.0.1:9999"


def test_no_base_url_unconfigured_uses_the_passed_token_unchanged() -> None:
    class _InertManager:
        def bearer_for(self, base_url: str, tenant: str) -> str | None:
            return None

    with (
        patch("nexus.catalog.http_catalog_client.HttpCatalogClient", _CapturingClient),
        patch("nexus.db.data_token.get_data_token_manager", return_value=_InertManager()),
        patch(
            "nexus.db.service_endpoint.resolve_service_endpoint",
            return_value=("http://127.0.0.1:9999", "resolved-static-token"),
        ),
    ):
        client = make_catalog_client_for_migration(token="static-token")

    assert client.token == "static-token"
    assert client.base_url == "http://127.0.0.1:9999"


def test_no_base_url_no_token_returns_default_client() -> None:
    """Unchanged pre-existing behavior: no base_url, no token -> the
    no-arg HttpCatalogClient() default-resolution path, never touched by
    this fix."""
    with patch("nexus.catalog.http_catalog_client.HttpCatalogClient", _CapturingClient):
        client = make_catalog_client_for_migration()

    assert client.base_url is None
    assert client.token is None
