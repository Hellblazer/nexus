# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-222 Phase 1 (nexus-yye8l.11): the vector client's request-key plumbing.

The key reaches ``_request_once`` through a thread-local set immediately before each POST
attempt and cleared right after it (nexus-zxgld gap 6), never through a new parameter on
``_request_once`` or ``_post`` (222-research-11). These tests drive the real transport against a
scripted loopback server and read what went over the wire.
"""
from __future__ import annotations

import threading
import uuid

import pytest

import nexus.db.http_vector_client as hv
from nexus.db import pooled_http  # noqa: F401  (loaded so the pool reset below exists)
from tests._module_seam import module_time
from tests.db import _scripted_vector_server as sv

_SWEEP = "/v1/vectors/gc/quarantine-orphans"
_UPSERT = "/v1/vectors/upsert-chunks"
_KEY_HEADER = "idempotency-key"


@pytest.fixture
def srv(monkeypatch):
    server = sv.start()
    hv.reset_connection_pool_for_tests()
    monkeypatch.setattr(hv, "_resolve_endpoint", lambda: (server.url, "tok"))
    monkeypatch.setattr(
        "nexus.db.service_endpoint.guard_production_write", lambda base_url: None,
    )
    yield server
    hv.reset_connection_pool_for_tests()
    sv.stop(server)


def _post(path: str = _SWEEP, body: dict | None = None):
    return hv._post(path, body or {"collection": "c"}, tenant="default")


def test_a_keyed_attempt_sends_idempotency_key_on_every_post_attempt(srv, monkeypatch):
    """One key, every attempt: the gateway-retry loop re-invokes _request_once per attempt and the
    thread-local, set once around the POST, is read each time."""
    module_time(monkeypatch, hv).sleep = lambda s: None
    srv.script(f"POST {_UPSERT}", sv.err(503, "gw"), sv.ok({"upserted": 1}))
    key = str(uuid.uuid4())
    with hv._request_key(key):
        assert _post(_UPSERT) == {"upserted": 1}
    sent = [r["headers"].get(_KEY_HEADER) for r in srv.calls("POST", _UPSERT)]
    assert sent == [key, key], "both attempts of one logical POST carry the one key"


def test_two_keyed_attempts_of_one_logical_call_send_one_value(srv):
    srv.script(f"POST {_SWEEP}", sv.ok({"moved": 0}))
    key = str(uuid.uuid4())
    for _ in range(3):
        with hv._request_key(key):
            _post()
    assert {r["headers"].get(_KEY_HEADER) for r in srv.calls("POST", _SWEEP)} == {key}


def test_an_unkeyed_call_sends_no_idempotency_key_header(srv):
    srv.script(f"POST {_SWEEP}", sv.ok({"moved": 0}))
    _post()
    assert _KEY_HEADER not in srv.calls("POST", _SWEEP)[0]["headers"]


def test_the_thread_local_is_empty_after_the_call_returns_and_after_it_raises(srv):
    srv.script(f"POST {_SWEEP}", sv.ok({"moved": 0}), sv.err(422, "boom"), sv.ok({"moved": 0}))
    key = str(uuid.uuid4())
    with hv._request_key(key):
        _post()
    assert getattr(hv._request_key_capture, "value", None) is None
    with pytest.raises(hv.VectorServiceError):
        with hv._request_key(key):
            _post()
    assert getattr(hv._request_key_capture, "value", None) is None
    _post()  # an unkeyed call after both: no stale key
    assert _KEY_HEADER not in srv.calls("POST", _SWEEP)[-1]["headers"]


def test_the_key_does_not_leak_to_another_thread(srv):
    srv.script(f"POST {_SWEEP}", sv.ok({"moved": 0}))
    seen: list[object] = []
    with hv._request_key(str(uuid.uuid4())):
        t = threading.Thread(target=lambda: seen.append(getattr(hv._request_key_capture, "value", None)))
        t.start()
        t.join()
    assert seen == [None]


def test_status_poll_gets_outside_the_keyed_attempt_carry_no_key(srv):
    rid = str(uuid.uuid4())
    srv.script(f"POST {_SWEEP}", sv.ok({"moved": 0}))
    srv.script("GET /v1/requests/", sv.ok({"request_id": rid, "state": "unknown"}))
    with hv._request_key(rid):
        _post()
    hv._get(f"/v1/requests/{rid}", tenant="default")
    assert _KEY_HEADER not in srv.calls("GET", "/v1/requests/")[0]["headers"]


@pytest.mark.parametrize("reply, expect", [
    (sv.ok({"moved": 1}, **{"Idempotency-Key": "k-200", "Idempotency-Replayed": "true"}),
     {"Idempotency-Key": "k-200", "Idempotency-Replayed": "true"}),
    (sv.err(409, "request_in_flight", **{"Idempotency-Key": "k-409", "Retry-After": "7"}),
     {"Idempotency-Key": "k-409", "Retry-After": "7"}),
    (sv.err(503, "sweep_gate_busy", **{"Idempotency-Key": "k-503", "Retry-After": "5"}),
     {"Idempotency-Key": "k-503", "Retry-After": "5"}),
], ids=["200", "409", "503"])
def test_idempotency_headers_are_readable_after_a_200_a_409_and_a_503(srv, reply, expect):
    srv.script(f"POST {_SWEEP}", reply)
    try:
        _post()
    except hv.VectorServiceError:
        assert reply.status != 200
    assert hv._pop_idempotency_response_headers() == expect
    assert hv._pop_idempotency_response_headers() == {}, "a pop clears"


def test_a_response_without_the_headers_leaves_no_stale_capture(srv):
    srv.script(f"POST {_SWEEP}", sv.ok({"moved": 0}, **{"Retry-After": "9"}), sv.ok({"moved": 0}))
    _post()
    _post()
    assert hv._pop_idempotency_response_headers() == {}


@pytest.mark.parametrize("keyed", [True, False], ids=["keyed", "unkeyed"])
@pytest.mark.parametrize("code", [502, 503, 504])
def test_request_never_gateway_retries_a_sweep_path_keyed_or_not(srv, monkeypatch, keyed, code):
    module_time(monkeypatch, hv).sleep = lambda s: pytest.fail("a sweep must not sleep and resend")
    srv.script(f"POST {_SWEEP}", sv.err(code, "gw"), sv.ok({"moved": 1}))
    with pytest.raises(hv.VectorServiceError):
        if keyed:
            with hv._request_key(str(uuid.uuid4())):
                _post()
        else:
            _post()
    assert len(srv.calls("POST", _SWEEP)) == 1


def test_the_skipped_collections_capture_is_unchanged(srv):
    srv.script(f"POST {_SWEEP}", sv.ok({"moved": 0}, **{"X-Nexus-Skipped-Collections": "a,b"}))
    _post()
    assert hv._pop_response_headers() == {"X-Nexus-Skipped-Collections": "a,b"}
