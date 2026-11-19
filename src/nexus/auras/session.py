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
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import structlog

from nexus.auras.aura import Aura
from nexus.db.t2.http_tuple_store import SchemaViolationError

_log = structlog.get_logger(__name__)


class AuraLoader:
    """Loads one session's auras once; see the module docstring.

    *discover* returns the parsed auras of both library roots; *is_on* says whether one is turned on; *entry*
    returns the registry's ``(name, pid)`` for the session id, or ``None``; *registered_owner* returns the nearest
    process at or above this server that has a registry entry, or ``None`` when the walk up cannot reach one;
    *make_runtime* builds the evaluator for the loaded auras and *attach* hands it to the waiter.
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
    ) -> None:
        self.session_id = session_id
        self.subs = subs
        self._discover = discover
        self._is_on = is_on
        self._entry = entry
        self._owner = registered_owner
        self._make_runtime = make_runtime
        self._attach = attach
        #: True once the load decision is final: auras loaded, none turned on, or refused for good.
        self.loaded = False
        self._miss_logged = False

    def _not_loaded(self, reason: str, **fields: Any) -> None:
        _log.info("auras_not_loaded", session_id=self.session_id, reason=reason, **fields)

    def on_resolve(self, name: str | None) -> None:
        """Called with each registry resolve of this session's name. Never raises."""
        if self.loaded:
            return
        if name is None:
            if not self._miss_logged:
                self._miss_logged = True
                self._not_loaded("session_not_named", detail="no session registry, or no interactive entry yet")
            return
        try:
            self._load()
        except Exception as exc:  # noqa: BLE001 -- an aura never stops the waiter
            # Config and files are read once a server start, so a retry would only repeat this line. Drop any
            # topic already held: with no sink attached, its rows would be neither evaluated nor announced.
            self.loaded = True
            self.subs.release_auras()
            _log.warning("auras_load_failed", session_id=self.session_id, error=str(exc))

    def _load(self) -> None:
        # The on/off filter first: every aura ships off, so most sessions stop here, before any process walk.
        auras = [a for a in self._discover() if self._is_on(a.name)]
        if not auras:
            self.loaded = True
            self._not_loaded("none_turned_on")
            return
        entry = self._entry(self.session_id)
        if entry is None:
            return  # the entry went between the name read and this one: the next resolve tries again
        _name, pid = entry
        owner = self._owner()
        self.loaded = True
        if owner is None:
            self._not_loaded("ancestry_unreadable", entry_pid=pid)
            return
        if owner != pid:
            self._not_loaded("not_this_sessions_process", entry_pid=pid, owner_pid=owner)
            return
        accepted: list[Aura] = []
        blocks: dict[str, str] = {}
        for aura in auras:
            if aura.threads in blocks:
                _log.warning(
                    "aura_refused", aura=aura.name,
                    reason=f"display.threads {aura.threads!r} is already written by aura {blocks[aura.threads]!r}",
                )
                continue
            try:
                self.subs.subscribe_aura(aura.name, aura.subspace)
            except SchemaViolationError as exc:
                _log.warning("aura_refused", aura=aura.name, subspace=aura.subspace, reason=str(exc))
                continue
            blocks[aura.threads] = aura.name
            accepted.append(aura)
        if not accepted:
            self._not_loaded("none_accepted")
            return
        self._attach(self._make_runtime(accepted))
        _log.info("auras_loaded", session_id=self.session_id, auras=[a.name for a in accepted])


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

    def make_runtime(auras: list[Aura]) -> AuraRuntime:
        read, put = t2_threads_io(store_ctx)
        return AuraRuntime(
            auras, writer=ThreadsBlockWriter(session_id, read_entries=read, put=put, identity=_own_identity(session_id)),
            notify=notify,
        )

    def attach(runtime: AuraRuntime) -> None:
        waiter.aura_sink = runtime.offer

    return AuraLoader(
        session_id, subs,
        discover=discover,
        is_on=lambda name: is_enabled(name, repo_root=top[0]),
        entry=lambda sid: resolve_session_entry(sid, log_miss=False),
        registered_owner=lambda: registered_owner(os.getpid(), _claude_sessions_dir()),
        make_runtime=make_runtime,
        attach=attach,
    )


#: Seconds a worker waits for the waiter's loop to take a channel notice: above the send's own gate bound
#: (``channel.GATE_WAIT_TIMEOUT_S``, 30 s), so a stalled gate answers first and a held notice is reported as held.
NOTICE_TIMEOUT_S: float = 35.0


def on_loop(loader: AuraLoader, loop: Any) -> Callable[[str | None], None]:
    """A callback for each registry resolve that runs :meth:`AuraLoader.on_resolve` on *loop*: the 60 s
    re-resolve runs on the lease thread, and the subscription set is read on the waiter's loop."""

    def call(name: str | None) -> None:
        if loader.loaded:
            return
        try:
            loop.call_soon_threadsafe(loader.on_resolve, name)
        except RuntimeError:  # the loop is closed: the waiter is gone
            pass

    return call
