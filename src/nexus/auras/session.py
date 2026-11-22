# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""When a session's auras load (RDR-249 Technical Design 1 and 3; bead nexus-mdbds.11.1 part A).

The MCP server loads the auras turned on for a session the first time Claude Code's session registry names it:
at server start, or at a later 60 s re-resolve after a startup miss. A later miss does not unload them. A host
with no registry loads none and logs one INFO line saying why. A ``claude -p`` child that inherited its parent's
session id resolves to the parent's interactive entry, so the loader also requires that entry's pid to be an
ancestor of this server, and the nearest one that has a registry entry of any kind: a ``claude -p`` started from
the session has the session's claude in its ancestor chain too, but its own claude, which writes a registry file
under its own pid and session id (observed 2026-11-15, with kind ``interactive``), is nearer. When the chain cannot be read up to a registered process, it loads none and says
so. Each loaded aura holds its trigger topic as an aura-owned subscription. An aura past ``MAX_BOARD_TOPICS``, or
one whose ``display.threads`` value another loaded aura already writes, is refused by name.

An aura whose file carries ``[load] group = "<name>"`` (RDR-252) loads only when the session's identity is a member
of that group and the group is live (:mod:`nexus.auras.load_gate`). The identity, the memberships (one read) and each
named group's record (one read per group) are injected, so the stage is tested without an engine. A server that
started unbound, or whose reads fail, loads no group-gated aura and says why; an ungated aura is not affected. Every
decision is recorded (:attr:`AuraLoader.decisions`) and handed to the state-file writer (``state_sink``), which
``nx aura list`` reads (:mod:`nexus.auras.loaded_state`).
"""

from __future__ import annotations

import dataclasses
import threading
import weakref
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from nexus.auras.aura import Aura
from nexus.auras.load_gate import GroupReader, default_group_reader, group_is_live
from nexus.auras.loaded_state import AuraState, LoadedState
from nexus.db.t2.http_tuple_store import SchemaViolationError

_log = structlog.get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class _Plan:
    """What a load's reads found (:meth:`AuraLoader._prepare`), for :meth:`AuraLoader._commit` to apply. ``kind`` is
    ``load`` (the rest of the fields are set), ``retry`` (try at the next resolve), or the reason nothing loads."""

    kind: str
    auras: Sequence[Aura] = ()
    pid: int = 0
    identity: str | None = None
    gate: dict[str, AuraState] = dataclasses.field(default_factory=dict)
    fields: dict[str, Any] = dataclasses.field(default_factory=dict)


class AuraLoader:
    """Loads one session's auras once; see the module docstring.

    *discover* returns the parsed auras of both library roots; *is_on* says whether one is turned on; *entry*
    returns the registry's ``(name, pid)`` for the session id, or ``None``; *registered_owner* returns the nearest
    process at or above this server that has a registry entry, or ``None`` when the walk up cannot reach one;
    *make_runtime* builds the evaluator for the loaded auras and *attach* hands it to the waiter.

    RDR-252: *identity* returns the session's bound identity id, or ``None`` when it is unbound; *memberships*
    returns the names of the groups that identity belongs to (one call per load); *read_group* returns one group's
    record, ``None`` for no record, or raises (one call per group a loading aura names); *now* is the clock for a
    group's ``ends``; *state_sink* receives ``(claude pid, LoadedState)`` after each decision and after an identity
    change (:meth:`note_identity`). Each failing read is a reason, never an exception out of the loader.
    """

    def __init__(
        self,
        session_id: str,
        subs: Any,
        *,
        discover: Callable[[], Sequence[Aura]],
        is_on: Callable[[str], bool],
        entry: Callable[[str], tuple[str, int] | None],
        registered_owner: Callable[[], int | None],
        make_runtime: Callable[[list[Aura]], Any],
        attach: Callable[[Any], None],
        identity: Callable[[], str | None] | None = None,
        memberships: Callable[[str], Iterable[str]] | None = None,
        read_group: GroupReader | None = None,
        now: Callable[[], datetime] | None = None,
        state_sink: Callable[[int, LoadedState], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.subs = subs
        self._discover = discover
        self._is_on = is_on
        self._entry = entry
        self._owner = registered_owner
        self._make_runtime = make_runtime
        self._attach = attach
        self._identity = identity if identity is not None else (lambda: None)
        self._memberships = memberships
        self._read_group = read_group
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._state_sink = state_sink
        #: What each turned-on aura's load came to, by name: loaded or not, why, and its ``[load]`` group.
        self.decisions: dict[str, AuraState] = {}
        #: The identity the load was evaluated for (``None`` when the server was unbound).
        self.loaded_identity: str | None = None
        self._state: LoadedState | None = None
        self._state_pid: int | None = None
        #: The on auras the load saw, the threads values they claimed, the runtime built for them (None until one was),
        #: the identity the lease reported before a load, and whether the one late re-gate has run.
        self._discovered: Sequence[Aura] = ()
        self._blocks: dict[str, str] = {}
        self._runtime: Any = None
        self._noted_identity: str | None = None
        self._regated = False
        self._loading = False
        #: Removes the lease listener that feeds :meth:`note_identity` (set by :func:`start_for_waiter`).
        self._release: Callable[[], None] | None = None
        #: True once the load decision is final: auras loaded, none turned on, or refused for good.
        self.loaded = False
        self._miss_logged = False

    def _not_loaded(self, reason: str, **fields: Any) -> None:
        _log.info("auras_not_loaded", session_id=self.session_id, reason=reason, **fields)

    def on_resolve(self, name: str | None) -> None:
        """Called with each registry resolve of this session's name. Never raises. This form reads and commits on the
        calling thread (a test, or a caller with no loop); the server uses :meth:`on_resolve_async` through
        :func:`on_loop`, which does the reads on a worker thread."""
        if not self._resolve_miss(name):
            return
        try:
            plan = self._prepare()
        except Exception as exc:  # noqa: BLE001 -- an aura never stops the waiter
            self._load_failed(exc)
            return
        self._finish(plan)

    async def on_resolve_async(self, name: str | None) -> None:
        """:meth:`on_resolve` with every blocking read (the library files, the process walk, the membership and group
        reads) on a worker thread, and the commit (the subscription set, the runtime, the state) back on the calling
        loop. A resolve that arrives while one is in flight is dropped: the next one tries again."""
        import asyncio  # noqa: PLC0415

        if self._loading or not self._resolve_miss(name):
            return
        self._loading = True
        try:
            try:
                plan = await asyncio.to_thread(self._prepare)
            except Exception as exc:  # noqa: BLE001 -- an aura never stops the waiter
                self._load_failed(exc)
                return
            self._finish(plan)
            if self._noted_identity is not None:  # the lease bound while the reads ran: the commit may have seen none
                await self.note_identity_async(self._noted_identity)
        finally:
            self._loading = False

    def _resolve_miss(self, name: str | None) -> bool:
        """False when there is nothing to load for this resolve: already decided, or the session is not named yet."""
        if self.loaded:
            return False
        if name is None:
            if not self._miss_logged:
                self._miss_logged = True
                self._not_loaded("session_not_named", detail="no session registry, or no interactive entry yet")
            return False
        return True

    def _load_failed(self, exc: Exception) -> None:
        # Config and files are read once a server start, so a retry would only repeat this line. Drop any
        # topic already held: with no sink attached, its rows would be neither evaluated nor announced.
        self.loaded = True
        self.subs.release_auras()
        _log.warning("auras_load_failed", session_id=self.session_id, error=str(exc))
        if self._state is not None:
            self.decisions = {
                name: AuraState(False, "load_failed", d.group) if d.loaded else d for name, d in self.decisions.items()
            }
            self._state = dataclasses.replace(self._state, auras=dict(self.decisions))
            self._emit_state()

    def _finish(self, plan: _Plan) -> None:
        try:
            self._commit(plan)
        except Exception as exc:  # noqa: BLE001 -- an aura never stops the waiter
            self._load_failed(exc)

    def _prepare(self) -> _Plan:
        """Every read of a load, touching no loader state: the library files, the on/off filter, the registry entry, the
        process walk, the identity, and the gate (one membership read, one group read per group). Safe on any thread."""
        # The on/off filter first: every aura ships off, so most sessions stop here, before any process walk.
        auras = [a for a in self._discover() if self._is_on(a.name)]
        if not auras:
            return _Plan("none_turned_on")
        entry = self._entry(self.session_id)
        if entry is None:
            return _Plan("retry")  # the entry went between the name read and this one: the next resolve tries again
        _name, pid = entry
        owner = self._owner()
        if owner is None:
            return _Plan("ancestry_unreadable", fields={"entry_pid": pid})
        if owner != pid:
            return _Plan("not_this_sessions_process", fields={"entry_pid": pid, "owner_pid": owner})
        # An identity the lease reported before this load (a pending bind that beat the registry) counts when the token
        # read misses it: the lease wrote the token file before it told us, so a miss is a read that raced the write.
        identity = self._identity_or_none() or self._noted_identity
        return _Plan("load", auras=auras, pid=pid, identity=identity, gate=self._gate(auras, identity))

    def _commit(self, plan: _Plan) -> None:
        """Apply a plan on the loop: the decisions, the subscriptions, the runtime and the state."""
        if plan.kind == "retry":
            return
        self.loaded = True
        if plan.kind != "load":
            self._not_loaded(plan.kind, **plan.fields)
            return
        self.loaded_identity = plan.identity
        self._discovered = plan.auras
        accepted = self._admit(plan.auras, plan.gate)
        self._publish_state(plan.pid, plan.identity)
        if not accepted:
            self._not_loaded("none_accepted")
            return
        self._runtime = self._make_runtime(accepted)
        self._attach(self._runtime)
        _log.info("auras_loaded", session_id=self.session_id, auras=[a.name for a in accepted])

    def _admit(self, auras: Sequence[Aura], gate: dict[str, AuraState]) -> list[Aura]:
        """Record each aura's decision and subscribe the ones that pass: the gate's verdict, then the threads-block
        and subscription refusals. Returns the auras now loaded."""
        accepted: list[Aura] = []
        for aura in auras:
            verdict = gate.get(aura.name)
            if verdict is not None and not verdict.loaded:
                self.decisions[aura.name] = verdict
                _log.info("aura_not_loaded", aura=aura.name, reason=verdict.reason, group=verdict.group)
                continue
            if aura.threads in self._blocks:
                _log.warning(
                    "aura_refused", aura=aura.name,
                    reason=f"display.threads {aura.threads!r} is already written by aura {self._blocks[aura.threads]!r}",
                )
                self.decisions[aura.name] = AuraState(False, "refused_threads", aura.load_group)
                continue
            try:
                self.subs.subscribe_aura(aura.name, aura.subspace)
            except SchemaViolationError as exc:
                _log.warning("aura_refused", aura=aura.name, subspace=aura.subspace, reason=str(exc))
                self.decisions[aura.name] = AuraState(False, "refused_subscription", aura.load_group)
                continue
            self._blocks[aura.threads] = aura.name
            accepted.append(aura)
            self.decisions[aura.name] = AuraState(True, "loaded", aura.load_group)
        return accepted

    # -- RDR-252: the group gate and the state file -------------------------------------------------------------------

    def _identity_or_none(self) -> str | None:
        try:
            return self._identity()
        except Exception as exc:  # noqa: BLE001 -- an identity that cannot be read is an unbound server
            _log.warning("auras_identity_unreadable", session_id=self.session_id, error=str(exc))
            return None

    def _gate(self, auras: Sequence[Aura], identity: str | None) -> dict[str, AuraState]:
        """The verdict of each group-gated aura in *auras*, by name: one membership read, then one group read per
        distinct group a not-yet-excluded aura names. An ungated aura has no entry and reads nothing."""
        gated = [a for a in auras if a.load_group is not None]
        out: dict[str, AuraState] = {}
        if not gated:
            return out

        def refuse(aura: Aura, reason: str) -> None:
            out[aura.name] = AuraState(False, reason, aura.load_group)

        if identity is None:
            for aura in gated:
                refuse(aura, "unbound")
            return out
        member_of: set[str] | None = None
        if self._memberships is not None:
            try:
                member_of = set(self._memberships(identity))
            except Exception as exc:  # noqa: BLE001 -- GroupUnreadable, an engine error or a bug: loads nothing gated
                _log.warning("auras_memberships_unreadable", session_id=self.session_id, error=str(exc))
        if member_of is None:
            for aura in gated:
                refuse(aura, "memberships_unreadable")
            return out
        records: dict[str, AuraState] = {}
        for aura in gated:
            group = aura.load_group
            assert group is not None
            if group not in member_of:
                refuse(aura, "not_a_member")
                continue
            if group not in records:
                records[group] = self._group_verdict(group)
            out[aura.name] = records[group]
        return out

    def _group_verdict(self, group: str) -> AuraState:
        if self._read_group is None:
            return AuraState(False, "group_unreadable", group)
        try:
            record = self._read_group(group)
        except Exception as exc:  # noqa: BLE001 -- GroupUnreadable, an engine error or a bug
            _log.warning("auras_group_unreadable", session_id=self.session_id, group=group, error=str(exc))
            return AuraState(False, "group_unreadable", group)
        if record is None:
            return AuraState(False, "no_group_record", group)
        if not group_is_live(record, self._now()):
            return AuraState(False, "group_disbanded", group)
        return AuraState(True, "loaded", group)

    def _publish_state(self, pid: int, identity: str | None) -> None:
        self._state_pid = pid
        self._state = LoadedState(identity=identity, loaded_identity=identity, auras=dict(self.decisions))
        self._emit_state()

    def _emit_state(self) -> None:
        if self._state_sink is None or self._state is None or self._state_pid is None:
            return
        try:
            self._state_sink(self._state_pid, self._state)
        except Exception as exc:  # noqa: BLE001 -- the state file is a convenience for nx aura list
            _log.warning("auras_state_write_failed", session_id=self.session_id, error=str(exc))

    def note_identity(self, identity: str | None) -> None:
        """The server's heartbeat saw its identity change: rewrite the state file with the new ``identity``. The loaded
        set and ``loaded_identity`` stay the ones the auras were built for, so ``nx aura list`` can report the
        mismatch until the server reconnects. Before a load has recorded anything it only remembers the identity, for
        the load to use if its token read misses it. Never raises.

        The one exception to "the loaded set stays": a server whose load saw NO identity (a Desktop server, or a pending
        bind the lease had not made yet) gates its group-gated auras again, once, when the lease reports its first bind,
        since nothing else would ever load them in this server's life."""
        pending = self._note(identity)
        if pending:
            self._regate(pending, identity, self._gate(pending, identity))
        self._rewrite_identity(identity)

    async def note_identity_async(self, identity: str | None) -> None:
        """:meth:`note_identity` with the late gate's reads on a worker thread (the lease's listener runs this on the
        loop through :func:`_follow_identity`)."""
        import asyncio  # noqa: PLC0415

        pending = self._note(identity)
        if pending:
            assert identity is not None
            try:
                gate = await asyncio.to_thread(self._gate, pending, identity)
            except Exception as exc:  # noqa: BLE001 -- _gate does not raise; a bug here is not the waiter's to die of
                _log.warning("auras_late_gate_failed", session_id=self.session_id, error=str(exc))
                return
            self._regate(pending, identity, gate)
        self._rewrite_identity(identity)

    def close(self) -> None:
        """Stop listening to the identity lease. The server's loader lives as long as the server; a test that starts
        one ends it so the lease's listener list does not grow."""
        release, self._release = self._release, None
        if release is not None:
            release()

    def flush_state(self, timeout: float = 10.0) -> bool:
        """Wait for the state writes handed to a threaded sink to finish (a sink with nothing to wait for is idle)."""
        wait = getattr(self._state_sink, "wait_idle", None)
        return True if wait is None else bool(wait(timeout))

    def _note(self, identity: str | None) -> list[Aura] | None:
        """Remember *identity*; return the auras to gate again (once, on the first bind of a server that loaded with no
        identity) or None."""
        if identity is not None:
            self._noted_identity = identity
        if self._state is None or identity is None or self.loaded_identity is not None or self._regated:
            return None
        self._regated = True
        return [a for a in self._discovered if self.decisions.get(a.name) == AuraState(False, "unbound", a.load_group)] or None

    def _rewrite_identity(self, identity: str | None) -> None:
        if self._state is None or self._state.identity == identity:
            return
        self._state = dataclasses.replace(self._state, identity=identity)
        self._emit_state()

    def _regate(self, pending: list[Aura], identity: str, gate: dict[str, AuraState]) -> None:
        self.loaded_identity = identity
        accepted: list[Aura] = []
        try:
            accepted = self._admit(pending, gate)
            if accepted:
                if self._runtime is not None:
                    self._runtime.extend(accepted, identity)
                else:
                    self._runtime = self._make_runtime(accepted)
                    self._attach(self._runtime)
                _log.info("auras_loaded_after_bind", session_id=self.session_id, auras=[a.name for a in accepted])
        except Exception as exc:  # noqa: BLE001 -- an aura never stops the waiter
            _log.warning("auras_late_load_failed", session_id=self.session_id, error=str(exc))
            for aura in accepted:  # its subscription stays held, but with no runtime to evaluate it, it is not loaded
                self.decisions[aura.name] = AuraState(False, "load_failed", aura.load_group)
        assert self._state is not None
        self._state = dataclasses.replace(
            self._state, loaded_identity=identity, auras=dict(self.decisions),
        )
        self._emit_state()


