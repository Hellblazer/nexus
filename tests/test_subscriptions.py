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
        s.subscribe("board/ci/nexus-develop", templates=[], store_factory=lambda: None, state_dir=None)
        [_mailbox, board] = s.entries()
        assert board["subspace"] == "board/ci/nexus-develop"
        since = datetime.fromisoformat(board["since"])
        assert since.tzinfo is not None
        assert before <= since <= datetime.now(UTC)

    def test_start_position_survives_the_json_round_trip_a_resume_makes(self) -> None:
        from nexus.mcp.subscriptions import SubscriptionSet

        s = SubscriptionSet(session_id="sess-a")
        s.subscribe("board/ci/nexus-develop", templates=[], store_factory=lambda: None, state_dir=None)
        [_m, board] = s.entries()
        data = s.to_json()
        assert data["board"] == ["board/ci/nexus-develop"]  # the older list shape is kept
        assert data["board_since"] == {"board/ci/nexus-develop": board["since"]}

        restored = SubscriptionSet.from_json(data)
        assert restored.entries() == s.entries()

    def test_a_record_written_before_the_start_position_delivers_everything(self) -> None:
        from nexus.mcp.subscriptions import SubscriptionSet

        restored = SubscriptionSet.from_json({"session_id": "sess-a", "board": ["board/x"]})
        assert restored.entries() == [{"subspace": "mailbox/sess-a"}, {"subspace": "board/x"}]

    def test_resubscribing_keeps_the_original_start_position(self) -> None:
        from nexus.mcp.subscriptions import SubscriptionSet

        s = SubscriptionSet(session_id="sess-a")
        s.subscribe("board/x", templates=[], store_factory=lambda: None, state_dir=None)
        first = s.entries()[1]["since"]
        s.subscribe("board/x", templates=[], store_factory=lambda: None, state_dir=None)
        assert s.entries()[1]["since"] == first


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


