#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostToolUse hook — captures slow tool durations to T2 hook_telemetry.

Reads the hook event JSON from stdin, extracts ``duration_ms``, and writes
a row to T2's ``hook_telemetry`` table when the duration exceeds the
threshold (env var ``NX_HOOK_TELEMETRY_THRESHOLD_MS``, default 2000ms).

Direct sqlite3 write — does NOT import the full ``nexus.db.t2`` stack to
keep per-invocation startup under ~150ms. The hook fires on every tool
call, so the cheap-fast path matters. Failures are silent (hook must
never block tool execution).

Schema is created/migrated by the ``nx`` CLI on first run via
``migrations.migrate_hook_telemetry``. This script assumes the table
exists; it skips writing silently if not (the user just hasn't run nx
yet, no point in failing the hook).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_THRESHOLD_MS = 2000


def _t2_path() -> Path:
    """Resolve T2 path WITHOUT importing nexus (avoids heavy startup)."""
    config_dir = os.environ.get("NEXUS_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "memory.db"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "nexus" / "memory.db"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0

    duration_ms = payload.get("duration_ms")
    if not isinstance(duration_ms, int):
        return 0

    threshold = int(os.environ.get("NX_HOOK_TELEMETRY_THRESHOLD_MS", str(DEFAULT_THRESHOLD_MS)))
    if duration_ms < threshold:
        return 0

    db_path = _t2_path()
    if not db_path.exists():
        return 0

    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute(
                "INSERT INTO hook_telemetry "
                "(ts, hook_event_name, tool_name, duration_ms, session_id, cwd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    str(payload.get("hook_event_name", "")),
                    str(payload.get("tool_name", "")),
                    duration_ms,
                    str(payload.get("session_id", "")),
                    str(payload.get("cwd", "")),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        # Table missing (first run before migration) or DB locked — silent skip.
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
