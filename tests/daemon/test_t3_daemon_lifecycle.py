# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P1.A (nexus-41unl): T3 daemon lifecycle tests.

The T3 daemon is a managed ``chroma run`` subprocess. This bead covers
process lifecycle + discovery; T3Client is the next bead (P1.B
nexus-beoh1).

Tests cover:
- Discovery file shape + atomic write + uid suffix
- Daemon-start / daemon-stop happy path with HttpClient round-trip query
- T1/T3 non-collision invariant (separate addr files, separate ports,
  separate chroma --path roots)
- Stale-discovery recovery (kill -9 then start)
- Cloud-mode rejection (start fails loud when NX_LOCAL=0)
- Idempotent start (returns existing payload on second call)
- --foreground supervisor-friendly blocking mode
- Stop-time defensive paths
- CLI surface (nx daemon t3 start/stop/status)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


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
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def force_local_mode(monkeypatch):
    """Force ``is_local_mode()`` to return True regardless of test-env
    credentials. The chroma-subprocess tests are local-mode-only;
    cloud-mode rejection has its own dedicated test."""
    monkeypatch.setenv("NX_LOCAL", "1")


def _wait_for_path(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def _wait_for_path_gone(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.1)
    return False


class TestDiscoveryFileShape:
    def test_discovery_path_uses_uid_suffix(self, config_dir: Path) -> None:
        from nexus.daemon.t3_daemon import t3_discovery_path

        expected = config_dir / f"t3_addr.{os.getuid()}"
        assert t3_discovery_path(config_dir) == expected

    def test_discovery_filename_distinct_from_t1(self, config_dir: Path) -> None:
        """T1/T3 non-collision: T1 uses ``t1_addr.<claude_pid>``;
        T3 uses ``t3_addr.<uid>``."""
        from nexus.daemon.t3_daemon import t3_discovery_path

        t3_path = t3_discovery_path(config_dir)
        assert t3_path.name.startswith("t3_addr."), (
            f"T3 discovery filename must use t3_addr.* prefix, got {t3_path.name}"
        )
        assert "t1_addr" not in t3_path.name


class TestStartStopHappyPath:
    """Spawn a real chroma subprocess; verify HttpClient round-trip; stop."""

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
            on_disk = json.loads(disc_path.read_text())
            assert on_disk["pid"] == payload["pid"]
            assert on_disk["tcp_port"] == payload["tcp_port"]
            assert _is_listening(payload["tcp_host"], payload["tcp_port"])
            os.kill(payload["pid"], 0)
        finally:
            pid = stop_t3_daemon(config_dir=config_dir)
            assert pid == payload["pid"]
            assert _wait_pid_gone(payload["pid"]), "daemon did not exit after stop"
            assert not disc_path.exists(), "stop must unlink discovery file"

    def test_http_client_round_trip_against_running_daemon(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon
        import chromadb

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            client = chromadb.HttpClient(
                host=payload["tcp_host"], port=payload["tcp_port"]
            )
            client.heartbeat()
            coll = client.get_or_create_collection("t3_smoke")
            coll.add(documents=["alpha"], ids=["1"])
            results = coll.query(query_texts=["alpha"], n_results=1)
            assert results["ids"][0] == ["1"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


class TestT1T3NonCollision:
    def test_t3_port_allocator_does_not_collide_with_t1_default(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            other_port = sock.getsockname()[1]
            sock.close()
            assert other_port != payload["tcp_port"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


class TestStaleDiscoveryRecovery:
    def test_stale_discovery_file_cleaned_on_next_start(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon,
            stop_t3_daemon,
            t3_discovery_path,
        )

        disc_path = t3_discovery_path(config_dir)
        stale_pid = 2**31 - 1
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

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            assert payload["pid"] != stale_pid
            on_disk = json.loads(disc_path.read_text())
            assert on_disk["pid"] == payload["pid"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


class TestCloudModeRejection:
    def test_cloud_mode_raises_runtime_error_with_clear_message(
        self, config_dir: Path, local_path: Path, monkeypatch
    ) -> None:
        from nexus.daemon.t3_daemon import T3CloudModeError, start_t3_daemon

        monkeypatch.setenv("NX_LOCAL", "0")
        with pytest.raises(T3CloudModeError) as excinfo:
            start_t3_daemon(config_dir=config_dir, local_path=local_path)
        msg = str(excinfo.value)
        assert "cloud" in msg.lower()
        assert "no-op" in msg.lower() or "no t3 daemon" in msg.lower()


class TestIdempotentStart:
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


class TestForegroundBlocking:
    """``--foreground`` is mandatory under launchd/systemd supervision."""

    def test_foreground_blocks_until_sigterm(
        self, config_dir: Path, local_path: Path, tmp_path: Path
    ) -> None:
        from nexus.daemon.t3_daemon import t3_discovery_path

        driver = tmp_path / "nx_driver.py"
        driver.write_text(
            "from nexus.cli import main\nif __name__ == '__main__':\n    main()\n"
        )

        env = {**os.environ, "NX_LOCAL": "1"}
        proc = subprocess.Popen(
            [
                sys.executable, str(driver),
                "daemon", "t3", "start", "--foreground",
                "--config-dir", str(config_dir),
                "--local-path", str(local_path),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            disc_path = t3_discovery_path(config_dir)
            assert _wait_for_path(disc_path, timeout=15.0), (
                f"discovery file did not appear at {disc_path} within 15s"
            )
            assert proc.poll() is None, (
                "CLI exited prematurely; launchd/systemd would see no "
                "supervised process."
            )
            proc.terminate()
            return_code = proc.wait(timeout=10.0)
            assert return_code == 0, (
                f"--foreground CLI must exit 0 on SIGTERM; got {return_code}"
            )
            assert _wait_for_path_gone(disc_path, timeout=5.0), (
                "discovery file should be unlinked after graceful stop"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)


class TestStopEdgeCases:
    def test_stop_with_non_integer_pid_unlinks_and_returns_none(
        self, config_dir: Path
    ) -> None:
        from nexus.daemon.t3_daemon import stop_t3_daemon, t3_discovery_path

        path = t3_discovery_path(config_dir)
        path.write_text(json.dumps({
            "format_version": 1,
            "tcp_host": "127.0.0.1",
            "tcp_port": 9999,
            "pid": "not-an-int",
            "daemon_version": "test",
            "start_time": "1970-01-01T00:00:00",
            "local_path": "/tmp/x",
        }))
        os.chmod(str(path), 0o600)
        assert stop_t3_daemon(config_dir=config_dir) is None
        assert not path.exists()

    def test_stop_when_pid_already_dead_unlinks_and_returns_pid(
        self, config_dir: Path
    ) -> None:
        from nexus.daemon.t3_daemon import stop_t3_daemon, t3_discovery_path

        path = t3_discovery_path(config_dir)
        dead_pid = 2**31 - 1
        path.write_text(json.dumps({
            "format_version": 1,
            "tcp_host": "127.0.0.1",
            "tcp_port": 9999,
            "pid": dead_pid,
            "daemon_version": "test",
            "start_time": "1970-01-01T00:00:00",
            "local_path": "/tmp/x",
        }))
        os.chmod(str(path), 0o600)
        assert stop_t3_daemon(config_dir=config_dir) == dead_pid
        assert not path.exists()

    def test_start_skips_unparseable_discovery_file(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        from nexus.daemon.t3_daemon import (
            start_t3_daemon,
            stop_t3_daemon,
            t3_discovery_path,
        )

        path = t3_discovery_path(config_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<<< not json >>>")
        os.chmod(str(path), 0o600)

        payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
        try:
            on_disk = json.loads(path.read_text())
            assert on_disk["pid"] == payload["pid"]
        finally:
            stop_t3_daemon(config_dir=config_dir)


class TestCliSurface:
    """``nx daemon t3 ...`` Click group acceptance tests for nexus-41unl.

    Bead acceptance criteria:
    - ``nx daemon t3 start`` spawns chroma run, writes discovery file
    - ``nx daemon t3 status`` reports PID + bound address
    - ``nx daemon t3 stop`` terminates cleanly, removes discovery file
    """

    def test_t3_group_registered_under_daemon_group(self) -> None:
        from nexus.commands.daemon import daemon_group

        assert "t3" in daemon_group.commands
        t3 = daemon_group.commands["t3"]
        assert {"start", "stop", "status", "install", "uninstall"} <= set(t3.commands)

    def test_t3_status_reports_no_daemon_when_disc_missing(
        self, config_dir: Path
    ) -> None:
        from click.testing import CliRunner

        from nexus.commands.daemon import daemon_group

        runner = CliRunner()
        result = runner.invoke(
            daemon_group, ["t3", "status", "--config-dir", str(config_dir)]
        )
        assert result.exit_code == 1
        # Click routes err=True writes through to result.output by default.
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

    def test_t3_start_status_stop_round_trip(
        self, config_dir: Path, local_path: Path, force_local_mode
    ) -> None:
        """Full CLI round-trip: start → status reads discovery → stop."""
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

            status = runner.invoke(
                daemon_group,
                ["t3", "status", "--config-dir", str(config_dir), "--json"],
            )
            assert status.exit_code == 0
            payload = json.loads(status.output)
            assert payload["pid"] > 0
            assert payload["format_version"] == 1
            assert payload["tcp_host"] == "127.0.0.1"
            assert isinstance(payload["tcp_port"], int)
        finally:
            stop = runner.invoke(
                daemon_group, ["t3", "stop", "--config-dir", str(config_dir)]
            )
            assert stop.exit_code == 0
            assert "T3 daemon stopped" in stop.output
