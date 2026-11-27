# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o step 2: the CLI's token paths send a stored credential only to
the origin it was issued for.

Each caller is run against a real port-0 ``http.server`` pair: a *real* service
whose origin the credential is bound to, and a *decoy* that an environment URL
(a repository's ``.claude/settings.json`` env) names instead. The decoy must see
no ``Authorization`` header at all, and the bound origin must still get its
token. Callers that make no request themselves are asserted to refuse with
``CredentialOriginMismatchError`` before any request could be built.
"""
from __future__ import annotations

import http.server
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from nexus.db import http_vector_client as hvc
from nexus.db import managed_endpoint as me
from nexus.db import service_endpoint as se
from nexus.db.t2 import _refreshable_client as rc
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin, _resolve_token_only
from nexus.health import _check_static_service_token
from nexus.upgrade_ladder.provisioning import ProvisionResult, establish_verified_service

MANAGED = "https://staging.example.com"


class _Server:
    """A port-0 HTTP server that records the Authorization header of every request."""

    def __init__(self) -> None:
        seen: list[str | None] = []
        self.seen = seen

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                seen.append(self.headers.get("Authorization"))
                payload = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer  # noqa: N815 — stdlib callback names

            def log_message(self, *_a: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def real() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.close()


@pytest.fixture
def decoy() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(se, "discover_lease", lambda: (None, None))
    hvc._invalidate_endpoint()
    yield
    hvc._invalidate_endpoint()


def _save(credentials: dict[str, str]) -> None:
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yml").write_text(yaml.safe_dump({"credentials": credentials}), encoding="utf-8")


def _lease(monkeypatch, url: str, token: str = "lease-tok") -> None:
    monkeypatch.setattr(se, "discover_lease", lambda: (url, token))
    hvc._invalidate_endpoint()


class _Echo(RefreshableHttpStoreMixin):
    def ping(self):
        return self._post("/v1/echo", {})


# ── resolve_service_endpoint, URL leg ─────────────────────────────────────────

@pytest.mark.expects_unresolvable_endpoint
def test_env_url_naming_another_origin_gets_no_saved_token(monkeypatch, decoy):
    _save({"service_url": MANAGED, "service_token": "saved"})
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    with pytest.raises(se.CredentialOriginMismatchError):
        se.resolve_service_endpoint()
    with pytest.raises(se.CredentialOriginMismatchError):
        _Echo()  # the store resolves through the same path; no request is ever built
    assert decoy.seen == []


def test_the_saved_origin_still_gets_its_saved_token_over_real_http(real, decoy):
    _save({"service_url": real.url, "service_token": "saved"})
    assert se.resolve_service_endpoint() == (real.url, "saved")
    assert _Echo().ping() == {"ok": True}
    assert real.seen == ["Bearer saved"] and decoy.seen == []


def test_an_env_url_equal_to_the_saved_origin_keeps_the_saved_token(monkeypatch, real):
    _save({"service_url": real.url, "service_token": "saved"})
    monkeypatch.setenv("NX_SERVICE_URL", real.url + "/")
    assert se.resolve_service_endpoint() == (real.url, "saved")


@pytest.mark.expects_unresolvable_endpoint
def test_env_url_naming_another_origin_gets_no_lease_token(monkeypatch, real, decoy):
    _lease(monkeypatch, real.url)
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    with pytest.raises(se.CredentialOriginMismatchError) as err:
        se.resolve_service_endpoint()
    assert "lease token" in str(err.value) and str(decoy.port) not in str(err.value)
    assert decoy.seen == []


def test_env_url_equal_to_the_lease_origin_gets_the_lease_token(monkeypatch, real):
    _lease(monkeypatch, real.url)
    monkeypatch.setenv("NX_SERVICE_URL", real.url)
    assert se.resolve_service_endpoint() == (real.url, "lease-tok")


def test_an_env_token_is_unbound_and_goes_with_the_env_url(monkeypatch, decoy):
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "from-env")
    assert se.resolve_service_endpoint() == (decoy.url, "from-env")
    assert _Echo().ping() == {"ok": True}
    assert decoy.seen == ["Bearer from-env"]  # the user exported it for this URL themselves


@pytest.mark.expects_unresolvable_endpoint
def test_env_url_with_no_credential_keeps_the_not_resolvable_error(monkeypatch, decoy):
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    with pytest.raises(se.ServiceTokenUnresolvableError) as err:
        se.resolve_service_endpoint()
    assert not isinstance(err.value, se.CredentialOriginMismatchError)
    assert "no service_token is resolvable" in str(err.value)


# ── resolve_service_config, host/port leg ─────────────────────────────────────

@pytest.mark.expects_unresolvable_endpoint
def test_env_host_alone_does_not_get_the_lease_token(monkeypatch, real):
    _lease(monkeypatch, real.url)
    monkeypatch.setenv("NX_SERVICE_HOST", "localhost")  # the lease port, another origin
    with pytest.raises(se.CredentialOriginMismatchError) as err:
        se.resolve_service_config()
    assert "NX_SERVICE_HOST" in str(err.value)
    with pytest.raises(se.CredentialOriginMismatchError):
        se.resolve_service_endpoint()


def test_env_host_equal_to_the_lease_host_gets_the_lease_token(monkeypatch, real):
    _lease(monkeypatch, real.url)
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    assert se.resolve_service_config() == ("127.0.0.1", real.port, "lease-tok")


@pytest.mark.expects_unresolvable_endpoint
def test_env_port_naming_another_port_does_not_get_the_lease_token(monkeypatch, real):
    _lease(monkeypatch, real.url)
    monkeypatch.setenv("NX_SERVICE_PORT", str(real.port + 1))  # a stale or second engine
    with pytest.raises(se.CredentialOriginMismatchError):
        se.resolve_service_config()


def test_lease_alone_resolves_as_before(monkeypatch, real):
    _lease(monkeypatch, real.url)
    assert se.resolve_service_endpoint() == (real.url, "lease-tok")
    assert _Echo().ping() == {"ok": True}
    assert real.seen == ["Bearer lease-tok"]


def test_env_host_port_with_an_env_token_needs_no_lease(monkeypatch, decoy):
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(decoy.port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", "from-env")
    assert se.resolve_service_config() == ("127.0.0.1", decoy.port, "from-env")


@pytest.mark.expects_unresolvable_endpoint
def test_env_host_port_with_nothing_else_stays_not_resolvable(monkeypatch):
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "9")
    with pytest.raises(se.ServiceEndpointUnresolvableError):
        se.resolve_service_config()


# ── T3 vector client ──────────────────────────────────────────────────────────

def _vector_post() -> object:
    return hvc._request_once("POST", "/v1/vectors/search", tenant="default", timeout=10, body={"query": "x"})


@pytest.mark.expects_unresolvable_endpoint
def test_vector_client_sends_no_saved_token_to_an_env_url_naming_another_origin(monkeypatch, decoy):
    _save({"service_url": MANAGED, "service_token": "saved"})
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    with pytest.raises(se.CredentialOriginMismatchError):
        _vector_post()
    assert decoy.seen == []


@pytest.mark.expects_unresolvable_endpoint
def test_vector_client_no_longer_pairs_an_env_url_with_the_lease_token(monkeypatch, real, decoy):
    _lease(monkeypatch, real.url)
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)  # the deliberate "mixed source" pairing is gone
    with pytest.raises(se.CredentialOriginMismatchError):
        _vector_post()
    assert decoy.seen == []


@pytest.mark.expects_unresolvable_endpoint
def test_vector_client_env_host_port_does_not_get_the_lease_token(monkeypatch, real, decoy):
    _lease(monkeypatch, real.url)
    monkeypatch.setenv("NX_SERVICE_HOST", "localhost")
    monkeypatch.setenv("NX_SERVICE_PORT", str(decoy.port))
    with pytest.raises(se.CredentialOriginMismatchError):
        _vector_post()
    assert decoy.seen == []


def test_vector_client_bound_origins_still_get_their_tokens(monkeypatch, real):
    _lease(monkeypatch, real.url)
    assert hvc._resolve_endpoint() == (real.url, "lease-tok")
    monkeypatch.setenv("NX_SERVICE_URL", real.url)
    assert _vector_post() == {"ok": True}
    assert real.seen == ["Bearer lease-tok"]
    _save({"service_url": real.url, "service_token": "saved"})
    assert hvc._resolve_endpoint() == (real.url, "saved")


def test_vector_client_saved_token_over_real_http(real):
    _save({"service_url": real.url, "service_token": "saved"})
    assert _vector_post() == {"ok": True}
    assert real.seen == ["Bearer saved"]


@pytest.mark.expects_unresolvable_endpoint
def test_vector_client_keeps_the_not_resolvable_error_with_no_credential(monkeypatch, decoy):
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    with pytest.raises(se.ServiceEndpointUnresolvableError):
        hvc._resolve_endpoint()


def test_vector_client_env_token_is_unbound(monkeypatch, decoy):
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "from-env")
    assert _vector_post() == {"ok": True}
    assert decoy.seen == ["Bearer from-env"]


# ── explicit-base_url stores (_resolve_token_only) ───────────────────────────

@pytest.mark.expects_unresolvable_endpoint
def test_a_store_given_a_base_url_gets_no_saved_token_for_another_origin(decoy):
    _save({"service_url": MANAGED, "service_token": "saved"})
    with pytest.raises(se.CredentialOriginMismatchError):
        _resolve_token_only(decoy.url)
    with pytest.raises(se.CredentialOriginMismatchError):
        _Echo(base_url=decoy.url)
    assert decoy.seen == []


@pytest.mark.expects_unresolvable_endpoint
def test_a_store_given_a_base_url_gets_no_lease_token_for_another_origin(monkeypatch, real, decoy):
    _lease(monkeypatch, real.url)
    with pytest.raises(se.CredentialOriginMismatchError):
        _Echo(base_url=decoy.url)
    assert decoy.seen == []


def test_a_store_given_the_bound_base_url_still_gets_the_token(monkeypatch, real):
    _save({"service_url": real.url, "service_token": "saved"})
    assert _Echo(base_url=real.url).ping() == {"ok": True}
    assert real.seen == ["Bearer saved"]
    real.seen.clear()
    _lease(monkeypatch, real.url)
    _save({})
    assert _Echo(base_url=real.url).ping() == {"ok": True}
    assert real.seen == ["Bearer lease-tok"]


@pytest.mark.expects_unresolvable_endpoint
def test_a_mismatch_is_final_and_never_waits_for_a_lease(monkeypatch, decoy):
    _save({"service_url": MANAGED, "service_token": "saved"})
    monkeypatch.setattr(se, "_has_ever_resolved_lease", True)  # the evidence gate would otherwise wait
    reads: list[float] = []
    monkeypatch.setattr(se, "discover_lease", lambda: reads.append(1.0) or (None, None))
    monkeypatch.setattr(rc, "discover_lease_with_wait", lambda **_: pytest.fail("a refusal must not wait"))
    with pytest.raises(se.CredentialOriginMismatchError):
        rc._resolve_token_only_with_evidence_gate(decoy.url)
    assert len(reads) == 1  # the lease was read once, to try it, and nothing waited


@pytest.mark.expects_unresolvable_endpoint
def test_a_store_with_only_a_mint_credential_for_another_origin_is_refused(decoy):
    _save({"service_url": MANAGED, "mint_token": "mint-secret"})
    with pytest.raises(se.CredentialOriginMismatchError) as err:
        _resolve_token_only(decoy.url)
    assert "mint_token" in str(err.value)


# ── managed endpoint ─────────────────────────────────────────────────────────

@pytest.mark.expects_unresolvable_endpoint
def test_managed_endpoint_refuses_a_saved_token_for_an_env_url_naming_another_host(monkeypatch, decoy):
    _save({"service_url": MANAGED, "service_token": "saved"})
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    with pytest.raises(se.CredentialOriginMismatchError):
        me.resolve_managed_endpoint()
    # a probe-only caller never sends the token: it gets the env URL and no token
    assert me.resolve_managed_endpoint(require_token=False) == (decoy.url, None)


def test_managed_endpoint_still_returns_the_token_for_its_own_origin():
    _save({"service_url": MANAGED, "service_token": "saved"})
    assert me.resolve_managed_endpoint() == (MANAGED, "saved")


@pytest.mark.expects_unresolvable_endpoint
def test_managed_endpoint_without_a_token_keeps_its_incompatible_error():
    _save({"service_url": MANAGED})
    with pytest.raises(me.ManagedServiceIncompatible):
        me.resolve_managed_endpoint()


# ── doctor whoami probe ───────────────────────────────────────────────────────

@pytest.mark.expects_unresolvable_endpoint
def test_doctor_whoami_probe_sends_no_saved_token_to_an_env_url_naming_another_host(monkeypatch, decoy):
    _save({"service_url": MANAGED, "service_token": "saved"})
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    [result] = _check_static_service_token()
    assert result.ok is False and "not probed" in result.detail
    assert decoy.seen == []


def test_doctor_whoami_probe_still_probes_the_bound_origin(real):
    _save({"service_url": real.url, "service_token": "saved"})
    _check_static_service_token()
    assert real.seen == ["Bearer saved"]


def test_doctor_whoami_probe_with_no_token_stays_not_applicable(decoy, monkeypatch):
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    [result] = _check_static_service_token()
    assert result.ok is True and "no static service_token" in result.detail
    assert decoy.seen == []


# ── upgrade ladder health gate ───────────────────────────────────────────────

@pytest.mark.expects_unresolvable_endpoint
def test_the_upgrade_health_gate_does_not_send_a_saved_token_to_another_https_origin():
    _save({"service_url": MANAGED, "service_token": "saved"})
    prov = ProvisionResult(service_url="https://other.example.com", host="other.example.com", port=443, pid=None, generation=None)

    def _health(**_kw):
        pytest.fail("the health gate must not run with a refused bearer")

    result = establish_verified_service(provision=lambda: prov, health_gate=_health)
    assert result.ready is False and result.service_url is None
    assert "saved service_token" in (result.reason or "")


def test_the_upgrade_health_gate_still_sends_the_bound_token():
    _save({"service_url": MANAGED, "service_token": "saved"})
    prov = ProvisionResult(service_url=MANAGED, host="staging.example.com", port=443, pid=None, generation=None)
    tokens: list[object] = []

    def _health(**kw):
        tokens.append(kw["token"])
        raise _Stop

    with pytest.raises(_Stop):
        establish_verified_service(provision=lambda: prov, health_gate=_health)
    assert tokens == ["saved"]


class _Stop(Exception):
    pass
