# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-om64x: HTTP stores recover from a stale NX_SERVICE_PORT after a
supervisor restart by re-resolving from the ServiceRegistry lease.

After ``_respawn`` allocates a new port and republishes the lease, a long-lived
MCP process still carries the OLD ``NX_SERVICE_PORT`` in its env — so env-first
``resolve_service_config`` keeps handing back the dead port. A store that
resolved ONCE at construction is stuck on connection-refused until the MCP
restarts (session mint + T1 scratch hard-fail). The fix: on ``httpx.ConnectError``
the store consults the lease DIRECTLY (bypassing the stale env), rebinds, and
retries once.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.daemon.service_registry import ServiceRegistry
from nexus.db import service_endpoint
from nexus.db.service_endpoint import recover_endpoint_from_lease


# ── recover_endpoint_from_lease (the shared primitive) ───────────────────────


class TestRecoverEndpointFromLease:
    def test_returns_new_endpoint_when_lease_differs(self, monkeypatch):
        monkeypatch.setattr(
            service_endpoint, "discover_lease",
            lambda: ("http://127.0.0.1:9999", "fresh-token"),
        )
        # current points at the stale (dead) port
        got = recover_endpoint_from_lease("http://127.0.0.1:8080")
        assert got == ("http://127.0.0.1:9999", "fresh-token")

    def test_none_when_lease_matches_current(self, monkeypatch):
        # lease == current → genuine outage, not a stale port; let it propagate
        monkeypatch.setattr(
            service_endpoint, "discover_lease",
            lambda: ("http://127.0.0.1:8080", "t"),
        )
        assert recover_endpoint_from_lease("http://127.0.0.1:8080") is None

    def test_none_when_no_lease(self, monkeypatch):
        monkeypatch.setattr(service_endpoint, "discover_lease", lambda: (None, None))
        assert recover_endpoint_from_lease("http://127.0.0.1:8080") is None

    def test_trailing_slash_insensitive(self, monkeypatch):
        monkeypatch.setattr(
            service_endpoint, "discover_lease",
            lambda: ("http://127.0.0.1:8080/", "t"),
        )
        # same endpoint modulo trailing slash → no spurious rebind
        assert recover_endpoint_from_lease("http://127.0.0.1:8080") is None


# ── store recovery: ConnectError → rebind → retry once ───────────────────────


class _StubResp:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = body
        self.status_code = status
        self.is_success = 200 <= status < 300
        self.text = str(body)

    def raise_for_status(self) -> None:
        if not self.is_success:  # pragma: no cover
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError(
                "err", request=req, response=httpx.Response(self.status_code, request=req))

    def json(self) -> dict:
        return self._body


class _FlakyClient:
    """Raises *exc* on the first post, then succeeds (the rebound client).

    *exc* lets us exercise both connect-refused (ConnectError) and the TCP-RST
    in-flight class (RemoteProtocolError / ReadError) — nexus-om64x must recover
    from all three (the supervisor SIGTERMs the JVM mid-flight)."""

    def __init__(self, *, raise_first: bool,
                 exc: Exception | None = None) -> None:
        self.raise_first = raise_first
        self.exc = exc or httpx.ConnectError("connection refused")
        self.posts = 0

    def post(self, path, json=None):  # noqa: A002
        self.posts += 1
        if self.raise_first and self.posts == 1:
            raise self.exc
        return _StubResp({"ok": True, "deleted": 0})

    def close(self) -> None:
        pass


