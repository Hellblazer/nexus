# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Endpoint resolution for the RDR-205 ledger projector:
:func:`nexus.hooks.tuple_ledger_project._resolve_base_url`.

This file used to test ``conexus/hooks/scripts/_endpoint_resolve.py``
(nexus-aginu), the plugin's stdlib-only mirror of
``nexus.db.service_endpoint``'s precedence. That mirror was deleted with the
last plugin script that imported it (nexus-z9cz2); its config.yml scanner
and data-token-lease reader went with it, because the wheel calls the
client's own primitives (``nexus.config.get_credential``,
``DataTokenManager.fresh_lease_token``) instead. The bearer policy
(nexus-g2lln) is pinned end to end by ``tests/hooks/test_tuple_ledger_project.py``.

What stays is the one piece the wheel still re-states rather than calls:
the base-URL precedence and its ``is_local_supervisor`` flag.
``TestResolveBaseUrlPrecedence`` pins the individual cases;
``TestResolveBaseUrlParityWithRealClient`` calls the real
``nexus.db.service_endpoint.resolve_service_endpoint`` on the SAME
env/config/lease state and asserts both sides agree, so a precedence change
on either side reds here.

The suite-wide autouse ``_isolate_config_dir`` fixture (``tests/conftest.py``)
redirects ``NEXUS_CONFIG_DIR`` to a fresh ``tmp_path`` per test, so both
resolvers see the same isolated on-disk state.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.hooks.tuple_ledger_project import _resolve_base_url, _Skip


