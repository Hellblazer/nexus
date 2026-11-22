# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.11): ``SubscriptionSet``
validation, mutation, the change-observer signal, T1 persistence across a
resume vs. a clear, and (kept for the aura loader) the read of Claude Code's
session registry.

RDR-253 Phase 2 (bead nexus-hczdo.2.1) retired the ``directory/<name>`` name
lease and the session mailbox: the one delivered mailbox is the identity's, a
``mailbox/<name>`` subscribe is refused, and nothing here writes to the
engine, so every set below is exercised with a POISON ``store_factory`` that
raises if ever called. The resume/clear persistence test runs against the real
engine substrate (``t2_service_env``) and a real T1 handle.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

import pytest

from nexus.db.t2.http_tuple_store import SchemaViolationError
from nexus.mcp.subscriptions import (
    MAX_BOARD_TOPICS,
    SubscriptionSet,
    load,
    persist,
)

#: The identity every set below is bound to: the one delivered mailbox is the identity's (RDR-253 Phase 2).
IDENT = "i5"


def _poison_store_factory():
    """A store_factory that fails the test if ever invoked -- the
    falsifier for "this path makes no engine call"."""
    def _boom():
        raise AssertionError("store_factory must not be called on this path")
    return _boom


#: A small fixture registry: enough for `_take_enabled`'s literal-before-
#: pattern resolution to exercise every branch this module's tests need,
#: without needing a real engine's `/registry` response.
_FIXTURE_TEMPLATES = [
    {"name": "board/<topic>", "take": {"enabled": False}},
    {"name": "queue/<name>", "take": {"enabled": True}},
    {"name": "lock/<resource>", "take": {"enabled": True}},
    {"name": "mailbox/<address>", "take": {"enabled": True}},
    {"name": "directory/<name>", "take": {"enabled": False}},
    {"name": "board/ci/<topic>", "take": {"enabled": False}},
]


