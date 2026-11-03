# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A gateway 503 holds every client process that shares a config dir (nexus-v1929).

The gateway retry loops (``http_vector_client._request`` for T3 and
``RefreshableHttpStoreMixin._once_with_gateway_retry`` for T2/catalog) each
back off on their own process's clock. On 2026-10-31 nineteen concurrent
``nx upgrade --auto`` children each retried the managed edge's 503s, about
2,000 gateway retries in all, and kept its admission pool at zero permits:
the retry load grew with the process count.

This module puts the backoff on disk. A 503 writes a not-before time to
``<config dir>/gateway-hold.json``; every client honours it before its first
attempt and before each retry. The strike count is shared too: it counts
waves of 503s (all the 503s that land inside one hold are one strike), and
the hold grows with it. A success after the hold clears the count, so a lone
process whose 503s end in a success keeps its old 2/5/10 s schedule; only an
unbroken run of failed waves goes past 10 s.

The hold is scoped to one endpoint (scheme, host and port; nexus-cphvg): a 503
from a local engine does not hold clients of a managed URL, and the reverse. A
503 whose body carries an engine ``reason`` (``tenant_creation_busy``) is about
one tenant or request, not an overloaded edge, and records nothing.

Processes held on a hold do not all wake at its end (nexus-xe1et). Each one
that sleeps toward ``not_before`` takes the next send slot in the file
(``next_slot``, advanced by :data:`SLOT_S` per claimant), so after the hold
the sends leave at one per :data:`SLOT_S`, and the concurrency at the edge is
bounded by the request time over that spacing. The slots run for
:data:`SPREAD_WINDOW_S` past the hold's end; a waiter that finds them all taken
goes at a random moment inside that window, so the load past the slots spreads
evenly instead of piling on one instant.

A caller also has a budget, the longest it will wait (``max_wait``: its request
timeout). A waiter whose slot, or whose hold, ends past its budget takes no slot
(it would never use it) and goes at a random moment in the last
:data:`SPREAD_WINDOW_S` of the budget, so a crowd of budgeted callers does not
come back together at the budget's end.

Everything here is best effort. An unreadable, garbled or unwritable file
means no shared hold, never a failed request. The callers do the sleeping
through their own ``time.sleep``, so this module never blocks.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import structlog

from nexus.db.engine_reasons import (
    QUARANTINE_RESTORE_BUSY_REASON,
    TENANT_CREATION_BUSY_REASON,
    error_reason,
)

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")

#: The hold file's name under the nexus config dir.
HOLD_FILENAME: str = "gateway-hold.json"

#: No hold, however it was written, keeps a client waiting longer than this.
HOLD_CAP_S: float = 30.0

#: The most jitter added to a wait that another process's hold imposed.
MAX_JITTER_S: float = 2.0

#: The gap between two sends that were held on one hold (nexus-xe1et). The edge
#: sees at most one released send per slot, so with a request time T the
#: concurrency after a hold is about T / SLOT_S while the slots last.
SLOT_S: float = 0.25

#: How long after a hold's end the send slots run. The Nth waiter leaves N *
#: :data:`SLOT_S` after the hold, up to this window (120 waiters at 0.25 s);
#: the waiters past that leave at a random moment inside it. So no waiter is
#: held longer than the hold itself (at most :data:`HOLD_CAP_S`) plus this
#: window, and the load never collapses onto the end of the window.
SPREAD_WINDOW_S: float = 30.0

#: Typed engine reasons of a 503 that is about one tenant or one request, not
#: an overloaded edge: it records no hold. Any other reason (a future one, or a
#: proxy's own JSON body) is treated as the edge's.
ENGINE_BUSY_REASONS: frozenset[str] = frozenset({TENANT_CREATION_BUSY_REASON, QUARANTINE_RESTORE_BUSY_REASON})

#: The most processes a hold names. A hold in force is replaced by the next wave,
#: so the list holds the distinct processes that recorded into one wave; past this
#: the oldest drop off (the newest, including the writer itself, always stay).
MAX_HOLDERS: int = 8

#: A 503 more than this long after the previous one starts the strike count over.
STRIKE_WINDOW_S: float = 60.0

#: The shared hold after the Nth 503 in the strike window. The first three
#: match ``gateway_backoff._GATEWAY_RETRY_SLEEPS``, so a single process keeps
#: its schedule; later strikes come from other processes and lengthen it.
ESCALATION_S: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 30.0)


def _hold_path() -> Path:
    from nexus import config as _config  # noqa: PLC0415 — deferred: keeps this leaf module free of config's import graph

    return _config.nexus_config_dir() / HOLD_FILENAME


