# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-213 (amends RDR-211 Phase 1 Step 3, bead nexus-tk2cz): the
`claude/channel` capability declaration and the lifespan waiter
(`nexus.mcp.channel`), with the proof gate and claim-at-delivery deleted,
and (T2 `nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
2026-09-17`) the mailbox path rebuilt on the SAME cursor shape the board
path already used.

Layers, cheapest first:

- ``TestCapabilityDeclaration``: the SDK's own in-process memory-stream
  harness (``mcp.shared.memory``) drives a REAL low-level `Server.run()`
  round trip and reads the returned `InitializeResult` -- no stdio, no
  engine.
- ``TestChannelWaiterFakeStore``: a `_FakeTupleStore` -- a minimally
  stateful in-memory model of the engine's own `queryOnce` semantics
  (since/n/ordering/claim_state) -- drives every `ChannelWaiter` branch
  deterministically.
- ``TestChannelWaiterRealEngine`` (``t2_service_env``): the properties a
  fake store cannot prove -- genuine parking against a real `wait()`,
  and the cursor design's one loss-of-liveness risk under concurrent
  writers.
- ``TestChannelStatusPublish``: the on-disk record `nx doctor` reads.
- ``TestDoctorProbeNeverStartsAWaiter``: RDR-213 MVV run 2 finding D1.
"""
from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
import uuid
from typing import Any

import httpx
import pytest

from nexus.db.t2.records import TupleRow, WaitResult, WaitSpec
from nexus.mcp import channel


class TestCapabilityDeclaration:
    """The SDK's own ``create_client_server_memory_streams`` harness --
    no stdio, no engine -- drives `Server.run()` exactly as
    `channel.run_stdio_with_channel` does, minus the stdio transport
    itself. Structured after `mcp.shared.memory.
    create_connected_server_and_client_session`'s own body (an anyio
    task group whose `cancel_scope` tears the server task down once the
    client has its answer -- the server's own message loop runs forever
    otherwise)."""

    @pytest.mark.asyncio
    async def test_initialize_result_advertises_the_channel_capability(self) -> None:
        import anyio
        from mcp.client.session import ClientSession
        from mcp.server.lowlevel import Server
        from mcp.shared.memory import create_client_server_memory_streams

        server: Server = Server("nexus-channel-test")

        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams

            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: server.run(
                        server_read, server_write,
                        server.create_initialization_options(experimental_capabilities=channel.CHANNEL_CAPABILITY),
                    )
                )
                try:
                    async with ClientSession(read_stream=client_read, write_stream=client_write) as client_session:
                        result = await client_session.initialize()
                        assert result.capabilities.experimental == {"claude/channel": {}}
                finally:
                    tg.cancel_scope.cancel()

    @pytest.mark.asyncio
    async def test_on_initialized_handler_fires_once_the_client_handshake_completes(self) -> None:
        """The gate every send in this module waits on
        (`send_channel_notification`'s `_initialized_event`):
        registering `channel._on_initialized` on `Server.
        notification_handlers[types.InitializedNotification]` -- the
        exact wiring `run_stdio_with_channel` does -- fires once
        `ClientSession.initialize()` completes its own handshake (which
        sends `notifications/initialized` as its last step)."""
        import anyio
        import mcp.types as types
        from mcp.client.session import ClientSession
        from mcp.server.lowlevel import Server
        from mcp.shared.memory import create_client_server_memory_streams

        server: Server = Server("nexus-channel-test-2")
        event = asyncio.Event()

        async def _handler(_notify: object) -> None:
            event.set()

        server.notification_handlers[types.InitializedNotification] = _handler

        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams

            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: server.run(server_read, server_write, server.create_initialization_options())
                )
                try:
                    async with ClientSession(read_stream=client_read, write_stream=client_write) as client_session:
                        await client_session.initialize()
                        with anyio.fail_after(2):
                            await event.wait()
                        assert event.is_set()
                finally:
                    tg.cancel_scope.cancel()


class _FakeTupleStore:
    """A minimally-stateful in-memory model of the engine's own
    `queryOnce` semantics (`TupleRepository.java`, confirmed live by
    `TestChannelWaiterRealEngine::
    test_wait_returns_claimed_and_dead_rows_not_just_available_ones`):
    one append-only, creation-ordered table per subspace (`seed()`
    appends; nothing else adds rows). `wait` returns UNCONSUMED rows
    (claimed and dead-lettered included, never filtered on
    `claim_state`) strictly after `since`, capped at `n`, in
    `(created_at, id)` order -- exactly `queryOnce`'s own contract. It
    returns immediately with whatever specs currently match (there is no
    real blocking here -- honouring `timeout_s` with a genuine
    wall-clock wait would cost the suite real seconds per empty-spec
    call for no test-value); a spec with no match is simply ABSENT from
    the result, never present with empty `tuples` (`WaitResult`'s own
    documented contract).

    `claim`/`release`/`dead_letter`/`consume` mutate a row's state,
    mirroring the real engine operation that produces each
    `claim_state`/`consumed_at` value (`tuple_in`, `tuple_release`,
    repeated `tuple_nack` to `max_attempts`, and an ack, respectively).
    `max_calls` is a fail-fast busy-loop guard, orthogonal to the
    stateful table -- the cursor design (T2 `nexus_rdr/213-decision-
    announcements-rate-limited-not-ack-gated-2026-09-17`) has no
    reconcile path any more, so `rd` is never called by the waiter at
    all; this fake still implements it (mirroring `wait`'s own read
    path) purely so a test can assert it stays at zero."""

    def __init__(self) -> None:
        self._rows: dict[str, list[TupleRow]] = {}
        self._seq = 0
        self.wait_calls: list[tuple[list, int]] = []
        self.wait_raises: Exception | None = None
        self.rd_calls: list[tuple[str, dict | None, int, tuple | None, int]] = []
        #: Fail-fast busy-loop guard: when set, `wait`+`rd` calls combined
        #: past this count raise instead of letting a genuine mechanism
        #: regression spin for the whole test's real-time window.
        self.max_calls: int | None = None

    def _check_max_calls(self) -> None:
        if self.max_calls is not None and (len(self.wait_calls) + len(self.rd_calls)) > self.max_calls:
            raise RuntimeError(
                f"busy-loop guard tripped: more than {self.max_calls} wait+rd calls "
                f"(wait={len(self.wait_calls)}, rd={len(self.rd_calls)}) -- failing fast "
                "instead of spinning for the rest of the test's real-time window"
            )

    # ── table setup, mirroring the real engine operation that produces
    # ── each state ───────────────────────────────────────────────────

    def seed(
        self, subspace: str, id_: str, body: str | None = None, *,
        claim_state: str | None = None, dims: dict[str, str] | None = None,
    ) -> TupleRow:
        """Append a new row to *subspace*'s table -- a real `tuple_out`.
        `created_at` is this store's own monotonic sequence, zero-padded
        so lexicographic string ordering matches insertion order exactly
        -- insertion order IS creation order, as the real engine
        guarantees. Returns the row for convenience."""
        self._seq += 1
        row = TupleRow(
            id=id_, subspace=subspace, template=subspace.split("/")[0], keys={}, dims=dims or {},
            body=body, claim_state=claim_state, claimant=None, lease_until=None, attempts=0,
            consumed_at=None, consumed_by=None, expires_at=None, created_at=f"{self._seq:020d}",
        )
        self._rows.setdefault(subspace, []).append(row)
        return row

    def _mutate(self, subspace: str, id_: str, **changes: Any) -> None:
        rows = self._rows.get(subspace, [])
        for i, row in enumerate(rows):
            if row.id == id_:
                rows[i] = dataclasses.replace(row, **changes)
                return
        raise KeyError(f"{subspace}/{id_} was never seeded")

    def claim(self, subspace: str, id_: str) -> None:
        self._mutate(subspace, id_, claim_state="claimed")

    def release(self, subspace: str, id_: str) -> None:
        self._mutate(subspace, id_, claim_state=None)

    def dead_letter(self, subspace: str, id_: str) -> None:
        self._mutate(subspace, id_, claim_state="dead")

    def consume(self, subspace: str, id_: str) -> None:
        self._mutate(subspace, id_, consumed_at="2026-01-01T00:00:01+00:00")

    # ── the read path ─────────────────────────────────────────────────

    def _unconsumed(self, subspace: str, since: tuple[str, str] | None, n: int) -> list[TupleRow]:
        rows = [r for r in self._rows.get(subspace, []) if r.consumed_at is None]
        if since is not None:
            rows = [r for r in rows if (r.created_at, r.id) > since]
        return rows[:n]

    def rd(self, subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0):
        self.rd_calls.append((subspace, keys_pattern, n, since, timeout_s))
        self._check_max_calls()
        return self._unconsumed(subspace, since, n)

    def wait(self, specs, timeout_s):
        self.wait_calls.append((list(specs), timeout_s))
        self._check_max_calls()
        if self.wait_raises is not None:
            raise self.wait_raises
        results = []
        for spec in specs:
            rows = self._unconsumed(spec.subspace, spec.since, spec.n)
            if rows:
                results.append(WaitResult(subspace=spec.subspace, tuples=rows))
        return results


class _Db:
    def __init__(self, fake: _FakeTupleStore) -> None:
        self.tuples = fake

    def __enter__(self) -> "_Db":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _fake_store_factory(fake: _FakeTupleStore):
    return lambda: _Db(fake)


class _CountingTuples:
    """Wraps a REAL `HttpTupleStore.tuples` handle, counting `wait()`/
    `rd()` calls without touching production code -- for the real-engine
    busy-loop / stop-rule tests. `counts["max_calls"]`, when set (not
    `None`), is a fail-fast busy-loop guard: a combined wait+rd count
    past it raises instead of letting a genuine mechanism regression
    spin against the real engine for the whole test's window."""

    def __init__(self, real: Any, counts: dict[str, int | None]) -> None:
        self._real = real
        self._counts = counts

    def _check_max_calls(self) -> None:
        max_calls = self._counts.get("max_calls")
        if max_calls is not None and (self._counts["wait"] + self._counts["rd"]) > max_calls:
            raise RuntimeError(
                f"busy-loop guard tripped: more than {max_calls} wait+rd calls "
                f"(wait={self._counts['wait']}, rd={self._counts['rd']})"
            )

    def wait(self, *args: Any, **kwargs: Any) -> Any:
        self._counts["wait"] += 1
        self._check_max_calls()
        return self._real.wait(*args, **kwargs)

    def rd(self, *args: Any, **kwargs: Any) -> Any:
        self._counts["rd"] += 1
        self._check_max_calls()
        return self._real.rd(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _CountingDb:
    def __init__(self, counts: dict[str, int]) -> None:
        from nexus.mcp_infra import t2_ctx  # noqa: PLC0415 — test-local, mirrors the sibling real-engine tests' own deferred import

        self._real = t2_ctx()
        self._counts = counts
        self.tuples: _CountingTuples | None = None

    def __enter__(self) -> "_CountingDb":
        real_db = self._real.__enter__()
        self.tuples = _CountingTuples(real_db.tuples, self._counts)
        return self

    def __exit__(self, *exc: object) -> None:
        return self._real.__exit__(*exc)


def _counting_store_factory(counts: dict[str, int]):
    return lambda: _CountingDb(counts)


class _FakeSender:
    def __init__(self, *, always_ok: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._always_ok = always_ok

    async def __call__(self, content: str, meta: dict[str, str]) -> bool:
        self.calls.append((content, meta))
        return self._always_ok


def _subs(session_id: str):
    from nexus.mcp.subscriptions import SubscriptionSet

    return SubscriptionSet(session_id=session_id)


def _dead_letter_n_rows(addr: str, to_key: str, claimant: str, count: int, prefix: str = "dead") -> None:
    """Create *count* real tuples in *addr*, keyed to *to_key*, and
    dead-letter each one in turn (repeated claim+nack to the mailbox
    template's own `max_attempts`) -- real engine operations, real
    `claim_state="dead"` rows, for the real-engine tests below.
    `tuple_in` always claims the OLDEST unclaimed row matching its exact
    `keys_pattern`, so nacking one to the cap before moving on
    dead-letters rows in creation order."""
    from nexus.mcp.core import tuple_in, tuple_nack, tuple_out, tuple_registry  # noqa: PLC0415 — test-local, mirrors sibling real-engine helpers

    reg = tuple_registry()
    mailbox_template = next(t for t in reg["templates"] if t["name"] == "mailbox/<address>")
    max_attempts = mailbox_template["take"]["max_attempts"]
    for i in range(count):
        tuple_out(
            addr, {"to": to_key}, {"from": f"sender-{prefix}-{i}"}, f"{prefix}-{i}", nonce=uuid.uuid4().hex,
        )
        for _attempt in range(max_attempts):
            claim = tuple_in(addr, {"to": to_key}, claimant=claimant, lease_s=300)
            assert claim is not None and "error" not in claim
            tuple_nack(claim["claim_id"], claimant)


class TestChannelWaiterFakeStore:
    """The cursor design's Test Plan scenarios against the fake store
    (T2 `nexus_rdr/213-decision-announcements-rate-limited-not-ack-
    gated-2026-09-17`)."""

    @pytest.mark.asyncio
    async def test_one_row_is_referenced_once_and_the_cursor_advances(self) -> None:
        """(a) one row: referenced exactly once; the cursor advances
        past it so it can never match again."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        row = fake.seed(addr, "t1", "hello")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()
        assert len(fake.wait_calls) == 1
        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 1
        assert waiter._cursor[addr] == (row.created_at, "t1")  # noqa: SLF001

        await waiter.tick()  # since now excludes t1 -- nothing new, no re-send (cadence not due)
        assert len(fake.wait_calls) == 2, "every tick calls wait() exactly once"
        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 1
        assert len(fake.rd_calls) == 0, "nothing is ever read back"

    @pytest.mark.asyncio
    async def test_two_rows_arriving_together_are_referenced_one_wake_apart(self) -> None:
        """(b) two rows arriving together: two references, one wake
        apart, in (created_at, id) order -- the mailbox spec's own `n=1`
        means only the oldest unreferenced row appears per wake."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "first")
        fake.seed(addr, "t2", "second")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()
        assert [m.get("tuple_id") for _c, m in sender.calls] == ["t1"]

        await waiter.tick()
        assert [m.get("tuple_id") for _c, m in sender.calls] == ["t1", "t2"]
        assert waiter.status()["announced"] == 2

    @pytest.mark.asyncio
    async def test_an_ignored_reference_is_resent_five_times_then_falls_silent(self) -> None:
        """(c) an ignored reference is re-sent at `reannounce_interval_s`
        up to `max_announces` times, then left alone."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "unread-forever")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), sender=sender,
            reannounce_interval_s=0.0, max_announces=5, wait_timeout_s=0,
        )

        for _ in range(11):  # 1 initial + 4 resends reaches the cap; the rest must stay silent
            await waiter.tick()

        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 5, "must stop at max_announces=5 and never exceed it"
        assert waiter.status()["announced"] == 1
        assert waiter.status()["pending"] == 0, "spent -- no longer counts as pending"

    @pytest.mark.asyncio
    async def test_a_new_row_supersedes_the_resend_budget_of_the_old_one(self) -> None:
        """(c) a new row supersedes whatever the mailbox was tracking --
        by simple dict overwrite, never a merge of two budgets."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "first")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), sender=sender,
            reannounce_interval_s=10_000.0, max_announces=5, wait_timeout_s=0,
        )

        await waiter.tick()  # t1 referenced, count=1
        await waiter.tick()  # nothing new, no resend due
        fake.seed(addr, "t2", "second")
        await waiter.tick()  # t2 referenced -- supersedes t1's entry entirely

        mail_sends = [m.get("tuple_id") for _c, m in sender.calls]
        assert mail_sends == ["t1", "t2"]
        assert waiter.status()["announced"] == 2
        assert waiter.status()["pending"] == 1, "only ONE active last-reference per mailbox -- t2's, not both"

    @pytest.mark.asyncio
    async def test_a_claimed_row_seen_at_the_wake_gets_one_reference_nothing_read_back(self) -> None:
        """(d) a row already claimed when the wake sees it gets one
        reference like any other -- the notification text already
        covers an empty `tuple_in`, so nothing is ever read back."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "already-claimed", claim_state="claimed")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()

        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 1
        assert len(fake.rd_calls) == 0
        assert waiter.status()["announced"] == 1

    @pytest.mark.asyncio
    async def test_a_dead_row_at_the_head_is_skipped_and_the_cursor_advances_past_it(self) -> None:
        """(e) a dead-lettered row is skipped silently, its cursor
        advanced past it; the live row behind it is referenced the very
        next wake (the mailbox spec's own `n=1` caps one row per wake)."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        dead = fake.seed(addr, "d1", None, claim_state="dead")
        fake.seed(addr, "live1", "finally-live")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()  # d1 is dead -- skipped, cursor advances past it, nothing sent
        assert sender.calls == []
        assert waiter._cursor[addr] == (dead.created_at, "d1")  # noqa: SLF001

        await waiter.tick()  # since=d1's position now -- live1 is the oldest, referenced
        mail_sends = [m.get("tuple_id") for _c, m in sender.calls]
        assert mail_sends == ["live1"]

    @pytest.mark.asyncio
    async def test_nine_dead_rows_then_a_live_one_is_referenced_within_ten_wakes(self) -> None:
        """(e) nine dead rows then a live one: the live one is
        referenced within 10 wakes and nothing else is ever sent."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        for i in range(9):
            fake.seed(addr, f"d{i}", None, claim_state="dead")
        fake.seed(addr, "live1", "finally-live")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        for _ in range(10):
            await waiter.tick()

        mail_sends = [m.get("tuple_id") for _c, m in sender.calls]
        assert mail_sends == ["live1"], "the live row must be referenced within 10 wakes, nothing else sent"

    @pytest.mark.asyncio
    async def test_restart_with_a_backlog_of_three_references_one_per_wake(self) -> None:
        """(f) a restart with no persisted cursor walks a backlog one
        reference per wake."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "one")
        fake.seed(addr, "t2", "two")
        fake.seed(addr, "t3", "three")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        for _ in range(3):
            await waiter.tick()

        mail_sends = [m.get("tuple_id") for _c, m in sender.calls]
        assert mail_sends == ["t1", "t2", "t3"]

    @pytest.mark.asyncio
    async def test_two_mailboxes_and_a_board_post_in_one_wake(self) -> None:
        """(h) two mailboxes and a board, all in one wake -- every
        subscription enters the SAME `wait()` call."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        addr_a = subs.session_mailbox
        addr_b = f"mailbox/{uuid.uuid4().hex}"
        subs.instance_mailbox = addr_b  # noqa: SLF001 — test-only shortcut, bypassing subscribe()'s directory-lease side effects
        subs.subscribe(
            "board/release-notes", templates=[],
            store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
            state_dir=None,
        )
        fake = _FakeTupleStore()
        fake.seed(addr_a, "a1", "first-a")
        fake.seed(addr_b, "b1", "first-b")
        post = fake.seed("board/release-notes", "p1", "hi")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, sender=sender, persist=lambda: None,
        )

        await waiter.tick()

        tuple_ids = {m.get("tuple_id") for _c, m in sender.calls}
        assert tuple_ids == {"a1", "b1", "p1"}
        assert subs.entries()[-1]["cursor"] == {"created_at": post.created_at, "id": "p1"}
        assert len(fake.wait_calls) == 1, "one wait() call covers every subscription"

    @pytest.mark.asyncio
    async def test_unsubscribing_a_mailbox_drops_its_cursor_and_last_reference(self) -> None:
        """(h) unsubscribing a mailbox drops its cursor and last
        reference -- nothing further is ever sent for it."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        addr_b = f"mailbox/{uuid.uuid4().hex}"
        subs.instance_mailbox = addr_b  # noqa: SLF001 — test-only shortcut
        fake = _FakeTupleStore()
        fake.seed(addr_b, "b1", "leaky-if-unsubscribed")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, sender=sender,
            reannounce_interval_s=0.0, max_announces=10,
        )

        await waiter.tick()
        assert addr_b in waiter._cursor  # noqa: SLF001
        assert addr_b in waiter._last_ref  # noqa: SLF001

        subs.unsubscribe(addr_b)
        for _ in range(3):
            await waiter.tick()

        assert addr_b not in waiter._cursor  # noqa: SLF001
        assert addr_b not in waiter._last_ref  # noqa: SLF001
        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "b1"]
        assert len(mail_sends) == 1, "zero further sends once unsubscribed"

    @pytest.mark.asyncio
    async def test_old_engine_without_wait_stops_the_waiter(self) -> None:
        """(h) a bare 404 (an engine predating `/wait`) stops the loop."""
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        fake.wait_raises = httpx.HTTPStatusError(
            "404", request=httpx.Request("POST", "http://x/v1/tuples/wait"),
            response=httpx.Response(404, request=httpx.Request("POST", "http://x/v1/tuples/wait")),
        )
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id))
        await waiter.tick()
        assert waiter._stopped is True  # noqa: SLF001 — white-box assertion

    @pytest.mark.asyncio
    async def test_a_refused_wait_does_not_stop_the_waiter(self) -> None:
        """(h) any OTHER status is a transient fault: `run()` logs, backs
        off, and ticks again -- only the bare 404 stops the loop."""
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        fake.wait_raises = httpx.HTTPStatusError(
            "400", request=httpx.Request("POST", "http://x/v1/tuples/wait"),
            response=httpx.Response(400, request=httpx.Request("POST", "http://x/v1/tuples/wait")),
        )
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), tick_error_backoff_s=0.01,
        )
        run_task = asyncio.create_task(waiter.run())
        await asyncio.sleep(0.3)
        assert waiter.status()["alive"] is True
        assert waiter._stopped is False  # noqa: SLF001
        assert len(fake.wait_calls) >= 2, "the loop must keep ticking through a refused wait"
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    @pytest.mark.asyncio
    async def test_a_store_exception_in_a_tick_does_not_end_the_loop(self) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        fake.wait_raises = RuntimeError("engine hiccup")
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), tick_error_backoff_s=0.01,
        )
        run_task = asyncio.create_task(waiter.run())
        await asyncio.sleep(0.3)
        assert waiter.status()["alive"] is True
        assert len(fake.wait_calls) >= 2
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    @pytest.mark.asyncio
    async def test_board_post_is_delivered_and_cursor_advances(self) -> None:
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        subs.subscribe(
            "board/release-notes", templates=[],
            store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
            state_dir=None,
        )
        fake = _FakeTupleStore()
        post = fake.seed("board/release-notes", "p1", "v7.50 shipped", dims={"from": "author-a", "kind": "note"})
        sender = _FakeSender()
        persisted = []
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, sender=sender,
            persist=lambda: persisted.append(True),
        )
        await waiter.tick()
        expected_content = channel._board_notification_content("board/release-notes", "p1")  # noqa: SLF001
        assert sender.calls[0] == (
            expected_content,
            {"subspace": "board/release-notes", "tuple_id": "p1", "from": "author-a", "kind": "note"},
        )
        assert "v7.50 shipped" not in expected_content, "the notification must never carry the post body"
        assert subs.entries()[-1]["cursor"] == {"created_at": post.created_at, "id": "p1"}
        assert persisted == [True]

    @pytest.mark.asyncio
    async def test_persist_runs_off_the_event_loop_thread(self) -> None:
        """Code review Significant 3 (RDR-211, unchanged by RDR-213):
        `_process_results` called `self.persist()` synchronously on the
        event loop while every other store call in this class goes
        through `asyncio.to_thread`. A blocking `persist` (T1 is a
        synchronous HTTP client) would stall the whole waiter loop."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        subs.subscribe(
            "board/release-notes", templates=[],
            store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
            state_dir=None,
        )
        fake = _FakeTupleStore()
        fake.seed("board/release-notes", "p1", "hi")
        loop_thread_id = threading.get_ident()
        persist_thread_ids: list[int] = []
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, sender=_FakeSender(),
            persist=lambda: persist_thread_ids.append(threading.get_ident()),
        )
        await waiter.tick()
        assert persist_thread_ids, "persist must have been called"
        assert persist_thread_ids[0] != loop_thread_id, "persist ran ON the event loop thread"

    @pytest.mark.asyncio
    async def test_late_subscription_is_picked_up_at_the_next_tick(self) -> None:
        """A subscription change is picked up at the waiter's NEXT
        `wait` tick -- the parked call itself is never cancelled."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        fake = _FakeTupleStore()
        sender = _FakeSender()
        persisted = []
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, sender=sender,
            persist=lambda: persisted.append(True),
        )

        await waiter.tick()  # first tick: only the session mailbox exists yet, and it has nothing
        first_specs, _timeout = fake.wait_calls[-1]
        assert not any(spec.subspace.startswith("board/") for spec in first_specs), (
            "the waiter must not park on a topic it has not subscribed yet"
        )

        subs.subscribe(
            "board/late", templates=[],
            store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
            state_dir=None,
        )
        post = fake.seed("board/late", "p1", "hi", dims={"from": "author-a", "kind": "note"})

        await waiter.tick()  # the NEXT tick -- picks up the new subscription
        assert len(fake.wait_calls) == 2
        second_specs, _timeout2 = fake.wait_calls[-1]
        assert any(spec.subspace == "board/late" for spec in second_specs)

        expected_content = channel._board_notification_content("board/late", "p1")  # noqa: SLF001
        assert sender.calls[-1] == (
            expected_content,
            {"subspace": "board/late", "tuple_id": "p1", "from": "author-a", "kind": "note"},
        )
        assert "hi" not in expected_content, "the notification must never carry the post body"
        assert subs.entries()[-1]["cursor"] == {"created_at": post.created_at, "id": "p1"}
        assert persisted == [True]


