# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P1.5.1 (nexus-s3dm): T3 daemon lifecycle tests.

The T3 daemon is a managed ``chroma run`` subprocess — chromadb's bundled
HTTP server is the production-quality RPC layer (RDR-112 §A1). This bead
covers process lifecycle + discovery; T3Client is the next bead (P1.5.3).

Tests cover:
- Discovery file shape + atomic write + uid suffix
- Daemon-start / daemon-stop happy path with HttpClient round-trip query
- T1/T3 non-collision invariant (separate addr files, separate ports,
  separate chroma --path roots)
- Stale-discovery recovery (kill -9 then start)
- Cloud-mode rejection (start fails loud when NX_LOCAL=0)
"""
from __future__ import annotations

import json
import os
import signal
import socket
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def _wait_pid_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Isolated config directory for each test."""
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def local_path(tmp_path: Path) -> Path:
    """Isolated chroma persistent path for each test."""
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def force_local_mode(monkeypatch):
    """Force ``is_local_mode()`` to return True regardless of test-env
    credentials. The chroma-subprocess tests are local-mode-only by
    design; cloud-mode rejection has its own dedicated test."""
    monkeypatch.setenv("NX_LOCAL", "1")


# ---------------------------------------------------------------------------
# Discovery file shape
# ---------------------------------------------------------------------------


class TestDiscoveryFileShape:
    """The discovery payload contract clients rely on."""

    def test_discovery_path_uses_uid_suffix(self, config_dir: Path) -> None:
        from nexus.daemon.t3_daemon import t3_discovery_path

        expected = config_dir / f"t3_addr.{os.getuid()}"
        assert t3_discovery_path(config_dir) == expected

    def test_discovery_filename_distinct_from_t1(self, config_dir: Path) -> None:
        """T1/T3 non-collision invariant (PLAN-AUDIT 2026-05-17): both
        shells start their own chroma; they must not collide on addr-file
        name. T1 uses ``t1_addr.<claude_pid>``; T3 uses ``t3_addr.<uid>``."""
        from nexus.daemon.t3_daemon import t3_discovery_path

        t3_path = t3_discovery_path(config_dir)
        # T1's pattern from session.py: t1_addr.<claude_pid>
        assert t3_path.name.startswith("t3_addr."), (
            f"T3 discovery filename must use t3_addr.* prefix, got {t3_path.name}"
        )
        assert "t1_addr" not in t3_path.name


# ---------------------------------------------------------------------------
# Daemon start/stop happy path (real chroma subprocess)
# ---------------------------------------------------------------------------


