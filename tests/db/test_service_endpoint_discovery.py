# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-pebfx.1 — ServiceRegistry-lease endpoint discovery for HttpVectorClient.

The supervisor (``storage_service_daemon``) publishes ``{host, port, token}``
to the ServiceRegistry lease (``storage_service_addr.<uid>``) after a healthy
``/health``; before this bead the client ignored it and hard-required
``NX_SERVICE_URL`` + ``NX_SERVICE_TOKEN`` env, with a silent hardcoded
``:8080`` fallback. Since the supervisor allocates a NEW free port on every
(re)start, every env-plumbed client broke silently after any auto-restart
(observed live during the 2026-06-10 RDR-155 production migration:
53748 → 54239 → 56915 in one afternoon).

Resolution order pinned here (bead design, RDR-156-adjacent fail-loud
discipline):

1. ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` env — each INDEPENDENTLY
   overrides its half (operator/test override).
2. ServiceRegistry lease — tier="storage_service", scope=str(os.getuid()),
   exactly what the supervisor publishes.
3. FAIL LOUD. The hardcoded ``http://127.0.0.1:8080`` default is retired —
   a silent wrong-port fallback is a correctness hazard, not a convenience.

Re-resolution: on HTTP 401 or a connection-refused class error, the cached
endpoint is invalidated, the lease re-read, and the request retried ONCE —
this is how clients ride through supervisor auto-restarts (new port, same
persisted token, republished lease).

The conftest autouse ``_isolate_config_dir`` fixture redirects
``NEXUS_CONFIG_DIR`` to tmp_path, so no test here can see a real lease.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from nexus.daemon.service_registry import ServiceRegistry


# ── helpers ──────────────────────────────────────────────────────────────────


def _config_dir() -> Path:
    # The autouse _isolate_config_dir fixture sets NEXUS_CONFIG_DIR per test.
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _publish_lease(*, host: str = "127.0.0.1", port: int, token: str) -> None:
    reg = ServiceRegistry(dir=_config_dir(), tier="storage_service")
    reg.publish(
        str(os.getuid()),
        endpoint={"host": host, "port": port, "token": token},
        version="test",
        owner_token="pebfx1-test-owner",
    )


@pytest.fixture(autouse=True)
def _clean_endpoint_state(monkeypatch):
    """Each test starts with no env override and a cold resolver cache."""
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    from nexus.db import http_vector_client as hvc

    hvc._invalidate_endpoint()
    yield
    hvc._invalidate_endpoint()


class _StubHandler(BaseHTTPRequestHandler):
    """Tiny service stub: 200 {"ok": true} when the bearer token matches
    ``server.expected_token``, else 401. Counts requests."""

    def do_POST(self):  # noqa: N802 (http.server API)
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        expected = getattr(self.server, "expected_token", None)
        got = self.headers.get("Authorization", "")
        self.server.request_auths.append(got)  # type: ignore[attr-defined]
        if expected is not None and got != f"Bearer {expected}":
            self.send_response(401)
            body = b'{"error": "bad token"}'
        else:
            self.send_response(200)
            body = b'{"ok": true, "results": [], "upserted": 0}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture()
def stub_server():
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.expected_token = None  # type: ignore[attr-defined]
    srv.request_auths = []  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


# ── resolution order ─────────────────────────────────────────────────────────


class TestResolutionOrder:
    def test_env_overrides_win_without_lease(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:7777")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, token = _resolve_endpoint()
        assert url == "http://127.0.0.1:7777"
        assert token == "env-token"

    def test_lease_resolves_when_env_absent(self):
        _publish_lease(port=4242, token="lease-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, token = _resolve_endpoint()
        assert url == "http://127.0.0.1:4242"
        assert token == "lease-token"

    def test_env_url_with_lease_token(self, monkeypatch):
        """Each half overrides independently: URL from env, token from lease."""
        monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:7777")
        _publish_lease(port=4242, token="lease-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, token = _resolve_endpoint()
        assert url == "http://127.0.0.1:7777"
        assert token == "lease-token"

    def test_env_token_with_lease_url(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        _publish_lease(port=4242, token="lease-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, token = _resolve_endpoint()
        assert url == "http://127.0.0.1:4242"
        assert token == "env-token"

    def test_fail_loud_when_neither_no_8080_fallback(self):
        """No env + no lease = RuntimeError. The legacy silent
        ``http://127.0.0.1:8080`` default must be gone — and the message
        must self-explain every recovery knob (GUI-spawn discipline,
        tests/test_credential_persistence_gui_spawn.py)."""
        from nexus.db.http_vector_client import _resolve_endpoint

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_endpoint()
        msg = str(exc_info.value)
        assert "NX_SERVICE_TOKEN" in msg
        assert "NX_SERVICE_URL" in msg
        assert "nx daemon service start" in msg
        assert "RDR-155" in msg
        assert "8080" not in msg

    def test_expired_lease_is_absent(self):
        """A TTL-expired lease is the same as no lease: fail loud."""
        reg = ServiceRegistry(
            dir=_config_dir(), tier="storage_service",
            ttl=10.0, clock=lambda: 100.0,  # published "in the past"
        )
        reg.publish(
            str(os.getuid()),
            endpoint={"host": "127.0.0.1", "port": 4242, "token": "stale"},
            version="test",
            owner_token="pebfx1-test-owner",
        )
        from nexus.db.http_vector_client import _resolve_endpoint

        with pytest.raises(RuntimeError):
            _resolve_endpoint()


# ── live re-resolution (the port-churn / restart ride-through) ───────────────


class TestReResolution:
    def test_connection_refused_rereads_lease_and_retries(self, stub_server):
        """Lease initially points at a dead port; after the cache primes,
        the supervisor 'restarts' (lease republished at the live port).
        The next request must ride through: refused → invalidate →
        re-resolve → retry → 200."""
        from nexus.db import http_vector_client as hvc

        live_port = stub_server.server_address[1]
        dead_port = _find_dead_port()
        _publish_lease(port=dead_port, token="tok-1")
        url, _ = hvc._resolve_endpoint()
        assert str(dead_port) in url  # cache primed on the dead endpoint

        _publish_lease(port=live_port, token="tok-1")  # "restart"
        result = hvc._post("/v1/vectors/search", {"q": "x"})
        assert result["ok"] is True

    def test_401_rereads_lease_token_and_retries(self, stub_server):
        """Token rotated + republished (HIGH-3: clients re-read it from the
        lease after restart): a 401 with the cached token must trigger one
        re-resolve + retry with the fresh token."""
        from nexus.db import http_vector_client as hvc

        live_port = stub_server.server_address[1]
        stub_server.expected_token = "tok-new"
        _publish_lease(port=live_port, token="tok-old")
        hvc._resolve_endpoint()  # cache primed with tok-old

        _publish_lease(port=live_port, token="tok-new")  # rotation
        result = hvc._post("/v1/vectors/search", {"q": "x"})
        assert result["ok"] is True
        assert stub_server.request_auths == ["Bearer tok-old", "Bearer tok-new"]

    def test_retry_is_single_shot(self):
        """Two dead endpoints in a row = error surfaces after exactly one
        re-resolve; no infinite retry loop."""
        from nexus.db import http_vector_client as hvc

        _publish_lease(port=_find_dead_port(), token="tok")
        with pytest.raises(Exception):
            hvc._post("/v1/vectors/search", {"q": "x"})


def _find_dead_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── catalog client shares the same resolution discipline ─────────────────────


class TestCatalogClientResolution:
    def test_catalog_resolve_config_falls_back_to_lease(self):
        _publish_lease(port=4243, token="lease-token")
        from nexus.catalog.http_catalog_client import _resolve_config

        host, port, token = _resolve_config()
        assert (host, port, token) == ("127.0.0.1", 4243, "lease-token")

    def test_catalog_env_halves_override_individually(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        _publish_lease(port=4243, token="lease-token")
        from nexus.catalog.http_catalog_client import _resolve_config

        host, port, token = _resolve_config()
        assert port == 9999          # env wins
        assert token == "lease-token"  # lease fills the missing half

    def test_catalog_fail_loud_when_neither(self):
        from nexus.catalog.http_catalog_client import _resolve_config

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_config()
        msg = str(exc_info.value)
        assert "nx daemon service start" in msg
        assert "NX_SERVICE_PORT" in msg
