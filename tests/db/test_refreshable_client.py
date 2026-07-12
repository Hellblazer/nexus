# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD for ``RefreshableHttpStoreMixin`` (nexus-bikit.2, step 1 of 1 for this bead).

LOCKED design: T2 ``nexus/design-bikit-refreshable-http-store-mixin.md``.
Full plan (audited via ``nx_plan_audit``): T2
``nexus/plan-bikit-refreshable-http-store-mixin.md``.

Problem this mixin fixes (substantive-critic, 2026-07-10): ~9 of the 10 T2
``Http*Store`` classes (``HttpMemoryStore``, ``HttpTaxonomyStore``, etc.) bake
their bearer token into ``self._headers`` / ``httpx.Client(headers=...)`` at
``__init__`` and never rebuild on a 401 or a supervisor-restart port change.
Only ``HttpVectorClient`` (T3) is immune — it resolves the token FRESH per
request off a shared, auto-invalidating lease cache
(``src/nexus/db/http_vector_client.py`` ``_is_retryable_endpoint_error``
194-213, ``_invalidate_endpoint`` 187-191, one-shot retry via
``_once_with_gateway_retry`` 294-347). This mixin ports that SHAPE (not the
urllib taxonomy — T2 stores use ``httpx``) to the T2 stores' shared base.

THIS BEAD DOES NOT IMPLEMENT THE MIXIN. ``nexus.db.t2._refreshable_client``
does not exist yet (that is nexus-bikit.3). Every test below imports it
LAZILY, inside the test body via ``_make_echo_store()`` below, precisely so
each test raises its own independent ``ImportError`` at CALL time rather than
one shared collection error masking all three test identities — the module
itself must still collect cleanly.

Pinned contract these tests lock in for bead .3 to satisfy (see ``docs/``
citations above for why this shape was chosen):

