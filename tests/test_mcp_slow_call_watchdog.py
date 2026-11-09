# SPDX-License-Identifier: AGPL-3.0-or-later
"""A slow MCP tool call is stack-sampled while it runs and logged with where its
time went; off unless NX_MCP_SLOW_CALL_S is set (nexus-3ceda.3)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from nexus.mcp import _slow_call_watchdog as wd
from nexus.mcp._sdk_patches import report_sdk_patches
from nexus.mcp._slow_call_watchdog import SlowCallWatchdog, install_slow_call_watchdog, threshold_from_env


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append(("warning", event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event, kw))


_PKG = "/site/nexus/"


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
    for off in ("0", "-1", "soon", "", "inf", "nan", "1e400"):
        assert threshold_from_env({wd.THRESHOLD_ENV: off}) is None


def test_off_installs_nothing() -> None:
    server = SimpleNamespace(middleware=[])
    assert install_slow_call_watchdog(server, env={}) == "off"
    assert server.middleware == []


def test_a_fast_call_is_never_sampled_or_logged() -> None:
    t = [0.0]
    log = _Log()
    dog = SlowCallWatchdog(5.0, clock=lambda: t[0], frames=lambda: {1: _nexus_stack()}, log=log, start_thread=False, pkg_dir=_PKG)
    token = dog.begin("search")
    t[0] = 4.9
    assert dog.sample_once() == 0
    assert dog.end(token) is None and log.events == []


def test_a_slow_call_reports_its_hot_frames_and_whether_it_was_waiting() -> None:
    t, cpu = [0.0], [10.0]
    log = _Log()
    stacks = {1: _nexus_stack(), 2: _idle_stack()}
    dog = SlowCallWatchdog(1.0, clock=lambda: t[0], cpu_clock=lambda: cpu[0], frames=lambda: stacks, log=log,
                           start_thread=False, pkg_dir=_PKG)
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
                           frames=lambda: {1: _frame("rank", _PKG + "plans/runner.py", 7)}, log=_Log(),
                           start_thread=False, pkg_dir=_PKG)
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
    dog = SlowCallWatchdog(1.0, clock=lambda: t[0], frames=dict, log=log, start_thread=False, pkg_dir=_PKG)
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
                           start_thread=False, pkg_dir=_PKG)
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

    busy_nexus_work.__code__ = busy_nexus_work.__code__.replace(co_filename=wd._PKG_DIR + "slow_tool.py")
    token = dog.begin("probe")
    worker = threading.Thread(target=busy_nexus_work)
    worker.start()
    time.sleep(0.3)
    release.set()
    worker.join()
    summary = dog.end(token)
    assert summary is not None and summary["samples"] >= 3
    assert any("busy_nexus_work (nexus/slow_tool.py" in h for h in summary["hot"]), summary["hot"]
    assert all("anyio" not in h and "threading.py:" not in h.split(" <- ")[0] for h in summary["hot"])


def test_the_middleware_times_tools_calls_only_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    server = SimpleNamespace(middleware=[])
    assert install_slow_call_watchdog(server, env={wd.THRESHOLD_ENV: "30"}) == "applied"
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


def test_a_failing_begin_never_reaches_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    server = SimpleNamespace(middleware=[])
    install_slow_call_watchdog(server, env={wd.THRESHOLD_ENV: "30"})
    (entry,) = server.middleware

    def broken_begin(_tool: str) -> int:
        raise RuntimeError("watchdog broke")

    monkeypatch.setattr(entry.watchdog, "begin", broken_begin)

    async def _next(_ctx: Any) -> str:
        return "answered"

    assert anyio.run(entry, SimpleNamespace(method="tools/call", params={"name": "search"}), _next) == "answered"


def test_a_tool_that_raises_still_ends_its_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    server = SimpleNamespace(middleware=[])
    install_slow_call_watchdog(server, env={wd.THRESHOLD_ENV: "30"})
    (entry,) = server.middleware
    ended: list[int] = []
    monkeypatch.setattr(entry.watchdog, "begin", lambda _tool: 7)
    monkeypatch.setattr(entry.watchdog, "end", ended.append)

    async def _boom(_ctx: Any) -> str:
        raise ValueError("tool failed")

    with pytest.raises(ValueError, match="tool failed"):
        anyio.run(entry, SimpleNamespace(method="tools/call", params={"name": "search"}), _boom)
    assert ended == [7]


def test_only_frames_inside_the_package_count() -> None:
    install = "/home/u/.local/share/nexus/tools/gen-1/lib/python3.12/site-packages/"
    idle_worker = _frame("wait", "/usr/lib/python3.12/threading.py", 359,
                         back=_frame("run", install + "anyio/_backends/_asyncio.py", 942))
    assert wd._stack_keys(idle_worker, install + "nexus/") is None
    ours = _frame("search", install + "nexus/mcp/core.py", 5, back=idle_worker)
    assert wd._stack_keys(_frame("recv", "/x/ssl.py", 1, back=ours), install + "nexus/") == (
        "search (nexus/mcp/core.py:5) <- recv (ssl.py:1)")


def test_an_idle_loop_with_main_at_its_base_is_skipped() -> None:
    loop = _frame("select", "/usr/lib/python3.12/selectors.py", 560,
                  back=_frame("run_forever", "/usr/lib/python3.12/asyncio/base_events.py", 600,
                              back=_frame("main", _PKG + "mcp/core.py", 16900)))
    assert wd._stack_keys(loop, _PKG) is None


def test_a_call_past_the_threshold_that_was_never_sampled_is_still_logged() -> None:
    t = [0.0]
    log = _Log()
    dog = SlowCallWatchdog(1.0, clock=lambda: t[0], frames=dict, log=log, start_thread=False, pkg_dir=_PKG)
    token = dog.begin("search")
    t[0] = 2.5
    summary = dog.end(token)
    assert summary is not None and summary["state"] == "unsampled" and summary["samples"] == 0


def test_an_installed_watchdog_reports_as_applied() -> None:
    server = SimpleNamespace(middleware=[])
    status = install_slow_call_watchdog(server, env={wd.THRESHOLD_ENV: "5"})
    log = _Log()
    report_sdk_patches(log, {"slow_call_watchdog": status})
    assert log.events == [("info", "mcp_sdk_patch_applied", {"patch": "slow_call_watchdog", "status": "applied"})]


@pytest.mark.parametrize("module", ["nexus.mcp.core", "nexus.mcp.catalog"])
@pytest.mark.parametrize(("value", "installed"), [("5", True), ("", False)])
def test_both_servers_install_it_only_when_asked(module: str, value: str, installed: bool) -> None:
    statuses = ("m._SDK_PATCH_RESULTS" if module == "nexus.mcp.core"
                else "m._patch_statuses(__import__('nexus.mcp.core', fromlist=['x'])._SDK_PATCH_RESULTS)")
    code = (f"import {module} as m; "
            "print(any(getattr(x, '_nx_slow_call_watchdog', False) for x in m.mcp.middleware)); "
            f"print({statuses}.get('slow_call_watchdog'))")
    env = {**os.environ, wd.THRESHOLD_ENV: value}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True,
                         timeout=120)
    present, status = out.stdout.strip().splitlines()[-2:]
    assert present == str(installed)
    # Off reports nothing: an "off" status would log mcp_sdk_patch_not_applied on every start.
    assert status == ("applied" if installed else "None")


def test_a_worker_blocked_in_a_subprocess_still_counts() -> None:
    blocked = _frame("select", "/usr/lib/python3.12/selectors.py", 415,
                     back=_frame("_communicate", "/usr/lib/python3.12/subprocess.py", 2100,
                                 back=_frame("index_repo", _PKG + "indexer.py", 77)))
    assert wd._stack_keys(blocked, _PKG) == "index_repo (nexus/indexer.py:77) <- select (selectors.py:415)"


def test_a_failed_thread_start_forgets_the_call_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    starts: list[int] = []
    real_start = threading.Thread.start

    def flaky_start(self: threading.Thread) -> None:
        starts.append(1)
        if len(starts) == 1:
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", flaky_start)
    dog = SlowCallWatchdog(30.0, log=_Log(), pkg_dir=_PKG)
    with pytest.raises(RuntimeError, match="start new thread"):
        dog.begin("search")
    assert dog._calls == {} and dog._thread is None
    token = dog.begin("search")
    assert dog._thread is not None and len(starts) == 2
    assert dog.end(token) is None
