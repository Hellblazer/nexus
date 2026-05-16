# SPDX-License-Identifier: AGPL-3.0-or-later
"""PID-liveness probe in find_t2_daemon (RDR-112, nexus-j6dj).

A discovery file points at the daemon's PID. After a crash the file
survives but the PID is dead; clients that trust the file end up
routing to a nonexistent socket. ``find_t2_daemon`` now sends
``os.kill(pid, 0)`` and treats ``ProcessLookupError`` as "stale" —
unlinks the file and returns ``None`` so the caller falls back cleanly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.daemon import discovery


def _write_payload(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def _live_payload(extras: dict | None = None) -> dict:
    return {
        "pid": os.getpid(),
        "uds_path": "/tmp/nx-t2-fake.sock",
        "tcp_host": "127.0.0.1",
        "tcp_port": 1,
        "daemon_version": "0.0.0",
        "daemon_protocol_version": "1.0",
        "start_time": "2026-05-16T00:00:00Z",
        "subspace_schema_digest": None,
        **(extras or {}),
    }


def _make_config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "config"
    cd.mkdir()
    return cd


class TestFindT2DaemonLiveness:
    def test_live_pid_returns_payload(self, tmp_path: Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        _write_payload(
            discovery.discovery_path(config_dir),
            _live_payload(),
        )

        result = discovery.find_t2_daemon(config_dir=config_dir)
        assert result is not None
        assert result["pid"] == os.getpid()

    def test_dead_pid_returns_none_and_removes_file(
        self, tmp_path: Path
    ) -> None:
        # Spawn a short-lived subprocess, capture its pid, wait for exit.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"]
        )
        dead_pid = proc.pid
        proc.wait(timeout=5)
        # macOS keeps the PID reserved as a zombie until the parent waits;
        # subprocess.wait above performs the wait, releasing the slot.
        # If the kernel did reuse the pid we would see ESRCH on kill(0).
        try:
            os.kill(dead_pid, 0)
        except ProcessLookupError:
            pass
        else:
            pytest.skip(
                "Kernel reused the test subprocess's PID before the probe; "
                "rerun the test."
            )

        config_dir = _make_config_dir(tmp_path)
        path = discovery.discovery_path(config_dir)
        _write_payload(path, _live_payload({"pid": dead_pid}))

        result = discovery.find_t2_daemon(config_dir=config_dir)
        assert result is None
        assert not path.exists(), (
            "Stale discovery file must be cleaned up on liveness failure."
        )

    def test_missing_pid_field_returns_none(self, tmp_path: Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        _write_payload(
            discovery.discovery_path(config_dir),
            {"uds_path": "/tmp/x.sock", "tcp_host": "127.0.0.1", "tcp_port": 1},
        )

        assert discovery.find_t2_daemon(config_dir=config_dir) is None

    def test_invalid_pid_type_returns_none(self, tmp_path: Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        _write_payload(
            discovery.discovery_path(config_dir),
            _live_payload({"pid": "not-an-int"}),
        )

        assert discovery.find_t2_daemon(config_dir=config_dir) is None

    def test_zero_pid_returns_none(self, tmp_path: Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        _write_payload(
            discovery.discovery_path(config_dir),
            _live_payload({"pid": 0}),
        )

        assert discovery.find_t2_daemon(config_dir=config_dir) is None

    def test_unparseable_payload_still_returns_none(
        self, tmp_path: Path
    ) -> None:
        """Existing JSON-parse error path preserved (regression guard)."""
        config_dir = _make_config_dir(tmp_path)
        discovery.discovery_path(config_dir).write_text("{not json")

        assert discovery.find_t2_daemon(config_dir=config_dir) is None

    def test_no_discovery_file_returns_none(self, tmp_path: Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        assert discovery.find_t2_daemon(config_dir=config_dir) is None
