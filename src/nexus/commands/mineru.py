# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nx mineru start/stop/status — MinerU server lifecycle management.

Manages a persistent mineru-api FastAPI process for PDF extraction.
PID file stored at ~/.config/nexus/mineru.pid (JSON: pid, port, started_at).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx
import structlog

_log = structlog.get_logger(__name__)

_HEALTH_TIMEOUT_SECONDS = 30
_HEALTH_POLL_INTERVAL = 0.5
_STOP_TIMEOUT_SECONDS = 10


def _pid_file_path() -> Path:
    return Path.home() / ".config" / "nexus" / "mineru.pid"


def _read_pid_file() -> dict | None:
    """Read and parse PID file. Returns None if absent or invalid."""
    path = _pid_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _server_env() -> dict[str, str]:
    """Build environment variables for the mineru-api subprocess."""
    from nexus.config import get_mineru_table_enable

    env = os.environ.copy()
    env.update({
        "MINERU_TABLE_ENABLE": str(get_mineru_table_enable()).lower(),
        "MINERU_PROCESSING_WINDOW_SIZE": "8",
        "MINERU_VIRTUAL_VRAM_SIZE": "8192",
        "MINERU_API_OUTPUT_ROOT": "/tmp/mineru-output",
        "MINERU_API_TASK_RETENTION_SECONDS": "300",
    })
    return env


@click.group("mineru")
def mineru_group() -> None:
    """MinerU server lifecycle management."""


def _find_free_port() -> int:
    """Bind to port 0, let the OS assign a free ephemeral port, then release it.

    There is a brief TOCTOU window between releasing the socket and mineru-api
    binding it; in practice this race is negligible on loopback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@mineru_group.command()
@click.option("--port", type=int, default=0, help="Port for mineru-api (0 = auto-assign).")
def start(port: int) -> None:
    """Start the MinerU API server."""
    if port == 0:
        port = _find_free_port()

    # Check if already running
    info = _read_pid_file()
    if info is not None and _is_process_alive(info["pid"]):
        click.echo(f"MinerU server already running (PID {info['pid']}, port {info['port']})")
        return

    # Launch subprocess — start_new_session=True so server survives terminal close
    cmd = ["mineru-api", "--host", "127.0.0.1", "--port", str(port)]
    try:
        proc = subprocess.Popen(
            cmd,
            env=_server_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        click.echo("Error: mineru-api not found on PATH. Install MinerU first.", err=True)
        raise SystemExit(1)

    # Write PID file immediately so concurrent starts detect the race
    pid_path = _pid_file_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(json.dumps({
        "pid": proc.pid,
        "port": port,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))

    # Poll /health until ready
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + _HEALTH_TIMEOUT_SECONDS
    healthy = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pid_path.unlink(missing_ok=True)
            click.echo(
                f"Error: mineru-api process exited unexpectedly "
                f"(port {port} may be in use). Check with: lsof -i :{port}",
                err=True,
            )
            raise SystemExit(1)
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code == 200:
                healthy = True
                break
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(_HEALTH_POLL_INTERVAL)

    if not healthy:
        pid_path.unlink(missing_ok=True)
        click.echo(
            f"Error: health check timeout after {_HEALTH_TIMEOUT_SECONDS}s. "
            f"If this is the first start, MinerU may be downloading models (~2-3 GB). "
            f"Try `nx mineru status` after a few minutes.",
            err=True,
        )
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        raise SystemExit(1)

    # Persist the server URL to config so pdf_extractor can discover it
    from nexus.config import set_config_value
    set_config_value("pdf.mineru_server_url", f"http://127.0.0.1:{port}")

    _log.info("mineru_started", pid=proc.pid, port=port)
    click.echo(f"MinerU server started (PID {proc.pid}, port {port})")


@mineru_group.command()
def stop() -> None:
    """Stop the MinerU API server."""
    info = _read_pid_file()
    pid_path = _pid_file_path()

    if info is None:
        click.echo("MinerU server not running (no PID file)")
        return

    pid = info["pid"]

    # Check if process is alive
    if not _is_process_alive(pid):
        click.echo(f"MinerU server not running (stale PID {pid})")
        pid_path.unlink(missing_ok=True)
        return

    # Send SIGTERM for graceful shutdown
    os.kill(pid, signal.SIGTERM)

    # Poll until process exits (os.waitpid only works on child processes;
    # the server was started by a different nx invocation)
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            break
        time.sleep(0.2)
    else:
        click.echo(
            f"Warning: MinerU server (PID {pid}) did not exit within "
            f"{_STOP_TIMEOUT_SECONDS}s",
            err=True,
        )

    pid_path.unlink(missing_ok=True)
    _log.info("mineru_stopped", pid=pid)
    click.echo(f"MinerU server stopped (PID {pid})")


@mineru_group.command()
def status() -> None:
    """Show MinerU server status. Removes stale PID file if server is not running."""
    info = _read_pid_file()
    pid_path = _pid_file_path()

    if info is None:
        click.echo("MinerU server not running")
        return

    pid = info["pid"]
    port = info["port"]

    if not _is_process_alive(pid):
        click.echo(f"MinerU server not running (stale PID {pid})")
        pid_path.unlink(missing_ok=True)
        return

    # Check /health
    url = f"http://127.0.0.1:{port}/health"
    try:
        resp = httpx.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            active = data.get("active_tasks", "?")
            completed = data.get("completed_tasks", "?")
            click.echo(
                f"MinerU server running and healthy (PID {pid}, port {port})\n"
                f"  Active tasks: {active}\n"
                f"  Completed tasks: {completed}"
            )
        else:
            click.echo(
                f"MinerU server unhealthy (PID {pid}, port {port}, "
                f"status {resp.status_code})"
            )
    except (httpx.ConnectError, httpx.TimeoutException):
        click.echo(
            f"MinerU server unhealthy (PID {pid}, port {port}, "
            f"health check failed)"
        )
