# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``indexed_at`` write-backs a manifest rung owes, kept across runs (nexus-f3yqd.5).

The engine restamps a document's ``indexed_at`` on every manifest write, and the manifest rungs write the
value read at scan back after each write. When the engine dies between the two, the write-back cannot be
made, and a later scan would read the engine's run-time stamp as the document's own, which switches off
the chunk-newer guard for its other rows. So the rung records the stamp it will owe BEFORE the write,
while the engine (and T2 behind it) still answers: a record made after the failure could not be written.
The record clears once the write-back succeeds, and the rung pays what is left before its next scan, in
this run or a later one.

A write-back that keeps failing for a reason other than an engine that is away (the document was deleted)
is dropped after :data:`MAX_TRIES` payments and logged, so it cannot defer the rung on every run.
"""

from __future__ import annotations

from typing import Any

import structlog

from nexus.upgrade_ladder.census_progress import NoteStore
from nexus.upgrade_ladder.engine_outage import engine_unreachable

_log = structlog.get_logger(__name__)

#: Payments of one owed write-back before it is dropped as unpayable.
MAX_TRIES: int = 3


class OwedRestores:
    """One rung's owed write-backs: ``tumbler -> {"stamp", "tries"}``, mirrored to *store* when one is given.
    A store that cannot be read or written degrades to the in-memory record (it is a safety net, never a
    reason to fail a write)."""

    def __init__(self, store: NoteStore | None) -> None:
        self._store = store
        self._owed: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        note = self._store.load() if self._store is not None else None
        docs = note.get("docs") if isinstance(note, dict) else None
        if isinstance(docs, dict):
            for doc_id, entry in docs.items():
                if isinstance(entry, dict) and isinstance(entry.get("stamp"), str):
                    self._owed.setdefault(str(doc_id), {"stamp": entry["stamp"], "tries": int(entry.get("tries") or 0)})

    def _save(self) -> None:
        if self._store is None:
            return
        if self._owed:
            self._store.save({"docs": self._owed})
        else:
            self._store.clear()

    def owe(self, stamps: dict[str, str]) -> None:
        """Before a write: record the stamp each document will need back. A document already owed keeps its
        earlier, original stamp."""
        self._load()
        added = False
        for doc_id, stamp in stamps.items():
            if doc_id not in self._owed:
                self._owed[doc_id] = {"stamp": stamp, "tries": 0}
                added = True
        if added:
            self._save()

    def settle(self, attempted: list[str], failed: list[str]) -> None:
        """After a write-back: clear what went through, keep what failed."""
        still = set(failed)
        changed = False
        for doc_id in attempted:
            if doc_id not in still and self._owed.pop(doc_id, None) is not None:
                changed = True
        if changed:
            self._save()

    def pay(self, writer: Any) -> list[str]:
        """Write back every owed stamp; the documents still owed after. An engine that is away raises, so the
        runner can wait for it and the payment runs again (the record is kept)."""
        self._load()
        if not self._owed:
            return []
        for doc_id, entry in sorted(self._owed.items()):
            try:
                writer.update(doc_id, indexed_at=entry["stamp"])
            except Exception as exc:
                if engine_unreachable(exc):
                    self._save()
                    raise
                entry["tries"] += 1
                if entry["tries"] >= MAX_TRIES:
                    _log.warning("ladder_owed_restore_dropped", doc_id=doc_id, tries=entry["tries"], error=str(exc))
                    del self._owed[doc_id]
                continue
            del self._owed[doc_id]
        self._save()
        return sorted(self._owed)
