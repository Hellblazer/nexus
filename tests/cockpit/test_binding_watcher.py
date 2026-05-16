# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/nexus/cockpit/bindings.py -- RDR-111 §Phase 2 reaction loop.

TDD-first: written before iteration on bindings.py. Covers match
evaluation, profile loading, dedup via cursor, multi-binding fan-out,
malformed YAML rejection, action error containment, and end-to-end
in-tuplespace reaction with the reference bindings.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import chromadb
import pytest

from nexus.cockpit.bindings import (
    Action,
    Binding,
    BindingContext,
    BindingProfile,
    BindingProfileError,
    EventRecord,
    _BindingWatcher,
    action_emit_derived,
    action_log_marker,
    load_profile,
    load_profiles_dir,
    matches,
)
from nexus.tuplespace.api import out
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db


# ---------------------------------------------------------------------------
# Fixtures: minimal subspace registry + tuples.db
# ---------------------------------------------------------------------------


_HOOK_EVENTS_YAML = """\
name: hook_events/tool_call_completed
tier: session
content_type: text
embed_from: content
dimensions:
  actor:     { type: string, required: true }
  session:   { type: string, required: true }
  project:   { type: string, required: true }
  timestamp: { type: string, required: true }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_NOTIFICATION_YAML = """\
name: hook_events/notification
tier: session
content_type: text
embed_from: content
dimensions:
  actor:     { type: string, required: true }
  session:   { type: string, required: true }
  project:   { type: string, required: true }
  timestamp: { type: string, required: true }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_DERIVED_YAML = """\
name: derived/<profile>
tier: session
content_type: text
embed_from: match_text
dimensions:
  profile:    { type: string, required: true }
  source_op:  { type: string, required: true }
  source_sub: { type: string, required: true }
  tuple_id:   { type: string, required: true }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 50
tiers: [session]
retention_seconds: 86400
"""


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "hook_events_tool_call_completed.yml").write_text(_HOOK_EVENTS_YAML)
    (d / "hook_events_notification.yml").write_text(_NOTIFICATION_YAML)
    (d / "derived.yml").write_text(_DERIVED_YAML)
    return d


