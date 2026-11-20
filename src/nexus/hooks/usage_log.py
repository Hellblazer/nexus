# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The usage file behind the 30-day review of ``nx brief`` topics: ``<config>/logs/brief.log``, one JSON object a line.

Two events share it, written through :func:`append`: ``brief_load`` (one per ``nx brief`` / MCP ``brief`` call, with the
topic, the session id and the repo) and ``session_start`` (one per SessionStart, the denominator for "loaded in N% of
sessions"). The CLI logs at WARNING with no file sink, so a structlog line alone would never reach a review; this file does.

Past :data:`MAX_BYTES` the file moves to ``brief.log.1`` (replacing an older one) and a new file takes the line, so it keeps
recording and the two stay near twice the cap. **No lock.** Two processes that cross the cap together can both rotate, and
the second then overwrites ``brief.log.1`` with a near-empty file: the worst case is one generation of lines lost or a line
duplicated in a usage count. ``fcntl`` is absent on Windows, and a count read over 30 days does not need exactness.

Standard library at module scope, as the hook that calls it runs on every session start.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["MAX_BYTES", "append", "path"]

#: Past this size the file rotates (about 6,000 lines at the size of a ``brief_load`` line with its ids).
MAX_BYTES = 1_000_000


def path() -> Path:
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    config = Path(override) if override else Path.home() / ".config" / "nexus"
    return config / "logs" / "brief.log"


def append(event: str, **fields: object) -> None:
    """Add one ``{"ts", "event", **fields}`` line. Never raises: usage accounting must not cost a session or a brief."""
    try:
        target = path()
        if target.exists() and target.stat().st_size > MAX_BYTES:
            target.replace(target.with_name(target.name + ".1"))
        target.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **fields}
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 — see the docstring
        return
