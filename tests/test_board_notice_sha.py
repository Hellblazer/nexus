# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1d6pz.11.3: a CI board notice carries the sha its row is for in its meta, so a session
can tell which push a wake is for without a read.

Option (a) of the bead, built on what the board carries today: the conexus adapter
(nexus-1d6pz.11.1, live 2026-11-09) writes the sha in the row body, and its ``sha`` dim stays
off until engine v0.1.184 carries the board-ci template. The notice takes the dim when present,
else the body's ``sha``, and only when it is exactly 40 hex digits: the body is unattested, so
nothing else of it reaches the notice. Tenant-free: the waiter's delivery is driven directly.
"""
from __future__ import annotations

import dataclasses
import json
import uuid

import pytest

from nexus.db.t2.records import TupleRow
from nexus.mcp import channel
from tests.hooks.test_stop_wake_guard import _load as _load_guard
from tests.test_mcp_channel import _FakeSender, _FakeTupleStore, _fake_store_factory, _subs

DONE = "board/ci/nexus-develop--done"
SHA_A = "a0dc17ec284ffce6d4ff5264a626b7ef8e1a293a"
SHA_B = "261600dab59f41455dd633b7d1e2a1ff7a4880de"


def _row(tid: str, body: object, dims: dict[str, str] | None = None, at: str = "2026-11-09T09:00:00.000001Z") -> TupleRow:
    return TupleRow(
        id=tid, subspace=DONE, template="board/ci/<topic>", keys={"topic": "nexus-develop--done"},
        dims={"from": "github", "kind": "sha", **(dims or {})},
        body=body if isinstance(body, str) else json.dumps(body), claim_state=None, claimant=None,
        lease_until=None, attempts=0, consumed_at=None, consumed_by=None, expires_at=None, created_at=at,
    )


def _done(sha: str) -> dict:
    return {"state": "concluded", "sha": sha, "runs": [[1, 1, "CI", "success"]], "n": 1, "url": "u"}


async def _deliver(subspace: str, rows: list[TupleRow]) -> list[tuple[str, dict[str, str]]]:
    session_id = str(uuid.uuid4())
    sender = _FakeSender()
    waiter = channel.ChannelWaiter(session_id, _fake_store_factory(_FakeTupleStore()), _subs(session_id), sender=sender)
    await waiter._deliver_board_rows(subspace, rows, None)  # noqa: SLF001 -- the delivery under test
    return sender.calls


@pytest.mark.asyncio
async def test_a_single_ci_done_notice_names_its_sha() -> None:
    [(content, meta)] = await _deliver(DONE, [_row("t1", _done(SHA_A))])
    assert meta["sha"] == SHA_A
    # meta only: the text stays server-built identifiers (2026-09-17 reference-only decision)
    assert SHA_A not in content


@pytest.mark.asyncio
async def test_two_pushes_two_notices_each_names_its_own_sha() -> None:
    """The bead's acceptance: two sessions push different shas; each notice names its sha, so
    each session ignores the other's without a tuple_rd."""
    first = await _deliver(DONE, [_row("t1", _done(SHA_A))])
    second = await _deliver(DONE, [_row("t2", _done(SHA_B), at="2026-11-09T09:01:00.000001Z")])
    assert [m["sha"] for _c, m in first + second] == [SHA_A, SHA_B]


@pytest.mark.asyncio
async def test_a_batch_names_each_post_s_sha_in_tuple_id_order() -> None:
    rows = [_row("t1", _done(SHA_A)), _row("t2", "not json", at="2026-11-09T09:00:01.000001Z"),
            _row("t3", _done(SHA_B), at="2026-11-09T09:00:02.000001Z")]
    [(content, meta)] = await _deliver(DONE, rows)
    assert meta["tuple_ids"] == "t1,t2,t3"
    assert meta["shas"] == f"{SHA_A},-,{SHA_B}"  # "-": no sha this notice can vouch for
    assert SHA_A[:12] not in content and SHA_B[:12] not in content


@pytest.mark.asyncio
async def test_the_dim_wins_over_the_body_once_the_adapter_writes_it() -> None:
    [(_c, meta)] = await _deliver(DONE, [_row("t1", _done(SHA_B), dims={"sha": SHA_A})])
    assert meta["sha"] == SHA_A


@pytest.mark.asyncio
@pytest.mark.parametrize("sha", [
    SHA_A.upper(),  # the adapter writes lowercase; anything else is not vouched for
    SHA_A[:12],  # a prefix
    SHA_A + "0",  # too long
    "g" * 40,  # not hex
    f"{SHA_A}\n injected text",  # the body is unattested: nothing but 40 hex digits passes
    1234,  # not a string
])
async def test_a_body_sha_that_is_not_exactly_40_lowercase_hex_is_left_out(sha) -> None:
    [(content, meta)] = await _deliver(DONE, [_row("t1", _done(sha))])  # type: ignore[arg-type]
    assert "sha" not in meta
    assert "injected" not in content


@pytest.mark.asyncio
async def test_a_bad_dim_falls_back_to_the_body() -> None:
    [(_c, meta)] = await _deliver(DONE, [_row("t1", _done(SHA_B), dims={"sha": "nope"})])
    assert meta["sha"] == SHA_B


@pytest.mark.asyncio
@pytest.mark.parametrize("subspace", ["board/ci/nexus-develop", "board/releases", "board/ci/nexus-develop--done/x"])
async def test_only_a_ci_done_topic_gets_a_sha(subspace: str) -> None:
    [(content, meta)] = await _deliver(subspace, [_row("t1", _done(SHA_A))])
    assert "sha" not in meta and SHA_A not in content


@pytest.mark.asyncio
async def test_a_nested_topic_is_not_a_ci_done_topic() -> None:
    [(_c, meta)] = await _deliver("board/ci/a/b--done", [_row("t1", _done(SHA_A))])
    assert "sha" not in meta


@pytest.mark.asyncio
async def test_an_oversized_or_non_string_body_gives_no_sha() -> None:
    big = json.dumps({**_done(SHA_A), "pad": "x" * 5000})
    [(_c, meta)] = await _deliver(DONE, [_row("t1", big)])
    assert "sha" not in meta
    assert channel._ci_done_sha(DONE, dataclasses.replace(_row("t2", "{}"), body=5)) is None  # noqa: SLF001


def test_the_channel_and_the_stop_hook_guard_agree_on_the_ci_done_topic() -> None:
    """The guard (nexus-1d6pz.11.5) allows a closing CI wait on a board/ci/*--done subscription;
    both patterns must name the same topics or one drifts silently."""
    guard = _load_guard()
    for topic in ["board/ci/nexus-develop--done", "board/ci/a--done", "board/ci/a/b--done", "board/ci/--done",
                  "board/ci/a--done/", "board/ci/a--donex", "board/releases"]:
        assert bool(channel._CI_DONE_TOPIC.fullmatch(topic)) == bool(guard._CI_DONE_TOPIC.fullmatch(topic)), topic  # noqa: SLF001
