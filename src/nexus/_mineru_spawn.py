# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU server spawn core — process-launch primitives.

nexus-1qdb9 review M1: the launch core is consumed by BOTH the CLI
(``nx mineru start``) and library-layer callers (``nexus.daemon.
mineru_lifecycle``, ``nexus.pdf_extractor``'s crash-restart). Hosting it
in ``nexus.commands.mineru`` re-created the CLI→library inversion the
post-4.32.4 audit already fixed once for the pid-file helpers (see
``nexus._mineru_pid``): importing the lifecycle transitively pulled
``click`` into pure library code. Same remedy — package-root private
module, re-exported by the CLI under the legacy names.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


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
    from nexus.config import get_mineru_table_enable  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

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


def spawn_server_process(port: int) -> subprocess.Popen | None:
    """Launch the mineru-api child + write its pid file (nexus-1qdb9).

    The launch core shared by the ``nx mineru start`` command, the
    on-demand lifecycle (``nexus.daemon.mineru_lifecycle``), and the
    crash-restart path (``nexus.pdf_extractor``). Returns the Popen
    handle, or ``None`` when the mineru-api binary is unavailable.
    The pid file is written IMMEDIATELY after the Popen so a concurrent
    elector sees the claim before health passes.
    """
    mineru_bin = _resolve_mineru_api_bin()
    if mineru_bin is None:
        return None
    cmd = [mineru_bin, "--host", "127.0.0.1", "--port", str(port)]
    output_root = _mineru_output_root()
    # RDR-148 Gap 4: the mineru-api server is long-lived; DEVNULL discarded
    # its startup banner and crash tracebacks, the only record of WHY it
    # died (same silent-death class as the chroma/storage-service children,
    # nexus-ovbr7). Route stdout+stderr to a rotated child log instead.
    from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — branch-local; only when spawning the server child

    # config_dir defaults to None (the global config dir): the mineru
    # subsystem is not config-dir-parameterized — _pid_file_path(),
    # _server_env() and _mineru_output_root() all resolve the default dir —
    # so unlike the daemon precedent (self._config_dir) None is correct here.
    server_log = open_child_log_or_devnull("mineru_server")
    try:
        proc = subprocess.Popen(
            cmd,
            env=_server_env(output_root),
            stdout=server_log,
            stderr=server_log,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError):
        return None
    finally:
        if not isinstance(server_log, int):
            server_log.close()

    # Write PID file with 0o600 + record output_root so stop can clean up
    # the per-user extraction artifact directory.
    from nexus._mineru_pid import _pid_file_path  # noqa: PLC0415 — deferred: patchable at the source module

    _write_pid_file(_pid_file_path(), {
        "pid": proc.pid,
        "port": port,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root),
    })
    return proc
