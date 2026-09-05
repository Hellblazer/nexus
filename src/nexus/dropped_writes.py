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
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: Cause classifier for a drop's *error* string (nexus-gjv9b review
#: fold-in round 3, critique CRITICAL 2 / code-review item 1). Purely a
#: text match over whatever ``str(exc)`` (or a caller's richer
#: ``f"{type(exc).__name__}: {exc}"``) already produced -- no re-dispatch,
#: no re-raising, never fails. Order matters: ``"guard_refused"`` is
#: checked FIRST because ``ProductionWriteGuardError``'s own message text
#: ("STOP: refusing a WRITE to ...") would otherwise also satisfy a naive
#: "connect"/"refused" pattern below it, and the two causes could not be
#: more different in what they mean for an operator — a guard refusal is
#: an un-opted-in DEV CHECKOUT correctly protecting itself (nexus-a2qhz;
#: expected on every un-opted-in process, not a service problem at all),
#: while a connection refusal is the engine itself unreachable.
#:
#: Recognized causes, in match order:
#:   guard_refused -- ``ProductionWriteGuardError`` (dev-checkout opt-in
#:       missing); never "service is down", so callers must not fold it
#:       into that framing.
#:   401 / 403     -- an ``httpx.HTTPStatusError``-shaped "HTTP 401"/"HTTP
#:       403" message (:meth:`RefreshableHttpStoreMixin._raise_for_status`'s
#:       exact wording), or a stdlib ``urllib.error.HTTPError`` cause
#:       string a caller already reduced to this literal token.
#:   5xx           -- "HTTP 5xx" shaped, any 500-599 status.
#:   timeout       -- a connect/read/write/pool timeout.
#:   connect       -- a transport-level connection failure (refused, DNS,
#:       reset) that is NOT the guard.
#:   unresolvable  -- no endpoint/credential could be resolved at all.
#:   other         -- anything else recognized as A failure but not
#:       classifiable further.
#: An empty *error* classifies as ``""`` (unclassified) rather than
#: "other" -- a caller with nothing to say should not manufacture a cause.
_CAUSE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("guard_refused", re.compile(r"refusing a write|productionwriteguarderror", re.IGNORECASE)),
    ("401", re.compile(r"\bhttp[ _]?401\b|\b401\b.*unauthorized", re.IGNORECASE)),
    ("403", re.compile(r"\bhttp[ _]?403\b|\b403\b.*forbidden", re.IGNORECASE)),
    ("5xx", re.compile(r"\bhttp[ _]?5\d\d\b", re.IGNORECASE)),
    ("timeout", re.compile(r"timed? ?out|timeout", re.IGNORECASE)),
    ("connect", re.compile(r"connect|connection refused|name or service not known|dns", re.IGNORECASE)),
    ("unresolvable", re.compile(r"unresolvable|not resolvable|no service_url|no.*token is resolvable", re.IGNORECASE)),
)


def classify_drop_cause(error: str) -> str:
    """Best-effort ``cause`` classification of *error* — never raises,
    never re-dispatches. See :data:`_CAUSE_PATTERNS` for the recognized
    vocabulary and match order. Returns ``""`` for an empty/blank
    *error* (nothing to classify), else one of the named causes, or
    ``"other"`` when *error* is non-empty but matches none of them.
    """
    text = str(error or "").strip()
    if not text:
        return ""
    for cause, pattern in _CAUSE_PATTERNS:
        if pattern.search(text):
            return cause
    return "other"


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
    #: The most common ``cause`` among IN-WINDOW drops (nexus-gjv9b review
    #: fold-in round 3, critique CRITICAL 2), and its count within that
    #: window. Empty/0 when no in-window drop carries a ``cause`` at all
    #: (historical records, or a producer that never classified its
    #: failure). Ties break on whichever cause was seen most RECENTLY —
    #: the same "what matters right now" framing as ``recent_last_hook``.
    #: Exists because ``recent_total`` alone collapses "one connection
    #: blip, resolved" and "the same auth failure on every single hook
    #: fire, still broken" into the identical number: a recurring
    #: STRUCTURAL cause (e.g. ``401``) must read differently from a
    #: transient one (``connect``/``timeout``) even though both decay on
    #: the same 24h clock.
    recent_dominant_cause: str = ""
    recent_dominant_cause_count: int = 0
    #: True iff ``recent_total > 0`` and EVERY in-window drop classified
    #: as ``"guard_refused"`` (code-review item 1: "a window of only
    #: guard refusals is not a WARN"). A guard refusal is an un-opted-in
    #: dev-checkout process correctly protecting itself (nexus-a2qhz) —
    #: every SessionEnd on such a checkout hits it by design, so a window
    #: made ENTIRELY of these carries no evidence the engine or a live
    #: producer is actually failing. A window with even one OTHER cause
    #: mixed in stays a real WARN — this flag is deliberately all-or-
    #: nothing, not "mostly guard refusals".
    recent_all_guard_refused: bool = False


