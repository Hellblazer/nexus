# SPDX-License-Identifier: Apache-2.0
"""nexus-r6u5: 10-parallel-worker work-stealing MVV harness.

RDR-110 P4.7 Minimum Viable Validation. The substrate works in unit
tests and the 360-critique umbrella validated correctness, but no
harness has stressed take/ack at 10x concurrent claimants competing
for a shared queue under both direct and daemon modes. This test
fills that gap.

## Invariants

- Exactly N_TUPLES successful claims across the worker pool
  (no duplicate claims, no missing tuples).
- After drain, the tuples table has zero rows with
  ``claim_state='claimed'`` AND zero rows with ``consumed_at IS NULL``
  among the test tuples.
- ``tuple_claim_log`` records exactly 2 * N_TUPLES rows
  (one ``claim`` + one ``ack`` per tuple).
- No tuple_id appears as the target of two distinct active claims
  in the audit trail.

## Modes

Parametrised across two backends:

1. ``direct`` -- workers open their own SQLite WAL connections and
   call ``nexus.tuplespace.api.take`` / ``ack`` synchronously.
   This exercises the SQLite CAS UPDATE ... RETURNING contention path.
2. ``daemon`` -- workers route every take/ack through ``T2Client``
   over a UDS socket to a T2Daemon running a TuplespaceService.
   Exercises RPC serialisation + the single-writer guarantee.

## Wall clock

Tests use ``N_TUPLES=200`` (default) to stay under a minute per mode
on EphemeralClient + ONNX MiniLM. The full ``N_TUPLES=1000`` variant
is gated behind ``@pytest.mark.slow``.
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

from nexus.daemon.t2_client import T2Client
from nexus.daemon.t2_daemon import T2Daemon
from nexus.daemon.tuplespace_service import TuplespaceService
from nexus.tuplespace.api import ack, out, take
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_WORKERS: int = 10
N_TUPLES_DEFAULT: int = 200    # default run (~30s wall clock)
N_TUPLES_SLOW: int = 1_000     # full RDR-110 acceptance criterion (-m slow)
POLL_SLEEP_S: float = 0.005    # 5 ms between empty-take retries
TIMEOUT_S: float = 120.0

_SUBSPACE = "tasks/r6u5"
_TEMPLATE = "tasks/<project>"

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
  # nexus-r6u5: default_n is the chroma top-K passed into the take
  # candidate scan. take() iterates the returned ids via the CAS
  # UPDATE's IN clause, so a small default_n + many consumed tuples
  # leaves the worker pool starved (chroma keeps returning the same
  # top-K which is now consumed). 100 gives plenty of headroom for
  # 10 workers x 200 tuples; the chroma quota cap is 300.
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
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tuples.db"


@pytest.fixture()
def chroma_client() -> chromadb.EphemeralClient:
    """EphemeralClient with isolated state per test.

    EphemeralClient shares an in-memory backend across instances in
    the same process; clear collections on entry to defend against
    bleed-over from sibling tests in the same suite run.
    """
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture()
def index(registry: Registry, chroma_client: chromadb.EphemeralClient) -> TupleIndex:
    return TupleIndex.from_registry(registry, chroma_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_worker_conn(db_path: Path) -> sqlite3.Connection:
    """Worker-thread SQLite connection: autocommit WAL with busy timeout.

    Autocommit (``isolation_level=None``) avoids Python sqlite3's
    implicit BEGIN that can otherwise serialise concurrent claim CAS
    attempts under WAL.
    """
    conn = sqlite3.connect(
        str(db_path), check_same_thread=False, isolation_level=None
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _populate_shared_queue(
    *,
    db_path: Path,
    index: TupleIndex,
    registry: Registry,
    n_tuples: int,
) -> None:
    """Populate ``n_tuples`` into the shared subspace via api.out (real path)."""
    conn = open_tuples_db(db_path)
    try:
        for i in range(n_tuples):
            out(
                conn=conn,
                index=index,
                registry=registry,
                subspace=_SUBSPACE,
                content=f"task variant {i}",
                dimensions={
                    "status": "open",
                    "priority": "P1",
                    "created_by": "harness",
                },
            )
    finally:
        conn.close()


def _audit_invariants(db_path: Path, *, n_tuples: int) -> dict[str, Any]:
    """Run the post-drain SQL checks and return a summary dict."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            "SELECT COUNT(*) FROM tuples WHERE subspace = ?", (_SUBSPACE,)
        ).fetchone()[0]
        consumed = conn.execute(
            "SELECT COUNT(*) FROM tuples "
            "WHERE subspace = ? AND consumed_at IS NOT NULL",
            (_SUBSPACE,),
        ).fetchone()[0]
        still_claimed = conn.execute(
            "SELECT COUNT(*) FROM tuples "
            "WHERE subspace = ? AND claim_state = 'claimed'",
            (_SUBSPACE,),
        ).fetchone()[0]
        still_available = conn.execute(
            "SELECT COUNT(*) FROM tuples "
            "WHERE subspace = ? AND consumed_at IS NULL "
            "AND (claim_state IS NULL OR claim_state != 'claimed')",
            (_SUBSPACE,),
        ).fetchone()[0]
        claim_log_count = conn.execute(
            "SELECT COUNT(*) FROM tuple_claim_log WHERE subspace = ?",
            (_SUBSPACE,),
        ).fetchone()[0]
        claim_log_by_transition = dict(
            conn.execute(
                "SELECT transition, COUNT(*) FROM tuple_claim_log "
                "WHERE subspace = ? GROUP BY transition",
                (_SUBSPACE,),
            ).fetchall()
        )
        # Duplicate-claim detection: every tuple_id should appear at
        # most once with a non-NULL claim_id across the active claim
        # log; the audit trail records every state transition.
        dup_active_claims = conn.execute(
            "SELECT tuple_id, COUNT(DISTINCT claim_id) FROM tuple_claim_log "
            "WHERE subspace = ? AND transition = 'claim' AND claim_id IS NOT NULL "
            "GROUP BY tuple_id "
            "HAVING COUNT(DISTINCT claim_id) > 1",
            (_SUBSPACE,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "n_tuples": n_tuples,
        "tuples_total": int(total or 0),
        "tuples_consumed": int(consumed or 0),
        "tuples_still_claimed": int(still_claimed or 0),
        "tuples_still_available": int(still_available or 0),
        "claim_log_count": int(claim_log_count or 0),
        "claim_log_by_transition": claim_log_by_transition,
        "duplicate_active_claims": [dict(r) for r in dup_active_claims],
    }


# ---------------------------------------------------------------------------
# Direct-mode worker
# ---------------------------------------------------------------------------


def _direct_worker(
    *,
    worker_idx: int,
    db_path: Path,
    index: TupleIndex,
    registry: Registry,
    start_event: threading.Event,
    successes: list[float],
    latencies_ms: list[float],
    sync_lock: threading.Lock,
    deadline: float,
) -> None:
    conn = _open_worker_conn(db_path)
    try:
        start_event.wait()
        attempt = 0
        while time.perf_counter() < deadline:
            t_attempt = time.perf_counter()
            # Vary query per attempt so chroma's top-K rotates and
            # consumed tuples don't starve future polls.
            attempt += 1
            result = take(
                conn=conn,
                index=index,
                registry=registry,
                subspace=_SUBSPACE,
                query=f"task variant {attempt}",
                claimant=f"direct-w{worker_idx}",
            )
            if result is None:
                # Either drained or transient contention; back off briefly.
                time.sleep(POLL_SLEEP_S)
                with sync_lock:
                    if successes and len(successes) >= deadline_target: 
                        return
                continue
            t_success = time.perf_counter()
            _tuple_dict, claim_id = result
            ack(conn=conn, claim_id=claim_id, claimant=f"direct-w{worker_idx}")
            with sync_lock:
                successes.append(t_success)
                latencies_ms.append((t_success - t_attempt) * 1000.0)
                if len(successes) >= deadline_target: 
                    return
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daemon-mode worker + daemon fixture
# ---------------------------------------------------------------------------


def _run_daemon_in_thread(
    daemon: T2Daemon,
) -> asyncio.AbstractEventLoop:
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _t() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    threading.Thread(target=_t, daemon=True).start()
    started.wait(timeout=5.0)
    return loop


def _stop_daemon_in_thread(
    daemon: T2Daemon, loop: asyncio.AbstractEventLoop
) -> None:
    asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=5.0)
    loop.call_soon_threadsafe(loop.stop)


