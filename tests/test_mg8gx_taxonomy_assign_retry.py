# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mg8gx: client-side retry/split for a failed taxonomy-assign batch.

Before this fix, ANY exception from ``assign_from_chashes`` (a statement/
lock timeout at the engine's r0vkh 30s bound, an edge 5xx, a 499) dropped
the WHOLE batch — every chash in it lost its topic assignment until a human
re-ran the index by hand (bead report: ~270-chunk batches, 3/5 failed,
800 chunks lost after an engine restart).

``_assign_from_chashes_with_retry`` retries a retryable failure split in
half, recursively with backoff, down to a floor; only what still fails at
the floor (or a non-retryable 4xx) is reported lost — the SAME
``failed_batches``/``failed_chunks``/hook_failures-tripwire contract
``tests/test_taxonomy_hook_tripwire.py`` already pins, just scoped down
from "the whole original batch" to "whatever genuinely never landed".

The assign upsert this route drives is
``ON CONFLICT (tenant, doc_id, topic_id)`` (idempotent), so resending all
or part of a batch is always safe — these tests never need to assert
anything about duplicate-write safety, only about which chashes come back
lost and how the retry schedule behaves.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from nexus import mcp_infra


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.invalid/v1/taxonomy/assignments/assign_from_chashes")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request),
    )


