# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-152 nexus-fjwxh — the centralized service-endpoint resolver.

src/nexus/db/service_endpoint.py is the ONE resolver the T2 stores, the
catalog client, and the T3 vector client all route through. Before it the
T2/scratch stores were env-only and would raise the moment the default flipped
to ``service`` on a box that had a running supervisor but no NX_SERVICE_PORT
exported. These tests pin the resolution order: env halves → ServiceRegistry
lease → fail loud.

The autouse ``_isolate_config_dir`` conftest fixture redirects NEXUS_CONFIG_DIR
to tmp_path, so no test here can see a real lease unless it publishes one.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.daemon.service_registry import ServiceRegistry
from nexus.db.service_endpoint import (
    discover_lease,
    resolve_service_config,
    resolve_service_endpoint,
)


def _config_dir() -> Path:
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _publish_lease(*, host: str = "127.0.0.1", port: int, token: str) -> None:
    reg = ServiceRegistry(dir=_config_dir(), tier="storage_service")
    reg.publish(
        str(os.getuid()),
        endpoint={"host": host, "port": port, "token": token},
        version="test",
        owner_token="fjwxh-test-owner",
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    yield


class TestResolveServiceConfig:
    def test_env_only_resolves_without_lease(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_HOST", "10.0.0.5")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        assert resolve_service_config() == ("10.0.0.5", 9999, "env-token")

    def test_lease_fills_when_env_absent(self):
        _publish_lease(port=4242, token="lease-token")
        host, port, token = resolve_service_config()
        assert (host, port, token) == ("127.0.0.1", 4242, "lease-token")

    def test_env_port_with_lease_token(self, monkeypatch):
        # Each half independent: port from env, token from the lease.
        _publish_lease(port=4242, token="lease-token")
        monkeypatch.setenv("NX_SERVICE_PORT", "7000")
        host, port, token = resolve_service_config()
        assert port == 7000
        assert token == "lease-token"

    def test_fail_loud_when_neither(self):
        # No env, no lease (isolated config dir) → RuntimeError, no 8080 default.
        with pytest.raises(RuntimeError, match="not resolvable"):
            resolve_service_config()

    def test_non_integer_port_raises(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_PORT", "not-a-number")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
        with pytest.raises(RuntimeError, match="must be an integer"):
            resolve_service_config()


class TestResolveServiceEndpoint:
    def test_returns_base_url_and_token(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        assert resolve_service_endpoint() == ("http://127.0.0.1:8123", "tok")

    def test_base_url_from_lease(self):
        _publish_lease(port=5555, token="lease-tok")
        base_url, token = resolve_service_endpoint()
        assert base_url == "http://127.0.0.1:5555"
        assert token == "lease-tok"


class TestSchemeAwareEndpoint:
    """RDR-166 nexus-n3bwh — a managed ``https://…:443`` endpoint must survive.

    ``resolve_service_endpoint`` is the ONE base-url authority the T2 stores,
    the catalog client, and the migration pre-gate build their URLs from. When
    ``NX_SERVICE_URL`` names a managed TLS endpoint it MUST be honoured verbatim
    (scheme + host + port), not flattened to ``http://host:port`` — the old
    behaviour turned ``https://api.conexus-nexus.com:443`` into
    ``http://…:443`` and broke every managed-TLS migration leg before the data
    path (already https-capable) ever ran.
    """

    def test_service_url_https_used_verbatim(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "managed-tok")
        assert resolve_service_endpoint() == (
            "https://api.conexus-nexus.com:443",
            "managed-tok",
        )

    def test_service_url_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443/")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "managed-tok")
        base_url, _ = resolve_service_endpoint()
        assert base_url == "https://api.conexus-nexus.com:443"

    def test_service_url_token_falls_back_to_lease(self, monkeypatch):
        # URL from env, token from the lease — each half independent.
        _publish_lease(port=4242, token="lease-token")
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        base_url, token = resolve_service_endpoint()
        assert base_url == "https://api.conexus-nexus.com:443"
        assert token == "lease-token"

    def test_no_service_url_preserves_http_from_env(self, monkeypatch):
        # Regression: the local supervisor path is unchanged (http).
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        assert resolve_service_endpoint() == ("http://127.0.0.1:8123", "tok")


class TestDiscoverLease:
    def test_absent_lease_returns_none(self):
        assert discover_lease() == (None, None)

    def test_present_lease_returns_url_token(self):
        _publish_lease(port=6161, token="dl-token")
        assert discover_lease() == ("http://127.0.0.1:6161", "dl-token")
