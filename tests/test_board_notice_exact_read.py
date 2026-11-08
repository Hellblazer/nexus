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

from nexus.mcp import channel
from nexus.mcp.core import tuple_out, tuple_rd
from nexus.mcp.subscriptions import SubscriptionSet
from nexus.mcp_infra import t2_ctx

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
        assert len(out) == 1 and "error" in out[0], out

    def test_half_a_from_pair_is_refused(self, t2_service_env) -> None:
        out = tuple_rd("board/x", n=1, from_created_at="2026-11-08T00:00:00Z")
        assert len(out) == 1 and "error" in out[0], out


class TestNoticeText:
    def test_single_notice_prints_the_inclusive_read(self) -> None:
        content = channel._board_notification_content(  # noqa: SLF001
            "board/ci/x", "p1", created_at="2026-11-08T12:00:00.000001Z",
        )
        assert (
            'tuple_rd("board/ci/x", n=1, from_created_at="2026-11-08T12:00:00.000001Z", '
            'from_id="p1")'
        ) in content

    def test_batch_notice_prints_the_inclusive_read_from_its_first_row(self) -> None:
        content = channel._board_batch_notification_content(  # noqa: SLF001
            "board/ci/x", 3, "p1", "p3", "2026-11-08T12:00:00.000001Z",
        )
        assert (
            'tuple_rd("board/ci/x", n=3, from_created_at="2026-11-08T12:00:00.000001Z", '
            'from_id="p1")'
        ) in content
        assert "newest" not in content