def _config_dir() -> Path:
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _publish_lease(*, host: str = "127.0.0.1", port: int, token: str) -> None:
    from nexus.daemon.service_registry import ServiceRegistry

    reg = ServiceRegistry(dir=_config_dir(), tier="storage_service")
    reg.publish(
        str(os.getuid()),
        endpoint={"host": host, "port": port, "token": token},
        version="test",
        owner_token="aginu-test-owner",
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_SERVICE_URL"):
        monkeypatch.delenv(k, raising=False)
    yield


class TestResolveBaseUrlPrecedence:
    def test_env_host_port_resolves(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        base_url, is_local = _resolve_base_url()
        assert base_url == "http://127.0.0.1:8123"
        assert is_local is False

    def test_base_url_from_lease(self) -> None:
        _publish_lease(port=5555, token="lease-tok")
        base_url, is_local = _resolve_base_url()
        assert base_url == "http://127.0.0.1:5555"
        assert is_local is True

    def test_service_url_https_used_verbatim(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        base_url, is_local = _resolve_base_url()
        assert base_url == "https://api.conexus-nexus.com:443"
        assert is_local is False

    def test_service_url_trailing_slash_stripped(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443/")
        base_url, _ = _resolve_base_url()
        assert base_url == "https://api.conexus-nexus.com:443"

    def test_service_url_wins_over_host_port(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        base_url, _ = _resolve_base_url()
        assert base_url == "https://api.conexus-nexus.com:443"

    def test_config_yml_service_url_resolves_when_env_absent(self) -> None:
        from nexus.config import set_credential

        set_credential("service_url", "https://api.conexus-nexus.com")
        base_url, is_local = _resolve_base_url()
        assert base_url == "https://api.conexus-nexus.com"
        assert is_local is False

    def test_env_service_url_wins_over_config_yml(self, monkeypatch) -> None:
        from nexus.config import set_credential

        set_credential("service_url", "https://config.example:443")
        monkeypatch.setenv("NX_SERVICE_URL", "https://env.example:443")
        base_url, _ = _resolve_base_url()
        assert base_url == "https://env.example:443"

    def test_skip_when_nothing_resolvable(self) -> None:
        with pytest.raises(_Skip, match="no service endpoint resolvable"):
            _resolve_base_url()

    def test_non_integer_port_skips(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "not-a-number")
        with pytest.raises(_Skip, match="not an integer"):
            _resolve_base_url()

    def test_expired_lease_is_absent(self) -> None:
        from nexus.daemon.service_registry import ServiceRegistry

        reg = ServiceRegistry(
            dir=_config_dir(), tier="storage_service", ttl=10.0, clock=lambda: 100.0,
        )
        reg.publish(
            str(os.getuid()),
            endpoint={"host": "127.0.0.1", "port": 4242, "token": "stale"},
            version="test",
            owner_token="aginu-test-owner",
        )
        with pytest.raises(_Skip):
            _resolve_base_url()

    def test_host_port_env_leg_fills_host_from_live_lease(self, monkeypatch) -> None:
        """nexus-aginu: a missing HOST is filled from a live local lease
        before defaulting to 127.0.0.1, matching the real client's
        resolve_service_config."""
        _publish_lease(host="10.0.0.9", port=4242, token="lease-tok")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")  # a DIFFERENT port than the lease
        base_url, is_local = _resolve_base_url()
        assert base_url == "http://10.0.0.9:9999"
        assert is_local is False  # still not the bare-lease leg -- PORT was explicit env

    def test_host_port_env_leg_defaults_host_when_no_lease(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        base_url, is_local = _resolve_base_url()
        assert base_url == "http://127.0.0.1:9999"
        assert is_local is False

    def test_host_port_env_leg_explicit_host_wins_over_lease(self, monkeypatch) -> None:
        _publish_lease(host="10.0.0.9", port=4242, token="lease-tok")
        monkeypatch.setenv("NX_SERVICE_HOST", "192.168.1.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        base_url, _ = _resolve_base_url()
        assert base_url == "http://192.168.1.1:9999"

    def test_host_only_env_leg_fills_port_from_live_lease(self, monkeypatch) -> None:
        """NX_SERVICE_HOST set alone keeps the ENV host and fills PORT from
        the lease, rather than falling through to the pure-lease leg and
        returning the lease's host."""
        _publish_lease(host="10.0.0.9", port=4242, token="lease-tok")
        monkeypatch.setenv("NX_SERVICE_HOST", "192.168.1.1")  # a DIFFERENT host than the lease
        base_url, is_local = _resolve_base_url()
        assert base_url == "http://192.168.1.1:4242"
        assert is_local is False  # still not the bare-lease leg -- HOST was explicit env

    def test_host_only_env_leg_skips_with_no_lease_port(self, monkeypatch) -> None:
        """HOST pinned via env, no PORT resolvable from anywhere: PORT has no
        default, so this is a named skip rather than a guessed port."""
        monkeypatch.setenv("NX_SERVICE_HOST", "192.168.1.1")
        with pytest.raises(_Skip, match="NX_SERVICE_PORT"):
            _resolve_base_url()


class TestResolveBaseUrlParityWithRealClient:
    def _real_base_url(self) -> str:
        from nexus.db.service_endpoint import resolve_service_endpoint

        base_url, _token = resolve_service_endpoint()
        return base_url

    def test_service_url_env(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        hook_url, is_local = _resolve_base_url()
        assert hook_url == self._real_base_url()
        assert is_local is False

    def test_config_yml_service_url(self) -> None:
        from nexus.config import set_credential

        set_credential("service_url", "https://api.conexus-nexus.com")
        set_credential("service_token", "tok")
        hook_url, is_local = _resolve_base_url()
        assert hook_url == self._real_base_url()
        assert is_local is False

    def test_env_host_port(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        hook_url, _ = _resolve_base_url()
        assert hook_url == self._real_base_url()

    def test_host_only_env_fills_port_from_lease(self, monkeypatch) -> None:
        _publish_lease(host="10.0.0.9", port=4242, token="lease-tok")
        monkeypatch.setenv("NX_SERVICE_HOST", "192.168.1.1")
        hook_url, _ = _resolve_base_url()
        assert hook_url == self._real_base_url()

    def test_port_only_env_fills_host_from_lease(self, monkeypatch) -> None:
        _publish_lease(host="10.0.0.9", port=4242, token="lease-tok")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        hook_url, _ = _resolve_base_url()
        assert hook_url == self._real_base_url()

    def test_bare_lease(self) -> None:
        _publish_lease(port=5555, token="lease-tok")
        hook_url, is_local = _resolve_base_url()
        assert hook_url == self._real_base_url()
        assert is_local is True
