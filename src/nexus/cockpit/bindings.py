# SPDX-License-Identifier: AGPL-3.0-or-later
"""_BindingWatcher -- reaction loop for the cockpit (RDR-111 §Phase 2, nexus-0xaq).

The hook-bridge layer (``nexus.cockpit.hook_bridge``) drains Claude Code
hook events into the tuplespace, but tuples sitting in ``tuples.db`` do
nothing on their own -- the cockpit needs a *reaction loop* that watches
committed events and dispatches user-declared bindings. This module is
that loop.

Design summary
--------------

- **Event source**: the ``events`` table in ``tuples.db`` (see RDR-110
  store.py -- the table is fed by ``trg_tuples_out`` and ``trg_claim_log_event``
  on every committed tuple operation). Each row has a monotonic ``rowid``
  used as the watcher cursor.

- **Cursor persistence**: ``watcher_state`` table, keyed by
  ``(subspace, profile)``. The watcher persists ``last_rowid`` after every
  batch so a restart resumes at-least-once without replaying the whole
  events table.

- **Idempotency / dedup**: rowid is strictly monotonic. The watcher only
  fetches ``rowid > last_rowid`` and advances ``last_rowid`` to the max
  rowid in the batch. Reprocessing the same event is impossible within a
  single watcher process. Across processes the cursor table guarantees
  the same property after a restart.

- **Concurrency**: single asyncio task. SQL work is dispatched via
  ``asyncio.to_thread`` to keep the loop non-blocking. Actions are
  invoked sequentially per event in cursor order; one slow action
  briefly holds up its successors but does not affect other watchers
  (each subspace gets its own task).

- **Error containment**: each binding action is wrapped in
  ``try/except Exception``. A raising action is logged with structured
  context and skipped; the watcher continues with the next binding and
  the next event. One bad binding does not crash the loop or starve
  other bindings.

- **EventStream RPC fallback**: this implementation polls the ``events``
  table directly. The daemon-mode ``event_stream.subscribe`` RPC
  (nexus-m4gm) is the future upgrade path -- when it ships, this module
  should grow a ``transport`` parameter and prefer the RPC. Until then,
  polling the same table the RPC reads from gives bit-identical
  semantics.

Limits
------

- ``match`` is a flat dict of field equality predicates against the
  event record. No regex, no dotted access, no boolean composition.
  Listed as a PR follow-up; v1 keeps surface area small.

- ``action`` is one of: ``python:<dotted.module:func>`` (called with
  ``(event, binding, context)``) or ``log:<marker>`` (emits a single
  structlog event with that marker). No shell-out by design.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, Optional  # noqa: F401  -- Callable used in type hints

import structlog
import yaml

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BindingProfileError(ValueError):
    """Raised when a binding profile YAML fails validation."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Action:
    """Validated action descriptor (kind + payload).

    ``kind`` is currently one of ``"python"`` or ``"log"``. ``target`` is
    the dotted callable for ``python`` or the marker string for ``log``.
    """

    kind: str
    target: str


@dataclasses.dataclass(frozen=True)
class Binding:
    """One binding row: when ``match`` is satisfied, fire ``action``."""

    name: str
    match: dict[str, Any]
    action: Action


@dataclasses.dataclass(frozen=True)
class BindingProfile:
    """A named bundle of bindings loaded from one YAML file."""

    name: str
    bindings: tuple[Binding, ...]


@dataclasses.dataclass(frozen=True)
class EventRecord:
    """One row out of the ``events`` table -- what bindings match against."""

    cursor: int
    subspace: str
    op: str
    tuple_id: str
    payload_summary: Optional[str]
    category: Optional[str]
    ts: float

    def as_match_target(self) -> dict[str, Any]:
        """Return a plain dict for predicate evaluation."""
        return {
            "subspace": self.subspace,
            "op": self.op,
            "tuple_id": self.tuple_id,
            "category": self.category,
        }


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


