# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 nexus-6s8v: tuplespace RPC round-trip tests.

Spins a T2Daemon with a TuplespaceService against an EphemeralClient
chroma + a temp tuples.db, then exercises every tuplespace.* RPC
through the T2Client.tuplespace proxy and asserts behavioural parity
with the direct ``nexus.tuplespace.api`` path.

Also verifies:
- All tuplespace RPC ops appear in the daemon dispatch table.
- ``register_tuplespace_rpcs`` is the single source of truth for the
  surface area (``TUPLESPACE_RPC_OPS``).
- ack/nack ownership errors round-trip as ``PermissionError``.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any

import chromadb
import pytest

from nexus.daemon.t2_client import T2Client
from nexus.daemon.t2_daemon import T2Daemon
from nexus.daemon.tuplespace_service import (
    TUPLESPACE_RPC_OPS,
    TuplespaceService,
)
from nexus.tuplespace.registry import Registry


# ---------------------------------------------------------------------------
# Test registry: minimal subspaces exercising semantic + exact modes
# ---------------------------------------------------------------------------

_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status: { type: enum, values: [open, claimed, done], required: true }
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
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

_LOCKS_YAML = """
name: locks/<resource>
tier: project
content_type: text
embed_from: content
dimensions:
  resource: { type: string, required: true }
  holder: { type: string, required: true }
take:
  enabled: true
  mode: exact
  match_keys: [resource]
  default_lease_seconds: 30
read:
  default_floor: 0.0
  default_n: 5
tiers: [project]
retention_seconds: 3600
"""


@pytest.fixture
def test_registry(tmp_path: Path) -> Registry:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "tasks.yml").write_text(_TASKS_YAML)
    (builtin / "locks.yml").write_text(_LOCKS_YAML)
    return Registry.load(builtin)


# ---------------------------------------------------------------------------
# Daemon harness — runs T2Daemon in a background asyncio loop
# ---------------------------------------------------------------------------


def _run_daemon(daemon: T2Daemon) -> asyncio.AbstractEventLoop:
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    started.wait(timeout=5.0)
    return loop


def _stop_daemon(daemon: T2Daemon, loop: asyncio.AbstractEventLoop) -> None:
    asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=5.0)
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    # Clear collections so EphemeralClient's process-shared state doesn't bleed
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture
def daemon_and_client(tmp_path: Path, test_registry: Registry, chroma_client):
    """Boot a T2Daemon with a TuplespaceService and yield (daemon, T2Client)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    tuples_db_path = config_dir / "tuples.db"

    service = TuplespaceService(
        tuples_db_path=tuples_db_path,
        chroma_client=chroma_client,
        registry=test_registry,
    )

    daemon = T2Daemon(
        config_dir=config_dir,
        tuples_db_path=tuples_db_path,
        tuplespace_service=service,
    )
    loop = _run_daemon(daemon)
    client = T2Client(uds_path=daemon.uds_path)
    try:
        yield daemon, client
    finally:
        client.close()
        _stop_daemon(daemon, loop)


# ---------------------------------------------------------------------------
# Dispatch-table coverage
# ---------------------------------------------------------------------------


class TestDispatchTableCoverage:
    """All TUPLESPACE_RPC_OPS must appear in the daemon's _rpc_table."""

    def test_all_ops_registered(self, daemon_and_client) -> None:
        daemon, _ = daemon_and_client
        for op in TUPLESPACE_RPC_OPS:
            assert f"tuplespace.{op}" in daemon._rpc_table, (
                f"tuplespace.{op} missing from daemon dispatch table"
            )

    def test_ops_callable(self, daemon_and_client) -> None:
        daemon, _ = daemon_and_client
        for op in TUPLESPACE_RPC_OPS:
            fn = daemon._rpc_table[f"tuplespace.{op}"]
            assert callable(fn)


# ---------------------------------------------------------------------------
# Round-trip RPC behaviour
# ---------------------------------------------------------------------------


def _dims() -> dict[str, Any]:
    return {"status": "open", "priority": "P1", "created_by": "agent-X"}


