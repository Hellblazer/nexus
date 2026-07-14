# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nx mineru start/stop/status — MinerU server lifecycle management.

Manages a persistent mineru-api FastAPI process for PDF extraction.
PID file stored at ~/.config/nexus/mineru.pid (JSON: pid, port, started_at).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
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
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    return nexus_config_dir() / "mineru.pid"


# nexus-8g79.10 (V4): the PID-file primitives moved to
# ``nexus._mineru_pid`` so library-layer callers (``nexus.config``,
# ``nexus.pdf_extractor``) can use them without reaching up into
# commands/. Re-exported under the legacy private names so CLI code
# inside this module keeps working unchanged.
from nexus._mineru_pid import (  # noqa: E402
    is_process_alive as _is_process_alive,
    read_pid_file as _read_pid_file,
)

# nexus-1qdb9 review M1: the spawn core moved to ``nexus._mineru_spawn``
# for the same layering reason (library callers — the on-demand lifecycle
# and the crash-restart path — must not import the CLI layer). Re-exported
# under the legacy names so CLI code and existing patch targets keep
# working unchanged.
from nexus._mineru_spawn import (  # noqa: E402
    _find_free_port,
    _mineru_output_root,
    _resolve_mineru_api_bin,
    _server_env,
    _write_pid_file,
    spawn_server_process,
)


@click.group("mineru")
def mineru_group() -> None:
    """MinerU server lifecycle management."""


@mineru_group.command()
@click.option("--port", type=int, default=0, help="Port for mineru-api (0 = auto-assign).")
def start(port: int) -> None:
    """Start the MinerU API server."""
    if port == 0:
        # nexus incident 2026-07-01: auto-assign used to ignore config
        # outright, so an operator with a fixed non-default local port in
        # pdf.mineru_server_url got a live server on a DIFFERENT random
        # port than get_mineru_server_url() would ever look for — success
        # message, invisible server. Honor the same "explicit operator
        # intent wins" precedence get_mineru_server_url() already applies
        # on the read side (RDR-148 Gap 1) on this write side too.
        from nexus.config import get_mineru_configured_fixed_port  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        configured_port = get_mineru_configured_fixed_port()
        port = configured_port if configured_port is not None else _find_free_port()

    # nexus-c7odl: the check-then-spawn runs under the same RDR-149
    # election as the on-demand lifecycle so an explicit `nx mineru start`
    # (or upgrade-finish's restart-stale, which shells to it) cannot race a
    # concurrent autostart into two live servers. The explicit verb is
    # deliberately NOT policy-gated — `nx mineru start` with
    # mineru_autostart=false must still work.
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.daemon.mineru_lifecycle import MINERU_TIER, _SPAWN_SCOPE  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    registry = ServiceRegistry(dir=nexus_config_dir(), tier=MINERU_TIER)
    with registry.election(_SPAWN_SCOPE):
        # Check if already running
        info = _read_pid_file()
        if info is not None and _is_process_alive(info["pid"]):
            click.echo(f"MinerU server already running (PID {info['pid']}, port {info['port']})")
            return

        # Launch subprocess — start_new_session=True so server survives
        # terminal close.
        # GH #1059: resolve mineru-api from the venv bin first, then fall
        # back to PATH.  uv tool install only links conexus's own entry
        # points, not dependencies', so the bare name "mineru-api" fails
        # even though the script exists at <venv>/bin/mineru-api.
        proc = spawn_server_process(port)
    if proc is None:
        click.echo("Error: mineru-api not found on PATH. Install MinerU first.", err=True)
        raise click.exceptions.Exit(1)
    pid_path = _pid_file_path()

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

    # nexus-oa7r: do NOT write the port to persistent config. The PID
    # file is the canonical source of truth; ``get_mineru_server_url``
    # reads it at call time. Persisting an ephemeral port to config
    # caused drift across reboots — the dead port survived as a
    # config record while the server didn't.
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
    # Both signals go through safe_killpg for mock-guard + error-swallow
    # consistency with every other subprocess cleanup site.
    from nexus.util.process_group import safe_killpg  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    if not safe_killpg(pid, signal.SIGTERM):
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
        safe_killpg(pid, signal.SIGKILL)
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
            import shutil  # noqa: PLC0415 — deferred to keep CLI startup fast

            shutil.rmtree(output_root, ignore_errors=True)
        except Exception:  # noqa: BLE001 — best-effort output cleanup; logged at debug
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
