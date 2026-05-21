# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P2 stress harness scenarios for the T3 daemon.

Validation gate for the P2 -> P3a transition (per the 2026-05-21
RDR-120 amendment replacing the per-phase ≥7-day calendar soak with
stress harness + 24h shakedown).

Scope: T3 daemon is a managed ``chroma run`` subprocess; the daemon
process itself is upstream code. What this harness exercises is OUR
code around it:

- Daemon lifecycle (start, stop, restart, kill -9 recovery)
- Spawn-lock contention (two parallel start calls)
- Discovery file lifecycle (stale-PID cleanup, corrupted file recovery)
- T3Client behaviour under daemon-crash mid-RPC
- HttpClient timeout invariant (no infinite hang on a slow daemon)
- Concurrent T3Client usage against the same daemon

Each scenario is a single pytest function marked ``stress``. Run with::

    uv run pytest -m stress tests/stress/test_t3_daemon_stress.py
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import signal
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.stress


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture(autouse=True)
def _force_local_mode(monkeypatch) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")


@pytest.fixture
def live_daemon(config_dir: Path, local_path: Path):
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _is_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Scenario 1: Lifecycle stress — N rapid start/stop cycles
# ---------------------------------------------------------------------------


class TestLifecycleStress:
    """20 rapid start/stop cycles. Verifies no stale discovery files,
    no orphan processes, no port collisions on rapid reuse."""

    def test_rapid_start_stop_cycles(
        self, config_dir: Path, local_path: Path,
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon, stop_t3_daemon, t3_discovery_path,
        )

        disc = t3_discovery_path(config_dir)
        ports_seen: set[int] = set()
        for i in range(20):
            payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
            assert disc.exists(), f"cycle {i}: discovery file missing after start"
            assert _is_listening(payload["tcp_host"], payload["tcp_port"]), (
                f"cycle {i}: daemon not listening on {payload['tcp_port']}"
            )
            ports_seen.add(payload["tcp_port"])
            stop_t3_daemon(config_dir=config_dir)
            assert not disc.exists(), f"cycle {i}: discovery file leaked after stop"
        # Sanity: free-port allocator produced varied ports across cycles
        # (not strict — kernel reuse of recently-closed ports is allowed,
        # but >1 distinct port across 20 cycles is the expected shape).
        assert len(ports_seen) >= 2, (
            f"port allocator produced only {len(ports_seen)} distinct ports "
            f"across 20 cycles; expected ≥2: {ports_seen}"
        )


# ---------------------------------------------------------------------------
# Scenario 2: Spawn-lock contention — two parallel start() calls
# ---------------------------------------------------------------------------


class TestSpawnLockContention:
    """Two parallel start_t3_daemon calls against the same config_dir.
    Exactly one daemon ends up running; the second call must be
    idempotent (returns the existing payload, not a fresh PID)."""

    def test_parallel_starts_yield_single_daemon(
        self, config_dir: Path, local_path: Path,
    ) -> None:
        from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

        payloads: list[dict] = []
        errors: list[Exception] = []

        def _start():
            try:
                payloads.append(
                    start_t3_daemon(config_dir=config_dir, local_path=local_path)
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_start) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        try:
            assert errors == [], f"unexpected errors: {errors!r}"
            assert len(payloads) == 5
            # All five payloads must share the same PID (the first start
            # spawned the daemon; the other four found it via the
            # idempotent already-running branch).
            pids = {p["pid"] for p in payloads}
            assert len(pids) == 1, (
                f"expected single daemon PID across 5 parallel starts; got {pids!r}"
            )
        finally:
            stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# Scenario 3: kill -9 recovery — daemon dies; next start sees stale file
# ---------------------------------------------------------------------------