class TestArmSessionName:
    """`arm_session_name` takes the name Claude Code already knows through
    the SAME `_arm_name_lease` an explicit `tuple_subscribe` uses, so the
    existing lease thread renews it. It is a convenience: it never beats an
    explicit choice and never makes one fail."""

    def test_arms_the_directory_lease_with_this_sessions_id(self) -> None:
        fake = _FakeTuples()
        s = SubscriptionSet(session_id="sess-auto")
        try:
            assert s.arm_session_name("nexus_3dc", store_factory=lambda: _fake_store_factory(fake)) is True
            assert s.leased_name == "nexus_3dc"
            assert _wait_for(lambda: len(fake.calls) == 1)
            [(subspace, keys, dims, _nonce, ttl)] = fake.calls
            assert (subspace, keys, dims) == ("directory/nexus_3dc", {"name": "nexus_3dc"}, {"session_id": "sess-auto"})
            assert ttl == int(DIRECTORY_TTL_S)
            assert s._lease_thread is not None and s._lease_thread.is_alive()  # noqa: SLF001 — the existing renewal thread
            assert s.entries() == [{"subspace": "mailbox/sess-auto"}]  # still never a delivered mailbox
        finally:
            s.shutdown()

    def test_an_unsafe_name_or_the_session_id_itself_is_a_quiet_no_op(self) -> None:
        s = SubscriptionSet(session_id="sess-auto")
        for bad in ("", "has space", "../x", "-lead", "sess-auto", "line\nbreak"):
            assert s.arm_session_name(bad, store_factory=_poison_store_factory()) is False
        assert s.leased_name is None

    def test_an_explicit_name_already_held_is_not_overridden(self, tmp_path) -> None:
        fake = _FakeTuples()
        factory = lambda: _fake_store_factory(fake)  # noqa: E731 — test-local
        s = SubscriptionSet(session_id="sess-auto")
        s.subscribe("mailbox/chosen", templates=[], store_factory=factory, state_dir=tmp_path)
        try:
            assert s.arm_session_name("from-registry", store_factory=_poison_store_factory()) is False
            assert s.leased_name == "chosen"
        finally:
            s.shutdown()

    def test_an_explicit_subscribe_of_another_name_replaces_the_auto_name(self, tmp_path) -> None:
        fake = _FakeTuples()
        factory = lambda: _fake_store_factory(fake)  # noqa: E731 — test-local
        s = SubscriptionSet(session_id="sess-auto")
        s.arm_session_name("from-registry", store_factory=factory)
        try:
            assert _wait_for(lambda: len(fake.calls) == 1)
            s.subscribe("mailbox/chosen", templates=[], store_factory=factory, state_dir=tmp_path)
            assert s.leased_name == "chosen"
            released = [c for c in fake.calls if c[0] == "directory/from-registry" and c[4] == 1]
            assert released, "the replaced auto name's row is released, not left to its TTL"
            # the explicit name is now a choice: a second explicit name is refused again
            with pytest.raises(SchemaViolationError):
                s.subscribe("mailbox/third", templates=[], store_factory=_poison_store_factory(), state_dir=tmp_path)
        finally:
            s.shutdown()

    def test_an_explicit_subscribe_of_the_same_name_is_idempotent_and_leaves_it_auto(self, tmp_path) -> None:
        fake = _FakeTuples()
        factory = lambda: _fake_store_factory(fake)  # noqa: E731 — test-local
        s = SubscriptionSet(session_id="sess-auto")
        s.arm_session_name("same", store_factory=factory)
        try:
            assert _wait_for(lambda: len(fake.calls) == 1)
            before = len(fake.calls)
            s.subscribe("mailbox/same", templates=[], store_factory=factory, state_dir=tmp_path)
            assert len(fake.calls) == before  # no second arm
            # still auto: a different explicit name replaces it rather than being refused
            s.subscribe("mailbox/other", templates=[], store_factory=factory, state_dir=tmp_path)
            assert s.leased_name == "other"
        finally:
            s.shutdown()

    def test_a_second_auto_arm_is_a_no_op(self) -> None:
        fake = _FakeTuples()
        s = SubscriptionSet(session_id="sess-auto")
        try:
            assert s.arm_session_name("n", store_factory=lambda: _fake_store_factory(fake)) is True
            assert s.arm_session_name("n", store_factory=_poison_store_factory()) is False
            assert _wait_for(lambda: len(fake.calls) == 1)
            assert len(fake.calls) == 1
        finally:
            s.shutdown()


class TestWaiterStartArmsTheSessionName:
    """nexus-u839f: the arm happens where the server starts its
    session-scoped background work, `_start_channel_waiter`, so server
    start, a deferred T1 mint and a `/clear` handoff (which calls the same
    function for the new id) all re-arm the name without a model step."""

    @pytest.fixture
    def server(self, tmp_path, monkeypatch):
        import os

        from nexus.mcp import core as _core

        sid = str(uuid.uuid4())
        fake = _FakeTuples()
        started: list[object] = []

        class _T1:
            def list_entries(self):
                return []

        class _Waiter:
            def __init__(self, *args, **kwargs) -> None:
                self.session_id = args[0]

            def start(self) -> None:
                started.append(self)

        monkeypatch.delenv("NX_MCP_PROBE", raising=False)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.setenv("NX_T1_SESSION_ID", sid)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setattr(_core, "_DEFERRED_T1_MINT", {})
        monkeypatch.setattr(_core, "_get_t1", lambda: (_T1(), None))
        monkeypatch.setattr(_core, "_t2_ctx", lambda: _fake_store_factory(fake))
        monkeypatch.setattr(_core._channel, "ChannelWaiter", _Waiter)
        sessions = tmp_path / "claude" / "sessions"
        # this process's own pid: alive, so the default liveness check passes
        _write_session_file(sessions, os.getpid(), sessionId=sid, name="nexus_3dc")
        try:
            yield _core, sid, fake, started
        finally:
            _core._channel.unregister_active_waiter(sid)
            _core._subscriptions.reset_cache(sid)

    def test_starting_the_waiter_arms_the_name_from_the_sessions_registry(self, server) -> None:
        core, sid, fake, started = server
        core._start_channel_waiter()
        assert len(started) == 1
        assert _wait_for(lambda: len(fake.calls) == 1)
        assert [(c[0], c[2]) for c in fake.calls] == [("directory/nexus_3dc", {"session_id": sid})]
        assert core._subscriptions._CACHE[sid].leased_name == "nexus_3dc"  # noqa: SLF001

    def test_a_second_start_with_a_live_waiter_arms_nothing_more(self, server) -> None:
        core, _sid, fake, started = server
        core._start_channel_waiter()
        core._channel.register_active_waiter(started[0])
        core._start_channel_waiter()
        assert _wait_for(lambda: len(fake.calls) == 1)
        time.sleep(0.1)
        assert len(fake.calls) == 1

    def test_no_registry_entry_still_starts_the_waiter_and_writes_nothing(self, server, tmp_path, monkeypatch) -> None:
        core, _sid, fake, started = server
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-such-claude-dir"))
        core._start_channel_waiter()
        assert len(started) == 1
        assert fake.calls == []

    def test_a_resolver_failure_never_stops_the_waiter(self, server, monkeypatch) -> None:
        core, _sid, fake, started = server

        def _boom(*_a, **_k):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(core._subscriptions, "resolve_session_name", _boom)
        core._start_channel_waiter()
        assert len(started) == 1
        assert fake.calls == []


