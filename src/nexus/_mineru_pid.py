# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU PID-file helpers — process-management primitives.

nexus-8g79.10 (V4): these helpers were previously inside
``nexus.commands.mineru`` (CLI presentation layer) but consumed by
``nexus.config`` and ``nexus.pdf_extractor`` (library layer) — an
inversion flagged by the post-4.32.4 audit.

Hosting them in the package root keeps ``nx mineru ...`` CLI as the
single user-facing surface while letting any library module check
process liveness or read the PID without reaching up into commands/.
"""
from __future__ import annotations

import json
from pathlib import Path


def _pid_file_path() -> Path:
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — circular-dep avoidance: config imports this module's siblings

    return nexus_config_dir() / "mineru.pid"


def read_pid_file() -> dict | None:
    """Read and parse PID file. Returns None if absent or invalid."""
    path = _pid_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive.

    Delegates to :func:`nexus.daemon.service_registry.pid_alive`
    (nexus-f9bgu.25): Windows branch, pid <= 0 dead (``os.kill(0, 0)``
    signalled our own process group and read as alive), another user's
    process alive.
    """
    from nexus.daemon.service_registry import pid_alive  # noqa: PLC0415 — deferred: library modules import this one cheaply

    return pid_alive(pid)


# ── a server whose pid this process cannot judge (nexus-dyfsw site 3) ────────

#: How long after ``started_at`` a pid file from another PID namespace counts as a server still
#: warming. ``spawn_server_process`` writes the file before ``/health`` answers (first start downloads
#: models), and the on-demand lifecycle waits up to this long for it (``_ENSURE_HEALTH_WAIT_S``).
FOREIGN_WARMUP_GRACE_S: float = 120.0

_HEALTH_PROBE_TIMEOUT_S = 1.0


def pid_file_from_other_namespace(info: dict) -> bool:
    """True when the pid file was written from a PID namespace other than ours.

    The one rule the leases use (``service_registry.payload_from_other_pid_namespace``), applied to
    the ``pid_ns`` field :func:`nexus._mineru_spawn.spawn_server_process` records. A file with no
    ``pid_ns`` (an older client, macOS, Windows) is never "other", so an ordinary install judges by
    pid exactly as before and never probes a port.
    """
    from nexus.daemon.service_registry import payload_from_other_pid_namespace  # noqa: PLC0415 — deferred: library modules import this one cheaply

    return payload_from_other_pid_namespace(info)


def health_body_is_mineru(body: object) -> bool:
    """True when a ``/health`` body carries the counters a MinerU server serves.

    ``processing_tasks`` and ``queued_tasks`` are integers there (``nx mineru status`` prints
    them). A bare 200 is not identity: any other service that now holds the recorded port answers
    200 too, and a pid file that kept that alone would wedge ``nx mineru`` after a port reuse.
    """
    if not isinstance(body, dict):
        return False
    return all(
        isinstance(body.get(key), int) and not isinstance(body.get(key), bool)
        for key in ("processing_tasks", "queued_tasks")
    )


def server_identified(info: dict) -> bool:
    """True when ``127.0.0.1:<info["port"]>/health`` answers 200 with a MinerU body (:func:`health_body_is_mineru`)."""
    port = info.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return False
    import httpx  # noqa: PLC0415 — deferred: library modules import this one cheaply

    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=_HEALTH_PROBE_TIMEOUT_S)
        return resp.status_code == 200 and health_body_is_mineru(resp.json())
    except (httpx.HTTPError, ValueError):
        return False


def _still_warming(info: dict, now: float) -> bool:
    from datetime import datetime  # noqa: PLC0415 — stdlib, deferred

    started = info.get("started_at")
    if not isinstance(started, str):
        return False
    try:
        age = now - datetime.fromisoformat(started).timestamp()
    except ValueError:
        return False
    return 0 <= age < FOREIGN_WARMUP_GRACE_S


def recorded_server_alive(info: dict, *, now: float | None = None) -> bool:
    """True when the server the pid file records is running.

    In this process's PID namespace, or when the file records none, the pid decides, as it always
    did. A file written from another PID namespace that shares this config directory (a container
    sharing HOME, a namespaced CI job on a real install) names a pid that means nothing here: a
    local process with that number proves nothing either way, and an absent one proves nothing
    about the server. That file is judged by identity on the recorded port instead: a ``/health``
    body with the MinerU counters (:func:`server_identified`), or a ``started_at`` young enough
    that the server may still be loading models (``FOREIGN_WARMUP_GRACE_S``). Anything else is
    stale. Unlinking a live foreign server's file let a second server start (nexus-dyfsw).
    """
    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    if not pid_file_from_other_namespace(info):
        return is_process_alive(pid)
    import time  # noqa: PLC0415 — stdlib, deferred

    return server_identified(info) or _still_warming(info, time.time() if now is None else now)


def clear_pid_file_if(pid: int) -> None:
    """Unlink the pid file only while it still names *pid*.

    A spawner whose child died or never answered clears its own record, not the record of a server
    another process started after the election was released.
    """
    info = read_pid_file()
    if info is not None and info.get("pid") == pid:
        _pid_file_path().unlink(missing_ok=True)