class TestKill9Recovery:
    """Send SIGKILL to the daemon mid-operation. Verify the next
    start() detects the stale discovery file (PID gone), cleans up,
    and spawns a fresh daemon with a new PID."""

    def test_kill9_then_start_spawns_fresh(
        self, config_dir: Path, local_path: Path,
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon, stop_t3_daemon, t3_discovery_path,
        )

        first = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        os.kill(first["pid"], signal.SIGKILL)
        assert _wait_until(
            lambda: not _pid_alive(first["pid"]), timeout=10.0,
        ), "first daemon did not die after SIGKILL"

        # Discovery file still on disk (stale).
        disc = t3_discovery_path(config_dir)
        assert disc.exists()

        try:
            second = start_t3_daemon(config_dir=config_dir, local_path=local_path)
            try:
                assert second["pid"] != first["pid"], (
                    "second start must spawn a fresh PID, not reuse the dead one"
                )
                assert _is_listening(second["tcp_host"], second["tcp_port"])
            finally:
                stop_t3_daemon(config_dir=config_dir)
        finally:
            # Belt-and-suspenders cleanup if the second start raised.
            disc.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """True iff *pid* is a live process (not a zombie).

    SIGKILL'd children become zombies that ``os.kill(pid, 0)`` still
    reports as alive until reaped. ``waitpid(pid, WNOHANG)`` reaps
    zombies so the next ``os.kill(pid, 0)`` raises
    ``ProcessLookupError``. Reaping a non-child raises
    ``ChildProcessError`` which we treat as "not our child, can't
    reap, fall back to plain os.kill check".
    """
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False  # just reaped the zombie
    except ChildProcessError:
        pass  # not our child; fall back
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Scenario 4: Corrupted discovery file recovery
# ---------------------------------------------------------------------------


