# SPDX-License-Identifier: AGPL-3.0-or-later
"""When a note's own TTL lapses, read from its catalog entry (nexus-6pfgb.15).

A T3 chunk row is one per chunk text, so the ``ttl_days`` and ``indexed_at`` on it are the last
writer's: two notes with identical text share it, and whichever was put last decides what ``nx
store expire`` sees for both. Each note's catalog entry holds its own values instead (nexus-6pfgb.5):
``meta.ttl_days`` and ``meta.put_at``. This module is the one place that reads them, so the expiry
sweep and the ``expires in Nd`` display cannot disagree.

Three spellings to keep apart:

- ``ttl_days`` absent or ``0`` is permanent. ``0`` is the retraction value a re-put without a TTL
  writes over a ``ttl_days`` the entry held (catalog ``update`` cannot delete a ``meta`` key); it is
  never an accepted input. Do not hand a catalog ``0`` to :func:`nexus.metadata_schema.is_expired`,
  which logs a legacy warning for it on every call.
- The clock is ``meta.put_at``, the time of the note's last put, written in the same update as
  ``ttl_days``. The entry's ``indexed_at`` is not used when ``put_at`` exists: the engine refreshes
  it on every manifest write, a heal or a backfill included, so a TTL counted from it drifts later
  with each one. An entry that carries ``ttl_days`` but no ``put_at`` (a note written between the
  two changes) falls back to ``indexed_at``, which can only be later than the put, so such a note
  expires late, never early.
- An entry with neither key was put before the catalog held the note's own values. That note's TTL
  lives only on the chunk row (:func:`has_own_keys`), and only a row the note itself wrote may be read
  as its TTL (:func:`row_written_by`).

- The entry's keys can be stale. Only the put path of a client that has nexus-6pfgb.5 writes them, so a
  re-put by an older client, or by a writer that bypasses it (the ``.nxexp`` import), leaves a note that
  looks lapsed after its owner made it permanent. :func:`catalog_ttl_stale` reads the note's own chunk
  rows for that (nexus-6pfgb.30); the sweep does not retire a note it flags.

Pure functions, no I/O; the caller passes ``now``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse; naive stamps are read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def note_ttl_days(meta: Any) -> int:
    """The note's own TTL in days from its catalog ``meta``; ``0`` when it is permanent
    (``ttl_days`` absent, ``0``, negative, not a number, or too large for a ``timedelta``)."""
    if not isinstance(meta, dict):
        return 0
    value = meta.get("ttl_days")
    if isinstance(value, bool):
        return 0
    try:
        days = int(value) if value is not None else 0
    except (TypeError, ValueError, OverflowError):  # OverflowError: int(float("inf"))
        return 0
    # past timedelta's range (about 2.7 million years) is permanent for every purpose here
    return days if 0 < days <= timedelta.max.days else 0


def has_own_keys(meta: Any) -> bool:
    """Whether the entry carries the note's own TTL facts (``put_at`` or ``ttl_days``), so the
    catalog, not the shared chunk row, says whether the note expires."""
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("put_at")) or "ttl_days" in meta


def clock_start(meta: Any, indexed_at: str) -> str:
    """The stamp the note's TTL counts from: ``meta.put_at``, else the entry's *indexed_at*."""
    if isinstance(meta, dict) and _parse_iso(meta.get("put_at")) is not None:
        return str(meta["put_at"])
    return indexed_at or ""


def lapsed(meta: Any, indexed_at: str, *, now: datetime) -> bool:
    """Whether the note's own TTL has run out at *now*. A permanent note, or one with no readable
    clock, never lapses."""
    days = note_ttl_days(meta)
    if days <= 0:
        return False
    start = _parse_iso(clock_start(meta, indexed_at))
    if start is None:
        return False
    try:
        return (now - start) >= timedelta(days=days)
    except OverflowError:  # a ttl_days past timedelta's range (huge, or 1e300): it never runs out
        return False


def row_written_by(owner_tumbler: Any, row_meta: Any, *, owner_title: str) -> bool:
    """Whether the chunk row's metadata is the write of the note *owner_tumbler* titled *owner_title*.

    A chunk row is one per chunk text, so its ``ttl_days`` and ``indexed_at`` are the last writer's, and
    a row is not rewritten when a co-owner leaves (the sweep retiring a TTL note, ``nx store delete``).
    ``note_write`` stamps ``catalog_doc_id``, the writer's tumbler, on every chunk it writes, so a row
    whose ``catalog_doc_id`` is the owner's was written by that owner and its TTL is the owner's.
    A tumbler alone is not proof. An ``.nxexp`` import carries each row's metadata whole, so a row keeps
    the SOURCE box's ``catalog_doc_id`` while the destination mints its own tumblers: a stale stamp can
    equal an unrelated destination note's tumbler. The row's ``title`` is the last writer's note title
    and travels with the stamp, so it must be the owner's too; a stale stamp that happens to equal the
    owner's tumbler does not also carry the owner's title unless the row is that note's, because a note
    title names one note in a collection. Either side empty is no match.

    A row with no ``catalog_doc_id`` (written before the stamp, or by a path that omits it) is nobody's:
    ``False``, never a fall-back to "the only owner left". The expiry sweep asks this before reading a
    row's TTL as a note's; the recovery-bundle export will adopt it (nexus-6pfgb.14).
    """
    if not isinstance(row_meta, dict) or not owner_title:
        return False
    stamp = row_meta.get("catalog_doc_id")
    if not (isinstance(stamp, str) and stamp and stamp == str(owner_tumbler)):
        return False
    title = row_meta.get("title")
    return isinstance(title, str) and title == owner_title


#: How far a chunk row's ``indexed_at`` may sit after the catalog's ``put_at`` and still be the same
#: put. The row is stamped in the T3 write and ``put_at`` in the catalog hook of one put, seconds apart;
#: five minutes is generous for that and far below the one-day minimum TTL.
PUT_SKEW = timedelta(minutes=5)


def row_names_note(owner_tumbler: Any, row_meta: Any, *, owner_title: str) -> bool:
    """Whether a chunk row is evidence about the put of the note *owner_tumbler* titled *owner_title*.

    The wider cousin of :func:`row_written_by`, for a check that can only REFUSE to retire a note, never
    retire one: a row whose ``title`` is the note's counts even when its ``catalog_doc_id`` is not the
    note's tumbler, because an ``.nxexp`` import keeps the SOURCE box's stamp on rows it writes over this
    note's. A note title names one note in a collection, so a same-titled row is that note's row.
    """
    if row_written_by(owner_tumbler, row_meta, owner_title=owner_title):
        return True
    return isinstance(row_meta, dict) and bool(owner_title) and row_meta.get("title") == owner_title


def catalog_ttl_stale(
    meta: Any, indexed_at: str, rows: Any, *, owner_tumbler: Any, owner_title: str,
    skew: timedelta = PUT_SKEW,
) -> bool:
    """Whether a chunk row of this note records a put the catalog entry's own TTL keys do not (nexus-6pfgb.30).

    The note's catalog ``ttl_days`` and ``put_at`` are written only by the put path of a client that has
    nexus-6pfgb.5; a re-put by an older client, or by any writer that bypasses the catalog hook (the
    ``.nxexp`` import), rewrites the chunk row and leaves them as they were, so a note its owner just made
    permanent still looks lapsed. Both signs are read from the note's OWN rows (:func:`row_names_note`; a
    row another note wrote last says nothing about this one):

    - the row's TTL is not the catalog's (a permanent row under a catalog ``ttl_days`` > 0, or another
      number of days), or
    - the row's ``indexed_at`` is more than *skew* after the catalog's clock (:func:`clock_start`).

    A row with no readable ``indexed_at`` only has the first sign to give. *rows* is the iterable of the
    chunk rows' metadata dicts.
    """
    days = note_ttl_days(meta)
    start = _parse_iso(clock_start(meta, indexed_at))
    for row in rows or ():
        if not row_names_note(owner_tumbler, row, owner_title=owner_title):
            continue
        if note_ttl_days(row) != days:
            return True
        stamped = _parse_iso(row.get("indexed_at"))
        if stamped is not None and start is not None and stamped - start > skew:
            return True
    return False
