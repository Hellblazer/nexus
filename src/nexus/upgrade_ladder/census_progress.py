# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Census progress for the RDR-192 rung, kept across ``nx upgrade --auto`` sessions
(nexus-y67n5).

WHY. The ``--auto`` time limit (nexus-dcbof, default 900 s) stops the RDR-192
rung between census calls. The census used to restart from zero in the next
session, so on an engine slower than about 9.2 s per call across 98 collections
every ``--auto`` session was cut inside the census and the backfill was never
reached (interactive ``nx upgrade`` has no limit and still converged). This
module keeps each collection's census reading so the next session resumes
where the last one stopped.

WHERE IT LIVES. One T2 memory note per tenant (project ``upgrade_ladder_state``),
beside the rung's residual memo that already lives there. T2 memory is the
engine's PG table, so this adds no SQLite and no local file, and the note
belongs to the engine and tenant it was written against: a different engine
has a different note, or none.

WHEN A CACHED READING IS NOT USED (a census is recomputed, never trusted):

* the reading is older than :data:`CENSUS_PROGRESS_MAX_AGE` (per collection,
  from the time that collection was read);
* the note was written at a different package version (a different engine tag
  can answer the census differently);
* the collection is no longer in the listing, or is new (the cache only ever
  answers for names the current listing carries);
* the collection's stored chunk count in the current listing differs from the
  count it had when it was read (a write, a delete or a backfill since).

After a collection is backfilled the rung drops its entry, so the re-census
between passes reads exactly the collections it changed.

