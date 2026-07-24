# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gbt5u (GH #1419 Issue 3a): engine HTTP sockets carry TCP keepalive.

Steve Harris's indexing run hung SILENTLY for 2+ hours and no client-side
timeout ever fired. The bead's premise was "no client-side timeout on
indexing HTTP calls"; an AST audit of every blocking call site in
``src/nexus`` falsified it — the vector client already bounds every request
(120s default, 600s for upsert-chunks). The timeouts were there and did not
help.

ROOT CAUSE: Python computes socket-timeout deadlines from the MONOTONIC
clock, and on Darwin that is ``mach_absolute_time()`` (asserted below), which
does not advance while the system is asleep. A laptop that sleeps mid-request
therefore burns ~none of its timeout budget: the budget is AWAKE time, not
wall-clock time. On wake the peer is long gone, but the TCP connection is a
zombie and nothing forces the client to notice — no socket in the tree set
``SO_KEEPALIVE``. Bounded timeout, unbounded hang.

FIX: enable TCP keepalive on the engine client's sockets, so a peer that
vanished during sleep is detected by the OS within a bounded interval after
wake and surfaces as an ordinary connection error, which the existing retry
path already handles. Hal decision 2026-07-24.
"""
from __future__ import annotations

import http.server
import socket
import threading

import pytest


def test_darwin_monotonic_clock_is_the_sleep_pausing_one() -> None:
    """The premise of the whole fix, pinned rather than assumed.

    If CPython ever switches Darwin's monotonic clock to the sleep-INCLUSIVE
    ``mach_continuous_time()``, socket timeouts would start firing across a
    sleep on their own and this fix's rationale would need re-examining. That
    is worth being told about rather than discovering by re-investigation.
    """
    import sys
    import time

    if sys.platform != "darwin":
        pytest.skip("Darwin-specific clock assertion")
    impl = time.get_clock_info("monotonic").implementation
    assert impl == "mach_absolute_time()", (
        f"Darwin monotonic clock is now {impl!r}. If this is a sleep-inclusive "
        "clock, socket timeouts now fire across system sleep unaided and "
        "nexus-gbt5u's keepalive rationale should be revisited."
    )


class TestEnableKeepalive:
    def test_sets_so_keepalive_on_a_real_socket(self) -> None:
        from nexus.db.http_vector_client import _enable_tcp_keepalive

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0
            _enable_tcp_keepalive(s)
            # TRUTHY, not == 1: Darwin returns the option BIT from so_options
            # (SO_KEEPALIVE == 0x8, so getsockopt yields 8), while Linux
            # normalizes to 1. Asserting == 1 passes on Linux and fails on the
            # platform the bug was reported from.
            assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0

    def test_sets_the_platform_idle_interval_when_available(self) -> None:
        """Bare SO_KEEPALIVE inherits the OS default idle time, which is 2
        HOURS on both Darwin and Linux — useless against a 2-hour hang. The
        idle knob is the part that actually bounds detection."""
        from nexus.db.http_vector_client import _KEEPALIVE_IDLE_S, _enable_tcp_keepalive

        idle_opt = getattr(socket, "TCP_KEEPALIVE", None) or getattr(
            socket, "TCP_KEEPIDLE", None
        )
        if idle_opt is None:
            pytest.skip("no TCP keepalive idle knob on this platform")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            _enable_tcp_keepalive(s)
            assert s.getsockopt(socket.IPPROTO_TCP, idle_opt) == _KEEPALIVE_IDLE_S
            assert _KEEPALIVE_IDLE_S < 7200, (
                "idle must beat the 2h OS default, or the fix is decorative"
            )

    def test_never_raises_on_a_socket_that_rejects_the_options(self) -> None:
        """A transport that cannot take these options (or a platform without
        them) must degrade to an unkeepalived-but-working connection, never
        break the request. This is a resilience improvement, not a gate."""
        from nexus.db.http_vector_client import _enable_tcp_keepalive

        class _Hostile:
            def setsockopt(self, *_a: object, **_k: object) -> None:
                raise OSError("unsupported")

        _enable_tcp_keepalive(_Hostile())  # must not raise


class TestRequestsUseKeepalivedSockets:
    """End-to-end over a REAL loopback HTTP server (port 0) — the point is
    that the socket the client actually transmits on carries the option, not
    that some helper was called."""

    @pytest.fixture
    def server(self):
        seen: dict[str, object] = {}

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — stdlib callback name
                seen["client_keepalive"] = self.connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_KEEPALIVE
                )
                payload = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_a: object) -> None:
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        yield httpd, seen
        httpd.shutdown()
        httpd.server_close()

    def test_request_once_transmits_on_a_keepalived_socket(
        self, server, monkeypatch,
    ) -> None:
        from nexus.db import http_vector_client as hvc

        httpd, _seen = server
        host, port = httpd.server_address[0], httpd.server_address[1]
        monkeypatch.setattr(
            hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"),
        )

        captured: list[int] = []
        real = hvc._enable_tcp_keepalive

        def _spy(sock):
            real(sock)
            captured.append(
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            )

        monkeypatch.setattr(hvc, "_enable_tcp_keepalive", _spy)

        out = hvc._request_once(
            "GET", "/v1/probe", tenant="default", timeout=10, body=None,
        )

        assert out == {"ok": True}
        assert captured, (
            "the request completed WITHOUT passing through the keepalive "
            "connection class — urlopen is still being used directly"
        )
        assert captured[0] != 0   # truthy, not ==1 — see the note above