def _discover(cwd: Path) -> tuple[list[Aura], Path]:
    """The parsed auras of both library roots for *cwd*, and the repo's top level (*cwd* outside a repo or when
    the probe fails). A refused file is logged by name and left out."""
    from nexus.auras import enumerate_auras  # noqa: PLC0415 -- the library loads only when a session is named
    from nexus.auras.check import check_clear  # noqa: PLC0415
    from nexus.lua_runtime.header import probe_repo  # noqa: PLC0415

    try:
        probe = probe_repo(cwd)
    except Exception as exc:  # noqa: BLE001 -- RepoProbeError and the like: the shipped root still loads
        _log.warning("auras_repo_probe_failed", cwd=str(cwd), error=str(exc))
        probe = None
    out: list[Aura] = []
    for entry in enumerate_auras(cwd=cwd, probe=probe):
        if not isinstance(entry.result, Aura):
            _log.warning("aura_file_refused", aura=entry.name, root=str(entry.root), reason=str(entry.result))
            continue
        # A [clear] a post could meet together with trigger.match: refused, as nx aura check refuses it
        # (nexus.auras.check.check_clear), so no post both fires and clears.
        if findings := check_clear(entry.result):
            _log.warning("aura_refused", aura=entry.name, reason="; ".join(f.detail for f in findings))
            continue
        out.append(entry.result)
    return out, (probe.root if probe else cwd)


