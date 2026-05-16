# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client-side discovery helper for the T2 daemon.

Reads the discovery file written by ``T2Daemon._write_discovery`` and
returns the parsed JSON payload, or ``None`` when no daemon is running
for the current UID.

Used by:
  - ``nexus.mcp.core`` to construct a ``T2Client`` under
    ``NX_STORAGE_MODE=daemon``.
  - ``nexus.cockpit.hook_bridge`` to route hook tuples through the
    daemon when one is reachable.

The discovery file lives at ``<config_dir>/t2_addr.<uid>``. The daemon
writes it atomically (tmpfile + os.replace) so a partial read is not
possible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.config import nexus_config_dir

_log = structlog.get_logger(__name__)


def discovery_path(config_dir: Optional[Path] = None) -> Path:
    """Return the discovery file path for the current UID."""
    cd = config_dir if config_dir is not None else nexus_config_dir()
    return cd / f"t2_addr.{os.getuid()}"


def find_t2_daemon(config_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the daemon's discovery payload, or ``None`` if absent / unreadable.

    Args:
        config_dir: Optional override (defaults to ``nexus_config_dir()``).

    Returns:
        Dict with keys ``uds_path``, ``tcp_host``, ``tcp_port``, ``pid``,
        ``daemon_version``, etc. (see ``T2Daemon._discovery_payload``).
        ``None`` when no discovery file exists, cannot be parsed, or its
        ``pid`` field refers to a process that is no longer alive.

    Liveness probe (nexus-j6dj): hard reboot / SIGKILL / OOM / panic
    between ``_write_discovery`` and ``_unlink_discovery`` leaves a stale
    file pointing at a dead PID. Clients that trust the file route to a
    nonexistent socket and either fall back to direct mode (with WAL
    conflicts once a new daemon eventually binds) or hang on the connect
    attempt. ``os.kill(pid, 0)`` is the canonical POSIX way to probe
    liveness without delivering a signal; ``ProcessLookupError`` means
    the PID is unallocated and the file is stale, in which case we
    best-effort unlink it so a future check is fast. A ``PermissionError``
    means the PID exists under a different UID — that is a sysadmin-
    level scenario; treat as live and let the eventual connect surface
    a clear error.
    """
    path = discovery_path(config_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("t2_discovery_read_failed", path=str(path), error=str(exc))
        return None

    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        _log.warning(
            "t2_discovery_invalid_pid", path=str(path), pid=repr(pid)
        )
        return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _log.warning("t2_discovery_stale_pid", path=str(path), pid=pid)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                "t2_discovery_unlink_failed",
                path=str(path),
                error=str(exc),
            )
        return None
    except PermissionError:
        # Live process under a different UID. Keep the payload — the
        # eventual connect will give a clearer error than we can here.
        pass

    return payload
