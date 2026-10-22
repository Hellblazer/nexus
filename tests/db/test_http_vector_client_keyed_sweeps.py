# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-222 Phase 1 (nexus-yye8l.12): the keyed bounded sweep client methods.

``gc_quarantine_orphans_bounded`` (with and without a floor) and ``gc_restore_rereferenced_bounded``
mint one uuid4 per call, send it as ``Idempotency-Key`` on every POST attempt of that call, and on an
ambiguous outcome (a gateway code, a socket timeout) ask ``GET /v1/requests/<key>`` what happened
instead of reporting failure or resending blind. Every test drives the real transport against a
scripted loopback server and reads what went over the wire.

Rules pinned here (nexus-zxgld, nexus-yye8l.12):

* poll on EVERY keyed call, the first of a fresh process included -- no echo precondition;
* committed returns the stored body; in_flight keeps polling until ``lease_until`` passes, then once
  more; rolled_back and unknown resend with the SAME key and the SAME bytes; 404 surfaces today's error;
* ``sweep_gate_busy`` (quarantine forms only) waits ``Retry-After`` (5 s when absent), then resends;
* a 409 ``request_in_flight`` polls and never re-POSTs; any other 409 keeps today's handling;
* 422 ``idempotency_key_reused`` and the two 400s raise a typed error and are never resent;
* an edge 503 (not the engine's body) waits ``Retry-After`` and then asks the ledger, which answers;
* at most 3 resends per logical request, then the last outcome surfaces.
"""
from __future__ import annotations

import gzip
import http.client
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import nexus.db.http_vector_client as hv
from nexus.catalog import chunk_quarantine as cq
from tests._module_seam import module_time
from tests.db import _scripted_vector_server as sv

_QUARANTINE = "/v1/vectors/gc/quarantine-orphans"
_RESTORE = "/v1/vectors/gc/restore-rereferenced"
_EXPIRE = "/v1/vectors/gc/expire-quarantine"
_STATUS = "/v1/requests/"
_KEY = "idempotency-key"
_QUARANTINED_AT = "2026-10-20T00:00:00Z"

#: name -> (path, call, gate_busy_possible)
_FORMS = {
    "quarantine": (
        _QUARANTINE,
        lambda c: c.gc_quarantine_orphans_bounded("col", "col__q", _QUARANTINED_AT, 20, 100),
        True,
    ),
    "quarantine_floor": (
        _QUARANTINE,
        lambda c: c.gc_quarantine_orphans_bounded(
            "col", "col__q", _QUARANTINED_AT, 20, 100, floor_fraction=0.5, floor_min_chunks=10,
        ),
        True,
    ),
    "restore": (
        _RESTORE,
        lambda c: c.gc_restore_rereferenced_bounded("col__q", "col", 100),
        False,
    ),
    # RDR-222 Amendment A1 (nexus-1579m): the bounded expire shares the quarantine forms' sweep gate.
    "expire": (
        _EXPIRE,
        lambda c: c.gc_expire_quarantine_bounded("col__q", "col", _QUARANTINED_AT, 1.0, 100, False, 100),
        True,
    ),
}
_QUARANTINE_FORMS = [n for n, f in _FORMS.items() if f[2]]
_STORED = {"moved": 7, "sample": [{"chash": "ab"}], "remaining": 0, "row_limit": 100}
_RESTORE_STORED = {"restored": 7, "remaining": 0, "row_limit": 100}


_EXPIRE_STORED = {"expired": 7, "refused": 2, "more": False, "row_limit": 100, "floor_refused": False}


def _stored(form: str) -> dict:
    return {"restore": _RESTORE_STORED, "expire": _EXPIRE_STORED}.get(form, _STORED)


class FakeClock:
    """The module's `_now` and `time.sleep` seam: a sleep advances the clock, nothing really waits."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    module_time(monkeypatch, hv).sleep = fake.sleep
    monkeypatch.setattr(hv, "_now", fake)
    return fake


@pytest.fixture
def srv(monkeypatch):
    server = sv.start()
    hv.reset_connection_pool_for_tests()
    monkeypatch.setattr(hv, "_resolve_endpoint", lambda: (server.url, "tok"))
    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", lambda base_url: None)
    yield server
    hv.reset_connection_pool_for_tests()
    sv.stop(server)


@pytest.fixture
def client() -> hv.HttpVectorClient:
    return hv.HttpVectorClient(tenant="default")


def _assert_unresolved_note(exc: BaseException, why: str) -> None:
    """The error says the sweep may have committed, names the key, and says where to look."""
    notes = "\n".join(getattr(exc, "__notes__", []))
    assert "outcome unknown" in notes and why in notes, notes
    assert "the sweep may have committed" in notes
    match = [w for w in notes.split() if w.count("-") == 4 and len(w) >= 36]
    assert match and f"GET /v1/requests/{match[0].rstrip(':,')}" in notes, notes


def _posts(srv, path):
    return srv.calls("POST", path)


def _polls(srv):
    return srv.calls("GET", _STATUS)


def _status(state: str, key: str = "", **extra) -> sv.Reply:
    return sv.ok({"request_id": key, "state": state, **extra})


def _committed(form: str) -> sv.Reply:
    return _status("committed", response_status=200, response=_stored(form))


# ── polling on an ambiguous outcome ──────────────────────────────────────────


# Bodies that cannot be read as JSON, each failing at a different layer: the JSON parser, the UTF-8 decode,
# a gzip stream cut short, and a gzip stream with a corrupt header.
_UNREADABLE = {
    "not-json": sv.Reply(200, b"{not json"),
    "not-utf8": sv.Reply(200, b'{"expired": "\xff\xfe"}'),
    "gzip-truncated": sv.Reply(200, gzip.compress(b'{"expired": 1}')[:12], {"Content-Encoding": "gzip"}),
    "gzip-corrupt": sv.Reply(200, b"\x1f\x8bnot-a-gzip-stream", {"Content-Encoding": "gzip"}),
}


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_a_socket_timeout_on_the_first_keyed_call_of_a_fresh_client_polls_and_committed_returns_the_stored_body(
    srv, client, clock, monkeypatch, form,
):
    """The ll31n shape on the wire. No earlier echo, nothing cached: the first keyed call polls."""
    path, call, _ = _FORMS[form]
    monkeypatch.setattr(hv, "_SWEEP_POST_TIMEOUT_S", 0.3)
    srv.script(f"POST {path}", "hang")
    srv.script(f"GET {_STATUS}", _committed(form))
    assert call(client) == _stored(form)
    (post,) = _posts(srv, path)
    (poll,) = _polls(srv)
    assert poll["path"] == _STATUS + post["headers"][_KEY], "the poll names the key the POST carried"
    assert _KEY not in poll["headers"], "a status poll carries no key"
    assert uuid.UUID(post["headers"][_KEY]).version == 4


@pytest.mark.parametrize("code", [502, 504])
@pytest.mark.parametrize("form", sorted(_FORMS))
def test_a_gateway_code_polls_and_committed_returns_the_stored_body(srv, client, clock, form, code):
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(code, "gw"))
    srv.script(f"GET {_STATUS}", _committed(form))
    assert call(client) == _stored(form)
    assert len(_posts(srv, path)) == 1 and len(_polls(srv)) == 1


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_a_409_request_in_flight_polls_and_never_re_posts(srv, client, clock, form):
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(409, "request_in_flight", **{"Retry-After": "4"}))
    srv.script(f"GET {_STATUS}", _committed(form))
    assert call(client) == _stored(form)
    assert len(_posts(srv, path)) == 1, "zero extra POSTs after a 409"
    assert len(_polls(srv)) == 1


