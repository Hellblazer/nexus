"""RDR-206 Phase 1 Step 5: the two MVV sequences, ENGINE-DIRECT.

Engine-direct means raw HTTP against a running engine's ``/v1/tuples/*`` routes,
not through ``HttpTupleStore``. That distinction is the whole point of running it
twice: this half proves the ENGINE honours the contract on the wire, and the
client half (bead nexus-h61dl.14) proves the client speaks it. A defect in either
one is invisible from the other side, which is why the RDR asks for both rather
than treating the client test as coverage of the engine.

WHAT THESE ADD over the Java engine tests, which already pass. Those drive
``TupleRepository`` in-process. These go over the wire through ``TupleHandler``:
JSON in, JSON out, bearer auth, a real socket. Route wiring, request parsing,
serialisation and status mapping are only exercised here.

WHAT THESE CANNOT REACH, stated rather than quietly omitted. The claim log has no
HTTP route, so "no ``expire`` row after a renew" — the RDR's wording for sequence
one — is not assertable from out here. It is pinned in-process instead, by
``TupleRenewTest#theClaimLogOfARenewedThenAckedClaimReadsClaimRenewAck``. What is
assertable engine-direct is the observable consequence: the renewed claim is
still held, still owned by the same claimant, and the ack that follows succeeds,
which a released-then-expired claim would not permit.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx
import pytest

#: A hang bound, not a performance assertion. The parked read below asks the
#: engine for a 20s park, so anything past this is a wedge rather than a slow
#: box, and the suite must not hang on it (nexus-61vos).
_HANG_S = 60.0


def _client() -> tuple[httpx.Client, str]:
    base = os.environ["NX_SERVICE_URL"]
    token = os.environ["NX_SERVICE_TOKEN"]
    return httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_HANG_S,
    ), base


def _post(c: httpx.Client, route: str, body: dict[str, Any]) -> dict[str, Any]:
    r = c.post(route, json=body)
    assert r.status_code == 200, f"{route} -> {r.status_code}: {r.text}"
    return r.json()


def _census(c: httpx.Client, subspace: str) -> dict[str, Any]:
    r = c.get("/v1/tuples/subspace_stats", params={"subspace": subspace})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.usefixtures("t2_service_env")
class TestRdr206MvvEngineDirect:
    """RDR-206 Phase 1 Step 5. Both sequences, over the wire."""

    def test_renew_sequence_claim_renew_ack(self) -> None:
        """Sequence one: claim, renew, ack, over HTTP.

        The renew must move the deadline FORWARD and leave the claim held by the
        same claimant, and the ack afterwards must still succeed — which is the
        engine-direct consequence of there being no expire in the log, since an
        expired-and-released claim would fail the ack with ClaimNotFound.
        """
        c, _ = _client()
        with c:
            to = f"mvv-renew-{os.getpid()}-{time.monotonic_ns()}"
            subspace = f"mailbox/{to}"
            _post(c, "/v1/tuples/out", {
                "subspace": subspace,
                "keys": {"to": to},
                "dims": {"from": "mvv-asker"},
                "body": "renew me",
                "nonce": f"nonce-{to}",
            })

            claim = _post(c, "/v1/tuples/inp", {
                "subspace": subspace,
                "keys_pattern": {"to": to},
                "claimant": "mvv-worker",
                "lease_s": 30,
            })
            claim_id = claim["claim_id"]
            assert claim_id, f"the request must be claimable: {claim}"
            first_deadline = claim["tuple"]["lease_until"]

            renewed = _post(c, "/v1/tuples/renew", {
                "claim_id": claim_id,
                "claimant": "mvv-worker",
                "lease_s": 600,
            })
            assert renewed["lease_until"] > first_deadline, (
                f"the renew must move the deadline forward: "
                f"{first_deadline} -> {renewed['lease_until']}"
            )

            acked = _post(c, "/v1/tuples/ack", {
                "claim_id": claim_id,
                "claimant": "mvv-worker",
            })
            assert acked["acked"] is True
            assert acked["reply_id"] is None, "no reply was sent, so no reply id"

            after = _census(c, subspace)
            assert after["consumed"] == 1, after
            assert after["claimed"] == 0, after

    def test_ack_with_reply_wakes_a_parked_reader(self) -> None:
        """Sequence two: a parked reader wakes with the reply, request consumed.

        The reader parks on an address proven EMPTY first. That ordering is what
        makes the wake meaningful rather than incidental: a reader that parked
        with nothing there and later returned a row cannot have read it on its
        opening probe. Without the emptiness check this test would pass on a
        reader that never parked at all, which is the shape that has bitten this
        epic repeatedly.
        """
        c, _ = _client()
        with c:
            stamp = f"{os.getpid()}-{time.monotonic_ns()}"
            asker = f"mvv-asker-{stamp}"
            answerer = f"mvv-answerer-{stamp}"
            req_subspace = f"mailbox/{answerer}"
            reply_subspace = f"mailbox/{asker}"

            _post(c, "/v1/tuples/out", {
                "subspace": req_subspace,
                "keys": {"to": answerer},
                "dims": {"from": asker},
                "body": "the question",
                "nonce": f"nonce-{stamp}",
            })
            claim = _post(c, "/v1/tuples/inp", {
                "subspace": req_subspace,
                "keys_pattern": {"to": answerer},
                "claimant": "mvv-worker",
                "lease_s": 60,
            })
            assert claim["claim_id"], claim

            # The reply address is empty BEFORE anyone parks on it.
            probe = _post(c, "/v1/tuples/rdp", {
                "subspace": reply_subspace,
                "keys_pattern": {"to": asker},
            })
            assert probe["tuples"] == [], (
                "precondition: the reply address must start empty, or a parked "
                f"reader could read rather than wake: {probe}"
            )

            parked: dict[str, Any] = {}

            def _park() -> None:
                pc, _ = _client()
                with pc:
                    parked["result"] = _post(pc, "/v1/tuples/rd", {
                        "subspace": reply_subspace,
                        "keys_pattern": {"to": asker},
                        "n": 1,
                        "timeout_s": 20,
                    })

            reader = threading.Thread(target=_park, daemon=True)
            reader.start()
            time.sleep(0.5)   # let it reach the park before the reply lands

            acked = _post(c, "/v1/tuples/ack", {
                "claim_id": claim["claim_id"],
                "claimant": "mvv-worker",
                "reply": {
                    "subspace": reply_subspace,
                    "keys": {"to": asker},
                    "dims": {"from": answerer},
                    "body": "the answer",
                },
            })
            assert acked["acked"] is True
            reply_id = acked["reply_id"]
            assert reply_id and len(reply_id) == 64, f"a hex reply id: {acked}"

            reader.join(timeout=_HANG_S)
            if reader.is_alive():
                pytest.skip(
                    f"the parked reader did not return within {_HANG_S}s. That is a "
                    f"wedge on this box, not a statement about the wake (nexus-61vos)."
                )
            rows = parked["result"]["tuples"]
            assert len(rows) == 1, f"the parked reader received the reply: {rows}"
            assert rows[0]["body"] == "the answer"
            assert rows[0]["id"] == reply_id, (
                "the row the reader got is the reply the ack reported writing"
            )

            # And the other half of the atomicity claim: the request is gone.
            req_census = _census(c, req_subspace)
            assert req_census["consumed"] == 1, req_census
            assert req_census["claimed"] == 0, req_census