class TestSubscribeValidation:
    def test_queue_is_refused_naming_in_and_the_list_is_unchanged(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        with pytest.raises(SchemaViolationError, match="`in`"):
            s.subscribe(
                "queue/builds", templates=_FIXTURE_TEMPLATES,
            )
        assert s.entries() == [{"subspace": s.identity_mailbox}]

    def test_lock_is_refused_naming_in(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        with pytest.raises(SchemaViolationError, match="`in`"):
            s.subscribe(
                "lock/release-train", templates=_FIXTURE_TEMPLATES,
            )
        assert len(s.entries()) == 1

    def test_a_subspace_that_is_neither_board_nor_mailbox_is_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        with pytest.raises(SchemaViolationError):
            s.subscribe(
                "directory/some-name", templates=_FIXTURE_TEMPLATES,
            )

    def test_empty_subspace_is_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        with pytest.raises(SchemaViolationError):
            s.subscribe("", templates=[])

    def test_mailbox_with_no_name_is_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        with pytest.raises(SchemaViolationError):
            s.subscribe("mailbox/", templates=[])



class TestMailboxSubscribeIsRefused:
    """RDR-253 Phase 2: no ``mailbox/<name>`` is subscribable; a refusal makes no engine call and writes no file."""

    @pytest.mark.parametrize("subspace", ["mailbox/nexus-23", "mailbox/evil\nname", "mailbox/evil/name", "mailbox/00552b58-87e2-4a8b-a69c-6ed0963a62ec"])
    def test_a_mailbox_that_is_not_the_identitys_is_refused_with_no_engine_call_or_file(self, tmp_path, subspace):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        with pytest.raises(SchemaViolationError, match="not subscribable.*identity mailbox"):
            s.subscribe(subspace, templates=[])
        assert list(tmp_path.rglob("*")) == []
        assert s.entries() == [{"subspace": "mailbox/i5"}] and s.version == 0

    def test_the_refusal_before_a_bind_says_the_identity_mailbox_comes_once_the_session_is_bound(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError, match="once the session is bound"):
            s.subscribe("mailbox/anything", templates=[])

    def test_subscribing_the_identity_mailbox_itself_is_a_noop(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        s.subscribe("mailbox/i5", templates=[])
        assert s.version == 0 and s.entries() == [{"subspace": "mailbox/i5"}]

    def test_a_set_has_no_lease_surface(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        for gone in ("leased_name", "arm_session_name", "shutdown", "session_mailbox"):
            assert not hasattr(s, gone), gone

    def test_a_record_written_with_a_leased_name_still_loads_and_the_name_is_dropped(self):
        restored = SubscriptionSet.from_json(
            {"session_id": "sess-a", "leased_name": "nexus-9", "board": ["board/x"]}
        )
        assert [e["subspace"] for e in restored.entries()] == ["board/x"]
        assert "leased_name" not in restored.to_json()

    def test_thirty_second_board_topic_accepted_thirty_third_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        for i in range(MAX_BOARD_TOPICS):
            s.subscribe(f"board/topic-{i}", templates=[])
        assert len(s.entries()) == MAX_BOARD_TOPICS + 1  # + the session mailbox
        with pytest.raises(SchemaViolationError):
            s.subscribe(f"board/topic-{MAX_BOARD_TOPICS}", templates=[])
        assert len(s.entries()) == MAX_BOARD_TOPICS + 1  # unchanged by the refusal

    def test_re_subscribing_the_same_board_topic_is_idempotent(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        s.subscribe("board/release-notes", templates=[])
        version_after_first = s.version
        s.subscribe("board/release-notes", templates=[])
        assert s.version == version_after_first
        assert len(s.entries()) == 2



class TestObserver:
    def test_observer_sees_each_change_with_the_new_list(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        seen: list[tuple[int, set[str]]] = []
        s.add_listener(lambda subs: seen.append((subs.version, {e["subspace"] for e in subs.entries()})))

        s.subscribe("board/release-notes", templates=[])
        assert len(seen) == 1
        version, subspaces = seen[0]
        assert version == 1
        assert "board/release-notes" in subspaces

        s.unsubscribe("board/release-notes")
        assert len(seen) == 2
        assert seen[1][0] == 2
        assert "board/release-notes" not in seen[1][1]

    def test_removed_listener_is_not_called_again(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        calls = []
        cb = lambda subs: calls.append(subs.version)  # noqa: E731 — test-local
        s.add_listener(cb)
        s.subscribe("board/a", templates=[])
        s.remove_listener(cb)
        s.subscribe("board/b", templates=[])
        assert calls == [1]


class TestUnsubscribe:
    def test_removes_a_board_topic(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        s.subscribe("board/release-notes", templates=[])
        assert len(s.entries()) == 2
        s.unsubscribe("board/release-notes")
        assert s.entries() == [{"subspace": s.identity_mailbox}]

    def test_identity_mailbox_cannot_be_unsubscribed(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        with pytest.raises(SchemaViolationError):
            s.unsubscribe(s.identity_mailbox)

    def test_unsubscribing_something_never_subscribed_is_a_noop(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()), identity=IDENT)
        s.unsubscribe("board/never-subscribed")  # must not raise


class TestPersistenceAcrossResumeAndClear:
    def test_resume_restores_the_list_and_a_clear_starts_fresh(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        from nexus.db.t1 import get_t1_database, mint_t1_session_token

        session_a = str(uuid.uuid4())
        minted_a = mint_t1_session_token(session_a, context="rdr211 subscriptions test")
        monkeypatch.setenv("NX_T1_SESSION", minted_a["session_token"])
        monkeypatch.setenv("NX_T1_SESSION_ID", session_a)
        t1_a = get_t1_database()

        subs = load(t1_a, session_a)
        subs.subscribe(
            "board/release-notes", templates=[],
        )
        persist(t1_a, subs)

        # A FRESH set instance for the SAME session id loads it back -- the
        # resume path (a new process, same session id).
        fresh_a = load(t1_a, session_a)
        names = {e["subspace"] for e in fresh_a.entries()}
        assert "board/release-notes" in names
        assert "mailbox/i5" not in names, "the identity is read from the token file at each load, never stored"

        # A DIFFERENT session id (the clear path) sees an empty list: T1 itself
        # is session-scoped, so it never finds session A's persisted row at all.
        session_b = str(uuid.uuid4())
        minted_b = mint_t1_session_token(session_b, context="rdr211 subscriptions test")
        monkeypatch.setenv("NX_T1_SESSION", minted_b["session_token"])
        monkeypatch.setenv("NX_T1_SESSION_ID", session_b)
        t1_b = get_t1_database()

        assert load(t1_b, session_b).entries() == []


# ── bead nexus-zxthy: a board topic's start position ─────────────────────


class TestBoardStartPosition:
    """A fresh board subscription records its subscribe time and pushes
    only what is created after it (T2 nexus/zxthy-board-subscription-
    design-2026-09-27): the shape every mature system defaults to (Kafka
    `latest`, Redis Streams `$`, NATS `DeliverNew`)."""

    def test_subscribe_records_a_utc_start_position_that_entries_expose(self) -> None:
        from datetime import UTC, datetime

        from nexus.mcp.subscriptions import SubscriptionSet

        before = datetime.now(UTC)
        s = SubscriptionSet(session_id="sess-a")
        s.subscribe("board/ci/nexus-develop", templates=[])
        [board] = s.entries()
        assert board["subspace"] == "board/ci/nexus-develop"
        since = datetime.fromisoformat(board["since"])
        assert since.tzinfo is not None
        assert before <= since <= datetime.now(UTC)

    def test_start_position_survives_the_json_round_trip_a_resume_makes(self) -> None:
        from nexus.mcp.subscriptions import SubscriptionSet

        s = SubscriptionSet(session_id="sess-a")
        s.subscribe("board/ci/nexus-develop", templates=[])
        [board] = s.entries()
        data = s.to_json()
        assert data["board"] == ["board/ci/nexus-develop"]  # the older list shape is kept
        assert data["board_since"] == {"board/ci/nexus-develop": board["since"]}

        restored = SubscriptionSet.from_json(data)
        assert restored.entries() == s.entries()

    def test_a_record_written_before_the_start_position_delivers_everything(self) -> None:
        from nexus.mcp.subscriptions import SubscriptionSet

        restored = SubscriptionSet.from_json({"session_id": "sess-a", "board": ["board/x"]})
        assert restored.entries() == [{"subspace": "board/x"}]

    def test_resubscribing_keeps_the_original_start_position(self) -> None:
        from nexus.mcp.subscriptions import SubscriptionSet

        s = SubscriptionSet(session_id="sess-a")
        s.subscribe("board/x", templates=[])
        first = s.entries()[0]["since"]
        s.subscribe("board/x", templates=[])
        assert s.entries()[0]["since"] == first


# ── nexus-u839f: the session's own name is re-armed without a model step ────


def _write_session_file(sessions_dir, pid, **fields) -> None:
    import json

    sessions_dir.mkdir(parents=True, exist_ok=True)
    body = {"pid": pid, "kind": "interactive", "updatedAt": 1, **fields}
    (sessions_dir / f"{pid}.json").write_text(json.dumps(body), encoding="utf-8")


def _log_everything() -> None:
    """The suite filters structlog at WARNING; the INFO/DEBUG lines these
    tests pin are dropped before `capture_logs` sees them. The autouse
    `_restore_structlog_after_test` undoes this."""
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll *predicate* until true: the registry arm sends from the lease
    thread, so a test observes the engine call a moment after the arm."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


#: Every key a real Claude Code registry entry carries (read as a key list from a
#: live ``~/.claude/sessions/*.json`` on 2026-10-22, Claude Code 2.x; values are
#: dummies). Some entries also carry bridgeSessionId, hostSessionId, jobId and
#: parkedJobId. resolve_session_name must read this shape, not just the five
#: fields it uses.
_REAL_SHAPE_KEYS = {
    "cwd": "/work/project", "entrypoint": "cli", "messagingSocketPath": "/tmp/peer.sock",
    "nameSince": 1_700_000_000_000, "nameSource": "user", "peerFeatures": ["a", "b"],
    "peerProtocol": 1, "pidDomain": "host", "procStart": "Thu Oct 22 10:00:00 2026",
    "startedAt": 1_700_000_000_000, "status": "idle", "statusUpdatedAt": 1_700_000_001_000,
    "tmux": "", "version": "2.1.0",
}


class TestResolveSessionName:
    """`resolve_session_name` reads Claude Code's own session registry
    (`<config>/sessions/<pid>.json`) for the name of the live interactive
    session whose id this server holds. Every miss is None, never a raise."""

    def _alive(self, *pids):
        return lambda pid: pid in pids

    def test_matches_on_session_id_for_a_live_interactive_entry(self, tmp_path) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        _write_session_file(tmp_path, 11, sessionId=sid, name="nexus_3dc")
        _write_session_file(tmp_path, 12, sessionId=str(uuid.uuid4()), name="someone-else")
        assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=self._alive(11, 12)) == "nexus_3dc"

    def test_a_dead_pid_a_non_interactive_kind_or_no_name_is_no_match(self, tmp_path) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        _write_session_file(tmp_path, 21, sessionId=sid, name="dead-holder")
        _write_session_file(tmp_path, 22, sessionId=sid, name="sdk-child", kind="sdk")
        _write_session_file(tmp_path, 23, sessionId=sid)
        _write_session_file(tmp_path, 24, sessionId=sid, name="   ")
        assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=self._alive(22, 23, 24)) is None

    def test_the_most_recently_updated_live_entry_wins(self, tmp_path) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        # Both orders: the newer entry in the file that sorts first fails "last file
        # wins", and in the file that sorts last fails "first file wins".
        _write_session_file(tmp_path, 31, sessionId=sid, name="new-name", updatedAt=9)
        _write_session_file(tmp_path, 32, sessionId=sid, name="old-name", updatedAt=5)
        assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=self._alive(31, 32)) == "new-name"
        sid2 = str(uuid.uuid4())
        _write_session_file(tmp_path, 41, sessionId=sid2, name="old-name-2", updatedAt=5)
        _write_session_file(tmp_path, 42, sessionId=sid2, name="new-name-2", updatedAt=9)
        assert resolve_session_name(sid2, sessions_dir=tmp_path, pid_alive=self._alive(41, 42)) == "new-name-2"

    def test_a_full_real_shape_entry_resolves(self, tmp_path) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        _write_session_file(tmp_path, 61, sessionId=sid, name="nexus_3dc", **_REAL_SHAPE_KEYS)
        assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=self._alive(61)) == "nexus_3dc"

    def test_a_pid_that_overflows_the_liveness_probe_is_a_miss_not_a_raise(self, tmp_path) -> None:
        from nexus.daemon.service_registry import pid_alive as real_pid_alive
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        _write_session_file(tmp_path, 10**30, sessionId=sid, name="huge-pid")
        # the real probe, not a fake: pid_alive(10**30) raises OverflowError
        assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=real_pid_alive) is None

    def test_a_huge_updated_at_and_a_deeply_nested_file_are_skipped_not_raised(self, tmp_path) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        (tmp_path / "71.json").write_text(
            '{"sessionId": "%s", "kind": "interactive", "name": "n", "pid": 71, "updatedAt": %s}' % (sid, "9" * 400),
            encoding="utf-8",
        )
        (tmp_path / "72.json").write_text("[" * 200_000, encoding="utf-8")
        assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=self._alive(71, 72)) is None

    def test_a_miss_on_an_existing_dir_logs_one_info_line_without_contents(self, tmp_path) -> None:
        from structlog.testing import capture_logs

        from nexus.mcp.subscriptions import resolve_session_name

        _log_everything()
        sid = str(uuid.uuid4())
        _write_session_file(tmp_path, 81, sessionId=str(uuid.uuid4()), name="SECRET-NAME-OF-ANOTHER")
        _write_session_file(tmp_path, 82, sessionId=str(uuid.uuid4()), name="another")
        with capture_logs() as logs:
            assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=self._alive(81, 82)) is None
        [line] = [e for e in logs if e["event"] == "session_name_unresolved"]
        assert line["log_level"] == "info"
        assert line["session_id"] == sid and line["sessions_dir"] == str(tmp_path) and line["files_read"] == 2
        assert "SECRET-NAME-OF-ANOTHER" not in repr(logs)

    def test_a_missing_dir_and_log_miss_false_log_nothing(self, tmp_path) -> None:
        from structlog.testing import capture_logs

        from nexus.mcp.subscriptions import resolve_session_name

        _log_everything()
        with capture_logs() as logs:
            assert resolve_session_name("s", sessions_dir=tmp_path / "absent", pid_alive=self._alive()) is None
            assert resolve_session_name("s", sessions_dir=tmp_path, pid_alive=self._alive(), log_miss=False) is None
        assert [e for e in logs if e["event"] == "session_name_unresolved"] == []

    def test_missing_dir_unreadable_and_malformed_files_are_quiet_misses(self, tmp_path) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        assert resolve_session_name(sid, sessions_dir=tmp_path / "absent", pid_alive=self._alive(1)) is None
        (tmp_path / "1.json").write_text("not json{{", encoding="utf-8")
        (tmp_path / "2.json").write_text("[1, 2]", encoding="utf-8")
        (tmp_path / "3.json").write_text('{"sessionId": "%s", "name": 7, "pid": "x"}' % sid, encoding="utf-8")
        (tmp_path / "4.key").write_text("secret", encoding="utf-8")
        assert resolve_session_name(sid, sessions_dir=tmp_path, pid_alive=self._alive(1, 2, 3)) is None

    def test_an_empty_session_id_matches_nothing(self, tmp_path) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        _write_session_file(tmp_path, 41, sessionId="", name="x")
        assert resolve_session_name("", sessions_dir=tmp_path, pid_alive=self._alive(41)) is None

    def test_the_default_dir_follows_claude_config_dir(self, tmp_path, monkeypatch) -> None:
        from nexus.mcp.subscriptions import resolve_session_name

        sid = str(uuid.uuid4())
        _write_session_file(tmp_path / "sessions", 51, sessionId=sid, name="from-env")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert resolve_session_name(sid, pid_alive=self._alive(51)) == "from-env"


# ── RDR-249: the server reports the registry's name to the aura loader, with no lease ─────────────────────────────


class TestTheServerWatchesTheRegistryNameForTheAuras:
    """``_start_channel_waiter`` starts a name watch, not a name lease (RDR-253 Phase 2): the registry name is read at
    once and every 60 s until the loader has decided or the waiter is no longer the session's, and nothing is written
    to the engine."""

    @pytest.fixture
    def server(self, tmp_path, monkeypatch):
        import os

        from nexus.mcp import core as _core

        sid = str(uuid.uuid4())
        started: list[object] = []
        names: list = []

        class _T1:
            def list_entries(self):
                return []

        class _Waiter:
            def __init__(self, *args, **kwargs) -> None:
                self.session_id = args[0]

            def start(self) -> None:
                started.append(self)

        class _OnName:
            loaded_flag = False

            def __call__(self, name) -> None:
                names.append(name)

            def loaded(self) -> bool:
                return self.loaded_flag

        on_name = _OnName()
        monkeypatch.delenv("NX_MCP_PROBE", raising=False)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_T1_SESSION_ID", sid)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setattr(_core, "_DEFERRED_T1_MINT", {})
        monkeypatch.setattr(_core, "_get_t1", lambda: (_T1(), None))
        monkeypatch.setattr(_core, "_t2_ctx", _poison_store_factory())
        monkeypatch.setattr(_core, "_aura_loader", lambda _w, _s: on_name)
        monkeypatch.setattr(_core._channel, "ChannelWaiter", _Waiter)
        sessions = tmp_path / "claude" / "sessions"
        # this process's own pid: alive, so the default liveness check passes
        _write_session_file(sessions, os.getpid(), sessionId=sid, name="nexus_3dc")
        try:
            yield _core, sid, started, names, on_name
        finally:
            _core._channel.unregister_active_waiter(sid)
            _core._subscriptions.reset_cache(sid)

    def test_starting_the_waiter_reports_the_registry_name_and_writes_nothing(self, server) -> None:
        core, sid, started, names, _on_name = server
        core._start_channel_waiter()
        assert len(started) == 1
        assert _wait_for(lambda: names == ["nexus_3dc"])  # _t2_ctx is a poison factory: an engine write would raise

    def test_a_miss_is_reported_as_none_and_retried_on_the_next_poll(self, server, tmp_path, monkeypatch) -> None:
        core, sid, started, names, _on_name = server
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-such-claude-dir"))
        core._start_channel_waiter()
        assert len(started) == 1
        assert _wait_for(lambda: names == [None])

    def test_the_watch_re_reads_until_the_loader_has_loaded_then_stops(self, server, tmp_path, monkeypatch) -> None:
        core, sid, started, names, on_name = server
        monkeypatch.setattr(core, "SESSION_NAME_POLL_S", 0.02)
        core._start_channel_waiter()
        core._channel.register_active_waiter(started[0])
        assert _wait_for(lambda: len(names) >= 3), "re-read on every poll while the loader has not decided"
        on_name.loaded_flag = True
        count = len(names)
        time.sleep(0.2)
        assert len(names) <= count + 1, "stopped once the loader said it had loaded"

    def test_the_watch_stops_when_its_waiter_is_no_longer_the_active_one(self, server, monkeypatch) -> None:
        core, sid, started, names, on_name = server
        monkeypatch.setattr(core, "SESSION_NAME_POLL_S", 0.02)
        thread = core._watch_session_name(started and started[0] or type("W", (), {"session_id": sid})(), on_name, poll_s=0.02)
        assert thread is not None
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "an unregistered waiter ends the watch at its first check"

    def test_a_second_start_with_a_live_waiter_starts_nothing_more(self, server) -> None:
        core, _sid, started, names, _on_name = server
        core._start_channel_waiter()
        core._channel.register_active_waiter(started[0])
        core._start_channel_waiter()
        assert len(started) == 1

    def test_a_reader_failure_never_stops_the_waiter_or_the_watch(self, server, monkeypatch) -> None:
        core, _sid, started, names, _on_name = server

        def _boom(*_a, **_k):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(core._subscriptions, "resolve_session_name", _boom)
        core._start_channel_waiter()
        assert len(started) == 1


class TestRegistryNameReaderLogsAMissOncePerRun:
    def test_session_name_unresolved_is_not_repeated_by_the_sixty_second_re_read(self, tmp_path, monkeypatch) -> None:
        from structlog.testing import capture_logs

        from nexus.mcp import core as _core

        _log_everything()
        sid = str(uuid.uuid4())
        _write_session_file(tmp_path / "sessions", 91, sessionId=str(uuid.uuid4()), name="other")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        resolve = _core._registry_name_reader(sid)
        with capture_logs() as logs:
            assert resolve() is None
            assert resolve() is None
            assert resolve() is None
        assert [e["event"] for e in logs].count("session_name_unresolved") == 1
        # found, then lost again: that miss is news and logs once more
        entry = tmp_path / "sessions" / "92.json"
        _write_session_file(tmp_path / "sessions", 92, sessionId=sid, name="back")
        import nexus.daemon.service_registry as _sr

        monkeypatch.setattr(_sr, "pid_alive", lambda _p: True)
        with capture_logs() as logs:
            assert resolve() == "back"
            entry.unlink()
            assert resolve() is None
            assert resolve() is None
        assert [e["event"] for e in logs].count("session_name_unresolved") == 1


# --- RDR-253 Phase 2: the identity mailbox is the one delivered mailbox --------------------------------


def test_a_session_with_an_identity_is_delivered_its_identity_mailbox_alone() -> None:
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    sid = "11111111-2222-4333-8444-555555555555"
    subs = SubscriptionSet(session_id=sid, identity="i12")
    assert [e["subspace"] for e in subs.entries()] == ["mailbox/i12"]
    assert not hasattr(subs, "session_mailbox"), "a session id is not a mail address"
    assert SubscriptionSet(session_id=sid).entries() == [], "no identity, no mailbox"


def test_setting_the_identity_bumps_the_set_so_the_waiter_rereads_it() -> None:
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    subs = SubscriptionSet(session_id="11111111-2222-4333-8444-555555555555")
    seen: list[int] = []
    subs._listeners.append(lambda s: seen.append(s.version))  # noqa: SLF001 -- the observer hook the waiter uses
    subs.set_identity("i12")
    subs.set_identity("i12")  # no change, no bump
    assert seen == [1] and subs.identity_mailbox == "mailbox/i12"
    subs.set_identity(None)
    assert seen == [1, 2] and subs.entries() == []


def test_the_identity_mailbox_cannot_be_unsubscribed_and_a_session_mailbox_is_nothing_to_remove() -> None:
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    sid = "11111111-2222-4333-8444-555555555555"
    subs = SubscriptionSet(session_id=sid, identity="i12")
    with pytest.raises(SchemaViolationError, match="identity mailbox cannot be unsubscribed"):
        subs.unsubscribe("mailbox/i12")
    subs.unsubscribe(f"mailbox/{sid}")  # not subscribed, so a plain no-op
    assert [e["subspace"] for e in subs.entries()] == ["mailbox/i12"]


def test_a_set_loaded_before_the_token_file_follows_the_lease_when_it_adopts_an_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP server and the bind hook start together, so the set usually loads with no identity; the lease
    tells it when it adopts one (a late token file), and again on a switch."""
    from nexus.mcp import identity_lease, subscriptions  # noqa: PLC0415

    monkeypatch.setattr(identity_lease, "_IDENTITY_LISTENERS", [])
    monkeypatch.setattr(identity_lease, "_LEASE", None)
    subs = subscriptions.SubscriptionSet(session_id="11111111-2222-4333-8444-555555555555")
    subscriptions._follow_identity(subs)  # noqa: SLF001 -- what get_or_load does on first load
    assert subs.identity_mailbox is None
    identity_lease._notify_identity("i12")  # noqa: SLF001 -- what IdentityLease._adopt does on a change
    assert [e["subspace"] for e in subs.entries()] == ["mailbox/i12"]
    identity_lease._notify_identity("i13")  # noqa: SLF001 -- a cross-seat switch
    assert subs.identity_mailbox == "mailbox/i13"
    identity_lease._notify_identity("not-an-id")  # noqa: SLF001
    assert subs.identity_mailbox is None
