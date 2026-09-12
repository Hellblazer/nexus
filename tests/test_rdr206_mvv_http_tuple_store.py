"""RDR-206 MVV, the CLIENT half: both sequences through ``HttpTupleStore``.

The engine-direct half (``tests/test_rdr206_mvv_engine_direct.py``) proves the
engine honours the contract on the wire. This half proves the client speaks it:
``renew`` posts the route and returns what the engine said, ``ack(reply=)``
serialises a ``ReplySpec`` the engine accepts and hands back the reply id. A
defect on either side is invisible from the other, which is why the RDR asks for
both (bead nexus-h61dl.14).

WHAT THIS HALF ADDS over the engine-direct one. Sequence one there renews a
30 s lease and acks at once, so it shows the deadline moved but never needs it
to have. Here the ack is deliberately sent AFTER the original lease has lapsed,
on the engine's own deadline, so the renew is the only reason the ack succeeds:
``liveClaimRow`` requires ``lease_until > now``. Sequence two adds the refused
ack: a reply-carrying ack on a stale claim must write no reply, which is the
observable half of "one transaction" that an out-then-ack client would fail.

WHAT THIS HALF CANNOT REACH, stated rather than omitted. The claim log has no
HTTP route, so the ``claim, renew, ack`` rows with no ``expire`` are pinned
in-process by ``TupleRenewTest#theClaimLogOfARenewedThenAckedClaimReadsClaimRenewAck``.
"The reader wakes before the ack returns" is an ordering between two HTTP
responses on two sockets, and their arrival order is not the engine's order, so
asserting it here would test the scheduler; the engine fires the signal after
commit, which ``TupleAckWithReplyTest`` pins in-process.

MUTATIONS, each run red and then restored (recorded in T2 rdr-206-mvv-2026-09-12):
delete the ``renew`` call and sequence one raises ``ClaimNotFoundError`` at the
ack; replace ``ack(reply=)`` with ``out`` then ``ack`` and sequence two fails on
the reply id and the stale-claim test finds a reply row that should not exist.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from nexus.db.t2.http_tuple_store import (
    ClaimNotFoundError,
    HttpTupleStore,
    ReplySpec,
)
from nexus.db.t2.records import TupleRow

#: A hang bound, not a performance assertion (nexus-61vos). The parked read asks
#: for 20 s, so anything past this is a wedge rather than a slow box.
_HANG_S = 60.0

#: The original lease. Short so the test waits past it quickly; the renew below
#: grants far more than the wait, so load on the box cannot land the ack outside
#: the renewed lease.
_SHORT_LEASE_S = 2
_RENEW_LEASE_S = 120

#: How far past the engine's ORIGINAL deadline the ack is sent. Client and engine
#: share this box's clock in ``t2_service_env``, so this margin only has to cover
#: timestamp rounding, not clock skew.
_PAST_DEADLINE_MARGIN_S = 0.5


def _stamp() -> str:
    return f"{os.getpid()}-{time.monotonic_ns()}"


def _parse(ts: str | None) -> datetime:
    assert ts, "the engine must render lease_until on a claimed tuple"
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _sleep_until(deadline: datetime) -> None:
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


@pytest.mark.usefixtures("t2_service_env")
class TestRdr206MvvThroughHttpTupleStore:
    """RDR-206 MVV, both sequences, through the client."""

    def test_renew_lets_an_ack_succeed_after_the_original_lease_lapsed(self) -> None:
        """Sequence one: claim on a short lease, renew, ack past the old deadline.

        Without the renew the ack below lands on a claim whose ``lease_until`` is
        in the past, and the engine answers ``ClaimNotFound``. With it the ack
        succeeds and the request is consumed. The wait is driven by the deadline
        the ENGINE returned on the claim, not by a local ``now + lease_s``.
        """
        store = HttpTupleStore()
        to = f"mvv-client-renew-{_stamp()}"
        subspace = f"mailbox/{to}"
        store.out(subspace, {"to": to}, {"from": "mvv-asker"}, "renew me", nonce=f"n-{to}")

        claimed = store.inp(subspace, {"to": to}, claimant="mvv-worker", lease_s=_SHORT_LEASE_S)
        assert claimed is not None, "the request must be claimable"
        row, claim_id = claimed
        original_deadline = _parse(row.lease_until)

        renewed_until = store.renew(claim_id, "mvv-worker", _RENEW_LEASE_S)
        assert renewed_until.tzinfo is not None, "renew returns an aware datetime"
        assert renewed_until > original_deadline + timedelta(seconds=_RENEW_LEASE_S // 2), (
            f"the renew must extend well past the original lease: "
            f"{original_deadline} -> {renewed_until}"
        )

        _sleep_until(original_deadline + timedelta(seconds=_PAST_DEADLINE_MARGIN_S))
        now = datetime.now(timezone.utc)
        assert now > original_deadline, "precondition: the ORIGINAL lease has lapsed"
        if now >= renewed_until:
            pytest.skip(
                f"the box stalled past the renewed lease ({renewed_until}); a stall is "
                f"not a statement about renew (nexus-61vos)"
            )

        assert store.ack(claim_id, "mvv-worker") is None, "no reply sent, no reply id"

        census = store.subspace_stats(subspace)
        assert census.consumed == 1, census
        assert census.claimed == 0, census

    def test_ack_with_reply_wakes_a_parked_reader_and_consumes_the_request(self) -> None:
        """Sequence two: B acks A's request with a reply; A's parked reader wakes.

        The reply address is proven EMPTY before the reader parks, so a returned
        row can only have arrived by the wake. The row the reader gets must be
        the row whose id ``ack`` returned, and the request must be consumed.
        """
        store = HttpTupleStore()
        stamp = _stamp()
        asker, answerer = f"mvv-client-asker-{stamp}", f"mvv-client-answerer-{stamp}"
        req_subspace, reply_subspace = f"mailbox/{answerer}", f"mailbox/{asker}"

        store.out(req_subspace, {"to": answerer}, {"from": asker}, "the question", nonce=f"n-{stamp}")
        claimed = store.inp(req_subspace, {"to": answerer}, claimant="mvv-worker", lease_s=60)
        assert claimed is not None
        _, claim_id = claimed

        assert store.rdp(reply_subspace, {"to": asker}) == [], (
            "precondition: the reply address starts empty, so the reader must wake"
        )

        parked: dict[str, list[TupleRow]] = {}

        def _park() -> None:
            parked["rows"] = HttpTupleStore().rd(reply_subspace, {"to": asker}, n=1, timeout_s=20)

        reader = threading.Thread(target=_park, daemon=True)
        reader.start()
        time.sleep(0.5)  # let it reach the park before the reply lands

        reply_id = store.ack(
            claim_id,
            "mvv-worker",
            reply=ReplySpec(
                subspace=reply_subspace,
                keys={"to": asker},
                dims={"from": answerer},
                body="the answer",
            ),
        )
        assert reply_id and len(reply_id) == 64, f"ack returns the reply's hex id: {reply_id!r}"

        reader.join(timeout=_HANG_S)
        if reader.is_alive():
            pytest.skip(f"the parked reader did not return within {_HANG_S}s; a wedge (nexus-61vos)")
        rows = parked["rows"]
        assert len(rows) == 1, f"the parked reader received the reply: {rows}"
        assert rows[0].id == reply_id, "the row read is the reply ack reported writing"
        assert rows[0].body == "the answer"

        census = store.subspace_stats(req_subspace)
        assert census.consumed == 1, census
        assert census.claimed == 0, census

    def test_a_refused_ack_writes_no_reply(self) -> None:
        """The other half of one transaction: an ack that fails writes nothing.

        The claim is released with ``nack`` so its claim id is stale, then acked
        with a VALID reply. The engine validates the reply, then fails to consume
        the claim, and rolls back: ``ClaimNotFoundError``, and the reply address
        stays empty. A client that wrote the reply with ``out`` and then acked
        would leave a reply row behind for a request nobody consumed.
        """
        store = HttpTupleStore()
        stamp = _stamp()
        asker, answerer = f"mvv-client-asker-{stamp}", f"mvv-client-answerer-{stamp}"
        req_subspace, reply_subspace = f"mailbox/{answerer}", f"mailbox/{asker}"

        store.out(req_subspace, {"to": answerer}, {"from": asker}, "the question", nonce=f"n-{stamp}")
        claimed = store.inp(req_subspace, {"to": answerer}, claimant="mvv-worker", lease_s=60)
        assert claimed is not None
        _, claim_id = claimed
        store.nack(claim_id, "mvv-worker")

        with pytest.raises(ClaimNotFoundError):
            store.ack(
                claim_id,
                "mvv-worker",
                reply=ReplySpec(
                    subspace=reply_subspace,
                    keys={"to": asker},
                    dims={"from": answerer},
                    body="an answer to a request nobody holds",
                ),
            )

        assert store.rdp(reply_subspace, {"to": asker}) == [], "a refused ack wrote a reply"
        census = store.subspace_stats(req_subspace)
        assert census.consumed == 0, census
        assert census.available == 1, census
