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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": pid,
        "port": port,
        "project": project,
        "cwd": str(Path.cwd()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))


@click.command("console")
@click.option("--port", type=int, default=8765, help="Port for the console server.")
@click.option("--host", type=str, default="127.0.0.1", help="Host to bind to.")
def console(port: int, host: str) -> None:
    """Start the nx console web UI (foreground)."""
    import uvicorn

    from nexus.console.app import create_app
    from nexus.logging_setup import configure_logging

    configure_logging("console")

    project = _project_name()
    pid_path = _pid_file_path(project)
    _write_pid_file(pid_path, os.getpid(), port, project)

    click.echo(f"nx console: http://{host}:{port}/ (project: {project})")

    try:
        app = create_app()
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        pid_path.unlink(missing_ok=True)