@pytest.mark.parametrize("state", ["unknown", "rolled_back"])
@pytest.mark.parametrize("form", sorted(_FORMS))
def test_unknown_and_rolled_back_resend_with_the_same_key_and_the_same_bytes(srv, client, clock, form, state):
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(504, "gw"), sv.ok(_stored(form)))
    srv.script(f"GET {_STATUS}", _status(state))
    assert call(client) == _stored(form)
    first, second = _posts(srv, path)
    assert first["headers"][_KEY] == second["headers"][_KEY]
    assert first["body"] == second["body"], "a resend is byte-identical: the key binds the body's hash"
    assert len(_polls(srv)) == 1


def test_in_flight_keeps_polling_every_5_seconds_until_the_lease_passes(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    lease = (clock.now + timedelta(seconds=12)).isoformat()
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(
        f"GET {_STATUS}",
        _status("in_flight", started_at=clock.now.isoformat(), lease_until=lease),
        _status("in_flight", started_at=clock.now.isoformat(), lease_until=lease),
        _committed("quarantine"),
    )
    assert call(client) == _STORED
    assert clock.sleeps == [5.0, 5.0]
    assert len(_polls(srv)) == 3 and len(_posts(srv, path)) == 1


def test_an_in_flight_status_is_polled_a_grace_past_its_lease_then_the_same_key_is_resent(srv, client, clock):
    """While a marker transaction holds an expired row the status route can answer in_flight with a
    lease already in the past: polling does not stop exactly at lease_until."""
    path, call, _ = _FORMS["quarantine"]
    lease = (clock.now + timedelta(seconds=4)).isoformat()
    in_flight = _status("in_flight", started_at=clock.now.isoformat(), lease_until=lease)
    srv.script(f"POST {path}", sv.err(504, "gw"), sv.ok(_STORED))
    srv.script(f"GET {_STATUS}", in_flight)  # in_flight forever: the holder is gone
    assert call(client) == _STORED
    # t=0 before the lease, t=5 one second past it (inside the grace), t=10 past lease + grace: one
    # more interval, then the resend (so a 409 loop is not four quick round trips).
    assert clock.sleeps == [5.0, 5.0, 5.0]
    assert len(_polls(srv)) == 3
    first, second = _posts(srv, path)
    assert first["headers"][_KEY] == second["headers"][_KEY] and first["body"] == second["body"]


def test_a_status_that_turns_rolled_back_inside_the_grace_resends_at_once(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    lease = (clock.now + timedelta(seconds=4)).isoformat()
    srv.script(f"POST {path}", sv.err(504, "gw"), sv.ok(_STORED))
    srv.script(
        f"GET {_STATUS}",
        _status("in_flight", started_at=clock.now.isoformat(), lease_until=lease),
        _status("rolled_back"),
    )
    assert call(client) == _STORED
    assert clock.sleeps == [5.0] and len(_polls(srv)) == 2


def test_a_pool_exhaustion_503_keeps_polling_in_flight_then_resends_and_a_409_after_it_is_a_poll_not_a_failure(
    srv, client, clock,
):
    """The engine leaves a pool-exhaustion 503's key in_flight until the 95 s lease lapses (no failure
    marker, no Retry-After). Poll it, never mint a new key; once it reads rolled_back the same-key
    resend may be told request_in_flight by a racing holder, which is a poll, not an error."""
    path, call, _ = _FORMS["quarantine"]
    lease = (clock.now + timedelta(seconds=95)).isoformat()
    in_flight = _status("in_flight", started_at=clock.now.isoformat(), lease_until=lease)
    srv.script(
        f"POST {path}", sv.err(503, "pool_exhausted"), sv.err(409, "request_in_flight"), sv.ok(_STORED),
    )
    srv.script(f"GET {_STATUS}", in_flight, in_flight, _status("rolled_back"), _committed("quarantine"))
    assert call(client) == _STORED
    posts = _posts(srv, path)
    assert len(posts) == 2, "one resend after rolled_back; the 409 was answered by a poll, not a POST"
    assert posts[0]["headers"][_KEY] == posts[1]["headers"][_KEY] and posts[0]["body"] == posts[1]["body"]
    assert len(_polls(srv)) == 4
    assert clock.sleeps == [5.0, 5.0], "no wait before the first poll of a 503 that carried no Retry-After"


def test_a_404_from_the_status_route_surfaces_the_original_gateway_error(srv, client, clock):
    """An engine with no ledger: today's behaviour, plus the one status GET that told us so."""
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(504, "gw"))  # no status script: the route answers 404
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 504
    assert len(_posts(srv, path)) == 1 and len(_polls(srv)) == 1
    _assert_unresolved_note(exc.value, "this engine keeps no request ledger")


def test_polls_that_fail_three_times_surface_the_original_error(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", sv.err(500, "internal server error"))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 504
    assert len(_polls(srv)) == 3 and len(_posts(srv, path)) == 1
    assert clock.sleeps == [5.0, 5.0]
    _assert_unresolved_note(exc.value, "the status polls kept failing")


@pytest.mark.parametrize("unreadable", sorted(_UNREADABLE))
def test_a_status_poll_whose_body_does_not_parse_is_a_failed_poll_and_the_next_poll_settles_it(
    srv, client, clock, unreadable,
):
    """A mangled poll body (edge HTML, a truncation) says nothing about the request: it counts as a failed
    poll, never a ValueError that would abort the indexer's whole prune pass."""
    path, call, form = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", _UNREADABLE[unreadable], _committed("quarantine"))
    assert call(client) == _stored("quarantine")
    assert len(_polls(srv)) == 2 and len(_posts(srv, path)) == 1


def test_status_polls_whose_bodies_never_parse_surface_the_original_error(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", sv.Reply(200, b"{not json"))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 504
    assert len(_polls(srv)) == 3
    _assert_unresolved_note(exc.value, "the status polls kept failing")


# ── sweep_gate_busy and the edge 503 ─────────────────────────────────────────


@pytest.mark.parametrize("form", _QUARANTINE_FORMS)
def test_sweep_gate_busy_waits_retry_after_and_resends_with_the_same_key(srv, client, clock, form):
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(503, "sweep_gate_busy", **{"Retry-After": "3"}), sv.ok(_stored(form)))
    assert call(client) == _stored(form)
    assert clock.sleeps == [3.0]
    first, second = _posts(srv, path)
    assert first["headers"][_KEY] == second["headers"][_KEY] and first["body"] == second["body"]
    assert _polls(srv) == [], "the gate busy means nothing was applied: no poll"


@pytest.mark.parametrize("form", _QUARANTINE_FORMS)
def test_sweep_gate_busy_without_retry_after_waits_5_seconds(srv, client, clock, form):
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(503, "sweep_gate_busy"), sv.ok(_stored(form)))
    call(client)
    assert clock.sleeps == [5.0]


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_request_claim_contended_waits_retry_after_and_resends_the_same_key_without_polling(
    srv, client, clock, form,
):
    """The ledger's claim loop was contended: the request did not run (restore included: it is the
    ledger, not the gate)."""
    path, call, _ = _FORMS[form]
    srv.script(
        f"POST {path}", sv.err(503, "request_claim_contended", **{"Retry-After": "1"}), sv.ok(_stored(form)),
    )
    assert call(client) == _stored(form)
    assert clock.sleeps == [1.0]
    first, second = _posts(srv, path)
    assert first["headers"][_KEY] == second["headers"][_KEY] and first["body"] == second["body"]
    assert _polls(srv) == []


def test_request_claim_contended_without_retry_after_waits_one_second(srv, client, clock):
    path, call, _ = _FORMS["restore"]
    srv.script(f"POST {path}", sv.err(503, "request_claim_contended"), sv.ok(_RESTORE_STORED))
    call(client)
    assert clock.sleeps == [1.0]


def test_an_edge_503_waits_retry_after_then_asks_the_ledger_and_resends_the_same_key(srv, client, clock):
    """The managed edge's own 503 (its in-flight gate): the request never reached the engine, so the
    ledger has no row and says unknown; the same-key resend is safe. Its body is not an engine error."""
    path, call, _ = _FORMS["quarantine"]
    srv.script(
        f"POST {path}",
        sv.Reply(503, b"<html>Service Unavailable</html>", {"Retry-After": "2"}),
        sv.ok(_STORED),
    )
    srv.script(f"GET {_STATUS}", _status("unknown"))
    assert call(client) == _STORED
    assert clock.sleeps == [2.0]
    first, second = _posts(srv, path)
    assert first["headers"][_KEY] == second["headers"][_KEY]
    assert len(_polls(srv)) == 1


def test_an_edge_503_whose_request_the_engine_did_run_reads_committed_from_the_ledger(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.Reply(503, b"", {"Retry-After": "2"}))
    srv.script(f"GET {_STATUS}", _committed("quarantine"))
    assert call(client) == _STORED
    assert len(_posts(srv, path)) == 1


def test_an_engine_503_that_is_not_the_gate_polls_without_waiting_for_a_retry_after_it_did_not_send(
    srv, client, clock,
):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(503, "pool_exhausted"))
    srv.script(f"GET {_STATUS}", _committed("quarantine"))
    assert call(client) == _STORED
    assert clock.sleeps == []


# ── typed refusals and unchanged errors ──────────────────────────────────────


@pytest.mark.parametrize("status, error", [
    (422, "idempotency_key_reused"),
    (400, "idempotency_key_malformed"),
    (400, "idempotency_key_unsupported"),
], ids=["reused", "malformed", "unsupported"])
@pytest.mark.parametrize("form", sorted(_FORMS))
def test_a_key_refusal_raises_the_typed_error_and_is_never_resent(srv, client, clock, form, status, error):
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(status, error), sv.ok(_stored(form)))
    with pytest.raises(hv.IdempotencyKeyRefusedError) as exc:
        call(client)
    assert exc.value.code == status and exc.value.engine_body == {"error": error}
    assert isinstance(exc.value, hv.VectorServiceError), "callers that catch VectorServiceError still do"
    assert len(_posts(srv, path)) == 1 and _polls(srv) == [] and clock.sleeps == []


