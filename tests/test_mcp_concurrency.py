# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concurrency tests for MCP server — multi-process T1/T2/T3 scenarios.

Uses multiprocessing.Process (not threads) to validate actual cross-process
SQLite WAL contention, matching the real deployment scenario of multiple
nx-mcp processes.
"""
from __future__ import annotations

import multiprocessing
import tempfile
from pathlib import Path

import chromadb
import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database


# ── Helpers ───────────────────────────────────────────────────────────────────

def _t2_writer(db_path_str: str, project: str, n: int):
    """Write n entries to T2 in a subprocess.

    Retries T2Database init on 'database is locked' — all processes race to
    run executescript() on a fresh file; WAL mode prevents data-loss but the
    schema DDL can transiently fail.
    """
    import time as _time
    for attempt in range(5):
        try:
            db = T2Database(Path(db_path_str))
            break
        except Exception:
            _time.sleep(0.1 * (attempt + 1))
    else:
        db = T2Database(Path(db_path_str))  # final attempt, let it raise
    for i in range(n):
        db.put(project=project, title=f"entry-{i}", content=f"content-{i}")
    db.close()


def _t2_reader(db_path_str: str, project: str, result_list):
    """Read entries from T2 in a subprocess — appends count to shared list."""
    db = T2Database(Path(db_path_str))
    entries = db.list_entries(project=project)
    result_list.append(len(entries))
    db.close()


# ── T1 isolation test ─────────────────────────────────────────────────────────

def test_t1_isolation_across_sessions():
    """T1 entries in one session are invisible to another session on same client."""
    client = chromadb.EphemeralClient()

    t1a = T1Database(session_id="proc-a", client=client)
    t1b = T1Database(session_id="proc-b", client=client)

    t1a.put("alpha data")
    t1b.put("beta data")

    a_entries = t1a.list_entries()
    b_entries = t1b.list_entries()

    assert len(a_entries) == 1
    assert a_entries[0]["content"] == "alpha data"
    assert len(b_entries) == 1
    assert b_entries[0]["content"] == "beta data"


# ── T2 concurrent writes ─────────────────────────────────────────────────────

def test_t2_concurrent_writes():
    """N processes writing to same SQLite file via WAL — all entries persisted.

    Pre-initialises the schema in the parent so worker processes never
    race on DDL (which doesn't play well with WAL the way INSERTs do).
    The original race-then-retry shape was flaky on Python 3.13 / slow
    CI runners — one worker would exhaust its retry budget and silently
    drop all 10 writes, producing an N=30 instead of N=40 result.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # Pre-init schema in parent — eliminates the DDL race entirely.
    # Subprocesses then contend only on INSERTs, which WAL handles well.
    db = T2Database(db_path)
    db.close()

    n_processes = 4
    entries_per_process = 10
    processes = []

    for i in range(n_processes):
        p = multiprocessing.Process(
            target=_t2_writer,
            args=(str(db_path), f"proj-{i}", entries_per_process),
        )
        processes.append(p)

    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)

    # Verify all writers exited cleanly. A non-zero exitcode means a
    # subprocess crashed (e.g. on a stray DDL race) and we'd silently
    # under-count below — fail loud here so the next maintainer sees
    # the real cause instead of a 30-vs-40 head-scratcher.
    for i, p in enumerate(processes):
        assert p.exitcode == 0, f"writer {i} exited with code {p.exitcode}"

    # Verify all entries were written
    db = T2Database(db_path)
    total = 0
    for i in range(n_processes):
        entries = db.list_entries(project=f"proj-{i}")
        total += len(entries)
    db.close()

    assert total == n_processes * entries_per_process


def test_t2_concurrent_reads_during_writes():
    """Reader processes don't block or crash during concurrent writes."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # Seed some data first
    db = T2Database(db_path)
    for i in range(5):
        db.put(project="readtest", title=f"seed-{i}", content=f"seed content {i}")
    db.close()

    manager = multiprocessing.Manager()
    result_list = manager.list()

    # Start a writer and reader concurrently
    writer = multiprocessing.Process(
        target=_t2_writer, args=(str(db_path), "readtest-write", 20)
    )
    reader = multiprocessing.Process(
        target=_t2_reader, args=(str(db_path), "readtest", result_list)
    )

    writer.start()
    reader.start()
    reader.join(timeout=30)
    writer.join(timeout=30)

    # Reader should have completed without crashing
    assert len(result_list) == 1
    assert result_list[0] >= 5  # at least the seeded entries


# ── T3 concurrent reads ──────────────────────────────────────────────────────

def test_t3_concurrent_reads():
    """Multiple parallel search calls don't interfere (EphemeralClient)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    t3 = T3Database(_client=client, _ef_override=ef)

    # Seed data
    t3.put(collection="knowledge__conctest", content="concurrent read test alpha", title="alpha")
    t3.put(collection="knowledge__conctest", content="concurrent read test beta", title="beta")

    errors: list[str] = []

    def _search(query: str) -> list:
        try:
            return t3.search(query, ["knowledge__conctest"], n_results=5)
        except Exception as e:
            errors.append(str(e))
            return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_search, q)
            for q in ["alpha", "beta", "concurrent", "read test"]
        ]
        results = [f.result() for f in as_completed(futures)]

    assert not errors, f"Concurrent read errors: {errors}"
    assert all(isinstance(r, list) for r in results)