class TestStartStopHappyPath:
    """Spawn a real chroma subprocess, verify HttpClient round-trip, then stop.

    Slow tests (~3-5s per case) because chroma startup is not free.
    """

    def test_start_writes_discovery_file_then_stop_cleans_it(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon,
            stop_t3_daemon,
            t3_discovery_path,
        )

        disc_path = t3_discovery_path(config_dir)
        assert not disc_path.exists()

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            assert disc_path.exists(), "discovery file must exist after start"
            assert payload["pid"] > 0
            assert payload["tcp_host"] in ("127.0.0.1", "localhost")
            assert isinstance(payload["tcp_port"], int)
            assert payload["tcp_port"] > 0
            assert payload["format_version"] == 1
            # Disc file content matches payload
            on_disk = json.loads(disc_path.read_text())
            assert on_disk["pid"] == payload["pid"]
            assert on_disk["tcp_port"] == payload["tcp_port"]
            # Process is alive + listening
            assert _is_listening(payload["tcp_host"], payload["tcp_port"])
            os.kill(payload["pid"], 0)  # raises if dead
        finally:
            pid = stop_t3_daemon(config_dir=config_dir)
            assert pid == payload["pid"]
            assert _wait_pid_gone(payload["pid"]), "daemon did not exit after stop"
            assert not disc_path.exists(), "stop must unlink discovery file"

    def test_http_client_round_trip_against_running_daemon(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        """An HttpClient pointed at the daemon must list collections."""
        from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon
        import chromadb

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            client = chromadb.HttpClient(
                host=payload["tcp_host"], port=payload["tcp_port"]
            )
            # heartbeat is the cheapest "is this really a chroma server" check
            client.heartbeat()
            coll = client.get_or_create_collection("t3_smoke")
            coll.add(documents=["alpha"], ids=["1"])
            results = coll.query(query_texts=["alpha"], n_results=1)
            assert results["ids"][0] == ["1"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# T1/T3 non-collision invariant
# ---------------------------------------------------------------------------


class TestT1T3NonCollision:
    """Both T1 and T3 spawn chroma subprocesses; verify they pick distinct
    ports and distinct addr-file names."""

    def test_t3_port_allocator_does_not_collide_with_t1_default(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        """T3 picks a free port; T1 also picks a free port. Both go through
        the OS port allocator so the OS guarantees distinctness."""
        from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            # Bind a separate socket and confirm it gets a different port.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            other_port = sock.getsockname()[1]
            sock.close()
            assert other_port != payload["tcp_port"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# Stale discovery recovery
# ---------------------------------------------------------------------------


class TestStaleDiscoveryRecovery:
    """A crashed daemon leaves a stale discovery file; the next start must
    detect the stale PID and proceed."""

    def test_stale_discovery_file_cleaned_on_next_start(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon,
            stop_t3_daemon,
            t3_discovery_path,
        )

        # Plant a stale discovery file pointing at a definitely-dead PID.
        disc_path = t3_discovery_path(config_dir)
        stale_pid = 2**31 - 1  # max int32, never a real PID
        disc_path.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "tcp_host": "127.0.0.1",
                    "tcp_port": 9999,
                    "pid": stale_pid,
                    "daemon_version": "stale",
                    "start_time": "1970-01-01T00:00:00",
                    "local_path": str(local_path),
                }
            )
        )
        os.chmod(str(disc_path), 0o600)

        # Start should succeed despite the stale file.
        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            assert payload["pid"] != stale_pid
            # Re-read the on-disk discovery file; it should point at the new pid.
            on_disk = json.loads(disc_path.read_text())
            assert on_disk["pid"] == payload["pid"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# Cloud mode rejection
# ---------------------------------------------------------------------------


class TestCloudModeRejection:
    """In cloud mode, ``start_t3_daemon`` is a no-op operationally and
    must fail loud — chroma's CloudClient is already HTTP-served."""

    def test_cloud_mode_raises_runtime_error_with_clear_message(
        self, config_dir: Path, local_path: Path, monkeypatch
    ) -> None:
        from nexus.daemon.t3_daemon import T3CloudModeError, start_t3_daemon

        monkeypatch.setenv("NX_LOCAL", "0")
        with pytest.raises(T3CloudModeError) as excinfo:
            start_t3_daemon(config_dir=config_dir, local_path=local_path)
        msg = str(excinfo.value)
        # The message must name the substantive reason, not just "no".
        assert "cloud" in msg.lower()
        assert "no-op" in msg.lower() or "no t3 daemon" in msg.lower()


# ---------------------------------------------------------------------------
# Idempotent start (already running)
# ---------------------------------------------------------------------------


class TestIdempotentStart:
    """A second ``start`` against a live daemon must detect the existing
    process and return the same discovery payload (no double-spawn)."""

    def test_second_start_returns_existing_payload(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

        first = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            second = start_t3_daemon(config_dir=config_dir, local_path=local_path)
            assert second["pid"] == first["pid"]
            assert second["tcp_port"] == first["tcp_port"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# CLI surface (nx daemon t3 start/stop/info)
# ---------------------------------------------------------------------------


class TestCliSurface:
    """Smoke-tests for the ``nx daemon t3 ...`` Click group."""

    def test_t3_group_registered_under_daemon_group(self) -> None:
        from nexus.commands.daemon import daemon_group

        assert "t3" in daemon_group.commands
        t3 = daemon_group.commands["t3"]
        assert {"start", "stop", "info"} <= set(t3.commands)

    def test_t3_info_reports_no_daemon_when_disc_missing(
        self, config_dir: Path
    ) -> None:
        from click.testing import CliRunner

        from nexus.commands.daemon import daemon_group

        runner = CliRunner()
        result = runner.invoke(
            daemon_group, ["t3", "info", "--config-dir", str(config_dir)]
        )
        assert result.exit_code == 1
        assert "No T3 daemon discovery file" in result.output

    def test_t3_stop_is_idempotent_when_no_daemon(self, config_dir: Path) -> None:
        from click.testing import CliRunner

        from nexus.commands.daemon import daemon_group

        runner = CliRunner()
        result = runner.invoke(
            daemon_group, ["t3", "stop", "--config-dir", str(config_dir)]
        )
        assert result.exit_code == 0
        assert "already stopped" in result.output

    def test_t3_start_cloud_mode_exits_nonzero(
        self, config_dir: Path, local_path: Path, monkeypatch
    ) -> None:
        from click.testing import CliRunner

        from nexus.commands.daemon import daemon_group

        monkeypatch.setenv("NX_LOCAL", "0")
        runner = CliRunner()
        result = runner.invoke(
            daemon_group,
            [
                "t3", "start",
                "--config-dir", str(config_dir),
                "--local-path", str(local_path),
            ],
        )
        assert result.exit_code == 1
        assert "cloud mode" in result.output.lower()

    def test_t3_start_info_stop_round_trip(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        """Full CLI round-trip: start → info reads discovery → stop."""
        from click.testing import CliRunner

        from nexus.commands.daemon import daemon_group

        runner = CliRunner()
        start = runner.invoke(
            daemon_group,
            [
                "t3", "start",
                "--config-dir", str(config_dir),
                "--local-path", str(local_path),
            ],
        )
        try:
            assert start.exit_code == 0, start.output
            assert "T3 daemon running" in start.output

            info = runner.invoke(
                daemon_group,
                ["t3", "info", "--config-dir", str(config_dir), "--json"],
            )
            assert info.exit_code == 0
            payload = json.loads(info.output)
            assert payload["pid"] > 0
            assert payload["format_version"] == 1
        finally:
            stop = runner.invoke(
                daemon_group, ["t3", "stop", "--config-dir", str(config_dir)]
            )
            assert stop.exit_code == 0
            assert "T3 daemon stopped" in stop.output
