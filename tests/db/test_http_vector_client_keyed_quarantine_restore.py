# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-222 (nexus-k9kr9): ``HttpVectorClient.gc_quarantine_restore`` as a keyed call.

A real run of at most ``QUARANTINE_RESTORE_KEYED_MAX_CHASHES`` chashes goes through ``_keyed_sweep_post``: one
uuid4 ``Idempotency-Key`` per logical call, on every POST attempt, and an ambiguous outcome is settled by
``GET /v1/requests/<key>``. Every test drives the real transport against the scripted loopback server and reads what
went over the wire.

Rules pinned here:

* a real run is keyed; a dry run, and a ``chashes`` list over the keyed ceiling, are not (the engine refuses a key
  on the first, and would refuse the second's size);
* ``quarantine_restore_busy`` (the reason is in ``reason``, the ``error`` is prose) did not run: it waits
  ``Retry-After`` (5 s when absent) and resends the SAME key and bytes, at most 3 times, then raises the error with
  the engine's body so the CLI keeps its exit 6;
* a gateway code or a socket timeout polls the ledger: committed returns the stored body, rolled_back resends;
* ``quarantine_restore_busy`` is "nothing ran" only when its body says ``nothing_moved`` is true; any other busy
  answer is raised at once with the engine's body;
* an old engine still works: one that ignores the header answers as before, and one that has the ledger but not keyed
  restore (400 ``idempotency_key_unsupported``, which ran nothing) is sent the call again without the key, but only
  when that refusal answers the first attempt (after a poll or a resend it is raised); any other key refusal is
  raised and never resent;
* a 422 ``keyed_restore_body_over_cap`` (the page's body would not fit the ledger's stored-response cap; the keyed
  transaction rolled back, nothing ran) is sent the same page once more unkeyed, and the result says
  ``ran_unkeyed``, but only when it answers the first attempt.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import nexus.db.http_vector_client as hv
from tests._module_seam import module_time
from tests.db import _scripted_vector_server as sv

_PATH = "/v1/vectors/gc/quarantine-restore"
_STATUS = "/v1/requests/"
_KEY = "idempotency-key"
_ORIGIN = "knowledge__kqr__bge-base-en-v15-768__v1"


def _chash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


_BODY = {"origin_collection": _ORIGIN, "quarantine_collection": None, "dry_run": False, "audit_id": 41,
         "audit_ids": [41, 42], "restored": 2, "rows": [], "source": None, "next_after": None}
_BUSY_BODY = {
    "error": "a manifest writer or an index run holds the collection's lock; nothing was moved, attached or audited",
    "reason": "quarantine_restore_busy", "retry_after_seconds": 5, "nothing_moved": True,
    "audit_ids": [], "moved_chashes": [],
}


class FakeClock:
    """The module's ``_now`` and ``time.sleep`` seam: a sleep advances the clock, nothing really waits."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
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


def _posts(srv):
    return srv.calls("POST", _PATH)


def _polls(srv):
    return srv.calls("GET", _STATUS)


def _status(state: str, **extra) -> sv.Reply:
    return sv.ok({"request_id": "", "state": state, **extra})


def _restore(client, **kw):
    return client.gc_quarantine_restore(_ORIGIN, chashes=[_chash("a"), _chash("b")], **kw)


# ── which calls are keyed ────────────────────────────────────────────────────


def test_a_real_run_is_one_keyed_post_whose_body_is_todays_body(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.ok(_BODY))
    assert _restore(client, reattach=False, actor="me") == _BODY
    (post,) = _posts(srv)
    assert uuid.UUID(post["headers"][_KEY]).version == 4
    assert json.loads(post["body"]) == {
        "origin_collection": _ORIGIN, "chashes": [_chash("a"), _chash("b")], "actor": "me", "reattach": False,
    }, "the key is the only addition: it rides in the header, never in the body"
    assert _polls(srv) == [] and clock.sleeps == []


def test_a_dry_run_is_not_keyed(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.ok(_BODY))
    _restore(client, dry_run=True)
    (post,) = _posts(srv)
    assert _KEY not in post["headers"], "the engine refuses a key on a dry run"
    assert json.loads(post["body"])["dry_run"] is True


def test_a_chashes_list_over_the_keyed_ceiling_is_not_keyed(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.ok(_BODY))
    over = [_chash(str(i)) for i in range(hv.QUARANTINE_RESTORE_KEYED_MAX_CHASHES + 1)]
    client.gc_quarantine_restore(_ORIGIN, chashes=over)
    (post,) = _posts(srv)
    assert _KEY not in post["headers"]
    assert len(json.loads(post["body"])["chashes"]) == len(over)


def test_exactly_the_keyed_ceiling_is_keyed(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.ok(_BODY))
    exact = [_chash(str(i)) for i in range(hv.QUARANTINE_RESTORE_KEYED_MAX_CHASHES)]
    client.gc_quarantine_restore(_ORIGIN, chashes=exact)
    (post,) = _posts(srv)
    assert _KEY in post["headers"]


@pytest.mark.parametrize("kwargs", [
    {"audit_id": 7, "offset": 0, "limit": 1000},
    {"quarantined_since": "2026-09-01T00:00:00Z", "limit": 1000},
], ids=["audit", "window"])
def test_the_cursor_sources_are_keyed_with_their_limit_untouched(srv, client, clock, kwargs):
    # The engine clamps a keyed `limit` to its ceiling; the client sends what the caller asked for.
    srv.script(f"POST {_PATH}", sv.ok(_BODY))
    client.gc_quarantine_restore(_ORIGIN, **kwargs)
    (post,) = _posts(srv)
    assert _KEY in post["headers"]
    assert json.loads(post["body"])["limit"] == 1000


# ── quarantine_restore_busy: nothing ran, the same key goes again ────────────


def test_a_busy_503_waits_retry_after_and_resends_the_same_key_and_bytes_without_polling(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.Reply(503, _BUSY_BODY, {"Retry-After": "3"}), sv.ok(_BODY))
    assert _restore(client) == _BODY
    assert clock.sleeps == [3.0]
    first, second = _posts(srv)
    assert first["headers"][_KEY] == second["headers"][_KEY] and first["body"] == second["body"]
    assert _polls(srv) == [], "a busy call rolled back whole: there is nothing to ask the ledger"


def test_a_busy_503_without_retry_after_waits_five_seconds(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.Reply(503, _BUSY_BODY), sv.ok(_BODY))
    _restore(client)
    assert clock.sleeps == [5.0]


def test_a_busy_that_never_clears_stops_at_three_resends_and_raises_the_engines_body(srv, client, clock):
    srv.script(f"POST {_PATH}", *[sv.Reply(503, _BUSY_BODY, {"Retry-After": "5"})] * 4)
    with pytest.raises(hv.VectorServiceError) as exc:
        _restore(client)
    assert exc.value.code == 503 and exc.value.reason == "quarantine_restore_busy"
    assert not isinstance(exc.value, hv.IdempotencyKeyRefusedError)
    body = exc.value.engine_body
    assert body["nothing_moved"] is True and body["audit_ids"] == [] and body["moved_chashes"] == []
    posts = _posts(srv)
    assert len(posts) == 4 and len({p["headers"][_KEY] for p in posts}) == 1
    assert clock.sleeps == [5.0, 5.0, 5.0] and _polls(srv) == []


@pytest.mark.parametrize("moved", [{"nothing_moved": False, "audit_ids": [91], "moved_chashes": [_chash("a")]},
                                   {"audit_ids": [91]}], ids=["something-moved", "field-absent"])
def test_a_busy_503_is_nothing_ran_only_when_the_engine_says_nothing_moved(srv, client, clock, moved):
    # Resending a call that part-ran is not what the typed busy answer promised; it is raised with the engine's body
    # (audit ids included), not resent and not polled.
    body = {**_BUSY_BODY, **moved}
    if "nothing_moved" not in moved:
        del body["nothing_moved"]
    srv.script(f"POST {_PATH}", sv.Reply(503, body, {"Retry-After": "5"}), sv.ok(_BODY))
    with pytest.raises(hv.VectorServiceError) as exc:
        _restore(client)
    assert exc.value.code == 503 and exc.value.reason == "quarantine_restore_busy"
    assert exc.value.engine_body["audit_ids"] == [91]
    assert len(_posts(srv)) == 1 and _polls(srv) == [] and clock.sleeps == []


def test_a_busy_503_that_says_nothing_moved_is_resent_even_after_an_earlier_resend(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.Reply(503, _BUSY_BODY), sv.Reply(503, _BUSY_BODY), sv.ok(_BODY))
    assert _restore(client) == _BODY
    assert len(_posts(srv)) == 3 and clock.sleeps == [5.0, 5.0]


def test_a_503_that_is_not_the_busy_reason_is_ambiguous_and_polls(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.err(503, "pool exhausted"))
    srv.script(f"GET {_STATUS}", _status("committed", response_status=200, response=_BODY))
    assert _restore(client) == _BODY
    assert len(_posts(srv)) == 1 and len(_polls(srv)) == 1


# ── an ambiguous outcome is settled by the ledger ────────────────────────────


@pytest.mark.parametrize("code", [502, 504])
def test_a_gateway_code_polls_and_committed_returns_the_stored_body(srv, client, clock, code):
    srv.script(f"POST {_PATH}", sv.err(code, "gw"))
    srv.script(f"GET {_STATUS}", _status("committed", response_status=200, response=_BODY))
    assert _restore(client) == _BODY
    (post,) = _posts(srv)
    (poll,) = _polls(srv)
    assert poll["path"] == _STATUS + post["headers"][_KEY]


@pytest.mark.parametrize("state", ["unknown", "rolled_back"])
def test_unknown_and_rolled_back_resend_the_same_key_and_bytes(srv, client, clock, state):
    srv.script(f"POST {_PATH}", sv.err(504, "gw"), sv.ok(_BODY))
    srv.script(f"GET {_STATUS}", _status(state))
    assert _restore(client) == _BODY
    first, second = _posts(srv)
    assert first["headers"][_KEY] == second["headers"][_KEY] and first["body"] == second["body"]


# ── old engines ──────────────────────────────────────────────────────────────


def test_an_engine_that_predates_the_key_guard_ignores_the_header_and_answers_as_before(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.ok(_BODY))
    assert _restore(client) == _BODY
    assert len(_posts(srv)) == 1 and _polls(srv) == [] and clock.sleeps == []


def test_an_engine_with_a_ledger_but_not_keyed_restore_is_sent_the_call_again_without_a_key(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.err(400, "idempotency_key_unsupported"), sv.ok(_BODY))
    assert _restore(client) == _BODY
    refused, again = _posts(srv)
    assert _KEY in refused["headers"] and _KEY not in again["headers"]
    assert refused["body"] == again["body"], "the same request, minus the key"
    assert _polls(srv) == [] and clock.sleeps == []


def test_the_first_attempt_refusal_is_marked_as_the_first_attempt(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.err(400, "idempotency_key_unsupported"))
    # Straight through _keyed_sweep_post: the flag the fallback reads.
    with pytest.raises(hv.IdempotencyKeyRefusedError) as exc:
        hv._keyed_sweep_post(_PATH, {"origin_collection": _ORIGIN}, tenant="default", gate_busy=False)
    assert exc.value.on_first_attempt is True


@pytest.mark.parametrize("first", ["gateway-then-rolled-back", "busy-then-resend"])
def test_an_unsupported_key_refusal_on_a_later_attempt_is_raised_never_sent_unkeyed(srv, client, clock, first):
    # The first attempt may have run (a lost 504, resolved by the ledger as rolled_back or unknown, or a busy resend):
    # if a later attempt is answered `idempotency_key_unsupported`, an unkeyed third POST would run the call twice.
    if first == "gateway-then-rolled-back":
        srv.script(f"POST {_PATH}", sv.err(504, "gw"), sv.err(400, "idempotency_key_unsupported"), sv.ok(_BODY))
        srv.script(f"GET {_STATUS}", _status("rolled_back"))
    else:
        srv.script(f"POST {_PATH}", sv.Reply(503, _BUSY_BODY), sv.err(400, "idempotency_key_unsupported"), sv.ok(_BODY))
    with pytest.raises(hv.IdempotencyKeyRefusedError) as exc:
        _restore(client)
    assert exc.value.code == 400 and exc.value.on_first_attempt is False
    posts = _posts(srv)
    assert len(posts) == 2 and all(_KEY in p["headers"] for p in posts), "no unkeyed POST was sent"
    assert any("earlier attempt" in n for n in getattr(exc.value, "__notes__", [])), "and the error says why"


def test_the_fallback_ran_nothing_twice_if_the_unkeyed_resend_fails_that_error_surfaces(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.err(400, "idempotency_key_unsupported"), sv.err(422, "unregistered_collection"))
    with pytest.raises(hv.VectorServiceError) as exc:
        _restore(client)
    assert exc.value.code == 422 and len(_posts(srv)) == 2


_OVER_CAP = {"error": "keyed_restore_body_over_cap", "body_bytes": 67343, "cap_bytes": 65536, "detail": "too big"}


def test_a_422_body_over_cap_on_the_first_attempt_is_sent_again_once_unkeyed_and_reported(srv, client, clock):
    # The keyed transaction rolled back and nothing ran; the unkeyed path stores no body, so no cap applies.
    srv.script(f"POST {_PATH}", sv.Reply(422, _OVER_CAP), sv.ok(_BODY))
    result = _restore(client)
    assert result == {**_BODY, "ran_unkeyed": "keyed_restore_body_over_cap"}, "the page says it ran unkeyed"
    refused, again = _posts(srv)
    assert _KEY in refused["headers"] and _KEY not in again["headers"]
    assert refused["body"] == again["body"], "the same request, minus the key"
    assert _polls(srv) == [] and clock.sleeps == []


def test_a_422_body_over_cap_is_sent_unkeyed_once_only_a_second_failure_surfaces(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.Reply(422, _OVER_CAP), sv.Reply(422, _OVER_CAP), sv.ok(_BODY))
    with pytest.raises(hv.VectorServiceError) as exc:
        _restore(client)
    assert exc.value.code == 422 and len(_posts(srv)) == 2, "one unkeyed resend, never a third POST"


@pytest.mark.parametrize("first", ["gateway-then-rolled-back", "busy-then-resend"])
def test_a_422_body_over_cap_on_a_later_attempt_is_raised_never_sent_unkeyed(srv, client, clock, first):
    # An earlier attempt may have run; an unkeyed POST after it could run the page twice.
    if first == "gateway-then-rolled-back":
        srv.script(f"POST {_PATH}", sv.err(504, "gw"), sv.Reply(422, _OVER_CAP), sv.ok(_BODY))
        srv.script(f"GET {_STATUS}", _status("rolled_back"))
    else:
        srv.script(f"POST {_PATH}", sv.Reply(503, _BUSY_BODY), sv.Reply(422, _OVER_CAP), sv.ok(_BODY))
    with pytest.raises(hv.KeyedRestoreBodyOverCapError) as exc:
        _restore(client)
    assert exc.value.code == 422 and exc.value.on_first_attempt is False
    posts = _posts(srv)
    assert len(posts) == 2 and all(_KEY in p["headers"] for p in posts), "no unkeyed POST was sent"
    assert any("earlier attempt" in n for n in getattr(exc.value, "__notes__", [])), "and the error says why"


@pytest.mark.parametrize("status, error", [(422, "idempotency_key_reused"), (400, "idempotency_key_malformed")])
def test_any_other_key_refusal_is_raised_and_never_resent(srv, client, clock, status, error):
    srv.script(f"POST {_PATH}", sv.err(status, error), sv.ok(_BODY))
    with pytest.raises(hv.IdempotencyKeyRefusedError) as exc:
        _restore(client)
    assert exc.value.code == status
    assert len(_posts(srv)) == 1 and _polls(srv) == []


def test_an_engine_without_a_ledger_fails_a_gateway_timeout_exactly_as_before(srv, client, clock):
    srv.script(f"POST {_PATH}", sv.err(504, "gw"))
    with pytest.raises(hv.VectorServiceError) as exc:
        _restore(client)
    assert exc.value.code == 504 and "HTTP 504" in str(exc.value)
    assert [r["method"] for r in srv.requests] == ["POST", "GET"]
