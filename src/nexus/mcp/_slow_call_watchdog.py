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
costs a dict insert and delete under a lock; the thread sleeps until the oldest
running call's deadline and samples only while some call is past it.

**What a sample counts.** The tool body runs on an anyio worker thread (a sync
tool) or on the event loop (an async one), and the middleware cannot tell
which, so every thread but the watchdog's own is sampled and only stacks that
pass through nexus code are counted: an idle worker or the loop's own select
carries none. Each counted stack contributes its innermost nexus frame and its
innermost frame overall, so a call blocked in ``httpx`` reads as
``search (nexus/mcp/core.py:123) <- _recv (ssl.py:…)``. Concurrent slow calls
share the samples taken while they overlap. Logged at WARNING the first time a
tool is slow in this process, INFO after (warn once per site).
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
#: A stack counts when one of its frames' files lies under this package.
_PKG_MARK = os.sep + "nexus" + os.sep

Stacks = Mapping[int, Any]


def threshold_from_env(env: Mapping[str, str] | None = None) -> float | None:
    raw = ((os.environ if env is None else env).get(THRESHOLD_ENV) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _where(frame: Any) -> str:
    code = frame.f_code
    path = code.co_filename
    i = path.rfind(_PKG_MARK)
    short = path[i + 1:] if i >= 0 else os.path.basename(path)
    return f"{code.co_name} ({short}:{frame.f_lineno})"


def _stack_keys(frame: Any) -> str | None:
    """The key one thread's stack contributes, or None when no frame is nexus code."""
    innermost = frame
    ours = None
    f = frame
    while f is not None:
        if _PKG_MARK in f.f_code.co_filename and not f.f_code.co_filename.endswith("_slow_call_watchdog.py"):
            ours = f
            break
        f = f.f_back
    if ours is None:
        return None
    return _where(ours) if ours is innermost else f"{_where(ours)} <- {_where(innermost)}"


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
    ) -> None:
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
                self._thread = threading.Thread(target=self._run, name="nx-slow-call-watchdog", daemon=True)
                self._thread.start()
            self._cond.notify()
        return token

    def end(self, token: int) -> dict[str, Any] | None:
        """Forget the call; when it went slow, log and return its summary."""
        now = self._clock()
        with self._cond:
            call = self._calls.pop(token, None)
        if call is None or not call.slow:
            return None
        cpu = max(0.0, self._cpu() - call.cpu_at_slow)
        slow_wall = max(now - call.slow_at, 1e-9)
        ratio = round(cpu / slow_wall, 2)
        summary = {
            "tool": call.tool,
            "wall_s": round(now - call.started, 3),
            "slow_after_s": self.threshold_s,
            "samples": call.samples,
            "cpu_s": round(cpu, 3),
            "cpu_ratio": ratio,
            "state": "computing" if ratio >= 0.5 else "waiting",
            "hot": [f"{n}x {k}" for k, n in call.counts.most_common(TOP_FRAMES)],
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
            key = _stack_keys(frame)
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
                    self._cond.wait(timeout=wait)
                self.sample_once()
            except Exception:  # noqa: BLE001 -- the watchdog must never take the server down; back off and go on
                time.sleep(1.0)


def install_slow_call_watchdog(server: object, env: Mapping[str, str] | None = None) -> str:
    """Add the watchdog to the server's middleware chain when ``NX_MCP_SLOW_CALL_S``
    is set. Returns a short status for the caller to log; never raises."""
    threshold = threshold_from_env(env)
    if threshold is None:
        return "off"
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
        return f"applied (threshold {threshold:g}s)"
    except Exception as exc:  # noqa: BLE001 -- an unverifiable SDK shape must not kill startup
        return f"unavailable: {type(exc).__name__}: {exc}"


__all__ = ["SAMPLE_INTERVAL_S", "THRESHOLD_ENV", "TOP_FRAMES", "SlowCallWatchdog", "install_slow_call_watchdog",
           "threshold_from_env"]
