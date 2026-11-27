# SPDX-License-Identifier: AGPL-3.0-or-later
"""Say what heavy work the engine is doing, for any command that waits on it
(nexus-mjmjl, nexus-f3yqd.3).

Two sources share one shape (agreed with the engine side): the engine's
``/v1/status`` ``boot_work`` list once HTTP is bound, and, before it binds,
the supervisor's migration record (:mod:`nexus.daemon.migration_status`).
Each entry: ``id``, ``what``, ``started_at`` (ISO-8601 UTC), ``done``,
``total``, ``unit``, an optional ``step`` (``what``, ``done``, ``total``,
``unit``) for an inner level of progress, ``safe_to_stop`` and ``on_stop``.
The engine sends no durations: the elapsed time is derived here. A top-level
``total`` is null until the pass has planned its last build (then final), so
no time left is derived from it; a step whose ``done`` is null (a waiting
phase) shows its phase alone; ``safe_to_stop`` never renders bare, always with
``on_stop``; an empty list does not mean idle. ``step.what`` names no
collection: the engine answers ``/v1/status`` with no tenant check (the managed edge still asks for a token).

:func:`render` turns entries into lines; :class:`Announcer` prints them from
inside a wait loop, once when the work is first seen and again every
:data:`REPEAT_S` with fresh numbers. Stdlib only.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

#: A wait loop repeats the lines this often, in seconds.
REPEAT_S: float = 30.0


def duration(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{int(seconds)} s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} m {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} m"


def _started(entry: dict[str, Any]) -> float | None:
    raw = entry.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _count(n: Any) -> int | None:
    return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) else None


def _progress(obj: dict[str, Any], *, top: bool) -> str:
    """``done of total unit``; empty when ``done`` is null (a waiting phase).
    At the top level ``total`` is null until the pass has planned its last
    build (then it is final): show the build in progress, ``index <done+1>``.
    ``done`` counts every build that ended, failed ones included."""
    done, total = _count(obj.get("done")), _count(obj.get("total"))
    unit = obj.get("unit") if isinstance(obj.get("unit"), str) else ""
    if done is None:
        return ""
    if total is None:
        if not top:
            return f"{done:,}" + (f" {unit}" if unit else "")
        noun = unit[:-2] if unit.endswith("es") else unit[:-1] if unit.endswith("s") else unit
        return f"{noun or 'item'} {done + 1:,}"
    return f"{done:,} of {total:,}" + (f" {unit}" if unit else "")


#: The engine's entry id for "the engine needs one restart" (the engine's RESTART_ENTRY_ID). Nothing runs: ``what``
#: is an instruction ("restart the engine once: it applies ..."), not a gerund.
RESTART_ID: str = "rdr229-restart"
#: The engine's entry id for a standing condition (the engine's HELD_BOOT_WORK_ID, also
#: ``pg_bundle_refresh.STANDING_CONDITION_ID``): ``what`` is the operator's reasons, joined with "; ", and nothing runs.
HELD_ID: str = "id-unique-index-held"
_RESTART_LEAD = "restart the engine once"

#: A data move's ``what`` opens with its class (the engine's MoveBootWork); the worker and reporter entries open with
#: ``blocked:``. Each maps to the head's lead-in and a kind: ``running`` (the engine is working on it, so "running for"
#: and the interrupt advice are true), ``pending`` (the engine will work on it, nothing runs yet: "listed for", the interrupt
#: advice is still true of the engine's coming work) or ``idle`` (nothing runs for it until something outside changes: no
#: interrupt advice, only what stopping the service costs).
_CLASS_LEAD: dict[str, tuple[str, str]] = {
    "working": ("The engine is working: ", "running"),
    "stalled": ("The engine is stalled: ", "running"),
    "blocked": ("The engine is blocked: ", "idle"),
    "paused": ("The engine is paused: ", "idle"),
    "queued": ("The engine has a data move queued: ", "pending"),
    "failed": ("The engine has a failed data move: ", "idle"),
    "runs complete": ("The engine has a data move whose runs are complete: ", "pending"),
}
_CLASS_RE = re.compile(r"^(" + "|".join(_CLASS_LEAD) + r"): ?(.*)$", re.S)


def _head(entry: dict[str, Any], what: str, elapsed: float | None) -> tuple[str, str]:
    """The first line's text without its closing progress, and the entry's kind (see ``_CLASS_LEAD``; a restart request
    or a held condition is ``standing``: nothing is running, there is no ``done of total``, and the engine's own sentence
    says what it costs). Every ``what`` the engine publishes reads as a sentence here, whatever its grammar: a gerund
    ("building search indexes") follows "The engine is", an instruction ("restart the engine once: ...") and a reasons
    list (the held entry) get their own lead-in, a data move's class ("failed: label") its own."""
    eid = entry.get("id")
    if what.startswith("The engine"):
        lead, kind, verb = "", "running", "running"
    elif eid == RESTART_ID or what.startswith(_RESTART_LEAD):
        what = what.split(": ", 1)[1] if ": " in what else ""
        lead, kind, verb = "The engine needs one restart" + (": " if what else ""), "standing", "waiting"
    elif eid == HELD_ID:
        lead, kind, verb = "The engine has a standing condition: ", "standing", "listed"
    elif m := _CLASS_RE.match(what):
        (lead, kind), what = _CLASS_LEAD[m.group(1)], m.group(2)
        verb = "running" if kind == "running" else "listed"
    else:
        lead, kind, verb = "The engine is ", "running", "running"
    head = lead + what
    if elapsed is not None:
        head += f", {verb} for {duration(elapsed)}"
    return head, kind


