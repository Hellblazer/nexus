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
collection (``/v1/status`` is unauthenticated).

:func:`render` turns entries into lines; :class:`Announcer` prints them from
inside a wait loop, once when the work is first seen and again every
:data:`REPEAT_S` with fresh numbers. Stdlib only.
"""

from __future__ import annotations

import math
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


def render(entries: Iterable[dict[str, Any]], *, now: float) -> list[str]:
    """Lines for the entries, never raising on a malformed one."""
    lines: list[str] = []
    for entry in entries:
        try:
            what = str(entry.get("what") or entry.get("id") or "heavy work")
            started = _started(entry)
            elapsed = now - started if started is not None else None
            head = f"The engine is {what}" if not what.startswith("The engine") else what
            if elapsed is not None:
                head += f", running for {duration(elapsed)}"
            progress = _progress(entry, top=True)
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
    """``/v1/status``, asked only when there is an engine to ask: a managed service, or a local install whose
    supervisor has published a lease. A local install with no lease (stopped, never started) is not
    resolved at all, so a command that only wants to say what the engine is doing never trips the
    endpoint resolver's not-running error."""
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred, not stdlib
    from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 — deferred, not stdlib
    from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 — deferred, not stdlib

    if is_local_mode() and discover_lease()[0] is None:
        return None
    return fetch_engine_status(timeout=timeout)


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
