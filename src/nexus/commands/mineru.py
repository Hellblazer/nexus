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


def _mineru_output_root() -> Path:
    """Return the per-user output root for MinerU extraction artifacts.

    Avoids the world-writable ``/tmp/mineru-output`` default (CLI review
    Critical): on shared Linux hosts, a local attacker can pre-create
    that path or symlink it to intercept extracted PDF content. Uses
    ``$XDG_RUNTIME_DIR`` when available (per-user, 0700 by spec) and
    falls back to ``~/.cache/nexus/mineru-output`` otherwise. Creates
    the directory with 0o700 so other users on the same host cannot
    read extracted documents.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        base = Path(runtime) / "nexus-mineru"
    else:
        base = Path.home() / ".cache" / "nexus" / "mineru-output"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Re-chmod in case the directory pre-existed with wider mode.
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _server_env(output_root: Path) -> dict[str, str]:
    """Build environment variables for the mineru-api subprocess."""
    from nexus.config import get_mineru_table_enable

    env = os.environ.copy()
    env.update({
        "MINERU_TABLE_ENABLE": str(get_mineru_table_enable()).lower(),
        "MINERU_PROCESSING_WINDOW_SIZE": "8",
        "MINERU_VIRTUAL_VRAM_SIZE": "8192",
        "MINERU_API_OUTPUT_ROOT": str(output_root),
        "MINERU_API_TASK_RETENTION_SECONDS": "300",
    })
    return env


def _write_pid_file(pid_path: Path, payload: dict) -> None:
    """Write the MinerU PID file with 0o600 mode.

    CLI review Critical: the previous ``pid_path.write_text(...)`` used
    the default umask (typically 0o644) which exposed the port + CWD
    to other users on shared hosts. Use ``os.open`` with explicit mode
    to enforce 0o600 regardless of umask.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload).encode("utf-8")
    # O_WRONLY | O_CREAT | O_TRUNC to replace atomically; the tempfile
    # approach is overkill here since only one mineru server per user
    # should exist at a time.
    fd = os.open(
        pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


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
    output_root = _mineru_output_root()
    try:
        proc = subprocess.Popen(
            cmd,
            env=_server_env(output_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        click.echo("Error: mineru-api not found on PATH. Install MinerU first.", err=True)
        raise click.exceptions.Exit(1)

    # Write PID file with 0o600 + record output_root so stop can clean up
    # the per-user extraction artifact directory.
    pid_path = _pid_file_path()
    _write_pid_file(pid_path, {
        "pid": proc.pid,
        "port": port,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root),
    })

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
            raise click.exceptions.Exit(1)
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
        raise click.exceptions.Exit(1)

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

    # SIGTERM the entire process group owned by *pid*. MinerU's
    # multiprocessing workers and their ``resource_tracker`` children
    # must receive the signal too — otherwise the tracker never gets
    # to ``sem_unlink`` and POSIX named semaphores leak into the
    # global namespace (bead nexus-ze2a root cause). ``start_new_session
    # =True`` at spawn (mineru.py start) guarantees they share one
    # killable pgid.
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid  # fallback: treat head pid as its own group
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        # Process group already gone — nothing to do.
        pid_path.unlink(missing_ok=True)
        click.echo(f"MinerU server stopped (PID {pid})")
        return

    # Poll until process exits (os.waitpid only works on child processes;
    # the server was started by a different nx invocation).
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            break
        time.sleep(0.2)
    else:
        # Escalate to SIGKILL on the group — same reason (reach workers).
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass  # already gone
        click.echo(
            f"Warning: MinerU server (PID {pid}) did not exit within "
            f"{_STOP_TIMEOUT_SECONDS}s; escalated SIGKILL to process group",
            err=True,
        )

    pid_path.unlink(missing_ok=True)

    # Clean up the per-user output directory — extracted PDF artifacts
    # may contain confidential content and should not linger past the
    # server's lifetime (CLI review Critical). Best-effort: the directory
    # may not exist on older installs that used the pre-fix /tmp path.
    output_root = info.get("output_root") if info else None
    if output_root:
        try:
            import shutil

            shutil.rmtree(output_root, ignore_errors=True)
        except Exception:
            _log.debug("mineru_output_cleanup_failed", path=output_root, exc_info=True)

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