class TestTokenStoreRecovery:
    def _store(self, monkeypatch):
        from nexus.db.t2.http_token_store import HttpTokenStore

        s = HttpTokenStore(base_url="http://127.0.0.1:8080", _token="tok")
        # first client raises ConnectError; rebind installs a working stub
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(
            service_endpoint, "discover_lease",
            lambda: ("http://127.0.0.1:9999", "fresh"),
        )
        monkeypatch.setattr(s, "_build_client", lambda: _FlakyClient(raise_first=False))
        return s

    def test_post_rebinds_and_retries_on_connect_error(self, monkeypatch):
        s = self._store(monkeypatch)
        out = s._post("/v1/tenants/create", {"name": "t"})
        assert out == {"ok": True, "deleted": 0}
        assert s._base_url == "http://127.0.0.1:9999"   # rebound to the lease
        assert s._auth_token == "fresh"                 # token refreshed from lease

    def test_post_propagates_when_no_lease(self, monkeypatch):
        """nexus-7dsgp: the store now passes a nonzero wait_budget_s, which
        would poll via REAL time.sleep if discover_lease itself were mocked
        to (None, None) -- mock discover_lease_with_wait (the whole
        poll loop) directly instead so this stays a fast unit test. The
        poll-loop mechanics themselves are covered with a fake clock in
        tests/db/test_service_endpoint_discovery.py::TestRecoverEndpointFromLeaseWait."""
        from nexus.db.t2.http_token_store import HttpTokenStore

        s = HttpTokenStore(base_url="http://127.0.0.1:8080", _token="tok")
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(service_endpoint, "discover_lease_with_wait", lambda **kw: (None, None))
        with pytest.raises(httpx.ConnectError):
            s._post("/v1/tenants/create", {"name": "t"})

    def test_post_passes_bounded_wait_budget_not_stacked(self, monkeypatch):
        """nexus-7dsgp: the retry path must opt into the bounded-wait
        mitigation (a nonzero, FINITE budget — not the pre-fix instant-miss
        default, and not an unbounded/None budget), and must call the
        wait-aware resolver EXACTLY ONCE per _post call (no double-retry
        stacking)."""
        from nexus.db.service_endpoint import DEFAULT_LEASE_WAIT_BUDGET_S
        from nexus.db.t2.http_token_store import HttpTokenStore

        calls: list[float] = []

        def _capture(*, budget_s=0.0, **kw):
            calls.append(budget_s)
            return ("http://127.0.0.1:9999", "fresh")

        s = HttpTokenStore(base_url="http://127.0.0.1:8080", _token="tok")
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(service_endpoint, "discover_lease_with_wait", _capture)
        monkeypatch.setattr(s, "_build_client", lambda: _FlakyClient(raise_first=False))
        s._post("/v1/tenants/create", {"name": "t"})
        assert calls == [DEFAULT_LEASE_WAIT_BUDGET_S]
        assert 0 < DEFAULT_LEASE_WAIT_BUDGET_S < 60  # sane, finite, bounded

    def test_post_recovers_from_mid_gap_lease_flip(self, monkeypatch):
        """The core nexus-7dsgp scenario end-to-end through the store's
        public _post: recover_endpoint_from_lease's mid-gap-flip poll
        behavior (proven with a fake clock — zero real time — against
        recover_endpoint_from_lease directly in
        tests/db/test_service_endpoint_discovery.py::TestRecoverEndpointFromLeaseWait)
        is exercised here at the store boundary by stubbing
        recover_endpoint_from_lease itself: it "misses" once, "waits"
        (no real sleep — this stub IS the wait), then succeeds, mirroring
        exactly what the real poll loop would do after the lease flips."""
        from nexus.db.t2.http_token_store import HttpTokenStore

        calls = {"n": 0}

        def _flaky_recover(current_base_url, *, wait_budget_s=0.0, **kw):
            calls["n"] += 1
            assert wait_budget_s > 0  # store opted into the bounded wait
            return ("http://127.0.0.1:9999", "fresh")

        monkeypatch.setattr(service_endpoint, "recover_endpoint_from_lease", _flaky_recover)
        s = HttpTokenStore(base_url="http://127.0.0.1:8080", _token="tok")
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(s, "_build_client", lambda: _FlakyClient(raise_first=False))
        out = s._post("/v1/tenants/create", {"name": "t"})
        assert out == {"ok": True, "deleted": 0}
        assert s._base_url == "http://127.0.0.1:9999"
        assert calls["n"] == 1  # exactly one rebind attempt -- no double-retry stacking

    @pytest.mark.parametrize("exc", [
        httpx.RemoteProtocolError("server disconnected"),  # TCP RST, in-flight
        httpx.ReadError("socket closed mid-read"),
    ])
    def test_post_recovers_from_tcp_reset_classes(self, monkeypatch, exc):
        from nexus.db.t2.http_token_store import HttpTokenStore

        s = HttpTokenStore(base_url="http://127.0.0.1:8080", _token="tok")
        s._client = _FlakyClient(raise_first=True, exc=exc)
        monkeypatch.setattr(
            service_endpoint, "discover_lease",
            lambda: ("http://127.0.0.1:9999", "fresh"),
        )
        monkeypatch.setattr(s, "_build_client", lambda: _FlakyClient(raise_first=False))
        out = s._post("/v1/tenants/create", {"name": "t"})
        assert out == {"ok": True, "deleted": 0}
        assert s._base_url == "http://127.0.0.1:9999"