class _FakeClock:
    """Injectable clock/sleep so the backoff schedule and the deadline
    cutoff are provable without a real wall-clock wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def now_fn(self) -> float:
        return self.now

    def sleep_fn(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _install_t2_index_write(monkeypatch, assign_side_effect) -> list[list[str]]:
    """Patch ``mcp_infra.t2_index_write`` so every call routes into a fresh
    mock ``db.taxonomy.assign_from_chashes`` driven by *assign_side_effect*.
    Returns the list of chash-lists each call was made with, in order.
    """
    calls: list[list[str]] = []

    def _fake(fn, **_kwargs):
        t2 = MagicMock()

        def _assign(collection, chashes, cross_collection=True):
            calls.append(list(chashes))
            return assign_side_effect(collection, chashes, cross_collection=cross_collection)

        t2.taxonomy.assign_from_chashes.side_effect = _assign
        return fn(t2)

    monkeypatch.setattr(mcp_infra, "t2_index_write", _fake)
    return calls


def test_timeout_then_halves_succeed_yields_no_lost_chunks(monkeypatch):
    def side_effect(_collection, chashes, cross_collection=True):
        if len(chashes) > 16:
            raise _http_error(500)
        return {"assigned": len(chashes), "cross_assigned": 0, "unmatched_chashes": []}

    calls = _install_t2_index_write(monkeypatch, side_effect)
    clock = _FakeClock()
    doc_ids = [f"c{i}" for i in range(32)]

    result, lost, failures = mcp_infra._assign_from_chashes_with_retry(
        "code__x__voyage-code-3__v1", doc_ids,
        deadline=1000.0, floor=16, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn,
    )

    assert lost == []
    assert failures == []
    assert result["assigned"] == 32
    # whole batch, then two 16-chunk halves — no further split needed.
    assert len(calls) == 3
    assert clock.sleeps == [1.0]


def test_persistent_failure_recurses_to_floor_and_reports_floor_sized_losses(monkeypatch):
    def side_effect(_collection, _chashes, cross_collection=True):
        raise _http_error(503)

    _install_t2_index_write(monkeypatch, side_effect)
    clock = _FakeClock()
    doc_ids = [f"c{i}" for i in range(64)]

    result, lost, failures = mcp_infra._assign_from_chashes_with_retry(
        "code__x__voyage-code-3__v1", doc_ids,
        deadline=1000.0, floor=16, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn,
    )

    assert sorted(lost) == sorted(doc_ids)
    # 64 -> 32,32 -> 16,16,16,16: exactly 4 terminal, floor-sized losses.
    assert len(failures) == 4
    assert result == {"assigned": 0, "cross_assigned": 0, "unmatched_chashes": []}


def test_4xx_is_not_retried(monkeypatch):
    def side_effect(_collection, _chashes, cross_collection=True):
        raise _http_error(400)

    calls = _install_t2_index_write(monkeypatch, side_effect)
    clock = _FakeClock()
    doc_ids = [f"c{i}" for i in range(64)]

    result, lost, failures = mcp_infra._assign_from_chashes_with_retry(
        "code__x__voyage-code-3__v1", doc_ids,
        deadline=1000.0, floor=16, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn,
    )

    assert lost == doc_ids
    assert len(failures) == 1
    assert len(calls) == 1, "a 4xx must fail immediately, never split/retried"
    assert clock.sleeps == []
    assert result == {"assigned": 0, "cross_assigned": 0, "unmatched_chashes": []}


def test_backoff_is_bounded(monkeypatch):
    def side_effect(_collection, _chashes, cross_collection=True):
        raise _http_error(500)

    _install_t2_index_write(monkeypatch, side_effect)
    clock = _FakeClock()
    doc_ids = [f"c{i}" for i in range(128)]

    mcp_infra._assign_from_chashes_with_retry(
        "code__x__voyage-code-3__v1", doc_ids,
        deadline=1000.0, floor=16, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn,
    )

    assert clock.sleeps, "a retryable failure must back off before splitting"
    assert max(clock.sleeps) <= max(mcp_infra._TAXONOMY_ASSIGN_RETRY_BACKOFF_S)
    assert all(s in mcp_infra._TAXONOMY_ASSIGN_RETRY_BACKOFF_S for s in clock.sleeps)
    # bounded: never more calls than one per node in the split tree down to
    # the floor (128 -> 64x2 -> 32x4 -> 16x8: 15 total nodes, 7 of them
    # internal/split nodes that sleep).
    assert len(clock.sleeps) == 7


def test_deadline_cuts_off_further_splitting(monkeypatch):
    def side_effect(_collection, _chashes, cross_collection=True):
        raise _http_error(500)

    calls = _install_t2_index_write(monkeypatch, side_effect)
    clock = _FakeClock()
    doc_ids = [f"c{i}" for i in range(64)]

    result, lost, failures = mcp_infra._assign_from_chashes_with_retry(
        "code__x__voyage-code-3__v1", doc_ids,
        deadline=-1.0, floor=16, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn,
    )

    assert lost == doc_ids
    assert len(failures) == 1, "an already-expired deadline is terminal immediately, no split"
    assert len(calls) == 1
    assert clock.sleeps == []
    assert result == {"assigned": 0, "cross_assigned": 0, "unmatched_chashes": []}


def test_transport_error_is_retryable(monkeypatch):
    """Connection resets/timeouts are exactly the shape a statement/lock
    timeout or a dropped edge connection surfaces as at the httpx layer —
    treat them the same as a 5xx."""
    def side_effect(_collection, chashes, cross_collection=True):
        if len(chashes) > 16:
            raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", "https://example.invalid/x"))
        return {"assigned": len(chashes), "cross_assigned": 0, "unmatched_chashes": []}

    _install_t2_index_write(monkeypatch, side_effect)
    clock = _FakeClock()
    doc_ids = [f"c{i}" for i in range(32)]

    result, lost, failures = mcp_infra._assign_from_chashes_with_retry(
        "code__x__voyage-code-3__v1", doc_ids,
        deadline=1000.0, floor=16, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn,
    )

    assert lost == []
    assert failures == []
    assert result["assigned"] == 32


def test_hook_reports_floor_sized_loss_through_full_pipeline(monkeypatch):
    """End-to-end: taxonomy_assign_batch_hook routes through the retry/split
    helper and still lands on the SAME failed_batches/failed_chunks
    counters and hook_failures tripwire
    (tests/test_taxonomy_hook_tripwire.py) as before nexus-mg8gx. Batch
    size == floor here so no split is attempted and no real backoff sleep
    is exercised (see the fake-clock tests above for split/backoff
    behaviour) — this test is about wiring, not the retry algorithm.
    """
    mcp_infra.reset_taxonomy_assign_run_stats()
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: MagicMock())
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda _t3: True)

    captured: list = []

    def _capture_write(fn, **_kwargs):
        t2 = MagicMock()
        captured.append(t2)
        t2.taxonomy.assign_from_chashes.side_effect = _http_error(503)
        return fn(t2)

    monkeypatch.setattr(mcp_infra, "t2_index_write", _capture_write)
    doc_ids = [f"c{i}" for i in range(16)]  # == default floor, no split attempted
    mcp_infra.taxonomy_assign_batch_hook(
        doc_ids, "knowledge__tw__model-ctx__v1", ["x"] * 16, [[0.1]] * 16, None,
    )

    # `captured` also picks up the tripwire's OWN t2_index_write call (to
    # persist the hook_failures row) — count only the assign_from_chashes
    # attempts, which is what "no split attempted" is actually about.
    assign_attempts = [t2 for t2 in captured if t2.taxonomy.assign_from_chashes.called]
    assert len(assign_attempts) == 1, "at/under the floor a retryable failure is terminal on first attempt"
    stats = mcp_infra.taxonomy_assign_run_stats()
    assert stats["attempted"] == 1
    assert stats["failed_batches"] == 1
    assert stats["failed_chunks"] == 16


def test_hook_recovers_from_a_split_batch_with_no_loss(monkeypatch):
    """A batch bigger than the floor whose whole-batch attempt fails
    transiently but whose halves succeed must NOT count as a failed batch
    — this is the exact GH-report shape (a large flush batch hitting the
    engine's statement-timeout bound) the bead exists to fix."""
    mcp_infra.reset_taxonomy_assign_run_stats()
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: MagicMock())
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda _t3: True)

    def _capture_write(fn, **_kwargs):
        t2 = MagicMock()

        def _assign(_collection, chashes, cross_collection=True):
            if len(chashes) > 16:
                raise _http_error(500)
            return {"assigned": len(chashes), "cross_assigned": 0, "unmatched_chashes": []}

        t2.taxonomy.assign_from_chashes.side_effect = _assign
        return fn(t2)

    monkeypatch.setattr(mcp_infra, "t2_index_write", _capture_write)
    # Avoid a real wall-clock sleep for the ONE backoff this batch triggers
    # (32 -> two 16s), without touching the retry/split algorithm itself.
    monkeypatch.setattr(mcp_infra.time, "sleep", lambda _seconds: None)
    doc_ids = [f"c{i}" for i in range(32)]
    mcp_infra.taxonomy_assign_batch_hook(
        doc_ids, "knowledge__tw__model-ctx__v1", ["x"] * 32, [[0.1]] * 32, None,
    )

    stats = mcp_infra.taxonomy_assign_run_stats()
    assert stats["attempted"] == 1
    assert stats["failed_batches"] == 0
    assert stats["failed_chunks"] == 0
