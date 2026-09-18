# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-213 (amends RDR-211 Phase 1 Step 3, bead nexus-tk2cz): the
``claude/channel`` capability declaration and the nexus MCP server's
lifespan waiter -- Gap 5, "push delivery to the session", with the proof
gate and claim-at-delivery removed.

Two independent halves live here:

- :func:`run_stdio_with_channel` replaces ``FastMCP.run_stdio_async`` at
  the server's one call site (:func:`nexus.mcp.core.main`): it declares
  ``capabilities.experimental["claude/channel"] = {}`` at initialize
  (which is what makes Claude Code register a listener -- RDR-211 Phase 1
  Step 0, T2 ``nexus_rdr/211-spike-4-channel-2026-09-17``), and stashes the
  raw stdio write stream on this module (:data:`_write_stream`) so a sender
  never needs a handle on the ``ServerSession`` FastMCP builds internally.
- :class:`ChannelWaiter` is the lifespan's one background task: it parks
  ``HttpTupleStore.wait`` over the session's :class:`~nexus.mcp.
  subscriptions.SubscriptionSet`, and NEVER claims anything (T2
  ``nexus_rdr/213-decision-notify-then-claim-2026-09-17`` -- Sam's
  original intent). The mailbox path and the board path share ONE shape
  (T2 ``nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
  2026-09-17``): a per-mailbox CURSOR past the last referenced row, kept
  in the wait spec every tick, plus the last reference's own re-send
  budget. The waiter tracks POSITION, not row identity -- no read-back,
  no claim-state classification, no reconcile.

RDR-211 gated every mailbox claim on proof that the channel was live for
this session (a parent command-line read, or a probe notification the
session had to answer), because a claim held for a session that could
never hear the channel would strand the message for the lease. RDR-213
deletes the gate along with the claim itself: with no claim to strand, the
worst a lost notification costs is a wait until the next wake or the next
prompt, which the ``UserPromptSubmit`` drain hook (``conexus/hooks/
scripts/mailbox_drain.py``) renders regardless. The command-line-reading
gate function, its probe fallback and probe MCP tool are deleted outright,
not kept as fallbacks (Approach item 3). So are the waiter's claimant
identity, its lease/renew loop, and the persisted-outstanding-claim
adoption at restart -- all of it existed only to make a claim survivable,
and there is no claim left to protect.

Every notification's ``content`` is still a FIXED template built only from
server-controlled identifiers (subspace, tuple id) -- never the tuple's
own body, ``from``, ``kind``, or ``correlation_id`` (those travel in
``meta`` only, unchanged from RDR-211): the channel is a push-to-ATTEND
signal, not a delivery transport, and the session claims it itself with
``tuple_in`` once notified -- which returns the body WITH the claim, so
there is no separate read-then-claim step. With the plugin's hooks
loaded, the notification itself fires ``UserPromptSubmit`` and the drain
hook claims, acks and renders the body with THAT prompt before the
session's own turn, so the session claims for itself only when that
rendering did not already happen (MVV finding F1, T2
``nexus_rdr/213-decision-hook-delivers-on-channel-wake-2026-09-17``).

It also publishes its :meth:`ChannelWaiter.status` to a per-session
on-disk record (:func:`write_channel_status`) at every wake, since the
`nx doctor` row (bead nexus-rplay.13) runs in the separate CLI process and
has no other way to see this process's live state.

Neither half needs ``mcp.server.session.ServerSession`` at all: sending a
notification is a raw ``JSONRPCNotification`` on the write stream (the
SDK's typed notification union has no member for a channel notification),
and gating a send on "the client has finished initializing" is done by
registering a handler for ``types.InitializedNotification`` on the
low-level ``Server`` -- the SAME public registration point
``Server.progress_notification()`` uses, confirmed reachable: a client
notification flows through ``BaseSession._receive_loop`` ->
``_received_notification`` (session-internal state) -> unconditionally
``_handle_incoming`` -> ``session.incoming_messages`` -> ``Server.run``'s
own message loop -> ``_handle_notification`` -> ``notification_handlers
[type(notify)]``. ``InitializedNotification`` is the one client
notification NOT swallowed before that last dispatch (unlike
``InitializeRequest``, which ``ServerSession._received_request`` answers
and completes without ever reaching the low-level ``Server``), so this
handler fires exactly once per session, right when the handshake
completes.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from nexus.db.t2.records import TupleRow, WaitResult, WaitSpec

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from nexus.mcp.subscriptions import SubscriptionSet

_log = structlog.get_logger(__name__)

#: Declared at initialize (RDR-211 Approach item 7). An empty object --
#: Claude Code's channel preview reads the KEY's presence, not any value
#: inside it (RDR-211 Phase 1 Step 0 spike).
CHANNEL_CAPABILITY: dict[str, dict[str, Any]] = {"claude/channel": {}}

#: The notification method the spike proved works (T2
#: ``nexus_rdr/211-spike-4-channel-2026-09-17``): the SDK's typed
#: notification union has no member for it, so it is sent as a raw
#: ``JSONRPCNotification``.
_CHANNEL_METHOD = "notifications/claude/channel"

#: Production constants (RDR-213 Technical Design "Delivery"). Tests
#: inject short overrides through :class:`ChannelWaiter`'s constructor so
#: the suite never actually waits 150s.
DEFAULT_WAIT_TIMEOUT_S = 25
DEFAULT_REANNOUNCE_INTERVAL_S = 150.0
DEFAULT_MAX_ANNOUNCES = 5
#: Seconds `run()` sleeps after a tick fails for a reason other than
#: "engine without wait" (a transient HTTP or store error) before the next
#: tick. The loop never dies on one bad round-trip.
DEFAULT_TICK_ERROR_BACKOFF_S: float = 5.0
#: Belt-and-braces floor: a minimum real-clock gap `run()` enforces
#: between the START of one tick and the START of the next, whenever a
#: tick returns faster than this. Genuinely defensive under the cursor
#: design (T2 `nexus_rdr/213-decision-announcements-rate-limited-not-ack-
#: gated-2026-09-17`) -- the cursor moving past every referenced row
#: already makes a busy loop structurally impossible, since `wait`'s own
#: `since` then excludes it from matching again -- kept as a belt against
#: a future bug, or an engine, that returns from `wait()` before its own
#: timeout for a reason this waiter did not anticipate.
DEFAULT_MIN_TICK_INTERVAL_S: float = 0.25
#: Consecutive fast ticks (faster than `min_tick_interval_s`) before the
#: floor logs a warning -- once per streak, not on every occurrence: an
#: occasional fast tick (e.g. a re-send point was already due) is
#: expected and not itself a problem.
_FAST_TICK_WARN_STREAK = 5

