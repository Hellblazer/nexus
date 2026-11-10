# SPDX-License-Identifier: AGPL-3.0-or-later
"""One step reporter for the long install verbs (nexus-mjmjl).

``nx upgrade`` and ``nx self install`` used to go quiet for minutes between
"installed gen-<stamp>" and their last line: engine convergence, the ladder's
data steps, the plugin fetch and the uv cache prune printed nothing while
they ran. Every such verb now walks its work through a :class:`StepReporter`:

* a line before each step: ``Step N of M: <what> (<detail>)``;
* while a step runs, progress (``done of total``) when the step reports it,
  else a heartbeat with the elapsed time; a terminal gets one line redrawn in
  place, anything else (a hook, a log, a pipe) gets plain lines at a slower
  cadence and never a control character;
* ``done in <duration>``, or ``failed after <duration>`` with the remedy;
* a summary of every step's duration.

Quiet mode (``nx upgrade --auto``) writes nothing to stdout and the same lines
to the log. The clock and the heartbeat ticker are injected, so tests drive
time by hand and never sleep.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import IO

import structlog

_log = structlog.get_logger(__name__)

#: A terminal is redrawn this often while a step runs, in seconds.
TTY_HEARTBEAT_S: float = 5.0
#: Plain output (no terminal) gets a heartbeat line this often, in seconds.
PLAIN_HEARTBEAT_S: float = 30.0
#: A lazy step announces itself once it has run this long, in seconds.
LAZY_ANNOUNCE_S: float = 1.0
#: The ticker thread wakes this often to ask whether a heartbeat is due.
TICK_S: float = 1.0

_CLEAR_LINE = "\r\x1b[K"
#: ``scheme://user:secret@`` in an error line.
_URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")


def format_duration(seconds: float) -> str:
    """``0.4 s``, ``12.0 s``, ``1 m 05 s``, ``1 h 02 m``."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    whole = int(round(seconds))
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes} m {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} m"


