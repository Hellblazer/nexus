# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx tuple wait``: the channel-free push path (RDR-233, bead nexus-7hzb2).

A Claude Code session launched without ``--channels`` (the Claude Desktop Code
tab) never hears the MCP channel waiter. It can still be woken: a Bash command
run with ``run_in_background`` re-invokes an idle session when it exits. This
module is the loop that command runs. It parks the engine ``wait`` over the
session's own ``mailbox/<session id>`` and its subscribed board topics, and
returns as soon as a row that is not the session's own arrives, rendering the
same fixed-template text the channel would have sent (never a tuple body).

No client cursor. Every spec carries a per-subscriber announce
(:data:`SUBSCRIBER_PREFIX` + session id, ``max=1``), so the ENGINE decides what
is due: each row is handed to this subscriber once, only while claimable (mail
the drain hook already consumed never wakes anyone), and the engine re-scans
from the oldest row, so a late-committed ``out`` is never skipped. The
subscriber name differs from the channel waiter's (the bare session id), so the
two never spend each other's stamps. Board specs carry the topic's
subscribe-time watermark as ``since``, as :class:`nexus.mcp.channel.ChannelWaiter`
does, so subscribing never replays a topic's backlog. A fresh ``waiter`` token
per run makes a newer ``nx tuple wait`` for the same session supersede an older
one's parked call, so arming twice leaves one waiter.

An own row (the engine stamped this session as its writer, or, on an engine that
renders no stamp, its ``from`` names this session) is stamped and dropped: it
never ends the wait.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from nexus.db.t2.records import Announce, TupleRow, WaitResult, WaitSpec

#: The per-subscriber stamp key prefix. The channel waiter's board subscriber is
#: the bare session id; this one must differ, or each would spend the other's.
SUBSCRIBER_PREFIX: str = "wait:"

#: Seconds one engine park lasts before the loop re-checks liveness and
#: re-reads the board subscriptions. The hand test that proved the wake used 25.
DEFAULT_PARK_S: int = 25

#: Rows one spec asks for per park. Every returned row is stamped, so the rest
#: of a larger burst is announced to the next run, after the re-arm.
WAIT_ROWS: int = 20

#: Each row is announced to this subscriber once.
_MAX_ANNOUNCES: int = 1

#: Re-announce interval sent with ``max=1``; it never applies, but the engine
#: requires a value.
_INTERVAL_S: int = 3600

#: Consecutive failed parks (each already retried by the store itself) before
#: the waiter gives up and exits non-zero. An exit wakes the session, so a
#: gateway blip must not end the wait; a store that stays down must.
MAX_CONSECUTIVE_ERRORS: int = 6

#: Seconds slept after a failed park, doubling per consecutive failure, capped.
ERROR_BACKOFF_S: float = 5.0
ERROR_BACKOFF_CAP_S: float = 60.0

#: Why :func:`run_wait` returned.
DELIVERED = "delivered"
SUPERSEDED = "superseded"
SESSION_CHANGED = "session_changed"
CLAUDE_GONE = "claude_gone"
PARKS_EXHAUSTED = "parks_exhausted"


class EngineTooOldError(RuntimeError):
    """The engine did not honour the per-subscriber announce (an engine older
    than bead nexus-q82tk), so it would stamp rows for nobody."""


@dataclass
class WaitOutcome:
    reason: str
    lines: list[str] = field(default_factory=list)
    #: Own rows the engine handed back and this waiter dropped.
    own_dropped: int = 0


def subscriber_for(session_id: str) -> str:
    return f"{SUBSCRIBER_PREFIX}{session_id}"


def build_specs(
    session_id: str, board_entries: list[dict[str, Any]], *, waiter_token: str,
) -> list[WaitSpec]:
    """The session mailbox first, then each board topic with its watermark.
    *board_entries* is :meth:`SubscriptionSet.entries`'s shape; any mailbox
    entry in it is ignored (the session's own mailbox is always first)."""
    from nexus.mcp.channel import DEFAULT_BOARD_START_SKEW_S, _parse_created_at  # noqa: PLC0415 — deferred: loads the MCP package

    subscriber = subscriber_for(session_id)

    def announce() -> Announce:
        return Announce(
            interval_s=_INTERVAL_S, max=_MAX_ANNOUNCES, subscriber=subscriber, waiter=waiter_token,
        )

    specs = [WaitSpec(subspace=f"mailbox/{session_id}", n=WAIT_ROWS, announce=announce())]
    for entry in board_entries:
        subspace = entry.get("subspace") or ""
        if not subspace.startswith("board/"):
            continue
        since: tuple[str, str] | None = None
        start = _parse_created_at(entry.get("since"))
        if start is not None:
            # Same margin and wire shape as ChannelWaiter._build_specs: the
            # engine's per-subscriber since is a created_at bound alone.
            since = ((start - timedelta(seconds=DEFAULT_BOARD_START_SKEW_S)).isoformat(), "")
        specs.append(WaitSpec(subspace=subspace, n=WAIT_ROWS, since=since, announce=announce()))
    return specs


