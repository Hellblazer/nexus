# SPDX-License-Identifier: AGPL-3.0-or-later
"""Size, rate and time left for a download (nexus-mjmjl).

The engine binary and the PostgreSQL bundle are tens of megabytes each and
were fetched with no word: on a slow link ``nx upgrade`` looked hung. A
:class:`DownloadMeter` reports on stderr, so ``--json`` output and the quiet
``--auto`` stdout stay as they were (under a hook stderr is the log):

* a terminal gets one line redrawn in place (a carriage return, no escape
  sequence):
  ``downloading nexus-service.txz: 41.2 MB of 92.0 MB (45%), 6.1 MB/s, about 8 s left``;
* anything else gets the size when the download starts, a line every
  :data:`PLAIN_EVERY_S`, and never a control character;
* both end with ``downloaded <name>: 92.0 MB in 15.1 s (6.1 MB/s)``.

Small files (checksums, signatures) are not reported. The clock is injected.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from typing import IO

#: Files smaller than this are fetched without a meter.
MIN_BYTES: int = 1_000_000
#: A terminal line is redrawn at most this often, in seconds.
TTY_EVERY_S: float = 0.5
#: Plain output gets a progress line this often, in seconds.
PLAIN_EVERY_S: float = 15.0



def _size(n: float) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{int(n)} B"


def _secs(s: float) -> str:
    if s < 60:
        return f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m} m {sec:02d} s"


class DownloadMeter:
    """Reports one download. Call :meth:`update` with the bytes written so
    far and :meth:`finish` once the body is complete."""

    def __init__(
        self,
        name: str,
        total: int | None,
        *,
        stream: IO[str] | None = None,
        tty: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.total = total if total and total > 0 else None
        self._stream = stream if stream is not None else sys.stderr
        if tty is None:
            try:
                tty = bool(self._stream.isatty())
            except Exception:  # noqa: BLE001 — an odd stream reads as no terminal
                tty = False
        self.tty = tty
        self._clock = clock
        self._start = clock()
        self._last = self._start
        self._done = 0
        self._drawn = 0  # length of the line a terminal redraw left on screen
        self._announced = False
        if self.total is not None:
            self._announce()

    def _announce(self) -> None:
        """Plain output: the first line names the file and its size. A download
        of unknown length announces itself once it passes :data:`MIN_BYTES`."""
        self._announced = True
        if not self.tty:
            size = f" ({_size(self.total)})" if self.total else ""
            self._write_line(f"downloading {self.name}{size}")

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:  # noqa: BLE001 — a progress line must never fail the download it reports on
            pass

    def _redraw(self, text: str) -> None:
        """Overwrite the current terminal line with *text*: a carriage return
        and spaces over any longer leftover, no escape sequence (a legacy
        Windows console prints those as text)."""
        self._write("\r" + text.ljust(self._drawn))
        self._drawn = len(text)

    def _write_line(self, text: str) -> None:
        if self._drawn:
            self._write("\r" + " " * self._drawn + "\r")
            self._drawn = 0
        self._write(text + "\n")

    def _rate(self, now: float) -> float:
        elapsed = now - self._start
        return self._done / elapsed if elapsed > 0 else 0.0

    def status(self, now: float | None = None) -> str:
        now = self._clock() if now is None else now
        rate = self._rate(now)
        parts = [_size(self._done)]
        if self.total:
            parts[0] += f" of {_size(self.total)} ({100 * self._done // self.total}%)"
        if rate > 0:
            parts.append(f"{_size(rate)}/s")
            if self.total and self._done < self.total:
                parts.append(f"about {_secs((self.total - self._done) / rate)} left")
        return f"downloading {self.name}: " + ", ".join(parts)

    def update(self, done: int) -> None:
        self._done = done
        if not self._announced:
            if done < MIN_BYTES:
                return
            self._announce()
        now = self._clock()
        every = TTY_EVERY_S if self.tty else PLAIN_EVERY_S
        if now - self._last < every:
            return
        self._last = now
        if self.tty:
            self._redraw(self.status(now))
        else:
            self._write_line(self.status(now))

    def finish(self) -> None:
        if not self._announced:
            return  # a small file of unknown length: nothing was said, nothing to close
        now = self._clock()
        elapsed = now - self._start
        rate = f" ({_size(self._rate(now))}/s)" if elapsed > 0 else ""
        self._write_line(f"downloaded {self.name}: {_size(self._done)} in {_secs(elapsed)}{rate}")

    def abandon(self) -> None:
        """The attempt failed: clear a redrawn line so the error starts clean."""
        if self._drawn:
            self._write("\r" + " " * self._drawn + "\r")
            self._drawn = 0


def meter_for(name: str, total: int | None) -> DownloadMeter | None:
    """A meter for a file worth reporting, else ``None``. A download of
    unknown length is reported (it may be large)."""
    if total is not None and total < MIN_BYTES:
        return None
    if sys.stderr is None:  # no console (a windowed Windows process, a closed fd 2)
        return None
    return DownloadMeter(name, total)


#: uv's lines for a package worth reporting: ``Downloading torch (75.3MiB)``
#: when it starts, `` Downloaded torch`` when it ends (printed piped, too).
_UV_START = re.compile(r"^\s*Downloading (\S+) \(([\d.]+)\s*(B|KiB|MiB|GiB)\)\s*$")
_UV_DONE = re.compile(r"^\s*Downloaded (\S+)\s*$")
_UV_SCALE = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
#: The build tally speaks this often, in seconds.
BUILD_EVERY_S: float = 15.0


class UvBuildTally:
    """A running tally over a uv build's stderr (nexus-mjmjl).

    uv names each large package as it starts and finishes downloading but
    prints no totals, rate or time left once its output is piped. Feed every
    stderr line to :meth:`feed`; call :meth:`tick` about once a second (a
    thread) and :meth:`finish` at the end. Every :data:`BUILD_EVERY_S` it writes
    one plain line: packages and megabytes done of those announced, the rate,
    the time left for the announced ones, and the time so far; while uv is
    downloading nothing it says the build is still running. Never raises."""

    def __init__(
        self, *, stream: IO[str] | None = None, clock: Callable[[], float] = time.monotonic,
        every_s: float = BUILD_EVERY_S,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._clock = clock
        self._every = every_s
        self._start = clock()
        self._last = self._start
        self._sizes: dict[str, int] = {}
        self._done: set[str] = set()
        import threading  # noqa: PLC0415 — stdlib

        self._lock = threading.Lock()

    def feed(self, line: str) -> None:
        try:
            if m := _UV_START.match(line):
                with self._lock:
                    self._sizes[m.group(1)] = int(float(m.group(2)) * _UV_SCALE[m.group(3)])
            elif m := _UV_DONE.match(line):
                with self._lock:
                    self._done.add(m.group(1))
        except Exception:  # noqa: BLE001 — a tally must never break the build it watches
            pass

    def status(self, now: float | None = None) -> str:
        now = self._clock() if now is None else now
        with self._lock:
            total = sum(self._sizes.values())
            done = sum(size for name, size in self._sizes.items() if name in self._done)
            n_all, n_done = len(self._sizes), len(self._done & self._sizes.keys())
        so_far = _secs(now - self._start)
        if not n_all:
            return f"build: still running, {so_far} so far"
        text = f"build: {n_done} of {n_all} large package(s) downloaded ({_size(done)} of {_size(total)})"
        elapsed = now - self._start
        if done and elapsed > 0:
            rate = done / elapsed
            text += f", {_size(rate)}/s"
            if done < total:
                text += f", about {_secs((total - done) / rate)} left"
        return f"{text}; {so_far} so far"

    def tick(self) -> None:
        now = self._clock()
        if now - self._last < self._every:
            return
        self._last = now
        self._write(self.status(now))

    def finish(self) -> None:
        with self._lock:
            n, total = len(self._done & self._sizes.keys()), sum(self._sizes.values())
        if n:
            self._write(f"build: downloaded {n} large package(s), {_size(total)}, in {_secs(self._clock() - self._start)}")

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001 — as above
            pass
