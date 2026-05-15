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
        ``None`` when no discovery file exists or it cannot be parsed.
    """
    path = discovery_path(config_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("t2_discovery_read_failed", path=str(path), error=str(exc))
        return None