- ``RefreshableHttpStoreMixin.__init__(self, base_url: str | None = None,
  tenant: str = "default", *, _token: str | None = None) -> None`` — mirrors
  the constructor shape shared by 9/10 existing T2 store classes (design
  doc's own survey). When ``base_url``/``_token`` are omitted, resolves via
  ``nexus.db.service_endpoint.resolve_service_endpoint()`` (env-first, no
  caching of its own — confirmed by direct read of that module). NOTE: an
  optional ``timeout`` kwarg is NOT part of this pinned contract — only 1/9
  target classes (``http_aspect_queue``) exposes it publicly; the other 8
  hardcode ``timeout=30.0`` internally. Bead .3's implementer may add it or
  not; these tests do not depend on it either way.
- ``self._post(path: str, payload: dict) -> Any`` and
  ``self._get(path: str, params: dict | None = None) -> Any`` — both return
  parsed JSON on success, and BOTH must go through the SAME shared
  invalidate/retry mechanism (not just ``_post`` — see the read-path test
  below, added specifically because a ``_post``-only fix would still ship
  the read-path 401 bug this mixin exists to close).
- On a 401 OR a connection-refused/reset from either, the mixin invalidates
  its cached endpoint/credential, re-resolves FRESH via
  ``resolve_service_endpoint()``, and retries EXACTLY ONCE. A second failure
  propagates — no retry loops (mirrors
  ``TestSelfHeal.test_persistent_401_retries_exactly_once_then_raises`` in
  ``tests/db/test_t1_cli_dedicated_session.py``). Verified via exact
  ``_REQUEST_COUNT`` assertions on EVERY test below, including the
  connection-refused path — an unbounded retry loop must fail these tests,
  not just "eventually succeed."

Harness pattern generalized from (read-only reference, not edited by this
bead) ``tests/db/test_t1_cli_dedicated_session.py``: module-level mutable
fake-service state (``_FakeHandler`` / ``_free_port`` / threaded
``HTTPServer``), toggled mid-test to simulate rotation / persistent failure /
port churn. That file's session-token model doesn't apply here (the ~9
in-scope T2 stores use a single flat bearer, not a dual session-token
credential), so this harness is deliberately simpler: one mutable
``_VALID_BEARER`` + one ``_ALWAYS_401`` flag, no session-mint endpoint.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest

# ── In-process fake service state (module-level, reset per test) ──────────────

_INITIAL_BEARER = "fake-initial-bearer-token"

_VALID_BEARER: str = _INITIAL_BEARER
_ALWAYS_401: bool = False
#: "METHOD /path" -> inbound request count, INCLUDING requests that 401 —
#: lets tests assert "retried exactly once" by counting round trips, not by
#: guessing at internal retry-loop implementation details.
_REQUEST_COUNT: dict[str, int] = {}


def _reset_fake_service_state() -> None:
    global _VALID_BEARER, _ALWAYS_401
    _VALID_BEARER = _INITIAL_BEARER
    _ALWAYS_401 = False
    _REQUEST_COUNT.clear()


class _FakeHandler(BaseHTTPRequestHandler):
    """Trivial flat-bearer echo service: POST/GET ``/v1/echo``.

    401s whenever ``_ALWAYS_401`` is set OR the ``Authorization`` header does
    not exactly match ``Bearer {_VALID_BEARER}`` — the single lever every
    test in this file drives one way or another.
    """

    def log_message(self, fmt, *args):  # noqa: A002 — matches BaseHTTPRequestHandler signature
        pass  # suppress test noise

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, method: str, path: str) -> None:
        key = f"{method} {path}"
        _REQUEST_COUNT[key] = _REQUEST_COUNT.get(key, 0) + 1

    def _check_bearer(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if _ALWAYS_401 or auth != f"Bearer {_VALID_BEARER}":
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/echo":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self._send(200, {"echo": body})

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("GET", path)
        if path != "/v1/echo":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        self._send(200, {"echo": "get-ok"})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_fake_server() -> tuple[HTTPServer, int]:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _stop_fake_server(server: HTTPServer) -> None:
    """Idempotent-per-instance stop. NEVER call twice on the SAME instance —
    ``HTTPServer.shutdown()`` on an already-stopped ``serve_forever`` loop
    blocks forever waiting for an ack no live loop will ever send. Callers
    that swap in a replacement server (the port-churn test) must repoint
    whatever holder the fixture teardown reads so the fixture's single
    teardown call targets only the CURRENTLY live instance."""
    server.shutdown()
    server.server_close()


class _FakeServerHandle:
    """Mutable holder so a test can swap in a replacement server instance and
    the fixture's teardown still only ever stops the CURRENTLY live one."""

    def __init__(self, server: HTTPServer, port: int) -> None:
        self.server = server
        self.port = port


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Start the fake flat-bearer service; point NX_SERVICE_* env at it.

    Mirrors ``tests/db/test_t1_cli_dedicated_session.py``'s ``fake_service``
    fixture shape but with no session-mint endpoint — this harness is for the
    single-flat-bearer T2 stores, not the T1 dual-credential model.
    """
    _reset_fake_service_state()
    handle = _FakeServerHandle(*_start_fake_server())

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(handle.port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield handle

    _stop_fake_server(handle.server)


def _make_echo_store(**kwargs: Any) -> Any:
    """Lazily import the NOT-YET-BUILT mixin and build a minimal concrete
    store on top of it. Deferred inside this function (not at module import
    time) so each calling test raises its OWN ``ImportError`` rather than one
    shared collection-time failure masking all three tests' identities."""
    from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

    class _EchoStore(RefreshableHttpStoreMixin):
        def echo_post(self, value: str) -> Any:
            return self._post("/v1/echo", {"value": value})

        def echo_get(self) -> Any:
            return self._get("/v1/echo")

    return _EchoStore(**kwargs)


# ── The 3 failing tests ─────────────────────────────────────────────────────


class TestRefreshableClientSelfHeal:
    def test_stale_bearer_selfheals_and_recovers(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sibling process rotates/republishes the bearer token mid-session
        (e.g. a re-minted credential written to the shared config/lease this
        store's peers read). The store's cached/baked header goes stale, but
        the NEXT call on the SAME instance must self-heal — invalidate,
        re-resolve fresh (picking up the rotated env/credential), retry
        once, and succeed transparently. Not a caller-visible error."""
        store = _make_echo_store()

        baseline = store.echo_post("before rotation")
        assert baseline == {"echo": {"value": "before rotation"}}
        assert _REQUEST_COUNT["POST /v1/echo"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-token"
        # The rotated credential is what a sibling process would have
        # re-published; NX_SERVICE_TOKEN is this harness's stand-in for that
        # shared credential source (resolve_service_endpoint() reads it
        # fresh, per service_endpoint.py's own docstring/design).
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.echo_post("after rotation")
        assert result == {"echo": {"value": "after rotation"}}
        # 1 (baseline) + 1 (401 on the now-stale cached header)
        # + 1 (retry with the freshly re-resolved bearer) == 3.
        assert _REQUEST_COUNT["POST /v1/echo"] == 3, (
            "expected exactly one failed attempt (stale header) followed by "
            "one successful retry — not a lucky first-try success and not a "
            "retry loop"
        )

    def test_stale_bearer_selfheals_and_recovers_on_read_path(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The GET-path analog of test_stale_bearer_selfheals_and_recovers
        (substantive-critic Critical finding, nexus-bikit.2 review round 1):
        every prior bearer-rotation assertion in this file ran over _post
        only. A bikit.3 implementation that wires the invalidate/re-resolve/
        retry logic into _post but not _get would pass every other test in
        this file while shipping the exact read-path 401 bug this mixin
        exists to close -- this test closes that gap directly."""
        store = _make_echo_store()

        baseline = store.echo_get()
        assert baseline == {"echo": "get-ok"}
        assert _REQUEST_COUNT["GET /v1/echo"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-token-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.echo_get()
        assert result == {"echo": "get-ok"}
        # 1 (baseline) + 1 (401 on the now-stale cached header)
        # + 1 (retry with the freshly re-resolved bearer) == 3.
        assert _REQUEST_COUNT["GET /v1/echo"] == 3, (
            "expected exactly one failed attempt (stale header) followed by "
            "one successful retry on the READ path — not a lucky first-try "
            "success and not a retry loop"
        )

    def test_persistent_401_retries_exactly_once_then_raises(
        self, fake_service
    ) -> None:
        """A genuinely broken credential (server always 401s regardless of
        the bearer sent) must fail after exactly one retry — not loop
        forever, and not swallow the failure silently."""
        global _ALWAYS_401
        _ALWAYS_401 = True

        store = _make_echo_store()

        with pytest.raises(Exception) as exc_info:  # noqa: PT011 — exception TYPE is bead .3's implementation decision, deliberately unpinned here
            store.echo_post("this will never succeed")

        message = str(exc_info.value).lower()
        assert "401" in message or "unauthorized" in message, (
            f"expected the auth failure to surface in the error, got: {exc_info.value!r}"
        )
        # Exactly one retry attempt (initial + one re-resolve-and-retry) —
        # not an unbounded retry loop.
        assert _REQUEST_COUNT["POST /v1/echo"] == 2

    def test_persistent_connection_refused_retries_exactly_once_then_raises(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The connection-error sibling of
        test_persistent_401_retries_exactly_once_then_raises (substantive-critic
        Significant finding, nexus-f2qvx.1 review round 1): a genuinely,
        permanently unreachable server -- not a port churn with a live
        replacement, nothing listening at all -- must fail after exactly one
        retry attempt, with the connection-class exception propagating to the
        caller. Endpoint is env-resolved (NOT constructor-pinned), matching the
        non-pinned re-resolution path this scenario actually exercises in
        production (a supervisor-restart that never comes back up, e.g. a
        crash-looping service).

        A connection-refused attempt never reaches ANY server (the OS rejects
        the SYN before any handler runs), so unlike the 401 case above,
        _REQUEST_COUNT can't observe the failed attempts server-side. Instead
        this instruments the store's own _request_once directly to prove
        "exactly one initial attempt + one retry, not a loop" -- the same
        non-vacuity bar the 401 test meets via server-side counting.

        The store is constructed AFTER env is repointed at the dead port
        (not before, then repointed) — the mixin caches self._base_url at
        construction time, so a store built against the live fake_service
        port and only repointed via env afterward would keep serving
        successfully off its already-resolved, still-live cached base_url
        and never touch the retry path at all.
        """
        # Start-then-stop yields a definitely-closed port without touching
        # fake_service's own live server, whose teardown must not be
        # double-stopped (see _stop_fake_server's docstring).
        dead_server, dead_port = _start_fake_server()
        _stop_fake_server(dead_server)
        monkeypatch.setenv("NX_SERVICE_PORT", str(dead_port))

        store = _make_echo_store()

        call_count = 0
        original_request_once = store._request_once

        def _counting_request_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return original_request_once(*args, **kwargs)

        store._request_once = _counting_request_once

        with pytest.raises((httpx.ConnectError, ConnectionRefusedError)):
            store.echo_get()

        assert call_count == 2, (
            "expected exactly one initial attempt + one retry against the "
            "re-resolved (still-dead) endpoint, not a retry loop"
        )

    def test_connection_refused_reresolves_baseurl_and_retries_once(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The f2qvx half: after a supervisor-style restart the OLD port is
        dead (connection refused) and a NEW port is live. The store must
        re-resolve the NEW base_url on the next call — not just retry
        against the stale, now-dead port with a fresh header — and succeed."""
        store = _make_echo_store()

        baseline = store.echo_get()
        assert baseline == {"echo": "get-ok"}

        old_port = fake_service.port
        _stop_fake_server(fake_service.server)

        new_server, new_port = _start_fake_server()
        # Guard against the (vanishingly unlikely) case the OS immediately
        # reallocates the same ephemeral port — the test's premise requires
        # a GENUINELY different base_url to be resolved.
        attempts = 0
        while new_port == old_port and attempts < 5:
            _stop_fake_server(new_server)
            new_server, new_port = _start_fake_server()
            attempts += 1
        assert new_port != old_port, "could not allocate a distinct port for the port-churn lever"

        # Repoint the fixture's holder so its single teardown call targets
        # this NEW server, not the already-stopped old one.
        fake_service.server = new_server
        fake_service.port = new_port
        monkeypatch.setenv("NX_SERVICE_PORT", str(new_port))

        result = store.echo_get()
        assert result == {"echo": "get-ok"}
        # 1 (baseline, old port) + 1 (retry, reaching the NEW port). Unlike
        # the 401 case, a connection-refused attempt never reaches ANY
        # server (the OS rejects the SYN before any handler runs), so the
        # failed attempt against the dead old port is not itself recorded —
        # only successful requests are. This assertion was missing entirely
        # pre-fix (substantive-critic Critical finding, review round 1):
        # without it, `result == {"echo": "get-ok"}` alone can't distinguish
        # "genuinely re-resolved and retried once" from a mixin that gave up
        # early and returned stale/cached data, or one that kept retrying
        # the dead old port and got lucky some other way.
        assert _REQUEST_COUNT["GET /v1/echo"] == 2, (
            "expected exactly one successful request before the port churn "
            "and exactly one successful retry against the re-resolved new "
            "port afterward"
        )


class TestPinnedEndpointNeverSilentlyRepointed:
    """substantive-critic Significant finding, nexus-bikit.3 review round 1:
    the constructor's own documented contract ("an explicitly-supplied half
    is never overwritten") was not honored by _invalidate_and_reresolve,
    which unconditionally re-resolved both halves on any retryable failure
    regardless of how the instance was constructed. A fully-pinned instance
    (both base_url and token supplied explicitly, e.g. a test wiring a
    store directly against a fake server) now fails FAST with a clear
    RuntimeError on a retryable failure instead of silently repointing
    itself to whatever resolve_service_endpoint() finds in the ambient
    environment -- which could be a completely different, unrelated
    service the caller never asked for."""

    def test_fully_pinned_instance_raises_clean_error_instead_of_repointing(
        self, fake_service
    ) -> None:
        from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

        class _EchoStore(RefreshableHttpStoreMixin):
            def echo_post(self, value: str) -> Any:
                return self._post("/v1/echo", {"value": value})

        # Pin BOTH halves explicitly to a bearer that is already wrong —
        # simulates a caller that deliberately bypassed env resolution.
        # Even though the AMBIENT environment (NX_SERVICE_TOKEN, set by the
        # fake_service fixture) has the actually-valid bearer, a fully
        # pinned instance must never fall back to it silently.
        store = _EchoStore(
            base_url=f"http://127.0.0.1:{fake_service.port}",
            _token="deliberately-wrong-pinned-bearer",
        )

        with pytest.raises(RuntimeError, match="cannot self-heal"):
            store.echo_post("this must not silently repoint")

    def test_base_url_pinned_token_omitted_selfheals_via_token_only_resolution(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """substantive-critic Critical finding, nexus-bikit.4 review round 1:
        __init__'s base_url-pinned-token-omitted branch was fixed to resolve
        ONLY the token via _resolve_token_only() (not the full
        resolve_service_endpoint(), which wrongly demands host/port also be
        independently resolvable) -- but _invalidate_and_reresolve() was NOT
        updated to match, so the retry path still called full resolution and
        would have failed the moment a base_url-pinned store needed to
        self-heal. This proves the RETRY path specifically, not just
        construction: deletes NX_SERVICE_HOST/NX_SERVICE_PORT from env (so
        full resolve_service_endpoint() would fail loud on the host/port
        side) while base_url stays pinned to the fake server and
        NX_SERVICE_TOKEN stays resolvable -- if _invalidate_and_reresolve()
        regresses back to calling the full resolver, this test fails with a
        host/port-unresolvable RuntimeError instead of a successful retry."""
        from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

        class _EchoStore(RefreshableHttpStoreMixin):
            def echo_post(self, value: str) -> Any:
                return self._post("/v1/echo", {"value": value})

        store = _EchoStore(base_url=f"http://127.0.0.1:{fake_service.port}")

        baseline = store.echo_post("before rotation")
        assert baseline == {"echo": {"value": "before rotation"}}

        # Remove the host/port env the FULL resolver would need -- if the
        # retry path regresses to calling resolve_service_endpoint() instead
        # of _resolve_token_only(), this makes that regression fail loudly
        # rather than silently succeeding for an unrelated reason.
        monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-pinned-base-url"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.echo_post("after rotation, host/port unresolvable")
        assert result == {"echo": {"value": "after rotation, host/port unresolvable"}}
        assert _REQUEST_COUNT["POST /v1/echo"] == 3, (
            "expected exactly one failed attempt (stale header) followed by "
            "one successful retry, self-healed via token-only resolution "
            "despite NX_SERVICE_HOST/PORT being unresolvable"
        )


class TestIsRetryableEndpointErrorClassifier:
    """Direct, deterministic unit tests of ``_is_retryable_endpoint_error``
    (substantive-critic Critical finding, nexus-bikit.3 review round 1): the
    classifier initially handled only ``httpx.ConnectError`` and
    ``httpx.RemoteProtocolError``, silently missing ``httpx.ConnectTimeout``
    (a ``TimeoutException`` subclass, NOT a ``ConnectError`` subclass) and
    ``httpx.ReadError`` (a ``NetworkError`` subclass, NOT a
    ``RemoteProtocolError`` subclass) -- verified via httpx's actual
    exception MRO, not assumed. A live end-to-end network-timeout
    reproduction would be slow/environment-dependent and non-deterministic
    (this project's convention is deterministic tests only), so these test
    the classifier function directly instead -- fast, exact, and precisely
    targets the gap the review found without needing a flaky live socket
    timeout."""

    def test_connect_timeout_is_retryable(self) -> None:
        import httpx

        from nexus.db.t2._refreshable_client import _is_retryable_endpoint_error

        assert _is_retryable_endpoint_error(httpx.ConnectTimeout("timed out"))

    def test_read_error_is_retryable(self) -> None:
        import httpx

        from nexus.db.t2._refreshable_client import _is_retryable_endpoint_error

        assert _is_retryable_endpoint_error(httpx.ReadError("reset mid-read"))

    def test_connect_error_is_retryable(self) -> None:
        import httpx

        from nexus.db.t2._refreshable_client import _is_retryable_endpoint_error

        assert _is_retryable_endpoint_error(httpx.ConnectError("refused"))

    def test_remote_protocol_error_is_retryable(self) -> None:
        import httpx

        from nexus.db.t2._refreshable_client import _is_retryable_endpoint_error

        assert _is_retryable_endpoint_error(httpx.RemoteProtocolError("reset"))

    def test_401_is_retryable(self) -> None:
        import httpx

        from nexus.db.t2._refreshable_client import _is_retryable_endpoint_error

        request = httpx.Request("GET", "http://example.invalid/v1/echo")
        response = httpx.Response(401, request=request)
        exc = httpx.HTTPStatusError("401", request=request, response=response)
        assert _is_retryable_endpoint_error(exc)

    def test_404_is_not_retryable(self) -> None:
        """A genuine client error (not an auth/connection staleness signal)
        must propagate immediately, not trigger a pointless re-resolve+retry."""
        import httpx

        from nexus.db.t2._refreshable_client import _is_retryable_endpoint_error

        request = httpx.Request("GET", "http://example.invalid/v1/echo")
        response = httpx.Response(404, request=request)
        exc = httpx.HTTPStatusError("404", request=request, response=response)
        assert not _is_retryable_endpoint_error(exc)

    def test_500_is_not_retryable(self) -> None:
        import httpx

        from nexus.db.t2._refreshable_client import _is_retryable_endpoint_error

        request = httpx.Request("GET", "http://example.invalid/v1/echo")
        response = httpx.Response(500, request=request)
        exc = httpx.HTTPStatusError("500", request=request, response=response)
        assert not _is_retryable_endpoint_error(exc)