class TestArmDoesNoEngineWorkOnTheCallingThread:
    """The registry arm can be called from the asyncio loop (server start,
    the deferred-mint callback, the `/clear` handoff tick), and `store.out`
    is a blocking HTTP call with a 30 s client timeout. The first send is
    the lease thread's first tick, never the caller's."""

    def test_the_store_is_never_touched_on_the_calling_thread(self) -> None:
        import threading

        main = threading.get_ident()
        touched_on: list[int] = []

        class _ThreadRecordingTuples(_FakeTuples):
            def out(self, *a, **k):
                touched_on.append(threading.get_ident())
                return super().out(*a, **k)

        fake = _ThreadRecordingTuples()

        @contextmanager
        def factory():
            touched_on.append(threading.get_ident())
            yield type("_Db", (), {"tuples": fake})()

        s = SubscriptionSet(session_id="sess-loop")
        try:
            assert s.arm_session_name("loop-safe", store_factory=factory) is True
            assert _wait_for(lambda: len(fake.calls) == 1)
            assert len(touched_on) >= 2  # the factory and the out(), both on the lease thread
            assert main not in touched_on, "the engine was touched on the calling (asyncio loop) thread"
        finally:
            s.shutdown()

    def test_an_explicit_subscribe_still_arms_synchronously(self, tmp_path) -> None:
        fake = _FakeTuples()
        s = SubscriptionSet(session_id="sess-explicit")
        try:
            s.subscribe("mailbox/chosen", templates=[], store_factory=lambda: _fake_store_factory(fake), state_dir=tmp_path)
            assert len(fake.calls) == 1, "tuple_subscribe's first arm stays synchronous"
        finally:
            s.shutdown()

    def test_an_engine_down_at_the_first_tick_is_retried_by_the_thread(self) -> None:
        fake = _FakeTuples()
        attempts = []

        class _Flaky(_FakeTuples):
            def out(self, *a, **k):
                attempts.append(1)
                if len(attempts) == 1:
                    raise ConnectionError("engine down")
                return super().out(*a, **k)

        flaky = _Flaky()
        s = SubscriptionSet(session_id="sess-retry")
        try:
            s.arm_session_name("retry-me", store_factory=lambda: _fake_store_factory(flaky), lease_poll_s=0.01)
            assert _wait_for(lambda: len(flaky.calls) == 1), "the heartbeat re-sends after the engine comes back"
        finally:
            s.shutdown()
        del fake


