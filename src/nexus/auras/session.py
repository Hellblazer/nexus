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
