# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpRekeyClient's rekey surface (RDR-180 Item6).

httpx.MockTransport idiom. RDR-155 P4b: the HttpRemapStore machinery
(record_batch / all_pairs / CompositeReadMapStore / seed_local_map /
run_batched_etl) died with the migration package; the rekey surface was
rehomed to ``nexus.db.t2.rekey_client.HttpRekeyClient`` (D-D split) and
these tests re-point there — the wire contract they pin is unchanged.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.db.t2.rekey_client import (
    HttpRekeyClient,
    RekeyJobFailedError,
    RekeyJobLostError,
    RekeyJobTimeoutError,
)

TOKEN = "fake-rekey-token"


def _store_with_handler(handler) -> HttpRekeyClient:
    store = HttpRekeyClient(base_url="http://svc", _token=TOKEN)
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    return store


# ── nexus-b878d: the rekey is submitted and polled, never awaited ────────────
#
# The endpoint answers 202 + job_id and the envelope arrives by poll, because
# synchronously the rekey outlived the tls sidecar's ~120s proxy_read_timeout
# (gate-xr789: 504 at 120.3s over a transaction that committed 88s later). The
# wait lives inside HttpRemapStore.rekey so the rung's (orphan_policy) ->
# envelope contract is untouched; these pin that absorption.


ENVELOPE = {"rehashed": 12, "collapsed_duplicates": 0, "residual_mismatched": 0}


def _rekey_handler(poll_states, *, seen=None):
    """POST /rekey -> 202 {job_id}; each GET pops the next state."""
    states = list(poll_states)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/remap/rekey":
            if seen is not None and request.content:
                seen.append(("body", json.loads(request.content)))
            return httpx.Response(202, json={"job_id": "abc123-job", "status": "running"})
        if request.method == "GET" and request.url.path == "/v1/remap/rekey/abc123-job":
            return httpx.Response(200, json=states.pop(0))
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    return handler


def test_rekey_submits_then_polls_until_the_envelope_arrives():
    seen: list = []
    store = _store_with_handler(_rekey_handler(
        [{"status": "running"}, {"status": "running"},
         {"status": "succeeded", "envelope": ENVELOPE}],
        seen=seen,
    ))

    assert store.rekey(poll_interval_s=0) == ENVELOPE

    calls = [c for c in seen if c[0] in {"POST", "GET"}]
    assert calls[0] == ("POST", "/v1/remap/rekey"), "submit first"
    assert all(c == ("GET", "/v1/remap/rekey/abc123-job") for c in calls[1:])
    assert len(calls) == 4, "one submit + three polls to reach terminal"


def test_rekey_passes_the_orphan_policy_through_the_submit():
    seen: list = []
    store = _store_with_handler(_rekey_handler(
        [{"status": "succeeded", "envelope": ENVELOPE}], seen=seen,
    ))
    store.rekey("synthesize", poll_interval_s=0)
    bodies = [c[1] for c in seen if c[0] == "body"]
    assert bodies == [{"orphan_policy": "synthesize"}]


def test_rekey_raises_loudly_when_the_job_reports_failed():
    store = _store_with_handler(_rekey_handler(
        [{"status": "failed", "error": "legacy id maps to two digests"}],
    ))
    with pytest.raises(RekeyJobFailedError, match="two digests"):
        store.rekey(poll_interval_s=0)


def test_rekey_timeout_is_could_not_tell_and_names_the_job():
    """The distinction the 504 collapsed: still-running is NOT known-failed."""
    store = _store_with_handler(_rekey_handler([{"status": "running"}]))
    with pytest.raises(RekeyJobTimeoutError) as excinfo:
        store.rekey(timeout_s=0, poll_interval_s=0)
    message = str(excinfo.value)
    assert "abc123-job" in message, "the operator needs the id to follow up"
    assert "may still be in flight" in message
    assert "event=rekey_complete" in message, "points at the authoritative record"


def test_rekey_against_a_pre_b878d_engine_returns_its_synchronous_envelope():
    """Version skew converges rather than refusing: an older engine did the
    work during the POST and handed back the envelope, which IS the result."""
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=ENVELOPE)

    store = _store_with_handler(handler)
    assert store.rekey(poll_interval_s=0) == ENVELOPE
    assert seen == [("POST", "/v1/remap/rekey")], "no poll against a sync engine"


def test_rekey_lost_to_an_engine_restart_gets_its_own_type():
    """The 410 case is could-not-tell and must be distinguishable.

    The mixin's _raise_for_status turns every non-2xx into a bare
    HTTPStatusError, which would flatten the ONE response carrying a
    structured could-not-tell payload into "some transport error" —
    indistinguishable from a connection fault. It gets a type, like
    failed and timeout do, and the engine's detail survives.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "abc123-job", "status": "running"})
        return httpx.Response(410, json={
            "error": "job belongs to a previous engine instance: the engine restarted "
                     "and this job's outcome is not in the current instance's memory. "
                     "It most likely rolled back, but that is NOT guaranteed. The rekey "
                     "is idempotent, so re-submitting is safe and self-answering.",
            "status": "lost",
            "store_changed": "unknown",
        })

    store = _store_with_handler(handler)
    with pytest.raises(RekeyJobLostError) as excinfo:
        store.rekey(poll_interval_s=0)

    message = str(excinfo.value)
    assert "abc123-job" in message
    assert "NOT guaranteed" in message, "the engine's honesty survives the translation"
    assert "idempotent" in message, "and so does the recovery instruction"


def test_a_non_410_poll_error_still_propagates_untyped():
    """Only 410 is reinterpreted; a real transport fault must not be dressed
    up as a lost job."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "abc123-job", "status": "running"})
        return httpx.Response(500, json={"error": "internal server error"})

    store = _store_with_handler(handler)
    with pytest.raises(httpx.HTTPStatusError):
        store.rekey(poll_interval_s=0)


def test_rekey_rejects_an_unrecognized_status_rather_than_looping():
    store = _store_with_handler(_rekey_handler([{"status": "sideways"}]))
    with pytest.raises(RekeyJobFailedError, match="unrecognized status"):
        store.rekey(poll_interval_s=0)


def test_rekey_conflict_on_the_poll_is_typed_as_known_failed():
    """A legacy-id collision is known-failed regardless of which status code
    the engine used to say so.

    The engine answers that case 409 rather than 200-with-status-failed, and
    _raise_for_status fires before the status dispatch — so without explicit
    translation the SAME outcome would reach callers as two different
    exception types depending purely on the server's choice of code.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "abc123-job", "status": "running"})
        return httpx.Response(409, json={
            "error": "legacy id 'deadbeef' maps to two distinct content digests",
        })

    store = _store_with_handler(handler)
    with pytest.raises(RekeyJobFailedError, match="two distinct content digests"):
        store.rekey(poll_interval_s=0)


def test_a_job_id_less_2xx_that_is_not_an_envelope_fails_loud():
    """The version-skew branch must not accept any job_id-less body as a
    finished rekey — absence of a key is too weak a signal to return a result
    on. A real legacy envelope is recognisable; a bare {} is not."""
    def envelope_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ENVELOPE)

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    assert _store_with_handler(envelope_handler).rekey(poll_interval_s=0) == ENVELOPE

    with pytest.raises(RekeyJobFailedError, match="refusing to guess"):
        _store_with_handler(empty_handler).rekey(poll_interval_s=0)
