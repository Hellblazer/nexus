# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.11): ``SubscriptionSet``
validation, mutation, the change-observer signal, the instance-mailbox
takeover, and T1 persistence across a resume vs. a clear.

Validation-only tests (queue/lock refusal naming ``in``, the 32-topic
bound, a malformed subspace, the observer, plain unsubscribe) never touch
a real store: a POISON ``store_factory`` that raises if ever called is the
falsifier proving the refusal happens before any engine call. The
instance-mailbox takeover, the directory-lease send/re-send, and the
resume/clear persistence tests run against the real engine substrate
(``t2_service_env``) and a real T1 handle, mirroring
``tests/test_mcp_tuple_tools.py``'s own fixtures.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import datetime

import pytest

from nexus.db.t2.http_tuple_store import SchemaViolationError
from nexus.mcp.subscriptions import (
    DIRECTORY_TTL_S,
    MAX_BOARD_TOPICS,
    SubscriptionSet,
    load,
    persist,
    registration_path,
    write_instance_registration,
)


def _poison_store_factory():
    """A store_factory that fails the test if ever invoked -- the
    falsifier for "this refusal happens before any engine call"."""
    def _boom():
        raise AssertionError("store_factory must not be called on this refusal path")
    return _boom


class _FakeTuples:
    """A minimal HttpTupleStore-shaped stand-in: records every `out()` call
    instead of hitting a real engine, for tests that need the instance-
    mailbox takeover's synchronous first arm to actually happen, but do
    not need to prove real engine behavior (that is the substrate-backed
    class below)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def out(self, subspace, keys, dims=None, body=None, *, nonce=None, ttl_seconds=None):
        self.calls.append((subspace, keys, dims, nonce, ttl_seconds))
        return "fake-tuple-id"


@contextmanager
def _fake_store_factory(fake: _FakeTuples):
    class _Db:
        tuples = fake
    yield _Db()


#: A small fixture registry: enough for `_take_enabled`'s literal-before-
#: pattern resolution to exercise every branch this module's tests need,
#: without needing a real engine's `/registry` response.
_FIXTURE_TEMPLATES = [
    {"name": "board/<topic>", "take": {"enabled": False}},
    {"name": "queue/<name>", "take": {"enabled": True}},
    {"name": "lock/<resource>", "take": {"enabled": True}},
    {"name": "mailbox/<address>", "take": {"enabled": True}},
    {"name": "directory/<name>", "take": {"enabled": False}},
    {"name": "ledger/<session_id>", "take": {"enabled": False}},
]


class TestSubscribeValidation:
    def test_queue_is_refused_naming_in_and_the_list_is_unchanged(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError, match="`in`"):
            s.subscribe(
                "queue/builds", templates=_FIXTURE_TEMPLATES,
                store_factory=_poison_store_factory(), state_dir=tmp_path,
            )
        assert s.entries() == [{"subspace": s.session_mailbox, "cursor": None}]

    def test_lock_is_refused_naming_in(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError, match="`in`"):
            s.subscribe(
                "lock/release-train", templates=_FIXTURE_TEMPLATES,
                store_factory=_poison_store_factory(), state_dir=tmp_path,
            )
        assert len(s.entries()) == 1

    def test_a_subspace_that_is_neither_board_nor_mailbox_is_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError):
            s.subscribe(
                "directory/some-name", templates=_FIXTURE_TEMPLATES,
                store_factory=_poison_store_factory(), state_dir=tmp_path,
            )

    def test_empty_subspace_is_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError):
            s.subscribe("", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path)

    def test_mailbox_with_no_name_is_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError):
            s.subscribe("mailbox/", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path)

    def test_mailbox_name_with_a_newline_is_refused_before_any_write_or_lease(self, tmp_path):
        """Code review Minor 6: an instance name outside the mailbox
        address charset must be refused loudly (`SchemaViolationError`),
        before `write_instance_registration` or the `directory/<name>`
        lease -- `_poison_store_factory` is the falsifier that no lease
        was ever started."""
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError):
            s.subscribe(
                "mailbox/evil\nname", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path,
            )
        assert list(tmp_path.rglob("*")) == []

    def test_mailbox_name_with_a_slash_is_refused_before_any_write_or_lease(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError):
            s.subscribe(
                "mailbox/evil/name", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path,
            )
        assert list(tmp_path.rglob("*")) == []

    def test_write_instance_registration_itself_rejects_a_charset_hostile_instance(self, tmp_path):
        """Defense in depth: even a direct call to
        `write_instance_registration` -- bypassing `subscribe`'s loud
        refusal -- must never write a bad name to disk."""
        write_instance_registration(tmp_path, "sess-1", "evil/name")
        assert list(tmp_path.rglob("*")) == []

    def test_subscribing_the_sessions_own_mailbox_is_a_noop(self, tmp_path):
        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        s.subscribe(
            f"mailbox/{sid}", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path,
        )
        assert s.instance_mailbox is None
        assert s.version == 0

    def test_a_second_instance_mailbox_name_is_refused(self, tmp_path):
        fake = _FakeTuples()
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        s.subscribe(
            "mailbox/name-a", templates=[], store_factory=lambda: _fake_store_factory(fake),
            state_dir=tmp_path,
        )
        try:
            assert s.instance_mailbox == "mailbox/name-a"
            with pytest.raises(SchemaViolationError):
                s.subscribe(
                    "mailbox/name-b", templates=[], store_factory=_poison_store_factory(),
                    state_dir=tmp_path,
                )
            assert s.instance_mailbox == "mailbox/name-a"
        finally:
            s.shutdown()

    def test_re_subscribing_the_same_instance_mailbox_is_idempotent(self, tmp_path):
        fake = _FakeTuples()
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        factory = lambda: _fake_store_factory(fake)  # noqa: E731 — test-local, one use site
        s.subscribe("mailbox/name-a", templates=[], store_factory=factory, state_dir=tmp_path)
        try:
            version_after_first = s.version
            s.subscribe("mailbox/name-a", templates=[], store_factory=factory, state_dir=tmp_path)
            assert s.version == version_after_first  # a no-op does not bump version
        finally:
            s.shutdown()

    def test_thirty_second_board_topic_accepted_thirty_third_refused(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        poison = _poison_store_factory()
        for i in range(MAX_BOARD_TOPICS):
            s.subscribe(f"board/topic-{i}", templates=[], store_factory=poison, state_dir=tmp_path)
        assert len(s.entries()) == MAX_BOARD_TOPICS + 1  # + the session mailbox
        with pytest.raises(SchemaViolationError):
            s.subscribe(f"board/topic-{MAX_BOARD_TOPICS}", templates=[], store_factory=poison, state_dir=tmp_path)
        assert len(s.entries()) == MAX_BOARD_TOPICS + 1  # unchanged by the refusal

    def test_re_subscribing_the_same_board_topic_is_idempotent(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        poison = _poison_store_factory()
        s.subscribe("board/release-notes", templates=[], store_factory=poison, state_dir=tmp_path)
        version_after_first = s.version
        s.subscribe("board/release-notes", templates=[], store_factory=poison, state_dir=tmp_path)
        assert s.version == version_after_first
        assert len(s.entries()) == 2


class TestObserver:
    def test_observer_sees_each_change_with_the_new_list(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        seen: list[tuple[int, set[str]]] = []
        s.add_listener(lambda subs: seen.append((subs.version, {e["subspace"] for e in subs.entries()})))

        s.subscribe("board/release-notes", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path)
        assert len(seen) == 1
        version, subspaces = seen[0]
        assert version == 1
        assert "board/release-notes" in subspaces

        s.unsubscribe("board/release-notes")
        assert len(seen) == 2
        assert seen[1][0] == 2
        assert "board/release-notes" not in seen[1][1]

    def test_removed_listener_is_not_called_again(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        calls = []
        cb = lambda subs: calls.append(subs.version)  # noqa: E731 — test-local
        s.add_listener(cb)
        s.subscribe("board/a", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path)
        s.remove_listener(cb)
        s.subscribe("board/b", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path)
        assert calls == [1]


class TestUnsubscribe:
    def test_removes_a_board_topic(self, tmp_path):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        poison = _poison_store_factory()
        s.subscribe("board/release-notes", templates=[], store_factory=poison, state_dir=tmp_path)
        assert len(s.entries()) == 2
        s.unsubscribe("board/release-notes")
        assert s.entries() == [{"subspace": s.session_mailbox, "cursor": None}]

    def test_session_mailbox_cannot_be_unsubscribed(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError):
            s.unsubscribe(s.session_mailbox)

    def test_unsubscribing_something_never_subscribed_is_a_noop(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        s.unsubscribe("board/never-subscribed")  # must not raise


class TestInstanceMailboxTakeover:
    """Real engine (t2_service_env): the registration file, the directory
    lease's shape and re-send cadence, and unsubscribe stopping it."""

    def test_writes_registration_file_and_sends_the_directory_lease(
        self, t2_service_env, tmp_path,
    ) -> None:
        from nexus.mcp_infra import t2_ctx

        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        name = f"inst-{uuid.uuid4().hex[:8]}"
        try:
            s.subscribe(f"mailbox/{name}", templates=[], store_factory=t2_ctx, state_dir=tmp_path)
            assert s.instance_mailbox == f"mailbox/{name}"

            reg_path = registration_path(tmp_path, sid)
            assert reg_path.read_text(encoding="utf-8") == f"{name}\n"

            with t2_ctx() as db:
                rows = db.tuples.rd(f"directory/{name}", {"name": name})
            assert len(rows) == 1
            assert rows[0].dims["session_id"] == sid
            created = datetime.fromisoformat(rows[0].created_at)
            expires = datetime.fromisoformat(rows[0].expires_at)
            assert abs((expires - created).total_seconds() - DIRECTORY_TTL_S) < 5
        finally:
            s.shutdown()

    def test_refused_for_any_other_name(self, t2_service_env, tmp_path) -> None:
        from nexus.mcp_infra import t2_ctx

        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        try:
            s.subscribe(
                f"mailbox/inst-{uuid.uuid4().hex[:8]}", templates=[],
                store_factory=t2_ctx, state_dir=tmp_path,
            )
            with pytest.raises(SchemaViolationError):
                s.subscribe(
                    f"mailbox/other-{uuid.uuid4().hex[:8]}", templates=[],
                    store_factory=t2_ctx, state_dir=tmp_path,
                )
        finally:
            s.shutdown()

    def test_unsubscribe_stops_the_lease(self, t2_service_env, tmp_path) -> None:
        from nexus.mcp_infra import t2_ctx

        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        name = f"inst-{uuid.uuid4().hex[:8]}"
        s.subscribe(f"mailbox/{name}", templates=[], store_factory=t2_ctx, state_dir=tmp_path)
        assert s._lease_thread is not None
        s.unsubscribe(f"mailbox/{name}")
        assert s.instance_mailbox is None
        assert s._lease_thread is None

    def test_re_sends_on_the_configured_interval(self, t2_service_env, tmp_path) -> None:
        from nexus.mcp_infra import t2_ctx

        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        name = f"inst-{uuid.uuid4().hex[:8]}"
        calls: list[float] = []

        @contextmanager
        def counting_store_factory():
            with t2_ctx() as db:
                original_out = db.tuples.out

                def _counting_out(*a, **kw):
                    calls.append(time.monotonic())
                    return original_out(*a, **kw)

                db.tuples.out = _counting_out
                yield db

        try:
            s.subscribe(
                f"mailbox/{name}", templates=[], store_factory=counting_store_factory,
                state_dir=tmp_path, directory_ttl_s=2.0, directory_heartbeat_s=0.2, lease_poll_s=0.05,
            )
            assert len(calls) == 1  # the synchronous first arm
            time.sleep(0.6)
            assert len(calls) >= 2, "the background thread never re-sent within its configured interval"
        finally:
            s.shutdown()


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
            "board/release-notes", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path,
        )
        persist(t1_a, subs)
        subs.shutdown()

        # A FRESH set instance for the SAME session id loads it back -- the
        # resume path (a new process, same session id).
        fresh_a = load(t1_a, session_a)
        try:
            names = {e["subspace"] for e in fresh_a.entries()}
            assert "board/release-notes" in names
            assert f"mailbox/{session_a}" in names
        finally:
            fresh_a.shutdown()

        # A DIFFERENT session id (the clear path) sees only its own
        # mailbox: T1 itself is session-scoped, so it never finds session
        # A's persisted row at all.
        session_b = str(uuid.uuid4())
        minted_b = mint_t1_session_token(session_b, context="rdr211 subscriptions test")
        monkeypatch.setenv("NX_T1_SESSION", minted_b["session_token"])
        monkeypatch.setenv("NX_T1_SESSION_ID", session_b)
        t1_b = get_t1_database()

        fresh_b = load(t1_b, session_b)
        try:
            assert fresh_b.entries() == [{"subspace": f"mailbox/{session_b}", "cursor": None}]
        finally:
            fresh_b.shutdown()
