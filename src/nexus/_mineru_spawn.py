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
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog
from nexus._winsec import make_user_dir
from nexus.util.nx_argv import which_off_cwd

_log = structlog.get_logger(__name__)

#: Health-poll cadence shared by every wait loop (CLI start, crash-restart).
_HEALTH_POLL_INTERVAL = 0.5


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
        # Absolute: the server starts in the filesystem root (nexus-x3m8e), so a
        # relative override would name a different directory for it, and the
        # pid file records this path for `nx mineru stop` to clean up.
        base = Path(os.path.abspath(override)) / "mineru-output"
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and Path(runtime).is_dir():
            base = Path(runtime) / "nexus-mineru"
        else:
            from nexus.db import onnx_model_root  # noqa: PLC0415 — the one copy of the cache root

            base = onnx_model_root.nexus_cache_root() / "mineru-output"
    make_user_dir(base)
    # Re-chmod in case the directory pre-existed with wider mode.
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _server_env(output_root: Path) -> dict[str, str]:
    """Build environment variables for the mineru-api subprocess."""
    env = os.environ.copy()
    # nexus-jd8fi: MINERU_TABLE_ENABLE is deliberately NOT exported. MinerU's
    # ``get_table_enable`` lets that env var override the per-request
    # ``table_enable`` form field, so exporting it froze the config value
    # read at spawn time into the server for its whole life and made the
    # flag the extractor sends with every request a no-op. Not set means
    # each request's own ``table_enable`` governs.
    env.pop("MINERU_TABLE_ENABLE", None)
    env.update({
        "MINERU_PROCESSING_WINDOW_SIZE": "8",
        # UNIT IS GB, not MB: mineru's get_vram() returns this value raw and
        # its auto-detect paths all convert bytes -> GB before comparing
        # against the batch-ratio ladder (>=8 -> 4, >=16 -> 8, higher -> 16).
        # "8192" read as 8192 GB selected ratio 16, whose MFR batches blow
        # unified memory on Apple Silicon and get the server silently killed
        # mid-batch (nexus-xyk9o, 2026-08-02: two server generations died at
        # 'MFR Predict 0/65'). 8 GB -> ratio 4, which survives formula-dense
        # windows on a 128 GB M-series box.
        "MINERU_VIRTUAL_VRAM_SIZE": "8",
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
    (c) ``which_off_cwd("mineru-api")`` — standard PATH lookup, covers manual
        installs and developer environments where the script IS on PATH.
    (d) None — caller emits the existing not-found error/log (unchanged).

    Every candidate in (a) and (b) is validated with ``is_file()`` AND
    ``os.access(X_OK)``; ``which`` already checks X_OK internally.
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

    # (c) PATH fallback. Off the working directory: on Windows ``shutil.which``
    # answers with a ``mineru-api.exe`` in the cwd first, and this result is spawned
    # (RDR-224 critique S1, nexus-f9bgu.37).
    return which_off_cwd("mineru-api")


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
        # nexus-6y4e0 surveyed this site and left it unwired: the server is
        # stopped by a LATER, separate ``nx mineru stop`` invocation reading
        # this pid from the pid file (see _write_pid_file below), not by
        # this process. A Windows job-object handle lives only in the
        # process that created it, so containment here needs a NAMED job
        # (CreateJobObjectW with a name a later ``stop`` process opens by
        # that same name) -- part of nexus-6y4e0's remaining Windows scope
        # (see its comments), not forced in here, and not verifiable until
        # qwentescence has a native Windows Python (nexus-6y4e0's own
        # closing report: only a broken uv trampoline and the Microsoft
        # Store install-alias stub are present there today).
        # nexus-x3m8e: start in a STABLE cwd (the filesystem root), never the
        # caller's. A server spawned from a session worktree that was later
        # deleted answered every task with FileNotFoundError while /health said
        # 200. The failing call, from the server's own traceback: importing
        # torchvision.ops pulls in torch._dynamo.config, whose module body runs
        # os.path.join(os.getcwd(), "torch_compile_debug"). Not CPython's
        # import system (it guards getcwd for the '' sys.path entry): any
        # getcwd/abspath/Path.cwd call in the server or its libraries.
        # detached_spawn_kwargs also pins a relative NEXUS_CONFIG_DIR, which
        # the child would otherwise resolve against its new cwd.
        from nexus.daemon.service_registry import detached_spawn_kwargs  # noqa: PLC0415 — branch-local; only when spawning the server

        stable = detached_spawn_kwargs()
        env = _server_env(output_root)
        if "env" in stable:
            env["NEXUS_CONFIG_DIR"] = stable.pop("env")["NEXUS_CONFIG_DIR"]
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=server_log,
            stderr=server_log,
            start_new_session=True,
            **stable,
        )
    except (FileNotFoundError, PermissionError):
        return None
    finally:
        if not isinstance(server_log, int):
            server_log.close()

    # Write PID file with 0o600 + record output_root so stop can clean up
    # the per-user extraction artifact directory.
    from nexus._mineru_pid import _pid_file_path  # noqa: PLC0415 — deferred: patchable at the source module

    # nexus-yq3vk: record WHICH mineru this server runs, and under which
    # interpreter. The pid file is discovered by any process sharing the config
    # dir (config.get_mineru_server_url step 2), and validation was
    # is_process_alive() alone — existence, not identity. A develop checkout
    # pinning mineru 3.1.11 therefore sent extraction to a server started by an
    # installed tool running 3.4.4, and uv.lock silently did not control the
    # code that produced the text. Different versions produce different
    # extraction, so this is a data-correctness fact, not bookkeeping.
    try:
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415 — deferred

        _mineru_version: str | None = _pkg_version("mineru")
    except Exception:  # noqa: BLE001 — an unknown version must not block a spawn
        _mineru_version = None

    _write_pid_file(_pid_file_path(), {
        "pid": proc.pid,
        "port": port,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root),
        "mineru_version": _mineru_version,
        "python": sys.executable,
        # nexus-x3m8e: the executable this server runs, beside the interpreter
        # and output root above: `nx mineru status` stats all three, because a
        # tool environment replaced under a running server takes its libraries
        # (and every lazy import after it) away while /health still says 200.
        "executable": mineru_bin,
    })
    return proc