def _read(path: Path) -> dict[str, Any]:
    """The hold file's fields (numbers, ``pid`` as an integer, ``pids`` as a list of integers, and ``url`` as text), or ``{}`` when it is absent or unusable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("not_before", "strikes", "last_503", "next_slot"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
            out[key] = float(value)
    if isinstance(raw.get("url"), str):
        out["url"] = raw["url"]
    if isinstance(raw.get("pid"), int) and not isinstance(raw["pid"], bool):
        out["pid"] = raw["pid"]
    if isinstance(raw.get("pids"), list):
        out["pids"] = [p for p in raw["pids"] if isinstance(p, int) and not isinstance(p, bool)][-MAX_HOLDERS:]
    return out


def _same_endpoint(state: dict[str, Any], url: str) -> bool:
    """False only when both the file and the caller name an endpoint and they differ.

    A caller that cannot name its endpoint, or a file written before endpoints
    were recorded, is treated as the same one: honouring a hold wrongly costs a
    short wait, ignoring one wrongly puts the retry storm back."""
    recorded = _safe_url(state.get("url") or "")
    named = _safe_url(url)
    if not named or not recorded:
        return True
    return recorded == named


def reason_from_body(raw: bytes | str | None) -> str | None:
    """The engine ``reason`` a 503 body carries, or ``None`` (an edge's own 503 has none)."""
    if not raw:
        return None
    try:
        return error_reason(json.loads(raw))
    except (ValueError, TypeError):
        return None


def _safe_url(url: str) -> str:
    """Scheme, host and port only: the file must never carry credentials.
    ``""`` when *url* is empty or names no host."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            return ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    except (ValueError, TypeError, AttributeError):
        return ""


def _write(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gateway-hold.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        from nexus.daemon.replace_guard import replace_with_retry  # noqa: PLC0415 — deferred: only a write pays for it

        replace_with_retry(tmp, path)  # Windows: a concurrent reader blocks a bare os.replace
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _jitter(remaining: float, rng: random.Random | None) -> float:
    return (rng or random).uniform(0.0, min(MAX_JITTER_S, 0.5 * remaining))


def _sane(state: dict[str, Any], now: float) -> dict[str, Any]:
    """Drop a hold no live writer could have set: one past the cap is a clock
    step back or a hand edit, and honouring it would stall every request. A
    ``next_slot`` further past the hold than the slots run is dropped alone."""
    not_before = state.get("not_before", 0.0)
    if not_before > now + HOLD_CAP_S + MAX_JITTER_S:
        return {}
    if state.get("next_slot", 0.0) > not_before + SPREAD_WINDOW_S + SLOT_S + 1e-6:
        return {k: v for k, v in state.items() if k != "next_slot"}
    return state


def _holder_list(state: dict[str, Any]) -> list[int]:
    """The processes that recorded a 503 into the hold in the file, oldest first: ``pids``, plus ``pid`` (the last one; all an older writer records)."""
    holders = list(state.get("pids", ()))
    if "pid" in state and state["pid"] not in holders:
        holders.append(state["pid"])
    return holders


def _ours(state: dict[str, Any]) -> bool:
    """True when this process is the only one that wrote the hold in the file.

    A hold another process also recorded into is not ours alone: that process
    is waiting on it too, so a caller that ignores its own hold (``others_only``)
    must not ignore this one."""
    return set(_holder_list(state)) == {os.getpid()}


def _tail_draw(earliest: float, latest: float, rng: random.Random | None) -> float:
    """A random moment in the last :data:`SPREAD_WINDOW_S` of ``[earliest, latest]``.

    For a caller whose budget ends before its slot would: it sends inside its
    budget, spread, instead of every such caller sending at the budget's end."""
    return (rng or random).uniform(max(earliest, latest - SPREAD_WINDOW_S), latest)


def _place(
    not_before: float, next_slot: float, rng: random.Random | None, *, budget: tuple[float, float] | None = None,
) -> tuple[float, float | None]:
    """The moment to send and the file's next slot (``None``: leave it).

    The next free slot, while it falls inside :data:`SPREAD_WINDOW_S` of the
    hold's end and, for a caller with a *budget* (its ``(earliest, latest)``
    send time), inside the budget; past that a random moment (inside the
    window after the hold, or the tail of the budget), taking no slot, so the
    callers behind it still get the slots that fit."""
    wake = max(not_before, next_slot)
    limit = not_before + SPREAD_WINDOW_S
    if budget is not None:
        limit = min(limit, budget[1])
    if wake <= limit:
        return wake, wake + SLOT_S
    if budget is not None:
        return _tail_draw(budget[0], budget[1], rng), None
    return not_before + (rng or random).uniform(0.0, SPREAD_WINDOW_S), None


def _endpoint_of(url: str | Callable[[], str]) -> str:
    if isinstance(url, str):
        return url
    try:
        return url()
    except Exception:  # noqa: BLE001 — best effort: an endpoint that cannot be named is held like any other
        return ""


def wait_before_attempt(
    *,
    after: float | None = None,
    max_wait: float | None = None,
    now: float | None = None,
    rng: random.Random | None = None,
    url: str | Callable[[], str] = "",
    others_only: bool = False,
) -> float:
    """Seconds this process should wait before sending, per the shared hold.

    *after* is the wall-clock time this process has already slept to (its own
    retry sleep), so only the part of the hold past it is returned: a retry
    re-checks the file and waits longer only when another process extended
    the hold meanwhile. ``0.0`` when there is no hold or it has passed. A
    hold in force returns the wait to this process's send slot (see the module
    docstring), never more than *max_wait* (the caller's own request timeout),
    so a short-budget caller such as a hook is not held past the time it would
    give the request. A caller whose slot lies past *max_wait*, or whose hold
    outlasts it, takes no slot: it draws a random moment in the last
    :data:`SPREAD_WINDOW_S` of its budget (nexus-3zreb), so a crowd of such
    callers (every production caller passes *max_wait*) does not send together
    at the budget's end. *url* is the endpoint the caller is about to send to; a
    hold recorded for another endpoint is not its hold. It may be a function
    returning the endpoint, called only when a hold is in force and names one,
    so a caller that must resolve its endpoint pays for that only then.

    *others_only* ignores a hold this very process wrote: a caller with its
    own short backoff (the data-token mint) then waits on the hold of another
    process, not on the schedule its own 503 recorded for the others.

    Reading is lock-free. Taking a slot needs the file's lock, and happens only
    when a hold is in force that ends inside *max_wait*, and only a slot that
    fits inside it is taken; if the lock
    cannot be had, the wait is the remaining hold plus up to
    :data:`MAX_JITTER_S` of jitter.
    """
    now = time.time() if now is None else now
    try:
        state = _sane(_read(_hold_path()), now)
    except Exception:  # noqa: BLE001 — best effort: a config-dir resolution failure means no shared hold
        return 0.0
    start = max(now, after or now)
    remaining = min(state.get("not_before", 0.0) - start, HOLD_CAP_S)
    if remaining <= 0.0:
        return 0.0
    if others_only and _ours(state):
        return 0.0
    endpoint = _endpoint_of(url) if state.get("url") else ""
    if not _same_endpoint(state, endpoint):
        return 0.0
    budget = None if max_wait is None else (start, start + max(0.0, max_wait))
    if budget is not None and remaining >= max_wait:
        # The hold outlasts the caller's budget: it sends inside the budget whatever the slots say,
        # spread over its tail so a crowd of such callers does not send at one instant. No slot, no lock.
        return max(0.0, _tail_draw(budget[0], budget[1], rng) - start)

    def claim(path: Path) -> float:
        held = _sane(_read(path), now)
        not_before = held.get("not_before", 0.0)
        if not_before <= start or not _same_endpoint(held, endpoint) or (others_only and _ours(held)):
            return start  # the hold moved or ended between the read and the lock
        wake, next_slot = _place(not_before, held.get("next_slot", 0.0), rng, budget=budget)
        if next_slot is not None:
            _write(path, {**held, "next_slot": next_slot})
        return wake

    try:
        wait = _locked(claim) - start
    except Exception as exc:  # noqa: BLE001 — best effort: no slot means the old jittered wait
        _log.debug("gateway_hold_slot_failed", error=type(exc).__name__)
        wait = remaining + _jitter(remaining, rng)
        if budget is not None and start + wait > budget[1]:
            wait = _tail_draw(budget[0], budget[1], rng) - start
    return max(0.0, wait)


#: How long a 503 path waits for the hold file's lock before giving up on
#: the shared hold for that one record. A wedged holder must not stall it.
LOCK_WAIT_S: float = 0.5


def _locked(fn: Callable[[Path], _T]) -> _T:
    """Run *fn(path)* under the hold file's lock."""
    from nexus._locking import lock_fd, unlock_fd  # noqa: PLC0415 — deferred: only a 503 or a post-hold success pays for it

    path = _hold_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(HOLD_FILENAME + ".lock"), os.O_RDWR | os.O_CREAT)  # a lock sentinel, no content
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                lock_fd(fd, blocking=False)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        try:
            return fn(path)
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