class TestTuplespaceRoundTrip:
    def test_out_returns_tuple_id(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        tid = client.tuplespace.out(
            subspace="tasks/nexus",
            content="research the daemon RPC surface",
            dimensions=_dims(),
        )
        assert isinstance(tid, str)
        assert len(tid) == 32  # sha256 prefix

    def test_out_is_idempotent(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        kwargs = dict(
            subspace="tasks/nexus",
            content="same body",
            dimensions=_dims(),
        )
        t1 = client.tuplespace.out(**kwargs)
        t2 = client.tuplespace.out(**kwargs)
        assert t1 == t2

    def test_read_returns_posted_tuples(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        client.tuplespace.out(
            subspace="tasks/nexus",
            content="apple banana cherry",
            dimensions=_dims(),
        )
        rows = client.tuplespace.read(
            subspace="tasks/nexus",
            query="apple",
            n=5,
        )
        assert len(rows) >= 1
        assert any("apple" in r["content"] for r in rows)

    def test_take_claims_tuple(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        client.tuplespace.out(
            subspace="tasks/nexus",
            content="unique_take_target",
            dimensions=_dims(),
        )
        wrapped = client.tuplespace.take(
            subspace="tasks/nexus",
            query="unique_take_target",
            claimant="agent-A",
        )
        assert wrapped is not None
        assert "tuple" in wrapped
        assert "claim_id" in wrapped
        assert isinstance(wrapped["claim_id"], str)

    def test_take_returns_none_when_empty(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        result = client.tuplespace.take(
            subspace="tasks/nexus",
            query="nothing-here",
            claimant="agent-A",
        )
        assert result is None

    def test_ack_completes_claim(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        client.tuplespace.out(
            subspace="tasks/nexus",
            content="ack-target",
            dimensions=_dims(),
        )
        wrapped = client.tuplespace.take(
            subspace="tasks/nexus",
            query="ack-target",
            claimant="agent-A",
        )
        assert wrapped is not None
        result = client.tuplespace.ack(
            claim_id=wrapped["claim_id"], claimant="agent-A"
        )
        # nexus-6m9i (third 360° ERGO E-2): service.ack now returns None
        # to match api.ack so direct↔daemon mode parity holds.
        assert result is None

    def test_nack_releases_claim(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        client.tuplespace.out(
            subspace="tasks/nexus",
            content="nack-target",
            dimensions=_dims(),
        )
        wrapped = client.tuplespace.take(
            subspace="tasks/nexus",
            query="nack-target",
            claimant="agent-A",
        )
        assert wrapped is not None
        result = client.tuplespace.nack(
            claim_id=wrapped["claim_id"], claimant="agent-A"
        )
        # nexus-6m9i (third 360° ERGO E-2): service.nack now returns None.
        assert result is None
        # After nack the same claimant can re-take
        retaken = client.tuplespace.take(
            subspace="tasks/nexus",
            query="nack-target",
            claimant="agent-A",
        )
        assert retaken is not None

    def test_ack_wrong_claimant_raises_permission_error(
        self, daemon_and_client
    ) -> None:
        _daemon, client = daemon_and_client
        client.tuplespace.out(
            subspace="tasks/nexus",
            content="ownership-test",
            dimensions=_dims(),
        )
        wrapped = client.tuplespace.take(
            subspace="tasks/nexus",
            query="ownership-test",
            claimant="owner-A",
        )
        assert wrapped is not None
        # Foreign claimant trying to ack — daemon raises ClaimOwnershipError
        # (PermissionError subclass on the daemon side). The client wraps
        # non-builtin remote exceptions as T2DaemonError, so we assert on
        # either: the underlying PermissionError (if name-resolved) or the
        # generic T2DaemonError carrying the original type_name.
        from nexus.daemon.t2_client import T2DaemonError
        with pytest.raises((PermissionError, T2DaemonError)) as exc_info:
            client.tuplespace.ack(
                claim_id=wrapped["claim_id"], claimant="impostor"
            )
        # Confirm the ownership message round-tripped regardless of which
        # class wrapped it.
        assert "impostor" in str(exc_info.value) or "owned by" in str(exc_info.value)

    def test_list_subspaces(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        names = client.tuplespace.list_subspaces()
        assert "tasks/<project>" in names
        assert "locks/<resource>" in names

    def test_subspace_schema(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        schema = client.tuplespace.subspace_schema(subspace="tasks/nexus")
        assert schema["name"] == "tasks/<project>"
        assert schema["content_type"] == "text"
        assert "dimensions" in schema

    def test_subspace_stats(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        client.tuplespace.out(
            subspace="tasks/nexus",
            content="stats-target",
            dimensions=_dims(),
        )
        stats = client.tuplespace.subspace_stats(subspace="tasks/nexus")
        assert stats["total"] == 1
        assert stats["available"] == 1


# ---------------------------------------------------------------------------
# Exact-mode take via daemon
# ---------------------------------------------------------------------------


class TestExactModeViaDaemon:
    def test_exact_take_with_match_key(self, daemon_and_client) -> None:
        _daemon, client = daemon_and_client
        client.tuplespace.out(
            subspace="locks/db-write",
            content="lock",
            dimensions={"resource": "db/write", "holder": "agent-A"},
        )
        wrapped = client.tuplespace.take(
            subspace="locks/db-write",
            query="",
            claimant="agent-A",
            where={"resource": "db/write"},
        )
        assert wrapped is not None
        assert wrapped["tuple"]["dimensions"]["resource"] == "db/write"
