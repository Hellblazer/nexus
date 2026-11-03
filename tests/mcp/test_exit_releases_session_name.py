# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-tu84w: a clean nx-mcp exit releases the session's peer-directory name.

Before this, ``_release_directory_entry`` ran only on an unsubscribe and the
T1 handoff, so every exit left ``directory/<name>`` live for one
``DIRECTORY_TTL_S`` (300 s). In the 2026-10-31 coordinated restart the fresh
sessions messaged each other inside that window and ``mailbox_send`` by name
was refused as ambiguous (two session ids holding ``linda``).

The release re-sends the lease's OWN nonce with ``ttl_seconds=1``, so it can
only lapse the row this process wrote. It runs on its own thread with a
bounded wait, alongside the T1 teardown, so a slow engine or a lock the main
thread holds when SIGTERM lands cannot hold up the exit.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
from contextlib import contextmanager

import pytest

from nexus import mcp_infra
from nexus.mcp import core
from nexus.mcp import subscriptions as subs
from tests._module_seam import setattr_in


class _FakeTuples:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.calls: list[tuple] = []
        self.delay_s = delay_s

    def out(self, subspace, keys, dims=None, body=None, *, nonce=None, ttl_seconds=None):
        if ttl_seconds == 1 and self.delay_s:
            time.sleep(self.delay_s)
        self.calls.append((subspace, keys, dims, nonce, ttl_seconds))
        return "fake-tuple-id"


def _factory(fake: _FakeTuples):
    @contextmanager
    def _cm():
        class _Db:
            tuples = fake
        yield _Db()
    return _cm