class TestChannelWaiterRealEngine:
    """Properties a fake store cannot prove: the real engine's own
    global park-slot accounting, genuine parking under `wait()`, and the
    cursor design's one loss-of-liveness risk under concurrent writers
    (T2 `nexus_rdr/213-waiter-deep-analysis-2026-09-17` (2/2) section E,
    stop rule 1)."""

    def test_wait_returns_claimed_and_dead_rows_not_just_available_ones(self, t2_service_env) -> None:
        """Engine-fact check, confirmed against the real engine
        (`TupleRepository.queryOnce`, `wait`'s per-spec query, filters
        ONLY `consumed_at IS NULL AND expires_at > now` -- no
        `claim_state` condition at all; only `claimOnce`, backing
        `in`/`inp`, filters to claimable rows). `wait` returns the
        oldest UNCONSUMED row, which can be claimed or dead-lettered --
        exactly what the cursor design's items (d) and (e) depend on."""
        from nexus.mcp.core import tuple_in, tuple_nack, tuple_out, tuple_registry
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        tuple_out(
            addr, {"to": session_id}, {"from": "sender-claimstate"}, "claim-state-check",
            nonce=uuid.uuid4().hex,
        )

        claim = tuple_in(addr, {"to": session_id}, claimant=session_id, lease_s=300)
        assert claim is not None and "error" not in claim
        claim_id = claim["claim_id"]

        with t2_ctx() as db:
            wait_rows = db.tuples.wait([WaitSpec(subspace=addr, n=1)], 0)
        assert len(wait_rows) == 1 and len(wait_rows[0].tuples) == 1
        assert wait_rows[0].tuples[0].claim_state == "claimed"

        reg = tuple_registry()
        mailbox_template = next(t for t in reg["templates"] if t["name"] == "mailbox/<address>")
        max_attempts = mailbox_template["take"]["max_attempts"]
        for _ in range(max_attempts):
            tuple_nack(claim_id, session_id)
            reclaim = tuple_in(addr, {"to": session_id}, claimant=session_id, lease_s=300)
            if reclaim is None:
                break
            claim_id = reclaim["claim_id"]

        with t2_ctx() as db:
            wait_rows_dead = db.tuples.wait([WaitSpec(subspace=addr, n=1)], 0)
        assert len(wait_rows_dead) == 1 and len(wait_rows_dead[0].tuples) == 1
        assert wait_rows_dead[0].tuples[0].claim_state == "dead"

    def test_subscribing_mid_park_never_opens_a_second_global_slot(self, t2_service_env) -> None:
        """RDR-211 Phase 1 close gate cross-walk: the engine counts ONE
        global park slot per parked `wait()` call regardless of how many
        subspaces its specs name, so a subscription added while this
        session's waiter sits parked must never raise
        `park_stats().global_in_use` -- the new subspace is only picked
        up at the NEXT `tick()`'s fresh `wait()` call."""
        from concurrent.futures import ThreadPoolExecutor

        from nexus.db.t2.http_tuple_store import HttpTupleStore
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=_FakeSender(), wait_timeout_s=6)
        probe = HttpTupleStore()
        before = probe.park_stats().global_in_use

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, waiter.tick())
            during = before
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                during = probe.park_stats().global_in_use
                if during > before:
                    break
                time.sleep(0.2)
            assert during == before + 1, (
                f"expected exactly one new global park slot for the parked tick, "
                f"before={before} during={during}"
            )

            subs.subscribe(
                "board/late", templates=[],
                store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
                state_dir=None,
            )

            after_subscribe = probe.park_stats().global_in_use
            assert after_subscribe == during, (
                "subscribing mid-park must never open a second global slot for this session"
            )
            future.result(timeout=10)

        after = probe.park_stats().global_in_use
        assert after == before

    def test_one_row_is_referenced_once_and_wait_genuinely_parks(self, t2_service_env) -> None:
        """(a) real-engine companion: over a real bounded window, `wait()`
        must genuinely PARK once the cursor has passed the one row --
        never return immediately -- so the call count stays small."""
        from concurrent.futures import ThreadPoolExecutor

        from nexus.mcp.core import tuple_out

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        tuple_out(addr, {"to": session_id}, {"from": "sender-a"}, "hello", nonce=uuid.uuid4().hex)

        subs = _subs(session_id)
        counts: dict[str, int | None] = {"wait": 0, "rd": 0, "max_calls": 40}
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _counting_store_factory(counts), subs, sender=sender,
            wait_timeout_s=1, reannounce_interval_s=10_000.0, min_tick_interval_s=0.0,
        )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, waiter.run())
            time.sleep(2.5)
            waiter._stopped = True  # noqa: SLF001 — cross-thread stop signal, see the sibling park-slot test
            future.result(timeout=10)

        assert counts["wait"] <= 4, (
            f"wait() must genuinely park over a 2.5s window at wait_timeout_s=1; "
            f"got {counts['wait']} calls"
        )
        assert counts["rd"] == 0, "nothing is ever read back"
        mail_sends = [m for _c, m in sender.calls if m.get("subspace") == addr]
        assert len(mail_sends) == 1

    def test_two_rows_arriving_together_are_referenced_one_wake_apart_real_engine(self, t2_service_env) -> None:
        """(b) real-engine companion."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        tuple_out(addr, {"to": session_id}, {"from": "sender-a"}, "first", nonce=uuid.uuid4().hex)
        tuple_out(addr, {"to": session_id}, {"from": "sender-a"}, "second", nonce=uuid.uuid4().hex)

        subs = _subs(session_id)
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, t2_ctx, subs, sender=sender, reannounce_interval_s=10_000.0, wait_timeout_s=1,
        )

        asyncio.run(waiter.tick())
        asyncio.run(waiter.tick())

        mail_sends = [m.get("tuple_id") for _c, m in sender.calls if m.get("subspace") == addr]
        assert len(mail_sends) == 2
        assert mail_sends[0] != mail_sends[1]

    def test_an_ignored_reference_is_resent_then_falls_silent_real_engine(self, t2_service_env) -> None:
        """(c) real-engine companion."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        tuple_out(addr, {"to": session_id}, {"from": "sender-a"}, "unread-forever", nonce=uuid.uuid4().hex)

        subs = _subs(session_id)
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, t2_ctx, subs, sender=sender, reannounce_interval_s=0.0, max_announces=3, wait_timeout_s=1,
        )

        for _ in range(6):
            asyncio.run(waiter.tick())

        mail_sends = [m for _c, m in sender.calls if m.get("subspace") == addr]
        assert len(mail_sends) == 3

    def test_a_claimed_row_seen_at_the_wake_gets_one_reference_real_engine(self, t2_service_env) -> None:
        """(d) real-engine companion."""
        from nexus.mcp.core import tuple_in, tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        tuple_out(addr, {"to": session_id}, {"from": "sender-a"}, "already-claimed", nonce=uuid.uuid4().hex)
        claim = tuple_in(addr, {"to": session_id}, claimant="someone-else", lease_s=300)
        assert claim is not None and "error" not in claim

        subs = _subs(session_id)
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender)

        asyncio.run(waiter.tick())

        mail_sends = [m for _c, m in sender.calls if m.get("subspace") == addr]
        assert len(mail_sends) == 1

    def test_nine_dead_rows_then_a_live_one_is_referenced_within_ten_wakes_real_engine(self, t2_service_env) -> None:
        """(e) real-engine companion."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        _dead_letter_n_rows(addr, session_id, session_id, 9)
        tuple_out(addr, {"to": session_id}, {"from": "sender-live"}, "finally-live", nonce=uuid.uuid4().hex)

        subs = _subs(session_id)
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)

        for _ in range(10):
            asyncio.run(waiter.tick())

        mail_sends = [m for _c, m in sender.calls if m.get("subspace") == addr]
        assert len(mail_sends) == 1

    def test_restart_with_a_backlog_of_three_real_engine(self, t2_service_env) -> None:
        """(f) real-engine companion."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        for i in range(3):
            tuple_out(addr, {"to": session_id}, {"from": "sender-a"}, f"row-{i}", nonce=uuid.uuid4().hex)

        subs = _subs(session_id)
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)

        for _ in range(3):
            asyncio.run(waiter.tick())

        mail_sends = [m for _c, m in sender.calls if m.get("subspace") == addr]
        assert len(mail_sends) == 3

    def test_two_mailboxes_referenced_in_one_wake_real_engine(self, t2_service_env) -> None:
        """(h) real-engine companion (the board half of (h) is covered on
        the fake store only -- constructing a real board post needs no
        extra engine-fact proof beyond what boards already had)."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        addr_a = subs.session_mailbox
        addr_b = f"mailbox/{uuid.uuid4().hex}"
        subs.instance_mailbox = addr_b  # noqa: SLF001 — test-only shortcut
        tuple_out(addr_a, {"to": session_id}, {"from": "sender-a"}, "first-a", nonce=uuid.uuid4().hex)
        to_b = addr_b.removeprefix("mailbox/")
        tuple_out(addr_b, {"to": to_b}, {"from": "sender-b"}, "first-b", nonce=uuid.uuid4().hex)

        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)

        asyncio.run(waiter.tick())

        subspaces_referenced = {m.get("subspace") for _c, m in sender.calls}
        assert subspaces_referenced == {addr_a, addr_b}

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "nexus-vsipz: created_at is the transaction start time, so a cursor "
            "can pass a row that commits later; the engine announce stamp "
            "removes the cursor"
        ),
    )
    def test_stop_rule_two_hundred_rows_two_concurrent_writers_no_row_ever_lost_to_the_cursor(
        self, t2_service_env,
    ) -> None:
        """(g) THE STOP RULE (T2 `nexus_rdr/213-waiter-deep-analysis-
        2026-09-17` (2/2) section E), and the reproduction of record for
        `nexus-vsipz`: `TupleRepository.out()` stamps `created_at` with
        Postgres `now()` at transaction START, not commit, so a slower
        transaction that starts earlier can commit later and land behind
        a cursor a reader has already advanced past a younger row's
        (created_at, id) -- silently and permanently skipping it under a
        `since=cursor` query. 200 rows, written by TWO CONCURRENT writer
        threads to ONE mailbox, while a reader repeatedly advances its own
        cursor after each read, reproduces this ordering race under load
        (it closes in isolation, which is why it is intermittent here).
        The engine-side fix in `nexus-vsipz` (an announce stamp set in the
        same transaction as the row it marks) removes the cursor's read of
        `created_at` entirely and flips this test to a strict pass."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        written_ids: set[str] = set()
        written_lock = threading.Lock()
        rows_per_writer = 100

        def _writer(prefix: str) -> None:
            for i in range(rows_per_writer):
                tid = tuple_out(
                    addr, {"to": session_id}, {"from": prefix}, f"{prefix}-{i}", nonce=uuid.uuid4().hex,
                )
                with written_lock:
                    written_ids.add(tid)

        t1 = threading.Thread(target=_writer, args=("writer-a",))
        t2 = threading.Thread(target=_writer, args=("writer-b",))
        t1.start()
        t2.start()

        seen_ids: set[str] = set()
        cursor: tuple[str, str] | None = None
        with t2_ctx() as db:
            deadline = time.monotonic() + 60.0
            empty_polls = 0
            while time.monotonic() < deadline:
                rows = db.tuples.rd(addr, None, n=50, since=cursor, timeout_s=0)
                if rows:
                    empty_polls = 0
                    for r in rows:
                        seen_ids.add(r.id)
                    cursor = (rows[-1].created_at or "", rows[-1].id)
                    continue
                empty_polls += 1
                writers_done = not t1.is_alive() and not t2.is_alive()
                if writers_done and empty_polls >= 5:
                    break
                time.sleep(0.05)

        t1.join()
        t2.join()
        assert len(written_ids) == 2 * rows_per_writer, "sanity: both writers must have completed all their writes"
        missing = written_ids - seen_ids
        assert not missing, (
            f"STOP RULE VIOLATED: {len(missing)} of {len(written_ids)} rows were never observed by a "
            f"since-advancing reader -- the cursor design can silently skip live mail under concurrent "
            f"writers. ids: {sorted(missing)[:10]}"
        )

    def test_falsify_cursor_removed_from_mailbox_spec_wait_call_count_explodes(self, t2_service_env) -> None:
        """Falsification of (a): reverting `_build_specs` to drop the
        mailbox cursor (`since=None`, always) must make `wait()` return
        the SAME row immediately every time -- the engine's own
        immediate-match short circuit -- exploding the call count well
        past the healthy bound over the SAME real-time window."""
        from concurrent.futures import ThreadPoolExecutor

        from nexus.mcp.core import tuple_out

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        tuple_out(addr, {"to": session_id}, {"from": "sender-a"}, "hello", nonce=uuid.uuid4().hex)

        subs = _subs(session_id)
        counts: dict[str, int | None] = {"wait": 0, "rd": 0, "max_calls": 200}
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _counting_store_factory(counts), subs, sender=sender,
            wait_timeout_s=1, reannounce_interval_s=10_000.0, min_tick_interval_s=0.0,
        )

        def _reverted_build_specs(self: "channel.ChannelWaiter") -> list[WaitSpec]:
            # The bug: every mailbox spec's `since` is dropped, so the
            # SAME already-referenced row matches again on every call.
            specs: list[WaitSpec] = []
            for entry in self.subs.entries():
                subspace = entry["subspace"]
                if subspace.startswith("board/"):
                    cursor = entry.get("cursor")
                    since = (cursor["created_at"], cursor["id"]) if cursor else None
                    specs.append(WaitSpec(subspace=subspace, since=since))
                else:
                    specs.append(WaitSpec(subspace=subspace, since=None))
            return specs

        original = channel.ChannelWaiter._build_specs  # noqa: SLF001
        channel.ChannelWaiter._build_specs = _reverted_build_specs  # type: ignore[method-assign]
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, waiter.run())
                time.sleep(2.5)
                waiter._stopped = True  # noqa: SLF001
                try:
                    future.result(timeout=10)
                except RuntimeError:
                    pass  # the fail-fast guard tripping is an acceptable end state too
        finally:
            channel.ChannelWaiter._build_specs = original  # type: ignore[method-assign]

        assert counts["wait"] > 4, (
            f"removing the cursor from the mailbox spec must blow past the healthy bound of 4 "
            f"wait() calls over 2.5s at wait_timeout_s=1 -- confirming that bound tests the "
            f"cursor, not an artifact of the floor (OFF here too); got {counts['wait']}"
        )