def is_own_row(row: TupleRow, session_id: str) -> bool:
    """True when this session wrote *row*. The engine's stamp decides when the
    engine renders one; only an engine that renders none falls back to the
    row's claimed ``from``. A row stamped with this session but sent from an
    agent id is a subagent's (subagents share the parent's token, JDR-001):
    it is mail TO the session, not its own, and wakes it, as the channel and
    the drain hook deliver it."""
    from nexus.tuple_sender import _is_agent_id, _same_session  # noqa: PLC0415 — deferred, sibling-module helpers

    claimed = row.dims.get("from")
    if row.sender_session_known:
        if isinstance(claimed, str) and _is_agent_id(claimed):
            return False
        return bool(row.sender_session) and _same_session(session_id, row.sender_session)
    return bool(claimed) and _same_session(session_id, claimed)


def render_row(subspace: str, row: TupleRow) -> str:
    """The channel's own text for *row*, sender label included."""
    from nexus.mcp import channel  # noqa: PLC0415 — deferred: loads the MCP package
    from nexus.tuple_sender import canonical_session  # noqa: PLC0415 — deferred, sibling-module helper

    case = channel._row_case(row)  # noqa: SLF001 — one rendering for both push paths
    session = canonical_session(row.sender_session)
    if subspace.startswith("mailbox/"):
        return channel._mailbox_notification_content(  # noqa: SLF001 — see above
            subspace, row.id, subspace.removeprefix("mailbox/"), case=case, session=session,
        )
    return channel._board_notification_content(subspace, row.id, case=case, session=session)  # noqa: SLF001 — see above


def _process(
    results: list[WaitResult], session_id: str, outcome: WaitOutcome,
) -> bool:
    """Fold one park's results into *outcome*; True when the waiter was
    superseded. Raises :class:`EngineTooOldError` on a result whose subscriber
    echo is not ours."""
    subscriber = subscriber_for(session_id)
    superseded = False
    for result in results:
        if result.superseded:
            superseded = True
            continue
        if result.tuples and result.subscriber != subscriber:
            raise EngineTooOldError(
                f"the engine did not honour the per-subscriber announce on {result.subspace} "
                f"(echoed {result.subscriber!r}); nx tuple wait needs engine-service-v0.1.129 "
                "or newer"
            )
        for row in result.tuples:
            if is_own_row(row, session_id):
                outcome.own_dropped += 1
                continue
            outcome.lines.append(render_row(result.subspace, row))
    return superseded


def run_wait(
    store: Any,
    session_id: str,
    *,
    board_entries: Callable[[], list[dict[str, Any]]],
    alive: Callable[[], bool],
    current_session: Callable[[], str | None],
    waiter_token: str,
    park_s: int = DEFAULT_PARK_S,
    max_parks: int | None = None,
    sleep: Callable[[float], None] | None = None,
) -> WaitOutcome:
    """Park until a row that is not this session's own arrives, then return it.

    Between parks: stop when *alive* says the ``claude`` process is gone, or
    when *current_session* names a different session (a ``/clear``). The board
    topics are re-read through *board_entries* every park, so a topic subscribed
    while parked joins at the next one. *max_parks* bounds the loop for tests;
    ``None`` waits until one of the stop conditions above.

    A failed park (an HTTP or transport error the store already retried) is
    slept off and retried; :data:`MAX_CONSECUTIVE_ERRORS` in a row re-raise the
    last one. A full park ceiling (``ParkCapExceededError``, 429) is retried the
    same way. Any other engine refusal (a ``TupleError``) and
    :class:`EngineTooOldError` are never retried: the next park would be
    refused the same way.
    """
    import time  # noqa: PLC0415 — stdlib, branch-local

    from nexus.db.t2.http_tuple_store import ParkCapExceededError, TupleError  # noqa: PLC0415 — deferred: CLI startup cost

    nap = sleep or time.sleep
    errors = 0
    outcome = WaitOutcome(reason=PARKS_EXHAUSTED)
    parks = 0
    while max_parks is None or parks < max_parks:
        if not alive():
            outcome.reason = CLAUDE_GONE
            return outcome
        current = current_session()
        if current and current != session_id:
            outcome.reason = SESSION_CHANGED
            return outcome
        specs = build_specs(session_id, board_entries(), waiter_token=waiter_token)
        parks += 1
        try:
            results = store.wait(specs, park_s)
        except TupleError as e:
            # 429: the engine's park ceiling is full; it clears as other
            # waits return, so it is slept off like a transport failure.
            if not isinstance(e, ParkCapExceededError):
                raise
            errors += 1
            if errors >= MAX_CONSECUTIVE_ERRORS:
                raise
            nap(min(ERROR_BACKOFF_S * 2 ** (errors - 1), ERROR_BACKOFF_CAP_S))
            continue
        except Exception:  # noqa: BLE001 — transient store/transport failure: back off, park again
            errors += 1
            if errors >= MAX_CONSECUTIVE_ERRORS:
                raise
            nap(min(ERROR_BACKOFF_S * 2 ** (errors - 1), ERROR_BACKOFF_CAP_S))
            continue
        errors = 0
        superseded = _process(results, session_id, outcome)
        if outcome.lines:
            outcome.reason = DELIVERED
            return outcome
        if superseded:
            outcome.reason = SUPERSEDED
            return outcome
    return outcome
