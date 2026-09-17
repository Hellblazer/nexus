# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.10): the ``claude/channel``
capability declaration and the nexus MCP server's lifespan waiter --
Gap 5, "push delivery to the session".

Two independent halves live here:

- :func:`run_stdio_with_channel` replaces ``FastMCP.run_stdio_async`` at
  the server's one call site (:func:`nexus.mcp.core.main`): it declares
  ``capabilities.experimental["claude/channel"] = {}`` at initialize
  (which is what makes Claude Code register a listener -- Phase 1 Step 0,
  T2 ``nexus_rdr/211-spike-4-channel-2026-09-17``), and stashes the raw
  stdio write stream on this module (:data:`_write_stream`) so a sender
  never needs a handle on the ``ServerSession`` FastMCP builds internally.
  This is design choice (b) from the bead's brief, not (a): nothing here
  subclasses ``mcp.server.lowlevel.Server`` -- the driver already calls
  ``Server.run`` itself, so it already has the write stream in scope
  before that call is made, with no need to reach back INTO a session
  object built inside it.
- :class:`ChannelWaiter` is the lifespan's one background task: it gates
  claiming on :func:`detect_channel_argv` or a probe round-trip
  (T2 ``nexus_rdr/211-decision-waiter-gate-2026-09-17``, replacing the
  RDR's original handshake-capability guard, which Phase 1 Step 0 found
  had nothing to read -- Claude Code's declared experimental capabilities
  are empty whether or not the channel flag was used), then loops
  ``HttpTupleStore.wait`` over the session's :class:`~nexus.mcp.
  subscriptions.SubscriptionSet`, delivering board posts on cursor and
  mail under pure back pressure (one live claim at a time, held, renewed
  and re-notified until the session's own ``tuple_ack``/``tuple_nack``
  supplies the credit for the next -- Sam, T2 ``nexus_rdr/211-decision-
  channel-delivery-2026-09-16`` item 6). Every notification's ``content``
  is a FIXED template built only from server-controlled identifiers
  (subspace, tuple id, and for mail the claim id and claimant) -- never
  the tuple's own body, ``from``, ``kind``, or ``correlation_id`` (Sam, T2
  ``nexus_rdr/211-decision-push-reference-2026-09-17``): the channel is a
  push-to-ATTEND signal, not a delivery transport, and the session reads
  the actual content back itself with ``tuple_rd`` once notified. It also
  publishes its :meth:`ChannelWaiter.status` to a per-session on-disk
  record (:func:`write_channel_status`) at every wake/renew/release,
  since the `nx doctor` row (bead nexus-rplay.13) runs in the separate
  CLI process and has no other way to see this process's live state --
  that same record is also what a restarted waiter for the SAME session
  reads back at start, BEFORE any normal claim, to renew and re-adopt an
  outstanding mailbox claim its crashed predecessor left live (RDR-211
  review, Significant 1): a crash-and-restart is otherwise
  indistinguishable, IN THIS PROCESS's memory, from never having claimed
  anything at all, which is exactly what let a fresh `_maybe_claim_mail`
  call claim a second message from a different mailbox while the first
  was still live.

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
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from nexus.db.t2.http_tuple_store import ClaimNotFoundError, ClaimOwnershipError
from nexus.db.t2.records import TupleRow, WaitResult, WaitSpec

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from nexus.mcp.subscriptions import SubscriptionSet

_log = structlog.get_logger(__name__)

#: Declared at initialize (RDR-211 Approach item 7). An empty object --
#: Claude Code's channel preview reads the KEY's presence, not any value
#: inside it (Phase 1 Step 0 spike).
CHANNEL_CAPABILITY: dict[str, dict[str, Any]] = {"claude/channel": {}}

#: The notification method the spike proved works (T2
#: ``nexus_rdr/211-spike-4-channel-2026-09-17``): the SDK's typed
#: notification union has no member for it, so it is sent as a raw
#: ``JSONRPCNotification``.
_CHANNEL_METHOD = "notifications/claude/channel"

#: Production constants (RDR-211 Technical Design "Delivery"). Tests
#: inject short overrides through :class:`ChannelWaiter`'s constructor so
#: the suite never actually waits 150s/300s/25s.
DEFAULT_LEASE_S = 300
DEFAULT_RENEW_INTERVAL_S = 150.0
DEFAULT_WAIT_TIMEOUT_S = 25
DEFAULT_MAX_RESENDS = 5

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
#: the ``async with stdio_server()`` block) so a sender never mistakes a
#: torn-down connection for a live one.
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


# ── The gate: parent argv, with a probe fallback ────────────────────────────
#
# T2 nexus_rdr/211-decision-waiter-gate-2026-09-17: Phase 1 Step 0 found
# Claude Code's declared experimental capabilities empty whether or not
# the channel launch flag was used, so the RDR's original "claim only
# when the handshake carried the channel" guard has nothing to read. Sam's
# replacement: read the parent `claude` process's own command line first
# (every nx-mcp's parent IS the claude process that spawned it, measured
# on the live box); when that shows neither flag, send one probe
# notification asking the session to call `tuple_channel_probe()` and
# claim only once that call arrives. No call, no claim, ever, in that
# process -- the drain hook is the floor either way.

_CHANNEL_ARGV_FLAGS: tuple[str, ...] = (
    "--channels server:nexus",
    "--dangerously-load-development-channels server:nexus",
    # The plugin form (bead nexus-tk2cz, measured 2026-09-17): a plugin on
    # the effective channel allowlist (Anthropic's, or `allowedChannelPlugins`
    # in managed settings) loads with no dialog as
    # `--channels plugin:conexus@<marketplace>`; the marketplace segment is
    # not fixed, so the match stops at the `@`.
    "--channels plugin:conexus@",
    "--dangerously-load-development-channels plugin:conexus@",
)

#: Seconds `run()` waits before the FIRST probe notification. Claude Code
#: registers channel delivery for a server shortly AFTER the connection is
#: up (0.5 s measured 2026-09-17, bead nexus-tk2cz); a probe sent at
#: lifespan start raced that registration and was dropped, and a dropped
#: probe meant no claim ever in that process.
DEFAULT_PROBE_DELAY_S: float = 5.0
#: Seconds `run()` waits for the probe's answer before sending the probe a
#: SECOND (and last) time. Two probes total, not one: the RDR-211 design's
#: "one probe" assumed the first one always reached the session.
DEFAULT_PROBE_RESEND_AFTER_S: float = 60.0
#: Seconds `run()` sleeps after a tick fails for a reason other than
#: "engine without wait" (a transient HTTP or store error) before the next
#: tick. The loop never dies on one bad round-trip.
DEFAULT_TICK_ERROR_BACKOFF_S: float = 5.0

_PROBE_CONTENT = (
    "The nexus MCP server's channel waiter is checking whether this session "
    "was launched with the Claude Code channel enabled for `server:nexus`. "
    "Please call the `tuple_channel_probe` tool once to confirm."
)


def _read_parent_command(pid: int) -> str:
    """Best-effort: the running process's full command line, or `""` on
    any failure (an unreadable /proc entry, no `ps` binary, a timeout).
    Never raises -- this is a liveness probe, not a precondition."""
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        return result.stdout or ""
    except Exception:  # noqa: BLE001 — best-effort probe; a failure here means "argv unreadable", not "channel absent"
        return ""


def detect_channel_argv(
    ppid: int | None = None, *, argv_reader: Callable[[int], str] = _read_parent_command,
) -> bool:
    """`True` iff the parent process's command line names the channel
    launch flag for `server:nexus`. `ppid` defaults to `os.getppid()`;
    `argv_reader` is the seam tests fake (T2
    ``nexus_rdr/211-spike-4-channel-2026-09-17`` addendum: every nx-mcp's
    parent pid IS the `claude` process, and `ps -o command=` reads its
    argv)."""
    import os  # noqa: PLC0415 — stdlib, branch-local

    pid = ppid if ppid is not None else os.getppid()
    argv = argv_reader(pid)
    return any(flag in argv for flag in _CHANNEL_ARGV_FLAGS)


# ── The waiter ───────────────────────────────────────────────────────────


@dataclass
class _Outstanding:
    """The one live mailbox claim this waiter may hold at a time (pure
    back pressure, RDR-211 decision item 6).

    ``claimed_at`` (ISO-8601, set once at the original claim and carried
    forward on adoption -- see :meth:`ChannelWaiter._adopt_persisted_
    outstanding`) is persisted alongside ``claim_id``/``subspace``/
    ``tuple_id``/``resend_count`` in the on-disk status record
    (:meth:`ChannelWaiter.status`'s ``outstanding`` key) so a crashed
    and restarted waiter for the SAME session can renew and re-adopt
    this exact claim instead of leaving it live and untracked while
    claiming a second one elsewhere (RDR-211 review, Significant 1)."""

    subspace: str
    tuple_id: str
    claim_id: str
    claimant: str
    content: str
    meta: dict[str, str]
    next_renew_at: float
    resend_count: int = 0
    claimed_at: str = ""


#: Sam's decision, T2 ``nexus_rdr/211-decision-push-reference-2026-09-17``:
#: the notification `content` a mailbox claim or board post sends is a
#: FIXED template built only from server-controlled identifiers -- never
#: the tuple's own body, `from`, `kind`, or `correlation_id` (those stay in
#: `meta`, unchanged). The channel is a push-to-attend signal, not a
#: delivery transport: the session reads the actual content back itself
#: with `tuple_rd` once notified. A resend at renew re-sends the exact
#: same string (`_Outstanding.content` is built once, at claim or
#: adoption time, and never rebuilt from the row again).
def _mailbox_notification_content(subspace: str, tuple_id: str, claim_id: str, claimant: str) -> str:
    return (
        f"nexus mailbox message: subspace {subspace}, tuple {tuple_id}, claim {claim_id} "
        f"held by {claimant}. Read it with tuple_rd on that subspace, then tuple_ack "
        "(with a reply for a request), tuple_nack, or tuple_release with the claim id."
    )


def _board_notification_content(subspace: str, tuple_id: str) -> str:
    return (
        f"nexus board post: subspace {subspace}, tuple {tuple_id}. Read new posts with "
        "tuple_rd on that subspace from your cursor (tuple_subscriptions shows it)."
    )


class ChannelWaiter:
    """One session's lifespan waiter: gates on the channel, then loops
    ``HttpTupleStore.wait`` over its :class:`~nexus.mcp.subscriptions.
    SubscriptionSet`, delivering board posts on cursor and mail under
    pure back pressure.

    Constants (`lease_s`, `renew_interval_s`, `wait_timeout_s`,
    `max_resends`) default to the production values (RDR-211 Technical
    Design "Delivery": 300/150/25/5) and are overridden only by tests, so
    the suite never actually waits real minutes.

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
        channel_live: bool,
        sender: Callable[[str, dict[str, str]], Awaitable[bool]] = send_channel_notification,
        persist: Callable[[], None] = lambda: None,
        state_dir: Path | None = None,
        lease_s: int = DEFAULT_LEASE_S,
        renew_interval_s: float = DEFAULT_RENEW_INTERVAL_S,
        wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
        max_resends: int = DEFAULT_MAX_RESENDS,
        probe_delay_s: float = DEFAULT_PROBE_DELAY_S,
        probe_resend_after_s: float = DEFAULT_PROBE_RESEND_AFTER_S,
        tick_error_backoff_s: float = DEFAULT_TICK_ERROR_BACKOFF_S,
    ) -> None:
        self.session_id = session_id
        self.store_factory = store_factory
        self.subs = subs
        self.claimant = f"waiter:{session_id}"
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
        self.lease_s = lease_s
        self.renew_interval_s = renew_interval_s
        self.wait_timeout_s = wait_timeout_s
        self.max_resends = max_resends
        self.probe_delay_s = probe_delay_s
        self.probe_resend_after_s = probe_resend_after_s
        self.tick_error_backoff_s = tick_error_backoff_s

        self._proof = "argv" if channel_live else "none"
        self.channel_live = asyncio.Event()
        if channel_live:
            self.channel_live.set()

        self._outstanding: _Outstanding | None = None
        self._alive = False
        self._stopped = False
        self._last_wake: datetime | None = None
        self._released_count = 0
        self._probes_sent = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

        subs.add_listener(self._on_subscription_change)

    # ── credit hooks (called from tuple_ack/tuple_nack; may be a worker thread) ──

    def note_credit(self, claim_id: str) -> None:
        """The credit for the next mailbox claim: called by `tuple_ack`/
        `tuple_nack` when the session consumes *claim_id* (RDR-211
        decision item 6 -- "the session's ack or nack is the credit for
        the next"). Thread-safe: FastMCP tool functions run in a worker
        thread, never on this waiter's own event loop."""
        self._call_soon(self._clear_outstanding, claim_id)

    def _clear_outstanding(self, claim_id: str) -> None:
        if self._outstanding is not None and self._outstanding.claim_id == claim_id:
            self._outstanding = None
            self._publish_status()

    def note_probe_ack(self) -> None:
        """`tuple_channel_probe()` calls this: the gate's probe fallback
        proved live. Thread-safe, same reasoning as :meth:`note_credit`."""
        self._call_soon(self._mark_probed)

    def _mark_probed(self) -> None:
        self._proof = "probe"
        self.channel_live.set()
        self._publish_status()

    def _call_soon(self, fn: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(fn, *args)
        else:
            fn(*args)

    def _on_subscription_change(self, _subs: "SubscriptionSet") -> None:
        """RDR-211 Technical Design "Waiting": "A subscription change
        cancels the parked wait ... and re-issues it with the new list".
        This waiter takes the RDR's explicitly-offered alternative --
        "let the engine's 25s cap bound it" -- rather than cancelling an
        in-flight synchronous httpx call from another thread: every
        `tick()` already rebuilds its `WaitSpec`s fresh from
        `subs.entries()`, so the new list is live at the very next tick,
        at most one `wait_timeout_s` later. This callback's only job is
        the log line the RDR asks to "record what you did"."""
        _log.info("channel_waiter_subscription_changed", session_id=self.session_id)

    # ── status (the doctor-row bead, .13, is the intended reader) ───────────

    def status(self) -> dict[str, Any]:
        """`proof`: `"argv"`, `"probe"`, or `"none"` (never proved live).
        `alive`: this waiter's task is running (never proved past
        `_stop_no_wait_support` or a real cancellation). `last_wake`:
        ISO-8601 timestamp of the last completed `wait()` round-trip, or
        `None` before the first one. `unacked`: 1 while a mailbox claim
        is outstanding, else 0 (pure back pressure caps this at one).
        `released`: the cumulative count of claims released after
        exhausting `max_resends`. `outstanding`: `None`, or
        `{claim_id, subspace, tuple_id, resends, claimed_at}` for the one
        live mailbox claim this waiter holds -- the record
        :meth:`_adopt_persisted_outstanding` reads back at the next
        waiter start for THIS session (RDR-211 review, Significant 1)."""
        outstanding: dict[str, Any] | None = None
        if self._outstanding is not None:
            o = self._outstanding
            outstanding = {
                "claim_id": o.claim_id, "subspace": o.subspace, "tuple_id": o.tuple_id,
                "resends": o.resend_count, "claimed_at": o.claimed_at,
            }
        return {
            "proof": self._proof,
            "alive": self._alive,
            "last_wake": self._last_wake.isoformat() if self._last_wake else None,
            "unacked": 1 if self._outstanding is not None else 0,
            "released": self._released_count,
            "outstanding": outstanding,
        }

    def _publish_status(self) -> None:
        """Best-effort refresh of the on-disk record (bead nexus-rplay.13)
        -- a no-op when this waiter was constructed with no `state_dir`.
        Called at every wake (:meth:`tick`'s end), every renew and release
        (:meth:`_renew_or_release`'s exit points), every proof change
        (:meth:`_mark_probed`), and this loop's own start/stop, so a
        cross-process reader never sees a record older than the waiter's
        current state by more than one in-flight operation."""
        if self.state_dir is not None:
            write_channel_status(self.state_dir, self.session_id, self.status())

    # ── the loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._alive = True
        await self._adopt_persisted_outstanding()
        self._publish_status()
        try:
            if not self.channel_live.is_set():
                await self._probe_until_live()
            while not self._stopped:
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
        finally:
            self._alive = False
            self._publish_status()

    async def _probe_until_live(self) -> None:
        """Send the probe after :attr:`probe_delay_s` (Claude Code registers
        channel delivery shortly after the connection is up, so an immediate
        probe is dropped), wait :attr:`probe_resend_after_s` for the
        session's `tuple_channel_probe` call, send the probe once more if it
        has not come, then wait for as long as the process lives. Two probes
        total; no call, no claim, ever, in this process."""
        await asyncio.sleep(self.probe_delay_s)
        await self._send_probe()
        try:
            await asyncio.wait_for(self.channel_live.wait(), timeout=self.probe_resend_after_s)
            return
        except TimeoutError:
            pass
        await self._send_probe()
        await self.channel_live.wait()

    async def _send_probe(self) -> None:
        if self._probes_sent >= 2:
            return
        self._probes_sent += 1
        await self.sender(_PROBE_CONTENT, {"kind": "channel_probe"})

    async def _adopt_persisted_outstanding(self) -> None:
        """At waiter start, BEFORE any normal claim: read back this
        session's last-persisted outstanding claim (if any) and try to
        renew it (RDR-211 review, Significant 1).

        A crashed process's live claim would otherwise sit untracked in
        memory while a FRESH `_maybe_claim_mail` call -- gated only by
        `self._outstanding is None` IN THIS PROCESS -- claims a second
        message from a different mailbox, exceeding the one-live-claim
        invariant across the crash: the existing same-claimant retake
        only protects a re-claim within THAT claim's own subspace, never
        a different one.

        `renew` succeeding means the claim is still live: adopt it
        (restoring the resend count and the original `claimed_at`) and
        re-send its notification once, so the session sees it again
        post-restart. `ClaimNotFoundError` means it already lapsed (a
        successor already reclaimed it, or the sweep did) -- nothing to
        adopt, and the stale record is left for the next `_publish_status`
        to overwrite.
        """
        if self.state_dir is None:
            return
        status = read_channel_status(self.state_dir, self.session_id)
        persisted = (status or {}).get("outstanding")
        if not persisted:
            return
        claim_id = persisted.get("claim_id")
        subspace = persisted.get("subspace")
        tuple_id = persisted.get("tuple_id")
        if not claim_id or not subspace or not tuple_id:
            return
        try:
            await asyncio.to_thread(self._call, lambda t: t.renew(claim_id, self.claimant, self.lease_s))
        except ClaimNotFoundError:
            return
        content = _mailbox_notification_content(subspace, tuple_id, claim_id, self.claimant)
        meta = {"subspace": subspace, "tuple_id": tuple_id, "claim_id": claim_id, "claimant": self.claimant}
        self._outstanding = _Outstanding(
            subspace=subspace, tuple_id=tuple_id, claim_id=claim_id, claimant=self.claimant,
            content=content, meta=meta,
            next_renew_at=time.monotonic() + self.renew_interval_s,
            resend_count=int(persisted.get("resends", 0) or 0),
            claimed_at=persisted.get("claimed_at") or datetime.now(UTC).isoformat(),
        )
        await self.sender(self._outstanding.content, self._outstanding.meta)
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
        """One iteration: renew/release a due outstanding claim, park on
        `wait()` over the current subscription list, then deliver
        whatever it returned. Exposed (not folded into :meth:`run`) so
        tests can drive iterations directly instead of a real timed
        loop."""
        now = time.monotonic()
        if self._outstanding is not None and now >= self._outstanding.next_renew_at:
            await self._renew_or_release(now)

        specs = self._build_specs()
        timeout_s = self.wait_timeout_s
        if self._outstanding is not None:
            remaining = self._outstanding.next_renew_at - time.monotonic()
            timeout_s = max(0, min(self.wait_timeout_s, int(remaining)))
        if not specs:
            # Mailbox-only subscriptions with a claim outstanding: every
            # mailbox is held out of the wait by back pressure and there is
            # no board to park on. The engine refuses an empty `wait`
            # (SchemaViolation, "must name at least one subspace"), and
            # before bead nexus-tk2cz that refusal ended the loop for good
            # after the FIRST mailbox delivery of any session without a
            # board topic. Sleep until the renew is due instead.
            await asyncio.sleep(max(1, timeout_s))
            self._publish_status()
            return
        try:
            results: list[WaitResult] = await asyncio.to_thread(self._call, lambda t: t.wait(specs, timeout_s))
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                # The route switch's default branch on an engine predating
                # `/wait` (`unknown tuples op: /wait`, a bare 404): no wait,
                # no waiter; the drain hook is the floor.
                self._stop_no_wait_support()
                return
            raise  # any other status is a transient fault: `run()` logs, backs off and ticks again
        self._last_wake = datetime.now(UTC)
        await self._process_results(results)
        if self._outstanding is None:
            await self._maybe_claim_mail()
        self._publish_status()

    def _stop_no_wait_support(self) -> None:
        self._stopped = True
        _log.warning("channel_waiter_no_wait_support", session_id=self.session_id)

    def _build_specs(self) -> list[WaitSpec]:
        specs: list[WaitSpec] = []
        for entry in self.subs.entries():
            subspace = entry["subspace"]
            if subspace.startswith("board/"):
                cursor = entry.get("cursor")
                since = (cursor["created_at"], cursor["id"]) if cursor else None
                specs.append(WaitSpec(subspace=subspace, since=since))
            elif self._outstanding is None:
                # Pure back pressure: a mailbox is left OUT of the wait
                # entirely while a claim is outstanding, since claiming
                # more mail ahead of the session's own ack/nack is
                # exactly what this design refuses to do.
                specs.append(WaitSpec(subspace=subspace, n=1))
        return specs

    async def _process_results(self, results: list[WaitResult]) -> None:
        advanced = False
        for result in results:
            if not result.subspace.startswith("board/"):
                continue
            for row in result.tuples:
                await self._deliver_board_post(result.subspace, row)
            if result.tuples:
                last = result.tuples[-1]
                self.subs.advance_cursor(result.subspace, (last.created_at or "", last.id))
                advanced = True
        if advanced:
            # Code review Significant 3: `self.persist()` (a T1 write-back,
            # a synchronous HTTP call) must never run directly on the event
            # loop -- every other store call in this class already goes
            # through `asyncio.to_thread` for exactly this reason.
            await asyncio.to_thread(self.persist)

    async def _deliver_board_post(self, subspace: str, row: TupleRow) -> None:
        meta = {"subspace": subspace, "tuple_id": row.id}
        for key in ("from", "kind"):
            if row.dims.get(key):
                meta[key] = row.dims[key]
        await self.sender(_board_notification_content(subspace, row.id), meta)

    async def _maybe_claim_mail(self) -> None:
        if self._outstanding is not None:
            # "Never claim while an adopted or live outstanding exists"
            # (RDR-211 review, Significant 1) -- enforced here too, not
            # only by `tick()`'s `if self._outstanding is None` gate, so
            # the invariant holds for any direct caller (tests included).
            return
        mailbox_subspaces = [e["subspace"] for e in self.subs.entries() if not e["subspace"].startswith("board/")]
        candidates: list[tuple[str, str, str]] = []
        for subspace in mailbox_subspaces:
            rows = await asyncio.to_thread(self._call, lambda t, s=subspace: t.rd(s, {}, n=1))
            if rows:
                candidates.append((rows[0].created_at or "", rows[0].id, subspace))
        if not candidates:
            return
        candidates.sort()
        _, _, subspace = candidates[0]
        # `in`/`inp` require every pinned key (unlike `rd`'s subset
        # matching above) -- the mailbox template pins `to`, whose value
        # is exactly the subspace's own address segment.
        to_address = subspace.removeprefix("mailbox/")
        result = await asyncio.to_thread(
            self._call,
            lambda t, s=subspace, a=to_address: t.in_(s, {"to": a}, claimant=self.claimant, lease_s=self.lease_s),
        )
        if result is None:
            return  # lost the race (the drain hook, or another waiter restart) -- fine, nothing to deliver
        row, claim_id = result
        meta = {"subspace": subspace, "tuple_id": row.id, "claim_id": claim_id, "claimant": self.claimant}
        for key in ("from", "kind", "correlation_id"):
            if row.dims.get(key):
                meta[key] = row.dims[key]
        self._outstanding = _Outstanding(
            subspace=subspace, tuple_id=row.id, claim_id=claim_id, claimant=self.claimant,
            content=_mailbox_notification_content(subspace, row.id, claim_id, self.claimant), meta=meta,
            next_renew_at=time.monotonic() + self.renew_interval_s,
            claimed_at=datetime.now(UTC).isoformat(),
        )
        await self.sender(self._outstanding.content, self._outstanding.meta)

    async def _renew_or_release(self, now: float) -> None:
        outstanding = self._outstanding
        if outstanding is None:  # pragma: no cover — guarded by the caller
            return
        if outstanding.resend_count >= self.max_resends:
            try:
                await asyncio.to_thread(self._call, lambda t: t.release(outstanding.claim_id, outstanding.claimant))
            except (ClaimNotFoundError, ClaimOwnershipError) as exc:
                _log.warning("channel_waiter_release_failed", claim_id=outstanding.claim_id, error=str(exc))
            self._released_count += 1
            self._outstanding = None
            self._publish_status()
            return
        try:
            await asyncio.to_thread(
                self._call, lambda t: t.renew(outstanding.claim_id, outstanding.claimant, self.lease_s),
            )
        except ClaimNotFoundError:
            # Lapsed already -- a successor's `in_` (or the sweep) already
            # reclaimed it; nothing left here to renew or re-notify.
            self._outstanding = None
            self._publish_status()
            return
        outstanding.resend_count += 1
        outstanding.next_renew_at = now + self.renew_interval_s
        await self.sender(outstanding.content, outstanding.meta)
        self._publish_status()

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


def note_credit(session_id: str, claim_id: str) -> None:
    """Called by `tuple_ack`/`tuple_nack` after a successful engine call
    (RDR-211 decision item 6). A no-op when this session has no active
    waiter (the channel is off, or this process pre-dates the waiter) --
    the credit hook only matters to a waiter that is holding a claim."""
    waiter = _ACTIVE.get(session_id)
    if waiter is not None:
        waiter.note_credit(claim_id)


def note_probe_ack(session_id: str) -> None:
    """Called by the `tuple_channel_probe` MCP tool. A no-op when this
    session has no active waiter."""
    waiter = _ACTIVE.get(session_id)
    if waiter is not None:
        waiter.note_probe_ack()