class TestScratchStoreRecovery:
    def _store(self, monkeypatch):
        from nexus.db.http_scratch_store import HttpScratchStore

        s = HttpScratchStore(base_url="http://127.0.0.1:8080", _token="tok",
                             session_id="sess-1")
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(
            service_endpoint, "discover_lease",
            lambda: ("http://127.0.0.1:9999", "fresh"),
        )
        monkeypatch.setattr(s, "_build_client", lambda: _FlakyClient(raise_first=False))
        return s

    def test_post_rebinds_and_retries(self, monkeypatch):
        s = self._store(monkeypatch)
        out = s._post("/v1/t1/list", {"session_id": "sess-1"})
        assert out == {"ok": True, "deleted": 0}
        assert s._base_url == "http://127.0.0.1:9999"
        assert s._headers["Authorization"] == "Bearer fresh"

    def test_post_raw_rebinds_and_retries(self, monkeypatch):
        s = self._store(monkeypatch)
        out = s._post_raw("/v1/t1/get", {"id": "x", "session_id": "sess-1"})
        assert out == {"ok": True, "deleted": 0}
        assert s._base_url == "http://127.0.0.1:9999"

    def test_post_propagates_when_no_lease(self, monkeypatch):
        """nexus-7dsgp: see TestTokenStoreRecovery's identical test for why
        discover_lease_with_wait (not discover_lease) is mocked here — the
        store now passes a nonzero wait_budget_s, which would poll via REAL
        time.sleep if only discover_lease were mocked."""
        from nexus.db.http_scratch_store import HttpScratchStore

        s = HttpScratchStore(base_url="http://127.0.0.1:8080", _token="tok",
                             session_id="sess-1")
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(service_endpoint, "discover_lease_with_wait", lambda **kw: (None, None))
        with pytest.raises(RuntimeError):
            s._post("/v1/t1/list", {"session_id": "sess-1"})

    def test_post_raw_propagates_when_no_lease(self, monkeypatch):
        from nexus.db.http_scratch_store import HttpScratchStore

        s = HttpScratchStore(base_url="http://127.0.0.1:8080", _token="tok",
                             session_id="sess-1")
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(service_endpoint, "discover_lease_with_wait", lambda **kw: (None, None))
        with pytest.raises(RuntimeError):
            s._post_raw("/v1/t1/get", {"id": "x", "session_id": "sess-1"})

    def test_post_passes_bounded_wait_budget_not_stacked(self, monkeypatch):
        """nexus-7dsgp: mirrors TestTokenStoreRecovery's equivalent — the
        scratch store must also opt into the bounded-wait mitigation with a
        finite, nonzero budget, called exactly once per _post."""
        from nexus.db.http_scratch_store import HttpScratchStore
        from nexus.db.service_endpoint import DEFAULT_LEASE_WAIT_BUDGET_S

        calls: list[float] = []

        def _capture(*, budget_s=0.0, **kw):
            calls.append(budget_s)
            return ("http://127.0.0.1:9999", "fresh")

        s = HttpScratchStore(base_url="http://127.0.0.1:8080", _token="tok",
                             session_id="sess-1")
        s._client = _FlakyClient(raise_first=True)
        monkeypatch.setattr(service_endpoint, "discover_lease_with_wait", _capture)
        monkeypatch.setattr(s, "_build_client", lambda: _FlakyClient(raise_first=False))
        s._post("/v1/t1/list", {"session_id": "sess-1"})
        assert calls == [DEFAULT_LEASE_WAIT_BUDGET_S]

    @pytest.mark.parametrize("exc", [
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ReadError("socket closed mid-read"),
    ])
    def test_post_recovers_from_tcp_reset(self, monkeypatch, exc):
        from nexus.db.http_scratch_store import HttpScratchStore

        s = HttpScratchStore(base_url="http://127.0.0.1:8080", _token="tok",
                             session_id="sess-1")
        s._client = _FlakyClient(raise_first=True, exc=exc)
        monkeypatch.setattr(
            service_endpoint, "discover_lease",
            lambda: ("http://127.0.0.1:9999", "fresh"),
        )
        monkeypatch.setattr(s, "_build_client", lambda: _FlakyClient(raise_first=False))
        out = s._post("/v1/t1/list", {"session_id": "sess-1"})
        assert out == {"ok": True, "deleted": 0}
        assert s._base_url == "http://127.0.0.1:9999"