def _validate_match(match: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(match, dict):
        raise BindingProfileError(
            f"{source}: 'match' must be a mapping, got {type(match).__name__}"
        )
    bad = [k for k in match if not isinstance(k, str)]
    if bad:
        raise BindingProfileError(f"{source}: non-string match keys: {bad!r}")
    return dict(match)


def _validate_action(action: Any, *, source: str) -> Action:
    if not isinstance(action, dict):
        raise BindingProfileError(
            f"{source}: 'action' must be a mapping, got {type(action).__name__}"
        )
    kind = action.get("kind")
    if kind == "python":
        callable_ref = action.get("callable")
        if not isinstance(callable_ref, str) or ":" not in callable_ref:
            raise BindingProfileError(
                f"{source}: python action requires 'callable: module.path:func'"
            )
        return Action(kind="python", target=callable_ref)
    if kind == "log":
        marker = action.get("marker")
        if not isinstance(marker, str) or not marker:
            raise BindingProfileError(
                f"{source}: log action requires non-empty 'marker' string"
            )
        return Action(kind="log", target=marker)
    raise BindingProfileError(
        f"{source}: unknown action kind {kind!r} (expected 'python' or 'log')"
    )


def load_profile(path: Path) -> BindingProfile:
    """Parse a binding-profile YAML file, raising on malformed input."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise BindingProfileError(f"{path.name}: malformed YAML -- {exc}") from exc

    if not isinstance(raw, dict):
        raise BindingProfileError(
            f"{path.name}: top-level YAML must be a mapping, "
            f"got {type(raw).__name__}"
        )

    name = raw.get("profile")
    if not isinstance(name, str) or not name:
        raise BindingProfileError(f"{path.name}: missing/empty 'profile' field")

    raw_bindings = raw.get("bindings")
    if not isinstance(raw_bindings, list):
        raise BindingProfileError(
            f"{path.name}: 'bindings' must be a list, got "
            f"{type(raw_bindings).__name__}"
        )

    bindings: list[Binding] = []
    seen_names: set[str] = set()
    for i, b in enumerate(raw_bindings):
        if not isinstance(b, dict):
            raise BindingProfileError(
                f"{path.name}: binding[{i}] must be a mapping"
            )
        bname = b.get("name")
        if not isinstance(bname, str) or not bname:
            raise BindingProfileError(
                f"{path.name}: binding[{i}] missing/empty 'name'"
            )
        if bname in seen_names:
            raise BindingProfileError(
                f"{path.name}: duplicate binding name {bname!r}"
            )
        seen_names.add(bname)
        source = f"{path.name}:{bname}"
        match = _validate_match(b.get("match"), source=source)
        action = _validate_action(b.get("action"), source=source)
        bindings.append(Binding(name=bname, match=match, action=action))

    return BindingProfile(name=name, bindings=tuple(bindings))


def load_profiles_dir(profiles_dir: Path) -> list[BindingProfile]:
    """Load all ``*.yml`` profiles in *profiles_dir* (non-recursive)."""
    if not profiles_dir.is_dir():
        return []
    profiles: list[BindingProfile] = []
    for yml in sorted(profiles_dir.glob("*.yml")):
        profiles.append(load_profile(yml))
    return profiles


# ---------------------------------------------------------------------------
# Match-predicate evaluation
# ---------------------------------------------------------------------------


def matches(event: EventRecord, predicate: dict[str, Any]) -> bool:
    """Return True iff every key in *predicate* equals the same field on *event*.

    Empty predicate matches everything (deliberate; lets a profile express
    a "fire on every event" binding). Unknown keys never match.
    """
    target = event.as_match_target()
    for k, v in predicate.items():
        if target.get(k) != v:
            return False
    return True


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


def _resolve_python_callable(target: str) -> Callable[..., Any]:
    mod_name, _, attr = target.partition(":")
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise BindingProfileError(
            f"python action target {target!r} is not callable"
        )
    return fn


#: Default TTL on the action_idempotency rows. RDR-111:933.
_IDEMPOTENCY_TTL_SECONDS = 300.0


def _idempotency_key(binding_name: str, tuple_id: str) -> str:
    """Deterministic key for the ``action_idempotency`` table.

    RDR-111 lines 909-942: ``sha256(binding_name + tuple_id)``. The key
    is per-(binding, event) so two different bindings on the same event
    each get their own dedup row, but a watcher restart that replays
    the same event for the same binding hits the dedup hit and skips.
    """
    raw = f"{binding_name}:{tuple_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _idempotency_check(
    memory_conn: sqlite3.Connection, *, key: str, now: float
) -> bool:
    """Return True if a non-expired key already exists for this action."""
    row = memory_conn.execute(
        "SELECT 1 FROM action_idempotency "
        "WHERE idempotency_key = ? AND expires_at > ? LIMIT 1",
        (key, now),
    ).fetchone()
    return row is not None


def _idempotency_record(
    memory_conn: sqlite3.Connection, *, key: str, expires_at: float
) -> None:
    """Insert (or refresh) the dedup row for a successfully dispatched action."""
    memory_conn.execute(
        "INSERT INTO action_idempotency (idempotency_key, expires_at) "
        "VALUES (?, ?) "
        "ON CONFLICT(idempotency_key) DO UPDATE SET expires_at = excluded.expires_at",
        (key, expires_at),
    )
    memory_conn.commit()


async def _dispatch_action(
    action: Action,
    *,
    event: EventRecord,
    binding: Binding,
    context: "BindingContext",
) -> None:
    if action.kind == "log":
        # Log actions are inherently idempotent, a second log line on
        # crash-replay is harmless, and the dedup gate would only waste a
        # SELECT round-trip per event.
        _log.info(
            "binding_action_log",
            marker=action.target,
            binding=binding.name,
            subspace=event.subspace,
            op=event.op,
            tuple_id=event.tuple_id,
        )
        return
    if action.kind == "python":
        # RDR-111:909-942 dedup gate. Only applied when memory_conn is
        # wired (production daemon path). Tests that pass a stub context
        # without memory_conn keep the legacy at-least-once semantics.
        if context.memory_conn is not None:
            key = _idempotency_key(binding.name, event.tuple_id)
            now = time.time()
            try:
                if _idempotency_check(context.memory_conn, key=key, now=now):
                    _log.info(
                        "binding_action_dedup_skip",
                        binding=binding.name,
                        subspace=event.subspace,
                        tuple_id=event.tuple_id,
                    )
                    return
            except sqlite3.Error:
                # Table missing or other persistent error, fall through to
                # dispatch (degrade-open). Without the table, replays cannot
                # be filtered, but skipping the action is the worse failure
                # mode (silent loss of side effect).
                _log.exception(
                    "binding_action_idempotency_check_failed",
                    binding=binding.name,
                    tuple_id=event.tuple_id,
                )

        fn = _resolve_python_callable(action.target)
        result = fn(event, binding, context)
        if asyncio.iscoroutine(result):
            await result

        if context.memory_conn is not None:
            try:
                _idempotency_record(
                    context.memory_conn,
                    key=_idempotency_key(binding.name, event.tuple_id),
                    expires_at=time.time() + _IDEMPOTENCY_TTL_SECONDS,
                )
            except sqlite3.Error:
                _log.exception(
                    "binding_action_idempotency_record_failed",
                    binding=binding.name,
                    tuple_id=event.tuple_id,
                )
        return
    raise BindingProfileError(f"unknown action kind {action.kind!r}")


# ---------------------------------------------------------------------------
# Context passed to python actions
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BindingContext:
    """Mutable context object passed to python action callables.

    Holds the tuplespace handles an action might need to write derived
    tuples back into the system. Tests inject a stub object; production
    wiring constructs one in :meth:`_BindingWatcher.start`.

    ``memory_conn`` is the optional ``memory.db`` connection used for
    the ``action_idempotency`` dedup gate (RDR-111 §lines 909-942,
    nexus-8wvs). When ``None`` the watcher runs without the dedup gate
   , tests that inject a stub context exercise this path. Production
    wiring (the daemon) populates it so a crash between dispatch and
    cursor save cannot re-fire a python action on restart.
    """

    conn: Optional[sqlite3.Connection] = None
    index: Any = None  # nexus.tuplespace.index.TupleIndex
    registry: Any = None  # nexus.tuplespace.registry.Registry
    profile_name: str = ""
    memory_conn: Optional[sqlite3.Connection] = None
    # Free-form bag so reference actions and tests can stash arbitrary
    # state without growing the dataclass for every consumer.
    extras: dict[str, Any] = dataclasses.field(default_factory=dict)


# ---------------------------------------------------------------------------
# Reference action callables (the "is this primitive useful?" demonstration)
# ---------------------------------------------------------------------------


def action_emit_derived(
    event: EventRecord,
    binding: Binding,
    context: BindingContext,
) -> None:
    """Reference binding action: in-tuplespace reaction.

    On a PostToolUse 'out' event, writes a derived tuple onto
    ``derived/<profile>`` recording the source event. This is the
    canonical "one event in, a different event out" demonstration.
    """
    if context.conn is None or context.index is None or context.registry is None:
        _log.warning(
            "binding_action_emit_derived_missing_context",
            binding=binding.name,
        )
        return
    from nexus.tuplespace.api import out

    profile = context.profile_name or "default"
    subspace = f"derived/{profile}"
    dimensions = {
        "profile": profile,
        "source_op": event.op,
        "source_sub": event.subspace,
        "tuple_id": event.tuple_id,
    }
    match_text = f"{event.subspace} {event.op} {event.tuple_id}"
    out(
        conn=context.conn,
        index=context.index,
        registry=context.registry,
        subspace=subspace,
        content=match_text,
        dimensions=dimensions,
        match_text=match_text,
    )


def action_log_marker(
    event: EventRecord,
    binding: Binding,
    context: BindingContext,
) -> None:
    """Reference binding action: side effect via structlog.

    Emits a structlog event with a stable marker key so a downstream
    log consumer (or a test) can detect the reaction. No subprocess, no
    network.
    """
    _log.info(
        "binding_marker",
        marker="cockpit.binding.notification",
        binding=binding.name,
        subspace=event.subspace,
        op=event.op,
        tuple_id=event.tuple_id,
    )


# ---------------------------------------------------------------------------
# Cursor + event-table SQL helpers (run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _load_cursor(
    conn: sqlite3.Connection, *, subspace_prefix: str, profile: str
) -> int:
    row = conn.execute(
        "SELECT last_rowid FROM watcher_state WHERE subspace = ? AND profile = ?",
        (subspace_prefix, profile),
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def _save_cursor(
    conn: sqlite3.Connection,
    *,
    subspace_prefix: str,
    profile: str,
    last_rowid: int,
) -> None:
    conn.execute(
        "INSERT INTO watcher_state (subspace, profile, last_rowid, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(subspace, profile) DO UPDATE SET "
        "last_rowid = excluded.last_rowid, updated_at = excluded.updated_at",
        (subspace_prefix, profile, last_rowid, time.time()),
    )
    conn.commit()


_GLOB_SPECIAL_CHARS = frozenset("*?[")


def _glob_to_prefix_range(subspace_glob: str) -> tuple[str, str] | None:
    """Return ``(lo, hi)`` so ``lo <= subspace < hi`` matches ``prefix*``.

    Returns ``None`` when ``subspace_glob`` is not a pure-prefix shape
    (e.g. ``*``, ``[abc]*``, ``foo?bar``); callers fall back to GLOB.

    For ``"tasks/*"`` returns ``("tasks/", "tasks0")``: ``"0"`` is the
    next ASCII byte after ``"/"`` (``0x30`` > ``0x2F``), and SQLite's
    default ``BINARY`` collation compares bytewise so the half-open range
    covers exactly the rows starting with ``"tasks/"``. We append a
    high-codepoint byte (``"￿"``) for prefixes that don't end in a
    safely-incrementable ASCII byte, giving SQLite something concrete to
    range-scan against rather than rebuilding the alphabet boundary.
    """
    if not subspace_glob.endswith("*"):
        return None
    prefix = subspace_glob[:-1]
    # Reject anything with glob metacharacters inside the prefix.
    if any(c in _GLOB_SPECIAL_CHARS for c in prefix):
        return None
    if not prefix:
        return None
    # Compute the smallest string strictly greater than every ``prefix*``
    # match. Using ``"￿"`` as the upper sentinel works for any UTF-8
    # input (SQLite stores TEXT as UTF-8, BINARY collation is bytewise).
    hi = prefix + "￿"
    return prefix, hi


def _build_fetch_event_batch_sql(
    *,
    subspace_glob: str,
    after_rowid: int,
    limit: int,
) -> tuple[str, tuple]:
    """Pick the right SQL + params for ``_fetch_event_batch``.

    Returns ``(sql, params)`` so callers (and tests) can run EXPLAIN
    QUERY PLAN against the rewrite. Three branches:

    - ``"*"`` -> drop the subspace predicate entirely. The query becomes
      a rowid range walk (integer primary key) and never touches
      ``idx_events_subspace_rowid``.
    - ``"prefix*"`` -> rewrite to ``subspace >= 'prefix' AND
      subspace < 'prefix￿'``, which SQLite resolves via
      ``idx_events_subspace_rowid`` as a range scan.
    - Anything else (e.g. ``"[abc]*"``, ``"foo?bar"``) keeps the
      original ``GLOB`` predicate. SQLite uses whatever index it can.
    """
    if subspace_glob == "*":
        return (
            "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
            "FROM events WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (after_rowid, limit),
        )
    prefix_range = _glob_to_prefix_range(subspace_glob)
    if prefix_range is not None:
        lo, hi = prefix_range
        return (
            "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
            "FROM events WHERE subspace >= ? AND subspace < ? AND rowid > ? "
            "ORDER BY rowid LIMIT ?",
            (lo, hi, after_rowid, limit),
        )
    return (
        "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
        "FROM events WHERE subspace GLOB ? AND rowid > ? "
        "ORDER BY rowid LIMIT ?",
        (subspace_glob, after_rowid, limit),
    )


def _fetch_event_batch(
    conn: sqlite3.Connection,
    *,
    subspace_glob: str,
    after_rowid: int,
    limit: int,
) -> list[EventRecord]:
    """Pull the next ``events`` batch since ``after_rowid``.

    nexus-anjo: dispatches on the glob shape so SQLite can use the
    right index. See :func:`_build_fetch_event_batch_sql` for the
    rewrite rules.
    """
    sql, params = _build_fetch_event_batch_sql(
        subspace_glob=subspace_glob, after_rowid=after_rowid, limit=limit
    )
    rows = conn.execute(sql, params).fetchall()
    return [
        EventRecord(
            cursor=int(r[0]),
            subspace=str(r[1]),
            op=str(r[2]),
            tuple_id=str(r[3]),
            payload_summary=r[4],
            category=r[5],
            ts=float(r[6]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


class _BindingWatcher:
    """Async polling reaction loop over the tuplespace ``events`` table.

    One watcher subscribes to a single *subspace_glob* (defaults to ``*``
    so all events are visible) and dispatches every loaded *profile*'s
    bindings on each event in cursor order.

    Wire from cockpit startup:

        >>> watcher = _BindingWatcher(
        ...     conn=conn,
        ...     profiles=load_profiles_dir(profiles_dir),
        ...     context=BindingContext(conn=conn, index=index, registry=registry),
        ... )
        >>> task = asyncio.create_task(watcher.run())
        >>> # ... later ...
        >>> watcher.request_stop()
        >>> await task

    The class is intentionally direct-mode polling. Daemon-mode users
    will swap the SQL polling for ``event_stream.subscribe`` once the
    RPC ships (nexus-m4gm, RDR-112). Until then, this is the same query
    the daemon-side handler runs -- identical semantics.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        profiles: Iterable[BindingProfile],
        context: BindingContext,
        subspace_glob: str = "*",
        poll_interval: float = 0.05,
        batch_limit: int = 100,
        stop_timeout: float = 5.0,
    ) -> None:
        self._conn = conn
        self._profiles: tuple[BindingProfile, ...] = tuple(profiles)
        self._context = context
        self._subspace_glob = subspace_glob
        self._poll_interval = poll_interval
        self._batch_limit = batch_limit
        self._stop_timeout = stop_timeout
        self._stop = asyncio.Event()
        # One cursor per (subspace_glob, profile_name). Loaded lazily in run().
        self._cursors: dict[str, int] = {}
        # Task handle from start(); stays None until start() is called.
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    # -- lifecycle --------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the watcher to exit at the next loop boundary."""
        self._stop.set()

    def start(self) -> "asyncio.Task[None]":
        """Schedule :meth:`run` on the current event loop. Idempotent.

        Returns the asyncio task driving the loop. Subsequent calls
        return the existing task without spawning a second one. The
        daemon stores the watcher instance and calls :meth:`stop`
        during shutdown; tests can ``await`` the returned task
        directly for fine-grained orchestration.
        """
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Signal the loop to exit and wait for the task to finish.

        Bounded by ``stop_timeout`` (default 5s, matching the daemon's
        socket-close timeout). On timeout the task is cancelled and a
        warning is logged. Safe to call before :meth:`start` (no-op)
        or twice (second call is a no-op).
        """
        self.request_stop()
        if self._task is None or self._task.done():
            return
        try:
            await asyncio.wait_for(self._task, timeout=self._stop_timeout)
        except asyncio.TimeoutError:
            _log.warning(
                "binding_watcher_stop_timeout",
                timeout=self._stop_timeout,
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def run(self) -> None:
        """Run the polling loop until :meth:`request_stop` is invoked."""
        # Load per-profile cursors once at startup.
        #
        # SQLite connection objects are pinned to the thread that opened
        # them (default ``check_same_thread=True``). Production callers
        # construct the watcher on the asyncio loop thread, so running
        # the SQL inline here is correct. We deliberately do NOT use
        # ``asyncio.to_thread`` for the SQLite calls -- that would land in
        # a worker thread and trip SQLite's same-thread guard. The
        # queries themselves are sub-millisecond and never block the
        # loop noticeably.
        for profile in self._profiles:
            self._cursors[profile.name] = _load_cursor(
                self._conn,
                subspace_prefix=self._subspace_glob,
                profile=profile.name,
            )

        _log.info(
            "binding_watcher_started",
            profiles=[p.name for p in self._profiles],
            subspace_glob=self._subspace_glob,
            cursors=dict(self._cursors),
        )

        while not self._stop.is_set():
            advanced = await self._tick()
            if advanced == 0:
                # Quiet -- wait for either poll-tick or stop.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

        _log.info(
            "binding_watcher_stopped",
            cursors=dict(self._cursors),
        )

    # -- single iteration --------------------------------------------------

    async def _tick(self) -> int:
        """Run one polling iteration. Returns the number of events processed."""
        total = 0
        for profile in self._profiles:
            cursor = self._cursors.get(profile.name, 0)
            try:
                events = _fetch_event_batch(
                    self._conn,
                    subspace_glob=self._subspace_glob,
                    after_rowid=cursor,
                    limit=self._batch_limit,
                )
            except sqlite3.Error:
                _log.exception(
                    "binding_watcher_fetch_failed",
                    profile=profile.name,
                    cursor=cursor,
                )
                continue

            if not events:
                continue

            self._context.profile_name = profile.name
            for event in events:
                await self._dispatch_event(profile, event)
            # Cursor advances even if individual bindings raised -- error
            # containment means we never get stuck on a poison event.
            new_cursor = max(e.cursor for e in events)
            self._cursors[profile.name] = new_cursor
            try:
                _save_cursor(
                    self._conn,
                    subspace_prefix=self._subspace_glob,
                    profile=profile.name,
                    last_rowid=new_cursor,
                )
            except sqlite3.Error:
                _log.exception(
                    "binding_watcher_cursor_save_failed",
                    profile=profile.name,
                    cursor=new_cursor,
                )
            total += len(events)
        return total

    async def _dispatch_event(
        self, profile: BindingProfile, event: EventRecord
    ) -> None:
        for binding in profile.bindings:
            if not matches(event, binding.match):
                continue
            try:
                await _dispatch_action(
                    binding.action,
                    event=event,
                    binding=binding,
                    context=self._context,
                )
            except Exception:
                # Containment per acceptance criterion 4: one bad binding
                # must not crash the watcher or starve siblings.
                _log.exception(
                    "binding_action_failed",
                    profile=profile.name,
                    binding=binding.name,
                    action_kind=binding.action.kind,
                    action_target=binding.action.target,
                    subspace=event.subspace,
                    op=event.op,
                    tuple_id=event.tuple_id,
                )


# ---------------------------------------------------------------------------
# Default profile location
# ---------------------------------------------------------------------------


def default_profiles_dir() -> Path:
    """Resolve the canonical profiles dir relative to the package."""
    here = Path(__file__).resolve()
    # src/nexus/cockpit/bindings.py → repo root → nx/tuplespace/builtin/bindings/profiles
    repo_root = here.parent.parent.parent.parent
    return repo_root / "nx" / "tuplespace" / "builtin" / "bindings" / "profiles"
