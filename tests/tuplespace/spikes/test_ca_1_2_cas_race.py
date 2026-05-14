# SPDX-License-Identifier: Apache-2.0
"""Spike CA-1 + CA-2: CAS race exactly-one-winner under controlled interleaving.

RDR-110 Phase 1 Step 7 CA spike (nexus-tq96).

Validates CA #2: "The Phase 1 stress test exercises the actual race window,
not just the happy path."

Design:
- 10 worker threads race on the same tuple per round (100 rounds total).
- A threading.Barrier(N_WORKERS) is installed as _take_pre_update_hook so all
  10 threads are simultaneously past candidate-selection and before the CAS
  UPDATE on every round.
- SQLite single-writer lock guarantees exactly one UPDATE wins per round.
- Assert: total wins == 100 (one per round), total misses == 900.

Mode: `exact` (locks/<resource>) bypasses Chroma entirely, avoiding EphemeralClient
thread-safety issues under 10-way concurrent read calls.  This is intentional: the
CAS race is a SQL-level property and exact mode exercises the same UPDATE...RETURNING
code path as semantic mode.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import chromadb
import pytest

import nexus.tuplespace.api as _api_module
from nexus.tuplespace.api import out, take, ack
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_WORKERS: int = 10
N_ROUNDS: int = 100

# Use exact mode (locks/<resource>) to bypass Chroma in the race test.
# The CAS guarantee lives in the SQLite UPDATE...RETURNING, so exact mode
# exercises the identical code path while being thread-safe.
_LOCKS_YAML = """
name: locks/<resource>
tier: project
content_type: text
embed_from: content
dimensions:
  resource: { type: string, required: true }
  holder:   { type: string, required: true }
take:
  enabled: true
  mode: exact
  match_keys: [resource]
  default_lease_seconds: 300
read:
  default_floor: 0.0
  default_n: 1
tiers: [project]
retention_seconds: 3600
"""

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "locks.yml").write_text(_LOCKS_YAML)
    return d


@pytest.fixture()
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tuples.db"


@pytest.fixture()
def main_conn(db_path: Path) -> sqlite3.Connection:
    conn = open_tuples_db(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def chroma_client() -> chromadb.EphemeralClient:
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
    """Open a fresh WAL connection for a worker thread.

    isolation_level=None puts the connection in autocommit mode so each
    statement is its own implicit transaction.  This avoids the "database is
    locked" OperationalError that arises when Python's sqlite3 holds an
    implicit BEGIN and 10 threads all try to acquire the write lock simultaneously.
    busy_timeout is set as a fallback retry budget.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _lock_dims(round_i: int) -> dict[str, Any]:
    return {"resource": f"race-resource-{round_i}", "holder": "pending"}


# ---------------------------------------------------------------------------
# Spike test
# ---------------------------------------------------------------------------


