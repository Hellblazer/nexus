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
    """Each test starts with no env override and a cold resolver cache.

    NX_SERVICE_HOST/PORT are scrubbed too (nexus-edwlp): the resolver now
    honors the host/port env halves, and the local-service gate exports
    them for the whole integration run — without the scrub, the lease and
    fail-loud tests below would resolve the gate's service instead.
    """
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
    monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
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

    def test_env_host_port_resolve_without_url(self, monkeypatch):
        """nexus-edwlp: the T3 resolver honors the NX_SERVICE_HOST/PORT env
        halves (T2-parity — resolve_service_config has always read them).
        Before this, a box with HOST/PORT/TOKEN exported but no NX_SERVICE_URL
        and no visible lease failed loud on the vector path while every T2
        store resolved fine — the exact split behind the local-service gate's
        9 T3 round-trip failures."""
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "7171")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, token = _resolve_endpoint()
        assert url == "http://127.0.0.1:7171"
        assert token == "env-token"

    def test_env_port_only_defaults_host(self, monkeypatch):
        """Host defaults to 127.0.0.1, mirroring resolve_service_config."""
        monkeypatch.setenv("NX_SERVICE_PORT", "7172")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, _ = _resolve_endpoint()
        assert url == "http://127.0.0.1:7172"

    def test_env_url_outranks_host_port(self, monkeypatch):
        """NX_SERVICE_URL stays the authoritative full endpoint."""
        monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:7777")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "7171")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, _ = _resolve_endpoint()
        assert url == "http://127.0.0.1:7777"

    def test_env_host_port_outranks_lease(self, monkeypatch):
        """Env halves win over the lease — the documented T2 trade-off
        (service_endpoint.py module doc: env is read first; a stale env
        beats a fresh lease and the 401/refused retry is the corrective)."""
        monkeypatch.setenv("NX_SERVICE_PORT", "7173")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
        _publish_lease(port=4242, token="lease-token")
        from nexus.db.http_vector_client import _resolve_endpoint

        url, token = _resolve_endpoint()
        assert url == "http://127.0.0.1:7173"
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