@pytest.mark.parametrize("status, body", [
    (409, {"error": "duplicate key value violates unique constraint", "sqlstate": "23505"}),
    (409, {"error": "pipeline_conflict"}),
    (500, {"error": "internal server error"}),
    (422, {"error": "unregistered_collection", "collection": "col"}),
    (404, {"error": "no such route"}),
], ids=["class23-409", "pipeline-409", "500", "422-other", "404"])
def test_other_errors_keep_their_handling_dispatching_on_the_error_field(srv, client, clock, status, body):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.Reply(status, body), sv.ok(_STORED))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == status and not isinstance(exc.value, hv.IdempotencyKeyRefusedError)
    assert len(_posts(srv, path)) == 1 and _polls(srv) == []


# ── the cap ──────────────────────────────────────────────────────────────────


def test_resends_stop_at_three_and_the_last_outcome_surfaces(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", _status("unknown"))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 504
    posts = _posts(srv, path)
    assert len(posts) == 4, "the first attempt plus three resends"
    assert {p["headers"][_KEY] for p in posts} == {posts[0]["headers"][_KEY]}
    assert len({p["body"] for p in posts}) == 1
    assert len(_polls(srv)) == 4, "every ambiguous POST is polled, the last one too"
    _assert_unresolved_note(exc.value, "the resend cap of 3 was reached")


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_the_fourth_post_timing_out_is_polled_and_a_committed_ledger_returns_the_stored_body(
    srv, client, clock, monkeypatch, form,
):
    """The ll31n shape at the cap: three resends were spent, the 4th POST times out, and the sweep
    committed. The failure must not surface; the ledger is asked."""
    path, call, _ = _FORMS[form]
    monkeypatch.setattr(hv, "_SWEEP_POST_TIMEOUT_S", 0.3)
    srv.script(f"POST {path}", sv.err(504, "gw"), sv.err(504, "gw"), sv.err(504, "gw"), "hang")
    srv.script(f"GET {_STATUS}", _status("unknown"), _status("unknown"), _status("unknown"), _committed(form))
    assert call(client) == _stored(form)
    posts = _posts(srv, path)
    assert len(posts) == 4 and len(_polls(srv)) == 4
    assert {p["headers"][_KEY] for p in posts} == {posts[0]["headers"][_KEY]}


def test_a_rolled_back_status_at_the_cap_surfaces_with_a_note_and_no_fifth_post(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(409, "request_in_flight"))
    srv.script(f"GET {_STATUS}", _status("rolled_back"))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 409
    assert len(_posts(srv, path)) == 4 and len(_polls(srv)) == 4
    _assert_unresolved_note(exc.value, "ledger last read rolled_back")


def test_a_409_loop_over_a_stale_in_flight_key_sleeps_an_interval_before_each_resend(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    stale = (clock.now - timedelta(seconds=100)).isoformat()
    srv.script(f"POST {path}", sv.err(409, "request_in_flight"))
    srv.script(f"GET {_STATUS}", _status("in_flight", started_at=stale, lease_until=stale))
    with pytest.raises(hv.VectorServiceError):
        call(client)
    assert len(_posts(srv, path)) == 4
    assert clock.sleeps == [5.0, 5.0, 5.0], "one interval before each resend, none after the last"


def test_a_gate_busy_that_never_clears_stops_at_the_cap_too(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(503, "sweep_gate_busy", **{"Retry-After": "1"}))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 503 and exc.value.engine_body == {"error": "sweep_gate_busy"}
    assert len(_posts(srv, path)) == 4
    assert _polls(srv) == [], "nothing ran: no poll, at the cap either"


# ── keys: one per call, the thread-local, a replay ───────────────────────────


def test_each_call_mints_its_own_key_and_the_thread_local_is_empty_after_each(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.ok(_STORED))
    call(client)
    call(client)
    k1, k2 = (p["headers"][_KEY] for p in _posts(srv, path))
    assert k1 != k2
    assert getattr(hv._request_key_capture, "value", None) is None
    srv.script(f"POST {path}", sv.err(422, "idempotency_key_reused"))
    with pytest.raises(hv.IdempotencyKeyRefusedError):
        call(client)
    assert getattr(hv._request_key_capture, "value", None) is None


def test_a_replayed_response_is_the_result(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.ok(_STORED, **{"Idempotency-Replayed": "true"}))
    assert call(client) == _STORED


# ── an engine that predates the ledger ───────────────────────────────────────

#: The POST the methods sent before this change (recorded from the unkeyed implementation).
_PRE_CHANGE_BODIES = {
    "quarantine": {
        "collection": "col", "quarantine_collection": "col__q", "quarantined_at": _QUARANTINED_AT,
        "sample_limit": 20, "row_limit": 100,
    },
    "quarantine_floor": {
        "collection": "col", "quarantine_collection": "col__q", "quarantined_at": _QUARANTINED_AT,
        "sample_limit": 20, "row_limit": 100, "floor_fraction": 0.5, "floor_min_chunks": 10,
        "force": False,
    },
    "restore": {"quarantine_collection": "col__q", "origin_collection": "col", "row_limit": 100},
    "expire": {
        "quarantine_collection": "col__q", "origin_collection": "col", "cutoff": _QUARANTINED_AT,
        "floor_fraction": 1.0, "floor_min_chunks": 100, "force": False, "row_limit": 100,
        "count_refused": True,
    },
}


# ── nexus-wbfpw.64: resolve_siblings, additive on the restore and expire bounded forms ──

_SIBLING_CALLS = {
    "restore": lambda c: c.gc_restore_rereferenced_bounded("col__q", "col", 100, resolve_siblings=True),
    "expire": lambda c: c.gc_expire_quarantine_bounded(
        "col__q", "col", _QUARANTINED_AT, 1.0, 100, False, 100, resolve_siblings=True,
    ),
}


@pytest.mark.parametrize("form", sorted(_SIBLING_CALLS))
def test_asking_for_the_siblings_adds_exactly_one_field_and_returns_the_engines_list(srv, client, clock, form):
    path = _FORMS[form][0]
    answer = {**_stored(form), "quarantine_collections": ["quarantine-col", "quarantine-code__old__m__v1"]}
    srv.script(f"POST {path}", sv.ok(answer))
    assert _SIBLING_CALLS[form](client) == answer
    (post,) = _posts(srv, path)
    assert json.loads(post["body"]) == {**_PRE_CHANGE_BODIES[form], "resolve_siblings": True}


@pytest.mark.parametrize("form", sorted(_SIBLING_CALLS))
def test_not_asking_sends_the_body_the_method_always_sent(srv, client, clock, form):
    """The field is sent only when asked: the default call's bytes are the pre-nexus-wbfpw.64 bytes."""
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.ok(_stored(form)))
    call(client)
    (post,) = _posts(srv, path)
    assert "resolve_siblings" not in json.loads(post["body"])


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_an_engine_that_ignores_the_header_answers_exactly_as_before(srv, client, clock, form):
    """OLD engine: the header is ignored, nothing is echoed, the status route is 404. The success path
    is one POST whose body is today's body; the key is the only addition."""
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.ok(_stored(form)))
    assert call(client) == _stored(form)
    (post,) = _posts(srv, path)
    assert json.loads(post["body"]) == _PRE_CHANGE_BODIES[form]
    assert _polls(srv) == [] and clock.sleeps == []


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_an_engine_without_a_ledger_fails_a_gateway_timeout_exactly_as_before(srv, client, clock, form):
    """Before: one POST, then the 504 raised. Now: that POST, one status GET answered 404, the same 504."""
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(504, "gw"))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 504 and "HTTP 504" in str(exc.value)
    assert [json.loads(p["body"]) for p in _posts(srv, path)] == [_PRE_CHANGE_BODIES[form]]
    assert [r["method"] for r in srv.requests] == ["POST", "GET"]
    assert clock.sleeps == []


