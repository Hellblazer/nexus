# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.10): the `claude/channel`
capability declaration and the lifespan waiter (`nexus.mcp.channel`).

Layers, cheapest first:

- ``TestCapabilityDeclaration``: the SDK's own in-process memory-stream
  harness (``mcp.shared.memory``) drives a REAL low-level `Server.run()`
  round trip and reads the returned `InitializeResult` -- no stdio, no
  engine.
- ``TestChannelArgvGate``: pure, a fake `ps` reader.
- ``TestChannelWaiterFakeStore``: a `_FakeTupleStore` (mirrors
  ``tests/test_subscriptions.py``'s own `_FakeTuples`) drives every
  `ChannelWaiter` branch deterministically with short injected constants
  -- back pressure, the re-send cap, the old-engine 404 stop, board
  delivery + cursor advance, and the probe gate.
- ``TestChannelWaiterRealEngine`` (``t2_service_env``): the one property a
  fake store cannot prove -- the engine's own same-claimant retake, which
  is what makes a restarted waiter's retake spend no attempt.
- ``TestCreditHookWiring``: `tuple_ack`/`tuple_nack` call
  `channel.note_credit`; `tuple_channel_probe` calls
  `channel.note_probe_ack`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from nexus.db.t2.records import TupleRow, WaitResult
from nexus.mcp import channel


def _row(
    id_: str, subspace: str, body: str | None, *, dims: dict[str, str] | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> TupleRow:
    return TupleRow(
        id=id_, subspace=subspace, template=subspace.split("/")[0], keys={}, dims=dims or {},
        body=body, claim_state=None, claimant=None, lease_until=None, attempts=0,
        consumed_at=None, consumed_by=None, expires_at=None, created_at=created_at,
    )


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


class TestChannelArgvGate:
    def test_channels_flag_is_detected(self) -> None:
        assert channel.detect_channel_argv(123, argv_reader=lambda _pid: "claude --channels server:nexus")

    def test_dangerously_load_development_channels_flag_is_detected(self) -> None:
        argv = "claude --dangerously-load-development-channels server:nexus"
        assert channel.detect_channel_argv(123, argv_reader=lambda _pid: argv)

    def test_plain_launch_is_not_detected(self) -> None:
        assert not channel.detect_channel_argv(123, argv_reader=lambda _pid: "claude --resume abc123")

    def test_unreadable_argv_is_not_detected(self) -> None:
        assert not channel.detect_channel_argv(123, argv_reader=lambda _pid: "")

    def test_default_reader_never_raises_on_a_bogus_pid(self) -> None:
        # No injected reader: exercises the real `ps` subprocess path
        # against a pid essentially guaranteed not to exist.
        assert channel._read_parent_command(999_999) == "" or isinstance(channel._read_parent_command(999_999), str)


class _FakeTupleStore:
    """Records every call; `wait`/`rd`/`in_` are pre-loaded with canned
    return values per test. Mirrors `tests/test_subscriptions.py`'s own
    `_FakeTuples` in spirit -- a store double, never a real engine."""

    def __init__(self) -> None:
        self.wait_calls: list[tuple[list, int]] = []
        self.rd_calls: list[str] = []
        self.in_calls: list[tuple[str, str, int]] = []
        self.renew_calls: list[tuple[str, str, int]] = []
        self.release_calls: list[tuple[str, str]] = []
        self.wait_results: list[list[WaitResult]] = []
        self.wait_raises: Exception | None = None
        self.rd_results: dict[str, list[TupleRow]] = {}
        self.in_results: dict[str, tuple[TupleRow, str] | None] = {}

    def wait(self, specs, timeout_s):
        self.wait_calls.append((list(specs), timeout_s))
        if self.wait_raises is not None:
            raise self.wait_raises
        return self.wait_results.pop(0) if self.wait_results else []

    def rd(self, subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0):
        self.rd_calls.append(subspace)
        return list(self.rd_results.get(subspace, []))

    def in_(self, subspace, keys_pattern, *, claimant, lease_s, timeout_s=0):
        self.in_calls.append((subspace, claimant, lease_s))
        return self.in_results.get(subspace)

    def renew(self, claim_id, claimant, lease_s):
        self.renew_calls.append((claim_id, claimant, lease_s))
        return datetime.now(UTC)

    def release(self, claim_id, claimant):
        self.release_calls.append((claim_id, claimant))


class _Db:
    def __init__(self, fake: _FakeTupleStore) -> None:
        self.tuples = fake

    def __enter__(self) -> "_Db":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _fake_store_factory(fake: _FakeTupleStore):
    return lambda: _Db(fake)


class _FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, content: str, meta: dict[str, str]) -> bool:
        self.calls.append((content, meta))
        return True


def _subs(session_id: str):
    from nexus.mcp.subscriptions import SubscriptionSet

    return SubscriptionSet(session_id=session_id)


class TestChannelWaiterFakeStore:
    def test_argv_proof_starts_channel_live(self) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=True,
        )
        assert waiter.channel_live.is_set()
        assert waiter.status()["proof"] == "argv"

    @pytest.mark.asyncio
    async def test_probe_gate_unblocks_on_note_probe_ack(self) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=False, sender=sender,
        )
        assert not waiter.channel_live.is_set()
        assert waiter.status()["proof"] == "none"
        run_task = asyncio.create_task(waiter.run())
        await asyncio.sleep(0.05)
        # Sent exactly the one probe notification, never claimed anything.
        assert any(meta.get("kind") == "channel_probe" for _content, meta in sender.calls)
        assert fake.in_calls == []
        waiter.note_probe_ack()
        await asyncio.sleep(0.05)
        assert waiter.status()["proof"] == "probe"
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    @pytest.mark.asyncio
    async def test_never_probed_never_claims(self) -> None:
        """"No call, no claim, ever, in that process" (T2
        nexus_rdr/211-decision-waiter-gate-2026-09-17)."""
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        row = _row("t1", f"mailbox/{session_id}", "hello")
        fake.rd_results[f"mailbox/{session_id}"] = [row]
        fake.in_results[f"mailbox/{session_id}"] = (row, "claim-1")
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=False, sender=_FakeSender(),
        )
        run_task = asyncio.create_task(waiter.run())
        await asyncio.sleep(0.1)
        assert fake.in_calls == []
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    @pytest.mark.asyncio
    async def test_back_pressure_second_claim_withheld_until_credit(self) -> None:
        """Mutation-check target: removing the `if self._outstanding is
        None` guard around `_maybe_claim_mail` (or leaving the mailbox
        spec in `_build_specs` while a claim is outstanding) must fail
        this test."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        row1 = _row("t1", addr, "first")
        row2 = _row("t2", addr, "second")
        fake.rd_results[addr] = [row1]
        fake.in_results[addr] = (row1, "claim-1")
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=True, sender=sender,
            renew_interval_s=10_000.0,  # never due within this test
        )

        await waiter.tick()  # claims row1
        assert len(fake.in_calls) == 1
        assert waiter.status()["unacked"] == 1
        assert sender.calls[-1] == ("first", waiter._outstanding.meta)  # noqa: SLF001 — white-box assertion

        # A second tick, still no credit: the mailbox must be OUT of the
        # wait spec list, and _maybe_claim_mail must not run at all.
        fake.rd_results[addr] = [row2]  # a second message has since arrived
        fake.in_results[addr] = (row2, "claim-2")
        await waiter.tick()
        assert len(fake.in_calls) == 1, "a second claim was attempted before the first was credited"
        assert not any(spec.subspace == addr for specs, _t in fake.wait_calls[-1:] for spec in specs)

        # The credit arrives (as tuple_ack/tuple_nack would supply it) --
        # only NOW may the second message be claimed.
        waiter.note_credit("claim-1")
        await waiter.tick()
        assert len(fake.in_calls) == 2
        assert sender.calls[-1] == ("second", waiter._outstanding.meta)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_resend_cap_then_release(self) -> None:
        """Mutation-check target: widening `max_resends` or dropping the
        `>=` -> `>` comparison must fail this test at the boundary."""
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        row = _row("t1", addr, "unacked-forever")
        fake.rd_results[addr] = [row]
        fake.in_results[addr] = (row, "claim-1")

        # Once released, the fake "forgets" the row -- simulating that
        # nothing else is waiting to claim it -- so the SAME tick's
        # unconditional re-check (`tick()` always tries `_maybe_claim_mail`
        # once `_outstanding` clears) does not immediately re-claim it and
        # mask the release this test is asserting.
        orig_release = fake.release

        def _release_and_forget(claim_id: str, claimant: str) -> None:
            orig_release(claim_id, claimant)
            fake.rd_results[addr] = []
            fake.in_results[addr] = None

        fake.release = _release_and_forget

        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=True, sender=sender,
            renew_interval_s=0.0, max_resends=5,  # every subsequent tick is immediately "due"
        )

        await waiter.tick()  # the initial claim + send (not a resend)
        assert waiter.status()["unacked"] == 1
        for _ in range(5):
            await waiter.tick()
        assert len(fake.renew_calls) == 5
        assert len(fake.release_calls) == 0, "released before exhausting all five re-sends"
        assert waiter.status()["unacked"] == 1

        await waiter.tick()  # the sixth check: exhausted -> release
        assert len(fake.release_calls) == 1
        assert fake.release_calls[0] == ("claim-1", waiter.claimant)
        assert waiter.status()["unacked"] == 0
        assert waiter.status()["released"] == 1
        # One initial send + five re-sends, all carrying the SAME content/claim_id.
        mail_sends = [c for c in sender.calls if c[1].get("claim_id") == "claim-1"]
        assert len(mail_sends) == 6
        assert all(content == "unacked-forever" for content, _meta in mail_sends)

    @pytest.mark.asyncio
    async def test_old_engine_without_wait_stops_the_waiter(self) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        fake.wait_raises = httpx.HTTPStatusError(
            "404", request=httpx.Request("POST", "http://x/v1/tuples/wait"),
            response=httpx.Response(404, request=httpx.Request("POST", "http://x/v1/tuples/wait")),
        )
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=True,
        )
        await waiter.tick()
        assert waiter._stopped is True  # noqa: SLF001 — white-box assertion

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
        post = _row("p1", "board/release-notes", "v7.50 shipped", dims={"from": "author-a", "kind": "note"})
        fake.wait_results = [[WaitResult(subspace="board/release-notes", tuples=[post])]]
        sender = _FakeSender()
        persisted = []
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), subs, channel_live=True, sender=sender,
            persist=lambda: persisted.append(True),
        )
        await waiter.tick()
        assert sender.calls[0] == (
            "v7.50 shipped",
            {"subspace": "board/release-notes", "tuple_id": "p1", "from": "author-a", "kind": "note"},
        )
        assert subs.entries()[-1]["cursor"] == {"created_at": post.created_at, "id": "p1"}
        assert persisted == [True]
        # Board posts carry no claim; nothing was claimed.
        assert fake.in_calls == []


class TestChannelWaiterRealEngine:
    """The one property a fake store cannot prove: the engine's OWN
    same-claimant retake."""

    def test_restarted_waiter_retakes_its_own_claim_with_no_attempt_spent(self, t2_service_env) -> None:
        from nexus.mcp_infra import t2_ctx

        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        with t2_ctx() as db:
            db.tuples.out(addr, {"to": session_id}, {"from": "sender"}, "payload", nonce=str(uuid.uuid4()))

        subs_a = _subs(session_id)
        waiter_a = channel.ChannelWaiter(session_id, t2_ctx, subs_a, channel_live=True, sender=_FakeSender())
        asyncio.run(waiter_a._maybe_claim_mail())  # noqa: SLF001 — drive the claim step directly
        assert waiter_a._outstanding is not None  # noqa: SLF001
        first_claim_id = waiter_a._outstanding.claim_id  # noqa: SLF001

        # A "restarted server for the same session": a FRESH ChannelWaiter,
        # same session id (hence the SAME stable claimant), same store.
        subs_b = _subs(session_id)
        waiter_b = channel.ChannelWaiter(session_id, t2_ctx, subs_b, channel_live=True, sender=_FakeSender())
        assert waiter_a.claimant == waiter_b.claimant
        asyncio.run(waiter_b._maybe_claim_mail())  # noqa: SLF001
        assert waiter_b._outstanding is not None  # noqa: SLF001
        assert waiter_b._outstanding.claim_id == first_claim_id  # noqa: SLF001 — the SAME claim, retaken

        with t2_ctx() as db:
            stats = db.tuples.subspace_stats(addr)
        assert stats.claimed == 1
        assert stats.dead == 0


class TestCreditHookWiring:
    def test_tuple_ack_calls_note_credit(self, t2_service_env, monkeypatch) -> None:
        from nexus.mcp.core import tuple_ack, tuple_in, tuple_out

        addr = f"mailbox/{uuid.uuid4().hex[:10]}"
        session_id = str(uuid.uuid4())
        monkeypatch.setenv("NX_T1_SESSION_ID", session_id)
        tuple_out(addr, {"to": addr}, {"from": "sender"}, "payload", nonce=str(uuid.uuid4()))
        claimant = f"claimant-{uuid.uuid4().hex[:8]}"
        claim = tuple_in(addr, {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            channel, "note_credit", lambda sid, cid: calls.append((sid, cid)),
        )
        tuple_ack(claim["claim_id"], claimant)
        assert calls == [(session_id, claim["claim_id"])]

    def test_tuple_nack_calls_note_credit(self, t2_service_env, monkeypatch) -> None:
        from nexus.mcp.core import tuple_in, tuple_nack, tuple_out

        addr = f"mailbox/{uuid.uuid4().hex[:10]}"
        session_id = str(uuid.uuid4())
        monkeypatch.setenv("NX_T1_SESSION_ID", session_id)
        tuple_out(addr, {"to": addr}, {"from": "sender"}, "payload", nonce=str(uuid.uuid4()))
        claimant = f"claimant-{uuid.uuid4().hex[:8]}"
        claim = tuple_in(addr, {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            channel, "note_credit", lambda sid, cid: calls.append((sid, cid)),
        )
        tuple_nack(claim["claim_id"], claimant)
        assert calls == [(session_id, claim["claim_id"])]

    def test_tuple_channel_probe_calls_note_probe_ack_and_returns_ok(self, monkeypatch) -> None:
        from nexus.mcp.core import tuple_channel_probe

        session_id = str(uuid.uuid4())
        monkeypatch.setenv("NX_T1_SESSION_ID", session_id)
        calls: list[str] = []
        monkeypatch.setattr(channel, "note_probe_ack", lambda sid: calls.append(sid))
        result = tuple_channel_probe()
        assert result == "ok"
        assert calls == [session_id]

    def test_note_probe_ack_flips_a_registered_waiter(self) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=False,
        )
        channel.register_active_waiter(waiter)
        try:
            channel.note_probe_ack(session_id)
            assert waiter.status()["proof"] == "probe"
            assert waiter.channel_live.is_set()
        finally:
            channel.unregister_active_waiter(session_id)

    def test_note_credit_on_an_unregistered_session_is_a_silent_no_op(self) -> None:
        channel.note_credit(str(uuid.uuid4()), "some-claim-id")  # must not raise


class TestChannelStatusPublish:
    """RDR-211 Phase 1 Step 3 (bead nexus-rplay.13): the on-disk status
    record the `nx doctor` row reads cross-process
    (`nexus.health._check_tuple_channel_delivery`)."""

    def test_no_state_dir_is_a_silent_no_op(self) -> None:
        """The default (`state_dir=None`, every other test in this file)
        must never raise just because nothing was ever wired to publish."""
        session_id = str(uuid.uuid4())
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(_FakeTupleStore()), _subs(session_id), channel_live=True,
        )
        waiter._publish_status()  # noqa: SLF001 — must not raise

    @pytest.mark.asyncio
    async def test_a_tick_publishes_the_status_record(self, tmp_path) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=True,
            state_dir=tmp_path,
        )
        assert channel.read_channel_status(tmp_path, session_id) is None

        await waiter.tick()

        recorded = channel.read_channel_status(tmp_path, session_id)
        assert recorded == waiter.status()
        assert recorded["proof"] == "argv"
        assert recorded["last_wake"] is not None

    @pytest.mark.asyncio
    async def test_probe_ack_publishes_the_updated_proof(self, tmp_path) -> None:
        session_id = str(uuid.uuid4())
        fake = _FakeTupleStore()
        sender = _FakeSender()
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=False, sender=sender,
            state_dir=tmp_path,
        )
        run_task = asyncio.create_task(waiter.run())
        await asyncio.sleep(0.05)
        assert channel.read_channel_status(tmp_path, session_id)["proof"] == "none"
        waiter.note_probe_ack()
        await asyncio.sleep(0.05)
        assert channel.read_channel_status(tmp_path, session_id)["proof"] == "probe"
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        # run()'s finally publishes alive=False on the way out.
        assert channel.read_channel_status(tmp_path, session_id)["alive"] is False

    @pytest.mark.asyncio
    async def test_release_publishes_the_incremented_count(self, tmp_path) -> None:
        session_id = str(uuid.uuid4())
        addr = f"mailbox/{session_id}"
        fake = _FakeTupleStore()
        row = _row("t1", addr, "unacked-forever")
        fake.rd_results[addr] = [row]
        fake.in_results[addr] = (row, "claim-1")
        waiter = channel.ChannelWaiter(
            session_id, _fake_store_factory(fake), _subs(session_id), channel_live=True, sender=_FakeSender(),
            state_dir=tmp_path, renew_interval_s=0.0, max_resends=0,
        )
        await waiter.tick()  # claims row1 into `_outstanding`
        assert channel.read_channel_status(tmp_path, session_id)["unacked"] == 1
        fake.in_results[addr] = None  # nothing else to claim on the next tick
        await waiter.tick()  # next_renew_at already due (renew_interval_s=0.0) -> release
        recorded = channel.read_channel_status(tmp_path, session_id)
        assert recorded["released"] == 1
        assert recorded["unacked"] == 0

    def test_write_channel_status_rejects_a_path_hostile_session_id(self, tmp_path) -> None:
        channel.write_channel_status(tmp_path, "../escape", {"proof": "none"})
        assert list(tmp_path.rglob("*")) == []

    def test_read_channel_status_missing_file_is_none(self, tmp_path) -> None:
        assert channel.read_channel_status(tmp_path, "no-such-session") is None

    def test_read_channel_status_malformed_json_is_none(self, tmp_path) -> None:
        path = channel._channel_status_path(tmp_path, "sess-1")  # noqa: SLF001
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        assert channel.read_channel_status(tmp_path, "sess-1") is None