def _daemon_worker(
    *,
    worker_idx: int,
    uds_path: Path,
    start_event: threading.Event,
    successes: list[float],
    latencies_ms: list[float],
    sync_lock: threading.Lock,
    deadline: float,
) -> None:
    # nexus-r6u5: bump per-RPC timeout. TuplespaceService.take serialises
    # through self._lock, so 10 concurrent take RPCs each waiting on
    # chroma embed + SQLite CAS can queue behind one another. The
    # RDR-114 default of 5s is too aggressive under sustained
    # contention; the spike's wall-clock target is well under 30s/RPC.
    client = T2Client(uds_path=uds_path, rpc_timeout_seconds=30.0)
    try:
        start_event.wait()
        attempt = 0
        while time.perf_counter() < deadline:
            t_attempt = time.perf_counter()
            attempt += 1
            result = client.tuplespace.take(
                subspace=_SUBSPACE,
                query=f"task variant {attempt}",
                claimant=f"daemon-w{worker_idx}",
            )
            if result is None:
                time.sleep(POLL_SLEEP_S)
                with sync_lock:
                    if successes and len(successes) >= deadline_target: 
                        return
                continue
            t_success = time.perf_counter()
            claim_id = result["claim_id"]
            client.tuplespace.ack(
                claim_id=claim_id, claimant=f"daemon-w{worker_idx}"
            )
            with sync_lock:
                successes.append(t_success)
                latencies_ms.append((t_success - t_attempt) * 1000.0)
                if len(successes) >= deadline_target: 
                    return
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Drain orchestration (shared between direct + daemon modes)
# ---------------------------------------------------------------------------


