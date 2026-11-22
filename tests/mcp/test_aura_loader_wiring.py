# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 acceptance test 10's wiring (nexus-mdbds.11.1): the server's name watch reports every registry resolve to
the aura loader, the 60 s re-resolve included, and the load runs on the waiter's loop. (RDR-253 Phase 2: the watch is a
thread of its own; the name lease it used to ride on is gone.)"""

from __future__ import annotations

import asyncio
import uuid

from nexus.auras import parse_aura
from nexus.auras.session import AuraLoader, on_loop
from nexus.mcp import channel, core
from nexus.mcp.subscriptions import SubscriptionSet
from tests.auras._support import minimal
from tests.test_mcp_channel import _FakeSender, _FakeTupleStore, _fake_store_factory


def test_a_startup_miss_then_the_watchs_re_resolve_loads_on_the_loop(monkeypatch) -> None:
    sid = str(uuid.uuid4())
    names = iter([None, "tamsin", "tamsin", "tamsin"])
    monkeypatch.setattr(core, "_registry_name_reader", lambda _sid: lambda: next(names, "tamsin"))
    subs = SubscriptionSet(session_id=sid)
    attached: list = []
    loop_threads: list[bool] = []

    def attach(runtime) -> None:
        loop_threads.append(asyncio.get_running_loop() is not None)
        attached.append(runtime)

    loader = AuraLoader(
        sid, subs,
        discover=lambda: [parse_aura(minimal("red").encode(), name="red")],
        is_on=lambda _n: True,
        entry=lambda _s: ("tamsin", 7),
        registered_owner=lambda: 7,
        make_runtime=lambda auras: auras,
        attach=attach,
    )

    async def scenario() -> None:
        waiter = channel.ChannelWaiter(sid, _fake_store_factory(_FakeTupleStore()), subs, sender=_FakeSender())
        channel.register_active_waiter(waiter)
        try:
            thread = core._watch_session_name(waiter, on_loop(loader, asyncio.get_running_loop()), poll_s=0.02)
            assert thread is not None
            for _ in range(200):
                if loader.loaded:
                    break
                await asyncio.sleep(0.01)
            await asyncio.to_thread(thread.join, 5.0)
            assert not thread.is_alive(), "the watch ends once the loader has loaded"
        finally:
            channel.unregister_active_waiter(sid)

    asyncio.run(scenario())
    assert loader.loaded and [[a.name for a in r] for r in attached] == [["red"]]
    assert loop_threads == [True], "the load must run on the waiter's loop, not the watch thread"
    assert subs.aura_topics() == ["board/topic"]


def test_the_server_builds_a_loader_callback_for_its_waiter() -> None:
    sid = str(uuid.uuid4())

    async def scenario():
        subs = SubscriptionSet(session_id=sid)
        waiter = channel.ChannelWaiter(sid, _fake_store_factory(_FakeTupleStore()), subs, sender=_FakeSender())
        return core._aura_loader(waiter, subs)  # noqa: SLF001

    assert callable(asyncio.run(scenario()))
