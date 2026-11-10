# SPDX-License-Identifier: AGPL-3.0-or-later
"""Say what heavy work the engine is doing, for any command that waits on it
(nexus-mjmjl, nexus-f3yqd.3).

Two sources share one shape (agreed with the engine side): the engine's
``/v1/status`` ``boot_work`` list once HTTP is bound, and, before it binds,
the supervisor's migration record (:mod:`nexus.daemon.migration_status`).
Each entry: ``id``, ``what``, ``started_at`` (ISO-8601 UTC), ``done``,
``total``, ``unit``, an optional ``step`` (``what``, ``done``, ``total``,
``unit``) for an inner level of progress, ``safe_to_stop`` and ``on_stop``.
The engine sends no durations: elapsed, rate and time left are derived here.

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


def _progress(obj: dict[str, Any], elapsed: float | None) -> str:
    done, total = _count(obj.get("done")), _count(obj.get("total"))
    unit = obj.get("unit") if isinstance(obj.get("unit"), str) else ""
    if done is None:
        return ""
    text = f"{done:,}" + (f" of {total:,}" if total else "") + (f" {unit}" if unit else "")
    if total and elapsed and elapsed > 0 and 0 < done < total:
        text += f", about {duration((total - done) * elapsed / done)} left"
    return text


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
            progress = _progress(entry, elapsed)
            if progress:
                head += f": {progress}"
            lines.append(head + ".")
            step = entry.get("step")
            if isinstance(step, dict) and step.get("what"):
                inner = _progress(step, None)
                lines.append(f"  now: {step['what']}" + (f" ({inner})" if inner else ""))
            on_stop = entry.get("on_stop")
            if entry.get("safe_to_stop") is True:
                lines.append(f"  It is safe to stop" + (f": {on_stop}." if on_stop else "."))
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


def notice(config_dir: Path, *, clock: Callable[[], float] = time.time) -> list[str]:
    """Lines for the local migration record; empty when nothing is running."""
    return render(local_entries(config_dir, clock=clock), now=clock())


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
                    self._echo("The engine finished that work.")
                return
            if self._last is not None and now - self._last < self._repeat:
                return
            self._last = now
            for line in render(entries, now=now):
                self._echo(line)
        except Exception:  # noqa: BLE001 — advisory output must never break the wait it reports on
            return
