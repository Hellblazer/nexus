# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""When a session's auras load (RDR-249 Technical Design 1 and 3; bead nexus-mdbds.11.1 part A).

The MCP server loads the auras turned on for a session the first time Claude Code's session registry names it:
at server start, or at a later 60 s re-resolve after a startup miss. A later miss does not unload them. A host
with no registry loads none and logs one INFO line saying why. A ``claude -p`` child that inherited its parent's
session id resolves to the parent's interactive entry, so the loader also requires that entry's pid to be an
ancestor of this server; when the ancestor chain cannot be read it loads none and says so. Each loaded aura holds
its trigger topic as an aura-owned subscription, and an aura past ``MAX_BOARD_TOPICS`` is refused by name.
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
    returns the registry's ``(name, pid)`` for the session id, or ``None``; *ancestors* returns a pid's parent
    chain (empty when it cannot be read); *make_runtime* builds the evaluator for the loaded auras and *attach*
    hands it to the waiter.
    """

    def __init__(
        self,
        session_id: str,
        subs: Any,
        *,
        discover: Callable[[], Sequence[Aura]],
        is_on: Callable[[str], bool],
        entry: Callable[[str], tuple[str, int] | None],
        ancestors: Callable[[int], frozenset[int]],
        my_pid: int,
        make_runtime: Callable[[list[Aura]], Any],
        attach: Callable[[Any], None],
    ) -> None:
        self.session_id = session_id
        self.subs = subs
        self._discover = discover
        self._is_on = is_on
        self._entry = entry
        self._ancestors = ancestors
        self._my_pid = my_pid
        self._make_runtime = make_runtime
        self._attach = attach
        #: True once the load decision is final: auras loaded, or refused for good (not this session's process).
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
            self.loaded = True  # config and files are read once a server start; a retry would only repeat this line
            _log.warning("auras_load_failed", session_id=self.session_id, error=str(exc))

    def _load(self) -> None:
        entry = self._entry(self.session_id)
        if entry is None:
            return  # the entry went between the name read and this one: the next resolve tries again
        _name, pid = entry
        chain = self._ancestors(self._my_pid)
        if not chain:
            self.loaded = True
            self._not_loaded("ancestry_unreadable", pid=self._my_pid)
            return
        if pid != self._my_pid and pid not in chain:
            self.loaded = True
            self._not_loaded("not_an_ancestor", entry_pid=pid, pid=self._my_pid)
            return
        auras = [a for a in self._discover() if self._is_on(a.name)]
        self.loaded = True
        accepted: list[Aura] = []
        for aura in auras:
            try:
                self.subs.subscribe_aura(aura.name, aura.subspace)
            except SchemaViolationError as exc:
                _log.warning("aura_refused", aura=aura.name, subspace=aura.subspace, reason=str(exc))
                continue
            accepted.append(aura)
        if not accepted:
            self._not_loaded("none_turned_on")
            return
        self._attach(self._make_runtime(accepted))
        _log.info("auras_loaded", session_id=self.session_id, auras=[a.name for a in accepted])


def _discover(cwd: Path) -> list[Aura]:
    """The parsed auras of both library roots; a refused file is logged by name and left out."""
    from nexus.auras import enumerate_auras  # noqa: PLC0415 -- the library loads only when a session is named
    from nexus.auras.check import check_clear  # noqa: PLC0415

    out: list[Aura] = []
    for entry in enumerate_auras(cwd=cwd):
        if not isinstance(entry.result, Aura):
            _log.warning("aura_file_refused", aura=entry.name, root=str(entry.root), reason=str(entry.result))
            continue
        # A [clear] a post could meet together with trigger.match has no tie-break here: refuse the aura, as
        # nx aura check does (nexus.auras.check.check_clear).
        if findings := check_clear(entry.result):
            _log.warning("aura_refused", aura=entry.name, reason="; ".join(f.detail for f in findings))
            continue
        out.append(entry.result)
    return out


def start_for_waiter(waiter: Any, subs: Any, *, store_ctx: Callable[[], Any], cwd: Path | None = None) -> AuraLoader:
    """An :class:`AuraLoader` for *waiter*'s session with the server's real dependencies: the library roots of
    *cwd*, the on/off resolver, the session registry, the process table, the run path, and a threads writer and
    a channel notice that go through the waiter. Call it on the waiter's loop; :meth:`AuraLoader.on_resolve` is
    then called on that loop too (see :func:`on_loop`)."""
    import asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415

    from nexus.auras.enable import is_enabled  # noqa: PLC0415
    from nexus.auras.runtime import AuraRuntime  # noqa: PLC0415
    from nexus.auras.threads_block import ThreadsBlockWriter, t2_threads_io  # noqa: PLC0415
    from nexus.daemon.service_registry import ancestor_pids  # noqa: PLC0415
    from nexus.mcp.subscriptions import resolve_session_entry  # noqa: PLC0415

    from nexus.lua_runtime.header import probe_repo  # noqa: PLC0415

    where = cwd if cwd is not None else Path.cwd()
    # The repo's .nexus.yml is at its git top level, the root enumerate_auras finds from *where*.
    probe = probe_repo(where)
    top = probe.root if probe else where
    loop = asyncio.get_running_loop()
    session_id = waiter.session_id

    def notify(text: str, meta: dict[str, str]) -> None:
        asyncio.run_coroutine_threadsafe(waiter._send(text, meta), loop)  # noqa: SLF001 -- the waiter's own send

    def make_runtime(auras: list[Aura]) -> AuraRuntime:
        read, put = t2_threads_io(store_ctx)
        return AuraRuntime(auras, writer=ThreadsBlockWriter(session_id, read_entries=read, put=put), notify=notify)

    def attach(runtime: AuraRuntime) -> None:
        waiter.aura_sink = runtime.offer

    return AuraLoader(
        session_id, subs,
        discover=lambda: _discover(where),
        is_on=lambda name: is_enabled(name, repo_root=top),
        entry=lambda sid: resolve_session_entry(sid, log_miss=False),
        ancestors=ancestor_pids,
        my_pid=os.getpid(),
        make_runtime=make_runtime,
        attach=attach,
    )


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