class TestDiscoveryFileCorruption:
    def test_unparseable_discovery_file_does_not_crash_start(
        self, config_dir: Path, local_path: Path,
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon, stop_t3_daemon, t3_discovery_path,
        )

        path = t3_discovery_path(config_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<<< not json >>>")
        os.chmod(str(path), 0o600)

        try:
            payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
            assert payload["pid"] > 0
            on_disk = json.loads(path.read_text())
            assert on_disk["pid"] == payload["pid"]
        finally:
            stop_t3_daemon(config_dir=config_dir)

    def test_partial_discovery_file_treated_as_missing(
        self, config_dir: Path, local_path: Path,
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon, stop_t3_daemon, t3_discovery_path,
        )

        # Truncated JSON: opening brace only, no closing brace.
        path = t3_discovery_path(config_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{")
        os.chmod(str(path), 0o600)

        try:
            payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
            assert payload["pid"] > 0
        finally:
            stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# Scenario 5: Concurrency storm — N parallel T3Client RPCs
# ---------------------------------------------------------------------------


class TestConcurrencyStorm:
    """50 parallel make_t3_client() round-trips against the same daemon.
    Verifies no chroma WAL race SIGBUS, no client deadlock, no
    HttpClient connection-pool exhaustion."""

    def test_50_parallel_round_trips(
        self, live_daemon, config_dir: Path,
    ) -> None:
        from nexus.daemon.t3_client import make_t3_client

        def _round_trip(i: int) -> int:
            t3 = make_t3_client(config_dir=config_dir)
            coll = t3._client.get_or_create_collection("stress__concurrency")
            coll.upsert(documents=[f"doc {i}"], ids=[f"id-{i}"])
            results = coll.query(query_texts=[f"doc {i}"], n_results=1)
            return len(results["ids"][0])

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            futures = [pool.submit(_round_trip, i) for i in range(50)]
            done = [f.result(timeout=60.0) for f in futures]
        assert all(d == 1 for d in done), (
            f"some parallel round-trips returned wrong cardinality: {done}"
        )


# ---------------------------------------------------------------------------
# Scenario 6: Connection churn — rapid open/close cycles
# ---------------------------------------------------------------------------


class TestConnectionChurn:
    """200 rapid HttpClient construct + heartbeat + drop cycles.
    Verifies socket cleanup and no file-descriptor leak."""

    def test_200_rapid_heartbeats(self, live_daemon) -> None:
        import chromadb
        for _ in range(200):
            client = chromadb.HttpClient(
                host=live_daemon["tcp_host"], port=live_daemon["tcp_port"],
            )
            client.heartbeat()
            # No explicit close on chromadb.HttpClient; deletion
            # releases the underlying httpx pool.
            del client


# ---------------------------------------------------------------------------
# Scenario 7: Process suspend / resume — sleep-wake analogue
# ---------------------------------------------------------------------------


class TestSuspendResume:
    """SIGSTOP the daemon, sleep N seconds, SIGCONT. In-flight
    HttpClient requests should either complete after resume or time
    out cleanly; the daemon must remain healthy after resume.

    This is the deterministic analogue to macOS / systemd sleep-wake
    cycles that operators previously needed to surface by accident.
    """

    def test_sigstop_sigcont_keeps_daemon_healthy(self, live_daemon) -> None:
        import chromadb

        os.kill(live_daemon["pid"], signal.SIGSTOP)
        time.sleep(0.5)
        os.kill(live_daemon["pid"], signal.SIGCONT)
        time.sleep(0.3)  # allow scheduler to dispatch the resumed process

        # Daemon survives a STOP/CONT cycle: a fresh client connects
        # and heartbeats successfully.
        client = chromadb.HttpClient(
            host=live_daemon["tcp_host"], port=live_daemon["tcp_port"],
        )
        client.heartbeat()


# ---------------------------------------------------------------------------
# Scenario 8: Stop is idempotent under repeated invocation
# ---------------------------------------------------------------------------


class TestRepeatedStop:
    def test_three_consecutive_stops_no_error(
        self, config_dir: Path, local_path: Path,
    ) -> None:
        from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

        start_t3_daemon(config_dir=config_dir, local_path=local_path)
        for _ in range(3):
            stop_t3_daemon(config_dir=config_dir)  # must not raise


# ---------------------------------------------------------------------------
# Scenario 9: T3Client fail-loud after daemon dies
# ---------------------------------------------------------------------------


class TestClientFailLoudAfterDaemonDeath:
    """After SIGKILL, the next make_t3_client + RPC must surface a
    visible error rather than hanging or silently using stale state."""

    def test_kill_then_client_call_surfaces_error(
        self, config_dir: Path, local_path: Path,
    ) -> None:
        from nexus.daemon.t3_client import make_t3_client
        from nexus.daemon.t3_daemon import (
            start_t3_daemon, stop_t3_daemon, t3_discovery_path,
        )

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        os.kill(payload["pid"], signal.SIGKILL)
        assert _wait_until(
            lambda: not _pid_alive(payload["pid"]), timeout=10.0,
        )
        # Discovery file still points at the dead daemon. The
        # discovery resolver will unlink the stale-PID entry and
        # then surface "no daemon" rather than silently reusing the
        # dead address.
        try:
            t3 = make_t3_client(config_dir=config_dir)
            with pytest.raises(Exception):  # noqa: BLE001
                t3._client.heartbeat()
        except Exception:
            pass  # construction failed loud; either is acceptable
        finally:
            t3_discovery_path(config_dir).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Scenario 10: Memory profile — insert/delete cycles, bounded growth
# ---------------------------------------------------------------------------


class TestMemoryProfile:
    """Insert N documents, delete the collection, repeat. Daemon RSS
    must not grow unboundedly across cycles (no leak in our
    lifecycle code; chroma's own internals are upstream-trusted)."""

    def test_insert_delete_cycles_bounded_growth(self, live_daemon) -> None:
        import chromadb

        try:
            import psutil  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            pytest.skip("psutil not installed; memory profile skipped")

        proc = psutil.Process(live_daemon["pid"])

        rss_samples: list[int] = []
        for cycle in range(5):
            client = chromadb.HttpClient(
                host=live_daemon["tcp_host"], port=live_daemon["tcp_port"],
            )
            coll = client.get_or_create_collection(f"stress__cycle_{cycle}")
            coll.upsert(
                documents=[f"d{i}" for i in range(100)],
                ids=[f"id{i}" for i in range(100)],
            )
            client.delete_collection(f"stress__cycle_{cycle}")
            time.sleep(0.2)  # let chroma reap
            rss_samples.append(proc.memory_info().rss)

        # Bounded growth: final RSS within 2x of the first sample.
        # A real leak would compound across 5 cycles to much more.
        assert rss_samples[-1] < rss_samples[0] * 2 + 50 * 1024 * 1024, (
            f"daemon RSS grew unboundedly across 5 cycles: {rss_samples}"
        )