def record_success(*, now: float | None = None, url: str = "") -> None:
    """A send succeeded: once the hold has passed, the strike count starts over.

    This is what keeps a lone process on its 2/5/10 s schedule: a run of 503s
    that ends in a success leaves no strikes behind for its next request.
    A success that arrives while a hold is still in force (a request another
    process's 503 did not cover) leaves the hold alone, and so does a success
    from another endpoint than the one the hold was recorded for.
    """
    now = time.time() if now is None else now
    try:
        path = _hold_path()
        if not path.exists():
            return

        def clear(p: Path) -> None:
            state = _sane(_read(p), now)
            if _same_endpoint(state, url) and state.get("not_before", 0.0) <= now:
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()

        _locked(clear)
    except Exception as exc:  # noqa: BLE001 — best effort: a stale strike count only lengthens a later hold
        _log.debug("gateway_hold_clear_failed", error=type(exc).__name__)


def record_503(
    delay: float,
    *,
    retry_after: float | None,
    url: str = "",
    now: float | None = None,
    rng: random.Random | None = None,
    reason: str | None = None,
    own_pace: bool = False,
) -> float:
    """Record a 503 in the shared hold and return how long to sleep before resending.

    *delay* is the caller's own scheduled sleep for this retry; *retry_after*
    is the response's parsed ``Retry-After`` (seconds), if any. The result is
    the longest of those, the hold for the shared strike count, and any hold
    another process already set, capped at :data:`HOLD_CAP_S`, then pushed to
    this process's send slot when other processes are already waiting for the
    same moment (see the module docstring).

    Strikes count waves, not responses. A 503 that arrives while a hold is in
    force belongs to the wave that set it, however many processes saw one, so
    nineteen clients failing together are one strike. A 503 after the hold
    has passed, within :data:`STRIKE_WINDOW_S` of the last one, is the next
    strike; :func:`record_success` clears the count.

    A *reason* in :data:`ENGINE_BUSY_REASONS` (the engine's typed reason from
    the 503 body, see :func:`reason_from_body`) means the 503 is about one
    tenant or request, not an overloaded edge: nothing is recorded and the
    caller's own delay (or the response's ``Retry-After``) is the sleep. A
    hold recorded for another endpoint than *url* is replaced by this one.

    *own_pace* is for a caller with a short backoff of its own (the data-token
    mint): the hold is recorded in full for the other processes, but the sleep
    is the caller's own unless another process's hold was already in force.

    A *delay* of zero with no *retry_after* returns ``0.0`` and writes
    nothing. Production schedules are never zero; this is how the test
    suites switch the gateway schedule off.
    """
    if delay <= 0.0 and not retry_after:
        return 0.0
    now = time.time() if now is None else now
    own = min(max(delay, retry_after or 0.0), HOLD_CAP_S)
    if reason in ENGINE_BUSY_REASONS:
        return own

    def update(path: Path) -> tuple[float, int, bool]:
        state = _sane(_read(path), now)
        if not _same_endpoint(state, url):
            state = {}
        not_before = state.get("not_before", 0.0)
        strikes = max(0, int(state.get("strikes", 0.0)))
        foreign = now < not_before and not _ours(state)
        if now < not_before:
            strikes = max(strikes, 1)  # same wave
        elif now - state.get("last_503", float("-inf")) <= STRIKE_WINDOW_S:
            strikes += 1
        else:
            strikes = 1
        holders = [] if now >= not_before else [p for p in _holder_list(state) if p != os.getpid()]
        holders = [*holders, os.getpid()][-MAX_HOLDERS:]
        escalated = ESCALATION_S[min(strikes, len(ESCALATION_S)) - 1]
        hold = min(max(own, escalated, not_before - now), HOLD_CAP_S)
        new_not_before = min(max(not_before, now + hold), now + HOLD_CAP_S)
        wake, next_slot = _place(new_not_before, state.get("next_slot", 0.0), rng)
        _write(path, {
            "not_before": new_not_before,
            "next_slot": state.get("next_slot", 0.0) if next_slot is None else next_slot,
            "strikes": strikes,
            "last_503": now,
            "url": _safe_url(url),
            "pid": os.getpid(),
            "pids": holders,
        })
        return wake - now, strikes, foreign

    try:
        sleep_s, strikes, foreign = _locked(update)
    except Exception as exc:  # noqa: BLE001 — best effort: an unwritable config dir falls back to this process's own schedule
        _log.debug("gateway_hold_record_failed", error=type(exc).__name__)
        return own
    if own_pace and not foreign:
        return own
    if sleep_s > own:
        _log.info("gateway_hold_shared", strikes=strikes, hold_s=sleep_s, own_s=own)
    return max(sleep_s, own)
