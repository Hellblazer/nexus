# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx console — embedded web UI for monitoring agentic Nexus activity."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


def _config_dir() -> Path:
    override = os.environ.get("NEXUS_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


def _project_name() -> str:
    """Derive project name from the current working directory."""
    return Path.cwd().name


def _pid_file_path(project: str) -> Path:
    return _config_dir() / f"console.{project}.pid"


def _write_pid_file(path: Path, pid: int, port: int, project: str) -> None:
    """Write the console PID file with 0o600 mode.

    CLI review Critical: the previous ``path.write_text(...)`` used the
    default umask (typically 0o644) which exposed the port + CWD to
    other users on shared hosts. Use ``os.open`` with explicit mode to
    enforce 0o600 regardless of umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "pid": pid,
        "port": port,
        "project": project,
        "cwd": str(Path.cwd()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _is_process_alive(pid: int) -> bool:
    """Return True if the PID is still running. Signal 0 probes liveness."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _check_stale_pid_file(path: Path) -> None:
    """Probe an existing PID file; delete it if the PID is gone.

    CLI review Critical: without this, a second ``nx console`` invocation
    silently starts a second server on the same port (uvicorn then fails
    with EADDRINUSE further downstream). Probe-and-reap on startup so the
    user gets a clean error when a live server is already running and a
    stale file is cleaned up automatically.
    """
    if not path.exists():
        return
    try:
        info = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupt file — reap it so we can write a fresh one.
        path.unlink(missing_ok=True)
        return
    pid = info.get("pid")
    port = info.get("port")
    if isinstance(pid, int) and _is_process_alive(pid):
        raise click.ClickException(
            f"nx console already running (PID {pid}, port {port}). "
            f"Stop it first, or remove {path} if the PID is a false positive."
        )
    # Stale — reap.
    path.unlink(missing_ok=True)


@click.command("console")
@click.option("--port", type=int, default=8765, help="Port for the console server.")
@click.option("--host", type=str, default="127.0.0.1", help="Host to bind to.")
def console(port: int, host: str) -> None:
    """Start the nx console web UI (foreground)."""
    import uvicorn

    from nexus.console.app import create_app
    from nexus.logging_setup import configure_logging

    configure_logging("console")

    # Warn on non-loopback binds — the console has no auth layer.
    if host != "127.0.0.1" and host != "localhost":
        click.echo(
            f"warning: binding to {host} exposes the console without auth. "
            f"Use a reverse proxy with TLS + auth for non-loopback deployments.",
            err=True,
        )

    project = _project_name()
    pid_path = _pid_file_path(project)

    # Reap a stale PID file or refuse to start over a live one.
    _check_stale_pid_file(pid_path)

    _write_pid_file(pid_path, os.getpid(), port, project)

    click.echo(f"nx console: http://{host}:{port}/ (project: {project})")

    try:
        app = create_app()
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        pid_path.unlink(missing_ok=True)
