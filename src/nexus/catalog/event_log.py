# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""RDR-101 Phase 1: append-only event log writer.

The event log is the canonical state per RDR-101 §"Core invariants". This
module owns the on-disk format (JSONL, one envelope per line) and the
locking pattern that the existing ``Catalog`` JSONL files use today: a
directory-level ``fcntl.flock`` taken before every append, mirroring
``Catalog._acquire_lock`` / ``Catalog._append_jsonl``.

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

import fcntl
import json
import os
from collections.abc import Iterator
from pathlib import Path

import structlog

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

    def append(self, event: Event) -> None:
        """Atomically append one event envelope to the log.

        Acquires an exclusive flock on the catalog directory, opens the
        log in append mode, writes one JSON line, and releases the lock.
        The lock pattern matches ``Catalog._acquire_lock`` exactly so
        Phase 1 writers and the existing JSONL writers don't race.

        Raises ``TypeError`` if the event payload contains values
        ``json.dumps`` cannot serialize (e.g. ``datetime``, ``Path``,
        ``Decimal``). Earlier versions silently coerced these via
        ``default=str``, which round-tripped them as strings on replay
        and produced silent data corruption. Callers must ensure
        ``meta`` and ``payload`` dict fields contain JSON-native
        primitives before calling.
        """
        line = json.dumps(event.to_dict(), separators=(",", ":"))
        dir_fd = os.open(str(self._dir), os.O_RDONLY)
        try:
            fcntl.flock(dir_fd, fcntl.LOCK_EX)
            with self._path.open("a") as f:
                f.write(line)
                f.write("\n")
        finally:
            fcntl.flock(dir_fd, fcntl.LOCK_UN)
            os.close(dir_fd)

    def append_many(self, events: list[Event]) -> None:
        """Append a batch of events under a single flock acquisition.

        Used by the projector synthesis path (Phase 1 / Phase 2) where one
        catalog row produces multiple events (e.g. tombstoned row →
        ``DocumentRegistered`` + ``DocumentDeleted``). A single flock keeps
        the batch atomic with respect to other catalog writers.

        Like ``append``, raises ``TypeError`` on non-JSON-serializable
        payload values rather than silently coercing them.
        """
        if not events:
            return
        lines = [
            json.dumps(e.to_dict(), separators=(",", ":"))
            for e in events
        ]
        dir_fd = os.open(str(self._dir), os.O_RDONLY)
        try:
            fcntl.flock(dir_fd, fcntl.LOCK_EX)
            with self._path.open("a") as f:
                for line in lines:
                    f.write(line)
                    f.write("\n")
        finally:
            fcntl.flock(dir_fd, fcntl.LOCK_UN)
            os.close(dir_fd)

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

    def truncate(self) -> None:
        """Drop the entire event log. Test-only — production code never calls."""
        self._path.write_text("")
