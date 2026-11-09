# SPDX-License-Identifier: AGPL-3.0-or-later
"""A slow MCP tool call is stack-sampled while it runs and logged with where its
time went; off unless NX_MCP_SLOW_CALL_S is set (nexus-3ceda.3)."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from nexus.mcp import _slow_call_watchdog as wd
from nexus.mcp._slow_call_watchdog import SlowCallWatchdog, install_slow_call_watchdog, threshold_from_env


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append(("warning", event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event, kw))


def _frame(name: str, path: str, line: int, back: Any = None) -> Any:
    return SimpleNamespace(f_code=SimpleNamespace(co_name=name, co_filename=path), f_lineno=line, f_back=back)


def _nexus_stack() -> Any:
    tool = _frame("search", "/site/nexus/mcp/core.py", 120)
    return _frame("_recv", "/usr/lib/python3/ssl.py", 900, back=_frame("send", "/site/httpx/_client.py", 50, back=tool))


def _idle_stack() -> Any:
    return _frame("select", "/usr/lib/python3/selectors.py", 10, back=_frame("_run", "/usr/lib/python3/threading.py", 1))


def test_the_threshold_reads_a_positive_number_or_is_off() -> None:
    assert threshold_from_env({}) is None
    assert threshold_from_env({wd.THRESHOLD_ENV: "2.5"}) == 2.5
    for off in ("0", "-1", "soon", ""):
        assert threshold_from_env({wd.THRESHOLD_ENV: off}) is None


def test_off_installs_nothing() -> None:
    server = SimpleNamespace(middleware=[])
    assert install_slow_call_watchdog(server, env={}) == "off"
    assert server.middleware == []


def test_a_fast_call_is_never_sampled_or_logged() -> None:
    t = [0.0]
    log = _Log()
    dog = SlowCallWatchdog(5.0, clock=lambda: t[0], frames=lambda: {1: _nexus_stack()}, log=log, start_thread=False)
    token = dog.begin("search")
    t[0] = 4.9
    assert dog.sample_once() == 0
    assert dog.end(token) is None and log.events == []


def test_a_slow_call_reports_its_hot_frames_and_whether_it_was_waiting() -> None:
    t, cpu = [0.0], [10.0]
    log = _Log()
    stacks = {1: _nexus_stack(), 2: _idle_stack()}
    dog = SlowCallWatchdog(1.0, clock=lambda: t[0], cpu_clock=lambda: cpu[0], frames=lambda: stacks, log=log,
                           start_thread=False)
    token = dog.begin("search")
    for step in range(1, 11):
        t[0] = 1.0 + step * 0.1
        dog.sample_once()
    cpu[0] = 10.05
    t[0] = 3.0
    summary = dog.end(token)
    assert summary is not None
    assert summary["tool"] == "search" and summary["wall_s"] == 3.0
    assert summary["samples"] == 10
    assert summary["state"] == "waiting" and summary["cpu_ratio"] < 0.5
    assert summary["hot"] == ["10x search (nexus/mcp/core.py:120) <- _recv (ssl.py:900)"], "idle stacks counted"
    assert log.events[0][:2] == ("warning", "mcp_tool_call_slow")


def test_a_computing_call_reads_as_computing() -> None:
    t, cpu = [0.0], [0.0]
    dog = SlowCallWatchdog(1.0, clock=lambda: t[0], cpu_clock=lambda: cpu[0],
                           frames=lambda: {1: _frame("rank", "/x/nexus/plans/runner.py", 7)}, log=_Log(),
                           start_thread=False)
    token = dog.begin("nx_answer")
    t[0] = 1.0
    dog.sample_once()
    t[0], cpu[0] = 3.0, 1.9
    summary = dog.end(token)
    assert summary is not None and summary["state"] == "computing"
    assert summary["hot"] == ["1x rank (nexus/plans/runner.py:7)"]


def test_warn_once_per_tool_then_info() -> None:
    t = [0.0]
    log = _Log()
    dog = SlowCallWatchdog(1.0, clock=lambda: t[0], frames=dict, log=log, start_thread=False)
    for tool in ("search", "search", "store_put"):
        token = dog.begin(tool)
        t[0] += 2.0
        dog.sample_once()
        dog.end(token)
    assert [(level, fields["tool"]) for level, _event, fields in log.events] == [
        ("warning", "search"), ("info", "search"), ("warning", "store_put")]


def test_a_sample_skips_the_watchdogs_own_thread() -> None:
    t = [0.0]
    me = threading.get_ident()
    dog = SlowCallWatchdog(0.5, clock=lambda: t[0], frames=lambda: {me: _nexus_stack()}, log=_Log(),
                           start_thread=False)
    token = dog.begin("search")
    t[0] = 1.0
    dog.sample_once()
    summary = dog.end(token)
    assert summary is not None and summary["hot"] == []


def test_the_real_thread_samples_a_real_slow_call() -> None:
    log = _Log()
    dog = SlowCallWatchdog(0.05, interval_s=0.01, log=log)
    release = threading.Event()

    def busy_nexus_work() -> None:  # this file is under tests/, so mark the frames as nexus code
        release.wait(0.5)

    busy_nexus_work.__code__ = busy_nexus_work.__code__.replace(co_filename="/fake/nexus/slow_tool.py")
    token = dog.begin("probe")
    worker = threading.Thread(target=busy_nexus_work)
    worker.start()
    time.sleep(0.3)
    release.set()
    worker.join()
    summary = dog.end(token)
    assert summary is not None and summary["samples"] >= 3
    assert any("busy_nexus_work (nexus/slow_tool.py" in h for h in summary["hot"]), summary["hot"]


def test_the_middleware_times_tools_calls_only_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    server = SimpleNamespace(middleware=[])
    assert install_slow_call_watchdog(server, env={wd.THRESHOLD_ENV: "30"}) == "applied (threshold 30s)"
    assert install_slow_call_watchdog(server, env={wd.THRESHOLD_ENV: "30"}) == "already applied"
    (entry,) = server.middleware
    seen: list[str] = []
    monkeypatch.setattr(entry.watchdog, "begin", lambda tool: seen.append(tool) or 1)

    def broken_end(_token: int) -> None:
        raise RuntimeError("watchdog broke")

    monkeypatch.setattr(entry.watchdog, "end", broken_end)

    async def _next(_ctx: Any) -> str:
        return "answered"

    assert anyio.run(entry, SimpleNamespace(method="tools/call", params={"name": "search"}), _next) == "answered"
    assert anyio.run(entry, SimpleNamespace(method="tools/list", params={}), _next) == "answered"
    assert seen == ["search"]
