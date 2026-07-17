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
    for k in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_SERVICE_URL"):
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

    def test_service_url_wins_over_host_port(self, monkeypatch):
        # All three set: NX_SERVICE_URL is the authoritative full endpoint and
        # takes precedence over NX_SERVICE_HOST/PORT (review M2).
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "managed-tok")
        assert resolve_service_endpoint() == (
            "https://api.conexus-nexus.com:443",
            "managed-tok",
        )

    def test_service_url_set_but_no_token_fails_loud(self, monkeypatch):
        # NX_SERVICE_URL with no token (env or lease) → RuntimeError, not a
        # silent token-less request.
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        with pytest.raises(RuntimeError, match="no service_token is resolvable"):
            resolve_service_endpoint()


class TestConfigYmlFallback:
    """RDR-166 nexus-v3p0x — greenfield managed onboarding ergonomics.

    `nx config set service_url/service_token` persists to config.yml; the
    resolver must CONSUME those (no env, no lease) so a greenfield managed user
    who ran `nx config set` reaches a resolvable endpoint. Env still wins over
    config.yml (get_credential precedence) — pinned below.
    """

    def test_config_yml_service_creds_resolve_when_env_absent(self):
        from nexus.config import set_credential

        # No env, no lease — only config.yml (written via the public surface).
        set_credential("service_url", "https://api.conexus-nexus.com")
        set_credential("service_token", "cfg-token")
        assert resolve_service_endpoint() == (
            "https://api.conexus-nexus.com",
            "cfg-token",
        )

    def test_env_service_url_wins_over_config_yml(self, monkeypatch):
        from nexus.config import set_credential

        set_credential("service_url", "https://config.example:443")
        set_credential("service_token", "cfg-token")
        monkeypatch.setenv("NX_SERVICE_URL", "https://env.example:443")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        assert resolve_service_endpoint() == ("https://env.example:443", "env-token")


class TestRecoverEndpointFromLease:
    """nexus-n3bwh review H1 — lease recovery must never override an explicitly
    pinned NX_SERVICE_URL with a discovered (always-local-http) lease."""

    def test_pinned_service_url_is_never_rebound_to_lease(self, monkeypatch):
        from nexus.db.service_endpoint import recover_endpoint_from_lease

        _publish_lease(port=4242, token="lease-token")
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        # current endpoint is the managed https one; a local lease exists and
        # would compare unequal — but the pin must suppress the rebind.
        assert (
            recover_endpoint_from_lease("https://api.conexus-nexus.com:443") is None
        )

    def test_lease_recovery_still_works_without_service_url(self):
        from nexus.db.service_endpoint import recover_endpoint_from_lease

        _publish_lease(port=4242, token="lease-token")
        # No NX_SERVICE_URL: a stale current endpoint rebinds to the fresh lease.
        assert recover_endpoint_from_lease("http://127.0.0.1:9999") == (
            "http://127.0.0.1:4242",
            "lease-token",
        )

    def test_config_yml_service_url_also_suppresses_rebind(self):
        # nexus-v3p0x: the guard reads service_url via get_credential, so a
        # config.yml-pinned managed endpoint (no env) is ALSO never rebound to a
        # discovered local lease — same protection as the env-pinned case.
        from nexus.config import set_credential
        from nexus.db.service_endpoint import recover_endpoint_from_lease

        _publish_lease(port=4242, token="lease-token")
        set_credential("service_url", "https://api.conexus-nexus.com")
        assert (
            recover_endpoint_from_lease("https://api.conexus-nexus.com") is None
        )


class TestDiscoverLease:
    def test_absent_lease_returns_none(self):
        assert discover_lease() == (None, None)

    def test_present_lease_returns_url_token(self):
        _publish_lease(port=6161, token="dl-token")
        assert discover_lease() == ("http://127.0.0.1:6161", "dl-token")


class TestMigrationHintOnFailure:
    """nexus-0rwwv: the endpoint-resolution failure is the exact wall an
    un-migrated 5.x→6.x install hits — the error must name the remedy when the
    install looks like a pending legacy footprint, and must NOT for
    migrated/fresh installs.

    RDR-185 P4.2 (nexus-n7u38.29): the remedy is now `nx upgrade`. This hint
    SURVIVED the bridge retirement while the `nx upgrade`/`nx doctor` notices
    did not, and the distinction is the point: those two were duplicate
    reports of a state the ladder already reports, whereas this fires on an
    ERROR path where the user is stuck with no walk in flight and the stock
    remedy ("start the supervisor") is actively wrong for them. A genuine
    remedy at a genuine wall — which must therefore name a verb the user can
    actually find. `nx guided-upgrade` is demoted out of --help, so pointing
    at it from an error would be a dead end.
    """

    def test_pending_footprint_names_the_single_trigger(self, monkeypatch, tmp_path):
        # THE vanilla-upgrader state (critique CRITICAL): legacy dir present,
        # NO backend env, NO service evidence — storage-mode left at its real
        # unpatched SERVICE hard default. The hint must still appear.
        monkeypatch.setenv("NX_MIGRATION_NOTICE", "1")
        monkeypatch.setattr(
            "nexus.migration.detection.resolve_default_local_leg",
            lambda: tmp_path,
        )
        with pytest.raises(RuntimeError, match="nx upgrade") as exc:
            resolve_service_config()
        assert "guided-upgrade" not in str(exc.value), (
            "the wall must not send the user to a verb that is hidden from --help"
        )

    def test_no_footprint_keeps_stock_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NX_MIGRATION_NOTICE", "1")
        monkeypatch.setattr(
            "nexus.migration.detection.resolve_default_local_leg",
            lambda: tmp_path / "absent",
        )
        with pytest.raises(RuntimeError) as exc:
            resolve_service_config()
        assert "ONE-TIME storage migration" not in str(exc.value)
