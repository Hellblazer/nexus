# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.11): the session MCP server's
per-session subscription set -- what the lifespan waiter
(:mod:`nexus.mcp.channel`) parks a single ``wait`` call on -- plus the
``directory/<name>`` name lease (RDR-208 Phase 3, bead nexus-galkv.20).

The set holds two kinds of DELIVERED entry (no cursor: since bead
nexus-vsipz a mailbox's position lives on the engine's row stamp, and since
bead nexus-q82tk a board's lives in the engine's per-subscriber delivery
row):

- ``mailbox/<session id>``, present from construction, never removable
  (it is the floor's own address).
- up to :data:`MAX_BOARD_TOPICS` ``board/<topic>`` subspaces.

A ``mailbox/<name>`` subscribe call is accepted for exactly one *name* per
session and starts the RDR-208 ``directory/<name>`` lease heartbeat (see
:func:`_directory_heartbeat`), so peers can resolve that name to this
session through ``mailbox_send`` -- but it is NOT a third delivered entry:
:meth:`SubscriptionSet.entries` never lists it, so neither the lifespan
waiter (:mod:`nexus.mcp.channel`) nor ``tuple_subscriptions`` ever treats it
as a mailbox to watch or report. Through RDR-208 Phase 2 this call ALSO
took over a per-session instance-registration file
(``<config>/tuple-watch/addresses.d/<session id>``) the drain hook read to
extend its own floor onto that name; Phase 3 (nexus-galkv.20) deleted that
file and its write, along with the entries()-listing that fed the waiter's
push delivery for it -- the retention window (R2 + 7 days) that transition
depended on has passed. A name registered under the old file format is
simply never read again; nothing migrates it, and nothing needs to.

Persisted in T1 scratch keyed by session id (see :func:`load`/:func:`persist`),
so a ``/resume`` (same session id, a fresh process) restores the list and a
``/clear`` (a new session id) starts clean -- T1 itself is already
session-scoped (``T1Database.list_entries`` filters on its own bound
``session_id``), so a fresh load under a different session id simply never
sees the old rows.

A mutation bumps :attr:`SubscriptionSet.version` and calls every
registered listener with ``self`` (:meth:`SubscriptionSet.add_listener`),
so the waiter can cancel its parked ``wait`` and re-issue it with the new
list (Approach item 6, Technical Design "Waiting").

**Lifted, not imported, from the former CLI mailbox-watch loop** (RDR-208
Phase 2 Step 1 / bead nexus-6konb.9): ``_directory_heartbeat``'s due/rotate/
nonce logic is the same shape as the original, adapted to log via structlog
instead of the CLI watcher's budgeted stdout emitter -- there is no Monitor
stream here to budget against. The original lived in the now-deleted CLI
watcher module until RDR-211 nexus-rplay.14 removed it along with the
watcher loop itself (RDR-211 Existing Infrastructure Audit).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2.http_tuple_store import SchemaViolationError

_log = structlog.get_logger(__name__)

# ── Lifted from the former CLI mailbox-watch module (byte-identical path/format) ─

_STATE_SUBDIR = "tuple-watch"

#: The mailbox address charset (RDR-211 fix round, bead nexus-rplay.18,
#: code review Minor 6): a `mailbox/<name>` instance name becomes a
#: `directory/<name>` lease key, so a name outside this charset -- a
#: newline, a slash, a leading `.`/`-`/`_` -- is refused rather than
#: sanitised.
_SAFE_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Matches ``directory.yaml``'s own ``retention_seconds`` -- a re-send can
#: never move a directory entry's expiry past its own ``created_at`` plus
#: this window, so a lease that outlives it mints a fresh nonce and writes
#: a brand-new entry before the old one's ceiling is reached (see
#: :func:`_directory_heartbeat`'s ``rotate`` check). Same constant nexus.
#: tuple_watch.DIRECTORY_RETENTION_S names, for the same reason: a property
#: of the ENGINE template, not something a caller should be able to move.
DIRECTORY_RETENTION_S: float = 604800.0

#: RDR-208 Phase 2 Step 1 defaults: a live holder writes its
#: ``directory/<name>`` entry with this TTL and re-sends at this cadence.
#: Overridable per call for tests only -- production callers never override.
DIRECTORY_TTL_S: float = 300.0
DIRECTORY_HEARTBEAT_S: float = 60.0

#: How often the background lease thread checks whether a re-send is due.
#: Independent of DIRECTORY_HEARTBEAT_S so a test can drive the interval
#: with a short poll without also shortening the TTL/heartbeat semantics
#: under test.
_LEASE_POLL_S: float = 5.0


@dataclass
class _DirectoryLease:
    """This subscription's own ``directory/<name>`` lease state, tracked
    across heartbeat calls. Purely in-process, mirroring the former CLI
    mailbox-watch module's own ``_DirectoryLease``: a fresh process (a
    `/resume`, or this session's next MCP server restart) re-arms from
    scratch."""

    armed: bool = False
    nonce: str = ""
    arm_time: float = 0.0
    last_send: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0


def _directory_nonce(t: float, pid: int) -> str:
    """Finer than one second and carries the pid, exactly mirroring the
    former CLI mailbox-watch module's own ``_directory_nonce`` -- two
    processes arming ``directory/<name>`` for the same session in the same
    second must not collide on one id."""
    return f"{t:.6f}-{pid}"


def _directory_heartbeat(
    store: Any,
    name: str,
    session_id: str,
    lease: _DirectoryLease,
    *,
    t: float,
    pid: int,
    ttl_s: float = DIRECTORY_TTL_S,
    heartbeat_s: float = DIRECTORY_HEARTBEAT_S,
) -> None:
    """One cycle's ``directory/<name>`` lease decision: arm, plain
    re-send, or re-nonce (RDR-208 Phase 2 Step 1). Mutates *lease* in
    place. Never raises: a write failure is logged and the caller's loop
    continues untouched, so the very next cycle retries.

    Adapted from the former CLI mailbox-watch module's own
    ``_directory_heartbeat`` -- same due/rotate/nonce decision, logged via
    structlog instead of that module's budgeted stdout emitter (this
    caller has no Monitor stream to budget against). *store* is an
    ``HttpTupleStore``-shaped object
    (anything exposing the same ``out`` signature).
    """
    due = not lease.armed or t - lease.last_send >= heartbeat_s
    if not due:
        return
    rotate = lease.armed and t >= lease.arm_time + DIRECTORY_RETENTION_S - ttl_s
    fresh = not lease.armed or rotate
    nonce = _directory_nonce(t, pid) if fresh else lease.nonce
    try:
        store.out(
            f"directory/{name}", {"name": name}, dims={"session_id": session_id},
            nonce=nonce, ttl_seconds=int(ttl_s),
        )
    except Exception as e:  # noqa: BLE001 — reported like the CLI watcher's own probe failure, never fatal
        text = f"{type(e).__name__}: {e}"
        _log.warning("subscriptions_directory_lease_failed", name=name, error=text)
        lease.last_error, lease.last_error_at = text, t
        return
    lease.last_error = ""
    lease.armed = True
    lease.nonce = nonce
    if fresh:
        lease.arm_time = t
    lease.last_send = t


def _release_directory_entry(
    store: Any, name: str, session_id: str, lease: _DirectoryLease,
) -> None:
    """Release this lease's live ``directory/<name>`` row on a DELIBERATE
    stop (an ``unsubscribe`` of the leased name, or ``shutdown`` on
    a session handoff): re-send the SAME nonce with ``ttl_seconds=1``, so
    the idempotent tuple id updates the live row's expiry and it lapses
    within about a second instead of at :data:`DIRECTORY_TTL_S` (RDR-208
    test plan; bead nexus-kdxyv restored it from the deleted CLI watcher's
    own ``_release_directory_entry``). A plain process exit never runs
    this and leaves the row for one TTL, as the RDR says. A no-op when
    the lease was never armed. Best-effort: a failure is logged and the
    row lapses by TTL, never raised into a handoff or a tool call."""
    if not lease.armed:
        return
    try:
        store.out(
            f"directory/{name}", {"name": name}, dims={"session_id": session_id},
            nonce=lease.nonce, ttl_seconds=1,
        )
    except Exception as e:  # noqa: BLE001 — best-effort release; the row lapses by TTL otherwise
        _log.warning("subscriptions_directory_release_failed", name=name, error=str(e))


def _lease_loop(
    stop: threading.Event,
    store_factory: Callable[[], Any],
    name: str,
    session_id: str,
    lease: _DirectoryLease,
    ttl_s: float,
    heartbeat_s: float,
    poll_s: float,
) -> None:
    """Background re-send loop for one leased name's directory
    lease. *store_factory* is called on every tick (never held across
    ticks) and must return a CONTEXT MANAGER yielding a T2Database-shaped
    object with a ``.tuples`` attribute -- ``_t2_ctx()``'s own contract --
    because a store built inside one MCP tool call's own ``with`` block is
    closed when that block exits and must never be reused from this
    thread afterward. Stops promptly on *stop*; a heartbeat failure is
    logged and the loop continues (matches :func:`_directory_heartbeat`'s
    own never-raise contract)."""
    while not stop.wait(poll_s):
        try:
            with store_factory() as db:
                _directory_heartbeat(
                    db.tuples, name, session_id, lease,
                    t=time.time(), pid=os.getpid(), ttl_s=ttl_s, heartbeat_s=heartbeat_s,
                )
        except Exception as e:  # noqa: BLE001 — background thread boundary; must never die silently OR crash the process
            _log.warning("subscriptions_lease_tick_failed", name=name, error=str(e))


# ── The subscription set (RDR-211 Technical Design "Subscriptions") ────────

#: "spike 3 tested three, and the bound guards the engine's per-call work,
#: not slots" (RDR-211 Technical Design).
MAX_BOARD_TOPICS = 32


def _take_enabled(templates: list[dict[str, Any]], subspace: str) -> bool:
    """Resolve *subspace* against the ``registry()`` wire's ``templates``
    list and return the matching template's ``take.enabled``.

    Deliberately a local copy of ``nexus.health._template_take_enabled``'s
    literal-before-pattern algorithm (mirrors ``TemplateRegistry.resolve()``:
    a literal template name is checked before any parameterised one, and a
    ``<param>`` segment matches anything in the corresponding position) --
    duplicated rather than imported so an MCP tool call never pulls in
    ``nexus.health``, a large module with its own import cost, for a
    fifteen-line pure function. Defaults to ``True`` (assume claimable) when
    nothing resolves, matching that function's own default: an unmatched
    subspace must never be silently treated as safe to subscribe.
    """
    segments = subspace.split("/")

    def _matches(name: str) -> bool:
        t_segments = name.split("/")
        if len(t_segments) != len(segments):
            return False
        return all(
            (ts.startswith("<") and ts.endswith(">")) or ts == ss
            for ts, ss in zip(t_segments, segments)
        )

    literal = [t for t in templates if "<" not in t.get("name", "")]
    patterned = [t for t in templates if "<" in t.get("name", "")]
    for t in literal:
        if t.get("name", "") == subspace:
            return bool(t.get("take", {}).get("enabled", True))
    for t in patterned:
        if _matches(t.get("name", "")):
            return bool(t.get("take", {}).get("enabled", True))
    return True


@dataclass
class SubscriptionSet:
    """One session's subscription list.

    ``session_mailbox`` (``mailbox/<session_id>``) is implicit and never
    stored in ``_board`` or removable; it is the only mailbox ever
    delivered. ``leased_name`` is at most one further name, armed through a
    ``mailbox/<name>`` subscribe call -- it starts the ``directory/<name>``
    lease so peers can resolve it via ``mailbox_send``, but (RDR-208 Phase
    3, bead nexus-galkv.20) it is never a delivered mailbox: it never
    appears in :meth:`entries`. ``_board`` is the subscribed ``board/<topic>``
    subspaces in subscription order (a dict used as an ordered set: the
    value is always ``None``; the per-topic cursor it once held moved to
    the engine, bead nexus-q82tk).
    """

    session_id: str
    #: The bare name currently leasing a `directory/<name>` row for this
    #: session, or None. NOT a mailbox subspace and NOT listed by
    #: :meth:`entries` -- see the class docstring.
    leased_name: str | None = None
    _board: dict[str, None] = field(default_factory=dict)
    version: int = 0
    _listeners: list[Callable[["SubscriptionSet"], None]] = field(default_factory=list, repr=False)
    _lease_thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _lease_stop: threading.Event | None = field(default=None, repr=False, compare=False)
    #: The live lease's state, name and store factory, kept so a deliberate
    #: stop can release the row it wrote (:func:`_release_directory_entry`).
    _lease: _DirectoryLease | None = field(default=None, repr=False, compare=False)
    _lease_name: str | None = field(default=None, repr=False, compare=False)
    _lease_store_factory: Callable[[], Any] | None = field(default=None, repr=False, compare=False)

    @property
    def session_mailbox(self) -> str:
        return f"mailbox/{self.session_id}"

    # ── Observer (Approach item 6, Technical Design "Waiting") ──────────────

    def add_listener(self, callback: Callable[["SubscriptionSet"], None]) -> None:
        """Register *callback* to be called with ``self`` after every
        mutation (subscribe/unsubscribe). The not-yet-built lifespan
        waiter (bead nexus-rplay.10) uses this to cancel its parked
        ``wait`` and re-issue it with the new list."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[["SubscriptionSet"], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _bump(self) -> None:
        self.version += 1
        for callback in list(self._listeners):
            callback(self)

    # ── Mutation ─────────────────────────────────────────────────────────

    def subscribe(
        self,
        subspace: str,
        *,
        templates: list[dict[str, Any]],
        store_factory: Callable[[], Any],
        state_dir: Path,
        directory_ttl_s: float = DIRECTORY_TTL_S,
        directory_heartbeat_s: float = DIRECTORY_HEARTBEAT_S,
        lease_poll_s: float = _LEASE_POLL_S,
    ) -> None:
        """Add *subspace* to this set.

        ``board/<topic>``: added, subject to :data:`MAX_BOARD_TOPICS`.
        ``mailbox/<name>``: accepted only as this session's own leased name
        (see :meth:`_arm_name_lease`) -- arms the ``directory/<name>``
        lease, never a delivered mailbox. Anything else that resolves to a
        take-enabled template (a queue or a lock) is refused naming ``in``,
        since those are never delivered; anything else is refused as
        neither a board topic nor the session's own leased name.

        *store_factory* is called (possibly more than once, possibly from
        a background thread later) to get a context manager yielding a
        T2Database-shaped object with a ``.tuples`` attribute -- see
        :func:`_lease_loop`'s docstring for why a raw store handle is
        never accepted directly. *state_dir* is currently unused (the
        per-session registration file it once fed was deleted at RDR-208
        Phase 3, bead nexus-galkv.20) and is kept only so the existing
        `mailbox/<name>` call shape does not change again.
        """
        if not subspace:
            raise SchemaViolationError("subspace must not be empty")
        if subspace.startswith("board/"):
            self._subscribe_board(subspace)
            return
        if subspace.startswith("mailbox/"):
            name = subspace[len("mailbox/"):]
            if not name:
                raise SchemaViolationError("mailbox subspace must include a name")
            if not _SAFE_INSTANCE_NAME.fullmatch(name):
                raise SchemaViolationError(
                    f"{name!r} is not a valid mailbox address name -- must match "
                    f"{_SAFE_INSTANCE_NAME.pattern!r}; refused before any write or lease"
                )
            self._arm_name_lease(
                name,
                store_factory=store_factory,
                directory_ttl_s=directory_ttl_s,
                directory_heartbeat_s=directory_heartbeat_s,
                lease_poll_s=lease_poll_s,
            )
            return
        if _take_enabled(templates, subspace):
            raise SchemaViolationError(
                f"{subspace!r} is a take-enabled subspace, worked with `in` and never "
                "delivered; tuple_subscribe accepts only board topics and the session's "
                "own leased name"
            )
        raise SchemaViolationError(
            f"{subspace!r} is not a board topic or the session's own leased name; refused"
        )

    def _subscribe_board(self, topic: str) -> None:
        if topic in self._board:
            return  # idempotent
        if len(self._board) >= MAX_BOARD_TOPICS:
            raise SchemaViolationError(
                f"at most {MAX_BOARD_TOPICS} board topics may be subscribed at once; "
                f"refused before adding {topic!r}"
            )
        self._board[topic] = None
        self._bump()

    def _arm_name_lease(
        self,
        name: str,
        *,
        store_factory: Callable[[], Any],
        directory_ttl_s: float,
        directory_heartbeat_s: float,
        lease_poll_s: float,
    ) -> None:
        """Arm *name*'s ``directory/<name>`` lease for this session. NEVER
        adds a mailbox entry: :attr:`leased_name` is bookkeeping only, read
        by :meth:`unsubscribe` and this method's own one-name-limit check,
        never by :meth:`entries` (RDR-208 Phase 3, bead nexus-galkv.20)."""
        if name == self.session_id:
            return  # already present from startup; not "a name"
        if self.leased_name is not None:
            if name == self.leased_name:
                return  # idempotent re-arm of the same name
            raise SchemaViolationError(
                f"this session already leases {self.leased_name!r}; only one "
                f"name is accepted, refusing {name!r}"
            )
        self._start_lease(name, store_factory, directory_ttl_s, directory_heartbeat_s, lease_poll_s)
        self.leased_name = name
        self._bump()

    def unsubscribe(self, subspace: str) -> None:
        """Remove *subspace*. The session's own mailbox can never be
        unsubscribed -- it is the floor's address. Unsubscribing a
        `mailbox/<name>` whose name this session leases releases the
        `directory/<name>` lease. Unsubscribing something not currently
        subscribed or leased is a silent no-op, not a refusal."""
        if subspace == self.session_mailbox:
            raise SchemaViolationError(
                "the session's own mailbox cannot be unsubscribed; it is the floor's address"
            )
        if self.leased_name and subspace == f"mailbox/{self.leased_name}":
            self._stop_lease()
            self.leased_name = None
            self._bump()
            return
        if subspace in self._board:
            del self._board[subspace]
            self._bump()

    def entries(self) -> list[dict[str, Any]]:
        """This set's DELIVERED subspaces, in the order
        ``tuple_subscriptions`` renders them: the session mailbox first,
        then board topics. No cursor: delivery position lives in the
        engine for every shape (beads nexus-vsipz, nexus-q82tk).

        Deliberately never includes :attr:`leased_name` (RDR-208 Phase 3,
        bead nexus-galkv.20): a leased name arms a `directory/<name>` lease
        for `mailbox_send` resolution, but it is not a mailbox this session
        watches or drains, so it is not an entry here either -- this is the
        one place :mod:`nexus.mcp.channel`'s waiter reads to decide what to
        wait on (:meth:`~nexus.mcp.channel.ChannelWaiter._build_specs`),
        so leaving it out here is what stops push delivery for it."""
        out: list[dict[str, Any]] = [{"subspace": self.session_mailbox}]
        for topic in self._board:
            out.append({"subspace": topic})
        return out

    # ── Lease thread lifecycle ───────────────────────────────────────────

    def _start_lease(
        self,
        name: str,
        store_factory: Callable[[], Any],
        ttl_s: float,
        heartbeat_s: float,
        poll_s: float,
    ) -> None:
        self._stop_lease()
        lease = _DirectoryLease()
        # Synchronous first arm: a caller observing right after subscribe()
        # (or right after a resume-load restarts this same lease) must see
        # the directory entry without racing the background thread's first
        # tick.
        with store_factory() as db:
            _directory_heartbeat(
                db.tuples, name, self.session_id, lease,
                t=time.time(), pid=os.getpid(), ttl_s=ttl_s, heartbeat_s=heartbeat_s,
            )
        stop = threading.Event()
        thread = threading.Thread(
            target=_lease_loop,
            args=(stop, store_factory, name, self.session_id, lease, ttl_s, heartbeat_s, poll_s),
            name=f"rdr211-directory-lease-{name}",
            daemon=True,
        )
        self._lease_stop, self._lease_thread = stop, thread
        self._lease, self._lease_name, self._lease_store_factory = lease, name, store_factory
        thread.start()

    def _stop_lease(self) -> None:
        """Stop the heartbeat thread, wait for an in-flight tick so it
        cannot re-send after the release, then release the row. Idempotent:
        a second call finds no lease and writes nothing."""
        stop, thread = self._lease_stop, self._lease_thread
        lease, name, factory = self._lease, self._lease_name, self._lease_store_factory
        self._lease_thread = self._lease_stop = None
        self._lease = self._lease_name = self._lease_store_factory = None
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=_LEASE_POLL_S + 5.0)
        if lease is None or name is None or factory is None:
            return
        try:
            with factory() as db:
                _release_directory_entry(db.tuples, name, self.session_id, lease)
        except Exception as e:  # noqa: BLE001 — best-effort release; see _release_directory_entry
            _log.warning("subscriptions_directory_release_failed", name=name, error=str(e))

    def shutdown(self) -> None:
        """Stop any live lease thread and release its directory row. Call
        before dropping a :class:`SubscriptionSet` (tests; the session
        handoff in ``nexus.mcp.core._t1_handoff_tick``)."""
        self._stop_lease()

    # ── Persistence (T1, keyed by session id) ───────────────────────────

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "leased_name": self.leased_name,
            "board": list(self._board),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SubscriptionSet":
        obj = cls(session_id=data["session_id"])
        # A record written before RDR-208 Phase 3 (nexus-galkv.20) carries
        # the retired "instance_mailbox"/"instance_name" keys instead --
        # absent here, so this simply reads None, which `load()` would
        # force anyway (the leased name is never restored on resume).
        obj.leased_name = data.get("leased_name")
        # A record written before bead nexus-q82tk carries a dict of
        # topic -> cursor; only the topics survive a `/resume` across that
        # change, which is exactly what the engine-side stamp makes
        # sufficient.
        board = data.get("board") or []
        topics = list(board.keys()) if isinstance(board, dict) else list(board)
        obj._board = dict.fromkeys(topics)
        return obj


# ── T1 persistence helpers ───────────────────────────────────────────────

#: Marks the one T1 entry per session that holds the serialized
#: subscription set. T1 has no upsert-by-title (unlike T2's memory_put),
#: so :func:`persist` deletes any prior tagged entry before writing a
#: fresh one -- at most one such row per session at any time.
_TAG = "rdr211-subscriptions"


def load(t1: Any, session_id: str, *, store_factory: Callable[[], Any] | None = None) -> "SubscriptionSet":
    """Build a FRESH :class:`SubscriptionSet` for *session_id* from
    whatever this session's T1 already holds (a ``/resume``), or an empty
    set with just the session mailbox (a ``/clear`` — a new session id has
    nothing to find, since T1 itself is already session-scoped).

    Board topics only. The leased name is NOT restored (bead nexus-kdxyv):
    the ``ListAgents`` name changes at every process start (RDR-208's
    identity table), so the name a resumed session held is stale by
    construction; RDR-211's own Subscriptions design says a ``/resume``
    under a new name repeats the ``tuple_subscribe`` call and the old
    name's mail strands, as RDR-208 accepted. Restoring the old name
    re-armed its directory lease for the life of the new process and made
    the new name's subscribe refuse as a second leased name. The old
    name's row lapses at its TTL from the old process's exit, as a plain
    exit leaves it. *store_factory* is accepted for the call shape
    :func:`get_or_load` passes and is unused: nothing restored here holds
    a lease.
    """
    for entry in t1.list_entries():
        tags = (entry.get("tags") or "").split(",")
        if _TAG not in tags:
            continue
        try:
            data = json.loads(entry["content"])
        except (TypeError, ValueError, KeyError):
            continue
        if data.get("session_id") != session_id:
            continue
        obj = SubscriptionSet.from_json(data)
        obj.leased_name = None  # see the docstring: the name is stale on resume
        return obj
    return SubscriptionSet(session_id=session_id)


def persist(t1: Any, subs: "SubscriptionSet") -> None:
    """Write *subs*'s current state back to T1, replacing any prior entry
    for its session (delete-then-put — T1 has no upsert-by-title)."""
    for entry in t1.list_entries():
        tags = (entry.get("tags") or "").split(",")
        if _TAG in tags:
            t1.delete(entry["id"])
    t1.put(content=json.dumps(subs.to_json()), tags=_TAG)


#: Process-lifetime cache, keyed by session id, so repeated MCP tool calls
#: in the SAME process reuse the SAME SubscriptionSet (and its live lease
#: thread) instead of restarting a lease on every call -- :func:`load`
#: alone, called fresh every time, would leak one lease thread per call.
_CACHE: dict[str, SubscriptionSet] = {}
_CACHE_LOCK = threading.Lock()


def get_or_load(t1: Any, session_id: str, *, store_factory: Callable[[], Any]) -> SubscriptionSet:
    """The cached :class:`SubscriptionSet` for *session_id* in THIS
    process, loading (and, if a lease was already active, restarting it)
    on first use."""
    with _CACHE_LOCK:
        obj = _CACHE.get(session_id)
        if obj is None:
            obj = load(t1, session_id, store_factory=store_factory)
            _CACHE[session_id] = obj
        return obj


def reset_cache(session_id: str | None = None) -> None:
    """Test/teardown hook: drop the process cache, stopping any live
    lease thread(s) first. *session_id* drops just that entry; omitted
    drops everything."""
    with _CACHE_LOCK:
        if session_id is None:
            targets = list(_CACHE.values())
            _CACHE.clear()
        else:
            obj = _CACHE.pop(session_id, None)
            targets = [obj] if obj is not None else []
    for obj in targets:
        obj.shutdown()
