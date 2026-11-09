# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a slow MCP tool call spends its time, sampled while it happens (nexus-3ceda.3).

A slow ``nx_answer`` or search used to leave one fact in ``mcp.log``: it was
slow. Opt in with ``NX_MCP_SLOW_CALL_S=<seconds>`` and a watchdog thread wakes
when a ``tools/call`` has run that long, samples every thread's stack with
``sys._current_frames()`` until the call ends, and logs one
``mcp_tool_call_slow`` event: the tool, its wall time, the process CPU it used
over the slow stretch (a low ratio means it was waiting, on the network or a
lock; a high one that it was computing), and the nexus functions the samples
landed in most.

**Zero cost when off.** Unset (or not a positive number), nothing is installed:
no middleware entry, no thread. On, a call that finishes under the threshold
costs a dict insert and delete under a lock and one wakeup of the watchdog
thread, which sleeps until the oldest running call's deadline and samples only
while some call is past it. A call that ran past the threshold without being
sampled is still logged, with ``state`` ``unsampled``.

**What a sample counts.** The tool body runs on an anyio worker thread (a sync
tool) or on the event loop (an async one), and the middleware cannot tell
which, so every thread but the watchdog's own is sampled and only stacks that
pass through the nexus package's own files are counted: an idle worker carries
none, and a loop parked in ``select`` is skipped though ``main()`` sits at its
base. Other nexus background threads (a subscription waiter, say) are counted
too, so a hot frame there is a neighbour of the slow call, not the call. A
worker blocked in ``subprocess.communicate`` is in ``selectors.py`` as well and
is counted: only a stack that also runs the asyncio loop is skipped. Each
counted stack contributes its innermost nexus frame and its innermost frame
overall, so a call blocked in ``httpx`` reads as
``search (nexus/mcp/core.py:123) <- _recv (ssl.py:…)``. Concurrent slow calls
share the samples taken while they overlap. Logged at WARNING the first time a
tool is slow in this process, INFO after (warn once per site).

**Scope.** Every ``tools/call`` on both servers, which includes ``nx_answer``
and the tool-tier hooks. Command-tier hooks (``nx-hook``) are short-lived
processes and are not watched. The summary is its own event beside the call's
``mcp_tool_call`` line, matched by ``pid`` and tool.
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import structlog

#: Seconds a call must run before it is sampled. Unset or not positive: off.
THRESHOLD_ENV: str = "NX_MCP_SLOW_CALL_S"
#: Seconds between samples while a call is slow.
SAMPLE_INTERVAL_S: float = 0.1
#: Most hot frames one event lists.
TOP_FRAMES: int = 8
#: A stack counts when one of its frames' files lies inside the nexus package itself,
#: matched on the package's own directory: a "/nexus/" substring would match every
#: frame of the default install under ~/.local/share/nexus/tools/, third-party ones included.
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep
_SELF = os.path.abspath(__file__)

Stacks = Mapping[int, Any]


