# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P6 follow-up (nexus-qnrvn): ``nx daemon t2 ensure-running``.

The command is idempotent: silent no-op if a daemon is already running
on the named config_dir, otherwise spawn a fresh one in the background
and poll the discovery file until the new daemon is reachable (or the
timeout expires).

Spawn is mocked — we exercise the discovery-file probe + the spawn-
argv shape + the timeout path without actually forking a daemon.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


def _discovery_path(config_dir: Path) -> Path:
    """Mirror ``nexus.daemon.t2_daemon.t2_discovery_path`` — the
    discovery file is keyed by the current UID, not a hardcoded
    501. macOS dev UIDs are usually 501; Linux GHA runner UIDs are
    1001. Hardcoding either fails on the other.
    """
    from nexus.daemon.t2_daemon import t2_discovery_path
    return t2_discovery_path(config_dir)


def _installed_conexus_version() -> str:
    from importlib.metadata import version as _v

    try:
        return _v("conexus")
    except Exception:
        return "0.0.0"


def _write_discovery(config_dir: Path, pid: int, version: str | None = None) -> None:
    """Pre-seed a discovery file shaped like the real daemon writes.

    ``version`` defaults to the installed conexus version so the
    "already running, current" path is exercised; pass an older string
    to simulate a stale daemon that ensure-running should cycle
    (nexus-5ldk1).
    """
    payload = {
        "format_version": 1,
        "uds_path": str(config_dir / "sockets" / "t2.sock"),
        "tcp_host": "127.0.0.1",
        "tcp_port": 12345,
        "pid": pid,
        "daemon_version": version if version is not None else _installed_conexus_version(),
        "start_time": "2026-05-22T19:00:00+00:00",
    }
    dest = _discovery_path(config_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload))


class TestEnsureRunning:
    def test_already_running_is_idempotent_silent_no_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Seed the discovery file with the current process's PID — the
        # probe checks via os.kill(pid, 0) which succeeds for any
        # running process the caller can signal.
        _write_discovery(tmp_path, os.getpid())

        spawn_calls: list[list[str]] = []

        def _no_spawn(argv, **_kw):  # noqa: ANN001
            spawn_calls.append(argv)
            raise AssertionError("ensure-running must not spawn when daemon is alive")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "already running" in result.output
        assert spawn_calls == []

    def test_stale_version_daemon_is_cycled_then_respawned(
        self, tmp_path, monkeypatch,
    ) -> None:
        """nexus-5ldk1: a LIVE daemon whose version != installed tool is
        stale (froze old code at start). ensure-running must SIGTERM it
        and respawn a current one, rather than leaving the stale daemon."""
        import signal as _signal

        # Live daemon at an older version than the installed tool.
        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )

        # Stateful os.kill: pid is alive until it receives SIGTERM, then
        # dead. Guards the test process: we never signal a real pid.
        state = {"terminated": False}

        def _fake_kill(pid, sig):  # noqa: ANN001
            if pid != 424242:
                raise ProcessLookupError
            if sig == 0:
                if state["terminated"]:
                    raise ProcessLookupError
                return
            if sig == _signal.SIGTERM:
                state["terminated"] = True
                return

        monkeypatch.setattr(os, "kill", _fake_kill)

        spawn_calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawn_calls.append(argv)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        # Stale daemon was SIGTERM'd and a respawn was attempted.
        assert state["terminated"] is True, "stale daemon was not cycled"
        assert len(spawn_calls) == 1, "no respawn after cycling stale daemon"
        assert "stale" in result.output.lower()

    def test_current_version_daemon_not_cycled(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A live daemon whose version == installed tool is left alone."""
        _write_discovery(tmp_path, pid=424242, version="9.9.9-installed")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # pid "alive"
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not cycle a current daemon"),
        )
        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "already running" in result.output

    def test_already_running_quiet_suppresses_output(
        self, tmp_path, monkeypatch,
    ) -> None:
        _write_discovery(tmp_path, os.getpid())
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not spawn"),
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--quiet"],
        )
        assert result.exit_code == 0
        assert result.output == ""

    def test_stale_discovery_pid_dead_triggers_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        """PID 1 is init (always alive). Use PID 2**31 - 1 which can't
        be a real PID on any supported platform — os.kill(pid, 0) raises
        ProcessLookupError, and the probe treats that as 'daemon dead'."""
        _write_discovery(tmp_path, 2**31 - 1)

        spawn_calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawn_calls.append(argv)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        runner = CliRunner()
        # timeout=0.2 — we expect the spawn to fire but the new daemon
        # won't actually start (Popen is mocked), so the timeout path
        # exits 1.
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert result.exit_code == 1
        assert len(spawn_calls) == 1
        argv = spawn_calls[0]
        # The spawn invokes the nx CLI's ``daemon t2 start`` subcommand
        # with the same --config-dir the operator passed in. The first
        # element is the resolved nx binary (or python -m fallback) so
        # we tail-match on the well-known suffix.
        assert argv[-4:] == ["daemon", "t2", "start", "--config-dir"] or \
               argv[-5:] == ["daemon", "t2", "start", "--config-dir", str(tmp_path)]

    def test_missing_discovery_file_triggers_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        # No discovery file pre-seeded; ensure-running must spawn.
        spawn_calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawn_calls.append(argv)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        # Spawn fired; mock didn't produce a discovery file so timeout=1.
        assert result.exit_code == 1
        assert len(spawn_calls) == 1
        assert "did not become reachable" in result.output

    def test_corrupt_discovery_file_triggers_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Discovery file present but not valid JSON — probe treats as dead.
        dest = _discovery_path(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("not json {{{")

        spawn_calls: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda argv, **kw: spawn_calls.append(argv) or type(
                "P", (), {"__init__": lambda self: None}
            )(),
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert result.exit_code == 1
        assert len(spawn_calls) == 1

    def test_timeout_message_names_log_paths(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The timeout warning must point the operator at the launchd /
        systemd log so they can self-diagnose without spelunking."""
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda argv, **kw: type("P", (), {"__init__": lambda self: None})(),
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert result.exit_code == 1
        # Both platform-specific log hints appear (the command doesn't
        # know which platform the operator is on, so it names both).
        assert "nexus-t2.err" in result.output
        assert "journalctl --user -u nexus-t2.service" in result.output
