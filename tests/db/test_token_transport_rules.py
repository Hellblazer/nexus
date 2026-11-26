# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o step 5: the transport rules for a bearer, on the CLI side.

(a) A bearer is never sent over plain http to a host that is not this machine, on every
    token-carrying path, and one rule (``normalize_origin`` through ``require_secure_transport``)
    decides it everywhere: the host/port leg of ``resolve_service_config``, the mint POST, and the
    stored-credential paths.
(b) A request that carries a bearer over plain http never goes through a proxy, whatever
    ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` name (an https request keeps the proxy: it can
    only tunnel ciphertext). The supervisor lease token is loopback http by its binding, so the
    rule covers it on every client.

Real port-0 ``http.server`` instances stand in for the engine and for the proxy; the proxy must
see nothing.
"""
from __future__ import annotations

import http.server
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from nexus.daemon.service_registry import ServiceRegistry, service_identity
from nexus.db import service_endpoint as se
from nexus.db.service_endpoint import (
    CredentialOriginMismatchError,
    InsecureServiceTransportError,
    resolve_service_config,
    resolve_service_endpoint,
)

_TOKEN = "bearer-for-the-test"
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class _Server:
    """A recording http.server on 127.0.0.1:0 (an engine, or a proxy that answers 502)."""

    def __init__(self, *, proxy: bool = False) -> None:
        outer = self
        self.seen: list[tuple[str, str, str]] = []  # (method, target, Authorization)

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.seen.append((self.command, self.path, self.headers.get("Authorization", "")))
                body = b"{}"
                self.send_response(502 if proxy else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "CONNECT":
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_CONNECT = _answer  # noqa: N815

            def log_message(self, *args: object) -> None:
                pass

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self._srv.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def engine() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.close()


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    """A recording proxy, named by every proxy variable the stacks read."""
    srv = _Server(proxy=True)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in _PROXY_VARS:
        monkeypatch.setenv(name, srv.url)
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_SERVICE_URL", "NX_MINT_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _publish_lease(port: int, token: str = "lease-token") -> None:
    cfg = Path(__import__("os").environ["NEXUS_CONFIG_DIR"])
    cfg.mkdir(parents=True, exist_ok=True)
    ServiceRegistry(dir=cfg, tier="storage_service").publish(
        service_identity(), endpoint={"host": "127.0.0.1", "port": port, "token": token},
        version="test", owner_token="ec37o-transport",
    )


# ── (a) no bearer over plain http off this machine ───────────────────────────────────────────

class TestHostPortLegRefusesCleartextOffMachine:
    @pytest.mark.expects_unresolvable_endpoint
    @pytest.mark.parametrize("host", ["10.0.0.5", "192.0.2.1", "svc.example", "[::2]"])
    def test_an_env_token_is_not_sent_to_a_non_loopback_host(self, monkeypatch, host) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", host.strip("[]"))
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        with pytest.raises(InsecureServiceTransportError) as err:
            resolve_service_config()
        assert "NX_SERVICE_HOST / NX_SERVICE_PORT" in str(err.value)
        assert "9999" not in str(err.value) and host.strip("[]") not in str(err.value)

    @pytest.mark.expects_unresolvable_endpoint
    def test_the_same_refusal_reaches_resolve_service_endpoint(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", "10.0.0.5")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        with pytest.raises(InsecureServiceTransportError):
            resolve_service_endpoint()

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
    def test_loopback_hosts_keep_the_token(self, monkeypatch, host) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", host)
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        assert resolve_service_config() == (host, 9999, "env-token")

    @pytest.mark.expects_unresolvable_endpoint
    def test_a_lease_token_never_pairs_with_a_non_loopback_host(self, monkeypatch) -> None:
        _publish_lease(4242)
        monkeypatch.setenv("NX_SERVICE_HOST", "10.0.0.5")
        with pytest.raises(CredentialOriginMismatchError):
            resolve_service_config()


class TestMintPostUsesTheOneRule:
    """The mint POST and the primitive each judged "is this cleartext off the machine" their own
    way (urlsplit versus ``normalize_origin``), and disagreed on a URL with userinfo. One rule now:
    the primitive's. A URL it cannot normalize, userinfo included, is refused for a bearer; the
    resolvers already refuse such a service URL (nexus-ujbf8)."""

    #: (url, refused): the primitive's verdict, spelled out so a change to either side shows here
    CASES = [
        ("https://engine.example", False), ("http://127.0.0.1:9", False), ("http://localhost:9", False),
        ("http://[::1]:9", False), ("http://engine.example", True), ("http://203.0.113.9:80", True),
        ("http://user:pw@127.0.0.1:9", True), ("https://user:pw@engine.example", True),
        ("http://127.0.0.1\\@evil.example", True), ("http://evil.example\\@127.0.0.1", True),
        ("http://127.0.0.1:80@evil.example", True), ("ftp://127.0.0.1", True),
    ]

    @pytest.mark.parametrize(
        "url",
        [pytest.param(u, marks=pytest.mark.expects_unresolvable_endpoint) if refused else u for u, refused in CASES],
    )
    def test_the_mint_post_and_the_primitive_agree(self, url, tmp_path) -> None:
        expected_refused = dict(self.CASES)[url]
        from nexus.db.data_token import DataTokenManager, DataTokenMintError

        sent: list[str] = []

        def poster(u, headers, body):
            sent.append(headers["Authorization"])
            return 200, {"data_token": "tok", "expires_in_seconds": 600}, {}

        refused_by_rule = False
        try:
            se.require_secure_transport(url)
        except InsecureServiceTransportError:
            refused_by_rule = True
        mgr = DataTokenManager(poster=poster, mint_credential=lambda: "M", config_dir=tmp_path / "cfg")
        try:
            got = mgr.bearer_for(url, "t1")
            minted = True
        except DataTokenMintError as exc:
            got, minted = None, False
            assert "://" not in str(exc) and "pw" not in str(exc)
        assert refused_by_rule is expected_refused, url
        assert minted is (not refused_by_rule), url
        assert (sent == []) is refused_by_rule
        assert got == ("tok" if minted else None)


# ── (b) no proxy for a plain-http bearer ─────────────────────────────────────────────────────

def _store_get(url: str, token: str) -> None:
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    store = HttpMemoryStore(base_url=url, _token=token)
    try:
        store.get(project="p", title="t")
    finally:
        store.close()


def _shared_client(url: str, token: str) -> None:
    from nexus.db.t2._refreshable_client import build_shared_t2_client

    with build_shared_t2_client() as client:
        client.get(f"{url}/x", headers={"Authorization": f"Bearer {token}"})


def _scratch_client(url: str, token: str) -> None:
    from nexus.db.http_scratch_store import HttpScratchStore

    store = HttpScratchStore(base_url=url, _token=token, session_id="ec37o-transport")
    try:
        store._client.get(f"{url}/x", headers={"Authorization": f"Bearer {token}"})
    finally:
        store.close()


def _token_store_client(url: str, token: str) -> None:
    from nexus.db.t2.http_token_store import HttpTokenStore

    with HttpTokenStore(base_url=url, _token=token) as store:
        store._client.get("/x")


def _identity_client(url: str, token: str) -> None:
    from nexus.identity.bind_client import IdentityClient

    IdentityClient(url, token)._http.get(f"{url}/x", headers={"Authorization": f"Bearer {token}"})


def _groups_client(url: str, token: str) -> None:
    from nexus.groups import GroupsClient

    GroupsClient(url, token)._http.get(f"{url}/x", headers={"Authorization": f"Bearer {token}"})


def _vector_client(url: str, token: str) -> None:
    from nexus.db import http_vector_client as hvc

    hvc._request_once("GET", "/v1/x", tenant="default", timeout=5.0, body=None)


def _mint_poster(url: str, token: str) -> None:
    from nexus.db.data_token import _default_poster

    _default_poster(f"{url}/v1/data-tokens/mint", {"Authorization": f"Bearer {token}"}, {"tenant": "t"})


def _engine_status(url: str, token: str) -> None:
    from nexus.db.http_engine_status import fetch_engine_status_at

    fetch_engine_status_at(url, token)


def _cloud_export_client(url: str, token: str) -> None:
    from nexus.backup.cloud_export import ExportClient

    class _Creds:
        def bearer(self) -> str:
            return token

        def rejected(self, sent: str) -> bool:
            return False

    client = ExportClient(url, _Creds())
    client._http.get(f"{url}/x", headers={"Authorization": f"Bearer {token}"})


def _cloud_import_client(url: str, token: str) -> None:
    from nexus.backup.cloud_import import ImportClient

    class _Creds:
        def bearer(self) -> str:
            return token

        def rejected(self, sent: str) -> bool:
            return False

    client = ImportClient(url, _Creds())
    client._http.get(f"{url}/x", headers={"Authorization": f"Bearer {token}"})


CLIENTS: dict[str, Callable[[str, str], None]] = {
    "t2_store": _store_get,
    "shared_t2_client": _shared_client,
    "scratch_store": _scratch_client,
    "token_store": _token_store_client,
    "identity_client": _identity_client,
    "groups_client": _groups_client,
    "vector_client": _vector_client,
    "mint_poster": _mint_poster,
    "engine_status": _engine_status,
    "cloud_export_client": _cloud_export_client,
    "cloud_import_client": _cloud_import_client,
}


@pytest.mark.parametrize("name", sorted(CLIENTS))
def test_a_plain_http_bearer_is_sent_direct_not_through_the_proxy(name, engine, proxy, monkeypatch) -> None:
    # the vector client resolves its own endpoint: hand it the engine and the token by env
    monkeypatch.setenv("NX_SERVICE_URL", engine.url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", _TOKEN)
    from nexus.db import http_vector_client as hvc

    hvc.reset_connection_pool_for_tests()
    try:
        CLIENTS[name](engine.url, _TOKEN)
    except Exception:  # noqa: BLE001 — the stand-in engine answers `{}`; what is asserted is where the request went
        pass
    assert engine.seen, f"{name}: the engine saw no request"
    assert all(auth == f"Bearer {_TOKEN}" for _, _, auth in engine.seen), engine.seen
    assert proxy.seen == [], f"{name}: the proxy saw {proxy.seen}"


def test_the_lease_token_reaches_a_store_without_touching_the_proxy(engine, proxy, monkeypatch) -> None:
    """End to end on the shape the rule exists for: a store that resolves the supervisor lease
    (env names only the lease's own port) carries the lease token, never through a proxy."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    _publish_lease(engine.port, token="the-lease-token")
    monkeypatch.setenv("NX_SERVICE_PORT", str(engine.port))
    store = HttpMemoryStore()
    try:
        store.get(project="p", title="t")
    finally:
        store.close()
    assert engine.seen and {a for _, _, a in engine.seen} == {"Bearer the-lease-token"}, engine.seen
    assert proxy.seen == []


@pytest.mark.parametrize("name", ["t2_store", "shared_t2_client", "vector_client", "mint_poster"])
def test_without_a_proxy_in_the_environment_nothing_changes(name, engine, monkeypatch) -> None:
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NX_SERVICE_URL", engine.url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", _TOKEN)
    from nexus.db import http_vector_client as hvc

    hvc.reset_connection_pool_for_tests()
    try:
        CLIENTS[name](engine.url, _TOKEN)
    except Exception:  # noqa: BLE001
        pass
    assert engine.seen


class TestHttpsKeepsTheProxy:
    """An https request can only be tunnelled by a proxy (CONNECT carries ciphertext), so
    corporate proxy settings keep working for the managed service."""

    def test_an_httpx_client_tunnels_https_through_the_proxy(self, proxy) -> None:
        from nexus.db.t2.shared_ssl import direct_cleartext_mounts, get_shared_ssl_context

        with httpx.Client(verify=get_shared_ssl_context(), mounts=direct_cleartext_mounts()) as client:
            with pytest.raises(httpx.HTTPError):
                client.get("https://127.0.0.1:9/x", headers={"Authorization": f"Bearer {_TOKEN}"})
        assert [m for m, _, _ in proxy.seen] == ["CONNECT"]
        assert all(auth == "" for _, _, auth in proxy.seen), "the bearer is inside the tunnel, never in the CONNECT"

    def test_the_pooled_opener_drops_only_the_http_proxy(self, proxy) -> None:
        import urllib.request

        from nexus.db.pooled_http import build_opener

        opener = build_opener(lambda sock: None)
        handler = next(h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler))
        assert "http" not in handler.proxies and handler.proxies.get("https") == proxy.url

    @pytest.mark.parametrize(
        ("url", "allowed"),
        [("https://api.conexus-nexus.com", True), ("https://10.0.0.5:8443/x", True),
         ("http://127.0.0.1:9", False), ("http://svc.example", False), ("http://u:p@127.0.0.1", False), ("", False)],
    )
    def test_the_rule(self, url, allowed) -> None:
        assert se.bearer_may_use_proxy(url) is allowed


def test_the_mounts_are_empty_when_no_proxy_is_set(monkeypatch) -> None:
    from nexus.db.t2.shared_ssl import direct_cleartext_mounts

    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})
    assert direct_cleartext_mounts() == {}
