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
    from nexus.config import nexus_config_dir

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
    # NEXUS_CONFIG_DIR takes first priority so sandbox runs keep all
    # Nexus artifacts (T2, catalog, MinerU output) under one isolated tree.
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        base = Path(override) / "mineru-output"
    else:
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


def _resolve_mineru_api_bin() -> str | None:
    """Locate the mineru-api executable, returning an absolute path or None.

    Search order (GH #1059):

    (a) ``Path(sys.executable).parent / "mineru-api"`` — sibling of the
        running interpreter.  Covers ``nx mineru start`` (CLI) and any path
        where the conexus tool-venv Python is the active interpreter.
    (b) Walk up from this module's own ``__file__`` checking each ancestor's
        ``bin/mineru-api``, bounded to 8 levels.  Covers the MCP/daemon
        auto-restart path where the *running* interpreter may differ from the
        conexus venv (e.g. system Python or a project venv) while nexus is
        still installed inside the conexus venv.  Both nexus and mineru are in
        the same venv (``conexus[local]``), so walking up from this file's
        site-packages location eventually reaches the venv root and its bin/.
    (c) ``shutil.which("mineru-api")`` — standard PATH lookup, covers manual
        installs and developer environments where the script IS on PATH.
    (d) None — caller emits the existing not-found error/log (unchanged).

    Every candidate in (a) and (b) is validated with ``is_file()`` AND
    ``os.access(X_OK)``; shutil.which already checks X_OK internally.
    """
    # (a) interpreter-sibling candidate
    candidate_a = Path(sys.executable).parent / "mineru-api"
    if candidate_a.is_file() and os.access(str(candidate_a), os.X_OK):
        return str(candidate_a)

    # (b) __file__-anchored walk — interpreter-agnostic venv-bin discovery
    _here = Path(__file__).resolve()
    for ancestor in _here.parents[:8]:
        candidate_b = ancestor / "bin" / "mineru-api"
        if candidate_b.is_file() and os.access(str(candidate_b), os.X_OK):
            return str(candidate_b)

    # (c) PATH fallback
    return shutil.which("mineru-api")


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
    # GH #1059: resolve mineru-api from the venv bin first, then fall back to
    # PATH.  uv tool install only links conexus's own entry points, not
    # dependencies', so the bare name "mineru-api" fails even though the script
    # exists at <venv>/bin/mineru-api.
    mineru_bin = _resolve_mineru_api_bin()
    if mineru_bin is None:
        click.echo("Error: mineru-api not found on PATH. Install MinerU first.", err=True)
        raise click.exceptions.Exit(1)
    cmd = [mineru_bin, "--host", "127.0.0.1", "--port", str(port)]
    output_root = _mineru_output_root()
    try:
        proc = subprocess.Popen(
            cmd,
            env=_server_env(output_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError):
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
    from nexus.util.process_group import safe_killpg

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
