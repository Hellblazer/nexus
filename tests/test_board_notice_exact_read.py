# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1d6pz.11.2: every board notice carries a read that returns exactly
the rows it announces.

Before this, the single-post notice said only "Read it with tuple_rd on that
subspace", and the batch notice with no known anchor printed
``tuple_rd(subspace, n=count)`` and called that the newest *count* rows.
``tuple_rd`` with no ``since`` returns the OLDEST rows (baton, 2026-11-08: a
read of ``board/ci/nexus-develop--done`` returned the 12:00Z rows, not the
post just announced). The engine has only an exclusive ``(created_at, id)``
cursor and no read by id, so ``tuple_rd`` gains an inclusive start,
``from_created_at`` + ``from_id``: the announced row's own fields. Every
notice prints that read with ``n`` equal to the rows it announces.

The waiter test runs against the real engine with older rows already in the
subspace, so a read that starts from the beginning returns the wrong rows and
fails it.
"""
from __future__ import annotations

import ast
import asyncio
import re
import uuid
from datetime import UTC, datetime

import pytest

from nexus.db.t2.records import TupleRow
from nexus.mcp import channel
from nexus.mcp.core import tuple_out, tuple_rd
from nexus.mcp.subscriptions import SubscriptionSet
from nexus.mcp_infra import t2_ctx
from nexus.tuple_wait import render_row

pytestmark = pytest.mark.t2_tenant


def _post(subspace: str, topic: str, body: str) -> str:
    tid = tuple_out(subspace, {"topic": topic}, {"from": "test-11-2"}, body, nonce=uuid.uuid4().hex)
    assert isinstance(tid, str) and re.fullmatch(r"[0-9a-f]{64}", tid), tid
    return tid


_READ = re.compile(r"(tuple_rd\(.*?\))\.")


def _run_printed_read(content: str) -> list[str]:
    """Parse the one ``tuple_rd(...)`` call a notice prints and run it."""
    m = _READ.search(content)
    assert m, f"notice carries no tuple_rd read: {content!r}"
    call = ast.parse(m.group(1), mode="eval").body
    assert isinstance(call, ast.Call)
    args = [ast.literal_eval(a) for a in call.args]
    kwargs = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
    rows = tuple_rd(*args, **kwargs)
    assert isinstance(rows, list) and all("error" not in r for r in rows), rows
    return [r["id"] for r in rows]


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, content: str, meta: dict[str, str]) -> bool:
        self.calls.append((content, meta))
        return True


class TestBoardNoticeReadIsExact:
    def test_single_then_single_then_batch_each_read_returns_the_announced_rows(
        self, t2_service_env, tmp_path,
    ) -> None:
        topic = f"exact-{uuid.uuid4().hex[:8]}"
        subspace = f"board/{topic}"
        old = [_post(subspace, topic, f"old {i}") for i in range(3)]

        session_id = str(uuid.uuid4())
        subs = SubscriptionSet(session_id=session_id)
        try:
            subs.subscribe(subspace, templates=[], store_factory=t2_ctx, state_dir=tmp_path)
            # start after the old rows (no skew widening), so the waiter announces only what follows
            subs._board[subspace] = datetime.now(UTC).isoformat()  # noqa: SLF001
            sender = _Sender()
            waiter = channel.ChannelWaiter(
                session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1, board_start_skew_s=0.0,
            )

            def board_notices() -> list[tuple[str, dict[str, str]]]:
                return [(c, m) for c, m in sender.calls if m.get("subspace") == subspace]

            first = _post(subspace, topic, "first")
            asyncio.run(waiter.tick())
            assert len(board_notices()) == 1, sender.calls
            content, _meta = board_notices()[-1]
            assert "newest" not in content
            assert _run_printed_read(content) == [first]

            second = _post(subspace, topic, "second")
            asyncio.run(waiter.tick())
            assert len(board_notices()) == 2, sender.calls
            assert _run_printed_read(board_notices()[-1][0]) == [second]

            batch = [_post(subspace, topic, f"batch {i}") for i in range(3)]
            asyncio.run(waiter.tick())
            assert len(board_notices()) == 3, sender.calls
            content, meta = board_notices()[-1]
            assert meta.get("count") == "3", meta
            assert "newest" not in content
            assert _run_printed_read(content) == batch

            # the control: a read from the start returns the old rows, not the announced one
            assert [r["id"] for r in tuple_rd(subspace, n=1)] == [old[0]]
        finally:
            subs.shutdown()


class TestBatchReads:
    def test_the_first_notice_after_subscribe_is_a_batch_and_its_read_is_exact(
        self, t2_service_env, tmp_path,
    ) -> None:
        """The G3 evidence: a batch with no earlier delivered post to anchor on."""
        topic = f"first-{uuid.uuid4().hex[:8]}"
        subspace = f"board/{topic}"
        old = [_post(subspace, topic, f"old {i}") for i in range(2)]
        session_id = str(uuid.uuid4())
        subs = SubscriptionSet(session_id=session_id)
        try:
            subs.subscribe(subspace, templates=[], store_factory=t2_ctx, state_dir=tmp_path)
            subs._board[subspace] = datetime.now(UTC).isoformat()  # noqa: SLF001
            sender = _Sender()
            waiter = channel.ChannelWaiter(
                session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1, board_start_skew_s=0.0,
            )
            batch = [_post(subspace, topic, f"b {i}") for i in range(3)]
            asyncio.run(waiter.tick())
            notices = [(c, m) for c, m in sender.calls if m.get("subspace") == subspace]
            assert len(notices) == 1 and notices[0][1].get("count") == "3", sender.calls
            assert _run_printed_read(notices[0][0]) == batch
            assert [r["id"] for r in tuple_rd(subspace, n=1)] == [old[0]]
        finally:
            subs.shutdown()

    def test_a_batch_whose_posts_are_not_consecutive_still_returns_its_last_post(
        self, t2_service_env, tmp_path,
    ) -> None:
        """A row sorting between two announced posts (here B, which this
        delivery leaves out, as a late commit would): the read returns A, B and
        C, never [A, B] without C."""
        topic = f"gap-{uuid.uuid4().hex[:8]}"
        subspace = f"board/{topic}"
        ids = [_post(subspace, topic, f"r{i}") for i in range(3)]
        with t2_ctx() as db:
            rows = {r.id: r for r in db.tuples.rd(subspace, n=10)}
        session_id = str(uuid.uuid4())
        subs = SubscriptionSet(session_id=session_id)
        try:
            sender = _Sender()
            waiter = channel.ChannelWaiter(session_id, t2_ctx, subs, sender=sender, wait_timeout_s=1)
            asyncio.run(waiter._deliver_board_rows(subspace, [rows[ids[0]], rows[ids[2]]], None))  # noqa: SLF001
            (content, meta), = sender.calls
            assert meta["tuple_ids"] == f"{ids[0]},{ids[2]}"
            assert _run_printed_read(content) == ids
        finally:
            subs.shutdown()


class TestTupleRdInclusiveStart:
    def test_from_returns_the_named_row_first_then_the_rows_after_it(self, t2_service_env) -> None:
        topic = f"from-{uuid.uuid4().hex[:8]}"
        subspace = f"board/{topic}"
        ids = [_post(subspace, topic, f"r{i}") for i in range(5)]
        rows = tuple_rd(subspace, n=5)
        by_id = {r["id"]: r for r in rows}
        third = by_id[ids[2]]

        one = tuple_rd(subspace, n=1, from_created_at=third["created_at"], from_id=third["id"])
        assert [r["id"] for r in one] == [ids[2]]
        two = tuple_rd(subspace, n=2, from_created_at=third["created_at"], from_id=third["id"])
        assert [r["id"] for r in two] == ids[2:4]
        rest = tuple_rd(subspace, n=10, from_created_at=third["created_at"], from_id=third["id"])
        assert [r["id"] for r in rest] == ids[2:]

    def test_a_row_that_is_not_there_is_an_error_not_the_next_row(self, t2_service_env) -> None:
        topic = f"gone-{uuid.uuid4().hex[:8]}"
        subspace = f"board/{topic}"
        ids = [_post(subspace, topic, f"r{i}") for i in range(2)]
        first = tuple_rd(subspace, n=1)[0]
        assert first["id"] == ids[0]
        out = tuple_rd(subspace, n=1, from_created_at=first["created_at"], from_id="0" * 64)
        assert len(out) == 1 and "error" in out[0], out
        assert "not in" in out[0]["error"]

    def test_from_and_since_together_are_refused(self, t2_service_env) -> None:
        out = tuple_rd(
            "board/x", n=1, since_created_at="2026-11-08T00:00:00Z", since_id="a" * 64,
            from_created_at="2026-11-08T00:00:00Z", from_id="a" * 64,
        )
        assert len(out) == 1 and "not both" in out[0].get("error", ""), out

    def test_half_a_from_pair_is_refused(self, t2_service_env) -> None:
        out = tuple_rd("board/x", n=1, from_created_at="2026-11-08T00:00:00Z")
        assert len(out) == 1 and "come as a pair" in out[0].get("error", ""), out

    def test_through_without_from_is_refused(self, t2_service_env) -> None:
        out = tuple_rd("board/x", n=1, through_created_at="2026-11-08T00:00:00Z", through_id="a" * 64)
        assert len(out) == 1 and "bound a from_created_at" in out[0].get("error", ""), out

    @pytest.mark.parametrize(("kwargs", "needle"), [
        ({"n": 0}, "must be at least 1"),
        ({"from_created_at": "2026-11-08T00:00:00"}, "UTC offset"),
        ({"from_id": "abc"}, "is not a tuple id"),
    ])
    def test_a_malformed_from_read_is_refused_by_name(self, t2_service_env, kwargs, needle) -> None:
        args = {"n": 1, "from_created_at": "2026-11-08T00:00:00Z", "from_id": "a" * 64, **kwargs}
        out = tuple_rd(f"board/bad-{uuid.uuid4().hex[:6]}", **args)
        assert len(out) == 1 and needle in out[0].get("error", ""), out

    def test_through_stops_at_its_row_and_returns_rows_between(self, t2_service_env) -> None:
        """The superset property a batch notice relies on: a row sorting between
        the first and last announced posts (late commit, or skipped by the
        announce) is returned, and the last announced post is never lost."""
        topic = f"through-{uuid.uuid4().hex[:8]}"
        subspace = f"board/{topic}"
        ids = [_post(subspace, topic, f"r{i}") for i in range(4)]
        rows = {r["id"]: r for r in tuple_rd(subspace, n=10)}
        a, c = rows[ids[0]], rows[ids[2]]
        got = tuple_rd(
            subspace, n=300, from_created_at=a["created_at"], from_id=a["id"],
            through_created_at=c["created_at"], through_id=c["id"],
        )
        assert [r["id"] for r in got] == ids[:3]
        missing = tuple_rd(
            subspace, n=300, from_created_at=a["created_at"], from_id=a["id"],
            through_created_at=c["created_at"], through_id="0" * 63 + "1",
        )
        assert len(missing) == 1 and "not in" in missing[0].get("error", ""), missing
        short = tuple_rd(
            subspace, n=2, from_created_at=a["created_at"], from_id=a["id"],
            through_created_at=c["created_at"], through_id=c["id"],
        )
        assert len(short) == 1 and "more than n=2 rows" in short[0].get("error", ""), short


class TestNoticeText:
    def test_single_notice_prints_the_inclusive_read(self) -> None:
        content = channel._board_notification_content(  # noqa: SLF001
            "board/ci/x", "p1", created_at="2026-11-08T12:00:00.000001Z",
        )
        assert (
            'tuple_rd("board/ci/x", n=1, from_created_at="2026-11-08T12:00:00.000001Z", '
            'from_id="p1")'
        ) in content

    def test_batch_notice_prints_a_read_from_its_first_post_through_its_last(self) -> None:
        content = channel._board_batch_notification_content(  # noqa: SLF001
            "board/ci/x", 3, "p1", "p3", "2026-11-08T12:00:00.000001Z", "2026-11-08T12:00:02Z",
        )
        assert (
            'tuple_rd("board/ci/x", n=300, from_created_at="2026-11-08T12:00:00.000001Z", '
            'from_id="p1", through_created_at="2026-11-08T12:00:02Z", through_id="p3")'
        ) in content
        assert "newest" not in content

    def test_nx_tuple_wait_prints_the_same_exact_read(self) -> None:
        row = TupleRow(
            id="b" * 64, subspace="board/ci/x", template="board/ci/<topic>", keys={}, dims={},
            body="x", claim_state=None, claimant=None, lease_until=None, attempts=0,
            consumed_at=None, consumed_by=None, expires_at=None, created_at="2026-11-08T12:00:00Z",
        )
        text = render_row("board/ci/x", row)
        assert f'tuple_rd("board/ci/x", n=1, from_created_at="2026-11-08T12:00:00Z", from_id="{"b" * 64}")' in text
