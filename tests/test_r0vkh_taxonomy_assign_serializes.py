# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-r0vkh: the taxonomy assign hook serializes against itself.

From nexus-eslkl (2026-08-08) the hook declared ``serialize = False`` on the
claim that per-flush chash sets are disjoint so concurrent fires never contend
for the same row. The claim covered topic_assignments and missed nexus.topics:
the assign INSERT's FK takes KEY SHARE on the topics rows it references and
the doc_count recount trigger UPDATEs the same rows, so N parallel flushes of
one index run issue N assign calls that queue on the head's transactionid,
each holding one engine pool connection. Live on engine-service-v0.1.123,
2026-09-16 09:05Z: one head ran 782 s, eight waiters 667-780 s, every PG route
on the box timed out for 11 minutes.

The first test pins the declaration; the second proves the behaviour through
``LockedHookRegistry`` with the REAL hook: two concurrent fires must never be
inside the assign round trip at the same time. It is the inverse of
``tests/test_indexer_concurrency.py``'s opt-out rendezvous test and was
measured red with ``serialize = False`` restored.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from nexus.hook_registry import HookRegistry, LockedHookRegistry
from nexus.mcp_infra import taxonomy_assign_batch_hook

# The fixture collection carries a voyage token; the tests assert nothing
# mode-specific (is_local_mode is patched off), so the RDR-109 mode lint's
# opt-in is declared rather than excluded.
pytestmark = pytest.mark.usefixtures("cloud_mode")


def test_the_hook_no_longer_opts_out_of_serialization() -> None:
    assert getattr(taxonomy_assign_batch_hook, "serialize", True) is True, (
        "taxonomy_assign_batch_hook.serialize = False re-arms the nexus-r0vkh "
        "pool convoy: concurrent fires contend on nexus.topics row locks"
    )
    assert taxonomy_assign_batch_hook.batch_grain == "flush"


def test_two_concurrent_fires_never_overlap_inside_the_assign_call() -> None:
    registry = HookRegistry()
    registry.register_batch(taxonomy_assign_batch_hook)
    locked = LockedHookRegistry(registry)

    # A barrier only BOTH threads can pass if they are inside the assign
    # round trip at the same time. Serialized hooks each time out on it.
    barrier = threading.Barrier(2, timeout=1)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0
    flight_lock = threading.Lock()

    def fake_t2_index_write(write_fn, *, op="t2_write"):
        nonlocal in_flight, max_in_flight
        with flight_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            try:
                barrier.wait()
                outcome = "rendezvous"
            except threading.BrokenBarrierError:
                outcome = "broken"
            with outcomes_lock:
                outcomes.append(outcome)
            return {"assigned": 1, "cross_assigned": 0, "unmatched_chashes": []}
        finally:
            with flight_lock:
                in_flight -= 1

    with (
        patch("nexus.mcp_infra.get_t3", return_value=object()),
        patch("nexus.db.http_vector_client.is_service_backed", return_value=True),
        patch("nexus.config.is_local_mode", return_value=False),
        patch("nexus.mcp_infra.t2_index_write", side_effect=fake_t2_index_write),
    ):
        threads = [
            threading.Thread(
                target=locked.fire_batch,
                args=([f"{i:064x}"], "code__r0vkh__voyage-code-3__v1", ["x"], None, [{}]),
                kwargs={"grain": "flush"},
            )
            for i in range(2)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)

    assert len(outcomes) == 2, f"both fires must reach the assign call, got {outcomes}"
    assert max_in_flight == 1, (
        f"{max_in_flight} assign calls were in flight at once: the hook is "
        "not serialized against itself"
    )
    assert outcomes == ["broken", "broken"], (
        f"a rendezvous means both threads were inside the assign call together: {outcomes}"
    )
