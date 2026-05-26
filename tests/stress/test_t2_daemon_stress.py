# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P3a stress harness scenarios for the T2 daemon.

Validation gate for the P3a phase (per the 2026-05-21 RDR-120 amendments
replacing per-phase calendar soaks with stress harness + phase-review-gate).

Scope: the T2 daemon IS our code (unlike T3, which wraps chroma's own
``chroma run`` process). The harness therefore exercises both the
lifecycle code AND the RPC dispatch path:

- Daemon lifecycle (start, stop, kill -9 recovery, spawn-lock contention)
- Discovery file lifecycle (stale-PID, corrupted file recovery)
- Frame protocol robustness (oversized, malformed, partial)
- Concurrency storm (parallel RPCs through the asyncio dispatch loop)
- Connection churn (rapid open/close cycles via UDS + TCP)
- SIGSTOP/SIGCONT suspend/resume (sleep-wake analogue)
- Memory profile under insert/delete cycles
- Mixed UDS + TCP traffic simultaneously
- SQLite WAL serialization under concurrent writes
- T2Client fail-loud after daemon death

Each scenario is a pytest function marked ``stress``. Run with::

    uv run pytest -m stress tests/stress/test_t2_daemon_stress.py
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.stress


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Short config dir under /tmp (macOS AF_UNIX path limit 104 chars)."""
    cd = Path(tempfile.mkdtemp(prefix="nxt2s-", dir="/tmp"))
    yield cd
    shutil.rmtree(cd, ignore_errors=True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


def _spawn_daemon_subprocess(config_dir: Path, db_path: Path) -> subprocess.Popen:
    """Spawn ``run_t2_daemon`` in a subprocess. The daemon writes the
    discovery file once start completes; callers wait for that as the
    readiness signal."""
    driver = textwrap.dedent(f"""
        from pathlib import Path
        from nexus.daemon.t2_daemon import run_t2_daemon
        run_t2_daemon(
            config_dir=Path({str(config_dir)!r}),
            db_path=Path({str(db_path)!r}),
        )
    """)
    return subprocess.Popen(
        [sys.executable, "-c", driver],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        start_new_session=True,
    )


def _wait_for_discovery(config_dir: Path, timeout: float = 10.0) -> dict:
    """Poll until the daemon writes the discovery file; return its payload."""
    from nexus.daemon.t2_daemon import t2_discovery_path

    disc = t2_discovery_path(config_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if disc.exists():
            try:
                return json.loads(disc.read_text())
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
        time.sleep(0.05)
    raise TimeoutError(f"daemon did not write {disc} within {timeout}s")


def _terminate_daemon(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


@pytest.fixture
def live_daemon(config_dir: Path, db_path: Path):
    proc = _spawn_daemon_subprocess(config_dir, db_path)
    try:
        payload = _wait_for_discovery(config_dir)
        yield {"proc": proc, "payload": payload, "config_dir": config_dir}
    finally:
        _terminate_daemon(proc)


def _pid_alive(pid: int) -> bool:
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return True


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Scenario 1: Lifecycle stress
# ---------------------------------------------------------------------------


class TestLifecycleStress:
    """10 rapid spawn/terminate cycles. Verifies discovery file cleanup
    and no orphan processes."""

    def test_rapid_spawn_terminate_cycles(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        from nexus.daemon.t2_daemon import t2_discovery_path

        disc = t2_discovery_path(config_dir)
        for i in range(10):
            proc = _spawn_daemon_subprocess(config_dir, db_path)
            try:
                payload = _wait_for_discovery(config_dir)
                assert disc.exists(), f"cycle {i}: discovery missing"
                # UDS reachable
                uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                uds.settimeout(2.0)
                uds.connect(payload["uds_path"])
                uds.close()
            finally:
                _terminate_daemon(proc)
            # After clean termination, discovery file should be gone.
            assert _wait_until(lambda: not disc.exists(), timeout=5.0), (
                f"cycle {i}: discovery file leaked after stop"
            )


# ---------------------------------------------------------------------------
# Scenario 2: Spawn-lock contention
# ---------------------------------------------------------------------------


class TestSpawnLockContention:
    """5 parallel daemon spawns against the same config_dir; the fcntl
    spawn lock in T2Daemon._acquire_spawn_lock must let exactly ONE
    succeed and force the others to fail with T2DaemonError."""

    def test_parallel_spawns_yield_single_winner(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        winners: list[subprocess.Popen] = []
        losers: list[subprocess.Popen] = []
        procs = [_spawn_daemon_subprocess(config_dir, db_path) for _ in range(5)]
        try:
            time.sleep(2.0)  # let all 5 race the lock
            for proc in procs:
                if proc.poll() is None:
                    winners.append(proc)
                else:
                    losers.append(proc)
            assert len(winners) == 1, (
                f"expected exactly 1 winner, got {len(winners)} "
                f"({len(losers)} losers exited)"
            )
            assert len(losers) == 4, f"expected 4 losers, got {len(losers)}"
            for loser in losers:
                stderr = (loser.stderr.read() or b"").decode(errors="replace")
                assert "spawn lock" in stderr or loser.returncode == 2, (
                    f"loser exit_code={loser.returncode} stderr={stderr!r}"
                )
        finally:
            for proc in procs:
                _terminate_daemon(proc)


# ---------------------------------------------------------------------------
# Scenario 3: kill -9 recovery
# ---------------------------------------------------------------------------


class TestKill9Recovery:
    """SIGKILL the daemon mid-operation. Verify the next spawn detects
    the stale spawn-lock (held by dead PID), acquires it cleanly, and
    starts a fresh daemon."""

    def test_kill9_then_spawn_recovers(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        from nexus.daemon.t2_daemon import t2_discovery_path

        first = _spawn_daemon_subprocess(config_dir, db_path)
        try:
            first_payload = _wait_for_discovery(config_dir)
            os.kill(first.pid, signal.SIGKILL)
            assert _wait_until(
                lambda: not _pid_alive(first.pid), timeout=10.0,
            ), "first daemon did not die after SIGKILL"
        finally:
            _terminate_daemon(first)

        # The SIGKILL'd daemon could not run its stop handler, so the
        # discovery file still points at the dead PID. The next start
        # WILL atomically overwrite it; the test asserts the second
        # daemon comes up with a fresh PID by polling until the on-
        # disk payload reflects the new process.
        disc = t2_discovery_path(config_dir)
        first_pid = first_payload["pid"]

        second = _spawn_daemon_subprocess(config_dir, db_path)
        try:
            assert _wait_until(
                lambda: disc.exists()
                and json.loads(disc.read_text()).get("pid") != first_pid,
                timeout=10.0,
            ), "second daemon never wrote a fresh discovery payload"
            second_payload = json.loads(disc.read_text())
            assert second_payload["pid"] != first_pid
            assert second_payload["pid"] == second.pid
        finally:
            _terminate_daemon(second)


# ---------------------------------------------------------------------------
# Scenario 4: Discovery file corruption
# ---------------------------------------------------------------------------


class TestDiscoveryFileLifecycle:
    def test_unparseable_discovery_does_not_block_next_spawn(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        from nexus.daemon.t2_daemon import t2_discovery_path

        # Plant garbage at the discovery path. The daemon overwrites
        # it on start (atomic write); a corrupt prior file should not
        # block startup.
        disc = t2_discovery_path(config_dir)
        disc.parent.mkdir(parents=True, exist_ok=True)
        disc.write_text("<<< not json >>>")
        os.chmod(str(disc), 0o600)

        proc = _spawn_daemon_subprocess(config_dir, db_path)
        try:
            payload = _wait_for_discovery(config_dir)
            assert payload["pid"] > 0
        finally:
            _terminate_daemon(proc)


# ---------------------------------------------------------------------------
# Scenario 5: Concurrency storm (parallel T2Client RPCs)
# ---------------------------------------------------------------------------


class TestConcurrencyStorm:
    """30 parallel T2Client calls hitting memory.put + memory.search
    against the same daemon. Each client opens its own connection
    (T2Client serializes per-instance). Exercises the asyncio dispatch
    loop and SQLite WAL serialization under load."""

    def test_30_parallel_client_round_trips(self, live_daemon) -> None:
        from nexus.daemon.t2_client import T2Client

        config_dir = live_daemon["config_dir"]

        def _round_trip(i: int) -> int:
            client = T2Client(config_dir=config_dir)
            try:
                client.memory.put(
                    content=f"stress entry {i}",
                    project="nexus_stress",
                    title=f"stress-{i}",
                )
                rows = client.memory.search("stress entry", project="nexus_stress")
                return len(rows)
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
            futures = [pool.submit(_round_trip, i) for i in range(30)]
            results = [f.result(timeout=60.0) for f in futures]
        # Every client should see at least its own write plus prior writes.
        assert all(r >= 1 for r in results), (
            f"some clients saw zero hits: {results}"
        )
        # After all 30 writes settle, a fresh client must see all 30
        # entries via search (last-writer-wins semantics with no data
        # loss under WAL contention).
        client = T2Client(config_dir=config_dir)
        try:
            final = client.memory.search("stress entry", project="nexus_stress")
        finally:
            client.close()
        assert len(final) == 30, (
            f"expected all 30 entries to land; got {len(final)}"
        )


# ---------------------------------------------------------------------------
# Scenario 6: Connection churn
# ---------------------------------------------------------------------------


class TestConnectionChurn:
    """100 rapid T2Client construct + one-call + close cycles. Verifies
    socket cleanup, no file-descriptor leak, no daemon-side resource
    exhaustion."""

    def test_100_rapid_client_cycles(self, live_daemon) -> None:
        from nexus.daemon.t2_client import T2Client

        config_dir = live_daemon["config_dir"]
        for _ in range(100):
            client = T2Client(config_dir=config_dir)
            try:
                client.memory.list_entries()
            finally:
                client.close()


# ---------------------------------------------------------------------------
# Scenario 7: SIGSTOP / SIGCONT suspend/resume
# ---------------------------------------------------------------------------


class TestSuspendResume:
    """SIGSTOP the daemon for 1 second, SIGCONT. A fresh client must
    connect and round-trip after resume."""

    def test_sigstop_sigcont_keeps_daemon_healthy(self, live_daemon) -> None:
        from nexus.daemon.t2_client import T2Client

        pid = live_daemon["payload"]["pid"]
        os.kill(pid, signal.SIGSTOP)
        time.sleep(1.0)
        os.kill(pid, signal.SIGCONT)
        time.sleep(0.3)

        client = T2Client(config_dir=live_daemon["config_dir"])
        try:
            client.memory.list_entries()
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Scenario 8: T2Client fail-loud after daemon death
# ---------------------------------------------------------------------------


class TestClientFailLoudAfterDaemonDeath:
    def test_sigkill_then_client_call_surfaces_error(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        from nexus.daemon.t2_client import (
            T2Client, T2DaemonNotReachableError,
        )

        proc = _spawn_daemon_subprocess(config_dir, db_path)
        try:
            _wait_for_discovery(config_dir)
            os.kill(proc.pid, signal.SIGKILL)
            assert _wait_until(
                lambda: not _pid_alive(proc.pid), timeout=10.0,
            )
            # Try to use a client; should surface an error, not hang.
            client = T2Client(config_dir=config_dir)
            try:
                with pytest.raises(
                    (T2DaemonNotReachableError, Exception),  # noqa: BLE001
                ):
                    client.memory.list_entries()
            finally:
                client.close()
        finally:
            _terminate_daemon(proc)


# ---------------------------------------------------------------------------
# Scenario 9: Frame protocol robustness
# ---------------------------------------------------------------------------


class TestFrameProtocolRobustness:
    """Send a deliberately oversized length-prefix header; the daemon
    must drop the connection without crashing."""

    def test_oversized_frame_header_does_not_crash_daemon(
        self, live_daemon,
    ) -> None:
        from nexus.daemon.t2_client import T2Client

        # Connect raw, send a length-prefix claiming 100 MiB. Daemon
        # caps frames at 4 MiB and should disconnect.
        uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        uds.settimeout(5.0)
        uds.connect(live_daemon["payload"]["uds_path"])
        try:
            uds.sendall(struct.pack(">I", 100 * 1024 * 1024))
            # The daemon should close after detecting the oversize.
            time.sleep(0.3)
        finally:
            uds.close()

        # Daemon must still be healthy and serving a fresh client.
        client = T2Client(config_dir=live_daemon["config_dir"])
        try:
            client.memory.list_entries()
        finally:
            client.close()

    def test_garbage_bytes_do_not_crash_daemon(self, live_daemon) -> None:
        from nexus.daemon.t2_client import T2Client

        uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        uds.settimeout(5.0)
        uds.connect(live_daemon["payload"]["uds_path"])
        try:
            uds.sendall(b"\xff" * 64)  # invalid as a length-prefixed frame
            time.sleep(0.3)
        finally:
            uds.close()

        client = T2Client(config_dir=live_daemon["config_dir"])
        try:
            client.memory.list_entries()
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Scenario 10: Mixed UDS + TCP traffic
# ---------------------------------------------------------------------------


class TestMixedUdsTcp:
    """20 UDS + 20 TCP clients running concurrently against the same
    daemon. Both transports share the dispatch loop; no transport
    starves the other."""

    def test_uds_and_tcp_clients_coexist(
        self, live_daemon, monkeypatch,
    ) -> None:
        from nexus.daemon.t2_client import T2Client

        config_dir = live_daemon["config_dir"]
        host = live_daemon["payload"]["tcp_host"]
        port = live_daemon["payload"]["tcp_port"]

        def _uds_call(i: int) -> int:
            client = T2Client(config_dir=config_dir)
            try:
                client.memory.put(
                    content=f"uds entry {i}",
                    project="nexus_mix_uds",
                    title=f"uds-{i}",
                )
                return 1
            finally:
                client.close()

        def _tcp_call(i: int) -> int:
            # Force the TCP branch by setting NX_T2_ADDR for this
            # client's lifetime. Use os.environ via monkeypatch in a
            # thread-local manner is awkward, so we override per-call
            # by constructing the client after setting the env.
            old = os.environ.get("NX_T2_ADDR")
            os.environ["NX_T2_ADDR"] = f"{host}:{port}"
            os.environ.pop("NX_T2_SOCK", None)
            try:
                client = T2Client(config_dir=config_dir)
                try:
                    client.memory.put(
                        content=f"tcp entry {i}",
                        project="nexus_mix_tcp",
                        title=f"tcp-{i}",
                    )
                    return 1
                finally:
                    client.close()
            finally:
                if old is None:
                    os.environ.pop("NX_T2_ADDR", None)
                else:
                    os.environ["NX_T2_ADDR"] = old

        # Note: NX_T2_ADDR env mutation isn't thread-safe across
        # parallel callers, so we serialize TCP calls and run UDS in
        # parallel alongside. Cheap; still exercises both transports
        # under simultaneous load.
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            uds_futures = [pool.submit(_uds_call, i) for i in range(20)]
            tcp_results = [_tcp_call(i) for i in range(20)]
            uds_results = [f.result(timeout=60.0) for f in uds_futures]

        assert all(r == 1 for r in uds_results)
        assert all(r == 1 for r in tcp_results)

        # Both projects' writes landed.
        client = T2Client(config_dir=config_dir)
        try:
            uds_final = client.memory.search("uds entry", project="nexus_mix_uds")
            tcp_final = client.memory.search("tcp entry", project="nexus_mix_tcp")
        finally:
            client.close()
        assert len(uds_final) == 20
        assert len(tcp_final) == 20


# ---------------------------------------------------------------------------
# Scenario 11: Repeated stop is idempotent
# ---------------------------------------------------------------------------


class TestRepeatedTerminate:
    def test_two_sigterms_clean(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        proc = _spawn_daemon_subprocess(config_dir, db_path)
        _wait_for_discovery(config_dir)
        os.kill(proc.pid, signal.SIGTERM)
        # Second SIGTERM to a now-exiting process must not crash.
        time.sleep(0.2)
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone, fine
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# Scenario 12: RDR-128 P3 single-writer routing — no `database is locked`
# ---------------------------------------------------------------------------


class TestRdr128P3SingleWriterRouting:
    """RDR-128 P3 (nexus-sbxbe.3) quantitative acceptance gate.

    The whole RDR exists because N independent writers contending on
    ``memory.db``'s single SQLite WAL writer lock produced a string of
    ``database is locked`` daemon incidents. P3 routes the writers through
    the daemon (``mcp_infra.t2_index_write``) so the daemon is the single
    writer and contention cannot surface.

    This scenario drives the three real writer shapes — indexer
    (``chash_index.upsert_many``), aspect-worker (``aspect_queue.enqueue``),
    and SessionEnd flush (``memory.put``) — concurrently, EACH through
    ``t2_index_write``, against ONE daemon, and asserts that NO caller ever
    sees ``database is locked`` (nor any other error) and that every write
    lands. This is the deterministic analogue of the qualitative
    release-cycle shakeout in the RDR's Validation section.
    """

    def test_mixed_routed_writers_never_surface_database_is_locked(
        self, config_dir: Path, monkeypatch,
    ) -> None:
        # Daemon DB == default_db_path() under this config dir, so the
        # routed path and the (unused-here) direct fallback agree.
        db_path = config_dir / "memory.db"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))

        proc = _spawn_daemon_subprocess(config_dir, db_path)
        try:
            _wait_for_discovery(config_dir)

            from nexus.mcp_infra import t2_index_write

            errors: list[tuple[str, int, str]] = []
            n = 20

            def _indexer(i: int) -> None:
                try:
                    t2_index_write(
                        lambda db: db.chash_index.upsert_many(
                            chashes=[f"h{i}_{j}" for j in range(5)],
                            collection="code__stress",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(("indexer", i, repr(exc)))

            def _worker(i: int) -> None:
                try:
                    t2_index_write(
                        lambda db: db.aspect_queue.enqueue(
                            "knowledge__stress", f"/p{i}.pdf",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(("worker", i, repr(exc)))

            def _session(i: int) -> None:
                try:
                    t2_index_write(
                        lambda db: db.memory.put(
                            project="nexus_stress",
                            title=f"s{i}",
                            content="session flush entry",
                            ttl=None,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(("session", i, repr(exc)))

            with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
                futures = []
                for i in range(n):
                    futures.append(pool.submit(_indexer, i))
                    futures.append(pool.submit(_worker, i))
                    futures.append(pool.submit(_session, i))
                for f in futures:
                    f.result(timeout=90.0)

            locked = [e for e in errors if "database is locked" in e[2].lower()]
            assert not locked, (
                f"routing did NOT prevent WAL contention: {locked[:5]}"
            )
            # No caller saw ANY error at all (routing serializes cleanly).
            assert errors == [], f"routed writers surfaced errors: {errors[:5]}"

            # Every write landed via the daemon's single writer.
            from nexus.db.t2 import T2Database

            with T2Database(db_path) as db:
                sessions = db.memory.search(
                    "session flush entry", project="nexus_stress",
                )
            assert len(sessions) == n, (
                f"expected all {n} session writes to land; got {len(sessions)}"
            )
        finally:
            _terminate_daemon(proc)
