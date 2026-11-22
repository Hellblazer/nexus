# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The MCP server's half of session identity (RDR-253 Step 3): handover, renewal, refresh, fork row, pending bind, end.

The SessionStart hook binds and writes the process's token file; this module keeps the binding alive from the
server, which is the one process that outlives every ``/clear``:

* **token read.** A new server reads the token file of its own Claude process (its pid and the registry's
  ``startedAt``), polling once a second for up to 30 seconds, since the hook and the server start together; if the
  file has not appeared it runs unbound and keeps looking on every heartbeat.
* **heartbeat re-read.** Every heartbeat it re-reads the file and adopts the identity and generation it finds, so a
  cross-seat ``/resume`` or a take-over reaches it within a minute even if the T1 handoff marker was lost.
* **renewal** every 60 seconds; **refresh** once the token has under 50 minutes of life left (so a sleep under about
  50 minutes never lapses it), through ``bind`` with the same generation, which rewrites the process's one token file. A renewal or refresh from a superseded generation is refused: the server
  stops writing identity state and says so.
* **fork row.** ``/branch`` runs no hook. When the registry entry of its Claude process names a session id the
  index does not know, the server appends the ``fork`` row at the renewal after the one that first sees it: on
  ``/clear`` Claude writes the new id before the SessionStart hook binds it, and one renewal of grace lets the hook
  bind it first.
* **pending bind.** While unbound it looks for the pending file of its own Claude process (the hook's registry wait
  timed out), binds from it with the data credential and deletes it. That credential may start, resume, fork,
  refresh and end a binding and create an ephemeral identity, and the pending bind needs nothing else.
* **end.** On a clean shutdown it appends ``end`` with its expected generation, so a late ``end`` from a superseded
  process is refused, and deletes its token file.

One thread per server (:func:`start`); :meth:`IdentityLease.tick` is one heartbeat and holds the logic, so a test
drives it with a fixed clock and no thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from nexus.identity import token_contract
from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError, evidence_body
from nexus.identity.binder import LaunchFacts, SessionBinder
from nexus.identity.holder import Processes, epoch_seconds, normalise_instant, read_registry_entry
from nexus.identity.pending import delete_pending, take_pending
from nexus.identity.token_file import (
    TokenFile, ancestor_token_path, delete_token_file, identity_dir, read_token_file, update_token_file,
)

_log = structlog.get_logger(__name__)

HEARTBEAT_S: float = 60.0
STARTUP_POLL_S: float = 1.0
STARTUP_WINDOW_S: float = 30.0
THREAD_POLL_S: float = 1.0
DEFAULT_TOKEN_LIFE_S: float = 3600.0
#: How long a shutdown waits for the Claude process to exit before deciding the shutdown is not a session end.
CLAUDE_EXIT_WAIT_S: float = 1.5
#: After a wake the engine may not be reachable for a few seconds (the network is coming back): a refresh the wake owes is
#: retried at this interval, not left for the next 60-second heartbeat.
WAKE_RETRY_S: float = 5.0
#: The retry doubles on each failed attempt and stops at the heartbeat, so an engine that stays down is asked once a minute.
WAKE_RETRY_MAX_S: float = HEARTBEAT_S
#: A wake is seen as the wall clock running this far ahead of the monotonic clock between two ticks (a sleep moves only the
#: wall clock on macOS and Linux), or as this long without a tick on any platform: where the monotonic clock counts sleep
#: too (Windows) the first signal shows nothing, and a process stalled this long may have slept without it.
WAKE_JUMP_S: float = HEARTBEAT_S
WAKE_GAP_S: float = 3 * HEARTBEAT_S
#: The one log line for a token that cannot be refreshed any more (engine 0.1.198: a refresh or fork needs a live token).
#: Refreshing stops until a SessionStart bind writes a fresh token into the file.
TOKEN_LAPSED_NOTICE = "the identity token expired while the session slept; it rebinds on the next start"


@dataclass
class TickReport:
    """What one lease tick did, for tests and the log."""

    adopted: bool = False
    pending_bound: bool = False
    renewed: bool = False
    refreshed: bool = False
    forked: bool = False
    superseded: bool = False
    error: str | None = None