#: Most parent hops :func:`registered_owner` walks before giving up.
MAX_HOPS: int = 64


def registered_owner(
    pid: int, sessions_dir: Path, *, ppid_of: Callable[[int], int | None] | None = None,
) -> int | None:
    """The nearest process at or above *pid* whose ``<sessions_dir>/<pid>.json`` exists, or ``None`` when the walk
    cannot reach one (a parent unreadable, the root passed, or :data:`MAX_HOPS` hops)."""
    if ppid_of is None:
        from nexus.session import _ppid_of as ppid_of  # noqa: PLC0415 -- the shared parent probe
    current: int | None = pid
    seen: set[int] = set()
    while current is not None and current not in seen and len(seen) < MAX_HOPS:
        if (sessions_dir / f"{current}.json").exists():
            return current
        seen.add(current)
        try:
            current = ppid_of(current)
        except Exception as exc:  # noqa: BLE001 -- an unreadable parent ends the walk
            _log.debug("aura_owner_walk_failed", pid=current, error=str(exc))
            return None
        if current is not None and current <= 1:
            return None
    return None


def _own_identity(session_id: str) -> str:
    """This process's identity (RDR-253 Step 5): the leased token file first, then the one a token file binds the
    session id to; ``""`` when neither resolves."""
    from nexus.identity.board import identity_for_session  # noqa: PLC0415
    from nexus.identity.token_header import current  # noqa: PLC0415

    try:
        record = current()
        return str(record.identity) if record is not None else identity_for_session(session_id)
    except Exception:  # noqa: BLE001 -- no identity is the Phase 1 fallback, never a failed load
        return ""


