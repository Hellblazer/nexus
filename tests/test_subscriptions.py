# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.11): ``SubscriptionSet``
validation, mutation, the change-observer signal, the name lease
(RDR-208 Phase 3, bead nexus-galkv.20), and T1 persistence across a
resume vs. a clear.

Validation-only tests (queue/lock refusal naming ``in``, the 32-topic
bound, a malformed subspace, the observer, plain unsubscribe) never touch
a real store: a POISON ``store_factory`` that raises if ever called is the
falsifier proving the refusal happens before any engine call. The name
lease, the directory-lease send/re-send, and the resume/clear persistence
tests run against the real engine substrate (``t2_service_env``) and a
real T1 handle, mirroring ``tests/test_mcp_tuple_tools.py``'s own
fixtures.
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
        assert s.entries() == [{"subspace": s.session_mailbox}]

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
        before the `directory/<name>` lease -- `_poison_store_factory` is
        the falsifier that no lease was ever started."""
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

    def test_subscribing_the_sessions_own_mailbox_is_a_noop(self, tmp_path):
        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        s.subscribe(
            f"mailbox/{sid}", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path,
        )
        assert s.leased_name is None
        assert s.version == 0

    def test_a_second_leased_name_is_refused(self, tmp_path):
        fake = _FakeTuples()
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        s.subscribe(
            "mailbox/name-a", templates=[], store_factory=lambda: _fake_store_factory(fake),
            state_dir=tmp_path,
        )
        try:
            assert s.leased_name == "name-a"
            with pytest.raises(SchemaViolationError):
                s.subscribe(
                    "mailbox/name-b", templates=[], store_factory=_poison_store_factory(),
                    state_dir=tmp_path,
                )
            assert s.leased_name == "name-a"
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
        assert s.entries() == [{"subspace": s.session_mailbox}]

    def test_session_mailbox_cannot_be_unsubscribed(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        with pytest.raises(SchemaViolationError):
            s.unsubscribe(s.session_mailbox)

    def test_unsubscribing_something_never_subscribed_is_a_noop(self):
        s = SubscriptionSet(session_id=str(uuid.uuid4()))
        s.unsubscribe("board/never-subscribed")  # must not raise


class TestNameLease:
    """Real engine (t2_service_env): the directory lease's shape and
    re-send cadence, unsubscribe stopping it, and -- RDR-208 Phase 3, bead
    nexus-galkv.20, pinning the Transition Test Plan's "then stops" half
    -- that arming a name writes NO per-session registration file any
    more and is NEVER a delivered mailbox (never listed by `entries()`,
    which is the one place the channel waiter and `tuple_subscriptions`
    both read)."""

    def test_arms_the_directory_lease_and_writes_no_registration_file(
        self, t2_service_env, tmp_path,
    ) -> None:
        from nexus.mcp_infra import t2_ctx

        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        name = f"inst-{uuid.uuid4().hex[:8]}"
        try:
            s.subscribe(f"mailbox/{name}", templates=[], store_factory=t2_ctx, state_dir=tmp_path)
            assert s.leased_name == name

            # THE "THEN STOPS" HALF: no per-session registration file is
            # written under state_dir any more -- the old drain-hook floor
            # for a registered instance name is gone outright, not merely
            # relocated. Checked as a specific path, not "state_dir holds
            # nothing at all": under the full suite (nexus-6qp25 xdist),
            # `tmp_path` doubles as this worker's `NEXUS_CONFIG_DIR`
            # sub-path (the autouse `_isolate_config_dir` fixture), and a
            # worker's FIRST substrate call may cache an unrelated
            # data-token lease file there -- legitimate T2 plumbing this
            # test has nothing to say about.
            assert not (tmp_path / "tuple-watch").exists()

            # A leased name is never a delivered mailbox.
            assert {"subspace": f"mailbox/{name}"} not in s.entries()

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
        assert s.leased_name is None
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


class TestDirectoryRelease:
    """nexus-kdxyv (RDR-208 test plan: "a /clear self-stop releases the
    watcher's entry within about a second; an ordinary exit leaves it for
    one TTL"). Stopping a lease on purpose -- ``unsubscribe`` of the
    leased name, or ``shutdown`` on a handoff -- re-sends the SAME
    nonce with ``ttl_seconds=1`` so the ``directory/<name>`` row lapses
    within about a second instead of at the 300 s TTL. The idempotent
    tuple id (keys + nonce) makes that an update of the live row, never a
    second row. A plain process exit still leaves the row for one TTL:
    nothing here runs on exit."""

    def _armed(self, tmp_path):
        fake = _FakeTuples()
        s = SubscriptionSet(session_id="sess-1")
        s.subscribe("mailbox/inst-a", templates=_FIXTURE_TEMPLATES,
                    store_factory=lambda: _fake_store_factory(fake), state_dir=tmp_path)
        assert len(fake.calls) == 1
        return s, fake

    def test_shutdown_releases_the_entry_with_the_same_nonce_and_ttl_one(self, tmp_path):
        s, fake = self._armed(tmp_path)
        arm_subspace, arm_keys, arm_dims, arm_nonce, arm_ttl = fake.calls[0]
        assert arm_ttl == int(DIRECTORY_TTL_S)
        s.shutdown()
        assert len(fake.calls) == 2, fake.calls
        subspace, keys, dims, nonce, ttl = fake.calls[1]
        assert (subspace, keys, dims) == (arm_subspace, arm_keys, arm_dims)
        assert nonce == arm_nonce
        assert ttl == 1

    def test_unsubscribe_releases_the_entry(self, tmp_path):
        s, fake = self._armed(tmp_path)
        s.unsubscribe("mailbox/inst-a")
        assert len(fake.calls) == 2, fake.calls
        assert fake.calls[1][4] == 1
        assert fake.calls[1][3] == fake.calls[0][3]

    def test_shutdown_twice_releases_once(self, tmp_path):
        s, fake = self._armed(tmp_path)
        s.shutdown()
        s.shutdown()
        assert len(fake.calls) == 2

    def test_shutdown_with_no_lease_writes_nothing(self, tmp_path):
        s = SubscriptionSet(session_id="sess-1")
        s.subscribe("board/t", templates=_FIXTURE_TEMPLATES,
                    store_factory=_poison_store_factory(), state_dir=tmp_path)
        s.shutdown()  # the poison factory proves no out() happened

    def test_a_failed_release_is_swallowed(self, tmp_path):
        s, fake = self._armed(tmp_path)

        def _boom(*a, **kw):
            raise RuntimeError("engine gone")
        fake.out = _boom
        s.shutdown()  # logged, never raised: a handoff must not die on this
        assert s._lease_thread is None

    def test_on_the_real_engine_the_row_lapses_within_seconds(self, t2_service_env, tmp_path) -> None:
        from nexus.mcp_infra import t2_ctx

        sid = str(uuid.uuid4())
        s = SubscriptionSet(session_id=sid)
        name = f"inst-{uuid.uuid4().hex[:8]}"
        s.subscribe(f"mailbox/{name}", templates=[], store_factory=t2_ctx, state_dir=tmp_path)
        with t2_ctx() as db:
            assert len(db.tuples.rd(f"directory/{name}", {"name": name})) == 1
        s.unsubscribe(f"mailbox/{name}")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with t2_ctx() as db:
                rows = db.tuples.rd(f"directory/{name}", {"name": name})
            if not rows:
                break
            time.sleep(0.25)
        assert not rows, "the released directory row did not lapse within 10 s (TTL is 300 s)"


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

        # nexus-kdxyv: the leased name is NOT restored on resume. The
        # ListAgents name changes at every process start (RDR-208), so the
        # resumed session subscribes its NEW name (RDR-211: "a /resume
        # under a new name repeats it, and the old name's mail strands");
        # restoring the old one re-armed a stale lease and made that
        # subscribe refuse as a second leased name.
        fake = _FakeTuples()
        subs2 = load(t1_a, session_a)
        subs2.subscribe("mailbox/name-before-resume", templates=[],
                        store_factory=lambda: _fake_store_factory(fake), state_dir=tmp_path)
        persist(t1_a, subs2)
        subs2.shutdown()
        resumed = load(t1_a, session_a, store_factory=lambda: _fake_store_factory(fake))
        try:
            assert resumed.leased_name is None
            assert "board/release-notes" in {e["subspace"] for e in resumed.entries()}
            before = len(fake.calls)
            resumed.subscribe("mailbox/name-after-resume", templates=[],
                              store_factory=lambda: _fake_store_factory(fake), state_dir=tmp_path)
            assert resumed.leased_name == "name-after-resume"
            # Assert WHICH names were written, not how many writes happened: a
            # heartbeat thread may tick under a loaded box, and a count would
            # make this test fail for a reason it does not name.
            after = [c[0] for c in fake.calls[before:]]
            assert "directory/name-after-resume" in after, after
            assert "directory/name-before-resume" not in after, after
        finally:
            resumed.shutdown()

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
            assert fresh_b.entries() == [{"subspace": f"mailbox/{session_b}"}]
        finally:
            fresh_b.shutdown()
