# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``nx tuple watch`` loop (bead nexus-6konb.2, MM-1.1).

A ping-then-pull watcher for RDR-205 mailboxes, written to be the source of a
Claude Code Monitor: every stdout line it emits becomes one notification that
wakes the watching session, so the contract is about what is *not* printed as
much as what is.

- Probes ``mailbox/<address>`` with a zero-timeout ``rd`` (no park slot held),
  fetching many rows per probe and filtering on ``claim_state``: a dead-lettered
  row stays readable for the whole retention window while never being claimable,
  and at ``n=1`` one such row at the head would hide every newer message.
- **Beyond ``probe_n`` (nexus-qw386):** a plain ``since=None`` probe returns only
  the OLDEST ``probe_n`` live-or-dead rows on the address (the engine orders by
  ``(created_at, id)`` ascending and caps at ``probe_n``); once an address holds
  more than that, the newest mail sits past the cap and a probe that never moves
  its window would never reach it. When a probe returns a FULL page (``==
  probe_n``, the standard "there may be more" signal), the address permanently
  switches to a persisted per-address cursor (:func:`state_path`'s JSON file) and
  every following probe passes ``since=<cursor>``, walking forward instead of
  re-reading the same head every cycle. The cursor only advances to a row old
  enough that no concurrent writer still assigned an earlier timestamp could
  land behind it undetected (the engine stamps ``created_at`` with the JVM clock
  at insert time, before commit, so two concurrent ``out`` calls to the same
  address can commit out of the order their timestamps would suggest); rows
  newer than that safety margin are left un-cursored and simply reappear on the
  next probe, which is free because of the seen-set dedup below. Once triggered,
  cursor mode never reverts to a bare head scan: the backlog that caused it is
  still there, so reverting would re-truncate on the very next cycle. This
  trades the FOREVER re-emit healing of the old, pre-cap rows the cursor walks
  past (they are still pinged/reported exactly once as the cursor reaches them)
  for guaranteed eventual visibility of the tail -- a strict improvement over
  never seeing it at all -- and costs no more than today's steady-state probe:
  the extra load is one page's worth of catch-up while paging past the backlog,
  never a full re-walk from the start on every cycle.
- Emits only on a hit. An empty probe prints nothing.
- Emits one ping line per newly seen live tuple, capped per cycle (a burst
  beyond the cap is one coalesced line, and every burst row still counts as
  pinged because the drain is address-wide), and capped again by a rolling
  budget of ``budget_lines`` stdout lines per ``budget_window_s`` across
  cycles and addresses: a batch the budget cannot afford collapses to one
  coalesced line, so a sustained flood costs one line per cycle, never more. A tuple is not re-pinged for
  ``reemit_after_s``; after that a row still present re-pings, which is what
  heals a dropped notification. ``max_emits`` per tuple, then silent and
  counted, so a row nobody drains cannot burn the Monitor's auto-stop budget.
- Watches every address given to it in ONE process, probing each once per cycle
  and sharing one emit budget across them (:func:`resolve_watch_addresses`
  decides the list once at startup). A session has two mailboxes -- its session
  id and its instance name -- and a second Monitor for the second address would
  double the ping rate against a throttle that is counted per monitor.
- Preflights before the loop (:func:`preflight`): one bounded registry call
  plus one census per address. A below-floor or unreachable engine 404s or
  refuses every call forever and is otherwise indistinguishable from an empty
  mailbox, so a failure here prints ONE named SKIP line and the loop is never
  entered. A dead backlog approaching ``probe_n`` warns: past the cap dead
  rows no longer hide fresh mail permanently (the cursor above pages past
  them), but they force the address into permanent cursor mode, add
  catch-up latency before new mail is seen, and drop the forever re-emit
  healing the address had while it fit inside one probe.
- Reports a probe failure on stdout, rate-limited to one line per
  ``error_report_every_s`` per address, with a changed error reported at once
  and a recovery line on stderr. Silence is not success: an engine that dies
  mid-run says so. The rate limit is what keeps a sustained outage under the
  measured auto-stop budget.
  A dead-lettered row the watcher NEVER saw alive goes to stdout and is healed by
  the same re-emit window as a live row: it is mail that will never be delivered
  and the session has heard nothing about it, which is the strongest form of the
  thing this stream rule protects. A row that was pinged while alive and later
  died stays on stderr, because its death is a status update on a message the
  session already knows about. Whether the Monitor merges stderr is still
  unmeasured, and deliberately does not matter: every line saying the watcher is
  not delivering something is on stdout.
- Holds one flock per watched address (:func:`acquire_watch_locks`), scoped
  machine-wide by ADDRESS, not by session: a ``/clear`` changes the session id,
  so a per-session lock would miss the double-arm it exists to catch. A second
  watcher prints one line naming the holder and exits. A dead holder's lock is
  released by the OS, so a stale file is acquired rather than refused, with no
  pid-liveness heuristic to get wrong.
- Never claims, never acks. The ping carries address, sender, kind,
  correlation id and tuple id, never the body; the id is for correlation and
  dedup only, because the mailbox template pins only ``to`` and a claim is
  address-wide.
- Seen-set state lives in one JSON file per address under
  ``<state_dir>/tuple-watch/``; losing it re-pings, never loses a message.

Measured budget (T2 nexus/mm-0.1-monitor-auto-stop-threshold-measured-2026-09-12):
the harness throttles at about 20 events in 20 s, delivers at most about one
event per 2 s under throttle, and kills the monitor after about 30 s of
sustained suppression. Suppressed events are lost.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

PING_PREFIX = "nx-tuple-watch:"
_STATE_SUBDIR = "tuple-watch"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class WatchConfig:
    interval_s: float = 3.0
    reemit_after_s: float = 600.0
    max_emits: int = 3
    probe_n: int = 300
    max_lines_per_cycle: int = 5
    budget_window_s: float = 20.0
    budget_lines: int = 8
    error_report_every_s: float = 300.0
    dead_backlog_warn_ratio: float = 0.8
    # nexus-qw386: once a probe returns a full page, the cursor this module pages
    # forward with never advances past (probe time - this margin). created_at is
    # stamped with the JVM clock at insert time, before commit (TupleRepository),
    # so two concurrent `out` calls to the same address can commit out of the
    # order their timestamps suggest; a margin comfortably larger than a single
    # HTTP+DB round trip (single-digit milliseconds, measured elsewhere in this
    # repo's own gates) gives a slower concurrent commit time to land before the
    # cursor is allowed past it, at the cost of re-probing a handful of the most
    # recent rows each cycle -- negligible next to re-walking a large backlog.
    cursor_safety_lag_s: float = 10.0


@dataclass
class WatchStats:
    cycles: int = 0
    pinged: int = 0
    coalesced: int = 0
    suppressed: int = 0
    dead_seen: int = 0
    probe_errors: int = 0
    budget_coalesced: int = 0


@dataclass
class _Seen:
    """Per-tuple emission record: when it was last pinged and how many times."""

    last_emit: float
    count: int

    def to_json(self) -> dict[str, Any]:
        return {"last_emit": self.last_emit, "count": self.count}


@dataclass
class _AddressState:
    seen: dict[str, _Seen] = field(default_factory=dict)
    dead_reported: set[str] = field(default_factory=set)
    # nexus-qw386: None means "scan the whole address from the start" (today's
    # behaviour, unchanged for any address that fits in one probe). Set once a
    # probe returns a full page and never cleared after that -- see the module
    # docstring's "Beyond probe_n" section for why reverting to None would just
    # re-truncate on the next cycle.
    cursor: tuple[str, str] | None = None


def _unique_addresses(addresses: Iterable[str]) -> list[str]:
    """First-occurrence-order dedup, shared by every address-taking entry point.

    A repeated address must collapse to one everywhere: ``acquire_watch_locks`` would
    otherwise take its own lock and then refuse itself on the second pass, and the
    probe loop would emit two pings for one row. Four independent copies of this
    one-liner across three beads was three chances for one of them to drift.
    """
    return [a for a in dict.fromkeys(addresses) if a]


def state_path(state_dir: Path, address: str) -> Path:
    return state_dir / _STATE_SUBDIR / (_SAFE_NAME.sub("_", address) + ".json")


def _load_state(path: Path) -> _AddressState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        seen = {
            tid: _Seen(float(v["last_emit"]), int(v["count"]))
            for tid, v in dict(raw.get("seen", {})).items()
        }
        cursor_raw = raw.get("cursor")
        cursor = (str(cursor_raw[0]), str(cursor_raw[1])) if cursor_raw else None
        return _AddressState(
            seen=seen, dead_reported=set(raw.get("dead_reported", [])), cursor=cursor,
        )
    except FileNotFoundError:
        return _AddressState()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        _log.warning("tuple_watch_state_unreadable", path=str(path), error=str(e))
        return _AddressState()


def _save_state(path: Path, st: _AddressState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen": {tid: s.to_json() for tid, s in st.seen.items()},
        "dead_reported": sorted(st.dead_reported),
        "cursor": list(st.cursor) if st.cursor else None,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _parse_epoch(created_at: str) -> float | None:
    """*created_at* (ISO-8601, as the engine renders it) to epoch seconds, or
    ``None`` on anything unparseable -- a row this module cannot date is left
    out of the cursor-advancement decision rather than guessed at."""
    try:
        return datetime.fromisoformat(created_at).timestamp()
    except (TypeError, ValueError):
        return None


def ping_line(address: str, row: Any) -> str:
    dims = row.dims or {}
    return (
        f"{PING_PREFIX} new mail at mailbox/{address}"
        f" from={dims.get('from', '?')} kind={dims.get('kind', '-')}"
        f" correlation_id={dims.get('correlation_id', '-')} tuple_id={row.id}."
        f" Drain it now with the mailbox drain (claiming is address-wide, the tuple id"
        f" is for correlation only): nx tuple in mailbox/{address} --pattern to={address}"
        f" --claimant <your-id> --lease-s 60"
    )


def _coalesced_line(address: str, extra: int) -> str:
    return (
        f"{PING_PREFIX} {extra} more new mail at mailbox/{address} not listed;"
        f" one address-wide drain collects all of it."
    )


def _budget_line(address: str, count: int) -> str:
    return (
        f"{PING_PREFIX} {count} new mail at mailbox/{address} (ping budget reached, not"
        f" listed); one address-wide drain collects all of it."
    )


class _Emitter:
    """stdout gate: one Monitor notification per line, bounded by a rolling budget."""

    def __init__(self, config: WatchConfig, emit: Callable[[str], None]) -> None:
        self._config = config
        self._emit = emit
        self._recent: deque[float] = deque()

    def _afford(self, t: float, lines: int) -> bool:
        window_start = t - self._config.budget_window_s
        while self._recent and self._recent[0] < window_start:
            self._recent.popleft()
        return len(self._recent) + lines <= self._config.budget_lines

    def emit_error(self, line: str, t: float) -> None:
        """An outage line. It is already rate-limited by the caller's window, so it
        is never withheld by the ping budget -- silence is the failure mode this
        line exists to prevent -- but it does count toward it."""
        self._emit(line)
        self._recent.append(t)

    def emit_batch(self, address: str, rows: list[Any], t: float, stats: WatchStats) -> None:
        cap = self._config.max_lines_per_cycle
        head, tail = rows[:cap], rows[cap:]
        wanted = len(head) + (1 if tail else 0)
        if not self._afford(t, wanted):
            # The single coalesced line is always affordable: a flood costs one
            # line per cycle, and the drain is address-wide anyway.
            self._emit(_budget_line(address, len(rows)))
            self._recent.append(t)
            stats.budget_coalesced += len(rows)
            return
        for row in head:
            self._emit(ping_line(address, row))
            self._recent.append(t)
        if tail:
            self._emit(_coalesced_line(address, len(tail)))
            self._recent.append(t)
            stats.coalesced += len(tail)


@dataclass(frozen=True)
class ResolvedAddresses:
    """What one watcher process should watch, decided once at startup."""

    addresses: list[str]
    notices: list[str] = field(default_factory=list)
    error: str = ""


def resolve_watch_addresses(
    explicit: Iterable[str],
    *,
    instance: str = "",
    session_id: str | None = None,
) -> ResolvedAddresses:
    """Decide the address list for one watcher, ONCE, before the loop.

    A session has two mailboxes and they are reached differently. The SESSION ID
    is in this process's own environment, so a freshly spawned watcher resolves
    it correctly (``CLAUDE_CODE_SESSION_ID`` is right at spawn; it only goes
    stale in a long-lived process, which this is not). The INSTANCE NAME -- the
    ``ListAgents`` row, e.g. ``nexus-19`` -- exists in no environment variable
    anywhere, so it can only arrive as a literal at arm time.

    One process watches both. Two Monitors would not: the auto-stop budget is
    per monitor and a second one doubles the ping rate against one throttle.

    Explicit positional addresses win outright and suppress every default, which
    is what the SessionStart arming (MM-3.1) passes and what leaves the existing
    literal-address callers unchanged. Otherwise the session id is watched, the
    instance is added when given, and its ABSENCE is said out loud rather than
    silently halving the watch. Nothing to watch at all is a SKIP, not a warning.
    """
    explicit_list = _unique_addresses(explicit)
    if explicit_list:
        return ResolvedAddresses(addresses=explicit_list)

    addresses: list[str] = []
    notices: list[str] = []
    if session_id:
        addresses.append(session_id)
    if instance:
        addresses.append(instance)
    addresses = _unique_addresses(addresses)

    if not addresses:
        return ResolvedAddresses(
            addresses=[],
            error=(
                f"{PING_PREFIX} SKIP: no mailbox to watch -- no address was given, no"
                f" session id resolved, and --instance was not set, so nothing is being"
                f" watched. Pass an address, or --instance <name>."
            ),
        )
    if not instance:
        notices.append(
            f"{PING_PREFIX} WARNING: watching only the session-id mailbox. The"
            f" instance-name mailbox is NOT watched, and mail sent to this instance by"
            f" name will not be pinged, because --instance was not given (the instance"
            f" name is in no environment variable; it has to be passed at arm time).",
        )
    if not session_id:
        notices.append(
            f"{PING_PREFIX} WARNING: watching only the instance-name mailbox. No session"
            f" id resolved, so mail addressed to this session's id will not be pinged.",
        )
    return ResolvedAddresses(addresses=addresses, notices=notices)


def _error_note(e: BaseException) -> str:
    """A short class note for an outage line, or "" when there is nothing to add."""
    from nexus.db.t2.http_tuple_store import (  # noqa: PLC0415 — deferred: CLI startup cost
        ClaimOwnershipError,
        ParkCapExceededError,
        UnknownSubspaceError,
    )

    if isinstance(e, ParkCapExceededError):
        # B2: this watcher probes with timeout_s=0 and takes no park slot, so a park
        # cap here is never this loop's own doing -- say so rather than swallowing it.
        return " -- park cap, but this watcher never parks: something else holds the slots"
    if isinstance(e, UnknownSubspaceError):
        return " -- the subspace no longer resolves to a template"
    if isinstance(e, ClaimOwnershipError):
        return " -- claim ownership error on a read-only watcher"
    text = f"{type(e).__name__}: {e}".lower()
    if "401" in text or "403" in text or "auth" in text or "token" in text:
        return " -- looks like an auth failure, not a blip"
    return ""


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    detail: str = ""


def preflight(
    store: Any,
    addresses: Iterable[str],
    *,
    config: WatchConfig,
    emit: Callable[[str], None],
) -> PreflightResult:
    """One bounded probe of the engine before the loop starts.

    An engine below the floor that first served ``/v1/tuples`` 404s every call
    forever, and an unreachable one refuses every call forever; either way the
    loop would print nothing, which reads exactly like an empty mailbox. So this
    runs first and, on failure, prints ONE line beginning ``SKIP`` on STDOUT --
    the stream the Monitor watches, because a skip the session cannot see is the
    silent no-op this guard exists to prevent -- and the caller exits.
    """
    try:
        store.registry()
    except Exception as e:  # noqa: BLE001 — the whole point is to classify, not propagate
        detail = f"{type(e).__name__}: {e}"
        _log.warning("tuple_watch_preflight_failed", error=detail)
        emit(
            f"{PING_PREFIX} SKIP: the tuple space is not answering, so no mailbox is being"
            f" watched ({detail}){_error_note(e)}. Nothing will be delivered until this is fixed.",
        )
        return PreflightResult(ok=False, detail=detail)

    for address in _unique_addresses(addresses):
        subspace = f"mailbox/{address}"
        try:
            census = store.subspace_stats(subspace)
        except Exception as e:  # noqa: BLE001 — same classification, per address
            detail = f"{type(e).__name__}: {e}"
            _log.warning("tuple_watch_preflight_failed", address=address, error=detail)
            emit(
                f"{PING_PREFIX} SKIP: {subspace} is not readable, so it is not being watched"
                f" ({detail}){_error_note(e)}.",
            )
            return PreflightResult(ok=False, detail=detail)
        dead = getattr(census, "dead", 0) or 0
        if dead >= config.probe_n * config.dead_backlog_warn_ratio:
            # Past probe_n the read cap truncates and dead rows hide fresh mail again
            # -- the head-of-line class this watcher filters for, returning at scale.
            emit(
                f"{PING_PREFIX} WARNING: {subspace} holds {dead} dead-lettered rows against a"
                f" probe cap of {config.probe_n}; at the cap they hide fresh mail. Purge them.",
            )
    return PreflightResult(ok=True)


@dataclass
class WatchLocks:
    """Held flocks, one per watched address. ``release()`` is idempotent."""

    ok: bool
    holders: list[Any] = field(default_factory=list)
    refused_address: str = ""

    def release(self) -> None:
        from nexus._locking import unlock_file  # noqa: PLC0415 — deferred: CLI startup cost

        while self.holders:
            handle = self.holders.pop()
            try:
                unlock_file(handle)
            except OSError:  # pragma: no cover — releasing a dying process's lock
                pass
            finally:
                handle.close()


def lock_path(state_dir: Path, address: str) -> Path:
    return state_dir / _STATE_SUBDIR / (_SAFE_NAME.sub("_", address) + ".lock")


def acquire_watch_locks(
    addresses: Iterable[str],
    *,
    state_dir: Path,
    emit: Callable[[str], None],
) -> WatchLocks:
    """Take one exclusive advisory lock per address, machine-wide.

    The scope is the ADDRESS, never the session: a ``/clear`` mints a new session
    id, so a per-session lock would admit exactly the second watcher it exists to
    refuse, and every ping would double. Because the lock is an ``flock``, a
    holder that dies has it released by the OS -- a stale file is acquired, not
    refused, with no pid-liveness guess. The body still carries pid, session id
    and start time so a LIVE holder can be named in the refusal.
    """
    from nexus._locking import lock_file  # noqa: PLC0415 — deferred: CLI startup cost
    from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deferred

    locks = WatchLocks(ok=True)
    session_id = resolve_active_session_id() or "unknown-session"
    for address in _unique_addresses(addresses):
        path = lock_path(state_dir, address)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        try:
            lock_file(handle, blocking=False)
        except (BlockingIOError, OSError):
            handle.seek(0)
            held = handle.read().strip() or "an unnamed process"
            handle.close()
            locks.ok = False
            locks.refused_address = address
            emit(
                f"{PING_PREFIX} SKIP: mailbox/{address} is already watched by {held}."
                f" This second watcher is exiting rather than doubling every ping.",
            )
            locks.release()
            return locks
        handle.seek(0)
        handle.truncate()
        handle.write(
            f"pid={os.getpid()} session={session_id} address={address}"
            f" started_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        )
        handle.flush()
        locks.holders.append(handle)
    return locks


def _probe_once(
    store: Any,
    address: str,
    st: _AddressState,
    *,
    config: WatchConfig,
    t: float,
    emitter: _Emitter,
    report: Callable[[str], None],
    stats: WatchStats,
) -> None:
    since = st.cursor
    rows = store.rd(f"mailbox/{address}", {"to": address}, n=config.probe_n, since=since, timeout_s=0)
    # A full page (== probe_n) is the standard pagination signal that there may be
    # more beyond it -- the engine caps `rd` at probe_n regardless of how many rows
    # actually exist, so this call alone cannot tell "exactly probe_n" from "more".
    # Only a `since=None` call that came back short is a genuine full-address view;
    # anything else (already in cursor mode, or a fresh full page) sees a WINDOW,
    # not the whole address, and must not prune or reset on that partial view.
    full_scan = since is None and len(rows) < config.probe_n
    boundary = t - config.cursor_safety_lag_s
    safe_cursor = since
    present: set[str] = set()
    new_rows: list[Any] = []
    for row in rows:
        # Rows arrive ordered ascending by (created_at, id) (engine guarantee), so
        # the first one too recent to be safe ends the advance for every row after
        # it too -- but that only matters once `full_scan` is False and this value
        # is actually used below.
        row_epoch = _parse_epoch(row.created_at)
        if row_epoch is not None and row_epoch <= boundary:
            safe_cursor = (row.created_at, row.id)
        if row.claim_state == "dead":
            if row.id not in st.dead_reported:
                st.dead_reported.add(row.id)
                stats.dead_seen += 1
                line = (
                    f"{PING_PREFIX} dead-lettered mail at mailbox/{address} tuple_id={row.id}"
                    f" (attempts={row.attempts}); it can never be claimed, so it will not be"
                    f" delivered."
                )
                if row.id in st.seen:
                    # Already pinged while it was alive: the session knows this message
                    # exists, so its death is a status update, not news of lost mail.
                    report(line)
                else:
                    # NEVER seen alive -- the watcher's first sight of it is already dead.
                    # This is undeliverable mail the session has heard nothing about, which
                    # is the strongest form of "the watcher is not delivering something",
                    # so it goes to the stream the Monitor reads. Recorded in `seen` as an
                    # emission so the re-emit window heals a lost notice exactly as it does
                    # for a live row, rather than this being the one report with no retry.
                    emitter.emit_error(line, t)
                    st.seen[row.id] = _Seen(last_emit=t, count=1)
            elif (seen := st.seen.get(row.id)) is not None and seen.count < config.max_emits \
                    and t - seen.last_emit >= config.reemit_after_s:
                # Heal a dropped dead-letter notice on the same window and cap as a live row.
                emitter.emit_error(
                    f"{PING_PREFIX} dead-lettered mail still at mailbox/{address}"
                    f" tuple_id={row.id}; it can never be claimed.",
                    t,
                )
                st.seen[row.id] = _Seen(last_emit=t, count=seen.count + 1)
            present.add(row.id)
            continue
        present.add(row.id)
        seen = st.seen.get(row.id)
        if seen is None:
            new_rows.append(row)
        elif seen.count >= config.max_emits:
            if t - seen.last_emit >= config.reemit_after_s:
                stats.suppressed += 1
                seen.last_emit = t  # count each missed window once, not every cycle
        elif t - seen.last_emit >= config.reemit_after_s:
            new_rows.append(row)

    if full_scan:
        # `present` is the WHOLE address here (nothing was truncated), so absence
        # really does mean consumed or expired -- prune exactly as before nexus-qw386.
        for tid in [tid for tid in st.seen if tid not in present]:
            del st.seen[tid]
        st.dead_reported.intersection_update(present)
    else:
        # `present` is only this page's window; a row outside it may simply not
        # have been reached yet, so pruning by absence here would misfire. Advance
        # the persisted cursor instead (never past `boundary`) so the NEXT probe
        # walks forward rather than re-reading this same window -- nexus-qw386's
        # fix for the address exceeding one probe's worth of rows.
        st.cursor = safe_cursor

    if new_rows:
        emitter.emit_batch(address, new_rows, t, stats)
        for row in new_rows:
            prev = st.seen.get(row.id)
            st.seen[row.id] = _Seen(last_emit=t, count=(prev.count + 1) if prev else 1)
        stats.pinged += len(new_rows)


def run_watch(
    store: Any,
    addresses: Iterable[str],
    *,
    config: WatchConfig,
    state_dir: Path,
    iterations: int = 0,
    emit: Callable[[str], None],
    report: Callable[[str], None],
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> WatchStats:
    """Probe every address once per ``config.interval_s``; ``iterations=0``
    runs until interrupted. State is reloaded from disk on every cycle so a
    deleted file re-pings and never crashes the loop."""
    addrs = _unique_addresses(addresses)
    if not addrs:
        raise ValueError("at least one address is required")
    stats = WatchStats()
    emitter = _Emitter(config, emit)
    failing: dict[str, tuple[str, float]] = {}  # address -> (error text, last reported at)
    while iterations <= 0 or stats.cycles < iterations:
        t = now()
        # Rotate which address goes first each cycle. This does NOT fix an observed
        # starvation: with the current constants one address can take at most
        # max_lines_per_cycle + 1 = 6 of the 8-line budget, so the second always has room
        # for a detailed ping when the window is clear, and when the window is saturated
        # both addresses take the one-line coalesced path equally. What rotation removes
        # is the latent dependence on that arithmetic -- raise max_lines_per_cycle or
        # lower budget_lines and a fixed order would let the head address eat the budget
        # every cycle, leaving the tail permanently coalesced. Cheap insurance, not a fix.
        offset = stats.cycles % len(addrs)
        for address in addrs[offset:] + addrs[:offset]:
            path = state_path(state_dir, address)
            try:
                st = _load_state(path)
                _probe_once(
                    store, address, st, config=config, t=t, emitter=emitter, report=report,
                    stats=stats,
                )
                _save_state(path, st)
            except Exception as e:  # noqa: BLE001 — a probe failure is reported, not fatal
                stats.probe_errors += 1
                text = f"{type(e).__name__}: {e}"
                _log.warning("tuple_watch_probe_failed", address=address, error=text)
                prior = failing.get(address)
                # A CHANGED error is news and reports at once (a blip becoming an auth
                # failure is a different problem). The comparison is on the rendered
                # text, not the exception type, deliberately: two failures of the same
                # type with different detail (a 502 then a 401, both HTTPStatusError)
                # are different problems and the second must not be swallowed.
                # The SAME error re-reports only once
                # per window, so a sustained outage costs one line per window rather
                # than one per cycle -- silence would hide the outage, a line per cycle
                # would trip the measured auto-stop.
                due = (
                    prior is None
                    or prior[0] != text
                    or t - prior[1] >= config.error_report_every_s
                )
                if due:
                    failing[address] = (text, t)
                    emitter.emit_error(
                        f"{PING_PREFIX} probe failed for mailbox/{address}: {text}"
                        f"{_error_note(e)}. No mail can be seen while this lasts;"
                        f" reported at most once per"
                        f" {int(config.error_report_every_s)}s.",
                        t,
                    )
                continue
            if address in failing:
                del failing[address]
                # Pairs with the outage line, which is on stdout and says "No mail can be
                # seen while this lasts". Splitting the two halves of one state transition
                # across streams would leave a session that cannot see stderr watching the
                # failure arrive and never learning it ended -- recovery by inference from
                # silence, which is the inference this module exists to make unnecessary.
                emitter.emit_error(f"{PING_PREFIX} probe recovered for mailbox/{address}", t)
        stats.cycles += 1
        if iterations <= 0 or stats.cycles < iterations:
            sleep(config.interval_s)
    return stats
