# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o: no urllib opener that carries a bearer follows a redirect.

urllib copies ``Authorization`` to a redirect's target (verified: GET 307, POST 302), so a bound
origin that answers 3xx would hand its credential to any host, and an https origin's to plain
http. Every opener that carries a bearer includes the no-follow handler, so the 3xx surfaces as an
error and the decoy is never dialled. This module drives each opener against a real port-0
server that answers every request with a redirect to a second server (the decoy).

The end-to-end legs, each caller as a real process, are in ``tests/test_ec37o_decoy_suite.py``;
the httpx clients (which follow nothing by default) are pinned by ``tests/test_ec37o_credential_read_lint.py``
and ``test_httpx_bearer_clients_do_not_follow_redirects`` below.
"""
from __future__ import annotations

import http.server
import importlib.util
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
_CODES = (301, 302, 303, 307, 308)


def _no_line_breaks(value: str) -> str:
    """A header value built from request text carries no CR or LF, so it cannot split the response."""
    return value.replace("\r", "").replace("\n", "")


class _Recorder:
    """A port-0 server recording ``(method, path, Authorization)``; with *redirect_to* it answers 3xx there."""

    def __init__(self, redirect_to: str | None = None, code: int = 302) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        self.redirect_to = redirect_to
        self.code = code
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                outer.requests.append((self.command, self.path, self.headers.get("Authorization")))
                if outer.redirect_to is not None:
                    self.send_response(outer.code)
                    self.send_header("Location", outer.redirect_to + _no_line_breaks(self.path))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer  # noqa: N815 -- stdlib callback names

            def log_message(self, *_a: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def auth(self) -> list[str]:
        return [a for _m, _p, a in self.requests if a is not None]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def decoy() -> Iterator[_Recorder]:
    server = _Recorder()
    yield server
    server.close()


@pytest.fixture
def bound(decoy) -> Iterator[_Recorder]:
    """The bound origin: it receives the credential, then redirects to the decoy."""
    server = _Recorder(redirect_to=decoy.url)
    yield server
    server.close()


def _plugin_module(name: str, rel: str) -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    path = _SCRIPTS / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BEARER = {"Authorization": "Bearer SECRET"}


@pytest.mark.parametrize("code", _CODES)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_the_pooled_opener_of_the_vector_client_follows_no_redirect(bound, decoy, code, method) -> None:
    from nexus.db.pooled_http import build_opener  # noqa: PLC0415 -- the unit under test

    bound.code = code
    opener = build_opener(lambda _sock: None)
    req = urllib.request.Request(bound.url + "/v1/x", data=b"{}" if method == "POST" else None, method=method, headers=BEARER)
    with pytest.raises(urllib.error.HTTPError) as err:
        opener.open(req, timeout=10)
    assert err.value.code == code
    assert bound.auth == ["Bearer SECRET"]  # the bound origin is the one that gets it
    assert decoy.requests == []


@pytest.mark.parametrize("code", (302, 307))
def test_the_data_token_mint_poster_follows_no_redirect(bound, decoy, code) -> None:
    from nexus.db.data_token import _default_poster  # noqa: PLC0415 -- the unit under test

    bound.code = code
    status, _body, _headers = _default_poster(bound.url + "/v1/data-tokens/mint", dict(BEARER), {"tenant": "default"})
    assert status == code
    assert decoy.requests == []


@pytest.mark.parametrize("code", (302, 307))
def test_the_cli_mailbox_drain_follows_no_redirect(bound, decoy, code) -> None:
    from nexus.hooks import mailbox_drain as drain  # noqa: PLC0415 -- the unit under test

    bound.code = code
    with pytest.raises(drain._Skip):
        drain._post(bound.url, "SECRET", "/v1/tuples/claim", {"a": 1}, is_local=True)
    assert decoy.requests == []


@pytest.mark.parametrize("code", (302, 307))
def test_the_plugin_mailbox_drain_follows_no_redirect(bound, decoy, code) -> None:
    drain = _plugin_module("_plugin_drain_redirects", "mailbox_drain.py")
    bound.code = code
    with pytest.raises(drain._Skip):
        drain._post(bound.url, "SECRET", "/v1/tuples/claim", {"a": 1}, is_local=True)
    with pytest.raises(drain._Skip):
        drain._get(bound.url, "SECRET", "/v1/tuples/x", {"q": "1"}, is_local=True, budget_s=5.0)
    assert bound.auth == ["Bearer SECRET", "Bearer SECRET"]
    assert decoy.requests == []


@pytest.mark.parametrize("code", (302, 307))
def test_the_routing_poster_follows_no_redirect(bound, decoy, code) -> None:
    lib = _plugin_module("_plugin_routing_lib_redirects", "routing/_lib.py")
    bound.code = code
    cause = lib._post_routing_event_once({"rule": "r"}, bound.url, "SECRET", 10.0)
    assert cause == f"http_{code}"
    assert decoy.requests == []


@pytest.mark.parametrize("code", _CODES)
def test_the_handler_both_copies_provide_refuses_every_redirect(bound, decoy, code) -> None:
    """The factory itself, CLI copy and plugin copy: a bare opener carrying only it refuses the hop."""
    from nexus.db.service_endpoint import no_redirect_handler  # noqa: PLC0415 -- the unit under test

    ep = _plugin_module("_plugin_endpoint_redirects", "_endpoint_resolve.py")
    bound.code = code
    for factory in (no_redirect_handler, ep.no_redirect_handler):
        opener = urllib.request.build_opener(factory())
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(urllib.request.Request(bound.url + "/x", headers=BEARER), timeout=10)
        assert err.value.code == code
    assert decoy.requests == []


def test_an_https_origin_redirecting_to_plain_http_is_not_followed() -> None:
    """https -> http is a hop to a different origin like any other: the handler never builds the follow-up request."""
    from nexus.db.service_endpoint import no_redirect_handler  # noqa: PLC0415 -- the unit under test

    handler = no_redirect_handler()
    req = urllib.request.Request("https://engine.example/v1/x", headers=BEARER)
    assert handler.redirect_request(req, None, 302, "Found", {}, "http://engine.example/v1/x") is None


def test_the_shared_t2_httpx_client_does_not_follow_redirects(bound, decoy) -> None:
    """httpx follows nothing by default. The shared client every T2 store rides on is built as the stores
    build it, and a 3xx from the bound origin never reaches the decoy. (The stores that build their own
    client, and the rest of the bearer clients, are pinned by the follow_redirects lint in
    ``tests/test_ec37o_credential_read_lint.py``.)"""
    from nexus.db.t2._refreshable_client import build_shared_t2_client  # noqa: PLC0415 -- the factory the stores use

    client = build_shared_t2_client(timeout=5.0)
    try:
        assert client.follow_redirects is False
        resp = client.get(bound.url + "/v1/x", headers=BEARER)
        assert resp.status_code == 302
    finally:
        client.close()
    assert bound.auth == ["Bearer SECRET"]
    assert decoy.requests == []


def test_control_a_stock_opener_would_have_carried_the_bearer_to_the_decoy(bound, decoy) -> None:
    """Non-vacuity: the server really redirects, and urllib's stock opener really forwards Authorization,
    so the absences asserted above are the handler's doing and not a harness that never redirected."""
    bound.code = 307
    req = urllib.request.Request(bound.url + "/v1/x", headers=BEARER)
    try:
        urllib.request.build_opener().open(req, timeout=10).close()
    except urllib.error.HTTPError:
        pass
    if not decoy.requests:
        pytest.skip("this Python no longer follows the redirect on its own")
    assert decoy.auth == ["Bearer SECRET"]
