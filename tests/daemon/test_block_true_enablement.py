# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ry0v: client-side block=True enablement (RDR-110 P3.1).

Wires ``T2Client.tuplespace.take(block=True, ...)`` to dispatch to
the daemon's ``blocking_take`` RPC (nexus-73vq) so existing
``take()`` callers get the blocking semantics by flipping a flag.

Parallel-tracks the daemon-side service: ``TuplespaceService.take(
block=True, ...)`` internally delegates to ``blocking_take`` rather
than forwarding ``block=True`` to ``api.take`` (which still raises
``BlockingNotSupported`` for the direct-mode path).

Tests cover the end-to-end client RPC path and the service-internal
delegation.
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
from nexus.tuplespace.registry import Registry


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
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture()
def chroma_client() -> chromadb.EphemeralClient:
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


def _short_config_dir() -> Path:
    """Tempdir under /tmp (macOS 104-char UDS path cap), cleaned at exit.

    nexus-xc3w (S360-test F2): register an atexit shutil.rmtree so the
    daemon's tuples.db + WAL + UDS socket do not accumulate across
    test runs. Per-test cleanup is intentionally NOT used because the
    daemon often outlives the test function (started in a sibling
    thread); a process-exit hook is the safest place to reap.
    """
    import atexit as _atexit
    import shutil as _shutil
    import tempfile as _tempfile
    d = Path(_tempfile.mkdtemp(prefix="ry0v-", dir="/tmp"))
    _atexit.register(_shutil.rmtree, str(d), ignore_errors=True)
    return d


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
    # nexus-xc3w (S360-test F3): loop.stop() exits run_forever but
    # leaves the loop's internal fds open. Close from outside the
    # loop thread is safe once run_forever has returned; schedule
    # via call_soon_threadsafe to enforce that ordering.
    loop.call_soon_threadsafe(loop.close)


def _dims() -> dict[str, Any]:
    return {"status": "open", "priority": "P1", "created_by": "harness"}


# ---------------------------------------------------------------------------
# Service-internal delegation: TuplespaceService.take(block=True) routes
# to blocking_take rather than forwarding to api.take(block=True).
# ---------------------------------------------------------------------------


class TestServiceTakeDelegatesBlockTrue:
    """``TuplespaceService.take(block=True, ...)`` delegates to
    ``blocking_take`` instead of raising ``BlockingNotSupported``.
    """

    def test_take_block_true_returns_candidate(
        self, tmp_path: Path, registry: Registry, chroma_client
    ) -> None:
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=chroma_client,
            registry=registry,
        )
        try:
            service.out(
                subspace="tasks/ry0v",
                content="ready",
                dimensions=_dims(),
            )
            result = service.take(
                subspace="tasks/ry0v",
                query="ready",
                claimant="solo",
                block=True,
                timeout_seconds=5.0,
            )
            assert result is not None
            assert "claim_id" in result
            assert "tuple" in result
            service.ack(claim_id=result["claim_id"], claimant="solo")
        finally:
            service.close()

    def test_take_block_true_returns_none_on_timeout(
        self, tmp_path: Path, registry: Registry, chroma_client
    ) -> None:
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=chroma_client,
            registry=registry,
        )
        try:
            t0 = time.perf_counter()
            result = service.take(
                subspace="tasks/ry0v",
                query="nothing-here",
                claimant="solo",
                block=True,
                timeout_seconds=0.3,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert result is None
            assert 250 <= elapsed_ms <= 1000, (
                f"timeout near 300ms; got {elapsed_ms:.0f}ms"
            )
        finally:
            service.close()

    def test_take_block_false_still_uses_api_take(
        self, tmp_path: Path, registry: Registry, chroma_client
    ) -> None:
        """``block=False`` keeps the legacy fast-path through api.take()."""
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=chroma_client,
            registry=registry,
        )
        try:
            # No tuples; non-blocking take returns None immediately.
            t0 = time.perf_counter()
            result = service.take(
                subspace="tasks/ry0v",
                query="anything",
                claimant="solo",
                block=False,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert result is None
            assert elapsed_ms < 200, (
                f"block=False must be immediate; got {elapsed_ms:.0f}ms"
            )
        finally:
            service.close()


# ---------------------------------------------------------------------------
# End-to-end client RPC: T2Client.tuplespace.take(block=True) over UDS
# ---------------------------------------------------------------------------


class TestClientTakeBlockTrueOverRpc:
    """``T2Client.tuplespace.take(block=True, ...)`` dispatches to the
    daemon's ``blocking_take`` RPC and surfaces the result.
    """

    def test_client_take_block_true_wait_then_hit(
        self, registry: Registry, chroma_client
    ) -> None:
        """The client times out the legitimate wait without RpcTimeoutError."""
        config_dir = _short_config_dir()
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
            client = T2Client(uds_path=daemon.uds_path)

            # Sibling out after a short delay.
            def _delayed_out() -> None:
                time.sleep(0.2)
                service.out(
                    subspace="tasks/ry0v",
                    content="delayed",
                    dimensions=_dims(),
                )

            threading.Thread(target=_delayed_out, daemon=True).start()

            t0 = time.perf_counter()
            result = client.tuplespace.take(
                subspace="tasks/ry0v",
                query="delayed",
                claimant="rpc-client",
                block=True,
                timeout_seconds=5.0,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            assert result is not None
            assert "claim_id" in result
            # Wake should land roughly when the sibling out fires.
            assert 180 <= elapsed_ms <= 1500, (
                f"wake near 200ms expected; got {elapsed_ms:.0f}ms"
            )
            client.tuplespace.ack(
                claim_id=result["claim_id"], claimant="rpc-client"
            )
            client.close()
        finally:
            _stop_daemon_in_thread(daemon, loop)
            service.close()

    def test_client_take_block_true_returns_none_on_timeout(
        self, registry: Registry, chroma_client
    ) -> None:
        """The legitimate blocking wait is not killed by the default
        ``rpc_timeout_seconds`` of 5 s; the client receives ``None`` after
        ``timeout_seconds`` elapses on the daemon side.
        """
        config_dir = _short_config_dir()
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
            client = T2Client(uds_path=daemon.uds_path)
            t0 = time.perf_counter()
            result = client.tuplespace.take(
                subspace="tasks/ry0v",
                query="nothing-here",
                claimant="rpc-client",
                block=True,
                timeout_seconds=0.4,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert result is None
            assert 350 <= elapsed_ms <= 1500, (
                f"timeout near 400ms; got {elapsed_ms:.0f}ms"
            )
            client.close()
        finally:
            _stop_daemon_in_thread(daemon, loop)
            service.close()
