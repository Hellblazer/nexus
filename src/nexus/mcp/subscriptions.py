# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.11): the session MCP server's
per-session subscription set -- what the not-yet-built lifespan waiter
(bead nexus-rplay.10) will park a single ``wait`` call on -- plus the
instance-mailbox takeover (Technical Design "Subscriptions").

The set holds three kinds of entry, each with a cursor the waiter will
advance:

- ``mailbox/<session id>``, present from construction, never removable
  (it is the floor's own address).
- at most one further mailbox, this session's own instance-name mailbox,
  added once via :meth:`SubscriptionSet.subscribe` with a
  ``mailbox/<name>`` subspace. Subscribing it also takes over the
  per-session instance registration file the drain hook
  (``conexus/hooks/scripts/mailbox_drain.py``) reads and starts the
  RDR-208 ``directory/<name>`` lease, both previously owned by ``nx tuple
  watch --instance`` (:mod:`nexus.tuple_watch`).
- up to :data:`MAX_BOARD_TOPICS` ``board/<topic>`` subspaces.

Persisted in T1 scratch keyed by session id (see :func:`load`/:func:`persist`),
so a ``/resume`` (same session id, a fresh process) restores the list and a
``/clear`` (a new session id) starts clean -- T1 itself is already
session-scoped (``T1Database.list_entries`` filters on its own bound
``session_id``), so a fresh load under a different session id simply never
sees the old rows.

A mutation bumps :attr:`SubscriptionSet.version` and calls every
registered listener with ``self`` (:meth:`SubscriptionSet.add_listener`),
so the future waiter can cancel its parked ``wait`` and re-issue it with
the new list (Approach item 6, Technical Design "Waiting").

**Lifted, not imported, from** :mod:`nexus.tuple_watch` **(RDR-208 Phase 2
Step 1 / bead nexus-6konb.9):** ``write_instance_registration``'s path and
on-disk format are byte-identical to the original (the drain hook reads
``<config>/tuple-watch/addresses.d/<session id>``, one instance name per
line) -- copied rather than imported because that module's session-marker
contract (``_read_session_marker``/``write_session_marker``/
``session_marker_path``/``record_clear_and_write_session_marker``/
``cleared_record_path``) is being rehomed by a concurrent bead, and this
module must not become one of its importers mid-move. ``_directory_heartbeat``'s
due/rotate/nonce logic is the same shape as the original, adapted to log via
structlog instead of the CLI watcher's budgeted stdout emitter -- there is
no Monitor stream here to budget against. The originals stay in
``tuple_watch.py`` untouched; a later deletion bead removes that module
once the watcher itself is retired (RDR-211 Existing Infrastructure Audit).
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

# ── Lifted from nexus.tuple_watch (byte-identical path/format) ─────────────

_STATE_SUBDIR = "tuple-watch"

#: Mirrors ``nexus.tuple_watch._SAFE_SESSION_ID`` exactly -- a session id
#: outside this charset becomes a stray path-hostile filename, so a bad
#: value is a silent no-op here too, never trusted with a directory write.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

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


def registry_dir(state_dir: Path) -> Path:
    return state_dir / _STATE_SUBDIR / "addresses.d"


def registration_path(state_dir: Path, session_id: str) -> Path:
    return registry_dir(state_dir) / session_id


def write_instance_registration(state_dir: Path, session_id: str, instance: str) -> None:
    """Register *instance* as this session's own instance-name mailbox, so
    ``mailbox_drain.py`` can drain it for this session and only this
    session -- never a machine-wide file another session's prompt could
    read first.

    Written atomically (temp file, then rename) so a concurrent reader
    never observes a partial write. Best-effort: a failure here must never
    crash the caller -- it only means this session's instance-addressed
    mail has no drain floor until the next successful call.

    A *session_id* outside the safe charset (or an empty *instance*) is a
    silent no-op, mirroring ``nexus.tuple_watch.write_instance_registration``
    exactly (same path, same format, same guard) -- see this module's
    docstring for why it is copied rather than imported.
    """
    if not instance or not _SAFE_SESSION_ID.fullmatch(session_id):
        return
    path = registration_path(state_dir, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(instance + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as e:  # pragma: no cover — best-effort, disk-failure path
        _log.debug("subscriptions_registration_write_failed", session_id=session_id, error=str(e))


@dataclass
class _DirectoryLease:
    """This subscription's own ``directory/<name>`` lease state, tracked
    across heartbeat calls. Purely in-process, mirroring
    ``nexus.tuple_watch._DirectoryLease``: a fresh process (a `/resume`,
    or this session's next MCP server restart) re-arms from scratch."""

    armed: bool = False
    nonce: str = ""
    arm_time: float = 0.0
    last_send: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0


def _directory_nonce(t: float, pid: int) -> str:
    """Finer than one second and carries the pid, exactly mirroring
    ``nexus.tuple_watch._directory_nonce`` -- two processes arming
    ``directory/<name>`` for the same session in the same second must not
    collide on one id."""
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

    Adapted from ``nexus.tuple_watch._directory_heartbeat`` -- same
    due/rotate/nonce decision, logged via structlog instead of the CLI
    watcher's budgeted stdout emitter (this caller has no Monitor stream
    to budget against). *store* is an ``HttpTupleStore``-shaped object
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
    """Background re-send loop for one instance mailbox's directory
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


def _cursor_json(cursor: tuple[str, str] | None) -> dict[str, str] | None:
    if cursor is None:
        return None
    return {"created_at": cursor[0], "id": cursor[1]}


@dataclass
class SubscriptionSet:
    """One session's subscription list.

    ``session_mailbox`` (``mailbox/<session_id>``) is implicit and never
    stored in ``_board`` or removable. ``instance_mailbox`` is at most one
    further mailbox. ``_board`` maps a subscribed ``board/<topic>`` to its
    cursor (``None`` until the not-yet-built waiter advances it).
    """

    session_id: str
    instance_mailbox: str | None = None
    _instance_name: str | None = field(default=None, repr=False)
    _board: dict[str, tuple[str, str] | None] = field(default_factory=dict)
    _session_cursor: tuple[str, str] | None = field(default=None, repr=False)
    _instance_cursor: tuple[str, str] | None = field(default=None, repr=False)
    version: int = 0
    _listeners: list[Callable[["SubscriptionSet"], None]] = field(default_factory=list, repr=False)
    _lease_thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _lease_stop: threading.Event | None = field(default=None, repr=False, compare=False)

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
        ``mailbox/<name>``: accepted only as this session's own instance
        mailbox (see :meth:`_subscribe_instance_mailbox`). Anything else
        that resolves to a take-enabled template (a queue or a lock) is
        refused naming ``in``, since those are never delivered; anything
        else is refused as neither a board topic nor the session's own
        mailbox.

        *store_factory* is called (possibly more than once, possibly from
        a background thread later) to get a context manager yielding a
        T2Database-shaped object with a ``.tuples`` attribute -- see
        :func:`_lease_loop`'s docstring for why a raw store handle is
        never accepted directly.
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
            self._subscribe_instance_mailbox(
                name,
                store_factory=store_factory,
                state_dir=state_dir,
                directory_ttl_s=directory_ttl_s,
                directory_heartbeat_s=directory_heartbeat_s,
                lease_poll_s=lease_poll_s,
            )
            return
        if _take_enabled(templates, subspace):
            raise SchemaViolationError(
                f"{subspace!r} is a take-enabled subspace, worked with `in` and never "
                "delivered; tuple_subscribe accepts only board topics and the session's "
                "own instance mailbox"
            )
        raise SchemaViolationError(
            f"{subspace!r} is not a board topic or the session's own instance mailbox; refused"
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

    def _subscribe_instance_mailbox(
        self,
        name: str,
        *,
        store_factory: Callable[[], Any],
        state_dir: Path,
        directory_ttl_s: float,
        directory_heartbeat_s: float,
        lease_poll_s: float,
    ) -> None:
        subspace = f"mailbox/{name}"
        if subspace == self.session_mailbox:
            return  # already present from startup; not "an instance name"
        if self.instance_mailbox is not None:
            if subspace == self.instance_mailbox:
                return  # idempotent re-subscribe of the same name
            raise SchemaViolationError(
                f"this session already subscribes {self.instance_mailbox!r}; only one "
                f"instance mailbox is accepted, refusing {subspace!r}"
            )
        write_instance_registration(state_dir, self.session_id, name)
        self._start_lease(name, store_factory, directory_ttl_s, directory_heartbeat_s, lease_poll_s)
        self.instance_mailbox = subspace
        self._instance_name = name
        self._bump()

    def unsubscribe(self, subspace: str) -> None:
        """Remove *subspace*. The session's own mailbox can never be
        unsubscribed -- it is the floor's address. Unsubscribing something
        not currently subscribed is a silent no-op, not a refusal."""
        if subspace == self.session_mailbox:
            raise SchemaViolationError(
                "the session's own mailbox cannot be unsubscribed; it is the floor's address"
            )
        if self.instance_mailbox and subspace == self.instance_mailbox:
            self._stop_lease()
            self.instance_mailbox = None
            self._instance_name = None
            self._instance_cursor = None
            self._bump()
            return
        if subspace in self._board:
            del self._board[subspace]
            self._bump()

    # ── Cursor bookkeeping (RDR-211 Phase 1 Step 3, bead nexus-rplay.10) ────

    def advance_cursor(self, subspace: str, cursor: tuple[str, str]) -> None:
        """Move *subspace*'s delivery cursor forward -- the lifespan
        waiter's own bookkeeping as it delivers board posts past their
        prior cursor.

        Deliberately does NOT call :meth:`_bump` / notify listeners: a
        cursor advance is not a subscription-LIST mutation, and
        re-issuing the parked ``wait`` on every delivered tuple would be
        wasteful and wrong (Technical Design "Waiting" -- only a
        subscribe/unsubscribe re-issues it). A silent no-op for a
        *subspace* this set does not hold (e.g. one unsubscribed between
        the waiter reading its list and advancing this cursor).
        """
        if subspace == self.session_mailbox:
            self._session_cursor = cursor
        elif self.instance_mailbox and subspace == self.instance_mailbox:
            self._instance_cursor = cursor
        elif subspace in self._board:
            self._board[subspace] = cursor

    def entries(self) -> list[dict[str, Any]]:
        """This set's subspaces with their cursors, in the order
        ``tuple_subscriptions`` renders them: the session mailbox first,
        then the instance mailbox if any, then board topics."""
        out: list[dict[str, Any]] = [
            {"subspace": self.session_mailbox, "cursor": _cursor_json(self._session_cursor)},
        ]
        if self.instance_mailbox:
            out.append({"subspace": self.instance_mailbox, "cursor": _cursor_json(self._instance_cursor)})
        for topic, cursor in self._board.items():
            out.append({"subspace": topic, "cursor": _cursor_json(cursor)})
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
        thread.start()

    def _stop_lease(self) -> None:
        if self._lease_stop is not None:
            self._lease_stop.set()
        self._lease_thread = None
        self._lease_stop = None

    def shutdown(self) -> None:
        """Stop any live lease thread. Call before dropping a
        :class:`SubscriptionSet` (tests; a session-end/handoff path)."""
        self._stop_lease()

    # ── Persistence (T1, keyed by session id) ───────────────────────────

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "instance_mailbox": self.instance_mailbox,
            "instance_name": self._instance_name,
            "board": {k: (list(v) if v else None) for k, v in self._board.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SubscriptionSet":
        obj = cls(session_id=data["session_id"])
        obj.instance_mailbox = data.get("instance_mailbox")
        obj._instance_name = data.get("instance_name")
        board = data.get("board") or {}
        obj._board = {k: (tuple(v) if v else None) for k, v in board.items()}
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

    When the restored state names an instance mailbox and *store_factory*
    is given, the directory lease is restarted immediately (a resumed
    session keeps holding the name it registered before). Passing no
    *store_factory* restores the list without restarting the lease --
    used by callers (tests; :func:`get_or_load` cache misses that will
    call :meth:`SubscriptionSet.subscribe` again themselves) that manage
    the lease separately.
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
        if obj.instance_mailbox and obj._instance_name and store_factory is not None:
            obj._start_lease(
                obj._instance_name, store_factory, DIRECTORY_TTL_S, DIRECTORY_HEARTBEAT_S, _LEASE_POLL_S,
            )
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
