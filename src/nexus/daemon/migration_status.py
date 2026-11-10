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
import re
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
#: Changesets whose interruption costs the most (a long copy that restarts
#: from its first row): the notice says so in so many words (Sam lost hours
#: not knowing that stopping made it worse; wording agreed with the engine side).
DO_NOT_STOP_IDS: frozenset[str] = frozenset({"vectors-030-1"})
DO_NOT_STOP = "Do not stop nexus: stopping restarts this step from the beginning. Your data is safe."
#: The active statement and the sizes are read at most this often while migrating.
STEP_EVERY_S: float = 30.0
#: The walk steps of vectors-030-1, by the dollar tag of its top-level DO
#: block (pg_stat_activity shows the block, not the statement inside it);
#: wording from the engine side. Any other statement reads "finishing up".
WALK_STEPS: dict[str, str] = {
    "w0": "preparing",
    "w2": "creating the new per-model tables",
    "w3": "copying chunks into the per-model tables",
    "w5": "building the new tables' indexes",
    "w6": "checking the copy row by row",
    "w71": "swapping the new tables in",
    "w72": "swapping the new tables in",
    "w75": "renaming constraints and indexes",
    "w77": "recreating views, policies and grants",
}
#: The copy step, the only one whose database growth estimates progress.
COPY_STEP = "w3"
_TAG = re.compile(r"\$(w\d+)\$")


def walk_step(statement: str | None) -> tuple[str | None, str]:
    """(tag, plain words) for the executing statement of a walked changeset."""
    m = _TAG.search(statement or "")
    tag = m.group(1) if m else None
    return tag, WALK_STEPS.get(tag or "", "finishing up")


def copy_estimate(db_now: int, db_base: int, source: int) -> int | None:
    """Percent copied, from database growth since the step's first sample
    over the source table's size, capped at 99 until the changeset ends."""
    if source <= 0 or db_now < db_base:
        return None
    return min(99, int(100 * (db_now - db_base) / source))


def changeset_id(changeset: str | None) -> str | None:
    """``db/changelog/x.xml::vectors-030-1::author`` -> ``vectors-030-1``; a bare id stays."""
    if not changeset:
        return None
    parts = changeset.split("::")
    return parts[1] if len(parts) >= 3 else parts[0]


def status_path(config_dir: Path) -> Path:
    return config_dir / "service" / FILE_NAME


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def entry(
    *, changeset: str | None, pending: int | None, started: float, updated: float, step: str | None = None,
) -> dict[str, Any]:
    """The migration as one ``boot_work`` entry. *statement* is the head of the
    SQL the migrating backend is executing (``pg_stat_activity``), shown as the
    inner step. *step* is the inner step's line, already in plain words."""
    where = []
    if changeset:
        where.append(f"at {changeset_id(changeset)}")
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
        "step": {"what": step} if step else None,
        "safe_to_stop": False,
        "on_stop": ON_STOP,
        # Client-only field (the file source): a fixed line shown in place of
        # the generic stop advice.
        "notice": DO_NOT_STOP if changeset_id(changeset) in DO_NOT_STOP_IDS else None,
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

    def __init__(
        self, config_dir: Path, *, clock: Callable[[], float] = time.time,
        statement_probe: Callable[[], tuple[str | None, int | None, int | None]] | None = None,
    ) -> None:
        """*statement_probe* returns (executing statement head, database size,
        source table size); any part may be ``None``."""
        self._dir = config_dir
        self._clock = clock
        self._probe = statement_probe
        self._step: str | None = None
        self._last_probe = -math.inf
        self._base: tuple[str | None, int] | None = None  # (changeset, db size at its first sample)
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
        if self._probe is not None and changeset_id(changeset) in DO_NOT_STOP_IDS and (
            now - self._last_probe >= STEP_EVERY_S or key != self._last_key
        ):
            self._last_probe = now
            self._step = self._walk_step(changeset)
        elif changeset_id(changeset) not in DO_NOT_STOP_IDS:
            self._step = None
        self._last_key, self._last_write = key, now
        write(self._dir, entry(
            changeset=changeset, pending=pending, started=self._started, updated=now, step=self._step,
        ))

    def _walk_step(self, changeset: str | None) -> str | None:
        try:
            statement, db_size, source = self._probe()  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — the probe is advisory
            return None
        tag, words = walk_step(statement)
        if isinstance(db_size, int) and (self._base is None or self._base[0] != changeset):
            self._base = (changeset, db_size)
        if tag == COPY_STEP and isinstance(db_size, int) and isinstance(source, int) and self._base:
            pct = copy_estimate(db_size, self._base[1], source)
            if pct is not None:
                return f"{words}: about {pct}% copied (estimate)"
        return words

    def close(self) -> None:
        self._started = None
        clear(self._dir)