@pytest.fixture
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "tuples.db"
    c = open_tuples_db(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture
def index(registry, chroma_client) -> TupleIndex:
    return TupleIndex.from_registry(registry, chroma_client)


@pytest.fixture
def context(conn, index, registry) -> BindingContext:
    return BindingContext(
        conn=conn, index=index, registry=registry, profile_name="default"
    )


# ---------------------------------------------------------------------------
# Helper: drop a hook-event tuple so an `out` event row appears.
# ---------------------------------------------------------------------------


def _emit_hook_tuple(
    conn,
    index,
    registry,
    *,
    subspace: str,
    extra: str = "",
) -> str:
    return out(
        conn=conn,
        index=index,
        registry=registry,
        subspace=subspace,
        content=f"event-content {extra}",
        dimensions={
            "actor": "test-actor",
            "session": "sess-1",
            "project": "/tmp/p",
            "timestamp": "1700000000.0",
        },
    )


# ---------------------------------------------------------------------------
# matches() -- predicate evaluation
# ---------------------------------------------------------------------------


class TestMatches:
    def _event(self, **kw) -> EventRecord:
        defaults: dict[str, Any] = dict(
            cursor=1,
            subspace="hook_events/notification",
            op="out",
            tuple_id="abc",
            payload_summary=None,
            category="data",
            ts=1.0,
        )
        defaults.update(kw)
        return EventRecord(**defaults)

    def test_empty_predicate_matches_everything(self):
        assert matches(self._event(), {}) is True

    def test_single_field_equality(self):
        ev = self._event(op="out")
        assert matches(ev, {"op": "out"}) is True
        assert matches(ev, {"op": "claim"}) is False

    def test_multi_field_all_must_match(self):
        ev = self._event(subspace="hook_events/tool_call_completed", op="out")
        assert matches(
            ev, {"subspace": "hook_events/tool_call_completed", "op": "out"}
        ) is True
        # op mismatch
        assert matches(
            ev, {"subspace": "hook_events/tool_call_completed", "op": "claim"}
        ) is False

    def test_unknown_key_never_matches(self):
        assert matches(self._event(), {"nonexistent_field": "x"}) is False


# ---------------------------------------------------------------------------
# Profile loading + validation
# ---------------------------------------------------------------------------


class TestProfileLoading:
    def test_load_minimal_profile(self, tmp_path: Path):
        p = tmp_path / "p.yml"
        p.write_text(
            "profile: alpha\n"
            "bindings:\n"
            "  - name: b1\n"
            "    match: {op: out}\n"
            "    action: {kind: log, marker: hi}\n"
        )
        prof = load_profile(p)
        assert prof.name == "alpha"
        assert len(prof.bindings) == 1
        assert prof.bindings[0].name == "b1"
        assert prof.bindings[0].action == Action(kind="log", target="hi")

    def test_python_action(self, tmp_path: Path):
        p = tmp_path / "p.yml"
        p.write_text(
            "profile: a\n"
            "bindings:\n"
            "  - name: b1\n"
            "    match: {}\n"
            "    action: {kind: python, callable: a.b:c}\n"
        )
        prof = load_profile(p)
        assert prof.bindings[0].action == Action(kind="python", target="a.b:c")

    def test_rejects_malformed_yaml(self, tmp_path: Path):
        p = tmp_path / "p.yml"
        p.write_text("profile: a\nbindings: [::oops")
        with pytest.raises(BindingProfileError, match="malformed YAML"):
            load_profile(p)

    def test_rejects_missing_profile(self, tmp_path: Path):
        p = tmp_path / "p.yml"
        p.write_text("bindings: []\n")
        with pytest.raises(BindingProfileError, match="profile"):
            load_profile(p)

    def test_rejects_non_list_bindings(self, tmp_path: Path):
        p = tmp_path / "p.yml"
        p.write_text("profile: a\nbindings: nope\n")
        with pytest.raises(BindingProfileError, match="'bindings' must be a list"):
            load_profile(p)

    def test_rejects_unknown_action_kind(self, tmp_path: Path):
        p = tmp_path / "p.yml"
        p.write_text(
            "profile: a\n"
            "bindings:\n"
            "  - name: b1\n"
            "    match: {}\n"
            "    action: {kind: shell, command: rm -rf /}\n"
        )
        with pytest.raises(BindingProfileError, match="unknown action kind"):
            load_profile(p)

    def test_rejects_duplicate_binding_name(self, tmp_path: Path):
        p = tmp_path / "p.yml"
        p.write_text(
            "profile: a\n"
            "bindings:\n"
            "  - {name: x, match: {}, action: {kind: log, marker: m}}\n"
            "  - {name: x, match: {}, action: {kind: log, marker: m}}\n"
        )
        with pytest.raises(BindingProfileError, match="duplicate binding"):
            load_profile(p)

    def test_load_profiles_dir_empty_when_missing(self, tmp_path: Path):
        assert load_profiles_dir(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# Reference action: action_log_marker
# ---------------------------------------------------------------------------


class TestActionLogMarker:
    def test_emits_structlog_event(self, context, caplog):
        ev = EventRecord(
            cursor=1,
            subspace="hook_events/notification",
            op="out",
            tuple_id="abc",
            payload_summary=None,
            category="data",
            ts=1.0,
        )
        binding = Binding(
            name="b",
            match={},
            action=Action(kind="log", target="cockpit.binding.notification"),
        )
        # Should not raise.
        action_log_marker(ev, binding, context)


# ---------------------------------------------------------------------------
# Reference action: action_emit_derived (in-tuplespace reaction)
# ---------------------------------------------------------------------------


class TestActionEmitDerived:
    def test_writes_to_derived_subspace(self, conn, index, registry, context):
        ev = EventRecord(
            cursor=1,
            subspace="hook_events/tool_call_completed",
            op="out",
            tuple_id="src-tuple-1",
            payload_summary=None,
            category="data",
            ts=1.0,
        )
        binding = Binding(
            name="b",
            match={},
            action=Action(kind="python", target="x:y"),
        )
        action_emit_derived(ev, binding, context)

        # A row landed on derived/default
        rows = conn.execute(
            "SELECT subspace FROM tuples WHERE subspace = 'derived/default'"
        ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# _BindingWatcher: end-to-end via tuples.db events table
# ---------------------------------------------------------------------------


async def _run_briefly(watcher: _BindingWatcher, *, ticks: int = 5):
    """Drive the watcher long enough to drain pending events."""
    task = asyncio.create_task(watcher.run())
    # Yield control several times so the loop polls.
    for _ in range(ticks):
        await asyncio.sleep(0.02)
    watcher.request_stop()
    await asyncio.wait_for(task, timeout=2.0)


class TestBindingWatcher:
    @pytest.mark.asyncio
    async def test_dispatches_python_action_on_matching_event(
        self, conn, index, registry, context
    ):
        # Pre-emit one tuple so an events row exists.
        _emit_hook_tuple(
            conn, index, registry, subspace="hook_events/tool_call_completed"
        )

        calls: list[EventRecord] = []

        def _action(event, binding, ctx):
            calls.append(event)

        # Install into a temp module so the dotted-callable resolver finds it.
        mod = types.ModuleType("nexus_test_bindings_action_mod")
        mod._action = _action
        sys.modules["nexus_test_bindings_action_mod"] = mod

        profile = BindingProfile(
            name="t1",
            bindings=(
                Binding(
                    name="b",
                    match={"subspace": "hook_events/tool_call_completed", "op": "out"},
                    action=Action(
                        kind="python",
                        target="nexus_test_bindings_action_mod:_action",
                    ),
                ),
            ),
        )
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        await _run_briefly(watcher)

        assert len(calls) == 1
        assert calls[0].subspace == "hook_events/tool_call_completed"
        assert calls[0].op == "out"

    @pytest.mark.asyncio
    async def test_dedup_via_cursor(self, conn, index, registry, context):
        _emit_hook_tuple(
            conn, index, registry, subspace="hook_events/tool_call_completed"
        )

        calls: list[EventRecord] = []

        def _action(event, binding, ctx):
            calls.append(event)

        mod = types.ModuleType("nexus_test_bindings_dedup_mod")
        mod._action = _action
        sys.modules["nexus_test_bindings_dedup_mod"] = mod

        profile = BindingProfile(
            name="dedup-prof",
            bindings=(
                Binding(
                    name="b",
                    match={},
                    action=Action(
                        kind="python",
                        target="nexus_test_bindings_dedup_mod:_action",
                    ),
                ),
            ),
        )
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        # First run picks up the event.
        await _run_briefly(watcher)
        assert len(calls) == 1

        # A second watcher with the SAME profile name and SAME conn must
        # see the persisted cursor and process zero events.
        calls.clear()
        watcher2 = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        await _run_briefly(watcher2)
        assert calls == []

    @pytest.mark.asyncio
    async def test_multi_binding_fan_out(self, conn, index, registry, context):
        _emit_hook_tuple(
            conn, index, registry, subspace="hook_events/tool_call_completed"
        )

        hits: list[str] = []

        def _a(event, binding, ctx):
            hits.append("a")

        def _b(event, binding, ctx):
            hits.append("b")

        mod = types.ModuleType("nexus_test_bindings_fanout_mod")
        mod._a = _a
        mod._b = _b
        sys.modules["nexus_test_bindings_fanout_mod"] = mod

        profile = BindingProfile(
            name="fan",
            bindings=(
                Binding(
                    name="ba",
                    match={"op": "out"},
                    action=Action(
                        kind="python", target="nexus_test_bindings_fanout_mod:_a"
                    ),
                ),
                Binding(
                    name="bb",
                    match={"op": "out"},
                    action=Action(
                        kind="python", target="nexus_test_bindings_fanout_mod:_b"
                    ),
                ),
            ),
        )
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        await _run_briefly(watcher)

        assert sorted(hits) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_action_error_containment(self, conn, index, registry, context):
        _emit_hook_tuple(
            conn, index, registry, subspace="hook_events/tool_call_completed"
        )

        good_hits: list[int] = []

        def _bad(event, binding, ctx):
            raise RuntimeError("boom")

        def _good(event, binding, ctx):
            good_hits.append(1)

        mod = types.ModuleType("nexus_test_bindings_err_mod")
        mod._bad = _bad
        mod._good = _good
        sys.modules["nexus_test_bindings_err_mod"] = mod

        profile = BindingProfile(
            name="err",
            bindings=(
                Binding(
                    name="bad",
                    match={},
                    action=Action(
                        kind="python", target="nexus_test_bindings_err_mod:_bad"
                    ),
                ),
                Binding(
                    name="good",
                    match={},
                    action=Action(
                        kind="python", target="nexus_test_bindings_err_mod:_good"
                    ),
                ),
            ),
        )
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        await _run_briefly(watcher)

        # The bad action raised; the good action still ran.
        assert good_hits == [1]
        # Cursor still advanced (watcher did not get stuck on the bad event).
        row = conn.execute(
            "SELECT last_rowid FROM watcher_state WHERE profile = 'err'"
        ).fetchone()
        assert row is not None
        assert int(row[0]) > 0

    @pytest.mark.asyncio
    async def test_end_to_end_with_reference_bindings(
        self, conn, index, registry, context
    ):
        """The two reference bindings demonstrate the primitive end-to-end."""
        # Emit one PostToolUse event and one Notification event.
        _emit_hook_tuple(
            conn, index, registry, subspace="hook_events/tool_call_completed"
        )
        _emit_hook_tuple(
            conn, index, registry, subspace="hook_events/notification"
        )

        profile = BindingProfile(
            name="default",
            bindings=(
                Binding(
                    name="post_tool_use_to_derived",
                    match={
                        "subspace": "hook_events/tool_call_completed",
                        "op": "out",
                    },
                    action=Action(
                        kind="python",
                        target="nexus.cockpit.bindings:action_emit_derived",
                    ),
                ),
                Binding(
                    name="notification_log_marker",
                    match={"subspace": "hook_events/notification", "op": "out"},
                    action=Action(
                        kind="python",
                        target="nexus.cockpit.bindings:action_log_marker",
                    ),
                ),
            ),
        )
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        await _run_briefly(watcher)

        # Reference 1: derived/default got a tuple from the PostToolUse event.
        derived = conn.execute(
            "SELECT id FROM tuples WHERE subspace = 'derived/default'"
        ).fetchall()
        assert len(derived) == 1

    @pytest.mark.asyncio
    async def test_request_stop_terminates_loop(self, conn, context):
        profile = BindingProfile(name="stop-prof", bindings=())
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.05)
        watcher.request_stop()
        await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# Tests: lifecycle wrappers (nexus-9eiw)
# ---------------------------------------------------------------------------


class TestBindingWatcherLifecycle:
    """start() / stop() wrappers — idempotent, bounded-stop, no-op safety."""

    @pytest.mark.asyncio
    async def test_start_returns_running_task_and_is_idempotent(
        self, conn, context
    ) -> None:
        profile = BindingProfile(name="lifecycle", bindings=())
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        task1 = watcher.start()
        task2 = watcher.start()
        try:
            assert task1 is task2
            assert not task1.done()
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_stop_terminates_running_loop(self, conn, context) -> None:
        profile = BindingProfile(name="lifecycle", bindings=())
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        watcher.start()
        await asyncio.sleep(0.02)
        await watcher.stop()
        assert watcher._task is not None
        assert watcher._task.done()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self, conn, context) -> None:
        profile = BindingProfile(name="lifecycle", bindings=())
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        # Must not raise even though start() never ran.
        await watcher.stop()
        assert watcher._task is None

    @pytest.mark.asyncio
    async def test_double_stop_is_noop(self, conn, context) -> None:
        profile = BindingProfile(name="lifecycle", bindings=())
        watcher = _BindingWatcher(
            conn=conn, profiles=[profile], context=context, poll_interval=0.01
        )
        watcher.start()
        await asyncio.sleep(0.02)
        await watcher.stop()
        # Second stop is a no-op — must not deadlock or raise.
        await watcher.stop()


# ---------------------------------------------------------------------------
# Tests: action_idempotency dedup gate (nexus-8wvs)
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_conn(tmp_path: Path):
    """memory.db connection seeded with the action_idempotency schema."""
    from nexus.db.migrations import migrate_action_idempotency_table

    db = tmp_path / "memory.db"
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    migrate_action_idempotency_table(c)
    yield c
    c.close()


class TestActionIdempotency:
    """RDR-111:909-942 dedup gate prevents python-action replay on restart."""

    def _make_event(self, *, tuple_id: str) -> EventRecord:
        return EventRecord(
            cursor=1,
            subspace="hook_events/notification",
            op="out",
            tuple_id=tuple_id,
            payload_summary=None,
            category="data",
            ts=1700000000.0,
        )

    def _make_binding(self) -> Binding:
        return Binding(
            name="dedup-binding",
            match={"subspace": "hook_events/notification"},
            action=Action(
                kind="python",
                target="tests.cockpit.test_binding_watcher:_idem_action_recorder",
            ),
        )

    @pytest.mark.asyncio
    async def test_first_dispatch_runs_action(
        self, conn, index, registry, memory_conn
    ) -> None:
        from nexus.cockpit.bindings import _dispatch_action

        _idem_invocations.clear()
        ctx = BindingContext(
            conn=conn,
            index=index,
            registry=registry,
            memory_conn=memory_conn,
            profile_name="default",
        )
        binding = self._make_binding()
        event = self._make_event(tuple_id="tuple-1")
        await _dispatch_action(
            binding.action, event=event, binding=binding, context=ctx
        )
        assert len(_idem_invocations) == 1

    @pytest.mark.asyncio
    async def test_replay_with_same_tuple_id_is_deduped(
        self, conn, index, registry, memory_conn
    ) -> None:
        from nexus.cockpit.bindings import _dispatch_action

        _idem_invocations.clear()
        ctx = BindingContext(
            conn=conn,
            index=index,
            registry=registry,
            memory_conn=memory_conn,
            profile_name="default",
        )
        binding = self._make_binding()
        event = self._make_event(tuple_id="tuple-2")
        await _dispatch_action(
            binding.action, event=event, binding=binding, context=ctx
        )
        await _dispatch_action(
            binding.action, event=event, binding=binding, context=ctx
        )
        # First call runs the action, second hits the dedup gate.
        assert len(_idem_invocations) == 1

    @pytest.mark.asyncio
    async def test_log_action_skips_dedup_check(
        self, conn, index, registry, memory_conn
    ) -> None:
        """log actions are inherently idempotent — no row should be written."""
        from nexus.cockpit.bindings import _dispatch_action

        ctx = BindingContext(
            conn=conn,
            index=index,
            registry=registry,
            memory_conn=memory_conn,
            profile_name="default",
        )
        binding = Binding(
            name="log-only",
            match={"subspace": "hook_events/notification"},
            action=Action(kind="log", target="LOG-MARKER"),
        )
        event = self._make_event(tuple_id="tuple-3")
        await _dispatch_action(
            binding.action, event=event, binding=binding, context=ctx
        )
        count = memory_conn.execute(
            "SELECT count(*) FROM action_idempotency"
        ).fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_expired_row_lets_next_dispatch_through(
        self, conn, index, registry, memory_conn
    ) -> None:
        from nexus.cockpit.bindings import _dispatch_action, _idempotency_key

        _idem_invocations.clear()
        binding = self._make_binding()
        event = self._make_event(tuple_id="tuple-4")
        # Pre-seed an expired row for this (binding, tuple_id).
        memory_conn.execute(
            "INSERT INTO action_idempotency (idempotency_key, expires_at) "
            "VALUES (?, ?)",
            (_idempotency_key(binding.name, event.tuple_id), 1.0),
        )
        memory_conn.commit()

        ctx = BindingContext(
            conn=conn,
            index=index,
            registry=registry,
            memory_conn=memory_conn,
            profile_name="default",
        )
        await _dispatch_action(
            binding.action, event=event, binding=binding, context=ctx
        )
        # Expired row did not dedup; the action ran exactly once.
        assert len(_idem_invocations) == 1

    @pytest.mark.asyncio
    async def test_no_memory_conn_disables_dedup_gate(
        self, conn, index, registry
    ) -> None:
        """Backward-compat: contexts without memory_conn keep at-least-once."""
        from nexus.cockpit.bindings import _dispatch_action

        _idem_invocations.clear()
        ctx = BindingContext(
            conn=conn,
            index=index,
            registry=registry,
            memory_conn=None,
            profile_name="default",
        )
        binding = self._make_binding()
        event = self._make_event(tuple_id="tuple-5")
        await _dispatch_action(
            binding.action, event=event, binding=binding, context=ctx
        )
        await _dispatch_action(
            binding.action, event=event, binding=binding, context=ctx
        )
        # No dedup gate → both calls run.
        assert len(_idem_invocations) == 2


# Used by TestActionIdempotency via the python-action lookup mechanism.
_idem_invocations: list[str] = []


def _idem_action_recorder(
    event: EventRecord,
    binding: Binding,
    context: BindingContext,
) -> None:
    _idem_invocations.append(event.tuple_id)
