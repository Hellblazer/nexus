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
import os
from pathlib import Path


def _pid_file_path() -> Path:
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / "mineru.pid"


def read_pid_file() -> dict | None:
    """Read and parse PID file. Returns None if absent or invalid."""
    path = _pid_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
