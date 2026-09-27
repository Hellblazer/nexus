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
  original intent). Mailboxes and boards now take DIFFERENT shapes (bead
  nexus-vsipz, RDR-213 engine half, superseding the cursor-shares-one-
  shape design T2 ``nexus_rdr/213-decision-announcements-rate-limited-
  not-ack-gated-2026-09-17`` first landed; bead nexus-q82tk then moved
  boards onto the same mechanism): every spec, mailbox or board, asks
  the ENGINE to gate cadence and cap with an ``announce={interval_s,
  max[, subscriber]}`` field, and the engine returns a row only when it
  is claimable and due, stamping ``announced_at``/``announce_count`` in
  the same statement that selects it (``TupleRepository.WaitSpec
  .Announce``, service-side). A mailbox's stamp lives on the row. A
  board post is read by many sessions and never claimed, so its stamp
  lives per ``(subspace, subscriber, tuple)`` in
  ``nexus.tuple_deliveries``, keyed by this session's id, with ``max=1``
  (:data:`DEFAULT_BOARD_MAX_ANNOUNCES`): announced once to each
  subscriber, never again, since a post is never claimed and "unanswered"
  is not observable for it. This closes the cursor design's one
  structural gap for both shapes: a cursor keyed on ``(created_at, id)``
  can skip a transaction that started earlier but committed later,
  because a client-side position has no way to know a slower sibling is
  still in flight. The engine's own re-scan of the claimable-and-due set,
  ordered oldest first with no position to skip past, cannot lose that
  row. The waiter tracks nothing about pacing itself any more -- no
  cursor for either shape, no last-reference bookkeeping, no re-send
  pass, no same-tick double-send exclusion, no dead-row skip (the
  engine's own claimable filter already excludes a dead-lettered row) --
  it renders whatever the engine hands it and stops.

  Bead nexus-n36sw (follow-up to nexus-zxthy): a fresh board subscription
  still had no start position of its own before this bead -- the engine
  stamped and returned EVERY retained post the very first time a new
  subscriber asked, and this waiter's own client-side drop
  (:meth:`ChannelWaiter._deliver_board_rows`) only hid the ones it judged
  older than the topic's subscribe time from the NOTIFICATION, after the
  engine had already exhausted them. A board spec now also sends
  ``since`` (:meth:`ChannelWaiter._build_specs`), which
  ``TupleRepository.queryOnceAnnounceSubscriber`` folds into its own
  candidate predicate: a row at or before the watermark is never
  selected, stamped, or returned in the first place. An engine predating
  this bead refuses ``since`` alongside ANY ``announce`` outright;
  :meth:`ChannelWaiter.tick` detects that refusal on first contact and
  falls back to the pre-nexus-n36sw client-side drop for the rest of the
  waiter's life, so a new client against an old engine still works.

RDR-211 gated every mailbox claim on proof that the channel was live for
this session (a parent command-line read, or a probe notification the
session had to answer), because a claim held for a session that could
never hear the channel would strand the message for the lease. RDR-213
deletes the gate along with the claim itself: with no claim to strand, the
worst a lost notification costs is a wait until the next wake or the next
prompt, which the ``UserPromptSubmit`` drain hook
(:mod:`nexus.hooks.mailbox_drain`) renders regardless. The command-line-reading
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
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from nexus.db.limits import MAX_QUERY_RESULTS
from nexus.db.t2.http_tuple_store import SchemaViolationError
from nexus.db.t2.records import Announce, TupleRow, WaitResult, WaitSpec

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
#: A board post is announced ONCE per subscriber (bead nexus-q82tk): a
#: post is never claimed, so nothing can tell the engine it was acted on,
#: and a re-announce budget would only wake the session again for a post
#: it already saw. The old cursor announced a post once too.
DEFAULT_BOARD_MAX_ANNOUNCES = 1
#: Bead nexus-zxthy: rows one board spec asks for per wait. The engine's
#: own default is 1, which turned a backlog into one notification per
#: tick (282 in 80 s on 2026-09-27). Bounded like a consumer's in-flight
#: cap (Reactive Streams demand, MQTT Receive Maximum, NATS
#: max_ack_pending); the rows of one wait fold into ONE notification.
DEFAULT_BOARD_WAIT_ROWS: int = 100
#: Bead nexus-zxthy: seconds `run()` settles after a tick that returned
#: board rows before the next wait, so a cluster of posts (a CI push
#: starts ~20 jobs together) lands in one wait and one notification
#: instead of one per tick. Also the bound on how much a mailbox
#: reference can be delayed behind a board burst.
DEFAULT_BOARD_COALESCE_S: float = 3.0
#: Bead nexus-zxthy (widened nexus-n36sw): the start-position filter
#: compares the ENGINE's `created_at` with the CLIENT's subscribe time, two
#: clocks. A post made just after subscribing on an engine whose clock runs
#: behind this box would otherwise read as backlog and be dropped; a
#: margin this wide costs at most one folded notification of recent posts
#: on subscribe. Bead nexus-n36sw moved the comparison itself onto the
#: engine (`WaitSpec.since` alongside a per-subscriber `announce`), so this
#: margin is now subtracted from the watermark BEFORE it is sent
#: (`_build_specs`), not applied to a client-side compare after the fact --
#: same margin, same reason, moved to where the compare now runs.
DEFAULT_BOARD_START_SKEW_S: float = 30.0
#: Seconds `run()` sleeps after a tick fails for a reason other than
#: "engine without wait" (a transient HTTP or store error) before the next
#: tick. The loop never dies on one bad round-trip.
DEFAULT_TICK_ERROR_BACKOFF_S: float = 5.0
#: Bead nexus-ymfak (nexus-rxuiq residual): the startup catch-up read's
#: recency window is `wait_timeout_s + this margin`, seconds -- wide
#: enough to cover a row an orphaned predecessor waiter's still-parked
#: `wait()` stamped in the gap between that predecessor's process dying
#: and this waiter's construction (bounded by `wait_timeout_s`, the
#: longest that parked call can still be live), plus slack for the
#: catch-up read's own round trip.
DEFAULT_CATCHUP_MARGIN_S: float = 5.0

#: Belt-and-braces floor: a minimum real-clock gap `run()` enforces
#: between the START of one tick and the START of the next, whenever a
#: tick returns faster than this. Genuinely defensive, not the fix, for
#: either subspace shape: every spec's `announce` field makes the
#: engine itself refuse to return a row before its own interval/cap says
#: so (bead nexus-vsipz for mailboxes, nexus-q82tk for boards) -- kept as
#: a belt against a
#: future bug, or an engine, that returns from `wait()` before its own
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
# being reachable. Written the same way the now-deleted per-session
# instance-registration file was (`<state_dir>/tuple-watch/addresses.d/
# <session id>`, RDR-208 Phase 3, bead nexus-galkv.20 removed it): same
# parent directory shape, same session-id-keyed leaf, same atomic
# temp-file-then-rename write, under its own `channel-status.d` leaf.
#
# A missing, unreadable, or malformed file all read as "no status
# recorded for this session" -- never a crash, never a stale guess.

#: A session id outside this safe, boring charset becomes a bare
#: directory-entry name, so it is refused rather than sanitised -- the
#: same charset `nexus.mcp.subscriptions` validates a session id against.
_SAFE_CHANNEL_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _channel_status_dir(state_dir: Path) -> Path:
    return state_dir / "tuple-watch" / "channel-status.d"


def _channel_status_path(state_dir: Path, session_id: str) -> Path:
    return _channel_status_dir(state_dir) / session_id


def write_channel_status(state_dir: Path, session_id: str, status: dict[str, Any]) -> None:
    """Best-effort atomic write of *status* (a :meth:`ChannelWaiter.status`
    dict) for *session_id* under *state_dir*. A *session_id* outside the
    safe charset is a silent no-op. Never raises: a write failure (a
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


#: Sam's decision, T2 ``nexus_rdr/211-decision-push-reference-2026-09-17``,
#: carried into RDR-213: the notification `content` a mailbox row or board
#: post sends is a FIXED template built only from server-controlled
#: identifiers -- never the tuple's own body, `from`, `kind`, or
#: `correlation_id` (those stay in `meta`, unchanged). MVV finding F1 (T2
#: `nexus_rdr/213-decision-hook-delivers-on-channel-wake-2026-09-17`): with
#: the plugin's hooks loaded, the channel notification itself fires
#: `UserPromptSubmit` and the drain hook claims, acks and renders the
#: body with THAT prompt, before the model's turn, so the session claims
#: for itself only when no body was rendered that way -- the text states
#: both outcomes so the model does not act on an already-claimed row. The
#: SAME text covers a row someone else claims or consumes in the gap
#: between this notification being sent and the model acting on it (bead
#: nexus-vsipz: the engine's announce-mode query excludes a claimed-and-
#: live row from being referenced in the FIRST place, but a race after
#: the reference is already in flight is still possible): the model calls
#: `tuple_in`, gets nothing, and this text already says what that means.
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
        f"nexus board post: subspace {subspace}, tuple {tuple_id}. Read it with "
        "tuple_rd on that subspace. Posts are never claimed."
    )


def _board_batch_notification_content(
    subspace: str, count: int, first_id: str, last_id: str, since: tuple[str, str] | None,
) -> str:
    """Bead nexus-zxthy: ONE notification for *count* posts of one wait.
    Identifiers only, as the single-post shape (Sam, 2026-09-17, T2
    nexus_rdr/211-decision-push-reference-2026-09-17): the subspace, the
    count, the first and last tuple id, and how to read them. *since* is
    the ``(created_at, id)`` of the last post this waiter delivered for
    the topic before this batch, when it knows one, so the read hint is
    exact; otherwise the hint is the newest *count* rows."""
    if since is not None:
        read = (
            f'tuple_rd("{subspace}", n={count}, since_created_at="{since[0]}", '
            f'since_id="{since[1]}")'
        )
    else:
        read = f'tuple_rd("{subspace}", n={count})'
    return (
        f"nexus board posts: subspace {subspace}, {count} new posts, tuples {first_id} "
        f"to {last_id}. Read them with {read}. Posts are never claimed."
    )


def _parse_created_at(value: str | None) -> datetime | None:
    """ISO-8601 as the wire renders ``created_at``, or ``None`` when absent
    or not parseable (a row the start-position filter then keeps: an
    unknown age is delivered, never silently dropped)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_announced_at(value: str | None) -> datetime | None:
    """``TupleRow.announced_at`` as the wire actually sends it -- an
    ISO-8601 timestamp string, or ``None`` for a row nothing has ever
    announced. Returns ``None`` on a missing or unparseable value (never
    raises): the catch-up read this backs (:meth:`ChannelWaiter.
    _catchup_mailbox_rows`) treats "can't tell" exactly like "not
    recent" -- skip the row, let the ordinary announce-mode `tick()` loop
    find it on its own schedule instead."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_last_waiter_token_ns = 0


def _mint_waiter_token() -> str:
    """``<time_ns>-<uuid4 hex>``, strictly increasing within this process
    (bead nexus-rxuiq): the engine orders tokens by time, then by id, and a
    same-nanosecond tie broken by a random id would let the earlier of two
    waiters win. Across processes the wall clock orders them, which is what a
    ``claude --resume`` into a new process needs; two processes minting in
    the same nanosecond are ordered arbitrarily. Known limitation: if the
    host clock STEPS backwards between an old process's mint and its
    successor's, the successor loses and stops as ``superseded``; the drain
    hook still delivers, and an MCP restart mints a fresh token."""
    global _last_waiter_token_ns
    now = max(time.time_ns(), _last_waiter_token_ns + 1)
    _last_waiter_token_ns = now
    return f"{now}-{uuid.uuid4().hex}"


def _probe_serving_engine_version() -> tuple[int, int, int] | None:
    """Best-effort ``GET /version`` ``release_version`` for the engine THIS
    session is actually talking to (local or cloud, whichever
    :func:`~nexus.db.service_endpoint.resolve_service_endpoint_with_
    evidence_gate` resolves) -- nexus-6konb.15 (RDR-213 MVV finding L1).

    Fails closed to ``None`` on ANY resolution, transport, non-200, or
    parse failure, INCLUDING a blank/dev ``release_version`` (a
    dev-checkout jar reports none): this call site has no fatal to raise
    on a probe failure, since "don't know" simply leaves the row-based
    fallback (:meth:`ChannelWaiter._engine_ignores_announce`/
    :meth:`~ChannelWaiter._engine_ignores_subscriber`) as the only
    detector, exactly as it was before this probe existed. Mirrors the
    ``GET /version`` probe idiom :func:`nexus.db.managed_endpoint.
    probe_managed_service` and :func:`nexus.db.http_engine_status.
    fetch_engine_status` already use, but never raises.

    Called once, at :meth:`ChannelWaiter.run`'s start -- never per tick.

    Worst-case start delay: in the local cold-lease path only --
    ``resolve_service_endpoint_with_evidence_gate`` retrying with
    ``DEFAULT_LEASE_WAIT_BUDGET_S`` (12.0s, as of this writing) after a
    first resolution fails for a process that has previously seen a live
    lease -- plus this probe's own 5s HTTP timeout, for up to roughly 17s
    total. That budget belongs to endpoint resolution and is unrelated to
    this probe's own failure handling. It delays only this waiter's FIRST
    tick, never the MCP server's own startup (the probe runs inside
    `run()`, which is already an independent asyncio task by the time it
    executes) and never a cloud-mode session (no lease to wait on there).
    """
    try:
        from nexus.db.service_endpoint import (  # noqa: PLC0415 — rare/branch-local: one call per waiter lifetime
            resolve_service_endpoint_with_evidence_gate,
        )
        base_url, _token = resolve_service_endpoint_with_evidence_gate()
    except Exception as exc:  # noqa: BLE001 — best-effort: an unresolvable endpoint means "don't know", not a crash
        _log.debug("channel_waiter_version_probe_endpoint_unresolvable", error=str(exc))
        return None

    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/version", timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — best-effort: a transport blip or a pre-/version engine means "don't know"
        _log.debug("channel_waiter_version_probe_failed", error=str(exc))
        return None

    if not isinstance(body, dict):
        return None

    from nexus.engine_version import parse_engine_version  # noqa: PLC0415 — leaf module, rare/branch-local path

    return parse_engine_version(body.get("release_version"))


class ChannelWaiter:
    """One session's lifespan waiter: loops ``HttpTupleStore.wait`` over
    its :class:`~nexus.mcp.subscriptions.SubscriptionSet`, delivering
    board posts and mailbox references, never a claim (RDR-213, T2
    ``nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
    2026-09-17``).

    Every subscription enters the SAME single ``wait`` call every tick
    (:meth:`_build_specs`); the spec is NEVER empty. A board's spec
    (bead nexus-q82tk) carries ``announce`` with ``subscriber`` set to this session's id and
    ``max=DEFAULT_BOARD_MAX_ANNOUNCES``: the engine keeps the stamp per
    subscriber in ``nexus.tuple_deliveries`` and returns a post to this
    session once. A mailbox's spec carries ``announce={interval_s,
    max}`` (bead nexus-vsipz, RDR-213 engine half): the ENGINE decides
    whether a row is claimable and due, and stamps it in the same
    statement that selects it, so a row it returns is a row this waiter
    has never seen re-sent too soon or too often. A returned mailbox row
    is always sent -- claimed or not, the notification text already
    states what an empty ``tuple_in`` means -- and ``_announced_total``
    increments only the first time a row is seen (``announce_count ==
    1``), never on a re-send the engine itself chose to make.

    This makes a busy loop structurally impossible for either subspace
    shape, by one mechanism: the engine-side due check stops a row from
    matching before its own interval/cap says so, per row for a mailbox
    and per (row, subscriber) for a board. Two rows arriving together are referenced one
    wake apart (the second becomes newly due the very next tick,
    immediately, never gated on the first being acked); a restart with a
    backlog walks it one reference per wake, since a never-announced row
    is always due. A restart that lands MID-INTERVAL on an already-
    announced, not-yet-due row does NOT re-announce it: the stamp lives
    in Postgres, not in this waiter, so the row simply stays silent until
    it is next due on its own schedule -- a real divergence from RDR-213's
    original client-side-dict design, named here rather than left
    implicit (bead nexus-vsipz review round; see the RDR's own amendment
    for the full accounting).

    Constants (`wait_timeout_s`, `reannounce_interval_s`, `max_announces`)
    default to the production values (RDR-213 Technical Design "Delivery":
    25/150/5) and are overridden only by tests, so the suite never
    actually waits real minutes. `reannounce_interval_s`/`max_announces`
    are sent to the ENGINE as `Announce.interval_s`/`.max` every tick
    (bead nexus-vsipz) -- this waiter no longer applies them itself.

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
        state_dir: Path | None = None,
        wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
        reannounce_interval_s: float = DEFAULT_REANNOUNCE_INTERVAL_S,
        max_announces: int = DEFAULT_MAX_ANNOUNCES,
        tick_error_backoff_s: float = DEFAULT_TICK_ERROR_BACKOFF_S,
        min_tick_interval_s: float = DEFAULT_MIN_TICK_INTERVAL_S,
        engine_version_probe: Callable[[], tuple[int, int, int] | None] = _probe_serving_engine_version,
        board_wait_rows: int = DEFAULT_BOARD_WAIT_ROWS,
        board_coalesce_s: float = DEFAULT_BOARD_COALESCE_S,
        board_start_skew_s: float = DEFAULT_BOARD_START_SKEW_S,
    ) -> None:
        self.session_id = session_id
        self.store_factory = store_factory
        self.subs = subs
        self.sender = sender
        #: nexus-6konb.15: a real ``GET /version`` probe by default (see
        #: :func:`_probe_serving_engine_version`); tests inject a fake
        #: returning a fixed tuple (or `None`) instead of touching HTTP.
        #: Called once, at :meth:`run`'s start.
        self.engine_version_probe = engine_version_probe
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
        self.board_wait_rows = board_wait_rows
        self.board_coalesce_s = board_coalesce_s
        self.board_start_skew_s = board_start_skew_s
        #: Bead nexus-zxthy: subspace -> `(created_at, id)` of the LAST
        #: board post this waiter delivered for it, the read hint the next
        #: batch notification carries. Memory only: the engine's stamp is
        #: the delivery record, this is a rendering aid.
        self._board_last_delivered: dict[str, tuple[str, str]] = {}
        #: Bead nexus-zxthy: set by `_process_results` when a tick returned
        #: board rows (kept or dropped), read and cleared by `run()` to
        #: settle `board_coalesce_s` before the next wait.
        self._board_activity = False
        #: Cumulative counts for `status()` (bead nexus-zxthy).
        self._board_batches = 0
        self._board_backlog_dropped = 0
        #: Bead nexus-n36sw: optimistic default -- `_build_specs` sends the
        #: topic's subscribe-time watermark as `since` on every board spec
        #: until proven unsupported. An engine predating this bead refuses
        #: ANY `since` alongside `announce` (its refusal does not look at
        #: `subscriber`), so the first such refusal flips this to `False`
        #: for the REST OF THIS WAITER'S LIFE (never re-tried -- there is no
        #: live-upgrade case for one running process) and `tick()` falls
        #: back to today's behaviour: no `since` on the wire, the client-side
        #: drop in `_deliver_board_rows` does the filtering instead, exactly
        #: as it did before this bead.
        self._board_since_supported = True
        #: Consecutive ticks in `run()`'s loop faster than
        #: `min_tick_interval_s` -- the floor's own bookkeeping, not the
        #: fix (see `DEFAULT_MIN_TICK_INTERVAL_S`).
        self._fast_tick_streak = 0
        self._warned_fast_ticks = False

        #: subspace -> `(announce_count, seen_at)` of the LAST mailbox row
        #: this waiter was handed, where `seen_at` is `time.monotonic()`
        #: (bead nexus-vsipz). The engine owns cadence and cap now -- this
        #: is not back-pressure state, only enough to answer `status()`'s
        #: `pending`/`oldest_pending_age_s` honestly: "pending" means "the
        #: last row we saw for this mailbox had not yet exhausted its
        #: announce budget when we saw it" -- the closest this waiter can
        #: state without reading a row back (which the design deliberately
        #: never does), not a claim that the row is still unconsumed.
        self._last_seen: dict[str, tuple[int, float]] = {}
        #: Cumulative count of DISTINCT rows ever referenced (incremented
        #: only when a mailbox row's own `announce_count == 1` -- its
        #: FIRST send -- never on a re-send the engine chose to make).
        self._announced_total = 0

        self._alive = False
        self._stopped = False
        #: `None` while running, or the running (never alive-and-stopped)
        #: waiter's own stop cause once `_stopped` flips (review round,
        #: bead nexus-vsipz): `"no_wait_support"` (a bare 404 from an
        #: engine predating `/wait` itself) or `"no_announce_support"` (an
        #: engine that answers `/wait` but never renders `announce_count`
        #: -- one predating THIS bead). `cancel()`'s own stop (a normal
        #: lifespan teardown) leaves this `None` -- it is not a fault.
        self._stopped_reason: str | None = None
        #: Bead nexus-rxuiq: this waiter's supersession token, sent on every
        #: announce spec. A later waiter for the same session mints a larger
        #: one (see `_mint_waiter_token` for how far that holds), and the engine then refuses this one's waits, including a
        #: call still parked after this waiter was cancelled or its process
        #: died, instead of letting that call stamp the next row as
        #: announced for nobody.
        self.waiter_token = _mint_waiter_token()
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
        `_stop_no_wait_support`/`_stop_no_announce_support` or a real
        cancellation). `stopped_reason` (bead nexus-vsipz review round):
        `None` while alive or on an ordinary `cancel()` teardown;
        `"no_wait_support"`, `"no_announce_support"`,
        `"no_subscriber_support"` (bead nexus-q82tk) or `"superseded"`
        (bead nexus-rxuiq, a newer waiter for this session took over) when
        one of those loud stops fired -- lets a reader (the doctor row) name WHY
        the waiter is not alive instead of only THAT it is not.
        `last_wake`: ISO-8601 timestamp of the last
        completed `wait()` round-trip, or `None` before the first one.
        `announced`: the cumulative count of DISTINCT rows this waiter has
        ever referenced (incremented only on a row's first send, never on
        a re-send the engine chose to make).

        `pending` (bead nexus-vsipz, RDR-213 engine half): the engine now
        owns cadence and cap, so this waiter has no local back-pressure
        state to report `pending` from precisely. The definition used here
        is the simplest HONEST one available without reading a row back
        (which this design deliberately never does): how many mailboxes'
        LAST SEEN row had not yet exhausted its announce budget
        (`announce_count < max_announces`) at the moment this waiter saw
        it -- not "is still genuinely outstanding" (a claimed-and-acked
        row's last-seen count does not change merely because it was
        consumed; this waiter would have no way to know). `
        oldest_pending_age_s`: seconds since the oldest such row was last
        seen, or `None` when `pending` is 0."""
        active = [seen for seen in self._last_seen.values() if seen[0] < self.max_announces]
        oldest_pending_age_s = (time.monotonic() - min(seen_at for _, seen_at in active)) if active else None
        return {
            "alive": self._alive,
            "stopped_reason": self._stopped_reason,
            "last_wake": self._last_wake.isoformat() if self._last_wake else None,
            "announced": self._announced_total,
            "pending": len(active),
            "oldest_pending_age_s": oldest_pending_age_s,
            # bead nexus-zxthy: board notifications sent (one per topic per
            # wait with new posts) and backlog rows dropped as older than
            # their topic's subscribe time.
            "board_batches": self._board_batches,
            "board_backlog_dropped": self._board_backlog_dropped,
            # bead nexus-n36sw: whether this waiter is sending `since` to the
            # engine on board specs (the engine-owned start position) or has
            # fallen back to the client-side drop for an engine that refuses
            # the combination.
            "board_since_supported": self._board_since_supported,
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
        """The loop. `tick`/`_build_specs` make a busy loop structurally
        impossible on their own, by TWO SEPARATE mechanisms (bead
        nexus-vsipz for mailboxes, nexus-q82tk for boards): every spec's
        `announce` field makes the ENGINE itself refuse to return a row
        before its own interval/cap says so (per row for a mailbox, per
        row and subscriber for a board), so `wait` genuinely parks. A
        healthy tick never returns faster than a genuine wake or its own
        capped timeout for either shape; the floor below is a defensive
        belt on top of that, not the fix -- see
        `DEFAULT_MIN_TICK_INTERVAL_S`.

        nexus-6konb.15 (RDR-213 MVV finding L1): before the first tick,
        `_check_engine_floor_at_start` asks the SAME question
        `_engine_ignores_announce`/`_engine_ignores_subscriber` answer from
        a returned row's shape, but from the engine's own `/version`
        identity instead -- so a below-floor engine never gets to report
        "alive" even while its mailbox stays empty (measured: 16+ minutes
        against engine-service-v0.1.127 with nothing sent, T2
        `nexus_rdr/6konb15-mvv-2026-09-25`).

        Bead nexus-ymfak (nexus-rxuiq residual): also before the first
        tick, `_catchup_mailbox_rows` -- see its own docstring for what
        gap it closes. Skipped when `_check_engine_floor_at_start` has
        already stopped this waiter (a confirmed below-floor engine):
        there is nothing to catch up against a substrate this waiter has
        already refused to trust."""
        self._loop = asyncio.get_running_loop()
        self._alive = True
        self._publish_status()
        try:
            await self._check_engine_floor_at_start()
            if not self._stopped:
                await self._catchup_mailbox_rows()
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
                if self._board_activity:
                    # bead nexus-zxthy: settle so a cluster of posts folds
                    # into the next wait instead of one notification per tick.
                    # The fast-tick floor's bookkeeping is reset here on
                    # purpose: this sleep paces the loop itself, and a tick
                    # that returned rows is a genuine wake, not a fast one.
                    self._board_activity = False
                    self._fast_tick_streak = 0
                    self._warned_fast_ticks = False
                    await asyncio.sleep(self.board_coalesce_s)
                    continue
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

    async def _check_engine_floor_at_start(self) -> None:
        """nexus-6konb.15: ask the engine's own `/version` identity, once,
        whether it is below the announce/subscriber floor -- BEFORE the
        first `tick()`, so a confirmed below-floor engine never reports
        `alive` at all rather than only after its first mail row arrives
        (`_engine_ignores_announce`/`_engine_ignores_subscriber`'s own
        row-based detection, unchanged and still the fallback here).

        `self.engine_version_probe` runs off the event loop
        (`asyncio.to_thread`) exactly like every other blocking call this
        waiter makes -- it is a real synchronous `httpx.get` in production
        (see `_probe_serving_engine_version`).

        A probe that cannot resolve a version -- unreachable, a
        non-nexus/non-200 response, or a blank/dev `release_version` (a
        dev-checkout jar reports none) -- returns `None` and changes
        NOTHING: this is "don't know", never "assume below floor". The
        loop starts normally and the row-based fallback stays the only
        detector for that case, exactly as it was before this check
        existed.

        `_probe_serving_engine_version` itself never raises (it fails
        closed to `None` internally), but `self.engine_version_probe` is
        an injectable callable -- a caller-supplied replacement, or a
        test double, could still raise. Reviewer fold: a raising probe
        must never crash the waiter's start any more than a failing one
        does, so it is treated identically to a `None` result -- logged,
        then the row-based fallback takes over."""
        try:
            version = await asyncio.to_thread(self.engine_version_probe)
        except Exception as exc:  # noqa: BLE001 — best-effort: a raising probe must never crash the waiter's start
            _log.warning(
                "channel_waiter_version_probe_raised", session_id=self.session_id, error=repr(exc),
            )
            return
        if version is None:
            return
        from nexus.engine_version import (  # noqa: PLC0415 — leaf module, rare/branch-local path
            CHANNEL_ANNOUNCE_MIN_ENGINE_VERSION,
            CHANNEL_SUBSCRIBER_MIN_ENGINE_VERSION,
        )
        if version < CHANNEL_ANNOUNCE_MIN_ENGINE_VERSION:
            self._stop_no_announce_support()
        elif version < CHANNEL_SUBSCRIBER_MIN_ENGINE_VERSION:
            self._stop_no_subscriber_support()

    async def _catchup_mailbox_rows(self) -> None:
        """Bead nexus-ymfak (nexus-rxuiq residual, DECISION: Sam
        2026-09-24): closes the gap the waiter-token fence (`Announce
        .waiter`, `_mint_waiter_token`) does not -- a row that arrives
        and gets announce-stamped by an ORPHANED predecessor waiter's
        still-parked `wait()` AFTER that predecessor's process has died
        but BEFORE this (successor) waiter's own first `tick()` ever
        calls `wait()` itself. The fence stops a superseded waiter's
        parked call from stamping a FUTURE row once the successor has
        admitted its own token; it does nothing for a row the orphan's
        call already claimed and stamped in the seconds before the
        successor existed at all, since there is no successor wait for
        the engine to prefer yet. That window is bounded by
        `wait_timeout_s` -- the longest a park predating this waiter's
        construction can still be live -- which is exactly what a
        `claude --resume` costs today: up to `wait_timeout_s` seconds of
        silence for a row the dead process's own parked call took and
        will never tell anyone about.

        One plain, non-blocking `rd` (never `wait` -- this is a read, not
        a park) per subscribed MAILBOX subspace, for rows `announced_at`
        within the last `wait_timeout_s + DEFAULT_CATCHUP_MARGIN_S`
        seconds: `rd` never stamps `announced_at`/`announce_count` (only
        an announce-mode `wait` does), so this cannot itself create a
        second orphan-shaped stamp. Every unclaimed, recently-stamped row
        found gets a reference through the SAME path a real tick's find
        would (`_reference_mailbox_row`) -- crediting `_announced_total`
        only on the row's own first send, recording `_last_seen`, no
        different from the tick that would eventually have rediscovered
        it on its own schedule. Boards are never touched (RDR-213 bead
        nexus-q82tk): a board's stamp is per `(subspace, subscriber)`,
        keyed on THIS session's id rather than a waiter token, so an
        orphan and its successor for the SAME session read the identical
        per-subscriber budget -- there is no orphan gap for a board post
        to fall into in the first place.

        A row the orphan ALREADY referenced before dying gets a harmless
        DUPLICATE reference here: this method has no way to know whether
        a notification already went out for it, and the notification
        text itself already covers a stale or already-claimed row (the
        model's `tuple_in` on it either claims cleanly or comes back
        empty, and either outcome is already explained). Cheaper to
        accept an occasional duplicate than to add state whose only job
        would be suppressing it.

        Failure-isolated per subspace: an `rd` failure (a transient HTTP
        or store error) is logged and this method moves on to the next
        mailbox, never raising past `run()`'s start -- exactly the same
        posture `tick()`'s own per-round-trip failures take, just before
        there is a loop to back off inside yet."""
        cutoff_s = self.wait_timeout_s + DEFAULT_CATCHUP_MARGIN_S
        now = datetime.now(UTC)
        for entry in self.subs.entries():
            subspace = entry["subspace"]
            if not subspace.startswith("mailbox/"):
                continue
            try:
                rows = await asyncio.to_thread(self._call, lambda t, sp=subspace: t.rd(sp, n=MAX_QUERY_RESULTS))
            except Exception as exc:  # noqa: BLE001 — failure-isolated: one bad mailbox must never block the others or the first tick
                _log.warning(
                    "channel_waiter_catchup_rd_failed",
                    session_id=self.session_id, subspace=subspace, error=repr(exc),
                )
                continue
            for row in rows:
                if row.claim_state is not None:
                    continue  # already claimed (or dead) -- not this method's job to re-surface it
                announced_at = _parse_announced_at(row.announced_at)
                if announced_at is None:
                    continue  # never announced, or an unparseable stamp -- "can't tell" is not "recent"
                age_s = (now - announced_at).total_seconds()
                # Symmetric: an engine clock far ahead of this host would
                # otherwise make every old row's age negative, "recent" forever.
                if abs(age_s) <= cutoff_s:
                    await self._reference_mailbox_row(subspace, row)

    def _call(self, fn: Callable[[Any], Any]) -> Any:
        """Run *fn* against a freshly opened tuples store, closing it
        before returning -- see the class docstring for why this never
        holds a store handle open across calls. Always run off the event
        loop (`asyncio.to_thread`): `HttpTupleStore` is a synchronous
        httpx client."""
        with self.store_factory() as db:
            return fn(db.tuples)

    async def tick(self) -> None:
        """One iteration. Drops the last-seen record of any mailbox no
        longer subscribed (unsubscribe), then parks ONE `wait()` call
        over EVERY subscription -- board or mailbox alike, the spec is
        never empty. There is no local re-send point to cap the timeout
        to any more (bead nexus-vsipz): a mailbox's own due timing now
        lives entirely in the engine's `announce` predicate, so this tick
        simply asks for up to `wait_timeout_s` and lets the engine decide
        when (or whether) anything is due before that. Exposed (not
        folded into :meth:`run`) so tests can drive iterations directly
        instead of a real timed loop.

        Bead nexus-n36sw: a board spec optimistically carries `since`
        (`_build_specs`, gated on `_board_since_supported`). An engine
        predating this bead refuses ANY `since` alongside `announce`
        (`SchemaViolationError`, wire code `SchemaViolation`, naming the
        `since` field) regardless of `subscriber` -- its refusal predates
        the per-subscriber carve-out this bead adds. The FIRST such
        refusal this waiter ever sees flips `_board_since_supported` to
        `False` for its whole remaining life and rebuilds+resends this
        SAME tick's specs without `since`, so this tick still delivers
        rather than waiting for the next one; every later tick's
        `_build_specs` call already omits it once the flag is flipped."""
        # One snapshot of the subscription list per tick (bead nexus-zxthy,
        # review): the subscribe/unsubscribe tools mutate the set from
        # worker threads, so the spec builder and the result processor
        # read the SAME list rather than two that may differ.
        entries = self.subs.entries()
        live_subspaces = {e["subspace"] for e in entries}
        for subspace in list(self._last_seen):
            if subspace not in live_subspaces:
                del self._last_seen[subspace]

        specs = self._build_specs(entries)
        try:
            results: list[WaitResult] = await asyncio.to_thread(
                self._call, lambda t: t.wait(specs, self.wait_timeout_s),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                # The route switch's default branch on an engine predating
                # `/wait` (`unknown tuples op: /wait`, a bare 404): no wait,
                # no waiter; the drain hook is the floor.
                self._stop_no_wait_support()
                return
            raise  # any other status is a transient fault: `run()` logs, backs off and ticks again
        except SchemaViolationError as exc:
            if not (self._board_since_supported and "since" in str(exc)):
                raise  # a schema violation this bead does not explain: a real request defect
            self._board_since_supported = False
            _log.warning(
                "channel_waiter_board_since_unsupported", session_id=self.session_id, error=str(exc),
            )
            specs = self._build_specs(entries)
            results = await asyncio.to_thread(
                self._call, lambda t: t.wait(specs, self.wait_timeout_s),
            )
        if any(result.superseded for result in results):
            self._stop_superseded()
            return
        if self._engine_ignores_announce(results):
            self._stop_no_announce_support()
            return
        if self._engine_ignores_subscriber(results):
            self._stop_no_subscriber_support()
            return
        await self._process_results(results, entries)
        self._last_wake = datetime.now(UTC)
        self._publish_status()

    def _stop_no_wait_support(self) -> None:
        self._stopped = True
        self._stopped_reason = "no_wait_support"
        _log.warning("channel_waiter_no_wait_support", session_id=self.session_id)

    def _stop_no_announce_support(self) -> None:
        self._stopped = True
        self._stopped_reason = "no_announce_support"
        _log.warning("channel_waiter_no_announce_support", session_id=self.session_id)

    @staticmethod
    def _engine_ignores_announce(results: list[WaitResult]) -> bool:
        """`True` the first time ANY row in *results* carries
        `announce_count=None` (bead nexus-vsipz): a real engine ALWAYS
        renders that field, on every tuple it returns, whether or not the
        spec that matched it carried `announce` at all (0 is the column
        default) -- so seeing `None` on a row returned FOR an announce-
        mode spec is proof the engine never even looked at that field,
        exactly the same class of evidence a 404 from `/wait` itself is
        for an engine predating `wait` entirely. Every spec carries
        `announce` now (bead nexus-q82tk moved boards onto it), so every
        row is checked. An engine that renders the field but predates
        the per-subscriber stamp (v0.1.128) is a different case, caught
        by :meth:`_engine_ignores_subscriber`."""
        for result in results:
            for row in result.tuples:
                if row.announce_count is None:
                    return True
        return False

    def _engine_ignores_subscriber(self, results: list[WaitResult]) -> bool:
        """`True` the first time a board result comes back without this
        session's id echoed as its `subscriber` (bead nexus-q82tk): an
        engine that honours `announce.subscriber` echoes it on the
        result, so a board result without it is proof the engine read
        `announce` but never the subscriber (v0.1.128) and stamped the
        board ROW instead -- which, at `max=1`, would silence the post
        for every other subscriber. A local install converges to the
        engine floor rather than refusing it at spawn
        (`nexus.engine_version`), so the window between a client upgrade
        and the engine's convergence is real, and this check is the
        guard for it: the waiter stops loud, the `nx doctor` row names
        the reason, and mailboxes keep working through the drain hook.
        Mailbox results are never checked: their spec carries no
        subscriber."""
        for result in results:
            if result.subspace.startswith("board/") and result.subscriber != self.session_id:
                return True
        return False

    def _stop_superseded(self) -> None:
        """Bead nexus-rxuiq: the engine answered that a newer waiter owns this
        session's subscriptions. In practice this waiter's task was already
        cancelled and nobody reads this; if two live waiters ever did share a
        session, the older one stops here rather than contending, and the
        drain hook stays the floor for it."""
        self._stopped = True
        self._stopped_reason = "superseded"
        _log.warning("channel_waiter_superseded", session_id=self.session_id)

    def _stop_no_subscriber_support(self) -> None:
        self._stopped = True
        self._stopped_reason = "no_subscriber_support"
        _log.warning("channel_waiter_no_subscriber_support", session_id=self.session_id)

    def _build_specs(self, entries: list[dict[str, Any]] | None = None) -> list[WaitSpec]:
        """Every subscription -- board or mailbox -- enters the spec
        every tick. The spec is NEVER empty. A board's spec (bead
        nexus-q82tk) carries `announce` with `subscriber` set to this
        session's id and `max=DEFAULT_BOARD_MAX_ANNOUNCES`, so the engine
        returns each post to this session once, from a per-subscriber
        stamp, with no client cursor to skip a late commit, and asks for
        up to `board_wait_rows` rows per wait (bead nexus-zxthy) that
        `_process_results` folds into one notification. A mailbox's
        spec (bead nexus-vsipz, RDR-213 engine half) asks for `n=1` and
        an `announce` field carrying this waiter's `reannounce_interval_s`/
        `max_announces` -- the engine, not this waiter, decides whether
        anything is due.

        Bead nexus-n36sw: a board spec ALSO carries `since` -- the topic's
        subscribe-time watermark (`entry["since"]`, `SubscriptionSet
        .entries`'s own ISO string), widened backwards by
        `board_start_skew_s` exactly as the pre-nexus-n36sw client-side
        compare was (this is a MOVE of that margin's application point, not
        a new one) -- gated on `_board_since_supported`: `True` (the
        default, and every tick after this bead's own engine has proven
        itself) sends it, so the engine excludes a backlog row from ever
        being selected or stamped; `False` (an engine that refused the
        combination on some earlier tick, `tick`'s own catch) omits it,
        restoring the pre-nexus-n36sw wire shape so an old engine still
        answers. Never sent when the topic has no recorded subscribe time
        (a `SubscriptionSet` restored from a pre-nexus-zxthy T1 record) --
        `since=None` (the field's own default) is `WaitSpec`'s unchanged
        "no watermark" case on both client and engine."""
        specs: list[WaitSpec] = []
        for entry in (self.subs.entries() if entries is None else entries):
            subspace = entry["subspace"]
            if subspace.startswith("board/"):
                since: tuple[str, str] | None = None
                if self._board_since_supported:
                    start = _parse_created_at(entry.get("since"))
                    if start is not None:
                        watermark = start - timedelta(seconds=self.board_start_skew_s)
                        # bead nexus-n36sw: the engine's per-subscriber since
                        # compare is `created_at` alone (no tuple id to pair
                        # it with at subscribe time) -- see
                        # `TupleRepository.queryOnceAnnounceSubscriber`'s own
                        # javadoc for why. The second element is a sentinel
                        # `_since_payload` renders as an omitted `id` key,
                        # never read by the engine's per-subscriber path.
                        since = (watermark.isoformat(), "")
                specs.append(WaitSpec(
                    subspace=subspace, n=self.board_wait_rows, since=since,
                    announce=Announce(
                        interval_s=int(self.reannounce_interval_s), max=DEFAULT_BOARD_MAX_ANNOUNCES,
                        subscriber=self.session_id, waiter=self.waiter_token,
                    ),
                ))
            else:
                specs.append(WaitSpec(
                    subspace=subspace, n=1,
                    announce=Announce(
                        interval_s=int(self.reannounce_interval_s), max=self.max_announces,
                        waiter=self.waiter_token,
                    ),
                ))
        return specs

    async def _process_results(
        self, results: list[WaitResult], entries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Board posts (bead nexus-q82tk): deliver each; the engine has
        already stamped the per-subscriber delivery row, so there is no
        cursor to advance and nothing to persist. Mailboxes (bead
        nexus-vsipz, RDR-213 engine half): each
        spec asks for `n=1`, so at most one row per mailbox per wake, and
        the engine has already decided it is claimable and due -- there
        is no dead-row skip here any more, because the engine's own
        claimable filter excludes a dead-lettered row before this waiter
        ever sees it. Every returned mailbox row is referenced, claimed
        or not (the notification text already covers an empty
        `tuple_in`).

        Bead nexus-n36sw: `since_by_topic` is now a FALLBACK, only live when
        `_board_since_supported` is `False` -- the engine already excluded
        anything at or before the watermark from ever being selected or
        stamped when it IS supported (the common case), so re-deriving and
        re-applying the same compare here would be redundant work finding
        nothing (every row `_deliver_board_rows` would drop is already
        absent from `results`). `None` for every topic in that case is
        `_deliver_board_rows`'s own existing "keep everything" branch --
        unchanged code, a plain no-op here rather than a special case."""
        since_by_topic: dict[str, datetime | None] = {}
        if self._board_since_supported:
            for e in (self.subs.entries() if entries is None else entries):
                since_by_topic[e["subspace"]] = None
        else:
            skew = timedelta(seconds=self.board_start_skew_s)
            for e in (self.subs.entries() if entries is None else entries):
                start = _parse_created_at(e.get("since"))
                since_by_topic[e["subspace"]] = (start - skew) if start is not None else None
        for result in results:
            if result.subspace.startswith("board/"):
                await self._deliver_board_rows(
                    result.subspace, result.tuples, since_by_topic.get(result.subspace),
                )
                continue
            for row in result.tuples:  # n=1 caps this to at most one row
                await self._reference_mailbox_row(result.subspace, row)

    async def _deliver_board_rows(
        self, subspace: str, rows: list[TupleRow], since: datetime | None,
    ) -> None:
        """Bead nexus-zxthy. Drop rows created before the topic's subscribe
        time (*since*, already widened by `board_start_skew_s`; a row
        whose `created_at` cannot be parsed is kept),
        then send ONE notification for what is left: the unchanged single-
        post shape for one row, the batch shape for more. The engine has
        already stamped every row here for this subscriber, dropped or
        not, so a dropped backlog row is never returned again; the start
        position only decides what is pushed, never what `tuple_rd` can
        read."""
        if not rows:
            return
        self._board_activity = True
        kept: list[TupleRow] = []
        for row in rows:
            created = _parse_created_at(row.created_at)
            if since is not None and created is not None and created < since:
                self._board_backlog_dropped += 1
                continue
            kept.append(row)
        if not kept:
            return
        if len(kept) == 1:
            row = kept[0]
            meta = {"subspace": subspace, "tuple_id": row.id}
            for key in ("from", "kind"):
                if row.dims.get(key):
                    meta[key] = row.dims[key]
            await self.sender(_board_notification_content(subspace, row.id), meta)
        else:
            first, last = kept[0], kept[-1]
            kinds: dict[str, int] = {}
            senders: set[str] = set()
            for row in kept:
                if row.dims.get("kind"):
                    kinds[row.dims["kind"]] = kinds.get(row.dims["kind"], 0) + 1
                if row.dims.get("from"):
                    senders.add(row.dims["from"])
            meta = {
                "subspace": subspace, "tuple_id": last.id, "first_tuple_id": first.id,
                "count": str(len(kept)),
                # every id this one notification covers, so a reader can
                # account for each post without a second read
                "tuple_ids": ",".join(row.id for row in kept),
            }
            if kinds:
                meta["kinds"] = ",".join(f"{k}={n}" for k, n in sorted(kinds.items()))
            if senders:
                meta["from"] = ",".join(sorted(senders))
            content = _board_batch_notification_content(
                subspace, len(kept), first.id, last.id, self._board_last_delivered.get(subspace),
            )
            await self.sender(content, meta)
        self._board_batches += 1
        last = kept[-1]
        if last.created_at:
            self._board_last_delivered[subspace] = (last.created_at, last.id)

    async def _reference_mailbox_row(self, subspace: str, row: TupleRow) -> None:
        """Send ONE reference for *row* (claimed or not -- the
        notification text already covers an empty `tuple_in`). The engine
        has already decided this row is due and stamped it (bead
        nexus-vsipz) -- this method's only jobs are rendering the
        notification, crediting `_announced_total` on the row's FIRST
        send (`announce_count == 1`, never on a re-send the engine chose
        to make), and recording the `status()` bookkeeping in
        `_last_seen`."""
        to_address = subspace.removeprefix("mailbox/")
        meta = {"subspace": subspace, "tuple_id": row.id}
        for key in ("from", "kind", "correlation_id"):
            if row.dims.get(key):
                meta[key] = row.dims[key]
        content = _mailbox_notification_content(subspace, row.id, to_address)
        await self.sender(content, meta)
        if row.announce_count == 1:
            self._announced_total += 1
        self._last_seen[subspace] = (row.announce_count or 0, time.monotonic())

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