# ── unresolved outcomes, transport classification, old engines ───────────────


def test_a_poll_budget_that_runs_out_surfaces_the_original_error_with_a_note(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    lease = (clock.now + timedelta(hours=1)).isoformat()
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", _status("in_flight", started_at=clock.now.isoformat(), lease_until=lease))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 504 and len(_posts(srv, path)) == 1
    assert sum(clock.sleeps) > hv._STATUS_POLL_BUDGET_S
    _assert_unresolved_note(exc.value, "the status poll budget ran out")


def test_a_status_state_the_client_does_not_know_surfaces_the_original_error_with_a_note(srv, client, clock):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", _status("exploded"))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 504
    _assert_unresolved_note(exc.value, "a state this client does not know")


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_committed_without_a_stored_body_reports_committed_with_unknown_counts(srv, client, clock, form):
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", _status("committed", response_status=200, response=None))
    result = call(client)
    assert isinstance(result, hv.CommittedWithUnknownCounts)
    assert dict(result) == {"committed_unknown_counts": True}
    assert len(_posts(srv, path)) == 1, "committed: never resent"


@pytest.mark.parametrize("entry", ["drop", "garbage"])
def test_a_reset_or_a_malformed_response_is_ambiguous_and_polled(srv, client, clock, entry):
    """A connection closed with no answer (a reset/abort family) and a response that is not HTTP
    (http.client.BadStatusLine) may follow a sweep that ran: ask the ledger."""
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", entry)
    srv.script(f"GET {_STATUS}", _committed("quarantine"))
    assert call(client) == _STORED
    assert len(_posts(srv, path)) == 1 and len(_polls(srv)) == 1


def test_a_bad_status_line_is_the_http_client_family_the_catch_must_cover(srv):
    srv.script(f"POST {_QUARANTINE}", "garbage")
    with pytest.raises(http.client.HTTPException):
        hv._post(_QUARANTINE, {"collection": "c"}, tenant="default")


def _refuse_status_polls(monkeypatch) -> None:
    def refuse(path, *, tenant="default"):
        raise AssertionError(f"a request that provably never ran must not be polled: {path}")

    monkeypatch.setattr(hv, "_get", refuse)


@pytest.fixture
def closed_port(monkeypatch):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(hv, "_resolve_endpoint", lambda: (f"http://127.0.0.1:{port}", "tok"))
    monkeypatch.setattr(hv, "_wait_for_lease_republication", lambda: None)
    return port


def test_a_refused_connection_proves_nothing_ran_and_raises_at_once_without_a_poll(
    closed_port, client, clock, monkeypatch,
):
    _refuse_status_polls(monkeypatch)
    path, call, _ = _FORMS["quarantine"]
    with pytest.raises(OSError):
        call(client)
    assert clock.sleeps == []


def test_a_managed_endpoints_timeout_reframed_without_a_code_is_polled(srv, client, clock, monkeypatch):
    path, call, _ = _FORMS["quarantine"]
    monkeypatch.setattr(hv, "_managed_remedy", lambda: "check NX_SERVICE_URL")
    monkeypatch.setattr(hv, "_SWEEP_POST_TIMEOUT_S", 0.3)
    srv.script(f"POST {path}", "hang")
    srv.script(f"GET {_STATUS}", _committed("quarantine"))
    assert call(client) == _STORED
    assert len(_polls(srv)) == 1


def test_a_managed_endpoints_refused_connection_is_unwrapped_and_raised_at_once(
    closed_port, client, clock, monkeypatch,
):
    """code=None VectorServiceError from a managed endpoint: its __cause__ is the refused connection."""
    _refuse_status_polls(monkeypatch)
    monkeypatch.setattr(hv, "_managed_remedy", lambda: "check NX_SERVICE_URL")
    path, call, _ = _FORMS["quarantine"]
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code is None and isinstance(exc.value.__cause__, OSError)


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_an_old_engine_with_a_socket_timeout_raises_the_timeout_after_one_post_and_one_404_poll(
    srv, client, clock, monkeypatch, form,
):
    """No ledger (status route 404): the call raises what it raised before (the raw timeout), the only
    additions being one status GET and a note."""
    path, call, _ = _FORMS[form]
    monkeypatch.setattr(hv, "_SWEEP_POST_TIMEOUT_S", 0.3)
    srv.script(f"POST {path}", "hang")
    with pytest.raises(TimeoutError) as exc:
        call(client)
    assert [r["method"] for r in srv.requests] == ["POST", "GET"]
    assert json.loads(srv.requests[0]["body"]) == _PRE_CHANGE_BODIES[form]
    _assert_unresolved_note(exc.value, "this engine keeps no request ledger")


def test_an_old_engine_503_with_a_long_retry_after_sleeps_at_most_five_seconds_before_the_404_poll(
    srv, client, clock,
):
    path, call, _ = _FORMS["quarantine"]
    srv.script(f"POST {path}", sv.err(503, "overloaded", **{"Retry-After": "45"}))
    with pytest.raises(hv.VectorServiceError) as exc:
        call(client)
    assert exc.value.code == 503
    assert clock.sleeps == [5.0], "Retry-After 45 is capped before a poll that an old engine answers 404"
    assert [r["method"] for r in srv.requests] == ["POST", "GET"]


def test_restore_never_waits_out_a_sweep_gate_busy_it_cannot_receive(srv, client, clock):
    """Restore takes no sweep gate. Were an engine to send that body there, it is an ordinary 503: polled."""
    path, call, _ = _FORMS["restore"]
    srv.script(f"POST {path}", sv.err(503, "sweep_gate_busy", **{"Retry-After": "3"}))
    srv.script(f"GET {_STATUS}", _committed("restore"))
    assert call(client) == _RESTORE_STORED
    assert len(_polls(srv)) == 1 and len(_posts(srv, path)) == 1


# ── chunk_quarantine's loops ─────────────────────────────────────────────────


def test_each_batch_of_the_quarantine_drain_carries_its_own_key_and_a_truncated_replay_is_accepted(
    srv, client, clock,
):
    srv.script(
        f"POST {_QUARANTINE}",
        sv.ok({"moved": 100, "sample": [{"chash": f"{i:02x}"} for i in range(5)], "remaining": 40, "row_limit": 100}),
        sv.ok(
            {"moved": 40, "sample": [{"chash": "ff"}], "sample_truncated": True, "remaining": 0, "row_limit": 100},
            **{"Idempotency-Replayed": "true"},
        ),
    )
    moved, sample = cq.quarantine_orphans_bounded_serverside(
        client, "col", "col__q", _QUARANTINED_AT, sample_limit=20, row_limit=100,
    )
    assert moved == 140, "moved is the sum of the batches, whatever length the sample is"
    assert len(sample) == 6
    k1, k2 = (p["headers"][_KEY] for p in _posts(srv, _QUARANTINE))
    assert k1 != k2


def test_a_batch_committed_with_unknown_counts_ends_the_quarantine_drain_and_a_strict_drain_raises(
    srv, client, clock,
):
    srv.script(f"POST {_QUARANTINE}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", _status("committed", response_status=200, response=None))
    moved, _sample = cq.quarantine_orphans_bounded_serverside(
        client, "col", "col__q", _QUARANTINED_AT, sample_limit=20, row_limit=100,
    )
    assert moved == 0 and len(_posts(srv, _QUARANTINE)) == 1, "stops, never loops on a guessed remaining"
    with pytest.raises(cq.BoundedDrainIncomplete) as exc:
        cq.quarantine_orphans_bounded_serverside(
            client, "col", "col__q", _QUARANTINED_AT, sample_limit=20, row_limit=100, strict=True,
        )
    assert exc.value.reason == "batch committed, counts unknown"


def test_a_batch_committed_with_unknown_counts_ends_the_restore_drain(srv, client, clock):
    srv.script(f"POST {_RESTORE}", sv.err(504, "gw"))
    srv.script(f"GET {_STATUS}", _status("committed", response_status=200, response=None))
    assert cq.restore_rereferenced_bounded_serverside(client, "col__q", "col", row_limit=100) == 0
    assert len(_posts(srv, _RESTORE)) == 1


def test_each_batch_of_the_restore_drain_carries_its_own_key(srv, client, clock):
    srv.script(
        f"POST {_RESTORE}",
        sv.ok({"restored": 100, "remaining": 5, "row_limit": 100}),
        sv.ok({"restored": 5, "remaining": 0, "row_limit": 100}),
    )
    assert cq.restore_rereferenced_bounded_serverside(client, "col__q", "col", row_limit=100) == 105
    k1, k2 = (p["headers"][_KEY] for p in _posts(srv, _RESTORE))
    assert k1 != k2


# ── the bounded expire drain (RDR-222 Amendment A1, nexus-1579m) ─────────────


def _expire_page(expired: int, *, more: bool, refused: int | None = None, row_limit: int = 100) -> dict:
    return {"expired": expired, "refused": refused, "more": more, "row_limit": row_limit, "floor_refused": False}


def _drain(client, *, strict: bool = False):
    return cq.expire_quarantine_bounded_serverside(
        client, "col__q", "col", _QUARANTINED_AT, row_limit=100, strict=strict,
    )


def test_each_page_of_the_expire_drain_carries_its_own_key_and_sums_the_pages(srv, client, clock):
    srv.script(
        f"POST {_EXPIRE}",
        sv.ok(_expire_page(100, more=True)),
        sv.ok(_expire_page(40, more=False)),
    )
    assert _drain(client) == (140, None, True)
    k1, k2 = (p["headers"][_KEY] for p in _posts(srv, _EXPIRE))
    assert k1 != k2
    assert [json.loads(p["body"])["row_limit"] for p in _posts(srv, _EXPIRE)] == [100, 100]


def test_a_non_strict_drain_never_asks_for_the_refused_count_and_a_strict_one_stops_asking_once_it_has_it(
    srv, client, clock,
):
    srv.script(f"POST {_EXPIRE}", sv.ok(_expire_page(100, more=True)), sv.ok(_expire_page(1, more=False)))
    _drain(client)
    assert [json.loads(p["body"])["count_refused"] for p in _posts(srv, _EXPIRE)] == [False, False]

    srv.requests.clear()
    srv.script(
        f"POST {_EXPIRE}",
        sv.ok(_expire_page(100, more=True, refused=3)),
        sv.ok(_expire_page(50, more=True, refused=None)),
        sv.ok(_expire_page(1, more=False, refused=None)),
    )
    assert _drain(client, strict=True) == (151, 3, True)
    assert [json.loads(p["body"])["count_refused"] for p in _posts(srv, _EXPIRE)] == [True, False, False]


def test_an_engine_that_ignores_row_limit_runs_the_unbounded_expire_and_its_answer_is_the_total(
    srv, client, clock,
):
    """No `more` in the answer: an engine before the bounded form (and before the ledger, so it ignored the key
    too) already did the whole expiry. The loop stops after that one POST."""
    srv.script(f"POST {_EXPIRE}", sv.ok({"expired": 7, "refused": 2}))
    assert _drain(client) == (7, 2, True)
    assert len(_posts(srv, _EXPIRE)) == 1


def test_a_page_committed_with_unknown_counts_does_not_end_the_drain(srv, client, clock):
    for strict in (False, True):
        srv.requests.clear()
        srv.script(f"POST {_EXPIRE}", sv.err(504, "gw"), sv.ok(_expire_page(3, more=False)))
        srv.script(f"GET {_STATUS}", _status("committed", response_status=200, response=None))
        expired, _refused, complete = _drain(client, strict=strict)
        assert (expired, complete) == (3, False)
        assert len(_posts(srv, _EXPIRE)) == 2


def test_a_strict_drain_whose_later_page_could_not_be_settled_names_the_key(srv, client, clock):
    srv.script(f"POST {_EXPIRE}", sv.ok(_expire_page(100, more=True, refused=0)), sv.err(504, "gw"))
    with pytest.raises(cq.BoundedDrainIncomplete) as exc:
        _drain(client, strict=True)
    assert exc.value.reason == "page failed" and exc.value.moved == 100 and exc.value.batches == 1
    assert exc.value.kind == "expire"
    assert isinstance(exc.value.__cause__, hv.VectorServiceError)
    _assert_unresolved_note(exc.value.__cause__, "this engine keeps no request ledger")


def test_a_refused_key_on_the_first_page_is_raised_unchanged_when_strict_and_never_resent(srv, client, clock):
    for strict in (True, False):
        srv.requests.clear()
        srv.script(f"POST {_EXPIRE}", sv.err(400, "idempotency_key_unsupported"), sv.ok(_expire_page(1, more=False)))
        if strict:
            with pytest.raises(hv.IdempotencyKeyRefusedError):
                _drain(client, strict=True)
        else:
            assert _drain(client) == (0, None, False), "non-strict: a warning, never a raise"
        assert len(_posts(srv, _EXPIRE)) == 1, "a refused key is never resent"


@pytest.mark.parametrize("unreadable", sorted(_UNREADABLE))
@pytest.mark.parametrize("form", sorted(_FORMS))
def test_a_200_whose_body_does_not_parse_is_ambiguous_and_polled(srv, client, clock, form, unreadable):
    """The page may have committed before its body was cut or mangled (edge HTML, a truncation that is not
    an IncompleteRead): the ledger is asked, never a blind resend and never a bare JSONDecodeError."""
    path, call, _ = _FORMS[form]
    srv.script(f"POST {path}", _UNREADABLE[unreadable])
    srv.script(f"GET {_STATUS}", _committed(form))
    assert call(client) == _stored(form)
    assert len(_posts(srv, path)) == 1, "committed: never resent"
    assert len(_polls(srv)) == 1


def test_a_200_whose_body_does_not_parse_and_the_ledger_cannot_settle_is_a_service_error_in_both_modes(
    srv, client, clock,
):
    """Unsettled, it surfaces like any other ambiguous outcome (a VectorServiceError naming the key), so
    a non-strict drain skips the sibling instead of aborting the whole prune with a ValueError."""
    srv.script(f"POST {_EXPIRE}", sv.Reply(200, b"{not json"))  # no ledger script: the poll finds none
    assert _drain(client) == (0, None, False), "non-strict: a warning, never a raise"
    srv.requests.clear()
    with pytest.raises(hv.VectorServiceError) as exc:
        _drain(client, strict=True)
    assert isinstance(exc.value.__cause__, ValueError)
    _assert_unresolved_note(exc.value, "this engine keeps no request ledger")


def test_a_later_page_whose_200_body_does_not_parse_is_settled_by_the_ledger(srv, client, clock):
    for strict in (False, True):
        srv.requests.clear()
        srv.script(
            f"POST {_EXPIRE}", sv.ok(_expire_page(100, more=True)), sv.Reply(200, b"{not json"),
        )
        srv.script(f"GET {_STATUS}", _status("committed", response_status=200, response=_expire_page(5, more=False)))
        assert _drain(client, strict=strict) == (105, None, True)
        assert len(_posts(srv, _EXPIRE)) == 2 and len(_polls(srv)) == 1


@pytest.mark.parametrize("body", [{"expired": None, "more": False}, {"expired": "x", "more": False}])
def test_a_parsed_body_with_a_bad_shape_still_propagates_in_both_modes(srv, client, clock, body):
    """A parsed body whose fields are wrong is a response-SHAPE defect, not an ambiguous outcome: no poll."""
    for strict in (False, True):
        srv.requests.clear()
        srv.script(f"POST {_EXPIRE}", sv.ok(body))
        with pytest.raises((TypeError, ValueError)):
            _drain(client, strict=strict)
        assert not _polls(srv)
