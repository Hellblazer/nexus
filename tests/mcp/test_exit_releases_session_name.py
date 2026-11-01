# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-tu84w: a clean nx-mcp exit releases the session's peer-directory name.

Before this, ``_release_directory_entry`` ran only on an unsubscribe and the
T1 handoff, so every exit left ``directory/<name>`` live for one
``DIRECTORY_TTL_S`` (300 s). In the 2026-10-31 coordinated restart the fresh
sessions messaged each other inside that window and ``mailbox_send`` by name
was refused as ambiguous (two session ids holding ``linda``).

The release re-sends the lease's OWN nonce with ``ttl_seconds=1``, so it can
only lapse the row this process wrote: another process holding the same name
under the same session id keeps its row.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager

import pytest

from nexus import mcp_infra
from nexus.mcp import core
from nexus.mcp import subscriptions as subs


class _FakeTuples:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def out(self, subspace, keys, dims=None, body=None, *, nonce=None, ttl_seconds=None):
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


@pytest.fixture(autouse=True)
def _empty_cache():
    subs.reset_cache()
    yield
    subs.reset_cache()


def _releases(fake: _FakeTuples, name: str) -> list[tuple]:
    return [c for c in fake.calls if c[0] == f"directory/{name}" and c[4] == 1]


def test_the_exit_release_lapses_the_row_this_process_wrote() -> None:
    fake = _FakeTuples()
    _armed(fake, "sess-exit", "linda")
    [(_, _, _, armed_nonce, ttl)] = fake.calls
    assert ttl == int(subs.DIRECTORY_TTL_S)

    core._release_session_names()

    [release] = _releases(fake, "linda")
    assert release == ("directory/linda", {"name": "linda"}, {"session_id": "sess-exit"}, armed_nonce, 1)
    assert subs._CACHE == {}  # noqa: SLF001

    before = len(fake.calls)
    core._release_session_names()  # idempotent: the second exit path writes nothing
    assert len(fake.calls) == before


def test_a_failing_release_never_raises_into_the_exit(monkeypatch) -> None:
    class _Broken:
        @staticmethod
        def reset_cache(*_a, **_k):
            raise RuntimeError("engine gone")
    monkeypatch.setattr(core, "_subscriptions", _Broken)
    core._release_session_names()  # logged, not raised


def test_sigterm_releases_the_name_before_the_t1_teardown_and_exit(monkeypatch) -> None:
    fake = _FakeTuples()
    _armed(fake, "sess-term", "wren")
    order: list[str] = []
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", False)
    monkeypatch.setattr(core, "_flush_search_telemetry_at_exit", lambda: None)
    monkeypatch.setattr(core, "_t1_shutdown", lambda: order.append(f"t1_shutdown releases={len(_releases(fake, 'wren'))}"))

    class _Exit(Exception):
        pass

    def _fake_exit(code: int) -> None:
        order.append(f"exit {code}")
        raise _Exit

    monkeypatch.setattr(core._os, "_exit", _fake_exit)
    with pytest.raises(_Exit):
        core._sigterm_handler(15, None)

    assert order == ["t1_shutdown releases=1", "exit 0"]


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