The age bound is a trade, stated plainly. A clean reading served from the cache
can be up to the bound old, and a legacy chunk that appeared since (a client
killed between ``store_put``'s document write and its chunk write, RDR-223)
would be missed by that reading. The completion record is already an
attestation, not proof (the rung docstring and the reaper's own in-engine
census cover it), and the rung's residual gate already trusts a 24 hour old
note. Six hours keeps consecutive session starts inside the bound.

STARVATION. Separately from the readings, the note counts consecutive sessions
the time limit cut. ``nx doctor`` shows the count in the ``Upgrade ladder`` row
(through the rung's ``detect()``), so a tenant whose upgrade never finishes is
visible instead of silently deferring forever. A box that never ran an
upgrade has no note and reads exactly as before.

Every storage failure degrades to "no note", which costs one more census, never
correctness.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

_log = structlog.get_logger(__name__)

#: How old one collection's cached reading may be and still stand in for a call.
CENSUS_PROGRESS_MAX_AGE = timedelta(hours=6)

#: Consecutive time-limit cuts at which the doctor row calls the upgrade starved.
STARVED_AFTER = 3

PROGRESS_PROJECT = "upgrade_ladder_state"
_TTL_DAYS = 30

#: The census totals a cached entry carries, in the order stored in ``"r"``.
READING_FIELDS = ("legacy", "unclassified", "superseded", "dead_owner", "no_owner", "scope")


class NoteStore(Protocol):
    """Durable, per-tenant single note."""

    def load(self) -> dict[str, Any] | None: ...
    def save(self, note: dict[str, Any]) -> None: ...
    def clear(self) -> None: ...


class T2NoteStore:
    """A :class:`NoteStore` on one T2 memory note. Every failure degrades to
    'no note'."""

    def __init__(self, title: str, *, project: str = PROGRESS_PROJECT, tags: str = "upgrade-ladder,rdr192") -> None:
        self._title = title
        self._project = project
        self._tags = tags

    @contextlib.contextmanager
    def _store(self) -> Iterator[Any]:
        from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — deferred; keeps cold CLI start cheap

        store = HttpMemoryStore()
        try:
            yield store
        finally:
            store.close()

    def load(self) -> dict[str, Any] | None:
        try:
            with self._store() as store:
                row = store.get(project=self._project, title=self._title)
            note = json.loads(row["content"]) if row else None
            return note if isinstance(note, dict) else None
        except Exception as exc:  # noqa: BLE001 — a note that cannot be read is no note
            _log.debug("census_progress_load_failed", error=str(exc))
            return None

    def save(self, note: dict[str, Any]) -> None:
        try:
            with self._store() as store:
                store.put(
                    self._project, self._title, json.dumps(note, sort_keys=True, separators=(",", ":")),
                    tags=self._tags, ttl=_TTL_DAYS, agent="nx-upgrade",
                )
        except Exception as exc:  # noqa: BLE001 — losing progress costs one more census, never correctness
            _log.warning("census_progress_save_failed", error=str(exc))

    def clear(self) -> None:
        try:
            with self._store() as store:
                store.delete(project=self._project, title=self._title)
        except Exception as exc:  # noqa: BLE001 — a stale note is harmless: every use is age and count checked
            _log.debug("census_progress_clear_failed", error=str(exc))


@dataclass(frozen=True)
class Deferrals:
    """How many consecutive sessions the time limit cut, since when."""

    count: int
    since: str
    last: str
    where: str

    @property
    def starved(self) -> bool:
        return self.count >= STARVED_AFTER

    def describe(self) -> str:
        head = "STARVED: " if self.starved else ""
        return (
            f"{head}the `nx upgrade --auto` time limit has cut {self.count} consecutive session(s) "
            f"since {self.since} (last {self.last}, {self.where}); the census keeps its progress and "
            "resumes. `nx upgrade` has no time limit and finishes it"
            + ("; or raise NX_UPGRADE_AUTO_DEADLINE_SECONDS" if self.starved else "")
        )


class CensusProgress:
    """Per-collection census readings and the time-limit cut count, in one note.

    ``store=None`` disables it: every read answers 'nothing', every write is a
    no-op. Each method loads the note, changes it and writes it back, so
    nothing is held in memory between calls and a concurrent process sees the
    latest (the rung's cross-process lock already keeps two converges apart).
    """

    def __init__(
        self,
        store: NoteStore | None,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_age: timedelta = CENSUS_PROGRESS_MAX_AGE,
    ) -> None:
        self._store = store
        self._now = now_fn
        self._max_age = max_age

    # ── note plumbing ────────────────────────────────────────────────────────

    def _load(self, version: str) -> dict[str, Any]:
        """The note for *version*, or a fresh empty one. A note written at
        another package version is not carried over."""
        note = self._store.load() if self._store is not None else None
        if isinstance(note, dict) and note.get("version") == version:
            note.setdefault("entries", {})
            return note
        return {"version": version, "entries": {}, "cuts": None}

    def _save(self, note: dict[str, Any]) -> None:
        if self._store is not None:
            self._store.save(note)

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # ── readings ─────────────────────────────────────────────────────────────

    def usable(self, version: str, stored: Mapping[str, int]) -> dict[str, dict[str, Any]]:
        """Cached readings that may stand in for a census call, keyed by
        collection name: only names in *stored* (the current listing's
        collection to stored-chunk-count map), read within the age bound, at
        this package version, with an unchanged stored count."""
        if self._store is None:
            return {}
        entries = self._load(version)["entries"]
        now = self._now()
        out: dict[str, dict[str, Any]] = {}
        for name, count in stored.items():
            entry = entries.get(name)
            if not isinstance(entry, dict):
                continue
            try:
                age = now - datetime.fromisoformat(str(entry["t"]))
                if age > self._max_age or age < timedelta(0) or int(entry["s"]) != int(count):
                    continue
                if not entry.get("q") and len(entry["r"]) != len(READING_FIELDS):
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            out[name] = entry
        return out

    def entry(self, *, stored: int, reading: tuple[int, ...] | None) -> dict[str, Any]:
        """A new cached entry: the census totals in :data:`READING_FIELDS`
        order, or ``None`` for a collection the engine refused as quarantine."""
        out: dict[str, Any] = {"s": int(stored), "t": self._stamp()}
        if reading is None:
            out["q"] = 1
        else:
            out["r"] = [int(x) for x in reading]
        return out

    def commit(self, version: str, entries: Mapping[str, dict[str, Any]]) -> None:
        """Replace the stored readings with *entries* (the still-usable ones
        plus the ones just read), keeping the cut count."""
        if self._store is None:
            return
        note = self._load(version)
        note["entries"] = dict(entries)
        self._save(note)

    def invalidate(self, version: str, name: str) -> None:
        """Forget *name*'s reading (it was just backfilled, so it changed)."""
        if self._store is None:
            return
        note = self._load(version)
        if note["entries"].pop(name, None) is not None:
            self._save(note)

    # ── starvation ───────────────────────────────────────────────────────────

    def note_cut(self, version: str, where: str) -> None:
        """The time limit cut this session at *where*."""
        if self._store is None:
            return
        note = self._load(version)
        cuts = note.get("cuts") or {}
        stamp = self._stamp()
        note["cuts"] = {
            "n": int(cuts.get("n", 0)) + 1,
            "since": cuts.get("since", stamp),
            "last": stamp,
            "where": where,
        }
        self._save(note)

    def reset_cuts(self, version: str) -> None:
        """A session ended without being cut: the upgrade is not starved."""
        if self._store is None:
            return
        note = self._load(version)
        if note.get("cuts"):
            note["cuts"] = None
            self._save(note)

    def deferrals(self, version: str) -> Deferrals | None:
        """The cut count, or ``None`` when no session was cut (including a box
        that never ran an upgrade)."""
        if self._store is None:
            return None
        cuts = self._load(version).get("cuts")
        try:
            if not isinstance(cuts, dict) or int(cuts.get("n", 0)) < 1:
                return None
            return Deferrals(
                count=int(cuts["n"]), since=str(cuts.get("since", "?")),
                last=str(cuts.get("last", "?")), where=str(cuts.get("where", "?")),
            )
        except (TypeError, ValueError):  # a note we did not write: no count, and detect() must never raise
            return None

    def clear(self) -> None:
        """The rung completed: nothing is owed, drop the note."""
        if self._store is not None:
            self._store.clear()
