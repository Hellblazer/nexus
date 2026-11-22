# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 Technical Design 3 (S7): an aura's trigger topic is an aura-owned
subscription. It is held in the server and delivered, counts against
MAX_BOARD_TOPICS, is not in board_topics() (the channel status record the
Stop-hook wake guard reads), cannot be removed by the session's unsubscribe,
and is not persisted: it is made again at each load (nexus-mdbds.11.1)."""

from __future__ import annotations

import json

import pytest

from nexus.mcp.subscriptions import MAX_BOARD_TOPICS, SubscriptionSet, resolve_session_entry, resolve_session_name
from nexus.db.t2.http_tuple_store import SchemaViolationError

_SID = "11111111-2222-3333-4444-555555555555"
_TOPIC = "board/ci/nexus-develop--done"


def _subscribe(subs: SubscriptionSet, topic: str) -> None:
    subs.subscribe(topic, templates=[])  # type: ignore[arg-type]


def _delivered(subs: SubscriptionSet) -> list[str]:
    return [e["subspace"] for e in subs.entries()]


def test_an_aura_topic_is_delivered_but_not_a_board_topic() -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    assert _TOPIC in _delivered(subs)
    assert subs.board_topics() == []
    assert subs.aura_topics() == [_TOPIC]
    assert not subs.session_owns(_TOPIC)
    assert subs.aura_only(_TOPIC)
    assert not subs.aura_only("board/never-held")


def test_an_aura_topic_entry_carries_a_start_position() -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    (entry,) = [e for e in subs.entries() if e["subspace"] == _TOPIC]
    assert entry.get("since")
    assert entry.get("held_by") == "aura"  # tuple_subscriptions shows the session it is not its own


def test_the_session_unsubscribe_does_not_remove_the_aura_ownership() -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    subs.unsubscribe(_TOPIC)
    assert _TOPIC in _delivered(subs)
    assert subs.aura_topics() == [_TOPIC]


def test_the_same_topic_held_by_both_is_one_delivered_entry_and_a_board_topic() -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    _subscribe(subs, _TOPIC)
    assert _delivered(subs).count(_TOPIC) == 1
    assert subs.board_topics() == [_TOPIC]
    assert subs.session_owns(_TOPIC)
    assert not subs.aura_only(_TOPIC)
    # The session's unsubscribe drops its own announcement, never the aura's hold.
    subs.unsubscribe(_TOPIC)
    assert subs.board_topics() == []
    assert _delivered(subs).count(_TOPIC) == 1
    # And the reverse: releasing the auras leaves the session's own subscription.
    _subscribe(subs, _TOPIC)
    subs.release_auras()
    assert subs.board_topics() == [_TOPIC]
    assert _delivered(subs).count(_TOPIC) == 1


def test_two_auras_on_one_topic_share_one_delivered_entry() -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    subs.subscribe_aura("ci-red-again", _TOPIC)
    assert _delivered(subs).count(_TOPIC) == 1


def test_an_aura_topic_counts_against_the_cap_and_is_refused_by_name() -> None:
    subs = SubscriptionSet(session_id=_SID)
    for i in range(MAX_BOARD_TOPICS - 1):
        _subscribe(subs, f"board/t{i}")
    subs.subscribe_aura("fits", "board/last")
    with pytest.raises(SchemaViolationError, match="over-cap"):
        subs.subscribe_aura("over-cap", "board/one-too-many")
    with pytest.raises(SchemaViolationError):
        _subscribe(subs, "board/another")
    # A topic already delivered adds nothing, so the session may still take it.
    _subscribe(subs, "board/last")
    assert "board/last" in subs.board_topics()


def test_an_aura_topic_must_be_a_board_topic() -> None:
    subs = SubscriptionSet(session_id=_SID)
    with pytest.raises(SchemaViolationError):
        subs.subscribe_aura("bad", "mailbox/someone")


def test_aura_topics_are_not_persisted() -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    restored = SubscriptionSet.from_json(subs.to_json())
    assert _TOPIC not in _delivered(restored)
    assert restored.aura_topics() == []


def test_an_aura_subscribe_and_release_notify_the_waiter() -> None:
    subs = SubscriptionSet(session_id=_SID)
    seen: list[int] = []
    subs.add_listener(lambda s: seen.append(s.version))
    subs.subscribe_aura("ci-red", _TOPIC)
    subs.subscribe_aura("ci-red", _TOPIC)  # idempotent: no second bump
    subs.release_auras()
    assert len(seen) == 2


def test_the_registry_entry_carries_the_pid_the_aura_loader_checks(tmp_path) -> None:
    (tmp_path / "77.json").write_text(
        json.dumps({"sessionId": _SID, "name": "tamsin", "kind": "interactive", "pid": 77, "updatedAt": 1}),
        encoding="utf-8",
    )
    (tmp_path / "78.json").write_text(
        json.dumps({"sessionId": _SID, "name": "child", "kind": "sdk-cli", "pid": 78, "updatedAt": 2}),
        encoding="utf-8",
    )
    alive = lambda _pid: True  # noqa: E731
    assert resolve_session_entry(_SID, sessions_dir=tmp_path, pid_alive=alive) == ("tamsin", 77)
    assert resolve_session_name(_SID, sessions_dir=tmp_path, pid_alive=alive) == "tamsin"
    assert resolve_session_entry("nobody", sessions_dir=tmp_path, pid_alive=alive) is None