class TestCASRaceExactlyOneWinner:
    """CA #1 + CA #2: single-statement CAS is race-safe under controlled interleaving."""

    def test_exactly_one_winner_per_round(
        self,
        db_path: Path,
        main_conn: sqlite3.Connection,
        index: TupleIndex,
        registry: Registry,
    ) -> None:
        """10 workers x 100 rounds; each round exactly 1 CAS winner, 9 misses.

        Each round inserts one tuple in locks/<resource> with a unique resource
        key.  All 10 workers call take(where={"resource": key}) in exact mode.
        The _take_pre_update_hook barrier forces all 10 threads to be
        simultaneously past candidate-selection before any UPDATE runs.
        SQLite's single-writer lock ensures exactly one UPDATE...RETURNING succeeds.
        """
        # hook_barrier: all N_WORKERS threads must arrive before any proceeds.
        # Auto-resets after all N_WORKERS arrive.
        hook_barrier = threading.Barrier(N_WORKERS)

        # Round synchronisation: main + N_WORKERS parties.
        round_start_barrier = threading.Barrier(N_WORKERS + 1)
        round_end_barrier = threading.Barrier(N_WORKERS + 1)

        # per_worker_round_wins[worker][round] = True if that worker won.
        per_worker_round_wins: list[list[bool]] = [[] for _ in range(N_WORKERS)]
        errors: list[Exception] = []

        original_hook = _api_module._take_pre_update_hook
        _api_module._take_pre_update_hook = hook_barrier.wait

        # Track current round's resource key so workers know what to query.
        current_resource: list[str] = [""]  # list for mutability from closure

        try:
            def worker_fn(worker_idx: int) -> None:
                conn = _open_worker_conn(db_path)
                try:
                    for _round_i in range(N_ROUNDS):
                        round_start_barrier.wait()

                        resource = current_resource[0]
                        result = take(
                            conn=conn,
                            index=index,
                            registry=registry,
                            subspace=f"locks/{resource}",
                            query="",   # unused in exact mode
                            claimant=f"worker-{worker_idx}",
                            where={"resource": resource},
                        )

                        won = result is not None
                        if won:
                            _t_dict, claim_id = result
                            ack(conn=conn, claim_id=claim_id, claimant=f"worker-{worker_idx}")

                        per_worker_round_wins[worker_idx].append(won)
                        round_end_barrier.wait()

                except Exception as exc:
                    import traceback
                    msg = f"worker-{worker_idx} failed: {exc}\n{traceback.format_exc()}"
                    print(f"\n[CAS WORKER ERROR] {msg}", flush=True)
                    errors.append(RuntimeError(msg))
                    for b in (round_start_barrier, round_end_barrier, hook_barrier):
                        try:
                            b.abort()
                        except Exception:
                            pass
                finally:
                    conn.close()

            threads = [
                threading.Thread(
                    target=worker_fn, args=(i,), name=f"cas-worker-{i}", daemon=True
                )
                for i in range(N_WORKERS)
            ]
            for t in threads:
                t.start()

            for round_i in range(N_ROUNDS):
                resource = f"race-resource-{round_i}"
                current_resource[0] = resource

                # Insert the round's tuple into locks/<resource>.
                out(
                    conn=main_conn,
                    index=index,
                    registry=registry,
                    subspace=f"locks/{resource}",
                    content=f"exclusive lock on {resource}",
                    dimensions=_lock_dims(round_i),
                )

                round_start_barrier.wait()
                round_end_barrier.wait()

            for t in threads:
                t.join(timeout=60.0)
                assert not t.is_alive(), f"Thread {t.name} did not finish in time"

        finally:
            _api_module._take_pre_update_hook = original_hook

        if errors:
            raise errors[0]

        per_round_wins = [
            sum(per_worker_round_wins[w][r] for w in range(N_WORKERS))
            for r in range(N_ROUNDS)
        ]
        total_wins = sum(per_round_wins)
        total_misses = N_WORKERS * N_ROUNDS - total_wins
        bad_rounds = [(r, per_round_wins[r]) for r in range(N_ROUNDS) if per_round_wins[r] != 1]

        print(
            f"\n[CA-1/CA-2 CAS race] "
            f"total_wins={total_wins} total_misses={total_misses} "
            f"total_attempts={N_WORKERS * N_ROUNDS} "
            f"bad_rounds={len(bad_rounds)}"
        )
        if bad_rounds:
            print(f"  Rounds with wrong winner count (first 10): {bad_rounds[:10]}")

        assert len(bad_rounds) == 0, (
            f"Expected exactly 1 winner per round across {N_ROUNDS} rounds. "
            f"Rounds with wrong winner count (first 10): {bad_rounds[:10]}. "
            "CAS atomicity may be broken."
        )
        assert total_wins == N_ROUNDS
        assert total_misses == N_WORKERS * N_ROUNDS - N_ROUNDS

        print(
            f"[CA-1/CA-2] PASS -- SQLite single-statement CAS is race-safe. "
            f"{total_wins}/{N_WORKERS * N_ROUNDS} take() calls succeeded (expected {N_ROUNDS})."
        )