def start_for_waiter(waiter: Any, subs: Any, *, store_ctx: Callable[[], Any], cwd: Path | None = None) -> AuraLoader:
    """An :class:`AuraLoader` for *waiter*'s session with the server's real dependencies: the library roots of
    *cwd*, the on/off resolver read at the repo's top level, the session registry, the process table, the run
    path, and a threads writer and a channel notice that go through the waiter. Call it on the waiter's loop;
    :meth:`AuraLoader.on_resolve` is then called on that loop too (see :func:`on_loop`). Nothing is read here:
    the files, the repo probe and the process walk run at the first resolve that names the session."""
    import asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415

    from nexus.auras.enable import is_enabled  # noqa: PLC0415
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415
    from nexus.auras.threads_block import ThreadsBlockWriter, t2_threads_io  # noqa: PLC0415
    from nexus.mcp.subscriptions import _claude_sessions_dir, resolve_session_entry  # noqa: PLC0415

    where = cwd if cwd is not None else Path.cwd()
    top: list[Path] = [where]
    loop = asyncio.get_running_loop()
    session_id = waiter.session_id

    def discover() -> list[Aura]:
        auras, top[0] = _discover(where)
        return auras

    def notify(text: str, meta: dict[str, str]) -> None:
        """Send on the waiter's loop and wait for the answer, so a lost notice is logged by the runtime. A gate
        that is shut holds the notice (``hold``) and it goes out when the gate opens."""
        if loop.is_closed():
            raise RuntimeError("the waiter's loop is closed")
        future = asyncio.run_coroutine_threadsafe(
            waiter._send(text, meta, hold=lambda: None), loop,  # noqa: SLF001 -- the waiter's own send
        )
        try:
            sent = future.result(timeout=NOTICE_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            raise RuntimeError(f"the waiter's loop did not take the notice in {NOTICE_TIMEOUT_S:g} s") from None
        if not sent:
            raise RuntimeError("the channel did not take the notice now: held until the gate opens, or no stream")

    loader_cell: list[AuraLoader] = []

    def make_runtime(auras: list[Aura]) -> AuraRuntime:
        read, put = t2_threads_io(store_ctx)
        return AuraRuntime(
            auras,
            writer=ThreadsBlockWriter(
                session_id, read_entries=read, put=put, identity_source=lambda: _own_identity(session_id),
            ),
            notify=notify,
            identity=loader_cell[0].loaded_identity if loader_cell else None,
        )

    def attach(runtime: AuraRuntime) -> None:
        waiter.aura_sink = runtime.offer

    loader = AuraLoader(
        session_id, subs,
        discover=discover,
        is_on=lambda name: is_enabled(name, repo_root=top[0]),
        entry=lambda sid: resolve_session_entry(sid, log_miss=False),
        registered_owner=lambda: registered_owner(os.getpid(), _claude_sessions_dir()),
        make_runtime=make_runtime,
        attach=attach,
        identity=bound_identity,
        memberships=read_memberships,
        read_group=default_group_reader(),
        state_sink=threaded_sink(state_writer(session_id)),
    )
    loader_cell.append(loader)
    loader._release = _follow_identity(loader, loop)  # noqa: SLF001 -- the loader's own listener handle
    return loader


def bound_identity() -> str | None:
    """The session's bound identity id from this process's token file, or ``None`` when it holds no usable token
    (an unbound server, or a Desktop server that did not make the bind itself from the pending file)."""
    from nexus.identity import token_header  # noqa: PLC0415 -- deferred: the identity package is a leaf

    record = token_header.current()
    return record.identity if record is not None else None


def read_memberships(identity: str) -> list[str]:
    """The names of the groups *identity* belongs to, by one read of RDR-253's membership route. Raises
    :class:`~nexus.auras.load_gate.GroupUnreadable` when the engine cannot answer."""
    from nexus.auras.load_gate import GroupUnreadable  # noqa: PLC0415
    from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError  # noqa: PLC0415

    client = None
    try:
        client = IdentityClient.from_config()
        rows = client.memberships(identity)
        if not all(isinstance(r, dict) and isinstance(r.get("group"), str) for r in rows):
            raise GroupUnreadable("a membership row names no group")
        return [r["group"] for r in rows]
    except (IdentityApiError, IdentityUnavailableError) as exc:
        raise GroupUnreadable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if client is not None:
            client.close()


#: Every state file this process wrote, by path, with the server pid that wrote it. ``atexit`` does not run when the
#: server ends in ``os._exit`` (the SIGTERM path), so :func:`remove_own_state_files` is also called from the MCP
#: server's exit teardown; both read this one record.
_OWN_STATE_FILES: dict[Path, int] = {}

#: The state writers and sinks that can still write a file, so removal can close them first (see
#: :func:`remove_own_state_files`). Weak: a writer or sink nothing holds any more has nothing left to close.
_CLOSABLES: weakref.WeakSet[Any] = weakref.WeakSet()

#: How long removal waits for a write already running on a sink before it removes anyway. The write checks the closed
#: flag again after it lands its file and removes it itself, so the wait only spares a second removal; it is not what
#: makes removal final.
_CLOSE_WAIT_S = 1.0


def remove_own_state_files() -> int:
    """Close every state writer and sink, then remove each state file this process wrote and still owns (a successor's
    file stays). Once this starts no later write recreates a file: a write not yet started or still queued does
    nothing, and one already running removes its own file when it lands. Safe to call twice: a file already gone
    removes nothing. Returns how many files were removed."""
    from nexus.auras.loaded_state import remove_own  # noqa: PLC0415

    closables = list(_CLOSABLES)
    for c in closables:
        c.close()
    for c in closables:
        wait = getattr(c, "wait_idle", None)
        if wait is not None:
            wait(_CLOSE_WAIT_S)
    removed = 0
    for path, server_pid in list(_OWN_STATE_FILES.items()):
        try:
            removed += bool(remove_own(path, server_pid=server_pid))
        except OSError as exc:
            _log.warning("auras_state_remove_failed", path=str(path), error=str(exc))
    return removed


class _StateWriter:
    """The state file writer: it names the holder (the Claude process the registry file records) and writes
    ``<config>/auras/loaded-<claude pid>.<startedAt>.json``. :meth:`close` ends it: a later write does nothing, and a
    write already running removes the file it just landed."""

    def __init__(self, session_id: str, on_exit: Callable[..., Any]) -> None:
        self._session_id = session_id
        self._on_exit = on_exit
        self._closed = threading.Event()
        self._registered: set[Path] = set()
        _CLOSABLES.add(self)

    def close(self) -> None:
        self._closed.set()

    def __call__(self, pid: int, state: LoadedState) -> None:
        import os  # noqa: PLC0415

        from nexus import config as _config  # noqa: PLC0415 -- resolved at call time, so a redirected config dir is honoured
        from nexus.auras.loaded_state import remove_own, state_dir, state_path, write_state  # noqa: PLC0415
        from nexus.identity.holder import read_registry_entry, this_host  # noqa: PLC0415

        if self._closed.is_set():
            return
        entry = read_registry_entry(pid)
        if entry is None or entry.started_at is None:
            _log.info("auras_state_not_written", session_id=self._session_id, reason="no registry file for the claude process")
            return
        path = state_path(state_dir(_config.nexus_config_dir()), pid, entry.started_at)
        write_state(path, dataclasses.replace(
            state, host=this_host(), pid=pid, started_at=entry.started_at, proc_start=entry.proc_start,
            server_pid=os.getpid(), session_id=self._session_id,
        ))
        _OWN_STATE_FILES[path] = os.getpid()
        if path not in self._registered:
            self._registered.add(path)
            self._on_exit(remove_own, path, server_pid=os.getpid())
        if self._closed.is_set():  # removal started while this write ran: it may already have passed this file
            remove_own(path, server_pid=os.getpid())


def state_writer(
    session_id: str, *, register_exit: Callable[..., Any] | None = None
) -> Callable[[int, LoadedState], None]:
    """The ``state_sink`` of the real loader: it writes the state file and removes it when the server exits.
    *register_exit* registers the removal (``atexit.register``, or a stand-in in a test)."""
    import atexit  # noqa: PLC0415

    return _StateWriter(session_id, register_exit if register_exit is not None else atexit.register)


class _Sink:
    """A state sink that hands each write to one worker thread, in order, so the file I/O of a load or an identity change
    never runs on the waiter's loop. ``wait_idle`` is for a test and for shutdown; ``close`` ends it: queued writes are
    dropped, a call after it does nothing, and the write behind it is closed too."""

    def __init__(self, write: Callable[[int, LoadedState], None]) -> None:
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        self._write = write
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aura-state")
        self._pending: list[Any] = []
        self._closed = threading.Event()
        _CLOSABLES.add(self)

    def __call__(self, pid: int, state: LoadedState) -> None:
        if self._closed.is_set():
            return
        self._pending = [f for f in self._pending if not f.done()]
        self._pending.append(self._pool.submit(self._run, pid, state))

    def _run(self, pid: int, state: LoadedState) -> None:
        if self._closed.is_set():
            return
        try:
            self._write(pid, state)
        except Exception as exc:  # noqa: BLE001 -- the state file is a convenience for nx aura list
            _log.warning("auras_state_write_failed", error=str(exc))

    def close(self) -> None:
        self._closed.set()
        for f in list(self._pending):
            f.cancel()
        close = getattr(self._write, "close", None)
        if close is not None:
            close()

    def wait_idle(self, timeout: float) -> bool:
        from concurrent.futures import wait  # noqa: PLC0415

        _done, not_done = wait(list(self._pending), timeout=timeout)
        return not not_done


def threaded_sink(write: Callable[[int, LoadedState], None]) -> _Sink:
    """*write* (a state writer) run on one worker thread; see :class:`_Sink`."""
    return _Sink(write)


def _spawn(loop: Any, coro_fn: Callable[..., Any], *args: Any, keep: set[Any]) -> None:
    """Start ``coro_fn(*args)`` as a task on *loop* from any thread. The task is held in *keep* until it ends, since the
    loop keeps only a weak reference to it."""

    def start() -> None:
        task = loop.create_task(coro_fn(*args))
        keep.add(task)
        task.add_done_callback(keep.discard)

    loop.call_soon_threadsafe(start)


def _follow_identity(loader: AuraLoader, loop: Any) -> Callable[[], None]:
    """Rewrite the state file (and, for a server that loaded unbound, gate once more) when the identity lease adopts
    another identity (RDR-252: "on each identity change the heartbeat sees"). Returns the function that removes the
    listener, which the loader keeps (:meth:`AuraLoader.close`). A server with no lease (one started by hand) has
    nothing to follow."""
    from nexus.mcp import identity_lease  # noqa: PLC0415 -- deferred: the MCP server's lease

    tasks: set[Any] = set()

    def changed(identity: str) -> None:
        try:
            _spawn(loop, loader.note_identity_async, identity, keep=tasks)
        except RuntimeError:  # the loop is closed: the waiter is gone
            pass

    return identity_lease.add_change_listener(changed)


#: Seconds a worker waits for the waiter's loop to take a channel notice: above the send's own gate bound
#: (``channel.GATE_WAIT_TIMEOUT_S``, 30 s), so a stalled gate answers first and a held notice is reported as held.
NOTICE_TIMEOUT_S: float = 35.0


def on_loop(loader: AuraLoader, loop: Any) -> Callable[[str | None], None]:
    """A callback for each registry resolve that runs :meth:`AuraLoader.on_resolve_async` on *loop*: the 60 s
    re-resolve runs on the server's name-watch thread, and the subscription set is read on the waiter's loop. The
    loader's reads (files, process walk, membership and group reads) run on a worker thread; only the commit is on the
    loop. The callback's ``loaded()`` says the loader has decided, so the watch can stop."""
    tasks: set[Any] = set()

    def call(name: str | None) -> None:
        if loader.loaded:
            return
        try:
            _spawn(loop, loader.on_resolve_async, name, keep=tasks)
        except RuntimeError:  # the loop is closed: the waiter is gone
            pass

    call.loaded = lambda: loader.loaded  # type: ignore[attr-defined] -- read by the server's name watch
    return call