class TestLeaseGapReresolveRetryVectorClient:
    """nexus-7dsgp (GH #1405 defect 1): the T3 vector client's bounded-wait
    mitigation, SCOPED to connection-class errors against an ALREADY
    lease-resolved endpoint only.

    A bare "nexus-service endpoint is not resolvable" RuntimeError on the
    FIRST attempt (no prior successful resolution, no connection-refused
    precursor) is DELIBERATELY excluded from the wait/retry — test-driven
    reversal (nexus-1091's aspect-worker drain integration suite): an
    earlier version of this fix ALSO caught that RuntimeError, which meant
    ANY cold-start caller with no supervisor running at all (every unit
    test touching T3 without a fake service, and any genuinely-
    unconfigured production install) silently paid the full 12s wait
    before its immediate fail-loud — a real regression, not a fix. See
    ``_request``'s docstring in http_vector_client.py for the full
    rationale. The tests below pin BOTH halves: the connection-class path
    (real evidence of a working-then-lost lease) DOES wait; the bare
    RuntimeError path does NOT."""

    def test_cold_start_unresolvable_fails_loud_immediately_no_wait(self, monkeypatch):
        """No lease ever published, no prior successful resolution — the
        exact shape of a unit test or a genuinely-never-configured
        install. Must fail loud on the FIRST attempt with ZERO wait and
        ZERO retry (this is the regression this bead must NOT introduce)."""
        from nexus.db import http_vector_client as hvc
        from nexus.db import service_endpoint as se

        hvc._invalidate_endpoint()

        def _poison_dlw(**kw):
            raise AssertionError(
                "discover_lease_with_wait must never be reached for a "
                "bare cold-start RuntimeError -- that would reintroduce "
                "the 12s-stall regression nexus-1091 caught"
            )

        monkeypatch.setattr(se, "discover_lease_with_wait", _poison_dlw)

        with pytest.raises(RuntimeError, match="not resolvable"):
            hvc._post("/v1/vectors/search", {"q": "x"})

    def test_connect_refused_against_cached_lease_then_recovers(self, monkeypatch, stub_server):
        """The OTHER half of the bug: ``_lease_cache`` is warm (a prior
        call succeeded), the supervisor respawns, the cached endpoint now
        connect-refuses, AND the new lease has not published yet. Must
        retry, wait, and recover once it appears — proven with a fake
        clock injected into ``discover_lease_with_wait`` so this stays a
        zero-real-sleep unit test, matching
        ``test_never_republishes_fails_loud_after_bounded_wait`` above."""
        from nexus.db import http_vector_client as hvc
        from nexus.db import service_endpoint as se

        dead_port = _find_dead_port()
        _publish_lease(port=dead_port, token="tok-stale")
        hvc._resolve_endpoint()  # warms _lease_cache on the (soon-to-be-dead) port

        live_port = stub_server.server_address[1]
        real_discover = se.discover_lease
        calls = {"n": 0}

        def _flaky_discover():
            calls["n"] += 1
            if calls["n"] < 2:
                return (None, None)
            _publish_lease(port=live_port, token="tok-recovered")
            return real_discover()

        monkeypatch.setattr(se, "discover_lease", _flaky_discover)

        fc_now = {"t": 0.0}

        def _fake_sleep(s: float) -> None:
            fc_now["t"] += s

        def _fake_clock() -> float:
            return fc_now["t"]

        real_dlw = se.discover_lease_with_wait

        def _dlw_with_fake_clock(**kw):
            kw["clock"] = _fake_clock
            kw["sleep"] = _fake_sleep
            return real_dlw(**kw)

        monkeypatch.setattr(se, "discover_lease_with_wait", _dlw_with_fake_clock)

        result = hvc._post("/v1/vectors/search", {"q": "x"})
        assert result["ok"] is True
        assert calls["n"] >= 2  # missed at least once before the flip landed

    def test_managed_cloud_never_retries_or_waits(self, monkeypatch):
        """The bead's core exclusion, at the vector client: a managed
        NX_SERVICE_URL retries its connection exactly once on a
        connection-class error (pre-existing dual-review H1 behavior) but
        the lease-wait must NEVER fire for it — proven via a poison
        discover_lease AND a poison discover_lease_with_wait that both
        fail the test if reached."""
        from nexus.db import http_vector_client as hvc
        from nexus.db import service_endpoint as se

        hvc._invalidate_endpoint()
        monkeypatch.setenv("NX_SERVICE_URL", "https://managed.invalid.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "managed-tok")

        def _poison_discover():
            raise AssertionError("discover_lease must never be reached for managed-cloud")

        def _poison_dlw(**kw):
            raise AssertionError("discover_lease_with_wait must never be reached for managed-cloud")

        monkeypatch.setattr(se, "discover_lease", _poison_discover)
        monkeypatch.setattr(se, "discover_lease_with_wait", _poison_dlw)

        # A connection to a nonexistent managed host fails with a
        # urllib.error.URLError (DNS/connect failure) — the pre-existing
        # connection-class retry still applies (dual-review H1), but must
        # never touch lease discovery. Bounded by urllib's own timeout, so
        # this is real network-stack latency, not this bead's wait budget.
        with pytest.raises(Exception):  # noqa: PT011 — network-class exception, not pinned here
            hvc._post("/v1/vectors/search", {"q": "x"}, timeout=1)

    def test_env_host_port_pinned_never_waits(self, monkeypatch):
        """Code-review round 1, Medium: an env-pinned NX_SERVICE_HOST/PORT
        deployment retries its connection exactly once (pre-existing
        behavior — unlike service_url, THIS leg's lease is discoverable,
        but _resolve_endpoint's ``url = url or lease_url`` precedence
        means an env-pinned url is NEVER overridden by a freshly
        discovered lease) — so waiting for the lease is pure latency for
        zero possible recovery. Proven via a poison discover_lease_with_wait
        that only fails the test on a NONZERO budget (the wait attempt);
        the normal budget_s=0.0 first-attempt read is allowed through."""
        from nexus.db import http_vector_client as hvc
        from nexus.db import service_endpoint as se

        hvc._invalidate_endpoint()
        dead_port = _find_dead_port()
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(dead_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", "env-pinned-tok")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)

        real_dlw = se.discover_lease_with_wait

        def _poison_nonzero_budget(*, budget_s: float = 0.0, **kw):
            if budget_s > 0:
                raise AssertionError(
                    "discover_lease_with_wait must never be called with a "
                    "nonzero budget for an env-host-port-pinned endpoint "
                    "-- the lease can never override the pinned url"
                )
            return real_dlw(budget_s=budget_s, **kw)

        monkeypatch.setattr(se, "discover_lease_with_wait", _poison_nonzero_budget)

        # A lease published elsewhere must also be structurally ignored --
        # env host/port always outranks it (nexus-edwlp, pre-existing).
        _publish_lease(port=dead_port + 1, token="lease-would-be-ignored")

        with pytest.raises(Exception):  # noqa: PT011 — connection-class exception, not pinned here
            hvc._post("/v1/vectors/search", {"q": "x"}, timeout=1)


def _find_dead_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── catalog client shares the same resolution discipline ─────────────────────


class TestCatalogClientResolution:
    def test_catalog_resolve_config_falls_back_to_lease(self):
        _publish_lease(port=4243, token="lease-token")
        from nexus.db.service_endpoint import resolve_service_config as _resolve_config

        host, port, token = _resolve_config()
        assert (host, port, token) == ("127.0.0.1", 4243, "lease-token")

    def test_catalog_env_halves_override_individually(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        _publish_lease(port=4243, token="lease-token")
        from nexus.db.service_endpoint import resolve_service_config as _resolve_config

        host, port, token = _resolve_config()
        assert port == 9999          # env wins
        assert token == "lease-token"  # lease fills the missing half

    def test_catalog_fail_loud_when_neither(self):
        from nexus.db.service_endpoint import resolve_service_config as _resolve_config

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_config()
        msg = str(exc_info.value)
        assert "nx daemon service start" in msg
        assert "NX_SERVICE_PORT" in msg


# ── dual-review fixes (S1, S2) + AUDIT closure (make_t3 serving path) ────────


class _OneShotResetHandler(_StubHandler):
    """First request: abrupt close (TCP RST / RemoteDisconnected — the
    mid-flight supervisor-SIGTERM signature). Subsequent requests: normal."""

    def do_POST(self):  # noqa: N802
        if not getattr(self.server, "reset_done", False):
            self.server.reset_done = True  # type: ignore[attr-defined]
            self.connection.close()
            return
        super().do_POST()


class TestDualReviewFixes:
    def test_lease_miss_is_not_cached_supervisor_starts_later(self):
        """S1: a client that resolves BEFORE the supervisor publishes its
        lease must pick the lease up on a later call without a process
        restart — a (None, None) miss must never stick."""
        from nexus.db import http_vector_client as hvc

        with pytest.raises(RuntimeError):
            hvc._resolve_endpoint()  # supervisor not up yet

        _publish_lease(port=4242, token="late-token")  # supervisor arrives
        url, token = hvc._resolve_endpoint()
        assert url == "http://127.0.0.1:4242"
        assert token == "late-token"

    def test_midflight_reset_retries(self):
        """S2: an in-flight request at supervisor-restart time gets a TCP
        RST (RemoteDisconnected), not a refusal — it must re-resolve and
        retry once, same as refused."""
        from nexus.db import http_vector_client as hvc

        srv = HTTPServer(("127.0.0.1", 0), _OneShotResetHandler)
        srv.expected_token = None  # type: ignore[attr-defined]
        srv.request_auths = []  # type: ignore[attr-defined]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            _publish_lease(port=srv.server_address[1], token="tok")
            result = hvc._post("/v1/vectors/search", {"q": "x"})
            assert result["ok"] is True
        finally:
            srv.shutdown()


# ── nexus-7dsgp: bounded-wait retry for the supervisor-respawn gap ───────────


class _FakeClock:
    """Deterministic monotonic clock + no-op sleep that ADVANCES the clock
    by the requested duration instead of actually blocking (nexus-7dsgp:
    "no real sleeps in unit tests, no blind-sleeps"). Records every sleep
    call so tests can assert the poll count / cadence."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestDiscoverLeaseWithWait:
    def test_zero_budget_is_single_immediate_call_no_sleep(self):
        """budget_s=0.0 (the default) must be IDENTICAL to a bare
        discover_lease() call: no polling, no sleep, regardless of whether
        a lease is present."""
        from nexus.db.service_endpoint import discover_lease_with_wait

        fc = _FakeClock()
        result = discover_lease_with_wait(clock=fc.clock, sleep=fc.sleep)
        assert result == (None, None)
        assert fc.sleeps == []
        assert fc.now == 0.0

    def test_lease_present_on_first_read_no_wait(self):
        _publish_lease(port=5551, token="tok-immediate")
        from nexus.db.service_endpoint import discover_lease_with_wait

        fc = _FakeClock()
        result = discover_lease_with_wait(budget_s=12.0, clock=fc.clock, sleep=fc.sleep)
        assert result == ("http://127.0.0.1:5551", "tok-immediate")
        assert fc.sleeps == []  # found on the first read -- never polled

    def test_lease_appears_mid_wait_is_picked_up(self, monkeypatch):
        """The mid-gap flip: discover_lease() misses on the first N reads,
        then a lease appears -- discover_lease_with_wait must return it
        WITHOUT exhausting the full budget (verified via the fake clock:
        elapsed time is less than budget_s)."""
        from nexus.db import service_endpoint as se

        calls = {"n": 0}
        real_discover = se.discover_lease

        def _flaky_discover():
            calls["n"] += 1
            if calls["n"] < 3:
                return (None, None)
            _publish_lease(port=5552, token="tok-flip")
            return real_discover()

        monkeypatch.setattr(se, "discover_lease", _flaky_discover)

        fc = _FakeClock()
        result = se.discover_lease_with_wait(
            budget_s=12.0, poll_interval_s=0.5, clock=fc.clock, sleep=fc.sleep
        )
        assert result == ("http://127.0.0.1:5552", "tok-flip")
        assert calls["n"] == 3
        assert fc.now < 12.0  # returned before exhausting the budget
        assert len(fc.sleeps) == 2  # two misses -> two polls before the hit

    def test_lease_never_appears_exhausts_budget_bounded(self, monkeypatch):
        """No lease ever -- must return (None, None) after roughly budget_s
        of SIMULATED time (fake clock — zero real wall-clock), with a
        BOUNDED poll count (never an infinite loop)."""
        from nexus.db.service_endpoint import discover_lease_with_wait

        fc = _FakeClock()
        result = discover_lease_with_wait(
            budget_s=12.0, poll_interval_s=0.5, clock=fc.clock, sleep=fc.sleep
        )
        assert result == (None, None)
        assert fc.now >= 12.0
        # 12.0 / 0.5 = 24 polls, bounded -- not unbounded.
        assert len(fc.sleeps) == 24

    def test_managed_cloud_never_reaches_this_wait(self):
        """Structural proof of the bead's "never for the managed-cloud URL
        path" contract: recover_endpoint_from_lease already early-returns
        for service_url before ever calling discover_lease_with_wait --
        covered directly in TestRecoverEndpointFromLeaseWait below."""


class TestRecoverEndpointFromLeaseWait:
    def test_default_wait_budget_zero_preserves_instant_miss(self, monkeypatch):
        """Existing (pre-nexus-7dsgp) callers that don't pass wait_budget_s
        must see EXACTLY the old instant-miss behavior -- no new latency
        introduced for callers that haven't opted in."""
        from nexus.db import service_endpoint

        monkeypatch.setattr(service_endpoint, "discover_lease", lambda: (None, None))
        assert service_endpoint.recover_endpoint_from_lease("http://127.0.0.1:8080") is None

    def test_wait_budget_recovers_from_mid_gap_flip(self, monkeypatch):
        from nexus.db import service_endpoint

        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                return (None, None)
            return ("http://127.0.0.1:9999", "fresh-token")

        monkeypatch.setattr(service_endpoint, "discover_lease", _flaky)
        fc = _FakeClock()
        got = service_endpoint.recover_endpoint_from_lease(
            "http://127.0.0.1:8080", wait_budget_s=12.0, clock=fc.clock, sleep=fc.sleep
        )
        assert got == ("http://127.0.0.1:9999", "fresh-token")
        assert fc.now < 12.0

    def test_wait_budget_exhausted_returns_none_bounded(self, monkeypatch):
        from nexus.db import service_endpoint

        monkeypatch.setattr(service_endpoint, "discover_lease", lambda: (None, None))
        fc = _FakeClock()
        got = service_endpoint.recover_endpoint_from_lease(
            "http://127.0.0.1:8080", wait_budget_s=12.0, poll_interval_s=0.5,
            clock=fc.clock, sleep=fc.sleep,
        )
        assert got is None
        assert fc.now >= 12.0
        assert len(fc.sleeps) == 24  # bounded, matches 12.0/0.5

    def test_managed_cloud_never_waits(self, monkeypatch):
        """The bead's core exclusion: when service_url is configured, the
        wait must never even be attempted -- discover_lease is never
        called at all (proven via a poison lambda that raises if invoked)."""
        from nexus.db import service_endpoint

        monkeypatch.setenv("NX_SERVICE_URL", "https://managed.example.com")

        def _poison():
            raise AssertionError("discover_lease must not be called for the managed-cloud path")

        monkeypatch.setattr(service_endpoint, "discover_lease", _poison)
        fc = _FakeClock()
        got = service_endpoint.recover_endpoint_from_lease(
            "https://managed.example.com", wait_budget_s=12.0, clock=fc.clock, sleep=fc.sleep
        )
        assert got is None
        assert fc.sleeps == []


class TestResolveServiceConfigWait:
    def test_zero_wait_budget_fails_loud_instantly(self):
        from nexus.db.service_endpoint import resolve_service_config

        fc = _FakeClock()
        with pytest.raises(RuntimeError):
            resolve_service_config(clock=fc.clock, sleep=fc.sleep)
        assert fc.sleeps == []

    def test_nonzero_wait_budget_recovers_from_mid_gap_flip(self, monkeypatch):
        from nexus.db import service_endpoint

        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                return (None, None)
            return ("http://127.0.0.1:6161", "tok-recovered")

        monkeypatch.setattr(service_endpoint, "discover_lease", _flaky)
        fc = _FakeClock()
        host, port, token = service_endpoint.resolve_service_config(
            wait_budget_s=12.0, clock=fc.clock, sleep=fc.sleep
        )
        assert (host, port, token) == ("127.0.0.1", 6161, "tok-recovered")
        assert fc.now < 12.0


class TestResolveServiceEndpointWait:
    def test_managed_leg_never_applies_wait_budget(self, monkeypatch):
        """service_url configured -- wait_budget_s must be structurally
        inert: the managed leg returns before resolve_service_config (the
        only wait-aware function) is ever reached, proven by a poisoned
        discover_lease that would raise if the wait path were reached."""
        from nexus.db import service_endpoint

        monkeypatch.setenv("NX_SERVICE_URL", "https://managed.example.com")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "managed-tok")
        fc = _FakeClock()

        def _poison():
            raise AssertionError("discover_lease must not be reached: token is env-resolved")

        monkeypatch.setattr(service_endpoint, "discover_lease", _poison)
        url, token = service_endpoint.resolve_service_endpoint(
            wait_budget_s=12.0, clock=fc.clock, sleep=fc.sleep
        )
        assert url == "https://managed.example.com"
        assert token == "managed-tok"
        assert fc.sleeps == []

    def test_local_leg_honors_wait_budget(self, monkeypatch):
        from nexus.db import service_endpoint

        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                return (None, None)
            return ("http://127.0.0.1:6262", "tok-local")

        monkeypatch.setattr(service_endpoint, "discover_lease", _flaky)
        fc = _FakeClock()
        url, token = service_endpoint.resolve_service_endpoint(
            wait_budget_s=12.0, clock=fc.clock, sleep=fc.sleep
        )
        assert url == "http://127.0.0.1:6262"
        assert token == "tok-local"
        assert fc.now < 12.0


class TestServingPathAuditClosure:
    def test_make_t3_serves_via_lease_with_no_env(self, stub_server):
        """The bead's AUDIT clause: the post-P4a serving path must work
        out-of-box with NO env set when the supervisor lease exists —
        ``make_t3()`` → ``HttpVectorClient`` → request resolves via the
        lease (this was exactly the broken-for-installed-users scenario)."""
        from nexus.db import make_t3

        _publish_lease(port=stub_server.server_address[1], token="lease-tok")
        t3 = make_t3()
        t3.search("anything", ["knowledge__x__minilm-l6-v2-384__v1"])
        # The proof: the request reached the stub authenticated with the
        # LEASE token, with zero env plumbing.
        assert stub_server.request_auths == ["Bearer lease-tok"]
