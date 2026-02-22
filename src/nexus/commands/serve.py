# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx serve — start/stop/status/logs for the Nexus background server."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click


def _config_dir() -> Path:
    return Path.home() / ".config" / "nexus"


def _pid_path() -> Path:
    return _config_dir() / "server.pid"


def _log_path() -> Path:
    return _config_dir() / "serve.log"


def _process_running(pid: int) -> bool:
    """Return True if a process with *pid* exists."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid() -> int | None:
    """Read PID from file; return None if file missing or invalid."""
    p = _pid_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except ValueError:
        return None


@click.group()
def serve() -> None:
    """Manage the Nexus background server."""


@serve.command("start")
def start_cmd() -> None:
    """Start the Nexus server as a background process."""
    pid = _read_pid()
    if pid is not None:
        if _process_running(pid):
            click.echo(f"Server already running (PID {pid}).")
            return
        # Stale PID — remove and restart
        _pid_path().unlink(missing_ok=True)

    _config_dir().mkdir(parents=True, exist_ok=True)
    with _log_path().open("a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "nexus.server_main"],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    _pid_path().write_text(str(proc.pid))
    click.echo(f"Server started (PID {proc.pid}).")


@serve.command("stop")
def stop_cmd() -> None:
    """Stop the running Nexus server."""
    pid = _read_pid()
    if pid is None:
        raise click.ClickException("No server running (no PID file found).")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"Process {pid} not found (stale PID file). Cleaning up.")
        _pid_path().unlink(missing_ok=True)
        return
    # Wait up to 5 seconds for the process to exit before reporting success.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _process_running(pid):
            break
        time.sleep(0.1)
    _pid_path().unlink(missing_ok=True)
    click.echo(f"Server stopped (PID {pid}).")


@serve.command("status")
def status_cmd() -> None:
    """Show server status."""
    pid = _read_pid()
    if pid is None or not _process_running(pid):
        click.echo("Server not running.")
        return
    click.echo(f"Server running (PID {pid}).")


@serve.command("logs")
@click.option("--lines", "-n", default=20, show_default=True, help="Number of tail lines to show.")
def logs_cmd(lines: int) -> None:
    """Show recent server log output."""
    log = _log_path()
    if not log.exists():
        click.echo("No log file found.")
        return
    all_lines = log.read_text().splitlines()
    for line in all_lines[-lines:]:
        click.echo(line)
