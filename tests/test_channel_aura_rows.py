# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 Technical Design 3 (nexus-mdbds.11.1, rule 6): a row on a topic
only an aura holds goes to the aura sink and is not announced to the agent.
When the session also subscribes the topic, its announcement is unchanged and
the aura sink still sees the row."""

from __future__ import annotations

import uuid

import pytest

from nexus.mcp import channel
from tests.test_mcp_channel import _FakeSender, _FakeTupleStore, _fake_store_factory, _subs


def _no_store():
    raise AssertionError("must not touch the store")


def _waiter(subs, fake, sender, seen):
    waiter = channel.ChannelWaiter(subs.session_id, _fake_store_factory(fake), subs, sender=sender)
    waiter.aura_sink = lambda topic, rows: seen.append((topic, [r.id for r in rows]))
    return waiter


@pytest.mark.asyncio
async def test_an_aura_only_topic_is_evaluated_and_not_announced() -> None:
    subs = _subs(str(uuid.uuid4()))
    subs.subscribe_aura("ci-red", "board/ci/x--done")
    fake = _FakeTupleStore()
    fake.seed("board/ci/x--done", "p1", "red", dims={"kind": "ci-done"})
    sender, seen = _FakeSender(), []
    await _waiter(subs, fake, sender, seen).tick()
    assert seen == [("board/ci/x--done", ["p1"])]
    assert [m for _c, m in sender.calls if m.get("subspace") == "board/ci/x--done"] == []


@pytest.mark.asyncio
async def test_a_topic_the_session_also_holds_is_announced_and_evaluated() -> None:
    subs = _subs(str(uuid.uuid4()))
    subs.subscribe_aura("ci-red", "board/ci/x--done")
    subs.subscribe("board/ci/x--done", templates=[])
    fake = _FakeTupleStore()
    fake.seed("board/ci/x--done", "p1", "red", dims={"kind": "ci-done"})
    sender, seen = _FakeSender(), []
    await _waiter(subs, fake, sender, seen).tick()
    assert seen == [("board/ci/x--done", ["p1"])]
    sends = [m for _c, m in sender.calls if m.get("subspace") == "board/ci/x--done"]
    assert len(sends) == 1 and sends[0]["tuple_id"] == "p1"


@pytest.mark.asyncio
async def test_a_failing_sink_never_stops_the_announcement() -> None:
    subs = _subs(str(uuid.uuid4()))
    subs.subscribe("board/ci/x--done", templates=[])
    fake = _FakeTupleStore()
    fake.seed("board/ci/x--done", "p1", "red")
    sender = _FakeSender()
    waiter = channel.ChannelWaiter(subs.session_id, _fake_store_factory(fake), subs, sender=sender)

    def boom(_topic, _rows):
        raise RuntimeError("aura bug")

    waiter.aura_sink = boom
    await waiter.tick()
    assert [m["tuple_id"] for _c, m in sender.calls if m.get("subspace") == "board/ci/x--done"] == ["p1"]


@pytest.mark.asyncio
async def test_an_aura_held_topic_with_no_sink_attached_is_still_announced() -> None:
    """A load that subscribed and then failed must never swallow rows: no sink, no suppression."""
    subs = _subs(str(uuid.uuid4()))
    subs.subscribe_aura("ci-red", "board/ci/x--done")
    fake = _FakeTupleStore()
    fake.seed("board/ci/x--done", "p1", "red")
    sender = _FakeSender()
    await channel.ChannelWaiter(subs.session_id, _fake_store_factory(fake), subs, sender=sender).tick()
    assert [m["tuple_id"] for _c, m in sender.calls if m.get("subspace") == "board/ci/x--done"] == ["p1"]


@pytest.mark.asyncio
async def test_a_row_on_a_topic_in_neither_set_is_announced_as_before() -> None:
    session_id = str(uuid.uuid4())
    subs = _subs(session_id)
    sender, seen = _FakeSender(), []
    waiter = _waiter(subs, _FakeTupleStore(), sender, seen)
    row = _FakeTupleStore().seed("board/gone", "g1", "x")
    await waiter._deliver_board_rows("board/gone", [row], None)  # noqa: SLF001 -- an unsubscribe raced the wait
    assert [m["tuple_id"] for _c, m in sender.calls] == ["g1"]
    assert seen == [("board/gone", ["g1"])]