# Module-level mutable so worker closures can read the target.
# Set per-test; threading-safe because workers only READ it after the
# test thread sets it (no concurrent writes).
deadline_target: int = N_TUPLES_DEFAULT


def _run_drain(
    *,
    worker_factory,
    n_tuples: int,
    timeout_s: float,
) -> dict[str, Any]:
    """Spawn N_WORKERS via ``worker_factory(idx, ...)`` and drain.

    Returns a stats dict (consumed, total_elapsed_ms, p50/p95/p99 ms).
    """
    global deadline_target
    deadline_target = n_tuples

    start_event = threading.Event()
    successes: list[float] = []
    latencies_ms: list[float] = []
    sync_lock = threading.Lock()
    deadline = time.perf_counter() + timeout_s

    workers = [
        threading.Thread(
            target=worker_factory,
            kwargs=dict(
                worker_idx=i,
                start_event=start_event,
                successes=successes,
                latencies_ms=latencies_ms,
                sync_lock=sync_lock,
                deadline=deadline,
            ),
            name=f"w-{i}",
            daemon=True,
        )
        for i in range(N_WORKERS)
    ]
    for t in workers:
        t.start()

    t0 = time.perf_counter()
    start_event.set()
    for t in workers:
        t.join(timeout=timeout_s)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "consumed": len(successes),
        "total_elapsed_ms": elapsed_ms,
        "p50_ms": _percentile(latencies_ms, 50),
        "p95_ms": _percentile(latencies_ms, 95),
        "p99_ms": _percentile(latencies_ms, 99),
        "max_latency_ms": max(latencies_ms) if latencies_ms else 0.0,
    }


def _assert_drain_invariants(
    *, audit: dict[str, Any], n_tuples: int, stats: dict[str, Any]
) -> None:
    """All invariants in one assertion block with rich diagnostics."""
    diagnostics = (
        f"\n  populated: {n_tuples}\n"
        f"  consumed by workers: {stats['consumed']}\n"
        f"  audit: {audit}\n"
        f"  latency p50={stats['p50_ms']:.1f}ms "
        f"p95={stats['p95_ms']:.1f}ms p99={stats['p99_ms']:.1f}ms "
        f"max={stats['max_latency_ms']:.1f}ms\n"
        f"  total elapsed: {stats['total_elapsed_ms']:.0f}ms"
    )

    assert stats["consumed"] == n_tuples, (
        f"worker pool consumed {stats['consumed']} != {n_tuples} expected"
        + diagnostics
    )
    assert audit["tuples_consumed"] == n_tuples, (
        f"DB shows {audit['tuples_consumed']} consumed != {n_tuples}"
        + diagnostics
    )
    assert audit["tuples_still_claimed"] == 0, (
        f"orphan claims: {audit['tuples_still_claimed']} tuples still in "
        f"claimed state after drain" + diagnostics
    )
    assert audit["tuples_still_available"] == 0, (
        f"missed tuples: {audit['tuples_still_available']} still available "
        f"after drain" + diagnostics
    )
    # tuple_claim_log: one 'claim' + one 'ack' per tuple = 2*N transitions.
    expected_log = 2 * n_tuples
    assert audit["claim_log_count"] == expected_log, (
        f"claim_log row count {audit['claim_log_count']} != {expected_log} "
        f"(expected one 'claim' + one 'ack' per tuple)" + diagnostics
    )
    by_transition = audit["claim_log_by_transition"]
    assert by_transition.get("claim", 0) == n_tuples, (
        f"claim transitions {by_transition.get('claim', 0)} != {n_tuples}"
        + diagnostics
    )
    assert by_transition.get("ack", 0) == n_tuples, (
        f"ack transitions {by_transition.get('ack', 0)} != {n_tuples}"
        + diagnostics
    )
    assert audit["duplicate_active_claims"] == [], (
        f"CAS race detected: tuples with multiple active claim ids: "
        f"{audit['duplicate_active_claims']}" + diagnostics
    )