def test_the_registry_is_not_re_read_before_the_interval() -> None:
    # NAME_RESOLVE_INTERVAL_S at its real 60 s: many lease-loop polls, no registry read.
    fake, reads = _FakeTuples(), []
    s = SubscriptionSet(session_id="sess-cadence")
    try:
        s.arm_session_name(
            "steady", store_factory=lambda: _fake_store_factory(fake),
            resolve=lambda: reads.append(1) or "steady", lease_poll_s=0.01,
        )
        assert _wait_for(lambda: len(fake.calls) >= 1)
        time.sleep(0.3)
        assert reads == []
    finally:
        s.shutdown()


class TestNameFollowsTheRegistry:
    """The lease thread re-reads the registry (at most once per
    NAME_RESOLVE_INTERVAL_S, patched to 0 here) so a `/rename`, or a name the
    startup read missed, reaches the directory without the model."""

    @pytest.fixture(autouse=True)
    def _fast_resolve(self, monkeypatch):
        import nexus.mcp.subscriptions as m

        monkeypatch.setattr(m, "NAME_RESOLVE_INTERVAL_S", 0.0)

    class _Registry:
        def __init__(self, name) -> None:
            self.name = name
            self.reads = 0

        def __call__(self):
            self.reads += 1
            return self.name

    def _arm(self, s, fake, registry, name):
        return s.arm_session_name(
            name, store_factory=lambda: _fake_store_factory(fake), resolve=registry, lease_poll_s=0.01,
        )

    def test_a_rename_releases_the_old_row_and_arms_the_new_one(self) -> None:
        fake, registry = _FakeTuples(), self._Registry("first")
        s = SubscriptionSet(session_id="sess-rn")
        try:
            assert self._arm(s, fake, registry, "first") is True
            assert _wait_for(lambda: any(c[0] == "directory/first" for c in fake.calls))
            registry.name = "second"
            assert _wait_for(lambda: any(c[0] == "directory/second" and c[4] != 1 for c in fake.calls))
            assert any(c[0] == "directory/first" and c[4] == 1 for c in fake.calls), "old row released, not left to its TTL"
            assert s.leased_name == "second"
        finally:
            s.shutdown()
        assert any(c[0] == "directory/second" and c[4] == 1 for c in fake.calls), "shutdown releases the CURRENT name"

    def test_a_startup_miss_is_retried_and_arms_the_late_name(self) -> None:
        fake, registry = _FakeTuples(), self._Registry(None)
        s = SubscriptionSet(session_id="sess-late")
        try:
            assert self._arm(s, fake, registry, None) is False
            assert s.leased_name is None and fake.calls == []
            # a repeat auto-arm while tracking is a no-op, not a second thread
            assert self._arm(s, fake, registry, None) is False
            registry.name = "late-name"
            assert _wait_for(lambda: s.leased_name == "late-name")
            assert _wait_for(lambda: any(c[0] == "directory/late-name" for c in fake.calls))
        finally:
            s.shutdown()

    def test_a_miss_or_an_unsafe_name_keeps_the_held_row(self) -> None:
        fake, registry = _FakeTuples(), self._Registry("held")
        s = SubscriptionSet(session_id="sess-keep")
        try:
            self._arm(s, fake, registry, "held")
            assert _wait_for(lambda: len(fake.calls) >= 1)
            for value in (None, "has space", "sess-keep"):
                registry.name = value
                reads = registry.reads
                assert _wait_for(lambda: registry.reads >= reads + 2)
            assert s.leased_name == "held"
            assert not any(c[4] == 1 for c in fake.calls), "nothing was released"
        finally:
            s.shutdown()

    def test_an_explicit_name_is_never_swapped(self, tmp_path) -> None:
        fake, registry = _FakeTuples(), self._Registry("auto-name")
        factory = lambda: _fake_store_factory(fake)  # noqa: E731 — test-local
        s = SubscriptionSet(session_id="sess-expl")
        try:
            self._arm(s, fake, registry, "auto-name")
            assert _wait_for(lambda: len(fake.calls) >= 1)
            s.subscribe("mailbox/chosen", templates=[], store_factory=factory, state_dir=tmp_path)
            registry.name = "renamed-later"
            reads = registry.reads
            time.sleep(0.2)
            assert registry.reads == reads, "no resolver runs once the name is a choice"
            assert s.leased_name == "chosen"
            assert not any(c[0] == "directory/renamed-later" for c in fake.calls)
        finally:
            s.shutdown()

    def test_an_explicit_subscribe_of_the_same_name_keeps_following_the_registry(self, tmp_path) -> None:
        # The SessionStart line asks every model to subscribe the name it already holds;
        # that must not pin it, or a /rename is never followed (nexus-u839f).
        fake, registry = _FakeTuples(), self._Registry("same")
        factory = lambda: _fake_store_factory(fake)  # noqa: E731 — test-local
        s = SubscriptionSet(session_id="sess-same")
        try:
            self._arm(s, fake, registry, "same")
            assert _wait_for(lambda: len(fake.calls) >= 1)
            s.subscribe("mailbox/same", templates=[], store_factory=factory, state_dir=tmp_path)
            registry.name = "renamed-later"
            assert _wait_for(lambda: s.leased_name == "renamed-later")
            assert any(c[0] == "directory/renamed-later" for c in fake.calls)
        finally:
            s.shutdown()

    def test_a_store_that_cannot_open_during_the_swap_keeps_the_held_row_and_retries(self) -> None:
        registry = self._Registry("old")
        state = {"down": False}
        fake = _FakeTuples()

        @contextmanager
        def factory():
            if state["down"]:
                raise ConnectionError("engine down")
            yield type("_Db", (), {"tuples": fake})()

        s = SubscriptionSet(session_id="sess-eng")
        try:
            s.arm_session_name("old", store_factory=factory, resolve=registry, lease_poll_s=0.01)
            assert _wait_for(lambda: len(fake.calls) >= 1)
            registry.name = "new"
            state["down"] = True
            reads = registry.reads
            assert _wait_for(lambda: registry.reads >= reads + 2)
            assert s.leased_name == "old"
            state["down"] = False
            assert _wait_for(lambda: s.leased_name == "new")
            assert _wait_for(lambda: any(c[0] == "directory/new" for c in fake.calls))
        finally:
            s.shutdown()

    def test_an_engine_write_failure_during_the_swap_is_re_sent_by_the_heartbeat(self) -> None:
        registry = self._Registry("old")
        state = {"fail": False}

        class _Tuples(_FakeTuples):
            def out(self, *a, **k):
                if state["fail"]:
                    raise ConnectionError("engine down")
                return super().out(*a, **k)

        fake = _Tuples()
        s = SubscriptionSet(session_id="sess-eng2")
        try:
            self._arm(s, fake, registry, "old")
            assert _wait_for(lambda: len(fake.calls) >= 1)
            registry.name = "new"
            state["fail"] = True
            assert _wait_for(lambda: s.leased_name == "new")
            state["fail"] = False
            assert _wait_for(lambda: any(c[0] == "directory/new" and c[4] != 1 for c in fake.calls)), (
                "the new row is armed by the next 5 s heartbeat once the engine answers"
            )
        finally:
            s.shutdown()


class TestResolverLogsAMissOncePerRun:
    def test_session_name_unresolved_is_not_repeated_by_the_sixty_second_re_read(self, tmp_path, monkeypatch) -> None:
        from structlog.testing import capture_logs

        from nexus.mcp import core as _core

        _log_everything()
        sid = str(uuid.uuid4())
        _write_session_file(tmp_path / "sessions", 91, sessionId=str(uuid.uuid4()), name="other")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        resolve = _core._session_name_resolver(sid)
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

    def test_an_unsafe_registry_name_is_a_debug_line_and_no_arm(self) -> None:
        from structlog.testing import capture_logs

        _log_everything()
        s = SubscriptionSet(session_id="sess-unsafe")
        with capture_logs() as logs:
            assert s.arm_session_name("has space", store_factory=_poison_store_factory()) is False
        [line] = [e for e in logs if e["event"] == "session_name_unsafe"]
        assert line["log_level"] == "debug" and line["session_id"] == "sess-unsafe"