def format_bytes(n: float) -> str:
    """``512 B``, ``3.4 MB``, ``1.2 GB`` (decimal units, as download sizes are quoted)."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{int(n)} B"


def run_ticker(wait: Callable[[float], bool], tick: Callable[[], None], interval: float = TICK_S) -> None:
    """Call *tick* every *interval* until *wait* returns true (the stop event
    was set). Separate from the thread so a test can drive it with a fake
    *wait*. A tick that raises (a closed stream) ends the heartbeat quietly."""
    while not wait(interval):
        try:
            tick()
        except Exception:  # noqa: BLE001 — the heartbeat is advisory
            return


@dataclass
class StepRecord:
    """One finished step, for the summary."""

    number: int
    what: str
    seconds: float
    ok: bool


@dataclass
class Step:
    """A running step. Steps call :meth:`progress` when they know how far they
    are; the reporter's ticker calls :meth:`tick` for the heartbeat."""

    reporter: StepReporter
    number: int
    what: str
    started: float
    announce: str = ""
    lazy: bool = False
    #: Redrawn in place (compact, terminal, and the step prints nothing itself).
    inline: bool = False
    announced: bool = True
    #: Set by the caller when the step turned out to be nothing (a path that
    #: returned before any work): a lazy step that never announced leaves no trace.
    discard: bool = False
    #: Set by the caller when the step ended without doing what it set out to
    #: (e.g. no service came up): the end line says so instead of "done in".
    end_note: str | None = None
    _last_emit: float = 0.0
    _progress: tuple[float, float | None, str, str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def elapsed(self) -> float:
        return self.reporter.clock() - self.started

    def progress(self, done: float, total: float | None = None, unit: str = "", note: str = "") -> None:
        """Record how far the step is; shown at the next heartbeat. *unit*
        ``"bytes"`` formats as sizes, anything else is a count label. *note*
        is the caller's own words after the figure (a rate, a time left, what
        the total is an estimate of)."""
        with self._lock:
            self._progress = (done, total, unit, note)
        self.tick()

    def _progress_text(self) -> str:
        if self._progress is None:
            return ""
        done, total, unit, note = self._progress
        if unit == "bytes":
            text = format_bytes(done) + (f" of {format_bytes(total)}" if total else "")
        else:
            label = f" {unit}" if unit else ""
            text = f"{int(done)}" + (f" of {int(total)}" if total else "") + label
        return text + (f" {note}" if note else "") + ", "

    def tick(self) -> None:
        """Emit a heartbeat when one is due (a lazy step first announces
        itself once it has run :data:`LAZY_ANNOUNCE_S`)."""
        if not self.announced:
            if self.elapsed() < LAZY_ANNOUNCE_S:
                return
            with self._lock:
                if self.announced:
                    return
                self.announced = True
            self.reporter._announce(self)
            return
        with self._lock:
            now = self.reporter.clock()
            if now - self._last_emit < self.reporter.heartbeat_s:
                return
            self._last_emit = now
            elapsed = f"{self._progress_text()}{format_duration(now - self.started)} elapsed"
            text = f"{self.announce} ... {elapsed}" if self.inline else f"  ... {elapsed}"
        self.reporter._heartbeat(text, inline=self.inline)
        self.reporter._say_work()


class StepReporter:
    """Announces, times and summarises the steps of one command.

    *total* is the step count shown as ``of M``. *echo* writes one line to the
    user (``click.echo``); *stream* is where a terminal redraw goes and *tty*
    whether it is a terminal (both default to stdout). *quiet* sends every line
    to stderr only (*log*; the hook running ``--auto`` sends its stderr to a log file). *clock* and *ticker* are seams: tests pass a fake clock
    and a ticker that does nothing, and call :meth:`Step.tick` themselves.
    """

    def __init__(
        self,
        total: int,
        *,
        echo: Callable[[str], None] | None = None,
        stream: IO[str] | None = None,
        tty: bool | None = None,
        quiet: bool = False,
        clock: Callable[[], float] = time.monotonic,
        ticker: Callable[[Step], contextlib.AbstractContextManager[None]] | None = None,
        name: str = "nx",
        log: Callable[[str], None] | None = None,
        compact: bool = False,
        slow_s: float = 3.0,
        heartbeat_s: float | None = None,
        announce: str = "Step {number} of {total}: {what}{detail}",
        show_reason: bool = True,
        lazy: bool = False,
        work: Callable[[], list[str]] | None = None,
        work_every_s: float = 30.0,
    ) -> None:
        import sys  # noqa: PLC0415 — stdlib, here so a test's capsys sees the live stdout

        self.total = total
        self._stream = stream if stream is not None else sys.stdout
        if echo is None:
            import click  # noqa: PLC0415 — deferred, keeps the module import light

            echo = click.echo
        self._echo = echo
        if tty is None:
            try:
                tty = bool(self._stream.isatty())
            except (AttributeError, ValueError, OSError):
                tty = False
        self.tty = tty and not quiet
        self.quiet = quiet
        self.clock = clock
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else (
            TTY_HEARTBEAT_S if self.tty else PLAIN_HEARTBEAT_S
        )
        self._announce_fmt = announce
        self.compact = compact
        self.slow_s = slow_s
        #: Compact on a terminal: the step line itself is redrawn in place.
        self.inline_steps = compact and self.tty
        self._ticker = ticker if ticker is not None else _thread_ticker
        self._name = name
        # Quiet (--auto) lines go to stderr, which the hook that runs it sends to
        # its log file; the CLI's structlog floor is WARNING, so an INFO event
        # would be dropped (critique r1).
        self._log_line = log if log is not None else _stderr_line
        self.show_reason = show_reason
        self.lazy_default = lazy
        #: nexus-f3yqd.3: lines saying what the engine is busy with, printed at a heartbeat at most every
        #: *work_every_s*, so a long wait on a busy engine shows its progress and not only the elapsed time
        self._work = work
        self._work_every_s = work_every_s
        self._last_work: float | None = clock()  # the command's own start notice already said it
        self._count = 0
        self._started = clock()
        self._dirty = False
        self.records: list[StepRecord] = []

    # -- output ---------------------------------------------------------
    def line(self, text: str) -> None:
        """One finished line: to the user, or only to the log when quiet."""
        self._clear()
        _clear_meter()
        if self.quiet:
            self._log_line(text)
            return
        _log.debug("step_line", command=self._name, line=text)
        self._echo(text)

    def _say_work(self) -> None:
        """At a heartbeat, the engine's work lines, at most every ``work_every_s``. Never raises."""
        if self._work is None:
            return
        now = self.clock()
        if self._last_work is not None and now - self._last_work < self._work_every_s:
            return
        self._last_work = now
        try:
            lines = self._work()
        except Exception:  # noqa: BLE001 — advisory, as the heartbeat is
            return
        for text in lines:
            self.line(text)

    def _heartbeat(self, text: str, *, inline: bool = False) -> None:
        if self.quiet:
            self._log_line(text.strip())
            return
        _clear_meter()
        if inline:
            # In place only for a step that prints nothing of its own while it
            # runs (doctor's core checks); a step that prints (nx upgrade's,
            # doctor's supplementary checks) would glue its line onto an
            # unterminated redraw (reviews r1 and r2), so it gets whole lines.
            try:
                self._stream.write(_CLEAR_LINE + text)
                self._stream.flush()
            except Exception:  # noqa: BLE001 — a progress line must never fail the step
                return
            self._dirty = True
        else:
            self._echo(text)

    def _clear(self) -> None:
        if self._dirty:
            self._dirty = False
            try:
                self._stream.write(_CLEAR_LINE)
                self._stream.flush()
            except Exception:  # noqa: BLE001 — as above
                pass

    # -- steps ----------------------------------------------------------
    def _announce(self, step: Step) -> None:
        step.announced = True
        if step.inline:
            self._heartbeat(step.announce, inline=True)
        else:
            self.line(step.announce)

    @contextlib.contextmanager
    def step(
        self, what: str, detail: str | None = None, *, remedy: str | None = None, lazy: bool | None = None,
        prints: bool = False,
    ) -> Generator[Step]:
        """Run one step: announce it, heartbeat while it runs, report its end.

        An exception propagates after the failure line; *remedy*, when given,
        is printed under it. A step that *prints* its own lines is never
        redrawn in place. A *lazy* step announces itself only once it has
        run :data:`LAZY_ANNOUNCE_S` or finished: a refusal it raises before
        that (a ``click`` usage error or exit) propagates with no line, so a
        command's checks-before-work stay a one-line refusal."""
        self._count += 1
        number = self._count
        suffix = f" ({detail})" if detail else ""
        announce = self._announce_fmt.format(number=number, total=max(self.total, number), what=what, detail=suffix)
        lazy = self.lazy_default if lazy is None else lazy
        step = Step(
            self, number, what, self.clock(), announce, lazy=lazy, announced=False,
            inline=self.inline_steps and not prints,
        )
        step._last_emit = step.started
        if not lazy:
            step.announced = True
            self._announce(step)
        ok = False
        try:
            with self._ticker(step):
                yield step
            ok = True
        except BaseException as exc:
            if not step.announced and _is_refusal(exc):
                self._count -= 1
                raise
            if not step.announced:
                step.announced = True
                self._announce(step)
            seconds = step.elapsed()
            self.records.append(StepRecord(number, what, seconds, False))
            self.line(f"  failed after {format_duration(seconds)}{self._reason(exc)}")
            if remedy:
                self.line(f"  {remedy}")
            raise
        finally:
            if ok and step.discard:
                self._count -= 1
            elif ok:
                if not step.announced:
                    step.announced = True
                    self._announce(step)
                seconds = step.elapsed()
                self.records.append(StepRecord(number, what, seconds, True))
                if step.end_note:
                    self.line(f"  {step.end_note} after {format_duration(seconds)}")
                elif not self.compact:
                    self.line(f"  done in {format_duration(seconds)}")
                elif seconds >= self.slow_s:
                    # Compact (doctor's many checks): only a slow step gets a line of its own.
                    self.line(f"  {what}: done in {format_duration(seconds)}")
                elif step.inline:
                    self._clear()

    def _reason(self, exc: BaseException) -> str:
        """``: <first line>`` of the error, credentials redacted; nothing when
        the reporter shows no reasons, or when the command prints the error
        itself (a click refusal) or it is only an exit code."""
        if not self.show_reason or _is_refusal(exc):
            return ""
        text = str(exc).strip()
        first = text.splitlines()[0] if text else type(exc).__name__
        try:
            from nexus._hook_runtime.failure_ring import scrub  # noqa: PLC0415 — deferred, stdlib-only
            from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred

            # Both: redact_credentials for the key=value forms, the failure ring's
            # scrub for Authorization/Bearer headers, PEM blocks and bare provider
            # tokens (pa-..., sk-...), then URL userinfo (review r2).
            first = _URL_USERINFO.sub(r"\1[redacted]@", redact_credentials(scrub(first)))
        except Exception:  # noqa: BLE001 — no redactor: say nothing rather than leak
            return ""
        return f": {first}"

    def skip(self) -> None:
        """A planned step that will not run this time: the count stays honest."""
        self.total = max(self._count, self.total - 1)

    def summary(self, *, min_s: float = 0.0) -> list[str]:
        """Print and return the summary of every step's duration; with
        *min_s*, only the steps that took at least that long are listed."""
        if not self.records:
            return []
        total = format_duration(self.clock() - self._started)
        failed = sum(1 for r in self.records if not r.ok)
        head = f"Summary: {len(self.records)} step(s) in {total}" + (f", {failed} failed" if failed else "")
        shown = [r for r in self.records if r.seconds >= min_s or not r.ok]
        if min_s > 0:
            head += f"; {len(shown)} took {format_duration(min_s)} or more" if shown else f"; none took {format_duration(min_s)} or more"
        width = max((len(r.what) for r in shown), default=0)
        lines = [head] + [
            f"  {r.number:>2}. {r.what.ljust(width)}  {format_duration(r.seconds)}{'' if r.ok else '  (failed)'}"
            for r in shown
        ]
        for text in lines:
            self.line(text)
        return lines


def _clear_meter() -> None:
    """A download meter's redrawn line is blanked before a step line is written,
    so the two never share a line. Never raises."""
    try:
        from nexus.progress.download_meter import clear_active_line  # noqa: PLC0415 — stdlib-only

        clear_active_line()
    except Exception:  # noqa: BLE001 — cosmetic
        pass


def _stderr_line(text: str) -> None:
    """The quiet reporter's default sink: one line on stderr. Never raises."""
    try:
        import click  # noqa: PLC0415 — deferred

        click.echo(text, err=True)
    except Exception:  # noqa: BLE001 — a progress line must never fail the step
        pass


def _is_refusal(exc: BaseException) -> bool:
    """A click usage error, refusal or exit: the command says it itself."""
    try:
        import click  # noqa: PLC0415 — deferred

        return isinstance(exc, (click.ClickException, click.exceptions.Exit, click.exceptions.Abort, SystemExit))
    except ImportError:  # pragma: no cover
        return isinstance(exc, SystemExit)


@contextlib.contextmanager
def _thread_ticker(step: Step) -> Generator[None]:
    """The production ticker: a daemon thread that asks the step for a
    heartbeat every :data:`TICK_S` until the step ends."""
    stop = threading.Event()
    thread = threading.Thread(
        target=run_ticker, args=(stop.wait, step.tick), name=f"step-{step.number}-ticker", daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2 * TICK_S)


@contextlib.contextmanager
def no_ticker(step: Step) -> Generator[None]:  # noqa: ARG001 — the ticker protocol passes the step
    """A ticker that never ticks: tests call :meth:`Step.tick` themselves."""
    yield
