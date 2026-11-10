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

from nexus.upgrade_ladder.census_progress import NoteStore, NoteUnreadable
from nexus.upgrade_ladder.engine_outage import engine_unreachable

_log = structlog.get_logger(__name__)

#: Payments of one owed write-back before it is dropped as unpayable.
MAX_TRIES: int = 3


def _timed_out(exc: BaseException) -> bool:
    import httpx  # noqa: PLC0415 — deferred

    link: BaseException | None = exc
    seen: set[int] = set()
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if isinstance(link, (TimeoutError, httpx.TimeoutException)):
            return True
        link = link.__cause__ or link.__context__
    return False


class OwedRestores:
    """One rung's owed write-backs: ``tumbler -> {"stamp", "tries"}``, mirrored to *store* when one is given.

    A note that cannot be read because the engine is away raises, so the runner waits and reads it again.
    One that cannot be read for another reason is never written this run either: saving the in-memory part
    would overwrite what the stored note still owes. A failed save only loses durability."""

    def __init__(self, store: NoteStore | None) -> None:
        self._store = store
        self._owed: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._unreadable = False  # the stored note could not be read: never overwrite it this run

    def _load(self) -> None:
        if self._loaded:
            return
        note = None
        if self._store is not None:
            try:
                note = self._store.load(strict=True)
            except NoteUnreadable as exc:
                if engine_unreachable(exc):
                    raise  # the runner waits for the engine and this read runs again
                self._unreadable = True
                _log.warning("ladder_owed_restores_unreadable", error=str(exc))
        self._loaded = True
        docs = note.get("docs") if isinstance(note, dict) else None
        if isinstance(docs, dict):
            for doc_id, entry in docs.items():
                if isinstance(entry, dict) and isinstance(entry.get("stamp"), str):
                    self._owed.setdefault(str(doc_id), {"stamp": entry["stamp"], "tries": int(entry.get("tries") or 0)})

    def _save(self) -> None:
        if self._store is None or self._unreadable:
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

    def settle(self, asked: list[str], failed: list[str]) -> None:
        """After a write: clear every document that was *asked* about, written or not, except those whose
        write-back *failed*. A document the engine never wrote (refused, raced, a 409) was never restamped
        and owes nothing; paying it later could undo a re-index made since."""
        still = set(failed)
        changed = False
        for doc_id in asked:
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
                if _timed_out(exc):
                    continue  # a slow engine is not a reason to give up on the write-back
                entry["tries"] += 1
                if entry["tries"] >= MAX_TRIES:
                    _log.warning(
                        "ladder_owed_restore_dropped", doc_id=doc_id, tries=entry["tries"], error=str(exc),
                        effect="the document keeps the engine's run-time indexed_at; its chunk-newer guard stays off",
                    )
                    del self._owed[doc_id]
                continue
            del self._owed[doc_id]
        self._save()
        return sorted(self._owed)