#: RDR-253 Step 5: called with the new identity whenever a lease adopts one (a late token file, a switch, a
#: take-over). The subscription set registers here so ``mailbox/<identity>`` is delivered from the moment the
#: binding exists, not only when the set happened to load after it.
_IDENTITY_LISTENERS: list[Callable[[str | None], None]] = []


def add_identity_listener(callback: Callable[[str | None], None]) -> None:
    """Register *callback*; it is called at once with the current lease's identity when one is bound."""
    _IDENTITY_LISTENERS.append(callback)
    identity = getattr(globals().get("_LEASE"), "identity", None)  # _LEASE is defined below, set by start()
    if identity:
        callback(identity)


def remove_identity_listener(callback: Callable[[str | None], None]) -> None:
    """Drop *callback* (a no-op when it is not registered): a torn-down subscription set stops being told."""
    try:
        _IDENTITY_LISTENERS.remove(callback)
    except ValueError:
        pass


def _notify_identity(identity: str | None) -> None:
    for callback in list(_IDENTITY_LISTENERS):
        try:
            callback(identity)
        except Exception as exc:  # noqa: BLE001 -- a listener's failure never stops the lease
            _log.warning("identity_listener_failed", error=str(exc))


class IdentityLease:
    """This MCP server's binding, kept alive. *claude_pid* is the nearest Claude ancestor.

    Two locks. ``_tick_lock`` serialises a heartbeat and a shutdown and is held across the engine calls; ``_lock``
    guards the in-memory state only and is never held across I/O, so a caller on the asyncio loop
    (:meth:`adopt_marker`) is never made to wait on the engine. *close_client* makes the lease close the engine client
    the factory made (production); a test that shares its client leaves it open."""

    def __init__(self, *, client_factory: Callable[[], Any], config_dir: Path, claude_pid: int,
                 procs: Processes | Any | None = None, clock: Callable[[], float] = time.time,
                 client_version: str | None = None, sleep: Callable[[float], None] = time.sleep,
                 close_client: bool = False, monotonic: Callable[[], float] | None = None,
                 fork_grace: bool = True) -> None:
        self._factory = client_factory
        self._idir = identity_dir(config_dir)
        self._claude_pid = claude_pid
        self._procs = procs if procs is not None else Processes()
        self._clock = clock
        self._sleep = sleep
        self._version = client_version
        self._close_client = close_client
        self._fork_grace = fork_grace             # one renewal of grace before a fork row (False: fork at first sight)
        self._client: Any = None
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self.identity: str | None = None
        self.generation: int | None = None
        self.token: str | None = None
        self.token_expires_at: float | None = None
        self.token_life_s: float = DEFAULT_TOKEN_LIFE_S
        self.session_id: str | None = None
        self.superseded: bool = False
        self._known_sessions: set[str] = set()
        # Wake detection needs a wall clock and a monotonic clock that belong together: the real pair, or a pair the test
        # injects. An injected wall clock alone (a test that steps it by minutes) never reads as a wake.
        self._mono: Callable[[], float] | None = monotonic or (time.monotonic if clock is time.time else None)
        self._seen: tuple[float, float] | None = None
        self._wake_pending = False
        self._wake_failures = 0                   # failed attempts at the refresh a wake owes: the retry backs off by them
        self._token_lapsed = False
        self._last_wall: float | None = None      # the wall clock at the last tick, to see a step backwards
        self._fork_sid: str | None = None         # the session id of the fork row in flight, for a once-per-id warning
        self._unknown_sid: str | None = None      # an unknown registry session id seen once: it gets one renewal of grace
        self._fork_warned: set[str] = set()
        self._started = clock()
        self._next_heartbeat = 0.0
        self._next_renew = 0.0

    # ── what the process is ─────────────────────────────────────────────────

    @property
    def bound(self) -> bool:
        return self.identity is not None and not self.superseded

    def token_path(self) -> Path | None:
        """This process's token file: its Claude pid and the ``startedAt`` the registry records; None until the
        registry file exists."""
        return ancestor_token_path(self._idir, self._claude_pid, self._procs.sessions_dir)

    def _engine(self) -> Any:
        """One engine client for the life of the lease, rebuilt after a failure to reach the engine."""
        if self._client is None:
            self._client = self._factory()
        return self._client

    def _drop_engine(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._close_client and hasattr(client, "close"):
            with contextlib.suppress(Exception):
                client.close()

    def _process_evidence(self) -> dict[str, Any]:
        """The process a refresh, fork or end speaks for: host, Claude pid, ``startedAt`` and OS start time."""
        entry = read_registry_entry(self._claude_pid, self._procs.sessions_dir)
        record = read_token_file(self.token_path()) if self.token_path() is not None else None
        start = (record.proc_start if record else None) or normalise_instant(self._procs.os_start(self._claude_pid))
        started_at = entry.started_at if entry else None
        # The host this process's own binding records speaks for it: its token file is the process's own (its pid and
        # startedAt name it), written by whichever client bound it, and the engine compares the host as a string on refresh
        # and fork. Absent a file, the machine's name as of now (Processes.host reads the key probe per call).
        host = record.host if record is not None and record.host else self._procs.host
        return {"host": host, "pid": self._claude_pid, "started_at": started_at,
                "proc_start": start}

    # ── one heartbeat ───────────────────────────────────────────────────────

    def due(self) -> bool:
        """Whether a heartbeat is due now (every second inside the startup window while unbound, else every 60). One is also
        due at once after the wall clock stepped backwards, which would otherwise stall the schedule by the step."""
        now = self._clock()
        return now >= self._next_heartbeat or (self._last_wall is not None and now < self._last_wall)

    def tick(self) -> TickReport:
        """One heartbeat: re-read the token file, bind from a pending file while unbound, then renew, refresh and
        append a fork row as due. Never raises: a failure is logged and the next heartbeat tries again."""
        report = TickReport()
        with self._tick_lock:
            now = self._clock()
            unbound_early = not self.bound and now - self._started < STARTUP_WINDOW_S
            self._next_heartbeat = now + (STARTUP_POLL_S if unbound_early else HEARTBEAT_S)
            self._observe_wake(now)
            try:
                report.adopted = self._reread()
                if self.identity is None:
                    report.pending_bound = self._pending_bind()
                if self.bound:
                    self._maintain(now, report)
                else:
                    self._wake_done()                    # nothing to refresh: a later bind brings its own fresh token
            except IdentityUnavailableError as exc:
                if self._wake_pending and token_contract.usable(self.token, self.token_expires_at, now=now):
                    # the network may be a few seconds behind the wake: try again soon, then less and less often
                    self._next_heartbeat = now + min(WAKE_RETRY_S * 2 ** self._wake_failures, WAKE_RETRY_MAX_S)
                    self._wake_failures += 1
                self._drop_engine()                      # the endpoint may have moved: resolve it again next time
                report.error = f"engine unavailable: {exc}"
                _log.debug("identity_lease_engine_unavailable", error=str(exc))
            except IdentityApiError as exc:
                report.error = f"{exc}"
                _log.warning("identity_lease_refused", error=str(exc), identity=self.identity)
            except Exception as exc:  # noqa: BLE001 — a heartbeat must never kill the server's thread
                report.error = f"{type(exc).__name__}: {exc}"
                _log.warning("identity_lease_tick_failed", error=report.error)
            report.superseded = self.superseded
        return report

    def _observe_wake(self, now: float) -> None:
        """Note a wake between the last tick and this one: the laptop slept, so the token's life ran on while no tick did.
        The refresh it owes (:meth:`_maintain`) is then made on this tick, whatever the token's age, so the next sleep
        starts from a full life. A wall clock that stepped backwards makes the renewal due on this tick: the schedule is set
        from the wall clock, and would otherwise stall by the size of the step (a renewal gap over 300 seconds lapses the seat)."""
        previous, self._last_wall = self._last_wall, now
        if previous is not None and now < previous:
            self._next_renew = 0.0
            _log.info("identity_lease_clock_stepped_back", step_s=round(previous - now))
        if self._mono is None:
            return
        mono = self._mono()
        last, self._seen = self._seen, (now, mono)
        if last is None:
            return
        wall_gap, mono_gap = now - last[0], mono - last[1]
        if wall_gap - mono_gap > WAKE_JUMP_S or wall_gap > WAKE_GAP_S:
            self._wake_pending, self._wake_failures = True, 0
            _log.info("identity_lease_wake", wall_gap_s=round(wall_gap), monotonic_gap_s=round(mono_gap))

    def _reread(self) -> bool:
        """Adopt what the token file says: a new identity or generation (an identity switch, a take-over, a
        refresh by a sibling) or a new token. True when anything changed."""
        path = self.token_path()
        record = read_token_file(path) if path is not None else None
        if record is None:
            return False
        return self._adopt(record)

    def _adopt(self, record: TokenFile) -> bool:
        with self._lock:
            changed = (record.identity, record.generation) != (self.identity, self.generation)
            newer_token = record.token != self.token
            if not changed and not newer_token and record.session_id == self.session_id:
                return False
            if changed and self.superseded and self.identity is not None:
                _log.info("identity_lease_readopted", old=self.identity, new=record.identity, generation=record.generation)
            if changed:
                self.superseded = False
                self._next_renew = 0.0
            if newer_token and token_contract.usable(record.token, record.expires_at, now=self._clock()):
                self._token_lapsed = False   # a live token the hook bound meanwhile: refreshing works again
            self.identity, self.generation = record.identity, record.generation
            self.token, self.token_expires_at = record.token, record.expires_at
            if record.session_id:
                self.session_id = record.session_id
            self._known_sessions.update(record.session_ids)
            if record.session_id:
                self._known_sessions.add(record.session_id)
        if changed:
            _notify_identity(record.identity)  # outside the lock: a listener may take its own
        return True

    def adopt_marker(self, *, identity: str | None, generation: int | None, token_file: str | None) -> bool:
        """Adopt the identity a T1 handoff marker names, reading the token file it points at. The path must be this
        process's own token file: a marker naming another path is ignored, never followed. The file is read before
        the state lock is taken, so this never waits on an engine call a heartbeat has in flight."""
        if not identity or token_file is None:
            return False
        own = self.token_path()
        if own is None or Path(token_file) != own:
            _log.warning("identity_marker_path_rejected", marker_path=token_file, own=str(own) if own else None)
            return False
        record = read_token_file(own)
        if record is None or record.identity != identity or (generation is not None and record.generation != generation):
            return False
        return self._adopt(record)

    # ── bound: renew, refresh, fork ─────────────────────────────────────────

    def _maintain(self, now: float, report: TickReport) -> None:
        with self._lock:
            identity, generation = self.identity, self.generation
        client = self._engine()
        if now >= self._next_renew:
            try:
                try:
                    self._fork_if_unknown(client, report)
                except IdentityApiError as exc:
                    if (token_contract.is_stale_generation(exc.status, exc.code)
                            or (exc.status == 409 and exc.code in ("not_held", "identity_not_held"))):
                        raise   # the renewal's own handling below: superseded, or the seat is gone
                    # any other refusal of the fork row (a lapsed token is handled inside it) never costs the seat its lease;
                    # a row that stays refused warns once per session id, not once a minute
                    sid, self._fork_sid = self._fork_sid, None
                    if sid not in self._fork_warned:
                        self._fork_warned.add(sid or "")
                        _log.warning("identity_lease_fork_failed", identity=identity, session_id=sid, error=str(exc))
                client.renew(identity, generation, self._version)
                report.renewed = True
            except IdentityApiError as exc:
                if token_contract.is_stale_generation(exc.status, exc.code):
                    self._superseded(exc, identity, generation)
                    return
                if exc.status == 409 and exc.code in ("not_held", "identity_not_held"):  # Step 1 / Step 2 spellings
                    _log.info("identity_lease_not_held", identity=identity, state=exc.body.get("state"))
                    self._wake_done()   # nothing to refresh for a seat nobody holds: the next bind brings its own token
                    return  # the seat was released or ended under this process: the token file will say what is next
                raise
            self._next_renew = now + HEARTBEAT_S
        due = (self.token_expires_at is not None
               and self.token_expires_at - now < token_contract.refresh_below(self.token_life_s))
        if self.token and not self._token_lapsed and (due or self._wake_pending):
            self._refresh(client, now, report)   # an unreachable engine raises past this: the wake's refresh is still owed
        self._wake_done()

    def _wake_done(self) -> None:
        """Nothing is owed for a wake any more: it was refreshed, or there is nothing to refresh (a lapsed token, a seat
        nobody holds, an unbound lease). A stale one would make a later rebind's fresh token refresh for no reason."""
        with self._lock:
            self._wake_pending, self._wake_failures = False, 0

    def _live_token(self) -> str | None:
        """The lease's token when it may be sent now; an expired one is not (the engine would read it as none)."""
        with self._lock:
            token, expires = self.token, self.token_expires_at
        return token if token_contract.usable(token, expires, now=self._clock()) else None

    def _lapse(self, exc: IdentityApiError, identity: str | None, held: str | None) -> bool:
        """Whether *exc* is the engine's 403 ``identity_token_required`` for a refresh or fork: the token is gone (engine
        0.1.198 gives an expired token no grace), so refreshing stops until a SessionStart bind writes a fresh token into
        the file, which :meth:`_adopt` picks up. Logged once. The token file is left as it is, and no bind is made in its
        place: a fenced write now fails with the engine's own 403.

        *held* is the token the lease held when the call went out (the one sent, or the expired one that was not). The
        flag is set only if the lease still holds it: a marker may have adopted a live token while the call was in flight,
        and the refusal is about the old one."""
        if not token_contract.is_token_required(exc.status, exc.code):
            return False
        with self._lock:
            if self.token != held:
                return True            # refused, but the token is not the one refused: leave the new one refreshing
            first, self._token_lapsed = not self._token_lapsed, True
            self._wake_pending, self._wake_failures = False, 0
        if first:
            _log.warning("identity_token_lapsed", identity=identity, consequence=TOKEN_LAPSED_NOTICE)
        return True

    def _refresh(self, client: Any, now: float, report: TickReport) -> None:
        with self._lock:
            identity, generation, held = self.identity, self.generation, self.token
        try:
            view = client.bind(identity, "refresh", generation,
                               evidence_body(client_version=self._version, **self._process_evidence()),
                               token=self._live_token())
        except IdentityApiError as exc:
            if token_contract.is_stale_generation(exc.status, exc.code):
                self._superseded(exc, identity, generation)
                return
            if self._lapse(exc, identity, held):
                return
            raise
        token, life = token_contract.token_from_response(view)
        if token is None:
            return  # an engine that issues none (Step 1): nothing to rewrite
        with self._lock:
            if (self.identity, self.generation) != (identity, generation):
                return  # a marker moved the lease to another identity while the call was out: this token is the old one's
            self.token, self.token_expires_at = token, token_contract.expires_at(life, now=now)
            if life:
                self.token_life_s = life
        self._rewrite_file()
        report.refreshed = True

    def _rewrite_file(self) -> None:
        """Write the lease's token (and session id) into the process's token file, as one compare-and-swap under the
        file lock: the hook may have rewritten the file meanwhile (a ``/clear``, a cross-seat ``/resume``), and a
        lease that still holds the old identity must not put it back. A file that names another identity or
        generation than this lease's, or that is gone, is left as it is: the next heartbeat adopts it."""
        path = self.token_path()
        with self._lock:
            identity, generation, token, expires, sid = (self.identity, self.generation, self.token,
                                                         self.token_expires_at, self.session_id)
        if path is None or identity is None or generation is None:
            return

        def swap(current: TokenFile | None) -> TokenFile | None:
            if current is None or (current.identity, current.generation) != (identity, generation):
                return None
            ids = current.session_ids + ((sid,) if sid and sid not in current.session_ids else ())
            # never regress the session id: a hook that wrote a newer one (a /clear) after this lease last read the
            # file is later in the file's own list than the lease's
            newer = current.session_id if (current.session_id and sid in current.session_ids and current.session_id in ids
                                           and ids.index(current.session_id) > ids.index(sid)) else sid
            return TokenFile(**{**current.__dict__, "token": token, "expires_at": expires,
                                "session_id": newer or current.session_id, "session_ids": ids, "written_at": 0.0})

        update_token_file(path, swap)

    def _fork_if_unknown(self, client: Any, report: TickReport) -> None:
        """``/branch``: a session id in the registry entry of this process that the index does not know."""
        if self._token_lapsed:
            return   # a fork needs a live token too: nothing to send, and the renewal must not wait on it
        entry = read_registry_entry(self._claude_pid, self._procs.sessions_dir)
        sid = entry.session_id if entry else None
        if not sid or sid in self._known_sessions:
            self._unknown_sid = None
            return
        self._fork_sid = sid
        try:
            client.session(sid)
            self._known_sessions.add(sid)
            self._unknown_sid = None
            return
        except IdentityApiError as exc:
            if exc.status != 404:
                raise
        if self._fork_grace and self._unknown_sid != sid:
            # One renewal of grace (develop run 44659989781): on /clear Claude writes the new id into its registry file
            # before the SessionStart hook binds it, and a fork row sent in that gap doubled the /clear's row. A /branch
            # runs no hook, so its id is still unknown at the next renewal and gets its fork row then.
            self._unknown_sid = sid
            return
        # the grace is spent: a refused bind is retried on the next renewal (the id joins _known_sessions once it binds)
        with self._lock:
            identity, generation, prior, held = self.identity, self.generation, self.session_id, self.token
        try:
            view = client.bind(identity, "fork", generation, evidence_body(
                session_id=sid, prior_session_id=prior, client_version=self._version, **self._process_evidence()),
                token=self._live_token())
        except IdentityApiError as exc:
            if self._lapse(exc, identity, held):
                return
            raise
        token, life = token_contract.token_from_response(view)
        with self._lock:
            self._known_sessions.add(sid)
            if (self.identity, self.generation) != (identity, generation):
                return  # moved to another identity while the call was out: the fork row belongs to the old one
            self.session_id = sid
            if token is not None:
                self.token, self.token_expires_at = token, token_contract.expires_at(life, now=self._clock())
        self._rewrite_file()
        report.forked = True
        _log.info("identity_lease_fork_row", identity=identity, session_id=sid, prior=prior)

    def _superseded(self, exc: IdentityApiError, identity: str | None, generation: int | None) -> None:
        """Flag this lease superseded, but only if it still holds the (identity, generation) the refused call was made for:
        a marker that moved it to another identity meanwhile makes the refusal about the old one, not about this."""
        with self._lock:
            if (self.identity, self.generation) != (identity, generation):
                return
            first = not self.superseded
            self.superseded = True
        if first:
            _log.error("identity_lease_superseded", identity=identity, generation=generation, detail=str(exc),
                       consequence="another process holds this identity now; this server stops writing identity state")

    # ── unbound: the pending bind ───────────────────────────────────────────

    def _pending_bind(self) -> bool:
        """Bind from the hook's pending file, when it names exactly this Claude process (pid and OS start time)."""
        entry = read_registry_entry(self._claude_pid, self._procs.sessions_dir)
        if entry is None or entry.started_at is None:
            return False
        start = normalise_instant(self._procs.os_start(self._claude_pid))
        if start is None:
            return False
        found = take_pending(self._idir, self._claude_pid, epoch_seconds(start))
        if found is None:
            return False
        path, pending = found
        facts = LaunchFacts(source=pending.source, session_id=pending.session_id, host=self._procs.host,
                            claude_pid=self._claude_pid, proc_start=start, client_version=self._version,
                            seat=pending.seat, name=pending.name, pane=pending.pane)
        facts.with_registry(entry)
        binder = SessionBinder(client=self._engine(), procs=self._procs, identity_dir=self._idir, allow_privileged=False,
                               clock=self._clock, sleep=self._sleep)
        outcome = binder.bind(facts)
        if outcome.action == "unbound":
            _log.warning("identity_pending_bind_failed", notices=outcome.notices)
            return False  # kept: the next heartbeat retries
        delete_pending(path)
        for notice in outcome.notices:
            _log.info("identity_pending_bind_notice", notice=notice)
        record = read_token_file(outcome.token_file) if outcome.token_file is not None else None
        if record is not None:
            self._adopt(record)
        _log.info("identity_pending_bind", identity=outcome.identity, row=outcome.row, generation=outcome.generation)
        return record is not None

    # ── shutdown ────────────────────────────────────────────────────────────

    def shutdown(self, *, wait_for_claude: bool = True) -> None:
        """A clean exit: append ``end`` with the expected generation and delete the token file. Done only when the
        Claude process is gone: a server shutting down under a live Claude (a ``/mcp`` reconnect) leaves the
        binding and the file for its successor, which has no hook to bind it again. A server that stopped writing
        because it was superseded sends nothing. Best effort and never raises."""
        got = self._tick_lock.acquire(timeout=1.0)        # a heartbeat stuck in a call must not hold the exit hostage
        try:
            if wait_for_claude:
                deadline = self._clock() + CLAUDE_EXIT_WAIT_S
                while self._procs.alive(self._claude_pid) and self._clock() < deadline:
                    self._sleep(0.1)
            if self._procs.alive(self._claude_pid):
                return
            with self._lock:
                identity, generation, superseded = self.identity, self.generation, self.superseded
            if identity is not None and generation is not None and not superseded:
                try:
                    self._engine().bind(identity, "end", generation,
                                        evidence_body(client_version=self._version, **self._process_evidence()))
                except IdentityApiError as exc:
                    _log.info("identity_end_refused", identity=identity, error=str(exc))
            path = self.token_path()
            if path is not None:
                delete_token_file(path)
            else:
                for stale in self._idir.glob(f"{self._claude_pid}.*.token"):
                    delete_token_file(stale)
        except Exception as exc:  # noqa: BLE001 — shutdown is best effort
            _log.warning("identity_shutdown_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._drop_engine()
            if got:
                self._tick_lock.release()


# ── the process's one lease ─────────────────────────────────────────────────

_LEASE: IdentityLease | None = None


def add_change_listener(listener: Callable[[str], None]) -> Callable[[], None]:
    """Call *listener* with the new identity id whenever a heartbeat or a handoff marker adopts another identity than
    the lease held (a cross-seat ``/resume``, a take-over, the first bind of an unbound server). Built on the one
    notifier, :func:`add_identity_listener`'s list, filtered to identity moves: a generation change of the same
    identity, and the identity already held when this is called, do not call it. It runs on the thread that adopted,
    outside the lease's lock, and an exception in it is logged and dropped. Returns the function that removes it.
    The aura loader listens (RDR-252)."""
    last: list[str | None] = [getattr(_LEASE, "identity", None)]

    def on_identity(identity: str | None) -> None:
        if identity and identity != last[0]:
            last[0] = identity
            listener(identity)

    _IDENTITY_LISTENERS.append(on_identity)

    def remove() -> None:
        with contextlib.suppress(ValueError):
            _IDENTITY_LISTENERS.remove(on_identity)

    return remove


_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_GUARD = threading.Lock()


def start() -> IdentityLease | None:
    """Start this server's identity lease on its own daemon thread. Idempotent. None when there is no Claude
    ancestor (a server started by hand) or identity is switched off with ``NX_IDENTITY_LEASE=0`` or ``NX_IDENTITY_BIND=0``."""
    global _LEASE, _THREAD
    with _GUARD:
        if _LEASE is not None:
            return _LEASE
        if os.environ.get("NX_IDENTITY_LEASE", "1") == "0" or (os.environ.get("NX_IDENTITY_BIND") or "").strip() == "0":
            return None  # NX_IDENTITY_BIND=0 is the one switch the SessionStart hook honours too (nexus.hooks.identity_bind)
        from nexus import config as _nx_config  # noqa: PLC0415 — deferred: server start cost; a module attribute so a patched config dir reaches it
        from importlib.metadata import PackageNotFoundError, version as _dist_version  # noqa: PLC0415
        from nexus.session import _command_name_of, find_immediate_claude_pid  # noqa: PLC0415

        pid = find_immediate_claude_pid(start_pid=os.getpid())
        if pid <= 0 or not _command_name_of(pid).lower().startswith("claude"):
            return None
        lease = IdentityLease(client_factory=IdentityClient.from_config, config_dir=_nx_config.nexus_config_dir(), claude_pid=pid,
                              client_version=_conexus_version(PackageNotFoundError, _dist_version), close_client=True)
        _STOP.clear()
        from nexus.identity import token_header  # noqa: PLC0415 — deferred: server start cost

        token_header.set_lease_owner(True)      # this server's spawn-time session id says nothing about its identity
        _THREAD = threading.Thread(target=_run, args=(lease,), name="nx-identity-lease", daemon=True)
        _LEASE = lease
        _THREAD.start()
        return lease


def _run(lease: IdentityLease) -> None:
    while not _STOP.wait(THREAD_POLL_S):
        if lease.due():
            lease.tick()


def adopt_marker(*, identity: str | None, generation: int | None, token_file: str | None) -> bool:
    """Called by the T1 handoff watcher for a marker that carries an identity. False when no lease runs."""
    lease = _LEASE
    return lease.adopt_marker(identity=identity, generation=generation, token_file=token_file) if lease else False


def shutdown() -> None:
    """Stop the thread and end the binding (:meth:`IdentityLease.shutdown`). Idempotent."""
    global _LEASE, _THREAD
    with _GUARD:
        lease, _LEASE = _LEASE, None
        thread, _THREAD = _THREAD, None
    _STOP.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    if lease is not None:
        lease.shutdown()
        from nexus.identity import token_header  # noqa: PLC0415

        token_header.set_lease_owner(False)


# ── The server's three call sites (mcp/core.py calls these and holds no identity logic of its own) ──────────────────


def start_safely() -> None:
    """The server's start path: :func:`start`, where a failure never costs the server (identity is additive)."""
    try:
        start()
    except Exception as exc:  # noqa: BLE001 - identity is additive; the server must start without it
        _log.warning("identity_lease_start_failed", error=str(exc))


async def follow_marker(marker: Any, log: Any) -> None:
    """The T1 handoff watcher found a marker: when the hook bound an identity (a /resume switch, a /clear), follow it,
    independent of the session id, which may be unchanged. The marker's token-file read stays off the event loop; a
    marker that cannot be followed is covered by the heartbeat's re-read."""
    if marker.identity is None:
        return
    try:
        await asyncio.to_thread(adopt_marker, identity=marker.identity, generation=marker.generation,
                                token_file=marker.token_file)
    except Exception as exc:  # noqa: BLE001 - the heartbeat's re-read covers a marker that could not be followed
        log.warning("identity_marker_adopt_failed", error=str(exc))


#: The exit path's identity shutdown (an ``end`` once the Claude process has exited, the token file's delete) has its
#: own thread and its own budget: it waits up to 1.5 s for Claude to exit before it decides, which the name release's
#: shared 3 s deadline cannot also hold.
SHUTDOWN_BUDGET_S: float = 5.0
_SHUTDOWN_THREAD: threading.Thread | None = None
_SHUTDOWN_DEADLINE: float = 0.0


def _shutdown_safely() -> None:
    try:
        shutdown()
    except Exception as exc:  # noqa: BLE001 - best-effort on exit; the binding lapses by lease, as a crash's does
        _log.warning("identity_shutdown_on_exit_failed", error=str(exc))


def start_shutdown() -> None:
    """Start the exit path's identity shutdown on its own thread. Idempotent while one runs; never blocks the exit."""
    global _SHUTDOWN_THREAD, _SHUTDOWN_DEADLINE
    running = _SHUTDOWN_THREAD
    if running is not None and running.is_alive():
        return
    thread = threading.Thread(target=_shutdown_safely, name="nx-mcp-identity-shutdown", daemon=True)
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001 - never block the exit
        _log.warning("identity_shutdown_on_exit_not_started", error=str(exc))
        return
    _SHUTDOWN_DEADLINE = time.monotonic() + SHUTDOWN_BUDGET_S
    _SHUTDOWN_THREAD = thread


def await_shutdown() -> None:
    """Wait for the exit path's identity shutdown until its own deadline, then give up."""
    thread = _SHUTDOWN_THREAD
    if thread is not None:
        thread.join(max(0.0, _SHUTDOWN_DEADLINE - time.monotonic()))


def current() -> IdentityLease | None:
    return _LEASE


def _conexus_version(missing: type[Exception], read: Callable[[str], str]) -> str:
    """The installed conexus version, ``unknown`` in a bare source tree. Read through importlib.metadata,
    not nexus.db.client_identity, which only an HTTP sender may import (tests/test_z0o2p24_client_version_header.py)."""
    try:
        return read("conexus")
    except missing:
        return "unknown"