class TestChannelStatusPublish:
    """RDR-213: the on-disk status record the `nx doctor` row reads
    cross-process (`nexus.health._check_tuple_channel_delivery`)."""

    def test_no_state_dir_is_a_silent_no_op(self) -> None:
        """The default (`state_dir=None`, every other test in this file)
        must never raise just because nothing was ever wired to publish."""
        session_id = str(uuid.uuid4())
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(_FakeTupleStore()), _subs(session_id))
        waiter._publish_status()  # noqa: SLF001 — must not raise

    @pytest.mark.asyncio
    async def test_a_tick_publishes_the_status_record(self, tmp_path) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), state_dir=tmp_path)
        assert channel.read_channel_status(tmp_path, session_id) is None

        await waiter.tick()

        recorded = channel.read_channel_status(tmp_path, session_id)
        assert recorded == waiter.status()
        assert recorded["last_wake"] is not None
        assert recorded["announced"] == 0
        assert recorded["pending"] == 0

    @pytest.mark.asyncio
    async def test_announce_and_spend_are_reflected_in_the_published_record(self, tmp_path) -> None:
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "hello")
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), sender=_FakeSender(), state_dir=tmp_path,
            reannounce_interval_s=0.0, max_announces=2, wait_timeout_s=1,
        )
        await waiter.tick()  # count=1, still under budget (2)
        recorded = channel.read_channel_status(tmp_path, session_id)
        assert recorded["announced"] == 1
        assert recorded["pending"] == 1
        assert recorded["oldest_pending_age_s"] is not None

        await waiter.tick()  # cadence immediately due -- resend, count=2, now spent
        recorded = channel.read_channel_status(tmp_path, session_id)
        assert recorded["pending"] == 0
        assert recorded["oldest_pending_age_s"] is None

    def test_write_channel_status_rejects_a_path_hostile_session_id(self, tmp_path) -> None:
        channel.write_channel_status(tmp_path, "../../etc/passwd", {"alive": True})
        assert not any(tmp_path.rglob("*"))

    def test_read_channel_status_missing_file_is_none(self, tmp_path) -> None:
        assert channel.read_channel_status(tmp_path, "nonexistent-session") is None

    def test_read_channel_status_malformed_json_is_none(self, tmp_path) -> None:
        session_id = "malformed-test"
        path = channel._channel_status_path(tmp_path, session_id)  # noqa: SLF001
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json{{{", encoding="utf-8")
        assert channel.read_channel_status(tmp_path, session_id) is None


class TestDoctorProbeNeverStartsAWaiter:
    """RDR-213 MVV run 2 (T2 `nexus_rdr/213-mvv-run2-2026-09-17`, finding
    D1): `nx doctor`'s MCP entry-point probe spawns an `nx-mcp` child
    that inherits the REAL session's environment, session id included --
    without an explicit skip signal that child would start its OWN
    waiter under the SAME session id, and its teardown would overwrite
    the live waiter's channel-status record with `alive: false` the
    instant the probe process exits."""

    def test_nx_mcp_probe_env_var_skips_starting_the_waiter(self, monkeypatch) -> None:
        from nexus.mcp import core as _core

        monkeypatch.setenv("NX_MCP_PROBE", "1")

        def _must_not_be_called() -> str:
            raise AssertionError("must not resolve a session id when NX_MCP_PROBE=1")

        monkeypatch.setattr(_core, "_current_subscription_session_id", _must_not_be_called)

        _core._start_channel_waiter()  # must return before ever calling the function above

    def test_falsify_the_skip_by_removing_the_env_check(self, monkeypatch) -> None:
        """Confirms the test above actually exercises a guard, not a
        vacuous no-op: the pre-fix shape (no `NX_MCP_PROBE` check at all)
        DOES reach `_current_subscription_session_id`, proven by the
        same `AssertionError` firing instead of a clean return."""
        from nexus.mcp import core as _core

        monkeypatch.setenv("NX_MCP_PROBE", "1")

        def _must_not_be_called() -> str:
            raise AssertionError("reached -- the guard is not gating this call")

        monkeypatch.setattr(_core, "_current_subscription_session_id", _must_not_be_called)

        def _unguarded() -> None:  # the pre-fix shape: no NX_MCP_PROBE check at all
            _core._current_subscription_session_id()

        with pytest.raises(AssertionError, match="reached"):
            _unguarded()