def record_drop(*, hook: str, collection: str, rows: int, error: str, cause: str = "") -> None:
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

    *cause* (nexus-gjv9b review fold-in round 3, critique CRITICAL 2 /
    code-review item 1) is an OPTIONAL short classifier — the recognized
    vocabulary is :data:`_CAUSE_PATTERNS`'s keys (``"guard_refused"``,
    ``"401"``, ``"403"``, ``"5xx"``, ``"timeout"``, ``"connect"``,
    ``"unresolvable"``, ``"other"``) — so :func:`count_drops` can report
    the DOMINANT failure mode within its recency window rather than a
    bare count that reads identically whether the underlying problem is
    a transient network blip, a persistently wrong credential, or (the
    code-review's own example) an un-opted-in dev checkout's guard
    correctly refusing — the LAST of which is not a service problem at
    all and must never be reported as "the engine is failing".

    When *cause* is left as the default ``""``, it is derived from
    *error* itself via :func:`classify_drop_cause` — the code-review's
    "the error field already carries the distinguishing text, classify
    from it" — so a producer never needs to duplicate this module's
    classification logic; passing an explicit *cause* only makes sense
    for a caller (like the routing hook's own stdlib ``urllib`` layer)
    that already knows its failure mode precisely from the transport
    itself, more reliably than any text match could.
    """
    resolved_cause = cause if cause else classify_drop_cause(error)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hook": hook,
        "collection": collection,
        "rows": int(rows),
        "error": str(error)[:200],
        "cause": str(resolved_cause)[:32],
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
    # cause -> (count, most-recent epoch seen for that cause) — the tiebreak
    # for recent_dominant_cause mirrors recent_last_hook's own "most recent
    # wins" framing rather than dict/Counter insertion order, which would
    # otherwise silently favor whichever cause the log happened to record
    # first.
    cause_counts: Counter[str] = Counter()
    cause_last_epoch: dict[str, float] = {}
    # Every in-window drop NOT classified as "guard_refused" -- an
    # unclassified ("") cause counts here too, deliberately: only a drop
    # AFFIRMATIVELY classified as guard_refused may excuse the window,
    # never one we simply have no opinion about.
    recent_non_guard_refused = 0

    def _finish() -> DropSummary:
        dominant_cause = ""
        dominant_count = 0
        if cause_counts:
            dominant_cause, dominant_count = max(
                cause_counts.items(),
                key=lambda kv: (kv[1], cause_last_epoch.get(kv[0], -1.0)),
            )
        return DropSummary(
            total=total, rows=rows, last_ts=last_ts,
            last_collection=last_collection, last_hook=last_hook,
            recent_total=recent_total, recent_last_hook=recent_last_hook,
            recent_dominant_cause=dominant_cause,
            recent_dominant_cause_count=dominant_count,
            recent_all_guard_refused=(recent_total > 0 and recent_non_guard_refused == 0),
        )

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
                    cause = str(rec.get("cause", "") or "")
                    if cause:
                        cause_counts[cause] += 1
                        cause_last_epoch[cause] = max(cause_last_epoch.get(cause, -1.0), epoch)
                    if cause != "guard_refused":
                        recent_non_guard_refused += 1
    except OSError:
        return _finish()

    return _finish()