# ── Cross-process status (RDR-211 Phase 1 Step 3, bead nexus-rplay.13) ─────
#
# `nx doctor` runs in the CLI process; the waiter runs in the session's
# `nx-mcp` process. The waiter publishes its `status()` dict to a small
# per-session JSON file so the CLI process can read it for THIS session
# with no network round trip and no dependency on the MCP process still
# being reachable. Byte-for-byte the same on-disk SHAPE
# `nexus.mcp.subscriptions.registration_path` uses for the drain hook's per-session instance
# registration (`<state_dir>/tuple-watch/addresses.d/<session id>`) --
# same parent directory, same session-id-keyed leaf, same atomic
# temp-file-then-rename write -- copied rather than imported for the same
# reason `nexus.mcp.subscriptions` carries its own copy of
# `write_instance_registration` (see that module's docstring): the retired
# CLI watcher module that first wrote these files is gone (nexus-rplay.14).
#
# A missing, unreadable, or malformed file all read as "no status
# recorded for this session" -- never a crash, never a stale guess.

#: Mirrors `nexus.mcp.subscriptions._SAFE_SESSION_ID` -- the value becomes a bare
#: directory-entry name, so anything outside a safe, boring charset is
#: refused rather than sanitised.
_SAFE_CHANNEL_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _channel_status_dir(state_dir: Path) -> Path:
    return state_dir / "tuple-watch" / "channel-status.d"


def _channel_status_path(state_dir: Path, session_id: str) -> Path:
    return _channel_status_dir(state_dir) / session_id