def render(entries: Iterable[dict[str, Any]], *, now: float, waiting: bool = True) -> list[str]:
    """Lines for the entries, never raising on a malformed one. *waiting* False is for a command that waits on
    nothing (``nx daemon service status``): it says what stopping the service costs, not what interrupting
    this command does."""
    lines: list[str] = []
    for entry in entries:
        try:
            what = str(entry.get("what") or entry.get("id") or "heavy work")
            started = _started(entry)
            elapsed = now - started if started is not None else None
            head, kind = _head(entry, what, elapsed)
            progress = "" if kind == "standing" else _progress(entry, top=True)
            if progress:
                head += f": {progress}"
            lines.append(head + ".")
            step = entry.get("step")
            if isinstance(step, dict) and step.get("what"):
                inner = _progress(step, top=False)
                lines.append(f"  now: {step['what']}" + (f" ({inner})" if inner else ""))
            on_stop = entry.get("on_stop")
            if isinstance(entry.get("notice"), str) and entry["notice"]:
                lines.append(f"  {entry['notice']}")
            elif kind == "standing":
                # Nothing is running, so neither "interrupting this command" nor "stopping the service" applies: the
                # engine's own sentence says what it costs (a restart request: nothing is lost).
                if on_stop:
                    lines.append(f"  {str(on_stop)[:1].upper()}{str(on_stop)[1:]}.")
            elif kind == "idle" or not waiting:
                # An idle entry (a blocked, paused or failed data move) is not work this command waits on and not work
                # interrupting it could stop: only what stopping the service costs is true of it.
                if on_stop:
                    lines.append(f"  Stopping the service now: {on_stop}.")
            elif entry.get("safe_to_stop") is True:
                # Never a bare "safe to stop" (engine review S3): say what stopping costs.
                lines.append(
                    "  Interrupting this command does not stop it."
                    + (f" Stopping the service now: {on_stop}." if on_stop else "")
                )
            else:
                lines.append(
                    "  Commands that need the engine wait until it finishes; interrupting this command does not "
                    "stop it." + (f" Stopping the service: {on_stop}." if on_stop else "")
                )
        except Exception:  # noqa: BLE001 — a malformed entry costs its own lines, never the caller
            continue
    return lines


