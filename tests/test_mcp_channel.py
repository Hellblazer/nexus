# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-213 (amends RDR-211 Phase 1 Step 3, bead nexus-tk2cz): the
`claude/channel` capability declaration and the lifespan waiter
(`nexus.mcp.channel`), with the proof gate and claim-at-delivery deleted.

Every spec, mailbox or board, asks the ENGINE to gate cadence and cap
via a `WaitSpec.announce` field (bead nexus-vsipz for mailboxes, whose
stamp lives on the row; bead nexus-q82tk for boards, whose stamp lives
per subscriber in `nexus.tuple_deliveries`, `max=1`) -- superseding the
first RDR-213 cut's cursor-shares-one-shape design (T2 `nexus_rdr/213-
decision-announcements-rate-limited-not-ack-gated-2026-09-17`), which carried a
structural gap this module's stop-rule test measured directly: a cursor
keyed on `(created_at, id)` can skip a transaction that started earlier
but committed later, because a client-side position has no way to know a
slower sibling is still in flight. The engine's own re-scan of the
claimable-and-due set, ordered oldest first with no position to skip
past, cannot lose that row.

Layers, cheapest first:

- ``TestCapabilityDeclaration``: the SDK's own in-process memory-stream
  harness (``mcp.shared.memory``) drives a REAL low-level `Server.run()`
  round trip and reads the returned `InitializeResult` -- no stdio, no
  engine.
- ``TestChannelWaiterFakeStore``: a `_FakeTupleStore` -- a minimally
  stateful in-memory model of the engine's own `queryOnce`/announce-mode
  semantics (since/n/ordering/claim_state/announce) -- drives every
  `ChannelWaiter` branch deterministically.
- ``TestChannelWaiterRealEngine`` (``t2_service_env``): the properties a
  fake store cannot prove -- genuine parking against a real `wait()`,
  and (the stop rule) that announce mode, unlike a client-side cursor,
  never loses a row under concurrent writers.
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

from nexus.db.t2.records import Announce, TupleRow, WaitResult, WaitSpec
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
    `queryOnce`/announce-mode semantics (`TupleRepository.java`,
    confirmed live by `TestChannelWaiterRealEngine::
    test_wait_returns_claimed_and_dead_rows_not_just_available_ones` for
    the plain path): one append-only, creation-ordered table per
    subspace (`seed()` appends; nothing else adds rows).

    A spec with NO `announce` (boards, always) gets the OLD `queryOnce`
    contract unchanged: UNCONSUMED rows (claimed and dead-lettered
    included, never filtered on `claim_state`) strictly after `since`,
    capped at `n`, in `(created_at, id)` order.

    A spec WITH `announce` (mailboxes, since bead nexus-vsipz) gets the
    announce-mode contract instead: rows narrowed to CLAIMABLE (`consumed_at
    IS NULL`, `claim_state` neither `claimed` nor `dead` -- this fake has
    no lease to model a lapsed-claim exception to that, unlike the real
    engine) and DUE (never announced, or last announced longer than
    `interval_s` ago with `announce_count < max`), oldest `created_at`
    first, capped at `n`, and STAMPED (`announced_at`/`announce_count`
    incremented) on every row returned, in the SAME call -- `wait`'s
    announce branch never reads a row back afterward to confirm the
    stamp; the returned dataclass instance already carries it.

    Both branches return immediately with whatever currently matches --
    no real blocking here, since honouring `timeout_s` with a genuine
    wall-clock wait would cost the suite real seconds per empty-spec call
    for no test-value; a spec with no match is simply ABSENT from the
    result, never present with empty `tuples` (`WaitResult`'s own
    documented contract).

    `claim`/`release`/`dead_letter`/`consume` mutate a row's state,
    mirroring the real engine operation that produces each
    `claim_state`/`consumed_at` value (`tuple_in`, `tuple_release`,
    repeated `tuple_nack` to `max_attempts`, and an ack, respectively).
    `max_calls` is a fail-fast busy-loop guard, orthogonal to the
    stateful table -- neither subspace shape has a reconcile path any
    more, so `rd` is never called by the waiter at all; this fake still
    implements it (mirroring the plain read path) purely so a test can
    assert it stays at zero."""

    def __init__(self) -> None:
        self._rows: dict[str, list[TupleRow]] = {}
        #: (subspace, id) -> monotonic time of the row's last announce-mode
        #: stamp -- the fake's OWN timing state, kept separate from the
        #: `TupleRow.announced_at` string field (an ISO-shaped placeholder
        #: here, never parsed) so due-ness can be computed against a real
        #: clock without needing a real timestamp format.
        self._announced_monotonic: dict[tuple[str, str], float] = {}
        #: (subspace, subscriber, id) -> (monotonic time of the last
        #: per-subscriber announce, per-subscriber count): the fake's
        #: model of `nexus.tuple_deliveries` (bead nexus-q82tk). The row's
        #: own `announced_at`/`announce_count` are never touched by a
        #: per-subscriber announce, exactly as in the engine.
        self._deliveries: dict[tuple[str, str, str], tuple[float, int]] = {}
        #: Simulates engine-service-v0.1.128 (bead nexus-q82tk): `announce`
        #: is honoured but `subscriber` is never read -- the stamp lands on
        #: the ROW and the result echoes no subscriber.
        self.ignore_subscriber = False
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
        guarantees. `announced_at=None, announce_count=0` -- the column
        defaults a fresh row genuinely has (never `None` for
        `announce_count`, which is reserved for simulating an engine that
        predates this bead -- see `seed_old_engine_row`). Returns the row
        for convenience."""
        self._seq += 1
        row = TupleRow(
            id=id_, subspace=subspace, template=subspace.split("/")[0], keys={}, dims=dims or {},
            body=body, claim_state=claim_state, claimant=None, lease_until=None, attempts=0,
            consumed_at=None, consumed_by=None, expires_at=None, created_at=f"{self._seq:020d}",
            announced_at=None, announce_count=0,
        )
        self._rows.setdefault(subspace, []).append(row)
        return row

    def seed_old_engine_row(self, subspace: str, id_: str, body: str | None = None) -> TupleRow:
        """Like `seed`, but with `announce_count=None` -- simulating a row
        rendered by an engine that predates bead nexus-vsipz and never
        includes the field in its JSON at all (`TupleRow.announce_count`'s
        own docstring). Used only by the "old engine" detection test."""
        row = self.seed(subspace, id_, body)
        self._mutate(subspace, id_, announce_count=None)
        return self._rows[subspace][-1]

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

    # ── the plain (non-announce) read path -- boards, `rd`/`rdp` ────────

    def _unconsumed(self, subspace: str, since: tuple[str, str] | None, n: int) -> list[TupleRow]:
        rows = [r for r in self._rows.get(subspace, []) if r.consumed_at is None]
        if since is not None:
            rows = [r for r in rows if (r.created_at, r.id) > since]
        return rows[:n]

    def rd(self, subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0):
        self.rd_calls.append((subspace, keys_pattern, n, since, timeout_s))
        self._check_max_calls()
        return self._unconsumed(subspace, since, n)

    # ── the announce-mode read path -- mailboxes (bead nexus-vsipz) ─────

    def _announce_due(self, subspace: str, row: TupleRow, announce: Announce) -> bool:
        # `announce_count is None` (`seed_old_engine_row`) simulates an
        # engine that predates this bead entirely: it ignores `announce`
        # and answers via its plain path, which has no concept of
        # due-ness at all -- so every such row is unconditionally
        # included, never excluded, never stamped (see `_announce_rows`).
        if row.announce_count is None:
            return True
        if announce.subscriber is not None and not self.ignore_subscriber:
            seen = self._deliveries.get((subspace, announce.subscriber, row.id))
            if seen is None:
                return True
            last_at, count = seen
            return (time.monotonic() - last_at) >= announce.interval_s and count < announce.max
        last = self._announced_monotonic.get((subspace, row.id))
        if last is None:
            return True
        return (time.monotonic() - last) >= announce.interval_s and row.announce_count < announce.max

    def _announce_rows(self, subspace: str, n: int, announce: Announce) -> list[TupleRow]:
        claimable = [
            r for r in self._rows.get(subspace, [])
            if r.consumed_at is None and r.claim_state not in ("claimed", "dead")
        ]
        due = [r for r in claimable if self._announce_due(subspace, r, announce)]
        due = due[:n]
        stamped: list[TupleRow] = []
        for row in due:
            if row.announce_count is None:
                # Old-engine simulation: returned exactly as stored --
                # unstamped, `announce_count` still `None` -- never
                # mutated by an announce-mode call this fake models.
                stamped.append(row)
                continue
            if announce.subscriber is not None and not self.ignore_subscriber:
                key = (subspace, announce.subscriber, row.id)
                prior = self._deliveries.get(key)
                new_count = (prior[1] if prior else 0) + 1
                self._deliveries[key] = (time.monotonic(), new_count)
                # The returned row carries the PER-SUBSCRIBER post-stamp
                # values; the stored row is untouched (engine contract).
                stamped.append(dataclasses.replace(row, announced_at="stamped", announce_count=new_count))
                continue
            new_count = row.announce_count + 1
            self._announced_monotonic[(subspace, row.id)] = time.monotonic()
            updated = dataclasses.replace(row, announced_at="stamped", announce_count=new_count)
            self._mutate(subspace, row.id, announced_at="stamped", announce_count=new_count)
            stamped.append(updated)
        return stamped

    def wait(self, specs, timeout_s):
        self.wait_calls.append((list(specs), timeout_s))
        self._check_max_calls()
        if self.wait_raises is not None:
            raise self.wait_raises
        results = []
        for spec in specs:
            rows = (
                self._announce_rows(spec.subspace, spec.n, spec.announce)
                if spec.announce is not None
                else self._unconsumed(spec.subspace, spec.since, spec.n)
            )
            if rows:
                honoured = None
                if spec.announce is not None and not self.ignore_subscriber:
                    honoured = spec.announce.subscriber
                results.append(WaitResult(subspace=spec.subspace, tuples=rows, subscriber=honoured))
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


def _inject_extra_mailbox(subs, addr: str) -> None:
    """Test-only shortcut: give *subs* a SECOND delivered mailbox entry
    without driving the real `directory/<name>` lease machinery a genuine
    `subscribe("mailbox/<name>")` call performs, and let `unsubscribe(addr)`
    drop it again.

    Since RDR-208 Phase 3 (bead nexus-galkv.20), `SubscriptionSet` never
    tracks a second delivered mailbox at all -- a leased name arms a
    lease but is never listed by `entries()` (see that method's own
    docstring), so `subscribe()` can no longer produce this shape. These
    tests exist to prove `ChannelWaiter` handles more than one delivered
    mailbox generically (still true: nothing in the waiter assumes there
    is only ever one), so this patches `entries()`/`unsubscribe()` on the
    instance directly -- the same two methods a real subscribe/unsubscribe
    pair would drive -- rather than reaching for a subscription-set
    attribute that no longer exists."""
    base_entries = subs.entries
    base_unsubscribe = subs.unsubscribe
    state = {"active": True}

    def _entries():
        out = base_entries()
        if state["active"]:
            out.append({"subspace": addr})
        return out

    def _unsubscribe(subspace):
        if subspace == addr:
            state["active"] = False
            return
        base_unsubscribe(subspace)

    subs.entries = _entries
    subs.unsubscribe = _unsubscribe


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
    """Mailbox scenarios against the fake store's announce-mode branch
    (bead nexus-vsipz, RDR-213 engine half). Letters (a)-(f) mirror the
    original RDR-213 Test Plan's own scenario numbering, carried forward
    from the cursor design this bead supersedes."""

    @pytest.mark.asyncio
    async def test_one_row_is_referenced_once_and_stamped(self) -> None:
        """(a) one row: referenced exactly once; the engine's own stamp
        (`announce_count=1`) is what keeps it from matching again before
        `reannounce_interval_s` elapses -- there is no client-side cursor
        for this waiter to hold any more."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "hello")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()
        assert len(fake.wait_calls) == 1
        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 1
        assert waiter._last_seen[addr][0] == 1  # noqa: SLF001 -- announce_count

        await waiter.tick()  # not yet due for a re-send (default reannounce_interval_s=150)
        assert len(fake.wait_calls) == 2, "every tick calls wait() exactly once"
        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 1
        assert len(fake.rd_calls) == 0, "nothing is ever read back"

    @pytest.mark.asyncio
    async def test_two_rows_arriving_together_are_referenced_one_wake_apart(self) -> None:
        """(b) two rows arriving together: two references, one wake
        apart, in (created_at, id) order -- the mailbox spec's own `n=1`
        means only the oldest DUE row appears per wake, and a row just
        stamped is no longer due, so the second row surfaces next."""
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
        """(c) the ENGINE re-announces at `interval_s` up to `max` times
        (via the `announce` field this waiter now sends every tick),
        then never again -- this waiter applies no budget of its own."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "unread-forever")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), sender=sender,
            reannounce_interval_s=0.0, max_announces=5, wait_timeout_s=0,
        )

        for _ in range(8):  # comfortably past 5 -- must never exceed the cap
            await waiter.tick()

        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 5, "must stop at max_announces=5 and never exceed it"
        assert waiter.status()["announced"] == 1, "credited once, on the first send, never on a re-send"
        assert waiter.status()["pending"] == 0, "spent -- no longer counts as pending"

    @pytest.mark.asyncio
    async def test_a_new_row_supersedes_the_last_seen_state_of_the_old_one(self) -> None:
        """(c) a new row's own stamp is what this waiter's `_last_seen`
        reflects once the engine starts returning it instead -- by
        simple dict overwrite, never a merge of two rows' state."""
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
        await waiter.tick()  # t1 not yet due -- nothing new
        fake.seed(addr, "t2", "second")
        await waiter.tick()  # t2 is due (never announced); t1 is not -- t2 wins the n=1 slot

        mail_sends = [m.get("tuple_id") for _c, m in sender.calls]
        assert mail_sends == ["t1", "t2"]
        assert waiter.status()["announced"] == 2
        assert waiter.status()["pending"] == 1, "one entry per mailbox -- t2's last-seen state, not both"

    @pytest.mark.asyncio
    async def test_a_claimed_row_is_never_returned_by_announce_mode_while_live(self) -> None:
        """(d), REVISED under bead nexus-vsipz: the engine's own
        claimable filter excludes a claimed-and-live row from announce
        mode entirely -- the opposite of the cursor design's own
        behaviour, which referenced it once regardless. Released, it
        becomes claimable again and is referenced on the next tick (the
        real engine's own count-continuation across a claim/release
        cycle is proven server-side, not by this fake: `TupleAnnounceTest
        .announce_claimedRow_excludedWhileLive_returnedAfterRelease_withCountContinuing`)."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "t1", "already-claimed", claim_state="claimed")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), sender=sender,
            reannounce_interval_s=0.0, wait_timeout_s=0,
        )

        await waiter.tick()
        assert sender.calls == [], "claimed-and-live -- announce mode must not see it at all"
        assert waiter.status()["announced"] == 0

        fake.release(addr, "t1")
        await waiter.tick()
        mail_sends = [m for _c, m in sender.calls if m.get("tuple_id") == "t1"]
        assert len(mail_sends) == 1, "released -- now claimable, and due"
        assert waiter.status()["announced"] == 1

    @pytest.mark.asyncio
    async def test_a_dead_row_is_never_returned_and_the_live_row_behind_it_is_referenced_immediately(
        self,
    ) -> None:
        """(e), REVISED under bead nexus-vsipz: a dead-lettered row is
        excluded from announce mode's match entirely -- not skipped one
        tick at a time via a cursor, simply never a candidate -- so the
        live row behind it is the oldest CLAIMABLE-and-due row from the
        very FIRST tick, not the second."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed(addr, "d1", None, claim_state="dead")
        fake.seed(addr, "live1", "finally-live")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()
        mail_sends = [m.get("tuple_id") for _c, m in sender.calls]
        assert mail_sends == ["live1"], "the dead row is never a candidate -- live1 wins the FIRST tick"

    @pytest.mark.asyncio
    async def test_nine_dead_rows_then_a_live_one_is_referenced_on_the_first_wake(self) -> None:
        """(e) nine dead rows then a live one: referenced on the FIRST
        wake (the engine's claimable filter excludes all nine before
        `n=1`/ordering is even applied) -- a strictly better bound than
        the cursor design's own "within 10 wakes"."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        for i in range(9):
            fake.seed(addr, f"d{i}", None, claim_state="dead")
        fake.seed(addr, "live1", "finally-live")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()

        mail_sends = [m.get("tuple_id") for _c, m in sender.calls]
        assert mail_sends == ["live1"], "referenced on the first wake; nothing else ever sent"

    @pytest.mark.asyncio
    async def test_restart_with_a_backlog_of_three_references_one_per_wake(self) -> None:
        """(f) a restart with no persisted state walks a backlog one
        reference per wake: a never-announced row is always due, but
        `n=1` caps one per tick, and the just-announced row is not due
        again before `reannounce_interval_s` (default 150s), so the
        NEXT-oldest never-announced row wins the following tick."""
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
    async def test_engine_that_never_renders_announce_count_stops_the_waiter(self) -> None:
        """The refusal this bead adds, mirroring the existing 404-on-
        `/wait` rule: an engine that ignores `announce` entirely (one
        predating nexus-vsipz) never renders `announce_count` on any
        row, including one matched by an announce-mode mailbox spec --
        detected on the FIRST such row, logged, and the waiter stops
        rather than spin against a substrate that cannot honour the
        cadence/cap it asked for."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        fake.seed_old_engine_row(addr, "t1", "from-an-old-engine")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), _subs(session_id), sender=sender)

        await waiter.tick()

        assert waiter._stopped is True  # noqa: SLF001 -- white-box assertion, mirrors the 404 test
        assert sender.calls == [], "must stop BEFORE referencing a row it cannot trust the stamp of"

    @pytest.mark.asyncio
    async def test_two_mailboxes_and_a_board_post_in_one_wake(self) -> None:
        """(h) two mailboxes and a board, all in one wake -- every
        subscription enters the SAME `wait()` call."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        addr_a = subs.session_mailbox
        addr_b = f"mailbox/{uuid.uuid4().hex}"
        _inject_extra_mailbox(subs, addr_b)
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
            session_id, _fake_store_factory(fake), subs, sender=sender,
        )

        await waiter.tick()

        tuple_ids = {m.get("tuple_id") for _c, m in sender.calls}
        assert tuple_ids == {"a1", "b1", "p1"}
        assert post.announce_count == 0, "the board row's own columns are never stamped (per-subscriber)"
        assert len(fake.wait_calls) == 1, "one wait() call covers every subscription"

    @pytest.mark.asyncio
    async def test_unsubscribing_a_mailbox_drops_its_last_seen_state(self) -> None:
        """(h) unsubscribing a mailbox drops its `_last_seen` bookkeeping
        -- nothing further is ever sent for it."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        addr_b = f"mailbox/{uuid.uuid4().hex}"
        _inject_extra_mailbox(subs, addr_b)
        fake = _FakeTupleStore()
        fake.seed(addr_b, "b1", "leaky-if-unsubscribed")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, sender=sender,
            reannounce_interval_s=0.0, max_announces=10,
        )

        await waiter.tick()
        assert addr_b in waiter._last_seen  # noqa: SLF001

        subs.unsubscribe(addr_b)
        for _ in range(3):
            await waiter.tick()

        assert addr_b not in waiter._last_seen  # noqa: SLF001
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
    async def test_board_post_is_delivered_once_per_subscriber_via_the_engine_stamp(self) -> None:
        """Bead nexus-q82tk: a board spec carries `announce` with this
        session's id as `subscriber` and `max=1`; the engine's per-
        subscriber stamp returns the post once and never again, and the
        waiter keeps no cursor and persists nothing."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        subs.subscribe(
            "board/release-notes", templates=[],
            store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
            state_dir=None,
        )
        fake = _FakeTupleStore()
        fake.seed("board/release-notes", "p1", "v7.50 shipped", dims={"from": "author-a", "kind": "note"})
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, sender=sender, reannounce_interval_s=0.0,
        )
        await waiter.tick()
        expected_content = channel._board_notification_content("board/release-notes", "p1")  # noqa: SLF001
        assert sender.calls[0] == (
            expected_content,
            {"subspace": "board/release-notes", "tuple_id": "p1", "from": "author-a", "kind": "note"},
        )
        assert "v7.50 shipped" not in expected_content, "the notification must never carry the post body"
        assert "cursor" not in expected_content
        specs, _timeout = fake.wait_calls[-1]
        board_spec = next(sp for sp in specs if sp.subspace == "board/release-notes")
        assert board_spec.since is None
        assert board_spec.announce == Announce(
            interval_s=0, max=channel.DEFAULT_BOARD_MAX_ANNOUNCES, subscriber=session_id,
        )
        assert all("cursor" not in e for e in subs.entries())

        for _ in range(3):
            await waiter.tick()
        board_sends = [m for _c, m in sender.calls if m.get("subspace") == "board/release-notes"]
        assert len(board_sends) == 1, "max=1: announced once to this subscriber, interval 0 notwithstanding"

    @pytest.mark.asyncio
    async def test_engine_that_ignores_the_subscriber_stops_the_waiter_before_any_board_send(self) -> None:
        """Bead nexus-q82tk: engine-service-v0.1.128 honours `announce`
        but never reads `subscriber`, so it stamps the board ROW and
        echoes no subscriber. A local install converges to the floor
        rather than refusing at spawn, so this pairing is real for the
        convergence window; the waiter must stop loud with
        `no_subscriber_support` before sending a reference whose stamp
        would silence the post for every other subscriber."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        subs.subscribe(
            "board/release-notes", templates=[],
            store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
            state_dir=None,
        )
        fake = _FakeTupleStore()
        fake.ignore_subscriber = True
        fake.seed("board/release-notes", "p1", "hi")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), subs, sender=sender)

        await waiter.tick()

        assert waiter.status()["stopped_reason"] == "no_subscriber_support"
        assert waiter._stopped is True  # noqa: SLF001
        assert sender.calls == [], "must stop BEFORE referencing a row whose stamp it cannot trust"

    @pytest.mark.asyncio
    async def test_two_subscribers_each_get_a_board_post_once(self) -> None:
        """Bead nexus-q82tk: the stamp is per subscriber. A second
        session's waiter over the same fake (one engine) is announced the
        same post once, unaffected by the first's stamp."""
        fake = _FakeTupleStore()
        fake.seed("board/shared", "p1", "hi")
        senders = []
        for _ in range(2):
            session_id = str(uuid.uuid4())
            subs = _subs(session_id)
            subs.subscribe(
                "board/shared", templates=[],
                store_factory=lambda: (_ for _ in ()).throw(AssertionError("must not touch the store")),
                state_dir=None,
            )
            sender = _FakeSender()
            senders.append(sender)
            waiter = channel.ChannelWaiter(
                session_id, _fake_store_factory(fake), subs, sender=sender, reannounce_interval_s=0.0,
            )
            await waiter.tick()
            await waiter.tick()
        for sender in senders:
            board_sends = [m for _c, m in sender.calls if m.get("subspace") == "board/shared"]
            assert len(board_sends) == 1

    @pytest.mark.asyncio
    async def test_late_subscription_is_picked_up_at_the_next_tick(self) -> None:
        """A subscription change is picked up at the waiter's NEXT
        `wait` tick -- the parked call itself is never cancelled."""
        session_id = str(uuid.uuid4())
        subs = _subs(session_id)
        fake = _FakeTupleStore()
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, _fake_store_factory(fake), subs, sender=sender)

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
        fake.seed("board/late", "p1", "hi", dims={"from": "author-a", "kind": "note"})

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


class TestChannelWaiterRealEngine:
    """Properties a fake store cannot prove: the real engine's own
    global park-slot accounting, genuine parking under `wait()`, and (the
    stop rule) that announce mode, unlike a client-side cursor, never
    loses a row under concurrent writers (T2 `nexus_rdr/213-waiter-deep-
    analysis-2026-09-17` (2/2) section E, stop rule 1)."""

    def test_a_leased_name_is_never_referenced_by_the_waiter(self, t2_service_env, tmp_path) -> None:
        """RDR-208 Phase 3 (bead nexus-galkv.20), the Transition Test
        Plan's "then stops" half at the waiter itself: a genuine
        `subscribe("mailbox/<name>")` call arms the name's
        `directory/<name>` lease through the REAL engine, but the waiter
        never builds a `WaitSpec` for it and never references a message
        sent to `mailbox/<name>` -- only this session's OWN mailbox is
        ever referenced. THE FALSIFIER: reverting `entries()` to include
        the leased name (this bead's actual code change) makes this fail,
        since the fake sender would then also see the leaked address."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp.subscriptions import SubscriptionSet
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        subs = SubscriptionSet(session_id=session_id)
        name = f"inst-{uuid.uuid4().hex[:8]}"
        try:
            subs.subscribe(f"mailbox/{name}", templates=[], store_factory=t2_ctx, state_dir=tmp_path)
            tuple_out(
                f"mailbox/{name}", {"to": name}, {"from": "sender-leaked"},
                "should never be referenced", nonce=uuid.uuid4().hex,
            )
            tuple_out(
                subs.session_mailbox, {"to": session_id}, {"from": "sender-own"},
                "own mailbox", nonce=uuid.uuid4().hex,
            )

            sender = _FakeSender()
            waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)
            asyncio.run(waiter.tick())

            subspaces_referenced = {m.get("subspace") for _c, m in sender.calls}
            assert subspaces_referenced == {subs.session_mailbox}
        finally:
            subs.shutdown()

    def test_wait_returns_claimed_and_dead_rows_not_just_available_ones(self, t2_service_env) -> None:
        """Engine-fact check, confirmed against the real engine
        (`TupleRepository.queryOnce`'s PLAIN branch -- no `announce` on
        the spec -- filters ONLY `consumed_at IS NULL AND expires_at >
        now`, no `claim_state` condition at all; only the announce-mode
        branch, and `claimOnce` backing `in`/`inp`, filter to claimable
        rows). A plain `wait` (a board's own spec, always; a mailbox
        spec with no `announce`) returns the oldest UNCONSUMED row
        regardless of claim state -- UNCHANGED by bead nexus-vsipz, which
        only narrows the ANNOUNCE-MODE branch. Items (d)/(e) below now
        depend on the OPPOSITE of this for a mailbox's own `announce`
        spec -- see their own docstrings."""
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
        must genuinely PARK once the engine's stamp excludes the one row --
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

    def test_a_claimed_row_is_never_referenced_by_announce_mode_real_engine(self, t2_service_env) -> None:
        """(d), REVISED under bead nexus-vsipz: real-engine companion of
        the claimable-exclusion fake test -- a claimed-and-live row is
        never returned by announce mode at all."""
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
        assert len(mail_sends) == 0, "claimed-and-live -- the engine's claimable filter must exclude it"

    def test_nine_dead_rows_then_a_live_one_is_referenced_on_the_first_wake_real_engine(
        self, t2_service_env,
    ) -> None:
        """(e), REVISED under bead nexus-vsipz: real-engine companion --
        the live row is referenced on the FIRST wake, since the engine's
        claimable filter excludes all nine dead rows from the match
        entirely, rather than the cursor design's "within 10 wakes"."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        _dead_letter_n_rows(addr, session_id, session_id, 9)
        tuple_out(addr, {"to": session_id}, {"from": "sender-live"}, "finally-live", nonce=uuid.uuid4().hex)

        subs = _subs(session_id)
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)

        asyncio.run(waiter.tick())

        mail_sends = [m for _c, m in sender.calls if m.get("subspace") == addr]
        assert len(mail_sends) == 1, "referenced on the FIRST wake"

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
        _inject_extra_mailbox(subs, addr_b)
        tuple_out(addr_a, {"to": session_id}, {"from": "sender-a"}, "first-a", nonce=uuid.uuid4().hex)
        to_b = addr_b.removeprefix("mailbox/")
        tuple_out(addr_b, {"to": to_b}, {"from": "sender-b"}, "first-b", nonce=uuid.uuid4().hex)

        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)

        asyncio.run(waiter.tick())

        subspaces_referenced = {m.get("subspace") for _c, m in sender.calls}
        assert subspaces_referenced == {addr_a, addr_b}

    def test_stop_rule_board_path_two_concurrent_writers_no_post_ever_lost(self, t2_service_env) -> None:
        """(g) THE STOP RULE, board path (bead nexus-q82tk; the Java-side
        deterministic proof is `TupleAnnounceTest.subscriberAnnounce_
        lateCommittingPost_isStillAnnouncedToTheSubscriber`, which holds a
        transaction open across a faster sibling's commit). This drives
        the REAL delivery path -- `_build_specs`'s board arm, the engine's
        per-subscriber stamp, `_process_results`, `_deliver_board_post`
        -- through `ChannelWaiter.tick()` against the real engine, with
        two writers posting to one board concurrently, and asserts every
        post is referenced to this subscriber exactly once. The client-
        side `since` cursor this replaced could skip a post whose
        transaction started earlier but committed later (`created_at` is
        the transaction START time); it was an xfail here until this
        bead. Strict now: a lost or duplicated post fails."""
        from nexus.mcp.core import tuple_out
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        topic = f"stop-rule-{uuid.uuid4().hex}"
        board = f"board/{topic}"
        subs = _subs(session_id)
        subs.subscribe(
            board, templates=[],
            store_factory=lambda: (_ for _ in ()).throw(AssertionError("a board subscription never touches the store")),
            state_dir=None,
        )
        written_ids: set[str] = set()
        written_lock = threading.Lock()
        rows_per_writer = 100

        def _writer(prefix: str) -> None:
            for i in range(rows_per_writer):
                tid = tuple_out(board, {"topic": topic}, {"from": prefix}, f"{prefix}-{i}", nonce=uuid.uuid4().hex)
                with written_lock:
                    written_ids.add(tid)

        sender = _FakeSender()
        waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)

        t1 = threading.Thread(target=_writer, args=("writer-a",))
        t2 = threading.Thread(target=_writer, args=("writer-b",))
        t1.start()
        t2.start()

        deadline = time.monotonic() + 90.0
        quiet_ticks = 0
        while time.monotonic() < deadline:
            before = len(sender.calls)
            asyncio.run(waiter.tick())
            if len(sender.calls) > before:
                quiet_ticks = 0
                continue
            quiet_ticks += 1
            if not t1.is_alive() and not t2.is_alive() and quiet_ticks >= 3:
                break

        t1.join()
        t2.join()
        assert len(written_ids) == 2 * rows_per_writer, "sanity: both writers must have completed all their writes"
        seen = [m.get("tuple_id") for _c, m in sender.calls if m.get("subspace") == board]
        missing = written_ids - set(seen)
        assert not missing, (
            f"STOP RULE VIOLATED: {len(missing)} of {len(written_ids)} posts were never announced to the "
            f"subscriber through the board path. ids: {sorted(missing)[:10]}"
        )
        assert len(seen) == len(set(seen)), "max=1 per subscriber: no post is announced twice"

    def test_announce_mode_never_loses_a_row_to_a_concurrent_writer_skew(self, t2_service_env) -> None:
        """(g) THE STOP RULE, mailbox path (bead nexus-vsipz, RDR-213
        engine half; T2 `nexus_rdr/213-waiter-deep-analysis-2026-09-17`
        (2/2) section E, and the Java-side deterministic proof
        `TupleAnnounceTest.announce_lateCommittingRow_isReturnedAtNextCall`,
        which holds a transaction open across a faster sibling's commit
        to reproduce the skew exactly -- Python cannot control engine
        transaction boundaries over HTTP, so this test reproduces the
        SAME class of risk statistically, the way the sibling `since`-
        cursor test above already does).

        `max=1` makes each row's own announce budget a ONE-SHOT: once
        the engine has returned it, it is permanently excluded
        (`announce_count(1)` is never `< max(1)` again), which is what
        lets an `n=1` poll loop DRAIN a backlog exactly the way a
        cursor-advancing `rd` loop would -- except announce mode has NO
        cursor to skip past, so a row that commits out of `created_at`
        order relative to its siblings is still the oldest UNSTAMPED
        claimable row the next time anyone asks, and gets picked up
        regardless of when it happened to commit."""
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
        spec = WaitSpec(subspace=addr, n=1, announce=Announce(interval_s=0, max=1))
        with t2_ctx() as db:
            deadline = time.monotonic() + 60.0
            empty_polls = 0
            while time.monotonic() < deadline:
                results = db.tuples.wait([spec], 0)
                rows = results[0].tuples if results else []
                if rows:
                    empty_polls = 0
                    for r in rows:
                        seen_ids.add(r.id)
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
            f"{len(missing)} of {len(written_ids)} rows were never observed by announce mode's own "
            f"one-shot drain -- this would be the same stop-rule violation the since-cursor test above "
            f"guards against, and announce mode is supposed to be immune to it. ids: {sorted(missing)[:10]}"
        )

    def test_falsify_announce_removed_from_mailbox_spec_wait_call_count_explodes(self, t2_service_env) -> None:
        """Falsification of (a): reverting `_build_specs` to send a
        mailbox spec with NO `announce` field at all must make `wait()`
        return the SAME never-excluded row immediately every time -- the
        engine's own immediate-match short circuit on the plain
        (non-announce) path -- exploding the call count well past the
        healthy bound over the SAME real-time window."""
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
            # The bug: every mailbox spec's `announce` is dropped, so the
            # SAME already-referenced (and never excluded) row matches
            # again on every call -- the plain `queryOnce` path has no
            # claim_state/due filtering at all.
            specs: list[WaitSpec] = []
            for entry in self.subs.entries():
                subspace = entry["subspace"]
                specs.append(WaitSpec(subspace=subspace, n=1))
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
            f"removing `announce` from the mailbox spec must blow past the healthy bound of 4 "
            f"wait() calls over 2.5s at wait_timeout_s=1 -- confirming that bound tests announce "
            f"mode, not an artifact of the floor (OFF here too); got {counts['wait']}"
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
