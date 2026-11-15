# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 acceptance test 10's wiring (nexus-mdbds.11.1): the server's name
resolver reports every resolve to the aura loader, the 60 s re-resolve on the
lease thread included, and the load runs on the waiter's loop."""

from __future__ import annotations

import asyncio
import uuid

from nexus.auras import parse_aura
from nexus.auras.session import AuraLoader, on_loop
from nexus.mcp import channel, core
from nexus.mcp.subscriptions import SubscriptionSet
from tests.auras._support import minimal
from tests.test_mcp_channel import _FakeSender, _FakeTupleStore, _fake_store_factory


class _StubSubs:
    """Stands in for SubscriptionSet.arm_session_name: keeps the resolver the lease thread would re-run."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.resolve = None

    def arm_session_name(self, name, *, store_factory, resolve) -> bool:
        self.resolve = resolve
        return False


def test_a_startup_miss_then_the_lease_threads_re_resolve_loads_on_the_loop(monkeypatch) -> None:
    sid = str(uuid.uuid4())
    names = iter([None, "tamsin"])
    monkeypatch.setattr(core, "_session_name_resolver", lambda _sid: lambda: next(names))
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
        stub = _StubSubs(sid)
        core._arm_session_name(stub, on_loop(loader, asyncio.get_running_loop()))  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert not loader.loaded and attached == []  # the startup resolve missed
        assert stub.resolve is not None
        assert await asyncio.to_thread(stub.resolve) == "tamsin"  # the lease thread's re-resolve
        for _ in range(100):
            if loader.loaded:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert loader.loaded and [[a.name for a in r] for r in attached] == [["red"]]
    assert loop_threads == [True], "the load must run on the waiter's loop, not the lease thread"
    assert subs.aura_topics() == ["board/topic"]


def test_the_server_builds_a_loader_callback_for_its_waiter() -> None:
    sid = str(uuid.uuid4())

    async def scenario():
        subs = SubscriptionSet(session_id=sid)
        waiter = channel.ChannelWaiter(sid, _fake_store_factory(_FakeTupleStore()), subs, sender=_FakeSender())
        return core._aura_loader(waiter, subs)  # noqa: SLF001

    assert callable(asyncio.run(scenario()))