def local_entries(config_dir: Path, *, clock: Callable[[], float] = time.time) -> list[dict[str, Any]]:
    """The supervisor's migration record, as a one-entry list (or empty)."""
    from nexus.daemon.migration_status import read  # noqa: PLC0415 — stdlib-only, deferred

    record = read(config_dir, clock=clock)
    return [record] if record else []


def _fetch_if_an_engine_is_there(*, timeout: float) -> dict[str, Any] | None:
    """``/v1/status``, asked only when there is an engine to ask: a managed service with a ``service_url``, or a
    local install whose supervisor has published a lease. A local install with no lease (stopped, never started) is not
    resolved at all, so a command that only wants to say what the engine is doing never trips the
    endpoint resolver's not-running error."""
    from nexus.config import get_credential, is_local_mode  # noqa: PLC0415 — deferred, not stdlib
    from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 — deferred, not stdlib
    from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 — deferred, not stdlib

    if is_local_mode():
        if discover_lease()[0] is None:
            return None
    elif not (get_credential("service_url") or "").strip():
        return None  # managed with no service_url: nothing to resolve, so the resolver's error is never raised
    return fetch_engine_status(timeout=timeout, mint_deadline_s=timeout)  # advisory: never a long mint


def engine_entries(*, fetch: Callable[..., dict[str, Any] | None] | None = None) -> list[dict[str, Any]]:
    """The engine's ``/v1/status`` ``boot_work`` list. Empty when the engine is idle, predates the field (no
    key), or cannot be reached: none of those is work to report. Never raises."""
    try:
        if fetch is None:
            fetch = _fetch_if_an_engine_is_there
        body = fetch(timeout=2.0)
    except Exception:  # noqa: BLE001 — advisory: an unreadable status is nothing to report
        return []
    work = body.get("boot_work") if isinstance(body, dict) else None
    return [entry for entry in work if isinstance(entry, dict)] if isinstance(work, list) else []


def engine_lines() -> list[str]:
    """Lines for the engine's own ``boot_work`` now: a step reporter's ``work`` source, so a long wait on a
    busy engine repeats its progress (nexus-f3yqd.3). Empty when idle or unreachable."""
    return render(engine_entries(), now=time.time())


def current_entries(
    config_dir: Path, *, clock: Callable[[], float] = time.time,
    fetch: Callable[..., dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """What the engine is doing: the supervisor's migration record while it has one (the engine has not bound
    HTTP yet), else the engine's own ``boot_work`` list."""
    local = local_entries(config_dir, clock=clock)
    return local if local else engine_entries(fetch=fetch)


def notice(
    config_dir: Path, *, clock: Callable[[], float] = time.time,
    fetch: Callable[..., dict[str, Any] | None] | None = None,
) -> list[str]:
    """Lines for the heavy work the engine is doing (nexus-f3yqd.3); empty when nothing is running."""
    return render(current_entries(config_dir, clock=clock, fetch=fetch), now=clock())


class Announcer:
    """For a wait loop: call :meth:`poll` on each iteration. It prints the
    lines when work is first seen and every :data:`REPEAT_S` after, and once
    more ("finished") when the work goes away. Never raises."""

    def __init__(
        self,
        echo: Callable[[str], None],
        source: Callable[[], list[dict[str, Any]]],
        *,
        clock: Callable[[], float] = time.time,
        repeat_s: float = REPEAT_S,
    ) -> None:
        self._echo = echo
        self._source = source
        self._clock = clock
        self._repeat = repeat_s
        self._last: float | None = None

    def poll(self) -> None:
        try:
            entries = self._source()
            now = self._clock()
            if not entries:
                if self._last is not None:
                    self._last = None
                    self._echo("The engine is no longer doing that work.")
                return
            if self._last is not None and now - self._last < self._repeat:
                return
            self._last = now
            for line in render(entries, now=now):
                self._echo(line)
        except Exception:  # noqa: BLE001 — advisory output must never break the wait it reports on
            return
