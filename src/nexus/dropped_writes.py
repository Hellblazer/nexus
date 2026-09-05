# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-129 B4 (nexus-uq8a4): meter for dropped best-effort T2 writes.

A *drop* is an **unrecovered** best-effort write: one the daemon could
not commit because ``memory.db``'s single WAL writer slot was held by
another process, and which exhausted any retry. The founding consumer was
the chash dual-write hook (retired by RDR-187 / nexus-piwya.4 — the
chunks tables are the chash store now, so that writer no longer exists);
before this module its failures were swallowed at debug in
``mcp_infra.py``, so the completeness gap was invisible without log
spelunking (RDR-129 Gap 4). The meter turns each drop into an appended
record that ``nx doctor`` aggregates into a number; historical records
naming the retired hook remain readable data. LIVE PRODUCERS as of
nexus-gjv9b: the ``capability_census``/``routing_events`` engine-table
writer swaps both degrade to this meter on service-down, so the meter
is no longer historical-only (see :func:`record_drop`'s docstring).

Design mirrors :mod:`nexus.routing_stats`: a JSONL append log under
``~/.config/nexus`` (env-overridable), aggregated for CLI reporting.
Appends use ``O_APPEND`` so concurrent writers from multiple ``nx-mcp``
processes interleave atomically (one drop record is far below ``PIPE_BUF``,
the POSIX atomic-write threshold). Recording must never raise: it runs
inside a best-effort hook whose contract forbids propagating.
"""
from __future__ import annotations

import calendar
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
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    return nexus_config_dir() / "dropped_writes.jsonl"


@dataclass(frozen=True)
class DropSummary:
    """Aggregated view of recorded drops, for ``nx doctor``."""

    total: int = 0
    rows: int = 0
    last_ts: str | None = None
    last_collection: str = ""
    #: The ``hook`` field of the MOST RECENT record (nexus-gjv9b: this
    #: meter is no longer historical-only — capability_census and
    #: routing_events both adopted ``record_drop``/the equivalent inline
    #: format for their own service-down degradation). ``_check_t2_
    #: dropped_writes`` keys its soft-WARN-vs-historical framing on this
    #: rather than assuming every drop is from the retired chash
    #: dual-write hook.
    last_hook: str = ""
    #: Drops within :func:`count_drops`'s ``recent_hours`` decay window
    #: (nexus-gjv9b review fold-in, critique CRITICAL 2): a live producer
    #: is expected to have OCCASIONAL drops during a real outage, and once
    #: the outage clears those drops must eventually stop being CURRENT
    #: evidence — without a decay window, one drop from months ago would
    #: soft-WARN ``nx doctor`` forever, exactly the "permanent false
    #: alarm" nexus-piwya.9's own retirement of the founding chash-hook
    #: alarm already learned not to ship. ``total``/``last_hook`` above
    #: stay LIFETIME figures (audit visibility never shrinks); these two
    #: are the DECISION inputs for "is this failing RIGHT NOW".
    recent_total: int = 0
    recent_last_hook: str = ""


def record_drop(*, hook: str, collection: str, rows: int, error: str) -> None:
    """Append one dropped-best-effort-write record. Never raises.

    RETIRED FOUNDING PRODUCER (RDR-187 / nexus-piwya.4): the chash
    dual-write hook that originally justified this meter is gone. LIVE
    PRODUCERS as of nexus-gjv9b PART 1/2: ``capability_census``
    (``nexus._session_end_census._post_capability_census``, this
    function called directly) and ``routing_events``
    (``conexus/hooks/scripts/routing/_lib.py``'s
    ``_record_dropped_routing_event``, which hand-replicates this
    function's exact on-disk record shape rather than importing it — that
    script has no ``nexus`` dependency). ``health._check_t2_dropped_writes``
    keys its soft-WARN-vs-historical framing on :attr:`DropSummary.last_hook`
    so a new producer's drops are never misreported as leftover chash-hook
    history.

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
    except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
        # The meter is itself best-effort; a metering failure must not break
        # the enclosing best-effort hook (RDR-129 B4).
        _log.debug("dropped_write_meter_record_failed", exc_info=True)


#: Default decay window for :func:`count_drops`'s ``recent_*`` fields
#: (nexus-gjv9b review fold-in, critique CRITICAL 2). 24 hours: this meter
#: answers "is a best-effort write failing RIGHT NOW", an operational
#: question on the same cadence as a live outage, not an audit/retention
#: question like ``expire_relevance_log``'s 90-day default — a drop from
#: last week is not evidence anything is broken today.
_DEFAULT_RECENT_HOURS = 24.0

#: The record_drop ``ts`` format (``time.strftime`` in ``record_drop``
#: itself) — UTC, second precision, trailing literal ``Z``.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_drop_ts_epoch(ts: str) -> float | None:
    """``ts`` (the exact ``record_drop``-written format) to a UTC epoch
    float, or ``None`` on anything that does not parse — a malformed or
    foreign-shaped timestamp must never crash the aggregate, only fall
    out of the recency window (never counted as "recent", which is the
    safe direction: it can only make the alarm LESS sticky, never more).
    """
    try:
        return calendar.timegm(time.strptime(ts, _TS_FORMAT))
    except (ValueError, TypeError):
        return None


def count_drops(recent_hours: float = _DEFAULT_RECENT_HOURS) -> DropSummary:
    """Aggregate the drop log into a :class:`DropSummary`.

    A missing log file means zero drops (the steady state). Malformed lines
    are skipped so a partial last write never poisons the count.

    ``recent_hours`` (nexus-gjv9b review fold-in) bounds the
    ``recent_total``/``recent_last_hook`` fields to drops whose ``ts`` is
    within this many hours of "now" — the decay window that keeps
    ``_check_t2_dropped_writes``'s soft-WARN from becoming a permanent
    false alarm over one drop from months ago. ``total``/``last_hook``
    remain LIFETIME figures, unaffected by this window.
    """
    path = default_log_path()
    if not path.exists():
        return DropSummary()

    cutoff = time.time() - (recent_hours * 3600.0)
    total = 0
    rows = 0
    last_ts: str | None = None
    last_collection = ""
    last_hook = ""
    recent_total = 0
    recent_last_hook = ""
    recent_last_epoch = -1.0
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
                last_hook = rec.get("hook", "") or last_hook
                epoch = _parse_drop_ts_epoch(ts) if ts else None
                if epoch is not None and epoch >= cutoff:
                    recent_total += 1
                    if epoch >= recent_last_epoch:
                        recent_last_epoch = epoch
                        recent_last_hook = rec.get("hook", "") or recent_last_hook
    except OSError:
        return DropSummary(
            total=total, rows=rows, last_ts=last_ts,
            last_collection=last_collection, last_hook=last_hook,
            recent_total=recent_total, recent_last_hook=recent_last_hook,
        )

    return DropSummary(
        total=total, rows=rows, last_ts=last_ts,
        last_collection=last_collection, last_hook=last_hook,
        recent_total=recent_total, recent_last_hook=recent_last_hook,
    )