def _wait_for(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _armed(fake: _FakeTuples, session_id: str, name: str) -> subs.SubscriptionSet:
    """An armed lease, held in the process cache the way get_or_load holds it."""
    s = subs.SubscriptionSet(session_id=session_id)
    assert s.arm_session_name(name, store_factory=_factory(fake)) is True
    assert _wait_for(lambda: len(fake.calls) == 1)
    with subs._CACHE_LOCK:  # noqa: SLF001 — the cache the exit path drains
        subs._CACHE[session_id] = s  # noqa: SLF001
    return s


def _release_and_wait() -> None:
    core._await_session_name_release(core._start_session_name_release())


def _releases(fake: _FakeTuples, name: str) -> list[tuple]:
    return [c for c in fake.calls if c[0] == f"directory/{name}" and c[4] == 1]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    subs.reset_cache()
    monkeypatch.setattr(core, "_NAME_RELEASE_THREAD", None)
    yield
    t = core._NAME_RELEASE_THREAD
    if t is not None:
        t.join(10)
    subs.reset_cache()


class _Exit(Exception):
    pass


def _sigterm_stubs(monkeypatch, order: list[str]) -> None:
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", False)
    monkeypatch.setattr(core, "_flush_search_telemetry_at_exit", lambda: None)
    monkeypatch.setattr(core, "_t1_shutdown", lambda: order.append("t1_shutdown"))

    def _fake_exit(code: int) -> None:
        order.append(f"exit {code}")
        raise _Exit

    setattr_in(monkeypatch, core, "_os._exit", _fake_exit)


def test_the_release_lapses_the_row_this_process_wrote() -> None:
    fake = _FakeTuples()
    _armed(fake, "sess-exit", "linda")
    [(_, _, _, armed_nonce, ttl)] = fake.calls
    assert ttl == int(subs.DIRECTORY_TTL_S)

    _release_and_wait()

    [release] = _releases(fake, "linda")
    assert release == ("directory/linda", {"name": "linda"}, {"session_id": "sess-exit"}, armed_nonce, 1)
    assert subs._CACHE == {}  # noqa: SLF001

    before = len(fake.calls)
    _release_and_wait()  # a second exit path writes nothing
    assert len(fake.calls) == before


def test_a_failing_release_never_raises_into_the_exit(monkeypatch) -> None:
    class _Broken:
        @staticmethod
        def reset_cache(*_a, **_k):
            raise RuntimeError("engine gone")
    monkeypatch.setattr(core, "_subscriptions", _Broken)
    logged: list[str] = []
    monkeypatch.setattr(core._log, "warning", lambda event, **_k: logged.append(event))
    core._release_session_names_now()  # on the caller's thread: raises if not caught
    assert logged == ["session_name_release_on_exit_failed"]


def test_a_slow_release_is_abandoned_at_its_deadline(monkeypatch) -> None:
    monkeypatch.setattr(core, "_NAME_RELEASE_TIMEOUT_S", 0.3)
    fake = _FakeTuples(delay_s=3.0)
    _armed(fake, "sess-slow", "marlowe")
    order: list[str] = []
    monkeypatch.setattr(core, "_t1_shutdown", lambda: order.append("t1_shutdown"))
    started = time.monotonic()
    core._exit_teardown()
    assert time.monotonic() - started < 2.0
    assert order == ["t1_shutdown"]


def test_a_second_exit_path_shares_the_first_ones_deadline(monkeypatch) -> None:
    """The lifespan abandons a stuck release at its deadline, then atexit
    runs: it waits out what is left of that deadline, not a fresh one."""
    monkeypatch.setattr(core, "_NAME_RELEASE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(core, "_t1_shutdown", lambda: None)
    fake = _FakeTuples(delay_s=3.0)
    _armed(fake, "sess-twice", "marginalia")
    _release_and_wait()  # the lifespan's wait, abandoned at 0.5 s
    started = time.monotonic()
    core._exit_teardown()  # atexit: the same release, the same deadline
    assert time.monotonic() - started < 0.3


def test_a_release_that_cannot_start_does_not_stop_the_t1_teardown(monkeypatch) -> None:
    class _NoThread:
        def __init__(self, *_a, **_k) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    setattr_in(monkeypatch, core, "threading.Thread", _NoThread)
    order: list[str] = []
    monkeypatch.setattr(core, "_t1_shutdown", lambda: order.append("t1_shutdown"))
    core._exit_teardown()
    assert order == ["t1_shutdown"]


def test_sigterm_releases_the_name_and_still_runs_the_t1_teardown(monkeypatch) -> None:
    fake = _FakeTuples()
    _armed(fake, "sess-term", "wren")
    order: list[str] = []
    _sigterm_stubs(monkeypatch, order)

    with pytest.raises(_Exit):
        core._sigterm_handler(15, None)

    assert order == ["t1_shutdown", "exit 0"]
    assert len(_releases(fake, "wren")) == 1


def test_sigterm_exits_while_the_main_thread_holds_the_cache_lock(monkeypatch) -> None:
    """``get_or_load`` holds ``_CACHE_LOCK`` across an HTTP call on the loop
    thread, and a signal handler runs on that same thread. The handler must
    not wait on the lock: the release thread waits, and the exit goes on."""
    monkeypatch.setattr(core, "_NAME_RELEASE_TIMEOUT_S", 0.3)
    order: list[str] = []
    _sigterm_stubs(monkeypatch, order)
    done = threading.Event()

    def _term() -> None:
        try:
            core._sigterm_handler(15, None)
        except _Exit:
            done.set()

    with subs._CACHE_LOCK:  # noqa: SLF001 — the main thread is mid get_or_load
        started = time.monotonic()
        _term()
        elapsed = time.monotonic() - started
    assert done.is_set()
    assert order == ["t1_shutdown", "exit 0"]
    assert elapsed < 2.0


def test_sigterm_during_the_lifespan_release_waits_for_it(monkeypatch) -> None:
    fake = _FakeTuples(delay_s=0.5)
    _armed(fake, "sess-both", "tamsin")
    order: list[str] = []
    _sigterm_stubs(monkeypatch, order)

    core._start_session_name_release()  # the lifespan's release, in flight
    with pytest.raises(_Exit):
        core._sigterm_handler(15, None)

    assert len(_releases(fake, "tamsin")) == 1  # done before the exit, not cut off


def test_the_exit_handlers_register_the_release_with_atexit(monkeypatch) -> None:
    registered: list = []
    signals: dict = {}
    core._install_exit_handlers(
        register=registered.append, set_signal=lambda sig, fn: signals.__setitem__(sig, fn),
    )

    assert registered == [core._exit_teardown]
    # and main() installs them: the pre-fix main registered _t1_shutdown inline
    assert "_install_exit_handlers()" in inspect.getsource(core.main)
    import signal  # noqa: PLC0415 — test-local

    expected = {signal.SIGTERM: core._sigterm_handler, signal.SIGINT: core._sigterm_handler}
    if hasattr(signal, "SIGHUP"):  # a terminal hangup (tmux kill-server) gets the same teardown (nexus-xwijj)
        expected[signal.SIGHUP] = core._sigterm_handler
    assert signals == expected

    fake = _FakeTuples()
    _armed(fake, "sess-atexit", "bob")
    order: list[str] = []
    monkeypatch.setattr(core, "_t1_shutdown", lambda: order.append("t1_shutdown"))
    registered[0]()
    assert order == ["t1_shutdown"]
    assert len(_releases(fake, "bob")) == 1


@pytest.mark.t2_tenant
def test_the_lifespan_exit_releases_the_name(monkeypatch) -> None:
    """The normal stdio exit: stdin EOF ends the lifespan's ``async with``."""
    monkeypatch.setattr(core, "_OWNED_T1_SESSION", {})
    monkeypatch.setattr(core, "_BORROWED_T1_SESSION", {})
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", False)
    monkeypatch.setattr(core, "_T1_SESSION_REFRESH_TASK", None)
    sid = os.environ["NX_T1_SESSION_ID"]
    monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: sid)
    mcp_infra.reset_t1_for_release()
    fake = _FakeTuples()

    async def _run() -> None:
        async with core._t1_lifespan(None):
            _armed(fake, sid, "halcyon")

    try:
        asyncio.run(_run())
    finally:
        mcp_infra.reset_t1_for_release()

    assert len(_releases(fake, "halcyon")) == 1