def threshold_from_env(env: Mapping[str, str] | None = None) -> float | None:
    raw = ((os.environ if env is None else env).get(THRESHOLD_ENV) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 < value < 1e9 else None


def _where(frame: Any, pkg_dir: str) -> str:
    code = frame.f_code
    path = code.co_filename
    parent = os.path.dirname(pkg_dir.rstrip(os.sep)) + os.sep
    short = path[len(parent):] if path.startswith(pkg_dir) else os.path.basename(path)
    return f"{code.co_name} ({short}:{frame.f_lineno})"


def _in_event_loop(frame: Any) -> bool:
    f, depth = frame, 0
    while f is not None and depth < 64:
        if f.f_code.co_filename.endswith(os.path.join("asyncio", "base_events.py")):
            return True
        f, depth = f.f_back, depth + 1
    return False


def _stack_keys(frame: Any, pkg_dir: str = _PKG_DIR) -> str | None:
    """The key one thread's stack contributes, or None when no frame is nexus code."""
    innermost = frame
    # An idle event loop parked in select carries the server's own main() at its
    # base: that is not where a call's time went. Only a loop: a worker blocked in
    # subprocess.communicate is in selectors.py too, and that is a slow call's time.
    if os.path.basename(innermost.f_code.co_filename) == "selectors.py" and _in_event_loop(innermost):
        return None
    ours = None
    f = frame
    while f is not None:
        path = f.f_code.co_filename
        if path.startswith(pkg_dir) and path != _SELF:
            ours = f
            break
        f = f.f_back
    if ours is None:
        return None
    if ours is innermost:
        return _where(ours, pkg_dir)
    return f"{_where(ours, pkg_dir)} <- {_where(innermost, pkg_dir)}"


class _Call:
    __slots__ = ("tool", "started", "deadline", "slow", "cpu_at_slow", "slow_at", "samples", "counts")

    def __init__(self, tool: str, started: float, deadline: float) -> None:
        self.tool = tool
        self.started = started
        self.deadline = deadline
        self.slow = False
        self.cpu_at_slow = 0.0
        self.slow_at = 0.0
        self.samples = 0
        self.counts: collections.Counter[str] = collections.Counter()


def _process_cpu() -> float:
    t = os.times()
    return t.user + t.system


class SlowCallWatchdog:
    """Samples stacks while any registered call is past *threshold_s*.

    *clock*, *cpu_clock*, *frames* (``sys._current_frames``) and *log* are injectable;
    :meth:`sample_once` lets a test drive one sample without the thread.
    """

    def __init__(
        self,
        threshold_s: float,
        *,
        interval_s: float = SAMPLE_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        cpu_clock: Callable[[], float] = _process_cpu,
        frames: Callable[[], Stacks] = sys._current_frames,  # noqa: SLF001 -- the stdlib's only way to read another thread's stack
        log: Any = None,
        start_thread: bool = True,
        pkg_dir: str = _PKG_DIR,
    ) -> None:
        self._pkg_dir = pkg_dir
        self.threshold_s = threshold_s
        self._interval_s = interval_s
        self._clock = clock
        self._cpu = cpu_clock
        self._frames = frames
        self._log = log
        self._start_thread = start_thread
        self._cond = threading.Condition()
        self._calls: dict[int, _Call] = {}
        self._next_id = 0
        self._thread: threading.Thread | None = None
        self._warned: set[str] = set()

    # -- per call ----------------------------------------------------------
    def begin(self, tool: str) -> int:
        now = self._clock()
        with self._cond:
            self._next_id += 1
            token = self._next_id
            self._calls[token] = _Call(tool, now, now + self.threshold_s)
            if self._start_thread and self._thread is None:
                thread = threading.Thread(target=self._run, name="nx-slow-call-watchdog", daemon=True)
                try:
                    thread.start()
                except Exception:
                    self._calls.pop(token, None)  # no thread: forget the call, retry the start next time
                    raise
                self._thread = thread
            self._cond.notify()
        return token

    def end(self, token: int) -> dict[str, Any] | None:
        """Forget the call; when it went slow, log and return its summary."""
        now = self._clock()
        with self._cond:
            call = self._calls.pop(token, None)
            if call is None:
                return None
            # Read under the lock: a sample in flight updates counts under it too.
            hot = [f"{n}x {k}" for k, n in call.counts.most_common(TOP_FRAMES)]
            samples = call.samples
        if not call.slow:
            if now - call.started < self.threshold_s:
                return None
            # Past the threshold but never sampled (the thread did not get the GIL
            # in time, or the call ended between ticks): still say it was slow.
            call.slow_at, call.cpu_at_slow = now, self._cpu()
        cpu = max(0.0, self._cpu() - call.cpu_at_slow)
        slow_wall = max(now - call.slow_at, 1e-9)
        ratio = round(cpu / slow_wall, 2)
        summary = {
            "tool": call.tool,
            "wall_s": round(now - call.started, 3),
            "slow_after_s": self.threshold_s,
            "samples": samples,
            "cpu_s": round(cpu, 3),
            "cpu_ratio": ratio,
            "state": ("unsampled" if samples == 0 else "computing" if ratio >= 0.5 else "waiting"),
            "hot": hot,
            "pid": os.getpid(),
        }
        self._emit(summary)
        return summary

    def _emit(self, summary: dict[str, Any]) -> None:
        try:
            log = self._log or structlog.get_logger(__name__)
            first = summary["tool"] not in self._warned
            self._warned.add(summary["tool"])
            (log.warning if first else log.info)("mcp_tool_call_slow", **summary)
        except Exception:  # noqa: BLE001, S110 -- observability must never break a tool call
            pass

    # -- sampling ----------------------------------------------------------
    def sample_once(self) -> int:
        """Mark calls past their deadline slow and, if any is, take one sample.
        Returns how many calls the sample was charged to."""
        now = self._clock()
        with self._cond:
            slow = []
            for call in self._calls.values():
                if not call.slow and now >= call.deadline:
                    call.slow, call.slow_at, call.cpu_at_slow = True, now, self._cpu()
                if call.slow:
                    slow.append(call)
        if not slow:
            return 0
        me = threading.get_ident()
        keys = []
        for ident, frame in self._frames().items():
            if ident == me:
                continue
            key = _stack_keys(frame, self._pkg_dir)
            if key is not None:
                keys.append(key)
        with self._cond:
            for call in slow:
                call.samples += 1
                call.counts.update(keys)
        return len(slow)

    def _wait_s(self) -> float | None:
        """Seconds to sleep before the next look; None to wait for a new call."""
        if not self._calls:
            return None
        if any(c.slow for c in self._calls.values()):
            return self._interval_s
        return max(0.0, min(c.deadline for c in self._calls.values()) - self._clock())

    def _run(self) -> None:
        while True:
            try:
                with self._cond:
                    wait = self._wait_s()
                    self._cond.wait(timeout=None if wait is None else min(wait, threading.TIMEOUT_MAX))
                self.sample_once()
            except Exception:  # noqa: BLE001 -- the watchdog must never take the server down; back off and go on
                time.sleep(1.0)


def install_slow_call_watchdog(server: object, env: Mapping[str, str] | None = None) -> str:
    """Add the watchdog to the server's middleware chain when ``NX_MCP_SLOW_CALL_S``
    is set. Returns a short status for the caller to log; never raises."""
    threshold = threshold_from_env(env)
    if threshold is None:
        return "off"  # the servers report nothing for "off"
    try:
        middleware = server.middleware  # type: ignore[attr-defined]
        if any(getattr(m, "_nx_slow_call_watchdog", False) for m in middleware):
            return "already applied"
        watchdog = SlowCallWatchdog(threshold)

        async def _watched(ctx: Any, call_next: Any) -> Any:
            token = None
            if ctx.method == "tools/call":
                try:
                    params = ctx.params
                    name = params.get("name") if isinstance(params, Mapping) else None
                    token = watchdog.begin(str(name))
                except Exception:  # noqa: BLE001 -- observability must never break a tool call
                    token = None
            try:
                return await call_next(ctx)
            finally:
                if token is not None:
                    try:
                        watchdog.end(token)
                    except Exception:  # noqa: BLE001, S110
                        pass

        _watched._nx_slow_call_watchdog = True  # type: ignore[attr-defined]
        _watched.watchdog = watchdog  # type: ignore[attr-defined]
        middleware.append(_watched)
        return "applied"
    except Exception as exc:  # noqa: BLE001 -- an unverifiable SDK shape must not kill startup
        return f"unavailable: {type(exc).__name__}: {exc}"


__all__ = ["SAMPLE_INTERVAL_S", "THRESHOLD_ENV", "TOP_FRAMES", "SlowCallWatchdog", "install_slow_call_watchdog",
           "threshold_from_env"]
