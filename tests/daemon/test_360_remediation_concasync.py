# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second 360° remediation Bundle B (nexus-abhy + nexus-71kc).

S360-conc S1 (nexus-abhy): blocking_take's poll must be interruptible
so daemon shutdown does not stall up to 30 seconds waiting for
in-flight executor threads to wind down their timers.

S360-conc S2 = S360-async S3 (dedup): the startup retention sweep
future is held but never awaited. Stop must:
- Attach a done callback that logs exceptions (so a failed sweep
  surfaces in operator output rather than dying silently at
  Future-GC time).
- Await the future before closing the tuplespace SQLite connection
  so a still-running DELETE does not race conn.close().

S360-async S1 (nexus-71kc): _liveness_heartbeat_task._beat() and
the lifespan-exit liveness_delete must dispatch their sync sqlite
work via asyncio.to_thread so they do not block the MCP event loop.

S360-async S2 (nexus-71kc): the console health route handlers must
dispatch _collect_health_data via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import chromadb
import pytest


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
def _chroma():
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


# ---------------------------------------------------------------------------
# S360-conc S1 (nexus-abhy): blocking_take interruptible by service.close()
# ---------------------------------------------------------------------------


class TestS360ConcInterruptibleBlockingTake:
    def test_close_unblocks_blocking_take_quickly(
        self, tmp_path: Path, _registry, _chroma
    ) -> None:
        """A blocking_take with a 30 s budget must return shortly after
        close() rather than waiting out its full timeout. The fix is
        for the poll loop to check the service's shutdown signal and
        for close() to fire the wake_event so any in-flight waiter
        returns immediately.
        """
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_chroma,
            registry=_registry,
        )

        elapsed_ms: list[float] = []
        result_holder: list[object] = []

        def _wait() -> None:
            t0 = time.perf_counter()
            try:
                r = service.blocking_take(
                    subspace="tasks/concasync",
                    query="never",
                    claimant="solo",
                    timeout_seconds=30.0,
                )
            except Exception:
                r = None
            elapsed_ms.append((time.perf_counter() - t0) * 1000.0)
            result_holder.append(r)

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        # Let the take settle into wait().
        time.sleep(0.05)
        service.close()
        t.join(timeout=3.0)
        assert not t.is_alive(), (
            "blocking_take did not return inside 3 s of close()"
        )
        assert elapsed_ms[0] < 2000.0, (
            f"blocking_take woke too slowly on close — {elapsed_ms[0]:.1f}ms"
        )


# ---------------------------------------------------------------------------
# S360-conc S2 / S360-async S3: startup sweep future awaited + callback'd
# ---------------------------------------------------------------------------


class TestS360StartupSweepDrained:
    def test_stop_waits_for_startup_sweep_future(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """T2Daemon.stop() must await self._startup_sweep_future before
        closing the underlying SQLite connection. We inject a sweep
        that holds a sentinel event and verify stop() does not return
        until the sweep is released.
        """
        from nexus.daemon.t2_daemon import T2Daemon

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        sweep_entered = threading.Event()
        sweep_release = threading.Event()
        sweep_finished_at: list[float] = []

        async def _run() -> None:
            daemon = T2Daemon(config_dir=tmp_path)

            def _slow_sweep() -> int:
                sweep_entered.set()
                # Hold until the test releases us OR a hard timeout.
                sweep_release.wait(timeout=5.0)
                sweep_finished_at.append(time.perf_counter())
                return 0

            monkeypatch.setattr(daemon, "_run_retention_sweep_sync", _slow_sweep)
            await daemon.start()
            assert sweep_entered.wait(timeout=2.0), (
                "sweep future never started"
            )

            stop_task = asyncio.create_task(daemon.stop())
            # stop() should be pending while the sweep is held.
            await asyncio.sleep(0.1)
            assert not stop_task.done(), (
                "stop() returned before the held sweep released — "
                "future is not being awaited"
            )

            stop_started_at = time.perf_counter()
            sweep_release.set()
            await asyncio.wait_for(stop_task, timeout=5.0)
            assert sweep_finished_at, "sweep never completed"
            # stop() returned AFTER the sweep released (i.e. it awaited).
            assert sweep_finished_at[0] >= stop_started_at - 0.05, (
                "stop() returned without awaiting the sweep future"
            )

        asyncio.run(_run())

    def test_startup_sweep_failure_is_logged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A sweep that raises must surface in operator logs rather
        than dying silently at Future-GC time.

        Uses ``structlog.testing.capture_logs`` because the project
        ships its logs through structlog (caplog only captures stdlib
        ``logging`` records).
        """
        import structlog.testing
        from nexus.daemon.t2_daemon import T2Daemon

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        sweep_done = threading.Event()

        async def _run(captured: list[dict]) -> None:
            daemon = T2Daemon(config_dir=tmp_path)

            def _boom() -> int:
                try:
                    raise RuntimeError("sweep failed (synthetic)")
                finally:
                    sweep_done.set()

            monkeypatch.setattr(daemon, "_run_retention_sweep_sync", _boom)
            await daemon.start()
            assert sweep_done.wait(timeout=2.0), "sweep never ran"
            await asyncio.sleep(0.1)
            await daemon.stop()

        with structlog.testing.capture_logs() as captured:
            asyncio.run(_run(captured))

        events = [rec.get("event", "") for rec in captured]
        assert any("startup_sweep_failed" in e for e in events), (
            "no startup_sweep_failed log emitted — exception swallowed "
            f"at Future-GC. Events: {events}"
        )


# ---------------------------------------------------------------------------
# S360-async S1 / S2: source-text guards for asyncio.to_thread dispatch
# ---------------------------------------------------------------------------


class TestS360AsyncWrappersInPlace:
    """Source-text guards that the loop-thread sqlite hot paths now
    dispatch via asyncio.to_thread."""

    def _read(self, rel: str) -> str:
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parent.parent.parent
        return (root / "src" / "nexus" / rel).read_text()

    def test_mcp_heartbeat_wraps_t2_ctx_in_to_thread(self) -> None:
        body = self._read("mcp/core.py")
        # The _beat() inner function and the lifespan-exit
        # liveness_delete must both dispatch the sync _t2_ctx() block
        # via asyncio.to_thread. The grep is intentionally tolerant of
        # exact spelling so a future refactor that uses
        # ``loop.run_in_executor`` style still passes.
        assert "asyncio.to_thread" in body or "run_in_executor" in body, (
            "mcp/core.py heartbeat / lifespan-exit must dispatch their "
            "sqlite work off the event loop."
        )
        # Stricter: at least one to_thread call appears in the file.
        # Catches the obvious regression of dropping the wrapper.
        assert body.count("asyncio.to_thread") >= 1

    def test_console_health_routes_wrap_collect_in_to_thread(self) -> None:
        body = self._read("console/routes/health.py")
        assert "asyncio.to_thread(_collect_health_data" in body, (
            "console health routes must await asyncio.to_thread("
            "_collect_health_data) so a slow sqlite probe does not "
            "block the route loop."
        )
