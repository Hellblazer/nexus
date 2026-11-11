# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The claim renewer covers a claim from the moment it is taken to its last verdict (RDR-235).

``TupleQueueSide.process`` once started the renewer after the per-claim catalog
checks and stopped it before the loop that writes results and acks or nacks. A
claim was unrenewed from ``claimed_at`` to the first renewal, and again from the
last renewal to its write. The engine refuses renew, ack, nack and the write
fence after ``lease_until``, with no resurrection, so a stall in either window
lapsed a claim a live worker still held (the MVV journey's "a write met a claim
that was not live", 2 s lease and 0.5 s renewal).

These run on a virtual clock and a fake engine that keeps the lease: a claim is
live while ``clock() < lease_until``, a renewal moves ``lease_until`` to
``clock() + lease``, and every other call on a lapsed claim raises
``ClaimNotFoundError``, as the engine does. A stall is a few clock steps of one
renew interval each, with the renewer given time to run between steps, so the
virtual clock sees the renewals a real stall would. No tenant, no engine.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from nexus.aspect_extractor import AspectRecord
from nexus.aspect_worker_tuples import TupleQueueSide
from nexus.db.t2.http_tuple_store import ClaimNotFoundError
from tests._settle import FakeClock

_LEASE_S = 2
_RENEW_S = 1.0
_STALL_S = 3 * _RENEW_S
_COLL = "knowledge__renewer-span__bge-768__v1"


class _LeaseEngine:
    """The slice of the tuple store the side uses, with the engine's lease rule."""

    def __init__(self, clock: FakeClock, n: int) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.rows = [
            SimpleNamespace(
                id=f"task{i}", attempts=0,
                body=json.dumps({
                    "collection": _COLL, "source_path": f"/doc-{i}.md",
                    "doc_id": f"1.1.{i}", "content_hash": "h",
                }),
            )
            for i in range(n)
        ]
        self.lease_until: dict[str, float] = {}
        #: ``(op, claim_id)`` in call order; a call on a lapsed claim records ``("lapsed:" + op, claim_id)``.
        self.ops: list[tuple[str, str]] = []

    def live(self, op: str, claim_id: str) -> None:
        with self._lock:
            until = self.lease_until.get(claim_id)
            if until is None or self._clock() >= until:
                self.ops.append((f"lapsed:{op}", claim_id))
                raise ClaimNotFoundError(f"{op}: no live claim {claim_id}")

    def inp(self, subspace: str, keys: dict, *, claimant: str, lease_s: int) -> Any:
        with self._lock:
            if not self.rows:
                return None
            row = self.rows.pop(0)
            claim_id = f"claim-{row.id}"
            self.lease_until[claim_id] = self._clock() + lease_s
            return row, claim_id

    def renew(self, claim_id: str, claimant: str, lease_s: int) -> None:
        self.live("renew", claim_id)
        with self._lock:
            self.lease_until[claim_id] = self._clock() + lease_s
            self.ops.append(("renew", claim_id))

    def consume(self, op: str, claim_id: str) -> None:
        self.live(op, claim_id)
        with self._lock:
            del self.lease_until[claim_id]
            self.ops.append((op, claim_id))

    def ack(self, claim_id: str, claimant: str, reply: Any = None) -> None:
        self.consume("ack", claim_id)

    def nack(self, claim_id: str, claimant: str, *, retry_after_s: Any = None, dead: bool = False) -> None:
        self.consume("nack", claim_id)

    def release(self, claim_id: str, claimant: str) -> None:
        self.consume("release", claim_id)

    def lapsed(self) -> list[tuple[str, str]]:
        with self._lock:
            return [op for op in self.ops if op[0].startswith("lapsed:")]

    def index(self, op: str, claim_id: str) -> int:
        with self._lock:
            return self.ops.index((op, claim_id))

    def has(self, op: str, claim_id: str) -> bool:
        with self._lock:
            return (op, claim_id) in self.ops


def _stall(clock: FakeClock, seconds: float = _STALL_S, step: float = _RENEW_S) -> None:
    """Hold the calling thread for *seconds* of virtual time, one renew interval at a time.

    A renewer parked on the clock gets to run its round after each step, as it would across a real
    stall. With no renewer parked (the code under test has not started one) the clock just moves."""
    for _ in range(round(seconds / step)):
        end = time.monotonic() + 0.3
        while clock.waiters() < 1 and time.monotonic() < end:
            time.sleep(0.005)
        renewer_parked = clock.waiters() >= 1
        clock.advance(step)
        if renewer_parked:
            clock.settle_waiters(parked=1, deadline=5.0)


def _record(claim: Any) -> AspectRecord:
    assert claim.row is not None
    return AspectRecord(
        collection=claim.row.collection, source_path=claim.row.source_path,
        problem_formulation="P", proposed_method="M", experimental_datasets=["d"],
        experimental_baselines=["b"], experimental_results="R", extras={}, confidence=0.9,
        extracted_at="2026-11-11T00:00:00+00:00", model_version="test", extractor_name="test",
    )


def _side(
    engine: _LeaseEngine, clock: FakeClock, *, batch_size: int,
    document_sensitive: Callable[[str], bool] | None = None,
    before_write: Callable[[str], None] | None = None,
) -> TupleQueueSide:
    def complete(fields: dict[str, Any], claim_id: str, claimant: str) -> None:
        if before_write is not None:
            before_write(claim_id)
        engine.consume("write", claim_id)  # the engine's persist-and-ack: one call, fenced on the claim

    return TupleQueueSide(
        worker_id="w", batch_size=batch_size, tuples_call=lambda fn: fn(engine),
        complete=complete,
        extract=lambda claims: [_record(c) for c in claims],
        on_self_fault=lambda reason, claim, exc: None,
        lease_s=_LEASE_S, renew_every_s=_RENEW_S, deadline_s=lambda n: 3600.0,
        document_sensitive=document_sensitive, clock=clock,
    )


def test_a_stalled_catalog_check_does_not_lapse_the_claim() -> None:
    """The window from the claim to the first renewal: ``document_sensitive`` takes three renew intervals."""
    clock = FakeClock()
    engine = _LeaseEngine(clock, 1)

    def sensitive(doc_id: str) -> bool:
        _stall(clock)
        return False

    side = _side(engine, clock, batch_size=1, document_sensitive=sensitive)
    claims, failed = side.claim_batch(park=False)
    assert len(claims) == 1 and not failed
    side.process(claims)

    claim_id = claims[0].claim_id
    assert engine.lapsed() == [], "no call met a lapsed claim"
    assert engine.has("renew", claim_id), "the claim was renewed while the check stalled"
    assert engine.index("renew", claim_id) < engine.index("write", claim_id), "renewed before its terminal call"
    assert side.held() == [], "the claim was written and forgotten"


def test_a_slow_write_does_not_lapse_the_claim_behind_it() -> None:
    """The window from the last renewal to a write: the first claim's write takes three renew intervals,
    and the second claim of the batch waits behind it."""
    clock = FakeClock()
    engine = _LeaseEngine(clock, 2)
    stalled: list[str] = []

    def before_write(claim_id: str) -> None:
        if not stalled:
            stalled.append(claim_id)
            _stall(clock)

    side = _side(engine, clock, batch_size=2, before_write=before_write)
    claims, failed = side.claim_batch(park=False)
    assert len(claims) == 2 and not failed
    side.process(claims)

    first, second = stalled[0], next(c.claim_id for c in claims if c.claim_id != stalled[0])
    assert engine.lapsed() == [], "neither write met a lapsed claim"
    assert engine.has("write", first) and engine.has("write", second)
    assert engine.has("renew", second), "the waiting claim was renewed while the first write stalled"
    assert engine.index("renew", second) < engine.index("write", second), "renewed before its own write"
    assert side.held() == []


def test_a_finished_claim_is_not_renewed() -> None:
    """The renewer renews only claims still held: the first claim is written and forgotten, then the second
    claim's write stalls, and no renewal reaches the first claim after its write (the engine would answer
    404, which the renewer would log as a lost claim)."""
    clock = FakeClock()
    engine = _LeaseEngine(clock, 2)
    writes: list[str] = []

    def before_write(claim_id: str) -> None:
        writes.append(claim_id)
        if len(writes) == 2:
            _stall(clock)

    side = _side(engine, clock, batch_size=2, before_write=before_write)
    claims, _ = side.claim_batch(park=False)
    side.process(claims)

    first, second = writes
    assert engine.lapsed() == [], "no renewal met the finished claim, and nothing lapsed"
    assert engine.has("renew", second), "the second claim was renewed through its stalled write"
    renewed_after_first_write = [
        op for op in engine.ops[engine.index("write", first):] if op == ("renew", first)
    ]
    assert renewed_after_first_write == []
