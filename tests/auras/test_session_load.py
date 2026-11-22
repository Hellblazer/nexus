# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 TD1 and TD3 loading (nexus-mdbds.11.1 part A, rules 1 to 4): auras
load when the session registry first names the session, a later re-resolve
included; a host with no registry and a claude -p child load none and say
why; an aura past MAX_BOARD_TOPICS is refused by name."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import structlog

from nexus.auras import parse_aura
from nexus.auras.session import AuraLoader, registered_owner
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
        parse_aura(
            minimal(n).replace('subspace = "board/topic"', f'subspace = "{topic}-{n}"')
            .replace('threads = "a block"', f'threads = "block {n}"').encode(),
            name=n,
        )
        for n in names
    ]


class _Harness:
    def __init__(self, *, auras=None, on=None, entry=(("tamsin", _PARENT)), owner=_PARENT):
        self.subs = SubscriptionSet(session_id=_SID)
        self.attached: list = []
        self.discovered = 0
        self.owner_calls = 0
        auras = _auras("a", "b") if auras is None else auras

        def discover():
            self.discovered += 1
            return auras

        def registered_owner():
            self.owner_calls += 1
            return owner

        self.loader = AuraLoader(
            _SID, self.subs,
            discover=discover,
            is_on=(on or (lambda _n: True)),
            entry=lambda _sid: entry,
            registered_owner=registered_owner,
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


def test_no_aura_turned_on_loads_nothing_and_never_walks_the_process_table() -> None:
    h = _Harness(on=lambda _n: False)
    h.loader.on_resolve("tamsin")
    assert h.attached == [] and h.subs.aura_topics() == []
    assert h.owner_calls == 0 and h.loader.loaded


def test_a_claude_p_child_with_the_parents_session_id_loads_none() -> None:
    """The child's own claude writes its own registry file and is nearer than the session's claude."""
    h = _Harness(owner=_ME + 1)
    with structlog.testing.capture_logs() as logs:
        h.loader.on_resolve("tamsin")
        h.loader.on_resolve("tamsin")
    assert h.attached == [] and h.subs.aura_topics() == []
    assert [e["reason"] for e in logs if e["event"] == "auras_not_loaded"] == ["not_this_sessions_process"]


def test_an_unreadable_ancestor_chain_loads_none_and_says_so() -> None:
    h = _Harness(owner=None)
    with structlog.testing.capture_logs() as logs:
        h.loader.on_resolve("tamsin")
    assert h.attached == [] and h.subs.aura_topics() == []
    assert [e["reason"] for e in logs if e["event"] == "auras_not_loaded"] == ["ancestry_unreadable"]


def test_the_entry_vanishing_between_the_name_and_the_pid_read_waits_for_the_next_resolve() -> None:
    h = _Harness(entry=None)
    h.loader.on_resolve("tamsin")
    assert h.attached == [] and not h.loader.loaded


def test_a_second_aura_writing_the_same_threads_block_is_refused_by_name() -> None:
    same = [
        parse_aura(minimal(n).replace('subspace = "board/topic"', f'subspace = "board/t-{n}"').encode(), name=n)
        for n in ("first", "second")
    ]
    h = _Harness(auras=same)
    with structlog.testing.capture_logs() as logs:
        h.loader.on_resolve("tamsin")
    assert [a.name for a in h.attached[0]] == ["first"]
    assert h.subs.aura_topics() == ["board/t-first"]
    assert [e["aura"] for e in logs if e["event"] == "aura_refused"] == ["second"]


def test_a_load_that_fails_after_subscribing_releases_the_topics() -> None:
    h = _Harness()

    def broken(_loaded):
        raise RuntimeError("runtime bug")

    h.loader._make_runtime = broken  # noqa: SLF001
    h.loader.on_resolve("tamsin")
    assert h.attached == [] and h.subs.aura_topics() == [] and h.loader.loaded


def test_an_aura_past_the_topic_cap_is_refused_by_name() -> None:
    h = _Harness(auras=_auras("fits", "over"))
    for i in range(MAX_BOARD_TOPICS - 1):
        h.subs.subscribe(f"board/t{i}", templates=[])  # type: ignore[arg-type]
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
    assert h.attached == [] and h.loader.loaded


class TestRegisteredOwner:
    """The walk up from the MCP server: the first process with a registry file of any kind."""

    def _registry(self, tmp_path: Path, *pids: int) -> Path:
        for pid in pids:
            (tmp_path / f"{pid}.json").write_text("{}", encoding="utf-8")
        return tmp_path

    def test_the_sessions_own_claude_is_the_owner(self, tmp_path: Path) -> None:
        parents = {_ME: 500, 500: _PARENT, _PARENT: 1}
        assert registered_owner(_ME, self._registry(tmp_path, _PARENT), ppid_of=parents.get) == _PARENT

    def test_a_claude_p_child_started_from_the_session_is_its_own_owner(self, tmp_path: Path) -> None:
        """mcp -> child claude (its own registry file) -> bash -> session claude (the interactive entry)."""
        child_claude, bash = 600, 601
        parents = {_ME: child_claude, child_claude: bash, bash: _PARENT, _PARENT: 1}
        sessions = self._registry(tmp_path, child_claude, _PARENT)
        assert registered_owner(_ME, sessions, ppid_of=parents.get) == child_claude

    def test_no_registered_process_up_to_the_root_is_none(self, tmp_path: Path) -> None:
        assert registered_owner(_ME, tmp_path, ppid_of={_ME: 500, 500: 1}.get) is None

    def test_an_unreadable_parent_is_none(self, tmp_path: Path) -> None:
        def unreadable(_pid: int) -> int:
            raise OSError("no parent readable")

        assert registered_owner(_ME, self._registry(tmp_path, _PARENT), ppid_of=unreadable) is None

    def test_a_cycle_ends_the_walk(self, tmp_path: Path) -> None:
        assert registered_owner(_ME, tmp_path, ppid_of={_ME: 500, 500: _ME}.get) is None