def classify_server_pid(pid: int) -> str:
    """What the live process behind a recorded server *pid* is: ``"mineru"``
    (a mineru-api server), ``"other"`` (the pid was recycled to something else)
    or ``"unreadable"`` (alive but its command line could not be read, which is
    inconclusive: a slow process-table read looks the same as a vanished
    process).

    The pid-recycle guard (nexus-5yrob) that EVERY stop of a pid-file server
    runs before it signals the pid's whole process group: ``nx mineru stop`` and
    the extractor's replacement of a broken server share it, so neither can
    signal an unrelated process group that reused the pid."""
    from nexus.upgrade_finish import _classify, process_command  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    live_cmd = process_command(pid)
    if not live_cmd:
        return "unreadable"
    return "mineru" if _classify(live_cmd) == "mineru" else "other"


#: Seconds the server group gets to exit on SIGTERM before the hard pass.
STOP_GRACE_SECONDS: float = 10.0


def stop_server_group(
    pid: int,
    *,
    grace: float = STOP_GRACE_SECONDS,
    _sleep=None,
) -> str:
    """Stop the mineru-api server group *pid* leads; the one stop core shared by
    ``nx mineru stop`` and the extractor's restart (nexus-x3m8e).

    SIGTERM to the whole group first (the multiprocessing resource tracker
    ignores it by design, so it still cleans up). If the group is still alive
    after *grace*, the hard pass SPARES the resource tracker: killing it
    leaks every POSIX named semaphore the group made, for good (the 2026-10-16
    machine-wide ENOSPC). Returns ``"gone"`` (no such group), ``"exited"`` or
    ``"escalated"``. The caller owns the pid file.
    """
    import signal  # noqa: PLC0415 — branch-local
    import time  # noqa: PLC0415 — branch-local

    from nexus._mineru_pid import is_process_alive  # noqa: PLC0415 — deferred: patchable at the source module
    from nexus.util.process_group import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        safe_killpg,
        safe_killpg_group,
    )

    sleep = _sleep if _sleep is not None else time.sleep
    if not safe_killpg(pid, signal.SIGTERM):
        return "gone"
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return "exited"
        sleep(0.2)
    if getattr(os, "killpg", None) is None:
        # Windows: no process group, the pid is the one process to terminate
        # (job-object reach belongs to the spawning process, nexus-6y4e0).
        safe_killpg(pid)
    else:
        # The hard pass spares the resource tracker by construction
        # (util.process_group), and a group left with only its tracker counts
        # as stopped: no second, whole-group kill follows it.
        safe_killpg_group(pid)
    return "escalated"