# ---------------------------------------------------------------------------
# Direct-mode test
# ---------------------------------------------------------------------------


class TestWorkStealingMvvDirect:
    """Direct-mode 10-worker work-stealing harness."""

    def test_drain_default_n(
        self,
        db_path: Path,
        index: TupleIndex,
        registry: Registry,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        n = N_TUPLES_DEFAULT
        _populate_shared_queue(
            db_path=db_path, index=index, registry=registry, n_tuples=n
        )

        def _factory(**kwargs):
            return _direct_worker(
                db_path=db_path, index=index, registry=registry, **kwargs
            )

        stats = _run_drain(
            worker_factory=_factory, n_tuples=n, timeout_s=TIMEOUT_S
        )
        audit = _audit_invariants(db_path, n_tuples=n)

        print(
            f"\n[r6u5 direct-mode N={n}] consumed={stats['consumed']} "
            f"elapsed={stats['total_elapsed_ms']:.0f}ms "
            f"p50={stats['p50_ms']:.1f}ms p99={stats['p99_ms']:.1f}ms"
        )
        _assert_drain_invariants(audit=audit, n_tuples=n, stats=stats)


# ---------------------------------------------------------------------------
# Daemon-mode test
# ---------------------------------------------------------------------------


class TestWorkStealingMvvDaemon:
    """Daemon-mode 10-worker work-stealing harness routed through T2Client."""

    def test_drain_default_n(
        self,
        tmp_path: Path,
        registry: Registry,
        chroma_client: chromadb.EphemeralClient,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        n = N_TUPLES_DEFAULT
        # macOS UDS path length cap: keep config dir under /tmp not pytest's
        # /private/var/folders/.../ which can exceed the 104-char limit.
        import tempfile as _tempfile
        config_dir = Path(_tempfile.mkdtemp(prefix="r6u5-", dir="/tmp"))
        tuples_db = config_dir / "tuples.db"

        service = TuplespaceService(
            tuples_db_path=tuples_db,
            chroma_client=chroma_client,
            registry=registry,
        )
        daemon = T2Daemon(
            config_dir=config_dir,
            tuples_db_path=tuples_db,
            tuplespace_service=service,
        )
        loop = _run_daemon_in_thread(daemon)
        try:
            # Pre-populate through the same service so chroma + sqlite
            # both have the data the daemon will route claims against.
            for i in range(n):
                service.out(
                    subspace=_SUBSPACE,
                    content=f"task variant {i}",
                    dimensions={
                        "status": "open",
                        "priority": "P1",
                        "created_by": "harness",
                    },
                )

            def _factory(**kwargs):
                return _daemon_worker(uds_path=daemon.uds_path, **kwargs)

            stats = _run_drain(
                worker_factory=_factory,
                n_tuples=n,
                timeout_s=TIMEOUT_S,
            )
            audit = _audit_invariants(tuples_db, n_tuples=n)

            print(
                f"\n[r6u5 daemon-mode N={n}] consumed={stats['consumed']} "
                f"elapsed={stats['total_elapsed_ms']:.0f}ms "
                f"p50={stats['p50_ms']:.1f}ms p99={stats['p99_ms']:.1f}ms"
            )
            _assert_drain_invariants(audit=audit, n_tuples=n, stats=stats)
        finally:
            _stop_daemon_in_thread(daemon, loop)
            service.close()


# ---------------------------------------------------------------------------
# Full-scale variants (RDR-110 acceptance criterion: 10 x 100 = 1000)
# ---------------------------------------------------------------------------


class TestWorkStealingMvvFullScale:
    """N_TUPLES=1000 cases, matching the RDR-110 acceptance criterion."""

    @pytest.mark.slow
    def test_drain_1000_direct(
        self,
        db_path: Path,
        index: TupleIndex,
        registry: Registry,
    ) -> None:
        n = N_TUPLES_SLOW
        _populate_shared_queue(
            db_path=db_path, index=index, registry=registry, n_tuples=n
        )

        def _factory(**kwargs):
            return _direct_worker(
                db_path=db_path, index=index, registry=registry, **kwargs
            )

        stats = _run_drain(
            worker_factory=_factory, n_tuples=n, timeout_s=TIMEOUT_S * 3
        )
        audit = _audit_invariants(db_path, n_tuples=n)

        print(
            f"\n[r6u5 direct-mode N={n}] consumed={stats['consumed']} "
            f"elapsed={stats['total_elapsed_ms']:.0f}ms "
            f"p50={stats['p50_ms']:.1f}ms p99={stats['p99_ms']:.1f}ms"
        )
        _assert_drain_invariants(audit=audit, n_tuples=n, stats=stats)
