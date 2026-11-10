# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine's migration, visible to every other nx command (nexus-mjmjl).

The engine runs its schema migration before it binds HTTP, so while it
migrates nothing answers, and on a large store that lasts 20 minutes or more.
The supervisor knows (``readiness`` tracks the phase) but only said so in its
own log: Sam's ``nx doctor`` sat 20 minutes writing to disk with no word that
a migration was running.

The supervisor now writes ``<config>/service/migration-status.json`` while the
readiness phase is MIGRATING (at most every :data:`WRITE_EVERY_S`) and removes
it when the phase ends or the wait ends. The record is one entry in the
engine's ``boot_work`` shape (nexus-f3yqd.3, agreed with the engine side):
``id``, ``what``, ``started_at``, ``done``, ``total``, ``unit``, ``step``,
``safe_to_stop``, ``on_stop``, plus ``updated`` for staleness. A record not
refreshed for :data:`STALE_S` (a supervisor that died mid-migration) is
ignored. :mod:`nexus.progress.boot_work` renders it. Stdlib only.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: A record not refreshed for this long is a dead supervisor's, not a migration.
STALE_S: float = 90.0
#: The supervisor rewrites the record at most this often.
WRITE_EVERY_S: float = 5.0
FILE_NAME = "migration-status.json"

ON_STOP = "the changeset in progress starts over at the next start; finished changesets are kept"


def status_path(config_dir: Path) -> Path:
    return config_dir / "service" / FILE_NAME


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def entry(*, changeset: str | None, pending: int | None, started: float, updated: float) -> dict[str, Any]:
    """The migration as one ``boot_work`` entry."""
    where = []
    if changeset:
        where.append(f"at {changeset}")
    if isinstance(pending, int):
        where.append(f"{pending} changeset(s) left")
    suffix = f" ({', '.join(where)})" if where else ""
    return {
        "id": "schema-migration",
        "what": f"migrating its database schema{suffix}",
        "started_at": iso(started),
        "done": None,
        "total": None,
        "unit": None,
        "step": None,
        "safe_to_stop": False,
        "on_stop": ON_STOP,
        "updated": updated,
    }


def write(config_dir: Path, record: dict[str, Any]) -> None:
    """Replace the record atomically. Never raises."""
    path = status_path(config_dir)
    tmp = path.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def clear(config_dir: Path) -> None:
    """The migration phase or the wait ended. Never raises."""
    try:
        status_path(config_dir).unlink()
    except OSError:
        pass


def read(config_dir: Path, *, clock: Callable[[], float] = time.time) -> dict[str, Any] | None:
    """The live record, or ``None`` when there is none or it is stale or malformed. Never raises."""
    try:
        record = json.loads(status_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("what"), str):
        return None
    updated = record.get("updated")
    if not isinstance(updated, (int, float)) or not math.isfinite(updated) or clock() - updated > STALE_S:
        return None
    return record


class Recorder:
    """The supervisor's side: call :meth:`tick` with each readiness tick's
    ``migration_active``, changeset and pending; :meth:`close` when the wait
    ends. Tracks when MIGRATING began, throttles writes, clears the record as
    soon as the phase is left."""

    def __init__(self, config_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._dir = config_dir
        self._clock = clock
        self._started: float | None = None
        self._last_write = -math.inf
        self._last_key: tuple[Any, ...] | None = None

    def tick(self, *, migrating: bool, changeset: str | None, pending: int | None) -> None:
        now = self._clock()
        if not migrating:
            if self._started is not None:
                self._started = None
                self._last_key = None
                clear(self._dir)
            return
        if self._started is None:
            self._started = now
        key = (changeset, pending)
        if key == self._last_key and now - self._last_write < WRITE_EVERY_S:
            return
        self._last_key, self._last_write = key, now
        write(self._dir, entry(changeset=changeset, pending=pending, started=self._started, updated=now))

    def close(self) -> None:
        self._started = None
        clear(self._dir)
