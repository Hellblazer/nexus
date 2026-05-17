# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-73vq: daemon-side blocking_take RPC handler.

RDR-112 P1.3.1. Adds a new ``tuplespace.blocking_take`` RPC method on
the daemon that polls until either a candidate becomes available
(returning the claim) or the timeout fires (returning ``None``).

This is the daemon-side companion to the RDR-110 direct-mode
``_DataVersionWatcher``: in daemon mode the daemon owns the SQLite
handle, so the polling lives inside the daemon process. Each
blocking RPC has its own polling loop running in the dispatch thread
pool; competing ``out()`` calls increment ``PRAGMA data_version``
which the polling loop observes and retries the claim CAS.

Tests cover the four contract corners:

- **immediate-hit**: candidate already present, returns on first try.
- **wait-then-hit**: blocks until a sibling thread calls ``out()``,
  then returns within the poll cadence.
- **timeout**: no candidate appears within the deadline -> ``None``.
- **N-concurrent drain**: 10 simultaneous blocking_take callers all
  succeed against a shared pool (work-stealing under contention).
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import chromadb
import pytest


_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:     { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:   { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 100
tiers: [project]
retention_seconds: 86400
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    return d


@pytest.fixture()
def registry(builtin_dir: Path):
    from nexus.tuplespace.registry import Registry
    return Registry.load(builtin_dir)


@pytest.fixture()
def chroma_client() -> chromadb.EphemeralClient:
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture()
def service(tmp_path: Path, registry, chroma_client):
    """A constructed TuplespaceService with a real tuples.db + EphemeralClient."""
    from nexus.daemon.tuplespace_service import TuplespaceService
    tuples_db = tmp_path / "tuples.db"
    svc = TuplespaceService(
        tuples_db_path=tuples_db,
        chroma_client=chroma_client,
        registry=registry,
    )
    yield svc
    svc.close()


def _dims() -> dict[str, Any]:
    return {"status": "open", "priority": "P1", "created_by": "harness"}


# ---------------------------------------------------------------------------
# RPC surface registration
# ---------------------------------------------------------------------------


class TestBlockingTakeSurface:
    """``blocking_take`` is exposed via TUPLESPACE_RPC_OPS and is callable."""

    def test_blocking_take_in_rpc_ops(self) -> None:
        from nexus.daemon.tuplespace_service import TUPLESPACE_RPC_OPS
        assert "blocking_take" in TUPLESPACE_RPC_OPS

    def test_service_has_blocking_take_method(self, service) -> None:
        assert hasattr(service, "blocking_take")
        assert callable(service.blocking_take)


# ---------------------------------------------------------------------------
# Contract: immediate-hit / timeout / wait-then-hit / concurrent drain
# ---------------------------------------------------------------------------


class TestBlockingTakeBehaviour:
    """Four contract corners exercised end-to-end against the real service."""

    def test_immediate_hit_when_candidate_present(self, service) -> None:
        """A candidate already in the tuplespace returns on first poll."""
        service.out(
            subspace="tasks/r6u5",
            content="ready",
            dimensions=_dims(),
        )
        t0 = time.perf_counter()
        result = service.blocking_take(
            subspace="tasks/r6u5",
            query="ready",
            claimant="solo",
            timeout_seconds=5.0,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert result is not None
        assert "claim_id" in result
        assert "tuple" in result
        # Sanity ceiling: immediate-hit should not loop more than a tick.
        assert elapsed_ms < 500, (
            f"immediate-hit should be sub-second; got {elapsed_ms:.0f}ms"
        )
        service.ack(claim_id=result["claim_id"], claimant="solo")

    def test_timeout_returns_none(self, service) -> None:
        """No candidate, deadline elapses, returns None within the budget."""
        t0 = time.perf_counter()
        result = service.blocking_take(
            subspace="tasks/r6u5",
            query="nothing-here",
            claimant="solo",
            timeout_seconds=0.3,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert result is None
        # Allow a generous over-budget margin for thread scheduling
        # (the poll loop wakes every ~10ms; on slow CI it may overshoot
        # the 300ms target by one tick).
        assert 250 <= elapsed_ms <= 1000, (
            f"timeout should land near 300ms; got {elapsed_ms:.0f}ms"
        )

    def test_wait_then_hit_when_sibling_outs(self, service) -> None:
        """blocking_take waits, then returns after a sibling thread out()s."""
        # Sibling thread populates after a short delay.
        def _delayed_out():
            time.sleep(0.2)
            service.out(
                subspace="tasks/r6u5",
                content="delayed",
                dimensions=_dims(),
            )

        threading.Thread(target=_delayed_out, daemon=True).start()

        t0 = time.perf_counter()
        result = service.blocking_take(
            subspace="tasks/r6u5",
            query="delayed",
            claimant="solo",
            timeout_seconds=5.0,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert result is not None, "blocking_take should return when sibling out()s"
        # Should pick up the new tuple within roughly one poll cadence
        # (~50ms tolerance) after the 200ms sibling write.
        # nexus-0cf1.3 (TR-3, 2026-05-17): widened upper bound from
        # 800ms to 1500ms to match the sibling test in
        # test_block_true_enablement.py. On heavily loaded CI hosts
        # with > 600ms scheduling jitter, the 800ms ceiling was the
        # first to flake.
        assert 180 <= elapsed_ms <= 1500, (
            f"wake should land near 200ms; got {elapsed_ms:.0f}ms"
        )
        service.ack(claim_id=result["claim_id"], claimant="solo")

    def test_concurrent_drain_10_callers(self, service) -> None:
        """10 simultaneous blocking_take callers all eventually succeed."""
        N = 10
        # Pre-populate the shared pool.
        for i in range(N):
            service.out(
                subspace="tasks/r6u5",
                content=f"task variant {i}",
                dimensions=_dims(),
            )

        successes: list[dict[str, Any]] = []
        errors: list[Exception] = []
        success_lock = threading.Lock()
        start_event = threading.Event()

        def worker(idx: int) -> None:
            try:
                start_event.wait()
                result = service.blocking_take(
                    subspace="tasks/r6u5",
                    query=f"task variant {idx}",
                    claimant=f"w-{idx}",
                    timeout_seconds=10.0,
                )
                with success_lock:
                    if result is None:
                        errors.append(
                            RuntimeError(f"worker {idx} got None within timeout")
                        )
                    else:
                        successes.append(result)
                        service.ack(claim_id=result["claim_id"], claimant=f"w-{idx}")
            except Exception as exc:
                with success_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(N)
        ]
        for t in threads:
            t.start()
        start_event.set()
        for t in threads:
            t.join(timeout=15.0)

        assert errors == [], f"unexpected errors: {errors[:3]}"
        assert len(successes) == N
        # Every claim_id is distinct (no double-claim race).
        claim_ids = {s["claim_id"] for s in successes}
        assert len(claim_ids) == N, (
            f"expected {N} distinct claim_ids; got {len(claim_ids)}"
        )
