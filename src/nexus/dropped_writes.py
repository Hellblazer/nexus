# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-129 B4 (nexus-uq8a4): meter for dropped best-effort T2 writes.

A *drop* is an **unrecovered** best-effort write: a chash dual-write the
daemon could not commit because ``memory.db``'s single WAL writer slot was
held by another process, and which exhausted any retry. Before this module
those failures were swallowed at debug in ``chash_dual_write_batch_hook``
(``mcp_infra.py``), so the completeness gap was invisible without log
spelunking (RDR-129 Gap 4). The meter turns each drop into an appended
record that ``nx doctor`` aggregates into a number.

Design mirrors :mod:`nexus.routing_stats`: a JSONL append log under
``~/.config/nexus`` (env-overridable), aggregated for CLI reporting.
Appends use ``O_APPEND`` so concurrent writers from multiple ``nx-mcp``
processes interleave atomically (one drop record is far below ``PIPE_BUF``,
the POSIX atomic-write threshold). Recording must never raise: it runs
inside a best-effort hook whose contract forbids propagating.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


def default_log_path() -> Path:
    """Return the drop-meter log path, honoring ``NX_DROPPED_WRITES_LOG_PATH``."""
    override = os.environ.get("NX_DROPPED_WRITES_LOG_PATH")
    if override:
        return Path(override)
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / "dropped_writes.jsonl"


@dataclass(frozen=True)
class DropSummary:
    """Aggregated view of recorded drops, for ``nx doctor``."""

    total: int = 0
    rows: int = 0
    last_ts: str | None = None
    last_collection: str = ""


def record_drop(*, hook: str, collection: str, rows: int, error: str) -> None:
    """Append one dropped-best-effort-write record. Never raises.

    *rows* is the number of records in the dropped batch (so the meter can
    report rows lost, not just call sites). *error* is the originating
    exception string (kept short for the log line).
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hook": hook,
        "collection": collection,
        "rows": int(rows),
        "error": str(error)[:200],
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        path = default_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND for atomic interleave across processes.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        # The meter is itself best-effort; a metering failure must not break
        # the enclosing best-effort hook (RDR-129 B4).
        _log.debug("dropped_write_meter_record_failed", exc_info=True)


def count_drops() -> DropSummary:
    """Aggregate the drop log into a :class:`DropSummary`.

    A missing log file means zero drops (the steady state). Malformed lines
    are skipped so a partial last write never poisons the count.
    """
    path = default_log_path()
    if not path.exists():
        return DropSummary()

    total = 0
    rows = 0
    last_ts: str | None = None
    last_collection = ""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                total += 1
                rows += int(rec.get("rows", 0) or 0)
                ts = rec.get("ts")
                if ts:
                    last_ts = ts
                last_collection = rec.get("collection", "") or last_collection
    except OSError:
        return DropSummary(
            total=total, rows=rows, last_ts=last_ts, last_collection=last_collection
        )

    return DropSummary(
        total=total, rows=rows, last_ts=last_ts, last_collection=last_collection
    )
