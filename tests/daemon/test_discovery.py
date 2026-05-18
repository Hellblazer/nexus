# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P1.5.2 (nexus-n8xg): tier-parametric discovery resolver tests.

Covers:
- ``discovery_path()`` backward-compat for T2 + new T3 path shape.
- ``find_t3_daemon()`` mirrors ``find_t2_daemon()`` validation
  invariants: PID-liveness, shutdown-marker, format_version > 1 rejection,
  non-dict shape rejection.
- ``discovery_resolve(tier)`` env-first, file-fallback chain:
  ``NX_T2_SOCK`` / ``NX_T2_ADDR`` for T2; ``NX_T3_ADDR`` for T3.
- ``DaemonNotRunningError`` raised with clear recovery hint when nothing
  resolves.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


def _live_pid() -> int:
    """Return a guaranteed-live PID for the test process."""
    return os.getpid()


def _write_payload(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))
    os.chmod(str(path), 0o600)


# ---------------------------------------------------------------------------
# discovery_path()
# ---------------------------------------------------------------------------


class TestDiscoveryPath:
    def test_default_tier_is_t2_backward_compat(self, config_dir: Path) -> None:
        """``discovery_path(config_dir)`` returns the T2 path with no tier kwarg.
        Backward-compat for the 6 existing positional callers."""
        from nexus.daemon.discovery import discovery_path

        expected = config_dir / f"t2_addr.{os.getuid()}"
        assert discovery_path(config_dir) == expected

    def test_explicit_tier_t2(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path

        expected = config_dir / f"t2_addr.{os.getuid()}"
        assert discovery_path(config_dir, tier="t2") == expected

    def test_explicit_tier_t3(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path

        expected = config_dir / f"t3_addr.{os.getuid()}"
        assert discovery_path(config_dir, tier="t3") == expected

    def test_unknown_tier_raises(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path

        with pytest.raises(ValueError):
            discovery_path(config_dir, tier="t9")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find_t3_daemon validation parity with find_t2_daemon
# ---------------------------------------------------------------------------


class TestFindT3Daemon:
    def test_returns_none_when_disc_file_absent(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import find_t3_daemon

        assert find_t3_daemon(config_dir) is None

    def test_returns_payload_when_pid_live(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path, find_t3_daemon

        payload = {
            "format_version": 1,
            "tcp_host": "127.0.0.1",
            "tcp_port": 9012,
            "pid": _live_pid(),
            "daemon_version": "test",
            "start_time": "1970-01-01T00:00:00",
            "local_path": "/tmp/chroma-t3",
        }
        _write_payload(discovery_path(config_dir, tier="t3"), payload)
        result = find_t3_daemon(config_dir)
        assert result is not None
        assert result["pid"] == _live_pid()
        assert result["tcp_port"] == 9012

    def test_stale_pid_unlinks_and_returns_none(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path, find_t3_daemon

        path = discovery_path(config_dir, tier="t3")
        _write_payload(
            path,
            {
                "format_version": 1,
                "tcp_host": "127.0.0.1",
                "tcp_port": 9013,
                "pid": 2**31 - 1,  # never a real PID
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
                "local_path": "/tmp/chroma-t3",
            },
        )
        assert find_t3_daemon(config_dir) is None
        assert not path.exists(), "stale discovery file must be unlinked"

    def test_shutdown_marker_returns_none(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path, find_t3_daemon

        _write_payload(
            discovery_path(config_dir, tier="t3"),
            {
                "format_version": 1,
                "status": "shutting_down",
                "shutdown_at": "1970-01-01T00:00:00",
                "tcp_host": "127.0.0.1",
                "tcp_port": 9014,
                "pid": _live_pid(),
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
                "local_path": "/tmp/chroma-t3",
            },
        )
        assert find_t3_daemon(config_dir) is None

    def test_format_version_too_new_returns_none(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path, find_t3_daemon

        _write_payload(
            discovery_path(config_dir, tier="t3"),
            {
                "format_version": 99,
                "tcp_host": "127.0.0.1",
                "tcp_port": 9015,
                "pid": _live_pid(),
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
                "local_path": "/tmp/chroma-t3",
            },
        )
        assert find_t3_daemon(config_dir) is None

    def test_non_dict_payload_returns_none(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path, find_t3_daemon

        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps(["not", "a", "dict"]))
        assert find_t3_daemon(config_dir) is None


# ---------------------------------------------------------------------------
# discovery_resolve — env-first, file-fallback chain
# ---------------------------------------------------------------------------


class TestDiscoveryResolveT3:
    def test_env_var_wins_over_file(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import discovery_path, discovery_resolve

        # Plant a file with one port; env-var advertises a different one.
        _write_payload(
            discovery_path(config_dir, tier="t3"),
            {
                "format_version": 1,
                "tcp_host": "127.0.0.1",
                "tcp_port": 9100,
                "pid": _live_pid(),
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
                "local_path": "/tmp/chroma-t3",
            },
        )
        monkeypatch.setenv("NX_T3_ADDR", "10.0.0.5:5555")
        result = discovery_resolve("t3", config_dir=config_dir)
        assert result["tcp_host"] == "10.0.0.5"
        assert result["tcp_port"] == 5555
        assert result["source"] == "env:NX_T3_ADDR"

    def test_file_fallback_when_env_unset(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import discovery_path, discovery_resolve

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        _write_payload(
            discovery_path(config_dir, tier="t3"),
            {
                "format_version": 1,
                "tcp_host": "127.0.0.1",
                "tcp_port": 9200,
                "pid": _live_pid(),
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
                "local_path": "/tmp/chroma-t3",
            },
        )
        result = discovery_resolve("t3", config_dir=config_dir)
        assert result["tcp_port"] == 9200
        assert result["source"] == "file"

    def test_raises_daemon_not_running_when_nothing_resolves(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import (
            DaemonNotRunningError,
            discovery_resolve,
        )

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        with pytest.raises(DaemonNotRunningError) as excinfo:
            discovery_resolve("t3", config_dir=config_dir)
        msg = str(excinfo.value)
        # The error message must surface a recovery hint.
        assert "nx daemon t3 start" in msg
        assert "t3" in msg.lower()

    def test_stale_pid_propagates_as_daemon_not_running(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import (
            DaemonNotRunningError,
            discovery_path,
            discovery_resolve,
        )

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        _write_payload(
            discovery_path(config_dir, tier="t3"),
            {
                "format_version": 1,
                "tcp_host": "127.0.0.1",
                "tcp_port": 9300,
                "pid": 2**31 - 1,
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
                "local_path": "/tmp/chroma-t3",
            },
        )
        with pytest.raises(DaemonNotRunningError):
            discovery_resolve("t3", config_dir=config_dir)

    def test_shutdown_marker_propagates_as_daemon_not_running(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import (
            DaemonNotRunningError,
            discovery_path,
            discovery_resolve,
        )

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        _write_payload(
            discovery_path(config_dir, tier="t3"),
            {
                "format_version": 1,
                "status": "shutting_down",
                "tcp_host": "127.0.0.1",
                "tcp_port": 9400,
                "pid": _live_pid(),
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
                "local_path": "/tmp/chroma-t3",
            },
        )
        with pytest.raises(DaemonNotRunningError):
            discovery_resolve("t3", config_dir=config_dir)

    def test_malformed_env_var_raises_value_error(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import discovery_resolve

        monkeypatch.setenv("NX_T3_ADDR", "not-a-host-port")
        with pytest.raises(ValueError):
            discovery_resolve("t3", config_dir=config_dir)


class TestDiscoveryResolveT2:
    """Regression: existing T2 callers continue to work unchanged."""

    def test_t2_file_resolution_matches_find_t2_daemon(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import (
            discovery_path,
            discovery_resolve,
            find_t2_daemon,
        )

        monkeypatch.delenv("NX_T2_SOCK", raising=False)
        monkeypatch.delenv("NX_T2_ADDR", raising=False)

        payload = {
            "format_version": 1,
            "uds_path": "/tmp/t2.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 9500,
            "pid": _live_pid(),
            "daemon_version": "test",
            "start_time": "1970-01-01T00:00:00",
        }
        _write_payload(discovery_path(config_dir, tier="t2"), payload)
        via_resolve = discovery_resolve("t2", config_dir=config_dir)
        via_find = find_t2_daemon(config_dir)
        assert via_find is not None
        # Same payload (modulo the ``source`` annotation added by resolve).
        for key in ("pid", "tcp_port", "uds_path", "tcp_host"):
            assert via_resolve[key] == via_find[key]
        assert via_resolve["source"] == "file"

    def test_nx_t2_sock_env_wins_over_file(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import discovery_path, discovery_resolve

        _write_payload(
            discovery_path(config_dir, tier="t2"),
            {
                "format_version": 1,
                "uds_path": "/tmp/file.sock",
                "tcp_host": "127.0.0.1",
                "tcp_port": 9501,
                "pid": _live_pid(),
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
            },
        )
        monkeypatch.setenv("NX_T2_SOCK", "/tmp/env.sock")
        monkeypatch.delenv("NX_T2_ADDR", raising=False)
        result = discovery_resolve("t2", config_dir=config_dir)
        assert result["uds_path"] == "/tmp/env.sock"
        assert result["source"] == "env:NX_T2_SOCK"

    def test_nx_t2_addr_env_wins_over_file_when_no_sock(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import discovery_path, discovery_resolve

        _write_payload(
            discovery_path(config_dir, tier="t2"),
            {
                "format_version": 1,
                "uds_path": "/tmp/file.sock",
                "tcp_host": "127.0.0.1",
                "tcp_port": 9502,
                "pid": _live_pid(),
                "daemon_version": "test",
                "start_time": "1970-01-01T00:00:00",
            },
        )
        monkeypatch.delenv("NX_T2_SOCK", raising=False)
        monkeypatch.setenv("NX_T2_ADDR", "10.0.0.5:5556")
        result = discovery_resolve("t2", config_dir=config_dir)
        assert result["tcp_host"] == "10.0.0.5"
        assert result["tcp_port"] == 5556
        assert result["source"] == "env:NX_T2_ADDR"

    def test_t2_raises_daemon_not_running_when_nothing_resolves(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import (
            DaemonNotRunningError,
            discovery_resolve,
        )

        monkeypatch.delenv("NX_T2_SOCK", raising=False)
        monkeypatch.delenv("NX_T2_ADDR", raising=False)
        with pytest.raises(DaemonNotRunningError) as excinfo:
            discovery_resolve("t2", config_dir=config_dir)
        assert "nx daemon t2 start" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Backward-compat: find_t2_daemon unchanged behavior
# ---------------------------------------------------------------------------


class TestFindT2DaemonBackwardCompat:
    def test_signature_unchanged(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import find_t2_daemon

        # Positional config_dir still works (existing call sites).
        result = find_t2_daemon(config_dir)
        assert result is None  # no discovery file → None
