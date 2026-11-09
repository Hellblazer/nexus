# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1d6pz.11.2: every board notice carries a read that returns the posts
it announces.

Before this, the single-post notice said only "Read it with tuple_rd on that
subspace", and the batch notice with no known anchor printed
``tuple_rd(subspace, n=count)`` and called that the newest *count* rows.
``tuple_rd`` with no ``since`` returns the OLDEST rows (baton, 2026-11-08: a
read of ``board/ci/nexus-develop--done`` returned the 12:00Z rows, not the
post just announced). The engine has only an exclusive ``(created_at, id)``
cursor and no read by id, so ``tuple_rd`` gains an inclusive start,
``from_created_at`` + ``from_id``, and an inclusive end, ``through_created_at``
+ ``through_id``. A single-post notice prints a one-row read from its post; a
batch prints a read from its first post through its last, because the
announced posts need not be consecutive in the subspace.

This file holds the tenant-free checks: the notice text, ``nx tuple wait``'s
text, and the refusals that happen before any storage is opened. The engine
checks ride existing engine tests at no extra tenant cost (the push train's
tenant ceiling): the real waiter with two concurrent writers runs every
notice's printed read (``tests/test_mcp_channel.py``,
``test_stop_rule_board_path_two_concurrent_writers_no_post_ever_lost``), and
the read cursor journey covers from, through, a missing row and the n cap
(``tests/test_mcp_tuple_tools.py``,
``test_f11_a_real_cursor_from_a_returned_row_resumes_after_it``).
"""
from __future__ import annotations

import pytest

from nexus.db.t2.records import TupleRow
from nexus.mcp import channel
from nexus.mcp.core import tuple_rd
from nexus.tuple_wait import render_row


class TestNoticeText:
    def test_single_notice_prints_a_one_row_read_from_its_post(self) -> None:
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

    def test_a_row_without_created_at_gets_the_plain_pointer_never_an_oldest_rows_read(self) -> None:
        single = channel._board_notification_content("board/ci/x", "p1")  # noqa: SLF001
        batch = channel._board_batch_notification_content("board/ci/x", 2, "p1", "p2", None, None)  # noqa: SLF001
        for text in (single, batch):
            assert "tuple_rd on that subspace" in text
            assert 'tuple_rd("board/ci/x", n=' not in text

    def test_nx_tuple_wait_prints_the_same_read(self) -> None:
        row = TupleRow(
            id="b" * 64, subspace="board/ci/x", template="board/ci/<topic>", keys={}, dims={},
            body="x", claim_state=None, claimant=None, lease_until=None, attempts=0,
            consumed_at=None, consumed_by=None, expires_at=None, created_at="2026-11-08T12:00:00Z",
        )
        text = render_row("board/ci/x", row)
        assert f'tuple_rd("board/ci/x", n=1, from_created_at="2026-11-08T12:00:00Z", from_id="{"b" * 64}")' in text


class TestRefusedBeforeStorage:
    """Each refusal names its field; none of these reaches the engine."""

    def test_from_and_since_together(self) -> None:
        out = tuple_rd(
            "board/x", n=1, since_created_at="2026-11-08T00:00:00Z", since_id="a" * 64,
            from_created_at="2026-11-08T00:00:00Z", from_id="a" * 64,
        )
        assert len(out) == 1 and "not both" in out[0].get("error", ""), out

    def test_half_a_from_pair(self) -> None:
        out = tuple_rd("board/x", n=1, from_created_at="2026-11-08T00:00:00Z")
        assert len(out) == 1 and "come as a pair" in out[0].get("error", ""), out

    def test_half_a_through_pair(self) -> None:
        out = tuple_rd(
            "board/x", n=1, from_created_at="2026-11-08T00:00:00Z", from_id="a" * 64,
            through_id="a" * 64,
        )
        assert len(out) == 1 and "come as a pair" in out[0].get("error", ""), out

    def test_through_without_from(self) -> None:
        out = tuple_rd("board/x", n=1, through_created_at="2026-11-08T00:00:00Z", through_id="a" * 64)
        assert len(out) == 1 and "bound a from_created_at" in out[0].get("error", ""), out

    def test_from_with_a_park(self) -> None:
        out = tuple_rd("board/x", n=1, from_created_at="2026-11-08T00:00:00Z", from_id="a" * 64, timeout_s=5)
        assert len(out) == 1 and "does not park" in out[0].get("error", ""), out

    @pytest.mark.parametrize(("kwargs", "needle"), [
        ({"n": 0}, "must be at least 1"),
        ({"from_created_at": "2026-11-08T00:00:00"}, "from_created_at"),
        ({"from_id": "abc"}, "from_id"),
        ({"through_created_at": "2026-11-07T00:00:00Z", "through_id": "a" * 64}, "ordered before"),
        ({"through_created_at": "yesterday", "through_id": "a" * 64}, "through_created_at"),
    ])
    def test_a_malformed_read_names_its_field(self, kwargs, needle) -> None:
        args = {"n": 1, "from_created_at": "2026-11-08T00:00:00Z", "from_id": "a" * 64, **kwargs}
        out = tuple_rd("board/x", **args)
        assert len(out) == 1 and needle in out[0].get("error", ""), out
