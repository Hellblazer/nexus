# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""RDR-101 Phase 1: append-only event log writer.

The event log is the canonical state per RDR-101 §"Core invariants". This
module owns the on-disk format (JSONL, one envelope per line) and the
locking pattern that the existing ``Catalog`` JSONL files use today: a
directory-level exclusive lock taken before every append, mirroring
``Catalog._acquire_lock`` / ``Catalog._append_jsonl``. Locking is
delegated to :mod:`nexus._locking`, which uses POSIX ``fcntl.flock`` on
Unix and a sentinel-file ``msvcrt.locking`` on Windows.

The Phase 1 writer is shadow-only: nothing reads from this log yet and
the existing ``Catalog.register()`` / ``update()`` continue to write to
``owners.jsonl``/``documents.jsonl``/``links.jsonl`` exactly as before.
Phase 3 cuts production writes over to this log; Phases 4-5 migrate
readers.

The replay path (``EventLog.replay``) yields ``Event`` envelopes in
append order. Bad lines (JSON decode error, missing ``type``) are
logged and skipped, matching the behaviour of ``read_documents`` /
``read_owners`` / ``read_links`` in ``catalog/tumbler.py``: catalog
JSONL is git-managed and machine-edited, so a hard fail on a single
malformed line would brick the projector.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import structlog

from nexus._locking import acquire_directory_lock, release_lock
from nexus.catalog.events import Event

_log = structlog.get_logger()

EVENTS_FILENAME = "events.jsonl"


