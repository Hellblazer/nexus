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
  count it had when it was read (a write, a delete or a backfill since), or
  either count is missing or negative (the listing fell back to a count it
  could not measure);
* the entry is malformed in any way (see MALFORMED NOTES).

After a collection is backfilled the rung drops its entry, so the re-census
between passes reads exactly the collections it changed.

The age bound is a trade, stated plainly. A clean reading served from the cache
can be up to the bound old. The stored-count check sees every chunk write and
delete, so what it can miss is a CATALOG-side change that leaves the stored
count alone: a document registered for a chunk that was already stored (a
no-owner chunk gaining a legacy owner), or a manifest row removed. The
completion record is an attestation, not proof (the rung docstring and the
reaper's own in-engine census cover it), and it records how many readings came
from the note and the oldest one's age. 24 hours matches the rung's residual
window (``RESIDUAL_RETRY_AFTER``) and is what a once-a-day user needs: each
session reads what it can inside the ``--auto`` time limit and the readings
must still stand when the last session finishes the walk.

The note is also checkpointed every :data:`CHECKPOINT_EVERY` readings during
the walk, not only when it ends, so a session killed outright (shutdown, OOM,
SIGKILL) loses at most that many readings.

STARVATION. Separately from the readings, the note counts consecutive sessions
the time limit cut WITHOUT making progress (no new reading kept, no collection
backfilled); a session that progressed resets the count, so a slow but
converging upgrade is never labelled starved. It also counts consecutive
sessions whose census call failed at the SAME collection (a call over the HTTP
timeout, say), which would otherwise stall silently. ``nx doctor`` shows both
in the ``Upgrade ladder`` row (through the rung's ``detect()``). A box that
never ran an upgrade has no note and reads exactly as before.

MALFORMED NOTES. The note is a T2 row any version, tool or person may have
written. :meth:`CensusProgress._load` accepts only the documented shape; a
note of any other shape counts as no note and is overwritten by the next
write. The rung also calls every progress method through :func:`guarded`, so
an unforeseen failure there is a logged warning and "no note", never a failed
rung.

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
#: Matches the rung's residual window (``RESIDUAL_RETRY_AFTER``).
CENSUS_PROGRESS_MAX_AGE = timedelta(hours=24)

#: Consecutive no-progress time-limit cuts at which the doctor row calls the
#: upgrade starved.
STARVED_AFTER = 3

#: Consecutive sessions whose census failed at the same collection before the
#: doctor row names it (one failure is an outage, two is a pattern).
STALLED_AFTER = 2

#: The census writes the note after this many new readings, not only at its end.
CHECKPOINT_EVERY = 10

PROGRESS_PROJECT = "upgrade_ladder_state"
_TTL_DAYS = 30

#: The census totals a cached entry carries, in the order stored in ``"r"``.
READING_FIELDS = ("legacy", "unclassified", "superseded", "dead_owner", "no_owner", "scope")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_quarantine_entry(entry: Mapping[str, Any]) -> bool:
    """Whether a cached entry records a collection the engine refused as
    quarantine. The ONE predicate: ``usable()`` and the rung both ask it, so an
    entry one of them would serve as quarantine the other cannot call usable
    (``q`` is exactly the integer 1 that :meth:`CensusProgress.entry` writes;
    ``2``, ``"x"``, ``[1]`` and ``True`` are not it)."""
    q = entry.get("q")
    return _is_int(q) and q == 1


#: Longest note-derived string the doctor row and ``nx upgrade --dry-run`` print.
_TEXT_MAX = 80
#: Longest collection name printed (a real one is well under this).
_NAME_MAX = 120


def _safe_text(value: object, limit: int = _TEXT_MAX) -> str:
    """A note field made printable: capped at *limit* characters and escaped
    (``repr`` without the quotes), so a lone surrogate cannot make
    ``click.echo`` raise ``UnicodeEncodeError`` and a huge or multi-line string
    cannot flood the row. Note fields are T2 data any tool may have written."""
    text = str(value)
    cut = text[:limit]
    return repr(cut)[1:-1] + ("..." if len(text) > limit else "")


def guarded[T](what: str, call: Callable[[], T], default: T) -> T:
    """Run one progress-note operation. Whatever it raises is "no note": a
    structlog warning and *default*, never a failure of the rung (the note only
    ever saves a census, it must never cost the upgrade)."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 — progress is an optimisation; nothing in it may fail the rung
        _log.warning("census_progress_failed", operation=what, error=f"{type(exc).__name__}: {exc}")
        return default


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
    """How many consecutive sessions the time limit cut without progress, since when."""

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
            f"{head}the `nx upgrade --auto` time limit ended {self.count} consecutive session(s) "
            f"before they read a new collection or backfilled one, since {_safe_text(self.since)} "
            f"(last {_safe_text(self.last)}, {_safe_text(self.where, _NAME_MAX)}); "
            "`nx upgrade` has no time limit and finishes it"
            + ("; or raise NX_UPGRADE_AUTO_DEADLINE_SECONDS" if self.starved else "")
        )


@dataclass(frozen=True)
class Stall:
    """A census call that failed at the same collection in consecutive sessions."""

    collection: str
    count: int
    since: str
    last: str

    def describe(self) -> str:
        name = _safe_text(self.collection, _NAME_MAX)
        return (
            f"census stalled at {name} for {self.count} sessions since {_safe_text(self.since)} "
            f"(last {_safe_text(self.last)}): the engine's census call for it failed each time; "
            f"`nx t3 census-manifest-less --collection {name}` shows why"
        )


def _cuts_shape_ok(cuts: object) -> bool:
    return cuts is None or (isinstance(cuts, dict) and _is_int(cuts.get("n", 0)) and cuts.get("n", 0) >= 0)


def _stall_shape_ok(stall: object) -> bool:
    return stall is None or (
        isinstance(stall, dict) and isinstance(stall.get("collection"), str)
        and _is_int(stall.get("n")) and stall["n"] >= 0
    )


class CensusProgress:
    """Per-collection census readings and the no-progress cut and stall counts,
    in one note.

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

    def _read(self, version: str) -> tuple[dict[str, Any], bool]:
        """The note for *version* (or a fresh empty one) and whether a stored
        note of the wrong shape was discarded. A note written at another
        package version is not carried over, and a note whose shape is not the
        documented one is no note (the next write replaces it)."""
        note = self._store.load() if self._store is not None else None
        malformed = False
        if isinstance(note, dict) and note.get("version") == version:
            entries = note.get("entries", {})
            if isinstance(entries, dict) and _cuts_shape_ok(note.get("cuts")) and _stall_shape_ok(note.get("stall")):
                note["entries"] = entries
                note.setdefault("cuts", None)
                note.setdefault("stall", None)
                return note, False
            _log.warning("census_progress_note_malformed", version=version)
            malformed = True
        return {"version": version, "entries": {}, "cuts": None, "stall": None}, malformed

    def _load(self, version: str) -> dict[str, Any]:
        return self._read(version)[0]

    def _save(self, note: dict[str, Any]) -> None:
        if self._store is not None:
            self._store.save(note)

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # ── readings ─────────────────────────────────────────────────────────────

    def age(self, entry: Mapping[str, Any]) -> timedelta | None:
        """How old *entry*'s reading is, or ``None`` when its stamp is unreadable."""
        try:
            return self._now() - datetime.fromisoformat(entry["t"])
        except (KeyError, ValueError, TypeError):  # missing, unparsable, not a string, or naive against an aware clock
            return None

    def usable(self, version: str, stored: Mapping[str, int]) -> dict[str, dict[str, Any]]:
        """Cached readings that may stand in for a census call, keyed by
        collection name: only names in *stored* (the current listing's
        collection to stored-chunk-count map), read within the age bound, at
        this package version, with an unchanged and measurable stored count and
        a well-formed entry."""
        if self._store is None:
            return {}
        entries = self._load(version)["entries"]
        out: dict[str, dict[str, Any]] = {}
        for name, count in stored.items():
            entry = entries.get(name)
            if not isinstance(entry, dict) or not _is_int(count) or count < 0:
                continue
            size = entry.get("s")
            if not _is_int(size) or size < 0 or size != count:
                continue
            age = self.age(entry)
            if age is None or age > self._max_age or age < timedelta(0):
                continue
            if "q" in entry and not is_quarantine_entry(entry):
                continue  # a quarantine marker that is not exactly the one entry() writes is a forged entry
            if not is_quarantine_entry(entry):
                reading = entry.get("r")
                if (
                    not isinstance(reading, list) or len(reading) != len(READING_FIELDS)
                    or not all(_is_int(x) and x >= 0 for x in reading)
                ):
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
        plus the ones just read), keeping the counters. A note that was not the
        documented shape is replaced here."""
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

    def end_session(
        self, version: str, *, cut_at: str | None, stalled_at: str | None, progressed: bool
    ) -> None:
        """Fold one finished session into the counters.

        *cut_at* is where the time limit cut it, if it did; *stalled_at* is the
        collection whose census call failed, if one did; *progressed* is
        whether it kept a new reading or finished a backfill. A cut counts only
        when the session did not progress, and any other session (progressed,
        uncut, or ended by an engine failure) resets the count: STARVED means
        consecutive sessions that got nowhere, not sessions that were cut. A
        failure at the same collection as the last session's adds to the stall
        count; any session that did not fail there clears it."""
        if self._store is None:
            return
        note, malformed = self._read(version)
        before = (note["cuts"], note["stall"])
        stamp = self._stamp()

        cuts = note["cuts"] or {}
        if cut_at is not None and not progressed:
            note["cuts"] = {
                "n": int(cuts.get("n", 0)) + 1, "since": cuts.get("since", stamp), "last": stamp, "where": cut_at,
            }
        else:
            note["cuts"] = None

        stall = note["stall"]
        if stalled_at is None:
            note["stall"] = None
        elif stall and stall["collection"] == stalled_at:
            note["stall"] = {**stall, "n": stall["n"] + 1, "last": stamp}
        else:
            note["stall"] = {"collection": stalled_at, "n": 1, "since": stamp, "last": stamp}

        if (note["cuts"], note["stall"]) != before:
            self._save(note)
        elif malformed:
            # Nothing to count, but the stored note is unreadable: replace it
            # (or drop it when the replacement would be empty), or every
            # detect() would log it again.
            if note["cuts"] is None and note["stall"] is None:
                self._store.clear()
            else:
                self._save(note)

    def deferrals(self, version: str) -> Deferrals | None:
        """The no-progress cut count, or ``None`` when no session was cut
        (including a box that never ran an upgrade)."""
        if self._store is None:
            return None
        cuts = self._load(version)["cuts"]
        if not isinstance(cuts, dict) or int(cuts.get("n", 0)) < 1:
            return None
        return Deferrals(
            count=int(cuts["n"]), since=str(cuts.get("since", "?")),
            last=str(cuts.get("last", "?")), where=str(cuts.get("where", "?")),
        )

    def stall(self, version: str) -> Stall | None:
        """The collection the census keeps failing at, once it has failed there
        in :data:`STALLED_AFTER` consecutive sessions."""
        if self._store is None:
            return None
        stall = self._load(version)["stall"]
        if not isinstance(stall, dict) or stall["n"] < STALLED_AFTER:
            return None
        return Stall(
            collection=stall["collection"], count=stall["n"],
            since=str(stall.get("since", "?")), last=str(stall.get("last", "?")),
        )

    def describe(self, version: str) -> str:
        """What ``nx doctor`` adds to the pending row: the cut and stall text,
        or an empty string for a box with neither."""
        parts = [x.describe() for x in (self.deferrals(version), self.stall(version)) if x is not None]
        return ". ".join(parts)

    def clear(self) -> None:
        """The rung completed: nothing is owed, drop the note."""
        if self._store is not None:
            self._store.clear()
