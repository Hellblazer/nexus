# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2kld (Bundle HR): daemon hardening remediation tests.

Four robustness fixes from the 2026-05-17 360° review:

- HR-1: blocking_take concurrency ceiling (semaphore + resource_exhausted)
- HR-2: find_t2_daemon honours the shutdown marker
- HR-3: blocking_take data_version polling is no longer vestigial
- HR-4: _glob_to_prefix_range honours the full Unicode range
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import chromadb
import pytest


# ---------------------------------------------------------------------------
# HR-1: blocking_take concurrency cap via semaphore + clear error
# ---------------------------------------------------------------------------


_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status: { type: enum, values: [open, in_progress, done], required: true }
  priority: { type: enum, values: [P0, P1, P2], required: true }
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


@pytest.fixture()
def _registry(tmp_path: Path):
    from nexus.tuplespace.registry import Registry

    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    return Registry.load(d)


@pytest.fixture()
def _chroma() -> chromadb.EphemeralClient:
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


class TestBlockingTakeConcurrencyCap:
    """The semaphore caps in-flight blocking_take RPCs."""

    def test_semaphore_constant_exposed(self) -> None:
        from nexus.daemon.tuplespace_service import (
            _BLOCKING_TAKE_MAX_CONCURRENT,
        )
        assert _BLOCKING_TAKE_MAX_CONCURRENT > 0
        # Default should be a sane multi-agent cap (somewhere in [4, 64]).
        assert 4 <= _BLOCKING_TAKE_MAX_CONCURRENT <= 64

    def test_overflow_raises_resource_exhausted(
        self, tmp_path: Path, _registry, _chroma, monkeypatch
    ) -> None:
        """Hold N+1 blocking_take calls; the last one must fail loud."""
        from nexus.daemon.tuplespace_service import TuplespaceService
        from nexus.daemon import tuplespace_service as svc_mod

        # Force a tiny cap for the test.
        monkeypatch.setattr(svc_mod, "_BLOCKING_TAKE_MAX_CONCURRENT", 2)

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_chroma,
            registry=_registry,
        )
        try:
            errors: list[Exception] = []
            done = threading.Event()

            def _hold(idx: int, timeout: float) -> None:
                try:
                    service.blocking_take(
                        subspace="tasks/hr",
                        query="never",
                        claimant=f"hold-{idx}",
                        timeout_seconds=timeout,
                    )
                except Exception as exc:
                    errors.append(exc)

            # Two holders block on a non-existent tuple for ~1s each.
            holders = [
                threading.Thread(
                    target=_hold, args=(i, 1.0), daemon=True
                )
                for i in range(2)
            ]
            for t in holders:
                t.start()
            # Let them grab the semaphore slots.
            time.sleep(0.1)

            # The third call must fail loud rather than queue silently.
            from nexus.daemon.tuplespace_service import (
                BlockingTakeResourceExhausted,
            )
            with pytest.raises(BlockingTakeResourceExhausted):
                service.blocking_take(
                    subspace="tasks/hr",
                    query="anything",
                    claimant="overflow",
                    timeout_seconds=0.2,
                )

            for t in holders:
                t.join(timeout=2.0)
        finally:
            service.close()


# ---------------------------------------------------------------------------
# HR-2: find_t2_daemon honours the shutdown marker
# ---------------------------------------------------------------------------