class EventLog:
    """Append-only JSONL event log under ``catalog_dir/events.jsonl``.

    Locking mirrors ``Catalog._acquire_lock``: an exclusive flock on the
    catalog directory. The same lock guards every JSONL append in the
    catalog today, so the event log's append cannot interleave with
    ``owners.jsonl`` / ``documents.jsonl`` / ``links.jsonl`` writes during
    the Phase 1 shadow window. Phase 3 collapses these into a single
    event-emit call and the multi-file lock dance goes away.

    The log file is created on first construction (touch-if-missing) so
    callers don't have to differentiate between "fresh catalog" and
    "existing catalog" states.
    """

    def __init__(self, catalog_dir: Path) -> None:
        self._dir = catalog_dir
        self._path = catalog_dir / EVENTS_FILENAME
        if not self._path.exists():
            self._path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def is_empty(self) -> bool:
        """Return True when the log file is missing or zero-bytes.

        Encapsulates the ``log.path.exists() or log.path.stat().st_size``
        check that callers previously inlined (RDR-112 P0.5, nexus-siva).
        """
        try:
            return self._path.stat().st_size == 0
        except FileNotFoundError:
            return True

    def append(self, event: Event) -> None:
        """Atomically append one event envelope to the log.

        Acquires an exclusive flock on the catalog directory, opens the
        log in append mode, writes one JSON line, and releases the lock.
        The lock pattern matches ``Catalog._acquire_lock`` exactly so
        Phase 1 writers and the existing JSONL writers don't race.

        Re-entrancy invariant (nexus-lrhg, RDR-108 audit finding 5):
        callers MUST NOT already hold the catalog directory flock.
        POSIX ``flock`` on the same file descriptor is re-entrant only
        when the caller passes the same ``dir_fd``; ``acquire_directory_lock``
        opens a fresh fd every call, so a holder calling ``append()``
        produces a second flock acquisition that may deadlock under
        some kernels (Linux ``flock`` LOCK_EX on a new fd against the
        same path is non-reentrant). For writer code paths that already
        hold the lock (e.g. ``Catalog`` mutators inside a single dir-fd
        scope), use :meth:`append_unlocked` and let the caller manage
        the flock around the whole atomic step.

        Raises ``TypeError`` if the event payload contains values
        ``json.dumps`` cannot serialize (e.g. ``datetime``, ``Path``,
        ``Decimal``). Earlier versions silently coerced these via
        ``default=str``, which round-tripped them as strings on replay
        and produced silent data corruption. Callers must ensure
        ``meta`` and ``payload`` dict fields contain JSON-native
        primitives before calling.
        """
        line = json.dumps(event.to_dict(), separators=(",", ":"))
        dir_fd = acquire_directory_lock(self._dir)
        try:
            with self._path.open("a") as f:
                f.write(line)
                f.write("\n")
        finally:
            release_lock(dir_fd)

    def append_unlocked(self, event: Event) -> None:
        """Append one event envelope WITHOUT acquiring the dir flock.

        Caller is responsible for holding an exclusive lock on the
        catalog directory for the whole atomic step. Use when the
        write is part of a larger sequence already running under
        ``acquire_directory_lock`` (e.g. a Catalog mutator that bundles
        SQLite UPDATE + JSONL append + event log append under one
        flock so the three projections converge or all fail together).

        Same JSON-serialization contract as :meth:`append`.
        """
        line = json.dumps(event.to_dict(), separators=(",", ":"))
        with self._path.open("a") as f:
            f.write(line)
            f.write("\n")

    def append_many(self, events: list[Event]) -> None:
        """Append a batch of events under a single flock acquisition.

        Used by the projector synthesis path (Phase 1 / Phase 2) where one
        catalog row produces multiple events (e.g. tombstoned row →
        ``DocumentRegistered`` + ``DocumentDeleted``). A single flock keeps
        the batch atomic with respect to other catalog writers.

        Same re-entrancy invariant as :meth:`append`: callers must NOT
        already hold the catalog directory flock; use
        :meth:`append_many_unlocked` in that case.

        Like ``append``, raises ``TypeError`` on non-JSON-serializable
        payload values rather than silently coercing them.
        """
        if not events:
            return
        lines = [
            json.dumps(e.to_dict(), separators=(",", ":"))
            for e in events
        ]
        dir_fd = acquire_directory_lock(self._dir)
        try:
            with self._path.open("a") as f:
                for line in lines:
                    f.write(line)
                    f.write("\n")
        finally:
            release_lock(dir_fd)

    def append_many_unlocked(self, events: list[Event]) -> None:
        """Append a batch of events WITHOUT acquiring the dir flock.

        Caller-managed flock equivalent of :meth:`append_many`. See
        :meth:`append_unlocked` for the invariant and use case.
        """
        if not events:
            return
        lines = [
            json.dumps(e.to_dict(), separators=(",", ":"))
            for e in events
        ]
        with self._path.open("a") as f:
            for line in lines:
                f.write(line)
                f.write("\n")

    def replay(self) -> Iterator[Event]:
        """Yield events in append order. Skips malformed lines with a warning.

        Catches the full failure surface so one bad line does not abort the
        iterator: JSONDecodeError on garbage text, missing-``type`` shape
        errors, and any TypeError/AttributeError/ValueError that
        ``Event.from_dict`` raises on a syntactically-valid JSON line whose
        payload has the wrong shape (``payload: null``, ``payload: [1,2,3]``,
        ``v: "abc"``, etc.). The catalog event log is git-managed and
        machine-edited; a hard fail on a single malformed line would brick
        the projector for the whole catalog.
        """
        if not self._path.exists():
            return
        with self._path.open() as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning(
                        "event_log_parse_error",
                        path=str(self._path),
                        lineno=lineno,
                        preview=line[:80],
                    )
                    continue
                if "type" not in obj:
                    _log.warning(
                        "event_log_missing_type",
                        path=str(self._path),
                        lineno=lineno,
                        preview=line[:80],
                    )
                    continue
                try:
                    yield Event.from_dict(obj)
                except (TypeError, AttributeError, ValueError) as exc:
                    _log.warning(
                        "event_log_payload_error",
                        path=str(self._path),
                        lineno=lineno,
                        preview=line[:80],
                        error=str(exc),
                    )
                    continue

    def replay_from(
        self,
        offset: int,
        *,
        limit_offset: int | None = None,
    ) -> Iterator[Event]:
        """Yield events whose start-of-line byte offset is in ``[offset, limit_offset)``.

        RDR-104 Step 1: offset-aware streaming iterator powering the
        incremental rebuild path in ``Catalog._ensure_consistent``.

        ``offset`` is a raw byte position in the file. The file is opened
        in **binary mode** + ``seek(offset)`` so byte positions are
        portable across platforms. Text-mode ``tell()`` returns an
        opaque cookie under universal-newline translation on Windows,
        which is NOT a portable offset; binary-mode is the only correct
        shape for marker round-tripping.

        ``limit_offset`` (when supplied) caps the iterator at the half-
        open boundary: a line whose start-of-line offset is ``>=
        limit_offset`` is excluded. The bounded form is **mandatory** for
        concurrent-appender safety in the incremental orchestrator: a
        writer landing between the orchestrator's ``stat()`` snapshot
        and this iterator's read window must not extend the iterator
        past the captured offset, or the marker the orchestrator
        persists drifts below the true tail and incremental never
        settles. The unbounded form (``limit_offset=None``) preserves
        natural "everything from offset onwards" semantics for any
        future caller.

        Mid-line / malformed-first-line behaviour follows the existing
        ``replay()`` pattern: warn-and-skip rather than raise. The
        orchestrator detects corruption at the caller layer (zero
        events yielded from a non-empty delta range) and escalates to
        full rebuild.

        Raises ``ValueError`` when ``offset > file_size`` — the marker
        is past the end of the file (truncated since marker write), so
        the caller falls back to full rebuild.
        """
        if not self._path.exists():
            return
        file_size = self._path.stat().st_size
        if offset > file_size:
            raise ValueError(
                f"replay_from offset {offset} exceeds file size {file_size}",
            )
        if offset == file_size:
            return  # nothing in the half-open range
        if limit_offset is not None and offset >= limit_offset:
            return
        with self._path.open("rb") as f:
            f.seek(offset)
            lineno = 0
            while True:
                line_start = f.tell()
                if limit_offset is not None and line_start >= limit_offset:
                    return
                raw = f.readline()
                if not raw:
                    return
                lineno += 1
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    _log.warning(
                        "event_log_decode_error",
                        path=str(self._path),
                        line_start=line_start,
                        error=str(exc),
                    )
                    continue
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning(
                        "event_log_parse_error",
                        path=str(self._path),
                        line_start=line_start,
                        preview=line[:80],
                    )
                    continue
                if "type" not in obj:
                    _log.warning(
                        "event_log_missing_type",
                        path=str(self._path),
                        line_start=line_start,
                        preview=line[:80],
                    )
                    continue
                try:
                    yield Event.from_dict(obj)
                except (TypeError, AttributeError, ValueError) as exc:
                    _log.warning(
                        "event_log_payload_error",
                        path=str(self._path),
                        line_start=line_start,
                        preview=line[:80],
                        error=str(exc),
                    )
                    continue

    def truncate(self) -> None:
        """Drop the entire event log. Test-only — production code never calls."""
        self._path.write_text("")
