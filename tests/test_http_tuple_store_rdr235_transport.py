# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 (bead nexus-5r2sc.3.2): the client transport for the engine's new
tuple fields, on ``HttpTupleStore``.

``out`` takes ``delay_s`` and ``mode="replace"``; ``nack`` takes ``retry_after_s``
and ``dead``. Each is sent only when the caller sets it, so an unset call is
byte-identical to the payload before this change (the engine's wire-contract
entries, engine-service-v0.1.169, say an older engine IGNORES an unknown key and
answers 200, so the client floor, not a probe, is what makes sending them safe).

Two layers: payload-shape tests over an ``httpx.MockTransport`` (what goes on
the wire, with and without the fields), and the same fields against the real
engine substrate (the engine honours them, and ``out_replace`` reads the outcome
it reports).
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from nexus.db.t2.http_tuple_store import HttpTupleStore


def _store_over(handler) -> HttpTupleStore:
    store = HttpTupleStore(base_url="http://mock.test", _token="test-token")
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    return store


class _Recorder:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/tuples")
        self.calls.append((path, json.loads(request.content)))
        return httpx.Response(200, json=self.responses[path])


class TestOutPayload:
    def test_an_unset_out_is_byte_identical_to_the_old_payload(self) -> None:
        rec = _Recorder({"/out": {"id": "ab"}})
        store = _store_over(rec)
        store.out("queue/x", {"name": "x"}, {"from": "a"}, "b", nonce="n", ttl_seconds=5)
        (_, payload), = rec.calls
        assert payload == {
            "subspace": "queue/x", "keys": {"name": "x"}, "dims": {"from": "a"},
            "body": "b", "nonce": "n", "ttl_seconds": 5,
        }
        assert "delay_s" not in payload and "mode" not in payload

    def test_delay_s_is_sent_when_set_including_zero(self) -> None:
        rec = _Recorder({"/out": {"id": "ab"}})
        store = _store_over(rec)
        store.out("queue/x", {"name": "x"}, nonce="n", delay_s=30)
        store.out("queue/x", {"name": "x"}, nonce="n2", delay_s=0)
        assert [p["delay_s"] for _, p in rec.calls] == [30, 0]

    def test_out_replace_sends_mode_and_returns_the_outcome(self) -> None:
        rec = _Recorder({"/out": {"id": "ab", "outcome": "replaced"}})
        store = _store_over(rec)
        got = store.out_replace("aspects", {"queue": "aspects"}, None, "{}", nonce="1.2.3", delay_s=7)
        assert got == ("ab", "replaced")
        (_, payload), = rec.calls
        assert payload["mode"] == "replace" and payload["delay_s"] == 7
        assert payload["nonce"] == "1.2.3"

    def test_out_replace_refuses_an_answer_with_no_outcome(self) -> None:
        """An engine that ignores ``mode`` runs a plain out and answers 200 with
        only an id. That is not a replace and must not be reported as one."""
        store = _store_over(_Recorder({"/out": {"id": "ab"}}))
        with pytest.raises(RuntimeError, match="engine-service-v0.1.169"):
            store.out_replace("aspects", {"queue": "aspects"}, None, "{}", nonce="1.2.3")

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"mode": "append"}, id="unknown-mode"),
            pytest.param({"delay_s": -1}, id="negative-delay"),
            pytest.param({"delay_s": 1.5}, id="fractional-delay"),
            pytest.param({"delay_s": True}, id="bool-delay"),
        ],
    )
    def test_bad_values_are_refused_before_sending(self, kwargs: dict) -> None:
        rec = _Recorder({"/out": {"id": "ab"}})
        store = _store_over(rec)
        with pytest.raises(ValueError):
            store.out("queue/x", {"name": "x"}, nonce="n", **kwargs)
        assert rec.calls == []

    def test_out_replace_without_a_nonce_is_refused(self) -> None:
        store = _store_over(_Recorder({"/out": {"id": "ab", "outcome": "created"}}))
        with pytest.raises(ValueError, match="nonce"):
            store.out_replace("aspects", {"queue": "aspects"}, None, "{}", nonce="")