class TestFindT2DaemonHonoursShutdownMarker:
    """A discovery file with status='shutting_down' must be treated as stale."""

    def test_shutting_down_status_yields_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.daemon import discovery

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        # Synth a marker-stamped file with a LIVE pid (this process).
        path = discovery.discovery_path(tmp_path)
        payload = {
            "uds_path": "/tmp/fake.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 9999,
            "pid": os.getpid(),  # alive
            "status": "shutting_down",
            "shutdown_at": "2026-05-17T05:00:00+00:00",
        }
        path.write_text(json.dumps(payload))

        # Even with a live pid, the marker must override.
        assert discovery.find_t2_daemon(tmp_path) is None

    def test_no_marker_still_returns_payload(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.daemon import discovery

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        path = discovery.discovery_path(tmp_path)
        payload = {
            "uds_path": "/tmp/fake.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 9999,
            "pid": os.getpid(),
        }
        path.write_text(json.dumps(payload))

        result = discovery.find_t2_daemon(tmp_path)
        assert result is not None
        assert result["pid"] == os.getpid()

    def test_marker_with_other_status_value_does_not_short_circuit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Future-proof: a 'running' or unknown status doesn't fire the gate."""
        from nexus.daemon import discovery

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        path = discovery.discovery_path(tmp_path)
        payload = {
            "uds_path": "/tmp/fake.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 9999,
            "pid": os.getpid(),
            "status": "running",
        }
        path.write_text(json.dumps(payload))
        result = discovery.find_t2_daemon(tmp_path)
        assert result is not None


# ---------------------------------------------------------------------------
# HR-3: blocking_take data_version polling is no longer vestigial
# (Code-level cleanup; behaviour-level test: the loop still works correctly.)
# ---------------------------------------------------------------------------


class TestBlockingTakeStillWorks:
    """Regression guard for the HR-3 simplification: behaviour unchanged."""

    def test_wait_then_hit_still_works(
        self, tmp_path: Path, _registry, _chroma
    ) -> None:
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_chroma,
            registry=_registry,
        )
        try:
            def _delayed_out() -> None:
                time.sleep(0.15)
                service.out(
                    subspace="tasks/hr",
                    content="ready",
                    dimensions={
                        "status": "open",
                        "priority": "P1",
                        "created_by": "x",
                    },
                )

            threading.Thread(target=_delayed_out, daemon=True).start()
            t0 = time.perf_counter()
            result = service.blocking_take(
                subspace="tasks/hr",
                query="ready",
                claimant="solo",
                timeout_seconds=5.0,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert result is not None
            assert 130 <= elapsed_ms <= 1500
            service.ack(claim_id=result["claim_id"], claimant="solo")
        finally:
            service.close()


# ---------------------------------------------------------------------------
# HR-4: _glob_to_prefix_range covers full Unicode (BMP boundary fix)
# ---------------------------------------------------------------------------


class TestGlobPrefixRangeFullUnicode:
    """Prefix glob upper sentinel must cover supplementary plane chars."""

    def test_prefix_range_upper_sentinel_above_supplementary(self) -> None:
        from nexus.cockpit.bindings import _glob_to_prefix_range

        result = _glob_to_prefix_range("tasks/*")
        assert result is not None
        lo, hi = result
        # The upper sentinel must compare greater than any UTF-8
        # supplementary-plane suffix.
        sample_emoji = "tasks/🔥"
        assert lo <= sample_emoji < hi, (
            f"upper sentinel {hi!r} does not cover supplementary plane "
            f"(got {sample_emoji!r} >= {hi!r})"
        )

    def test_prefix_range_includes_emoji_in_sql_range(
        self, tmp_path: Path
    ) -> None:
        """Direct DB exercise: rows with emoji subspace are reachable."""
        from nexus.cockpit.bindings import _build_fetch_event_batch_sql

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE events ("
            " rowid INTEGER PRIMARY KEY AUTOINCREMENT,"
            " subspace TEXT NOT NULL,"
            " op TEXT NOT NULL,"
            " tuple_id TEXT NOT NULL,"
            " payload_summary TEXT,"
            " category TEXT,"
            " ts REAL NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO events (subspace, op, tuple_id, payload_summary, "
            " category, ts) VALUES (?, 'out', 't1', '', 'data', 0.0)",
            ("tasks/🔥",),
        )
        conn.commit()

        sql, params = _build_fetch_event_batch_sql(
            subspace_glob="tasks/*", after_rowid=0, limit=10
        )
        rows = conn.execute(sql, params).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "tasks/🔥"
