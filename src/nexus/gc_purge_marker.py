# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local breadcrumb of ``nx catalog purge-trash`` executions (nexus-sybbh).

``nexus.gc_audit`` (the engine-side audit table, nexus-jqvzk) is supposed to
carry a row for every reap/purge, but was found completely empty on the live
store (bead nexus-sybbh) — the writer side has a real bug the engine half of
that bead is fixing. Until that lands (and as a permanent second signal
afterward — the same "did we actually check" honesty the RDR-129 B4 house
style requires everywhere else in ``nx doctor``), ``nx doctor`` needs SOME
independent evidence that a purge ran recently, so it can distinguish "gc_audit
is empty because nothing was purged" (fine, skip) from "gc_audit is empty
despite a purge running" (the actual defect, warn).

This module is that independent evidence: a small local JSON-lines file under
``nexus_config_dir()``, appended to ONLY when ``nx catalog purge-trash`` runs
for real (``--no-dry-run --confirm``, i.e. ``will_act=True`` in
``purge_trash_cmd``). It never talks to the engine and never reads gc_audit —
:func:`nexus.health._check_gc_audit_non_empty_after_purge` is the consumer
that cross-references the two.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from nexus.config import nexus_config_dir

_log = structlog.get_logger(__name__)

#: Filename under nexus_config_dir(). Deliberately NOT under logs/ — this is
#: a small structured marker file `nx doctor` parses, not free-text logging.
_MARKER_FILENAME = "gc_purge_markers.jsonl"

#: Cap on retained marker lines (oldest dropped first) — this file is a
#: recent-evidence breadcrumb, not a permanent audit trail (that job belongs
#: to nexus.gc_audit itself once its writer bug is fixed).
_MAX_MARKERS = 50


def _marker_path() -> Path:
    return nexus_config_dir() / _MARKER_FILENAME


def record_purge_marker(result: dict[str, Any], *, older_than_days: int) -> None:
    """Append one marker recording a REAL (non-dry-run) purge-trash execution.

    Best-effort: a failure here must never fail the purge itself (the purge
    already happened by the time this is called) — logged at WARNING and
    swallowed, never raised.
    """
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "older_than_days": older_than_days,
        "result": result,
    }
    path = _marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if path.exists():
            lines = path.read_text().splitlines()
        lines.append(json.dumps(entry))
        lines = lines[-_MAX_MARKERS:]
        tmp = path.with_suffix(f".tmp{time.time_ns()}")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 — best-effort local breadcrumb, must never fail the caller's purge
        _log.warning("gc_purge_marker_record_failed", error=str(exc))


def read_recent_purge_markers(*, within_days: int = 7) -> list[dict[str, Any]]:
    """Return markers recorded within the last *within_days* days, newest last.

    Returns ``[]`` on any read failure or when the file does not exist — a
    missing/unreadable marker file means "no local evidence", which the
    caller (``nx doctor``) must treat as a named skip, never a clean pass.
    """
    path = _marker_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except Exception as exc:  # noqa: BLE001 — best-effort read; caller degrades to "no evidence"
        _log.debug("gc_purge_marker_read_failed", error=str(exc))
        return []

    cutoff = time.time() - within_days * 86400
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["ts"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if ts.timestamp() >= cutoff:
            out.append(entry)
    return out