class TestNackPayload:
    def test_a_plain_nack_is_byte_identical_to_the_old_payload(self) -> None:
        rec = _Recorder({"/nack": {}})
        store = _store_over(rec)
        store.nack("cid", "w:0")
        assert rec.calls == [("/nack", {"claim_id": "cid", "claimant": "w:0"})]

    def test_retry_after_s_and_dead_are_sent_when_set(self) -> None:
        rec = _Recorder({"/nack": {}})
        store = _store_over(rec)
        store.nack("cid", "w:0", retry_after_s=0)
        store.nack("cid", "w:0", retry_after_s=60)
        store.nack("cid", "w:0", dead=True)
        assert [p for _, p in rec.calls] == [
            {"claim_id": "cid", "claimant": "w:0", "retry_after_s": 0},
            {"claim_id": "cid", "claimant": "w:0", "retry_after_s": 60},
            {"claim_id": "cid", "claimant": "w:0", "dead": True},
        ]

    def test_dead_false_sends_no_key(self) -> None:
        rec = _Recorder({"/nack": {}})
        store = _store_over(rec)
        store.nack("cid", "w:0", dead=False)
        assert "dead" not in rec.calls[0][1]

    @pytest.mark.parametrize("bad", [-1, 2.5, True])
    def test_bad_retry_after_is_refused_before_sending(self, bad) -> None:
        rec = _Recorder({"/nack": {}})
        store = _store_over(rec)
        with pytest.raises(ValueError):
            store.nack("cid", "w:0", retry_after_s=bad)
        assert rec.calls == []


@pytest.mark.usefixtures("t2_service_env")
class TestAgainstTheEngine:
    """The same fields against the real engine. The ``aspects`` template is
    keys+nonce with take enabled, which is what every case needs."""

    @staticmethod
    def _doc() -> str:
        return f"9.{uuid.uuid4().int % 10**9}.1"

    def test_replace_reports_created_then_replaced_on_one_row(self) -> None:
        store = HttpTupleStore()
        doc = self._doc()
        keys = {"queue": "aspects"}
        first = store.out_replace("aspects", keys, None, '{"v":1}', nonce=doc)
        second = store.out_replace("aspects", keys, None, '{"v":2}', nonce=doc)
        assert first[1] == "created" and second[1] == "replaced"
        assert first[0] == second[0], "one document is one task id"
        rows = [r for r in store.rd("aspects", keys, n=300) if r.id == first[0]]
        assert len(rows) == 1 and rows[0].body == '{"v":2}'

    def test_delay_and_the_nack_fields_reach_the_engine(self) -> None:
        """``delay_s``, ``nack(dead=True)``, ``nack(retry_after_s=)`` and a plain
        ``nack`` on one queue, one branch at a time; each assertion names its own.
        The delayed task is written FIRST, so a ``delay_s`` the client dropped
        would be claimed ahead of the dead-letter task."""
        store = HttpTupleStore()
        keys = {"queue": "aspects"}
        delayed = store.out("aspects", keys, None, "{}", nonce=self._doc(), delay_s=3600)
        dead_id = store.out("aspects", keys, None, "{}", nonce=self._doc())

        # delay_s: the immediate task is the one claimed, the delayed one is not.
        row, claim = store.inp("aspects", keys, claimant="rdr235-dead", lease_s=60)
        assert row.id == dead_id, f"task {delayed} was delayed an hour and must not be claimed first"

        # nack(dead=True): dead-lettered at once.
        store.nack(claim, "rdr235-dead", dead=True)
        census = store.subspace_stats("aspects")
        assert (census.dead, census.available, census.total) == (1, 1, 2), census
        assert store.inp("aspects", keys, claimant="rdr235-delay", lease_s=60) is None, (
            "the delayed task and the dead one are both unclaimable"
        )

        # nack(retry_after_s=): held back, not dead.
        held_id = store.out("aspects", keys, None, "{}", nonce=self._doc())
        row, claim = store.inp("aspects", keys, claimant="rdr235-retry", lease_s=60)
        assert row.id == held_id
        store.nack(claim, "rdr235-retry", retry_after_s=3600)
        assert store.inp("aspects", keys, claimant="rdr235-retry", lease_s=60) is None, (
            "retry_after_s held the task back"
        )
        census = store.subspace_stats("aspects")
        assert (census.dead, census.total) == (1, 3), census

        # A plain nack returns the task at once.
        plain_id = store.out("aspects", keys, None, "{}", nonce=self._doc())
        row, claim = store.inp("aspects", keys, claimant="rdr235-plain", lease_s=60)
        assert row.id == plain_id
        store.nack(claim, "rdr235-plain")
        again = store.inp("aspects", keys, claimant="rdr235-plain", lease_s=60)
        assert again is not None and again[0].id == plain_id, "a plain nack is claimable at once"
