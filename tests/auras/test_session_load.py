# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 TD1 and TD3 loading (nexus-mdbds.11.1 part A, rules 1 to 4): auras
load when the session registry first names the session, a later re-resolve
included; a host with no registry and a claude -p child load none and say
why; an aura past MAX_BOARD_TOPICS is refused by name."""

from __future__ import annotations

import logging

import pytest
import structlog

from nexus.auras import parse_aura
from nexus.auras.session import AuraLoader
from nexus.mcp.subscriptions import MAX_BOARD_TOPICS, SubscriptionSet
from tests.auras._support import minimal

@pytest.fixture(autouse=True)
def _log_everything() -> None:
    """The suite filters structlog at WARNING; these tests pin INFO lines. The suite's autouse
    _restore_structlog_after_test undoes this."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


_SID = "11111111-2222-3333-4444-555555555555"
_ME = 4242
_PARENT = 100


def _auras(*names: str, topic: str = "board/topic"):
    return [
        parse_aura(minimal(n).replace('subspace = "board/topic"', f'subspace = "{topic}-{n}"').encode(), name=n)
        for n in names
    ]


class _Harness:
    def __init__(self, *, auras=None, on=None, entry=(("tamsin", _PARENT)), ancestors=frozenset({_PARENT, 1})):
        self.subs = SubscriptionSet(session_id=_SID)
        self.attached: list = []
        self.discovered = 0
        auras = _auras("a", "b") if auras is None else auras

        def discover():
            self.discovered += 1
            return auras

        self.loader = AuraLoader(
            _SID, self.subs,
            discover=discover,
            is_on=(on or (lambda _n: True)),
            entry=lambda _sid: entry,
            ancestors=lambda _pid: ancestors,
            my_pid=_ME,
            make_runtime=lambda loaded: loaded,
            attach=self.attached.append,
        )


def test_auras_load_once_when_the_session_is_first_named() -> None:
    h = _Harness()
    h.loader.on_resolve("tamsin")
    h.loader.on_resolve("tamsin")
    assert [[a.name for a in r] for r in h.attached] == [["a", "b"]]
    assert h.subs.aura_topics() == ["board/topic-a", "board/topic-b"]
    assert h.discovered == 1


def test_a_startup_miss_then_a_later_re_resolve_loads_and_a_later_miss_keeps_them() -> None:
    h = _Harness()
    with structlog.testing.capture_logs() as logs:
        h.loader.on_resolve(None)
        h.loader.on_resolve(None)
    assert h.attached == []
    misses = [e for e in logs if e["event"] == "auras_not_loaded"]
    assert len(misses) == 1 and misses[0]["log_level"] == "info" and misses[0]["reason"]
    h.loader.on_resolve("tamsin")
    h.loader.on_resolve(None)
    assert len(h.attached) == 1 and h.subs.aura_topics()


def test_only_the_auras_turned_on_load() -> None:
    h = _Harness(on=lambda n: n == "b")
    h.loader.on_resolve("tamsin")
    assert [a.name for a in h.attached[0]] == ["b"]
    assert h.subs.aura_topics() == ["board/topic-b"]


def test_no_aura_turned_on_loads_nothing_and_subscribes_nothing() -> None:
    h = _Harness(on=lambda _n: False)
    h.loader.on_resolve("tamsin")
    assert h.attached == [] and h.subs.aura_topics() == []


def test_a_claude_p_child_with_the_parents_session_id_loads_none() -> None:
    h = _Harness(ancestors=frozenset({999, 1}))
    with structlog.testing.capture_logs() as logs:
        h.loader.on_resolve("tamsin")
        h.loader.on_resolve("tamsin")
    assert h.attached == [] and h.subs.aura_topics() == [] and h.discovered == 0
    assert [e["reason"] for e in logs if e["event"] == "auras_not_loaded"] == ["not_an_ancestor"]


def test_an_unreadable_ancestor_chain_loads_none_and_says_so() -> None:
    h = _Harness(ancestors=frozenset())
    with structlog.testing.capture_logs() as logs:
        h.loader.on_resolve("tamsin")
    assert h.attached == []
    assert [e["reason"] for e in logs if e["event"] == "auras_not_loaded"] == ["ancestry_unreadable"]


def test_the_entry_vanishing_between_the_name_and_the_pid_read_waits_for_the_next_resolve() -> None:
    h = _Harness(entry=None)
    h.loader.on_resolve("tamsin")
    assert h.attached == [] and not h.loader.loaded


def test_an_aura_past_the_topic_cap_is_refused_by_name() -> None:
    h = _Harness(auras=_auras("fits", "over"))
    for i in range(MAX_BOARD_TOPICS - 1):
        h.subs.subscribe(f"board/t{i}", templates=[], store_factory=lambda: None, state_dir=None)  # type: ignore[arg-type]
    with structlog.testing.capture_logs() as logs:
        h.loader.on_resolve("tamsin")
    assert [a.name for a in h.attached[0]] == ["fits"]
    refused = [e for e in logs if e["event"] == "aura_refused"]
    assert [e["aura"] for e in refused] == ["over"] and "32" in refused[0]["reason"]


@pytest.mark.parametrize("bad", [OSError("disk"), RuntimeError("bug")])
def test_a_discovery_failure_loads_none_and_never_raises(bad) -> None:
    h = _Harness()

    def boom():
        raise bad

    h.loader._discover = boom  # noqa: SLF001
    h.loader.on_resolve("tamsin")
    assert h.attached == []