def write_channel_status(state_dir: Path, session_id: str, status: dict[str, Any]) -> None:
    """Best-effort atomic write of *status* (a :meth:`ChannelWaiter.status`
    dict) for *session_id* under *state_dir*. A *session_id* outside the
    safe charset is a silent no-op, mirroring
    `nexus.mcp.subscriptions.write_instance_registration`. Never raises: a write failure (a
    read-only filesystem, a missing parent that cannot be created) only
    means the doctor row sees a stale or absent record, never that the
    waiter itself is affected.
    """
    if not _SAFE_CHANNEL_SESSION_ID.fullmatch(session_id):
        return
    path = _channel_status_path(state_dir, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:  # pragma: no cover — best-effort, disk-failure path
        _log.debug("channel_status_write_failed", session_id=session_id, error=str(e))


def read_channel_status(state_dir: Path, session_id: str) -> dict[str, Any] | None:
    """Read the record :func:`write_channel_status` last wrote for
    *session_id*, or ``None`` on a missing file, an unreadable one, or
    malformed JSON -- all three mean "no status recorded for this
    session" to a caller (the `nx doctor` row), never a crash.
    """
    path = _channel_status_path(state_dir, session_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ── Capability declaration + the write stream (design choice (b)) ──────────

#: Set by :func:`run_stdio_with_channel` for the lifetime of the stdio
#: connection; ``None`` outside it (HTTP/SSE transports, or before/after
#: the ``async with stdio_server() as`` block) so a sender never mistakes
#: a torn-down connection for a live one.
_write_stream: Any | None = None

#: Set once the client's ``notifications/initialized`` has been observed
#: (see the module docstring for how). A send before this point risks
#: interleaving with the client's own initialize round-trip, which is why
#: every send in this module awaits it first.
_initialized_event: asyncio.Event | None = None


async def _on_initialized(_notify: Any) -> None:
    if _initialized_event is not None:
        _initialized_event.set()


async def run_stdio_with_channel(mcp: "FastMCP") -> None:
    """Replaces ``mcp.run(transport="stdio")`` -> ``anyio.run(self.
    run_stdio_async)`` at the one call site in :func:`nexus.mcp.core.main`.

    Byte-identical to ``FastMCP.run_stdio_async`` except for the one line
    this bead exists to add: ``experimental_capabilities=
    CHANNEL_CAPABILITY`` on ``create_initialization_options``.
    ``FastMCP.run_stdio_async`` itself passes none (it calls
    ``create_initialization_options()`` with no arguments), so declaring
    the capability requires driving the low-level ``Server.run`` call
    directly -- which is also what hands this function the write stream
    early enough to stash it before any tool call, or the lifespan's
    waiter task, could need it.

    ``mcp._mcp_server.lifespan`` is already ``_t1_lifespan`` wrapped by
    FastMCP's own ``lifespan_wrapper`` (set at ``FastMCP.__init__`` time
    from ``settings.lifespan``), so calling ``Server.run`` here runs the
    EXACT SAME lifespan FastMCP would have run through
    ``run_stdio_async`` -- nothing about lifespan behavior changes, only
    the initialize options passed alongside it.
    """
    import mcp.types as types  # noqa: PLC0415 — deferred: only needed for the stdio driver
    from mcp.server.stdio import stdio_server  # noqa: PLC0415 — deferred: only needed for the stdio driver

    global _write_stream, _initialized_event

    server = mcp._mcp_server  # noqa: SLF001 — the low-level Server FastMCP already wires handlers/lifespan onto; there is no public accessor
    server.notification_handlers[types.InitializedNotification] = _on_initialized

    async with stdio_server() as (read_stream, write_stream):
        _write_stream = write_stream
        _initialized_event = asyncio.Event()
        try:
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(experimental_capabilities=CHANNEL_CAPABILITY),
            )
        finally:
            _write_stream = None
            _initialized_event = None


async def send_channel_notification(content: str, meta: dict[str, str]) -> bool:
    """Send one ``notifications/claude/channel`` notification on the live
    stdio write stream. Returns ``False`` (a documented no-op, never an
    exception) when there is no live write stream -- outside stdio
    transport, before the connection is up, or after it has torn down --
    or the client has not yet completed its own initialize handshake, so
    every caller in this module can call this unconditionally.
    """
    import mcp.types as types  # noqa: PLC0415 — deferred: only needed when actually sending
    from mcp.shared.message import SessionMessage  # noqa: PLC0415 — deferred: only needed when actually sending

    ws = _write_stream
    ev = _initialized_event
    if ws is None or ev is None:
        return False
    if not ev.is_set():
        await ev.wait()
        if _write_stream is None:  # torn down while we waited
            return False
    message = types.JSONRPCMessage(
        types.JSONRPCNotification(jsonrpc="2.0", method=_CHANNEL_METHOD, params={"content": content, "meta": meta})
    )
    await ws.send(SessionMessage(message=message))
    return True


# ── The waiter ───────────────────────────────────────────────────────────


@dataclass
class _LastRef:
    """The last reference sent for one mailbox (RDR-213 Approach item 2,
    T2 `nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
    2026-09-17`): the exact rendered `(content, meta)` a re-send replays
    verbatim (never re-derived from a row read back -- there is no row
    read-back anywhere in this design), when it was last sent, and how
    many times. Superseded outright -- this dict entry simply replaced --
    the instant a DIFFERENT row is referenced for the same mailbox;
    re-sent at `reannounce_interval_s` intervals up to `max_announces`
    times, then left alone (never dropped) until superseded."""

    content: str
    meta: dict[str, str]
    sent_at: float
    count: int = 1


#: Sam's decision, T2 ``nexus_rdr/211-decision-push-reference-2026-09-17``,
#: carried into RDR-213: the notification `content` a mailbox row or board
#: post sends is a FIXED template built only from server-controlled
#: identifiers -- never the tuple's own body, `from`, `kind`, or
#: `correlation_id` (those stay in `meta`, unchanged). MVV finding F1 (T2
#: `nexus_rdr/213-decision-hook-delivers-on-channel-wake-2026-09-17`): with
#: the plugin's hooks loaded, the channel notification itself fires
#: `UserPromptSubmit` and `mailbox_drain.py` claims, acks and renders the
#: body with THAT prompt, before the model's turn, so the session claims
#: for itself only when no body was rendered that way -- the text states
#: both outcomes so the model does not act on an already-claimed row. The
#: SAME text covers a row already claimed when the cursor passes it (T2
#: `nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-2026-
#: 09-17`): the model calls `tuple_in`, gets nothing, and this text
#: already says what that means.
def _mailbox_notification_content(subspace: str, tuple_id: str, to_address: str) -> str:
    return (
        f"nexus mailbox message: subspace {subspace}, tuple {tuple_id}. If its body is rendered "
        "with this message, the mailbox hook already claimed and acked it: act on it, claim "
        f'nothing. If not, claim it yourself with tuple_in("{subspace}", {{"to": "{to_address}"}}), '
        "then act: tuple_ack (with a reply for a request), tuple_nack, or tuple_release with the "
        "claim id. The waiter holds no claim."
    )


def _board_notification_content(subspace: str, tuple_id: str) -> str:
    return (
        f"nexus board post: subspace {subspace}, tuple {tuple_id}. Read new posts with "
        "tuple_rd on that subspace from your cursor (tuple_subscriptions shows it)."
    )


class ChannelWaiter:
    """One session's lifespan waiter: loops ``HttpTupleStore.wait`` over
    its :class:`~nexus.mcp.subscriptions.SubscriptionSet`, delivering
    board posts and mailbox references by CURSOR, never a claim (RDR-213,
    T2 ``nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
    2026-09-17``).

    Every subscription -- board or mailbox -- enters the SAME single
    ``wait`` call every tick, with ``since`` set to that subspace's own
    cursor (:meth:`_build_specs`): the position just past the last row
    this waiter has already referenced or skipped. The spec is NEVER
    empty. A wake returning a new mailbox row advances that mailbox's
    cursor past it and, unless the row is dead-lettered (skipped
    silently -- the ``UserPromptSubmit`` drain hook surfaces a dead row
    once on its own), sends ONE reference for it, claimed or not: the
    notification text already states what an empty ``tuple_in`` means, so
    there is nothing to read back first. The reference is re-sent at
    ``reannounce_interval_s`` intervals up to ``max_announces`` times
    (:meth:`_resend_due_references`) until superseded by the next row's
    own reference; nothing is ever read back to check whether the prior
    row was consumed.

    This makes a busy loop structurally impossible rather than excluded
    by a rule: the cursor moves past every row this waiter has ever acted
    on, so ``wait``'s own ``since`` filter stops that row from matching
    again, and the call genuinely parks. Two rows arriving together are
    referenced one wake apart (the second becomes the new cursor position
    the very next tick, immediately, not gated on the first being acked);
    a restart walks a backlog one reference per wake, exactly as RDR-213
    already says a restart re-announces once more. The cursor is not
    perfectly ordered against the engine's own ``created_at`` (stamped at
    transaction start, not commit -- ``nexus-vsipz``), so a row from a
    slower, earlier-started transaction can commit after the cursor has
    already advanced past a younger row's position and be skipped by
    every later ``since`` query; the cost is a delayed wake, never a lost
    message, since the ``UserPromptSubmit`` drain hook and ``tuple_in``
    both filter by claim state, not by this cursor.

    Constants (`wait_timeout_s`, `reannounce_interval_s`, `max_announces`)
    default to the production values (RDR-213 Technical Design "Delivery":
    25/150/5) and are overridden only by tests, so the suite never
    actually waits real minutes.

    `sender` defaults to :func:`send_channel_notification`; tests inject
    a fake recording calls instead of touching a real stdio connection.

    `store_factory` (never a bare `HttpTupleStore` handle) for the same
    reason :func:`~nexus.mcp.subscriptions._directory_heartbeat` takes
    one: a store handle obtained inside one `with _t2_ctx() as db:`
    block is CLOSED when that block exits (`T2Database.__exit__` ->
    `close()` -> `self.tuples.close()`), so this waiter -- which
    outlives any single call by the whole session -- opens a fresh
    context around each operation instead of holding one open
    indefinitely.
    """

    def __init__(
        self,
        session_id: str,
        store_factory: Callable[[], Any],
        subs: "SubscriptionSet",
        *,
        sender: Callable[[str, dict[str, str]], Awaitable[bool]] = send_channel_notification,
        persist: Callable[[], None] = lambda: None,
        state_dir: Path | None = None,
        wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
        reannounce_interval_s: float = DEFAULT_REANNOUNCE_INTERVAL_S,
        max_announces: int = DEFAULT_MAX_ANNOUNCES,
        tick_error_backoff_s: float = DEFAULT_TICK_ERROR_BACKOFF_S,
        min_tick_interval_s: float = DEFAULT_MIN_TICK_INTERVAL_S,
    ) -> None:
        self.session_id = session_id
        self.store_factory = store_factory
        self.subs = subs
        self.sender = sender
        #: Best-effort T1 write-back after a board cursor advances, so a
        #: `/resume` does not re-deliver posts already shown this
        #: session. Defaults to a no-op (tests; a caller managing
        #: persistence itself).
        self.persist = persist
        #: `None` (the default; tests that do not care about the on-disk
        #: status record) means :meth:`_publish_status` is a no-op. A real
        #: caller (`nexus.mcp.core._start_channel_waiter`) passes
        #: `nexus_config_dir()` so the `nx doctor` row (bead nexus-rplay.13)
        #: can read this waiter's status cross-process.
        self.state_dir = state_dir
        self.wait_timeout_s = wait_timeout_s
        self.reannounce_interval_s = reannounce_interval_s
        self.max_announces = max_announces
        self.tick_error_backoff_s = tick_error_backoff_s
        self.min_tick_interval_s = min_tick_interval_s
        #: Consecutive ticks in `run()`'s loop faster than
        #: `min_tick_interval_s` -- the floor's own bookkeeping, not the
        #: fix (see `DEFAULT_MIN_TICK_INTERVAL_S`).
        self._fast_tick_streak = 0
        self._warned_fast_ticks = False

        #: subspace -> `(created_at, id)` past the last row this waiter
        #: has referenced OR skipped (dead-lettered) for that mailbox --
        #: the position `_build_specs` threads into that mailbox's own
        #: `WaitSpec.since`, exactly the board path's own cursor. Only
        #: ever moves forward.
        self._cursor: dict[str, tuple[str, str]] = {}
        #: subspace -> the last reference sent for that mailbox (see
        #: `_LastRef`) -- the whole of this design's back-pressure state.
        self._last_ref: dict[str, _LastRef] = {}
        #: Cumulative count of DISTINCT rows ever referenced (never
        #: incremented on a re-send of the same reference).
        self._announced_total = 0

        self._alive = False
        self._stopped = False
        self._last_wake: datetime | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

        subs.add_listener(self._on_subscription_change)

    def _on_subscription_change(self, _subs: "SubscriptionSet") -> None:
        """RDR-211 Technical Design "Waiting" (unchanged by RDR-213): "A
        subscription change cancels the parked wait ... and re-issues it
        with the new list". This waiter takes the RDR's explicitly-offered
        alternative -- "let the engine's 25s cap bound it" -- rather than
        cancelling an in-flight synchronous httpx call from another
        thread: every `tick()` already rebuilds its `WaitSpec`s fresh from
        `subs.entries()`, so the new list is live at the very next tick,
        at most one `wait_timeout_s` later. This callback's only job is
        the log line the RDR asks to "record what you did"."""
        _log.info("channel_waiter_subscription_changed", session_id=self.session_id)

    # ── status (the doctor-row bead, .13, is the intended reader) ───────────

    def status(self) -> dict[str, Any]:
        """`alive`: this waiter's task is running (never proved past
        `_stop_no_wait_support` or a real cancellation). `last_wake`:
        ISO-8601 timestamp of the last completed `wait()` round-trip, or
        `None` before the first one. `announced`: the cumulative count of
        DISTINCT rows this waiter has ever referenced (never incremented
        on a re-send). `pending`: how many mailboxes have a last reference
        still under budget (`count < max_announces`) and not yet
        superseded by a newer row's own reference -- 0 or 1 per mailbox.
        `oldest_pending_age_s`: seconds since the oldest such reference
        was (last) sent, or `None` when `pending` is 0."""
        active = [ref for ref in self._last_ref.values() if ref.count < self.max_announces]
        oldest_pending_age_s = (time.monotonic() - min(ref.sent_at for ref in active)) if active else None
        return {
            "alive": self._alive,
            "last_wake": self._last_wake.isoformat() if self._last_wake else None,
            "announced": self._announced_total,
            "pending": len(active),
            "oldest_pending_age_s": oldest_pending_age_s,
        }

    def _publish_status(self) -> None:
        """Best-effort refresh of the on-disk record (bead nexus-rplay.13)
        -- a no-op when this waiter was constructed with no `state_dir`.
        Called at every wake (:meth:`tick`'s end) and this loop's own
        start/stop, so a cross-process reader never sees a record older
        than the waiter's current state by more than one in-flight
        operation."""
        if self.state_dir is not None:
            write_channel_status(self.state_dir, self.session_id, self.status())

    # ── the loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """The loop. The cursor design (`tick`/`_build_specs`) makes a
        busy loop structurally impossible on its own -- `wait`'s own
        `since` excludes every row this waiter has already referenced or
        skipped, so a healthy tick never returns faster than a genuine
        wake or its own capped timeout; the floor below is a defensive
        belt on top of that, not the fix -- see
        `DEFAULT_MIN_TICK_INTERVAL_S`."""
        self._loop = asyncio.get_running_loop()
        self._alive = True
        self._publish_status()
        try:
            while not self._stopped:
                tick_started = time.monotonic()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad round-trip must never end delivery for the session
                    _log.warning(
                        "channel_waiter_tick_failed", session_id=self.session_id,
                        error=repr(exc), backoff_s=self.tick_error_backoff_s,
                    )
                    await asyncio.sleep(self.tick_error_backoff_s)
                    continue  # the backoff above already paces this tick; the floor below is redundant for it
                elapsed = time.monotonic() - tick_started
                if elapsed < self.min_tick_interval_s:
                    self._fast_tick_streak += 1
                    if self._fast_tick_streak >= _FAST_TICK_WARN_STREAK and not self._warned_fast_ticks:
                        _log.warning(
                            "channel_waiter_fast_tick_floor_triggered",
                            session_id=self.session_id, streak=self._fast_tick_streak,
                        )
                        self._warned_fast_ticks = True
                    await asyncio.sleep(self.min_tick_interval_s - elapsed)
                else:
                    self._fast_tick_streak = 0
                    self._warned_fast_ticks = False
        finally:
            self._alive = False
            self._publish_status()

    def _call(self, fn: Callable[[Any], Any]) -> Any:
        """Run *fn* against a freshly opened tuples store, closing it
        before returning -- see the class docstring for why this never
        holds a store handle open across calls. Always run off the event
        loop (`asyncio.to_thread`): `HttpTupleStore` is a synchronous
        httpx client."""
        with self.store_factory() as db:
            return fn(db.tuples)

    async def tick(self) -> None:
        """One iteration. Drops the cursor and last reference of any
        mailbox no longer subscribed (unsubscribe), then parks ONE
        `wait()` call over EVERY subscription -- board or mailbox alike,
        the spec is never empty -- capped to the nearest still-active
        mailbox's next re-send point so a re-send is never late by up to
        a full tick merely because some OTHER subspace kept the spec
        matching sooner. Exposed (not folded into :meth:`run`) so tests
        can drive iterations directly instead of a real timed loop."""
        live_subspaces = {e["subspace"] for e in self.subs.entries()}
        for subspace in list(self._cursor):
            if subspace not in live_subspaces:
                del self._cursor[subspace]
        for subspace in list(self._last_ref):
            if subspace not in live_subspaces:
                del self._last_ref[subspace]

        specs = self._build_specs()
        effective_timeout_s = min(self.wait_timeout_s, self._seconds_to_nearest_resend_s())
        # `HttpTupleStore.wait`'s `timeout_s` is typed (and wired to the
        # engine's `long timeoutSeconds`) as an integer -- floor, never
        # round, so this tick wakes AT OR BEFORE the nearest re-send
        # point, never after it.
        wait_timeout_arg = max(0, int(effective_timeout_s))
        try:
            results: list[WaitResult] = await asyncio.to_thread(
                self._call, lambda t: t.wait(specs, wait_timeout_arg),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                # The route switch's default branch on an engine predating
                # `/wait` (`unknown tuples op: /wait`, a bare 404): no wait,
                # no waiter; the drain hook is the floor.
                self._stop_no_wait_support()
                return
            raise  # any other status is a transient fault: `run()` logs, backs off and ticks again
        just_referenced = await self._process_results(results)
        self._last_wake = datetime.now(UTC)
        await self._resend_due_references(skip=just_referenced)
        self._publish_status()

    def _stop_no_wait_support(self) -> None:
        self._stopped = True
        _log.warning("channel_waiter_no_wait_support", session_id=self.session_id)

    def _seconds_to_nearest_resend_s(self) -> float:
        """Seconds until the nearest still-active mailbox's next re-send
        is due (floored at 0); `wait_timeout_s` when none is due at all --
        either no mailbox has a last reference, or every one is already
        spent (`count >= max_announces`, which must never re-enter this
        calculation -- a spent reference is left alone, not resent)."""
        now = time.monotonic()
        due_times = [
            ref.sent_at + self.reannounce_interval_s
            for ref in self._last_ref.values()
            if ref.count < self.max_announces
        ]
        if not due_times:
            return float(self.wait_timeout_s)
        return max(0.0, min(due_times) - now)

    def _build_specs(self) -> list[WaitSpec]:
        """Every subscription -- board or mailbox -- enters the spec
        every tick (T2 `nexus_rdr/213-decision-announcements-rate-
        limited-not-ack-gated-2026-09-17`): a mailbox's own spec asks for
        `n=1` since its own cursor, exactly the board path's own shape.
        The spec is NEVER empty."""
        specs: list[WaitSpec] = []
        for entry in self.subs.entries():
            subspace = entry["subspace"]
            if subspace.startswith("board/"):
                cursor = entry.get("cursor")
                since = (cursor["created_at"], cursor["id"]) if cursor else None
                specs.append(WaitSpec(subspace=subspace, since=since))
            else:
                specs.append(WaitSpec(subspace=subspace, since=self._cursor.get(subspace)))
        return specs

    async def _process_results(self, results: list[WaitResult]) -> set[str]:
        """Board posts: unchanged -- deliver each, advance the cursor,
        persist. Mailboxes: each spec asks for `n=1`, so at most one NEW
        row per mailbox per wake -- dead-lettered: advance the cursor
        past it and send nothing (the drain hook surfaces a dead row once
        on its own); otherwise reference it, claimed or not (the
        notification text already covers an empty `tuple_in`), and
        advance the cursor past it too.

        Returns the set of mailbox subspaces just referenced THIS tick,
        so :meth:`tick` can exclude them from :meth:`_resend_due_
        references` -- a fresh reference's own `sent_at` is `now`, and
        `reannounce_interval_s=0` (several tests use it to force "always
        due" quickly) would otherwise make the SAME tick's resend pass
        re-send it a second time immediately."""
        advanced = False
        just_referenced: set[str] = set()
        for result in results:
            if result.subspace.startswith("board/"):
                for row in result.tuples:
                    await self._deliver_board_post(result.subspace, row)
                if result.tuples:
                    last = result.tuples[-1]
                    self.subs.advance_cursor(result.subspace, (last.created_at or "", last.id))
                    advanced = True
                continue
            for row in result.tuples:  # n=1 caps this to at most one row
                if row.claim_state == "dead":
                    self._cursor[result.subspace] = (row.created_at or "", row.id)
                    continue
                await self._reference_mailbox_row(result.subspace, row)
                just_referenced.add(result.subspace)
        if advanced:
            # Code review Significant 3 (RDR-211): `self.persist()` (a T1
            # write-back, a synchronous HTTP call) must never run directly
            # on the event loop -- every other store call in this class
            # already goes through `asyncio.to_thread` for exactly this
            # reason.
            await asyncio.to_thread(self.persist)
        return just_referenced

    async def _deliver_board_post(self, subspace: str, row: TupleRow) -> None:
        meta = {"subspace": subspace, "tuple_id": row.id}
        for key in ("from", "kind"):
            if row.dims.get(key):
                meta[key] = row.dims[key]
        await self.sender(_board_notification_content(subspace, row.id), meta)

    async def _reference_mailbox_row(self, subspace: str, row: TupleRow) -> None:
        """Send ONE reference for *row* (claimed or not -- the
        notification text already covers an empty `tuple_in`), advance
        *subspace*'s cursor past it, and start a fresh re-send budget --
        superseding, by simple dict overwrite, whatever reference this
        mailbox tracked before."""
        to_address = subspace.removeprefix("mailbox/")
        meta = {"subspace": subspace, "tuple_id": row.id}
        for key in ("from", "kind", "correlation_id"):
            if row.dims.get(key):
                meta[key] = row.dims[key]
        content = _mailbox_notification_content(subspace, row.id, to_address)
        await self.sender(content, meta)
        self._cursor[subspace] = (row.created_at or "", row.id)
        self._last_ref[subspace] = _LastRef(content=content, meta=meta, sent_at=time.monotonic(), count=1)
        self._announced_total += 1

    async def _resend_due_references(self, *, skip: set[str]) -> None:
        """Re-send every mailbox's last reference whose budget is not yet
        spent and whose re-send point is due -- the EXACT `(content,
        meta)` last sent, replayed verbatim; nothing is ever read back to
        check whether the row was consumed. *skip* names the mailboxes
        `_process_results` just referenced THIS tick -- excluded here
        so a fresh reference (`sent_at = now`) is never ALSO resent in
        the same tick merely because `reannounce_interval_s` happens to
        be 0 (several tests use that to force "always due" quickly)."""
        now = time.monotonic()
        for subspace, ref in self._last_ref.items():
            if subspace in skip:
                continue
            if ref.count < self.max_announces and (now - ref.sent_at) >= self.reannounce_interval_s:
                await self.sender(ref.content, ref.meta)
                ref.sent_at = now
                ref.count += 1

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def cancel(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown must never raise past the lifespan
            pass


# ── One waiter per process (one session per nx-mcp process) ─────────────────

_ACTIVE: dict[str, ChannelWaiter] = {}


def register_active_waiter(waiter: ChannelWaiter) -> None:
    _ACTIVE[waiter.session_id] = waiter


def unregister_active_waiter(session_id: str) -> None:
    _ACTIVE.pop(session_id, None)


def active_waiter(session_id: str) -> ChannelWaiter | None:
    return _ACTIVE.get(session_id)
